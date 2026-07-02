"""PatchWave state machine + resumable journal.

Per-CT pipeline steps (canonical order):
  verify -> snapshot -> pre_probe -> [power_on] -> patch -> post_probe ->
  [rollback?] -> power_restore -> agent_report -> done

Each step transitions PENDING -> OK or FAIL. Journal is append-only JSONL.
Resumability: on startup, runner reads current.json and any CT with step
status PENDING resumes from that step. OK steps are skipped.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

STEPS = [
    "verify",
    "snapshot",
    "pre_probe",
    "power_on",     # only when pre_state == stopped
    "patch",
    "post_probe",
    "rollback",     # conditional, only on post_probe FAIL
    "power_restore",
    "agent_report",
]

TERMINAL_OK = {"done"}
TERMINAL_FAIL = {"done_with_rollback", "aborted", "halted"}


class HaltedRunBlocked(Exception):
    """A prior halted run hasn't been released; refuse new runs."""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class RunState:
    def __init__(self, root: Path, run_id: str):
        self.root = Path(root)
        self.run_id = run_id
        self.run_dir = self.root / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.current_file = self.root / "current.json"
        self.journal_file = self.run_dir / "journal.jsonl"
        fresh = {"run_id": run_id, "started": now(), "cts": {}, "halted": False, "canary_released": False}
        self.data = fresh
        if self.current_file.exists():
            try:
                persisted = json.loads(self.current_file.read_text())
            except json.JSONDecodeError:
                persisted = None
            if persisted:
                if persisted.get("run_id") == run_id:
                    # resume
                    self.data = persisted
                else:
                    # different run requested — only allow if prior was released or terminal
                    if persisted.get("halted") and not persisted.get("canary_released"):
                        raise HaltedRunBlocked(
                            f"prior run {persisted.get('run_id')} is halted and not released; "
                            f"run `patchwave-run --release --run-id {persisted.get('run_id')}` first")
                    # archive prior run's final state, then start fresh
                    prior_dir = self.root / "runs" / persisted.get("run_id", "unknown")
                    prior_dir.mkdir(parents=True, exist_ok=True)
                    (prior_dir / "state.json.final").write_text(json.dumps(persisted, indent=2))
                    self.data = fresh

    def save(self):
        tmp = self.current_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.current_file)

    def log(self, ctid, step, status, **detail):
        rec = {"ts": now(), "ctid": ctid, "step": step, "status": status, **detail}
        with open(self.journal_file, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def ensure_ct(self, ctid):
        key = str(ctid)
        if key not in self.data["cts"]:
            self.data["cts"][key] = {"ctid": ctid, "status": "pending", "steps": {}}
        return self.data["cts"][key]

    def get_step_status(self, ctid, step):
        ct = self.ensure_ct(ctid)
        return ct["steps"].get(step, {}).get("status", "pending")

    @contextmanager
    def step(self, ctid, step):
        """Wrap a step execution; records start, marks pending, captures result.

        Exception policy: any exception inside the with block is captured to
        the step's status/detail and SWALLOWED. The pipeline must inspect step
        return values or step-status afterward to drive control flow — never
        rely on exceptions to abort. This was a real bug: a re-raise here
        crashed the runner mid-pipeline on a single apt timeout.
        """
        ct = self.ensure_ct(ctid)
        prior = ct["steps"].get(step, {}).get("status")
        if prior == "ok":
            yield "skipped"
            return
        ct["steps"][step] = {"status": "pending", "started": now()}
        self.save()
        self.log(ctid, step, "started", prior=prior)
        result = {"status": "fail", "detail": ""}
        try:
            yield result
        except Exception as e:
            result["status"] = "fail"
            result["detail"] = f"exception: {e!r}"
            # SWALLOW — do not re-raise. Caller drives flow via step status.
        finally:
            ct["steps"][step]["status"] = result["status"]
            ct["steps"][step]["ended"] = now()
            ct["steps"][step]["detail"] = result.get("detail", "")
            self.save()
            self.log(ctid, step, result["status"], detail=result.get("detail", ""))

    def mark_ct_done(self, ctid, terminal):
        ct = self.ensure_ct(ctid)
        ct["status"] = terminal
        ct["ended"] = now()
        self.save()
        self.log(ctid, "*", terminal)

    def mark_halt(self, reason):
        self.data["halted"] = True
        self.data["halt_reason"] = reason
        self.data["halted_at"] = now()
        self.save()

    def release_canary(self):
        self.data["halted"] = False
        self.data["canary_released"] = True
        self.data["released_at"] = now()
        self.save()
