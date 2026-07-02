# PatchWave — Phase 1

Self-contained PVE LXC patch runner. No PatchMon API dependency.
Reads inventory via `pvesh` / `pct`, scans pending updates via `apt`,
patches in-place via `pct exec`, snapshots via `pct snapshot`, reports via
`patchmon-agent` when present.

## Files

- `bin/patchwave-run` — main runner CLI
- `bin/patchwave-report` — daily Cowork report
- `bin/patchwave-status` — query current state
- `lib/` — runner internals (state, probes, pipeline, reporter, targets)
- `etc/targets.yaml` — tier classification + per-CT policy
- `etc/runner.conf` — runtime config (Cowork URL, dpkg opts, timeouts)
- `etc/personas/vulcan.md` — supervisor persona doc
- `state/current.json` — live run state (resumable)
- `state/runs/<id>/journal.jsonl` — append-only journal per run
- `systemd/patchwave-report.{service,timer}` — daily 07:15 trigger
- `/var/log/patchwave/report-YYYYMMDD.md` — fallback report copies

## Default safety posture

- `halt_after_canary = true` — runner halts after the first CT.
- CT 103: `stay_stopped` — runner refuses power-on.
- CT 125, CT 211: `never_auto` — runner refuses unless explicit `--ctid` lists them.
- Tier 3: `approval_gated` — flag rather than gate; expected to be invoked one CT at a time.
- All `pct snapshot` / `pct rollback` / `pct exec apt-get` / `pct start` / `pct stop`
  short-circuit in `--dry-run` mode.

## Go-live sequence (operator-driven; do not auto-execute)

On operator's explicit "go", and only then:

```bash
# 1. Recycle PatchMon so agent-reporting works (Phase-0 §2 carryover).
cd /opt/patchmon && docker compose down && docker compose up -d

# 2. Install the daily report timer (07:15).
cp /opt/patchwave/systemd/patchwave-report.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now patchwave-report.timer
systemctl list-timers patchwave-report.timer

# 3. Run the single Tier-0 canary against ct302. HALTS after one CT.
/opt/patchwave/bin/patchwave-run --tier 0 --ctid 302
/opt/patchwave/bin/patchwave-status

# 4. Operator review.
#    - Inspect: state/runs/<id>/journal.jsonl
#    - If patch.detail mentions NO_SECURITY_UPGRADES, ct302 had no surface to test;
#      run a second canary (see fallback list below) BEFORE releasing the queue.
#    - If patch.detail shows real upgrades applied + post_probe ok: release.
```

### Fallback canary candidates (if ct302 returns NO_SECURITY_UPGRADES)

Tier-0 stopped CTs likely to have a security surface (in priority order, all
low-blast):

1. `ct109 splunk-enterprise` — large legacy stack; high probability of stale base packages.
2. `ct300 elk-demo` — Elasticsearch-era Java stack; usually patches accumulate.
3. `ct110 atlas-networkvisualizer` — long-running demo CT; base OS likely behind.

Run: `patchwave-run --tier 0 --ctid <chosen> --run-id canary-2`

### Release and continue

```bash
# 5. Release the canary halt.
patchwave-run --release --run-id <id-from-status>

# 6. Run the rest of Tier-0 (still one-at-a-time with halt_after_canary=true).
patchwave-run --tier 0
```

## Reporting

- Phase 1 default: fallback markdown at `/var/log/patchwave/report-YYYYMMDD.md`.
  Daily 07:15 via systemd timer.
- Optional Slack push: set `report.slack_webhook_url` in `etc/runner.conf` to a
  Slack incoming-webhook URL. Empty (default) = no push, no failure.
- Cowork narrative is a later layer; not in Phase 1.

## Snapshot strategy (per-CT, bind-aware)

PVE 9.1.9 LXC `has_feature('snapshot')` walks **every** current-config
mountpoint and rejects if any volume can't snap. There is no flag-based
exclusion:

- `backup=0` is **vzdump-only** (`AbstractConfig.pm:755` calls
  `has_feature(..., $snapname eq 'vzdump')`, and `Config.pm:103` only honors
  `backup=0` when that flag is truthy).
- `--excludevol` does not exist on `pct snapshot`.

The only flag-based workaround is detaching the mountpoint from the live
config (`pct set <ctid> --delete mpN`) before snapshotting and re-attaching
after. PVE pending-config semantics matter:

- **Stopped CTs**: `--delete` applies live → snapshot succeeds. Verified on
  ct110 (2026-06-17).
- **Running CTs**: `--delete` writes `[pve:pending] delete: mpN`; `--current`
  config is unchanged; snapshot still rejected. Verified on ct101 (2026-06-17).
  Reattach by re-setting the mp to the same value cancels the pending change
  with no restart.

Per-CT strategy lives in `state/binds_classified.json` under `snap_strategy`:
`no-bind`, `detach-snap-attach` (stopped or stay_stopped),
`PENDING_OPERATOR_DECISION` (running with bind), or `DEFERRED` (rw data binds).

When the runner implements `detach-snap-attach`, reattach is an unconditional
restore on every exit path (modeled on `power_restore`), and startup
reconciliation re-attaches expected binds if a prior run was interrupted —
both invariants come from the persisted target's `binds_expected` list.

## Pre-go-live carryovers

- PatchMon Redis/asynq degradation (Phase-0 §2): cleared by step 1 above.
- Tier 3 probes for ct100, ct125, ct140 are now strict (status + body). Other
  Tier-3 entries (ct777, ct1906, ct211) carry a `refine:` flag; tighten before
  their gated runs.
- Vulcan endpoint stays disabled in Phase 1. Scaffold the receiver only after
  the runner has proven across Tiers 0–2 AND ct211 has been patched.
