# wq#1454 — orchestrator-applied patches (slice B: monitoring consumers read the inventory)

Lives in `patches/wq1454/` because `patches/APPLY.sh` is owned by the wq#1456 FLEET_MAP job. Orchestrator: `bash /home/lumen/fleet/inventory/patches/wq1454/APPLY.sh` then `bash /home/lumen/fleet/inventory/patches/wq1454/TEST.sh`.

The build worker cannot write outside `/home/lumen/fleet/inventory`. These four consumers live
elsewhere, so they are delivered as unified diffs against the live files as read on 2026-09-07:

| patch | target | change |
|---|---|---|
| `sentinel_probes.py.patch` | `/home/lumen/sentinel/probes/sentinel_probes.py` | `_expects_online(node)` = `fleet_inventory.is_monitored`; FLEET_MAP.md only when inventory + last-good cache both unreadable. Both hardcoded `(("isis",ip),("hathor",ip))` loops → `_monitored_by_role("inference")`, ip from the record. |
| `storage_probe.py.patch` | `/home/lumen/agents/storage-audit/storage_probe.py` | `ssh_target(node)` from inventory; old `SSH` dict kept as `SSH_FALLBACK` for names the inventory lacks/cannot resolve, each use logged `DEFECT`. `parked_set(man)` = manifest parked ∪ inventory not-monitored, used by `run_probe`, `aq_autoclear`, `--check`. `fleet_map_expects_online` → inventory. New `--roster` (no SSH). |
| `fleet-auth-ssh-probe.sh.patch` | `/home/lumen/bin/fleet-auth-ssh-probe.sh` | `NODES` from `fleet-inv ssh-list` (self host skipped; empty list = exit 1, not "all clear"). New `--list`. NOTE: seshat/anubis/thoth were probed on LAN IPs before; the inventory target is `user@tailscale_ip`. |
| `power-remediate.patch` | `/home/lumen/bin/power-remediate` | `eligible_now()` = policy `ELIGIBLE` ∩ `fleet_inventory.monitored()` at runtime (fail-closed to ∅). `run` drops state for nodes no longer eligible; `status` prints policy / inventory-monitored / eligible; two selftest checks added. |

Every consumer loads the roster with `fleet_inventory.load(strict=True, fallback=True)` and honours
`FLEET_INVENTORY_ROOT` (tests). `patches/wq1454/APPLY.sh` = bak-then-patch (idempotent) + compile/selftest + Gatus
regeneration via `gen_gatus_nodes.py --write --wait` (validate on scratch port → .bak → in-place write
→ confirm the live 1-nodes set on :8418). `patches/wq1454/TEST.sh` = the test-to-failure steps (i)/(iv)/(iii) with
PASS/FAIL per step; it restores `machines/sia.yaml` byte-for-byte on exit.

Gatus reload: v5.10.0 `main.go` `listenToConfigurationFileChanges` polls the config file's mtime every
30 s and restarts monitoring on change (`HasLoadedConfigurationBeenModified`); the local
`/home/lumen/bin/gatus` (v5.36.0) carries the same symbols. No container restart is required.
