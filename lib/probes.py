"""Probe runners — TCP, HTTP, docker. All read-only."""
import socket
import subprocess
from urllib import request, error


def tcp_probe(ip, port, timeout=5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, f"tcp {ip}:{port} ok"
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return False, f"tcp {ip}:{port} fail: {e}"


def http_probe(ip, port, path, timeout=10, expect_status=None, expect_body_substr=None):
    """HTTP probe.

    expect_status: int or list of ints — exact status code(s) required for OK.
                   If None, accept any 2xx-4xx (loose default for unclassified CTs).
    expect_body_substr: string — must appear in the response body for OK.
                        If None, body is not asserted.
    """
    url = f"http://{ip}:{port}{path}"
    req = request.Request(url, method="GET")
    expected = expect_status if expect_status is None or isinstance(expect_status, list) else [expect_status]

    def check_status(code):
        if expected is None:
            return 200 <= code < 500
        return code in expected

    try:
        with request.urlopen(req, timeout=timeout) as r:
            status = r.status
            body = r.read(8192).decode("utf-8", errors="replace") if expect_body_substr else ""
    except error.HTTPError as e:
        status = e.code
        try:
            body = e.read(8192).decode("utf-8", errors="replace") if expect_body_substr else ""
        except Exception:
            body = ""
    except (error.URLError, TimeoutError) as e:
        return False, f"http {url} fail: {e}"

    ok_status = check_status(status)
    ok_body = (expect_body_substr in body) if expect_body_substr else True
    detail = f"http {url} {status}"
    if expect_body_substr:
        detail += f" body_match={'yes' if ok_body else 'no'}"
    return (ok_status and ok_body), detail


def docker_running_probe(node, ctid, name=None):
    """Verify docker ps shows at least one running container inside the CT."""
    cmd = f"pct exec {ctid} -- bash -c 'command -v docker >/dev/null && docker ps --format \"{{{{.Names}}}}\" | head -5'"
    from .targets import remote
    rc, out, _ = remote(node, cmd, timeout=15)
    if rc != 0:
        return False, f"pct exec failed rc={rc}"
    names = [l for l in out.splitlines() if l.strip()]
    if not names:
        return False, "no running containers"
    return True, f"running: {','.join(names)}"


def run_all(ct, ip):
    """Execute all configured probes for a CT. Returns (overall_ok, details)."""
    probes = ct.get("probes", {}) or {}
    if not probes or not ip:
        return None, "no probes defined or ip unresolved"
    details = []
    overall = True
    for port in probes.get("tcp", []) or []:
        ok, det = tcp_probe(ip, port)
        details.append(det)
        overall = overall and ok
    for entry in probes.get("http", []) or []:
        if isinstance(entry, dict):
            ok, det = http_probe(
                ip, entry["port"], entry.get("path", "/"),
                expect_status=entry.get("expect_status"),
                expect_body_substr=entry.get("expect_body_substr"))
        else:
            ok, det = http_probe(ip, 80, str(entry))
        details.append(det)
        overall = overall and ok
    if probes.get("docker_running"):
        ok, det = docker_running_probe(ct["node"], ct["ctid"])
        details.append(det)
        overall = overall and ok
    return overall, "; ".join(details)
