"""Load targets.yaml and resolve per-CT IPs at runtime."""
import re
import subprocess
import yaml

NODE1_SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
             "root@<PROXMOX_NODE1_IP>"]


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def remote(node, cmd, timeout=20, check=False):
    """Run cmd on node1 (via ssh) or node2 (locally)."""
    if node == "node1":
        argv = NODE1_SSH + [cmd]
    else:
        argv = ["bash", "-c", cmd]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"remote failed (rc={p.returncode}): {cmd}\n{p.stderr}")
    return p.returncode, p.stdout, p.stderr


def ct_ip(node, ctid):
    """Resolve CT IP. Prefer a literal `ip=A.B.C.D` from `pct config` net0/net1.
    If every interface is `ip=dhcp` (and the CT is running), fall back to
    `pct exec <ctid> -- hostname -I` and pick the first 192.168.x.x token.
    Returns None if no IP can be resolved — callers must treat None as a
    HARD FAILURE when probes are declared (not a silent skip)."""
    rc, out, _ = remote(node, f"pct config {ctid}")
    static_ip = None
    is_dhcp = False
    for line in out.splitlines():
        if line.startswith("net0:") or line.startswith("net1:"):
            m = re.search(r"ip=([0-9.]+)", line)
            if m:
                static_ip = m.group(1)
                break
            if "ip=dhcp" in line:
                is_dhcp = True
    if static_ip:
        return static_ip
    if not is_dhcp:
        return None
    # DHCP fallback — needs CT running; pct exec returns non-zero if stopped.
    try:
        rc2, out2, _ = remote(node, f"pct exec {ctid} -- hostname -I", timeout=10)
    except Exception:
        return None
    if rc2 != 0:
        return None
    for tok in out2.split():
        # LAN only — skip docker bridges (172.17.x), link-local, loopback,
        # and IPv6.
        if tok.startswith("192.168."):
            return tok
    return None


def by_tier(targets, tier):
    return [c for c in targets["cts"] if c["tier"] == tier]


def by_ctid(targets, ctid):
    for c in targets["cts"]:
        if c["ctid"] == ctid:
            return c
    return None


def is_never_auto(targets, ctid):
    return ctid in (targets.get("policy", {}).get("never_auto") or [])


def is_stay_stopped(targets, ctid):
    return ctid in (targets.get("policy", {}).get("stay_stopped") or [])


def is_approval_gated(targets, ctid):
    return ctid in (targets.get("policy", {}).get("approval_gated_tier3") or [])
