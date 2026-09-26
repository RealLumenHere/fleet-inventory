# wq#1688 fleet-inv verify: hardware identity via the verify contract

## Root cause (evidence grade A: read the code + the store)
wq#1666 filled hardware/ram/storage/nic facts once, by hand. Nothing re-verifies them:
`lumen-verify` has no probe for `hardware`, `nic`, `nic_config`, `storage_layout`; its
`ram` probe runs `dmidecode` without sudo (always unknown off-root); its storage size sum
counts `zram0` as a disk (the wq#1580 thor artefact); and every SSH probe dials the bare
subject name, which only resolves for nodes with a matching MagicDNS/ssh-config name
(`ssh gameboy` -> name resolution failure, 2026-09-26).

## Rejected: attempt-5 `fleet_probe.py` (uncommitted in the primary tree)
It builds a machine string per predicate and ingests it when it differs from the stored
value. Every stored identity fact is curated free text (`Dell OptiPlex 3090 UFF, sn 1XLN9R3,
board 05G6P6 A00 ...`, Frank corrections inline), so the first run would supersede all ~50
of them with probe strings. That is the wq#2204 class (a measurement is never the new value
of a free-text fact), uses the banned `if old == new` reconciler shape (wq#1608), and makes
a second owner for `storage`/`ram`/`kernel`, which `lumen-verify` already owns (wq#1700).
Its kernel value embeds uptime, so it would re-ingest on every run.

## Chosen design
Identity is CONFIRMED by stable tokens, never rewritten:
- `hardware` (new probe `node_identity`): stored text must contain the DMI system or
  baseboard serial (`sudo -n dmidecode -s ...`). No serial stated -> unknown.
  Stated serial(s) present but neither observed serial matches -> `MISMATCH` (unknown).
- `nic`, `nic_config` (`node_identity`): every MAC stated in the value must be on the node
  (`ip -o link`). Missing MAC -> `MISMATCH`. None stated -> unknown.
- `storage`, `storage_layout` (`node_storage`): every `sn X` stated must be an observed
  whole-disk serial (lsblk TYPE=disk, loop/zram dropped) -> keyed by SERIAL, immune to
  nvme renumbering. No serial stated -> existing size check (storage only).
- `ram`: sum of type-17 sizes via `sudo -n dmidecode` (was unprivileged).
Confirm = report the STORED value to `report_observation` (evidence refresh, no new row).
Everything else is unknown with a detail line in fact_verifications. Nothing supersedes.
sudo -n denied (sia) -> unknown `sudo -n denied`, unprivileged predicates still checked.

SSH targets resolve through `fleet_inventory.ssh_target()` (user@tailscale_ip from the
store, wq#1631), falling back to the bare name only when the roster has no entry.

`lumen-verify node <subject>... [--probes ...]` forces those probes on that subject now.
`fleet-inv verify [<node>|--all] [--dry-run]` = roster walk -> `lumen-verify node` ->
per-predicate outcome, OFFLINE with newest stored fact when the node never answered.
`last_verified` is read from the store (newest confirmed fact_verifications row), NOT
written into machines/*.yaml: a probe rewriting tracked YAML on every run would dirty the
primary tree outside the PR flow, and observed evidence belongs to the store (wq#1631).
The YAML ops.last_verified key stays declared/historical.
`fleet-inv drift` identity rows read the newest fact_verifications outcome (MATCH /
DRIFT on MISMATCH / UNVERIFIED / NO-FACT) instead of re-probing.

## Tests (to failure)
drive-name shuffle, zram present, node offline, sudo denied, curated free text never
superseded, MISMATCH never superseded, unresolvable bare name resolved via roster.

## Rollback
Revert lumen-bin PR #149, minerva PR #39 and this PR. No schema change;
fact_verifications rows are append-only evidence.
