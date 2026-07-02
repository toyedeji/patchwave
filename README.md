# PatchWave

Autonomous security-patch orchestration for Proxmox LXC homelab clusters.

PatchWave runs nightly between 00:00–07:00, patches each container serially,
snapshots before every change, rolls back automatically on regression, and
delivers a markdown report at 07:15. Built and battle-tested on a 24-container
two-node Proxmox cluster.

## What it does
- Inventories all LXC containers across both Proxmox nodes via `pvesh`
- Tiers containers by criticality (stateless → stateful → critical infrastructure)
- Snapshots each container before patching (skips gracefully if bind mounts block it)
- Applies OS security updates only via `apt` — never touches app deps or held packages
- Runs functional probes before and after each patch
- Auto-rolls back on regression; flags FAILED-NO-ROLLBACK for human review
- Hard stops at 07:00 regardless of queue state; resumes next night
- Delivers a daily markdown report via email + Slack

## Architecture

```
                       ┌─────────────────────────┐
                       │  patchwave-run (CLI)    │
                       │  systemd timer @ nightly│
                       └───────────┬─────────────┘
                                   │
                                   ▼
              ┌────────────────────────────────────────┐
              │           lib/pipeline.py              │
              │                                        │
              │  verify → snapshot → pre_probe →       │
              │  [power_on] → dns_check → patch →      │
              │  post_probe → [rollback?] →            │
              │  power_restore → agent_report          │
              └────┬───────────────┬─────────────┬─────┘
                   │               │             │
                   ▼               ▼             ▼
         ┌──────────────┐  ┌───────────────┐ ┌──────────────┐
         │ Node 2 local │  │  Node 1 over  │ │ probes.py    │
         │ pct/pvesh    │  │  SSH (root@)  │ │ TCP/HTTP/docker│
         └──────────────┘  └───────────────┘ └──────────────┘
                   │
                   ▼
         ┌────────────────────────────────────────┐
         │  state/current.json + runs/<id>/       │
         │  journal.jsonl (append-only, resumable)│
         └───────────────┬────────────────────────┘
                         │
                         ▼
         ┌────────────────────────────────────────┐
         │ patchwave-report (07:15 systemd timer) │
         │   → /var/log/patchwave/report-YYYYMMDD │
         │   → Slack webhook (optional, non-blocking)
         └────────────────────────────────────────┘
```

Two-node model: PatchWave runs *on* Node 2 (invokes `pct` locally) and reaches
Node 1 over SSH. All CT operations are per-CT and serial (`max_concurrency = 1`).

## Snapshot strategies

Every CT is classified in `etc/targets.yaml` by how it can be recovered:

| Strategy | Applies to | Rollback path |
| --- | --- | --- |
| `no-bind` | No bind mounts | `pct snapshot` / `pct rollback` |
| `fix-forward-enhanced` | Running CT with read-only binds | No snapshot; pre-patch `dpkg` baseline + retained `.deb`s under `/var/cache/apt/archives/` for manual downgrade |
| `detach-snap-attach` | Stopped CT with read-only binds | Detach mp*, snapshot, reattach on every exit path *(not yet wired in pipeline — will refuse cleanly)* |
| `deferred` | CT with read-write data binds | Runner refuses to patch until per-CT data-survival contract is signed off |

## Safety guarantees

- **Canary halt.** `halt_after_canary = true` stops the queue after the first
  CT of every run; operator must `patchwave-run --release` to proceed.
- **Policy tiers.** `never_auto` CTs (e.g. PatchMon itself, supervisor host)
  refuse to run unless explicitly listed on the CLI. `stay_stopped` CTs
  refuse to power on.
- **Security-only apt.** `apt list --upgradable | grep -security` — nothing
  else. Held packages are honored. `dpkg` runs with `--force-confold` so
  admin-edited configs are preserved.
- **Never `apt clean`.** Old `.deb`s stay in the cache as a manual-downgrade
  path for fix-forward CTs.
- **DNS preflight.** Six-second `getent deb.debian.org` inside the CT before
  `apt-get`, so a resolver break fails fast instead of a 15-minute apt hang.
- **Rollback stops the CT first.** Rolling back live rootfs under a running
  process tree is unsafe; PatchWave issues `pct stop` before `pct rollback`.
- **Exceptions are swallowed at the step level.** The pipeline drives control
  flow via step status codes, not exception propagation — a single apt
  timeout cannot crash the runner mid-queue.

## Quickstart

```bash
# 1. Clone into /opt on your Proxmox orchestrator node (Node 2 in this layout).
git clone https://github.com/<you>/patchwave.git /opt/patchwave
cd /opt/patchwave

# 2. Copy the env template and fill in your IPs.
cp .env.example .env
$EDITOR .env

# 3. Substitute placeholders. PatchWave doesn't auto-load .env yet, so edit:
#    - etc/runner.conf           (node1_ssh, vulcan endpoint)
#    - lib/targets.py            (NODE1_SSH constant)
#    - etc/personas/vulcan.md    (host / endpoint lines, if you use Vulcan)
grep -RIn '<[A-Z_]*>' etc/ lib/ | grep -v .env

# 4. Install the daily report timer.
cp systemd/patchwave-report.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now patchwave-report.timer

# 5. Author your targets. Copy the example and delete/edit CTs to match
#    your fleet. Tier 0 = stopped/stateless, Tier 3 = critical.
$EDITOR etc/targets.yaml

# 6. Dry-run a single canary.
./bin/patchwave-run --tier 0 --ctid <your-lowest-blast-CT> --dry-run
./bin/patchwave-run --tier 0 --ctid <your-lowest-blast-CT>
./bin/patchwave-status
```

## Configuration

| File | Purpose |
| --- | --- |
| `.env.example` | Template of every placeholder used across the source tree |
| `etc/runner.conf` | Runtime: node1 SSH string, dpkg options, probe timeouts, Slack webhook, Vulcan endpoint |
| `etc/targets.yaml` | Per-CT tier / pre-state / snapshot strategy / probes / policy flags |
| `etc/personas/vulcan.md` | Supervisor persona spec (advisory, disabled by default) |

## Commands

```bash
# Show live run state.
./bin/patchwave-status

# Run a whole tier (respects halt_after_canary).
./bin/patchwave-run --tier 0

# Run one specific CT (bypasses never_auto if you name it explicitly).
./bin/patchwave-run --tier 3 --ctid 100

# Release a canary-halted run.
./bin/patchwave-run --release --run-id 20260702-013000

# Force build the daily report immediately.
./bin/patchwave-report
```

## Files at a glance

```
bin/patchwave-run       # main runner CLI
bin/patchwave-report    # daily 07:15 markdown/Slack report
bin/patchwave-status    # dump current.json for humans
lib/pipeline.py         # per-CT step machine + failure-recovery policy
lib/state.py            # resumable JSONL journal + halt semantics
lib/probes.py           # TCP / HTTP / docker-running probes
lib/targets.py          # targets.yaml loader + CT IP resolver
lib/reporter.py         # daily report builder + Slack push
etc/runner.conf         # runtime config
etc/targets.yaml        # per-CT policy + probes
etc/personas/vulcan.md  # advisory supervisor persona
systemd/                # patchwave-report timer + service
docs/                   # extended design notes
```

## Runtime state (gitignored)

- `state/current.json` — live per-run state, updated after every step.
- `state/runs/<run_id>/journal.jsonl` — append-only per-run journal.
- `state/runs/<run_id>/ct<ctid>-pre_patch.txt` — fix-forward baselines.
- `/var/log/patchwave/report-YYYYMMDD.md` — daily fallback report.

## License

MIT (see `LICENSE`).
