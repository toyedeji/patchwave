"""PatchWave per-CT pipeline.

Canonical order per brief:
  verify -> snapshot -> pre_probe -> [power_on] -> patch -> post_probe ->
  [rollback?] -> power_restore -> agent_report

Stopped-CT subflow: if pre_state == stopped, power_on before patch,
patch via pct exec, then power_off in power_restore.
"""
import shlex
import subprocess
import time

from . import probes
from .targets import remote, ct_ip, is_stay_stopped, is_never_auto


class Aborted(Exception):
    pass


def _power_state(node, ctid):
    rc, out, _ = remote(node, f"pct status {ctid}")
    return "running" if "running" in out else "stopped"


def _snapshot_name(prefix, run_id):
    return f"{prefix}-{run_id}"


def _kill_apt_in_ct(node, ctid):
    """After a host-side timeout on `pct exec ... apt-get`, the apt process tree
    inside the CT can still be alive (the local subprocess died, the in-CT one
    didn't get a signal). SIGTERM → SIGKILL the tree and clear dpkg locks.
    Read-write inside the CT; required to leave the CT in a recoverable state
    before rollback. Returns a short status string."""
    inner = (
        "set +e; "
        "APT_PIDS=$(pgrep -f 'apt-get|/usr/lib/apt/methods|dpkg' 2>/dev/null); "
        "if [ -n \"$APT_PIDS\" ]; then "
        "  kill -TERM $APT_PIDS 2>/dev/null; "
        "  sleep 5; "
        "  kill -KILL $APT_PIDS 2>/dev/null; "
        "  sleep 1; "
        "fi; "
        "STILL=$(pgrep -f 'apt-get|/usr/lib/apt/methods|dpkg' 2>/dev/null | wc -l); "
        "rm -f /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock; "
        "echo apt-still-alive=$STILL locks-cleared=yes"
    )
    cmd = f"pct exec {ctid} -- bash -c {shlex.quote(inner)}"
    try:
        rc, out, err = remote(node, cmd, timeout=30)
        return f"kill-apt rc={rc} out={out.strip()[:140]}"
    except Exception as e:
        return f"kill-apt exception (non-fatal): {e!r}"


def step_verify(state, ct, cfg, dry):
    with state.step(ct["ctid"], "verify") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        rc, out, _ = remote(node, f"pct status {ctid}")
        actual = "running" if "running" in out else "stopped"
        expected = ct.get("pre_state", "running")
        if actual != expected:
            r["status"] = "fail"
            r["detail"] = f"expected {expected}, found {actual} — operator-confirm before proceeding"
            return False
        r["status"] = "ok"
        r["detail"] = f"state={actual}"
        return True


def _has_snapshot_recovery(ct):
    """True when this CT will have an LVM snapshot to roll back to. False for
    fix-forward-enhanced (no snapshot taken) and deferred (run refused).
    detach-snap-attach DOES create a snapshot but is not yet wired in the
    pipeline; step_snapshot will refuse, so this still returns True (so
    callers correctly treat a refused snapshot as a snapshot-having CT that
    just hasn't been built yet — not as a fix-forward path)."""
    return ct.get("snap_strategy", "no-bind") in ("no-bind", "detach-snap-attach")


def step_snapshot(state, ct, cfg, dry, run_id):
    """Dispatch on ct['snap_strategy']:
      - no-bind: plain `pct snapshot`.
      - fix-forward-enhanced: skip snapshot; step_pre_patch_capture provides the
        recovery baseline instead. Reported as patched WITHOUT snapshot.
      - detach-snap-attach: NOT YET WIRED. Refuse so the operator doesn't
        accidentally exercise this path on stopped ro-bind CTs (110/120/200/103)
        before it's built.
      - deferred: refuse — CT is not in the current patch path (rw-bind survival
        contract not signed off).
    """
    strategy = ct.get("snap_strategy", "no-bind")
    with state.step(ct["ctid"], "snapshot") as r:
        if r == "skipped":
            return True
        if strategy == "fix-forward-enhanced":
            r["status"] = "ok"
            r["detail"] = (
                "skipped: fix-forward-enhanced (no snapshot; recovery via "
                "pre_patch_capture + retained debs)"
            )
            return True
        if strategy == "deferred":
            r["status"] = "fail"
            r["detail"] = "snap_strategy=deferred — CT excluded from current patch path"
            return False
        if strategy == "detach-snap-attach":
            r["status"] = "fail"
            r["detail"] = (
                "snap_strategy=detach-snap-attach not yet wired in pipeline "
                "(Tier-0 retry for 110/120/200 / Tier-3 103 blocked until built)"
            )
            return False
        # no-bind: plain pct snapshot
        node, ctid = ct["node"], ct["ctid"]
        name = _snapshot_name(cfg["runner"]["snapshot_prefix"], run_id)
        cmd = f'pct snapshot {ctid} {name} --description "patchwave run {run_id}"'
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: {cmd}"
            return True
        rc, out, err = remote(node, cmd, timeout=120)
        if rc != 0:
            r["status"] = "fail"
            r["detail"] = f"rc={rc} stderr={err.strip()[:200]}"
            return False
        r["status"] = "ok"
        r["detail"] = f"snapshot={name}"
        return True


def step_pre_patch_capture(state, ct, cfg, dry, run_id):
    """Capture pre-patch state for fix-forward-enhanced CTs (no snapshot, no
    automatic rollback). Records inside the CT:
      - apt list --upgradable (security-tagged only) — old + new versions
      - dpkg -l for each security pkg — exact old version strings
      - dpkg --audit baseline — pre-existing audit findings (so post-patch
        diff is unambiguous)
      - apt-get check baseline — pre-existing dep tree state

    Writes to state/runs/<run_id>/ct<ctid>-pre_patch.txt. Failure aborts the
    run before patching — refuse to patch a fix-forward CT without baseline."""
    if ct.get("snap_strategy") != "fix-forward-enhanced":
        return True
    with state.step(ct["ctid"], "pre_patch_capture") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        out_path = state.run_dir / f"ct{ctid}-pre_patch.txt"
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: capture dpkg/apt baseline to {out_path}"
            return True
        inner = (
            "set +e; "
            "echo '=== apt list --upgradable (security only) ==='; "
            "apt list --upgradable 2>/dev/null | awk -F/ '/-security/ {print}'; "
            "echo; echo '=== dpkg -l for security upgrade set ==='; "
            "SEC=$(apt list --upgradable 2>/dev/null | tail -n +2 | "
            "      awk -F/ '/-security/ {print $1}'); "
            "if [ -n \"$SEC\" ]; then dpkg -l $SEC 2>&1; "
            "else echo NO_SECURITY_UPGRADES; fi; "
            "echo; echo '=== dpkg --audit baseline ==='; "
            "dpkg --audit 2>&1; "
            "echo; echo '=== apt-get check baseline ==='; "
            "apt-get check 2>&1; echo \"rc=$?\""
        )
        cmd = f"pct exec {ctid} -- bash -c {shlex.quote(inner)}"
        try:
            rc, out, err = remote(node, cmd, timeout=60)
        except Exception as e:
            r["status"] = "fail"
            r["detail"] = f"pre_patch_capture exception: {e!r}"
            return False
        header = f"# pre_patch capture ct{ctid} run_id={run_id}\n\n"
        out_path.write_text(header + out + "\n--- stderr ---\n" + err + "\n")
        # Surface NO_SECURITY_UPGRADES at the journal level too so the operator
        # doesn't have to grep the capture file to know the run was a no-op.
        no_sec = "NO_SECURITY_UPGRADES" in out
        r["status"] = "ok"
        r["detail"] = (
            f"baseline -> {out_path}"
            + ("; no security upgrades pending" if no_sec else "")
        )
        return True


def step_pre_probe(state, ct, cfg, dry):
    with state.step(ct["ctid"], "pre_probe") as r:
        if r == "skipped":
            return True
        if ct.get("pre_state") == "stopped":
            r["status"] = "ok"
            r["detail"] = "skipped: pre_state=stopped (probe not applicable)"
            return True
        ip = ct_ip(ct["node"], ct["ctid"])
        probes_cfg = ct.get("probes") or {}
        if probes_cfg and not ip:
            r["status"] = "fail"
            r["detail"] = (
                f"probes declared ({list(probes_cfg.keys())}) but IP unresolved "
                f"for ct{ct['ctid']} — refusing to silently skip"
            )
            return False
        ok, det = probes.run_all(ct, ip)
        if ok is None:
            r["status"] = "ok"
            r["detail"] = f"no-probes-defined; ip={ip}; {det}"
            return True
        r["status"] = "ok" if ok else "fail"
        r["detail"] = f"ip={ip}; {det}"
        return ok


def step_power_on(state, ct, cfg, dry):
    """Only invoked when pre_state == stopped, before patch."""
    if ct.get("pre_state") != "stopped":
        return True
    if is_stay_stopped(state.data.get("targets_policy", {}), ct["ctid"]) or ct.get("stay_stopped"):
        with state.step(ct["ctid"], "power_on") as r:
            if r == "skipped":
                return True
            r["status"] = "fail"
            r["detail"] = "stay_stopped policy — refusing to power on"
        return False
    with state.step(ct["ctid"], "power_on") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: pct start {ctid} && wait-for-boot"
            return True
        rc, out, err = remote(node, f"pct start {ctid}", timeout=60)
        if rc != 0:
            r["status"] = "fail"
            r["detail"] = f"pct start rc={rc} {err.strip()[:200]}"
            return False
        # Wait for systemd to settle / boot to complete
        wait = int(cfg["probes"]["boot_wait_sec"])
        time.sleep(wait)
        r["status"] = "ok"
        r["detail"] = f"started; waited {wait}s for boot"
        return True


def step_dns_check(state, ct, cfg, dry):
    """Fast DNS-fail preflight. Runs after power_on (and before patch) for stopped
    pre_state, and for running CTs as a sanity check. Hard 6s budget — apt-get
    cannot complete if DNS to package mirrors is broken, so bail clearly here
    rather than waiting 15min on apt-get to time out."""
    with state.step(ct["ctid"], "dns_check") as r:
        if r == "skipped":
            return True
        if dry:
            r["status"] = "ok"
            r["detail"] = "WOULD: pct exec ... timeout 6 getent hosts deb.debian.org"
            return True
        node, ctid = ct["node"], ct["ctid"]
        inner = "timeout 8 getent hosts deb.debian.org >/dev/null 2>&1 && echo OK || echo TIMEOUT_OR_FAIL"
        cmd = f"pct exec {ctid} -- bash -c {shlex.quote(inner)}"
        try:
            rc, out, err = remote(node, cmd, timeout=15)
        except Exception as e:
            r["status"] = "fail"
            r["detail"] = f"dns_check exception: {e!r}"
            return False
        out_s = out.strip()
        if "OK" in out_s and "TIMEOUT_OR_FAIL" not in out_s:
            r["status"] = "ok"
            r["detail"] = "getent deb.debian.org < 6s"
            return True
        r["status"] = "fail"
        r["detail"] = f"DNS broken in CT (cannot resolve deb.debian.org within 6s); out={out_s[:120]}"
        return False


def step_snapshot_delete(state, ct, cfg, dry, run_id):
    """Delete the per-run snapshot after pipeline completion. Non-fatal — if
    deletion fails, the run is still successful; operator may delete manually."""
    with state.step(ct["ctid"], "snapshot_delete") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        name = _snapshot_name(cfg["runner"]["snapshot_prefix"], run_id)
        cmd = f"pct delsnapshot {ctid} {name}"
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: {cmd}"
            return True
        try:
            rc, out, err = remote(node, cmd, timeout=60)
        except Exception as e:
            r["status"] = "warn"
            r["detail"] = f"delsnapshot exception (non-fatal): {e!r}"
            return True
        if rc != 0:
            r["status"] = "warn"
            r["detail"] = f"delsnapshot rc={rc} {err.strip()[:200]} (non-fatal)"
            return True
        r["status"] = "ok"
        r["detail"] = f"deleted snapshot {name}"
        return True


def step_patch(state, ct, cfg, dry):
    with state.step(ct["ctid"], "patch") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        # security-only: list packages from security suite, install --only-upgrade
        # honor holds: apt-mark holds are honored by apt-get install --only-upgrade by default
        dpkg_opts = cfg["patch"]["dpkg_options"].split(",")
        dpkg_flags = " ".join(f"-o Dpkg::Options::={shlex.quote(o)}" for o in dpkg_opts)
        # We compute upgradable security pkgs inside the CT and pass them to apt-get install --only-upgrade.
        # Important: NO apt-get clean / autoclean / autoremove anywhere. For
        # fix-forward-enhanced CTs the retained debs in /var/cache/apt/archives/
        # are the manual-rollback path — operator can `dpkg -i <old.deb>` to
        # downgrade if the security upgrade regresses the app. For snapshot
        # CTs the retained debs cost nothing.
        inner = (
            "set -e; "
            "command -v apt >/dev/null || { echo NO_APT; exit 0; }; "
            # MANDATORY index refresh — without this, security packages whose
            # point versions have been superseded upstream return 404 (e.g.
            # ct300 elk-demo's nano_7.2-2ubuntu0.1). Budget 60s; on timeout
            # we still proceed off cache. Fleet-wide fix.
            "DEBIAN_FRONTEND=noninteractive timeout 60 apt-get -q update 2>/dev/null || echo UPDATE_TIMEOUT_OR_FAIL; "
            "SEC=$(apt list --upgradable 2>/dev/null | tail -n +2 | "
            "  awk -F/ '/-security/ {print $1}'); "
            "if [ -z \"$SEC\" ]; then echo NO_SECURITY_UPGRADES; exit 0; fi; "
            f"DEBIAN_FRONTEND=noninteractive apt-get -y {dpkg_flags} --only-upgrade install $SEC"
        )
        cmd = f"pct exec {ctid} -- bash -c {shlex.quote(inner)}"
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: {cmd[:200]}..."
            return True
        try:
            rc, out, err = remote(node, cmd, timeout=900)
        except subprocess.TimeoutExpired:
            kill_status = _kill_apt_in_ct(node, ctid)
            r["status"] = "fail"
            r["detail"] = f"apt-get exceeded 900s host timeout; killed in-CT apt tree and cleared dpkg lock; {kill_status}"
            return False
        out_tail = (out + err)[-400:]
        if "NO_APT" in out:
            r["status"] = "ok"
            r["detail"] = "no apt in CT — nothing to patch"
            return True
        if "NO_SECURITY_UPGRADES" in out:
            r["status"] = "ok"
            r["detail"] = "no security upgrades pending"
            return True
        if rc != 0:
            r["status"] = "fail"
            r["detail"] = f"apt-get rc={rc} tail={out_tail}"
            return False
        r["status"] = "ok"
        r["detail"] = f"patched; tail={out_tail[-200:]}"
        return True


def step_post_probe(state, ct, cfg, dry):
    """Two regimes:

    - pre_state == stopped: app may not auto-start, so app-level TCP/HTTP
      probes would false-positive. Instead, run an OS-integrity check
      INSIDE the CT (while still powered on, before power_restore stops it):
      `dpkg --audit` must be empty AND `apt-get check` must exit 0. This
      proves the package database is consistent and dependency tree is
      satisfied — the meaningful post-patch invariant for a stopped CT.
    - pre_state == running: app-level probes (TCP/HTTP) per targets.yaml.
    """
    settle = int(cfg["probes"]["post_patch_settle_sec"])
    if not dry:
        time.sleep(settle)
    with state.step(ct["ctid"], "post_probe") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        if ct.get("pre_state") == "stopped":
            if dry:
                r["status"] = "ok"
                r["detail"] = "WOULD: dpkg --audit + apt-get check inside CT"
                return True
            inner = (
                "AUDIT=$(dpkg --audit 2>&1); "
                "CHECK_OUT=$(apt-get check 2>&1); CHECK_RC=$?; "
                "if [ -z \"$AUDIT\" ] && [ $CHECK_RC -eq 0 ]; then echo OK; else "
                "echo FAIL; echo \"--- dpkg --audit ---\"; echo \"$AUDIT\"; "
                "echo \"--- apt-get check (rc=$CHECK_RC) ---\"; echo \"$CHECK_OUT\"; fi"
            )
            cmd = f"pct exec {ctid} -- bash -c {shlex.quote(inner)}"
            try:
                rc, out, err = remote(node, cmd, timeout=60)
            except Exception as e:
                r["status"] = "fail"
                r["detail"] = f"os_integrity exception: {e!r}"
                return False
            head = out.strip().splitlines()[0] if out.strip() else ""
            if head == "OK":
                r["status"] = "ok"
                r["detail"] = "os_integrity: dpkg --audit clean; apt-get check rc=0"
                return True
            r["status"] = "fail"
            r["detail"] = f"os_integrity FAIL: {out.strip()[:300]}"
            return False
        ip = ct_ip(node, ctid)
        probes_cfg = ct.get("probes") or {}
        if probes_cfg and not ip:
            r["status"] = "fail"
            r["detail"] = (
                f"probes declared ({list(probes_cfg.keys())}) but IP unresolved "
                f"for ct{ctid} — refusing to silently skip"
            )
            return False
        ok, det = probes.run_all(ct, ip)
        if ok is None:
            r["status"] = "ok"
            r["detail"] = f"no-probes-defined; ip={ip}; {det}"
            return True
        r["status"] = "ok" if ok else "fail"
        r["detail"] = f"ip={ip}; {det}"
        return ok


def step_rollback(state, ct, cfg, dry, run_id):
    """Stop CT before rolling back. Rolling back underneath a live process
    tree (apt or otherwise) is unsafe — the running rootfs is the source the
    rollback target is replacing. We `pct stop` (force if needed) first, then
    rollback to the snapshot."""
    with state.step(ct["ctid"], "rollback") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        name = _snapshot_name(cfg["runner"]["snapshot_prefix"], run_id)
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: pct stop {ctid} (if running); pct rollback {ctid} {name}"
            return True
        # Stop first if running. Use pct stop (force). Tolerate non-zero rc
        # in the rare race where CT shuts down between status check and stop.
        if _power_state(node, ctid) == "running":
            try:
                rc_s, out_s, err_s = remote(node, f"pct stop {ctid}", timeout=60)
                stop_detail = f"stopped (rc={rc_s})"
            except Exception as e:
                stop_detail = f"stop exception (proceeding): {e!r}"
        else:
            stop_detail = "already stopped"
        # Rollback
        try:
            rc, out, err = remote(node, f"pct rollback {ctid} {name}", timeout=180)
        except Exception as e:
            r["status"] = "fail"
            r["detail"] = f"rollback exception: {e!r}; pre-stop: {stop_detail}"
            return False
        if rc != 0:
            r["status"] = "fail"
            r["detail"] = f"rollback rc={rc} stderr={err.strip()[:200]}; pre-stop: {stop_detail}"
            return False
        r["status"] = "ok"
        r["detail"] = f"{stop_detail}; rolled back to {name}"
        return True


def step_power_restore(state, ct, cfg, dry):
    with state.step(ct["ctid"], "power_restore") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        target = ct.get("pre_state", "running")
        actual = _power_state(node, ctid)
        if actual == target:
            r["status"] = "ok"
            r["detail"] = f"already {target}"
            return True
        if target == "stopped":
            cmd = f"pct stop {ctid}"
        else:
            cmd = f"pct start {ctid}"
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: {cmd}"
            return True
        rc, out, err = remote(node, cmd, timeout=60)
        if rc != 0:
            r["status"] = "fail"
            r["detail"] = f"rc={rc} stderr={err.strip()[:200]}"
            return False
        r["status"] = "ok"
        r["detail"] = f"restored to {target}"
        return True


def step_agent_report(state, ct, cfg, dry):
    """Non-fatal final step. Per guardrail: a failed agent_report never fails
    the run and never triggers rollback. Failures are marked 'warn' (not 'fail')
    so the journal shows the dashboard didn't refresh, but the patch outcome
    (snapshot + apt + green post_probe) defines run success."""
    with state.step(ct["ctid"], "agent_report") as r:
        if r == "skipped":
            return True
        node, ctid = ct["node"], ct["ctid"]
        if _power_state(node, ctid) == "stopped":
            r["status"] = "ok"
            r["detail"] = "skipped: CT stopped post-restore; out-of-band report required"
            return True
        cmd = f"pct exec {ctid} -- bash -c 'command -v patchmon-agent >/dev/null && patchmon-agent report || echo NO_AGENT'"
        if dry:
            r["status"] = "ok"
            r["detail"] = f"WOULD: {cmd}"
            return True
        try:
            rc, out, err = remote(node, cmd, timeout=60)
        except Exception as e:
            r["status"] = "warn"
            r["detail"] = f"agent_report exception (non-fatal): {e!r}"
            return True
        if "NO_AGENT" in out:
            r["status"] = "ok"
            r["detail"] = "no patchmon-agent installed; skipped"
            return True
        if rc != 0:
            r["status"] = "warn"
            r["detail"] = f"agent_report rc={rc} (non-fatal; run still successful) {err.strip()[:200]}"
            return True
        r["status"] = "ok"
        r["detail"] = "agent report sent"
        return True


def _rollback_and_restore(state, ct, cfg, dry, run_id):
    """Recover path: rollback snapshot → power_restore → delete snapshot.
    Called on patch failure and post_probe regression. Each sub-step is
    non-fatal at this level — best-effort recovery, all journaled."""
    step_rollback(state, ct, cfg, dry, run_id)
    step_power_restore(state, ct, cfg, dry)
    step_snapshot_delete(state, ct, cfg, dry, run_id)


def run_pipeline(state, ct, cfg, dry, run_id):
    """Execute full per-CT pipeline. Returns terminal:
        'done' | 'done_with_rollback' | 'regressed' | 'aborted'.

    Snapshot semantics depend on ct['snap_strategy']:
      - no-bind / detach-snap-attach: snapshot exists → rollback available;
        snapshot_delete in cleanup paths.
      - fix-forward-enhanced: NO snapshot. step_pre_patch_capture runs first
        to record dpkg/apt baseline. On patch or post_probe failure: halt +
        mark_ct_done — no automatic rollback. The CT is left in whatever
        state apt/post-probe left it; operator inspects pre_patch capture
        and decides (downgrade via retained debs, hand-fix, etc).
      - deferred: step_snapshot refuses → run aborts at the snapshot step.

    Failure-recovery policy:
      - verify / snapshot / pre_patch_capture / pre_probe / power_on /
        dns_check fail: abort, no rollback (nothing was committed); attempt
        snapshot_delete only if a snapshot was actually created.
      - patch fail (snap CT): unconditional rollback + power_restore +
        snapshot_delete; terminal=aborted.
      - patch fail (fix-forward CT): halt + power_restore; terminal=aborted.
      - post_probe fail (snap CT): rollback + power_restore + snapshot_delete;
        terminal=done_with_rollback.
      - post_probe fail (fix-forward CT): halt; CT left as-is (do NOT auto-
        restore power) so operator can introspect; terminal=regressed.
      - Happy path: snapshot_delete (if snap CT) after agent_report +
        power_restore; terminal=done.
    """
    ctid = ct["ctid"]
    snap_ok = _has_snapshot_recovery(ct)

    if not step_verify(state, ct, cfg, dry):
        state.mark_ct_done(ctid, "aborted")
        return "aborted"

    if not step_snapshot(state, ct, cfg, dry, run_id):
        state.mark_ct_done(ctid, "aborted")
        return "aborted"

    # Pre-patch baseline capture — fix-forward CTs only; no-op otherwise.
    if not step_pre_patch_capture(state, ct, cfg, dry, run_id):
        # Couldn't capture baseline — refuse to patch a fix-forward CT blind.
        # No snapshot exists (we're on the fix-forward path), so nothing to
        # delete. Leave power as-is and abort.
        state.mark_ct_done(ctid, "aborted")
        return "aborted"

    if not step_pre_probe(state, ct, cfg, dry):
        if snap_ok:
            step_snapshot_delete(state, ct, cfg, dry, run_id)
        state.mark_ct_done(ctid, "aborted")
        return "aborted"

    if ct.get("pre_state") == "stopped":
        if not step_power_on(state, ct, cfg, dry):
            if snap_ok:
                step_snapshot_delete(state, ct, cfg, dry, run_id)
            state.mark_ct_done(ctid, "aborted")
            return "aborted"

    if not step_dns_check(state, ct, cfg, dry):
        step_power_restore(state, ct, cfg, dry)
        if snap_ok:
            step_snapshot_delete(state, ct, cfg, dry, run_id)
        state.mark_ct_done(ctid, "aborted")
        return "aborted"

    if not step_patch(state, ct, cfg, dry):
        if snap_ok:
            _rollback_and_restore(state, ct, cfg, dry, run_id)
        else:
            # Fix-forward: no snapshot to roll back. Halt the queue so the
            # operator inspects the pre_patch capture and decides recovery.
            state.mark_halt(
                f"ct{ctid} patch FAILED on fix-forward CT — no snapshot rollback. "
                f"Inspect {state.run_dir}/ct{ctid}-pre_patch.txt and decide manual recovery."
            )
            step_power_restore(state, ct, cfg, dry)
        state.mark_ct_done(ctid, "aborted")
        return "aborted"

    if not step_post_probe(state, ct, cfg, dry):
        if snap_ok:
            _rollback_and_restore(state, ct, cfg, dry, run_id)
            state.mark_ct_done(ctid, "done_with_rollback")
            return "done_with_rollback"
        # Fix-forward regression: patch succeeded but app post-probe failed.
        # No rollback. Halt and leave the CT in its current state for
        # operator inspection — do NOT auto-restore power.
        state.mark_halt(
            f"ct{ctid} post_probe REGRESSION after fix-forward patch — no snapshot "
            f"rollback. CT left running for inspection. Baseline: "
            f"{state.run_dir}/ct{ctid}-pre_patch.txt"
        )
        state.mark_ct_done(ctid, "regressed")
        return "regressed"

    # Happy path. Stopped pre_state → agent_report before power_restore so the
    # dashboard reflects the patched state while CT is still up. Running-CT
    # ordering unchanged. Snapshot deletion is the final step (snap CTs only).
    if ct.get("pre_state") == "stopped":
        step_agent_report(state, ct, cfg, dry)
        step_power_restore(state, ct, cfg, dry)
    else:
        step_power_restore(state, ct, cfg, dry)
        step_agent_report(state, ct, cfg, dry)
    if snap_ok:
        step_snapshot_delete(state, ct, cfg, dry, run_id)
    state.mark_ct_done(ctid, "done")
    return "done"
