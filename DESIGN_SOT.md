# Fleet inventory as source of truth + served page — DESIGN (2026-09-07, orch-fleet-inventory-sot)

## Root cause (evidence grade: A — read live from disk this session)
Node membership + status is hand-maintained in >=7 places. After nephthys was decommissioned
(wq#589, 2026-08-22) it kept alerting from FOUR of them (aq#799 Gatus, aq#800 storage_probe,
aq#608/#1070 power-remediate, aq#1022 probe-storage-integrity 'parked: ...nephthys' x264 still open).
The inverse defect exists too: sia is HEALTHY (lumen_state) and in storage_manifest but has NO
Gatus 1-nodes entry — silently unmonitored (over-suppression by omission).

Stores holding a roster today (all hand-edited):
1. /home/lumen/sentinel/config/config.yaml           group 1-nodes (13 live, 3 commented-out)
2. /home/lumen/sentinel/probes/sentinel_probes.py    hardcoded (("isis",ip),("hathor",ip)) x2 + FLEET_MAP.md status markers
3. /home/lumen/bin/fleet-auth-ssh-probe.sh           NODES= heredoc (still lists nephthys)
4. /home/lumen/agents/storage-audit/storage_probe.py SSH={} dict + storage_manifest.yaml 'parked' flags
5. /home/lumen/bin/power-remediate                   ELIGIBLE={"isis","hathor","khepri","nephthys"}
6. /home/lumen/FLEET_MAP.md                          prose table w/ [DEPLOYED]/[RETIRED] markers
7. bin/fleet-keymesh, fleet-update-check, fleet-maint-*, rotate-fleet-pw, fleet-roll-reboot, drift-assert, ui-inventory  (NODES lists)
8. minerva state_facts (lumen_state.py)              bi-temporal facts, no membership concept
9. /home/lumen/fleet/inventory/machines/*.yaml        the intended truth — 4 of ~20 machines, ZERO readers

## Class
CONFIG_DUPLICATION: the same fact (is X a fleet node, is it monitored) copied by hand into N consumers.
Fix the class: ONE loader, consumers import it, no consumer keeps a name list. A decommission is one YAML edit.

## Decisions
- TRUTH = machines/<name>.yaml. lumen_state may cache-derive; never contradict. FLEET_MAP roster table becomes derived (T4).
- Schema additions (SCHEMA.md): under `status:` add optional
    monitoring: {interval_s: 60, alert: true, alert_desc: "", gatus: true}
  defaults shown. `gatus:false` = keep in roster but no Gatus ICMP row (lumen self-ping is meaningless).
- MONITORED(name) := status.monitor is true AND status.status not in {decommissioned, dead, in-storage}.
  A node absent from inventory is NOT monitored (fail-closed on membership; consumers no longer carry names).
- Loader: /home/lumen/fleet/inventory/fleet_inventory.py (pyyaml only). API: load(strict) / all() / monitored()
  / is_monitored(name) / get(name) / ssh_target(name)->user@ts_ip / by_role(role) / validate().
  CLI /home/lumen/bin/fleet-inv (lumen-bin repo, PR): list [--monitored] [--json], get <name> [dotted.key],
  ssh-list, check, drift (compare tailscale_ip/lan_ip vs lumen_state, report only).
- LAST-GOOD: every successful strict load writes .roster.last_good.json (atomic). load(fallback=True) returns
  the cache on parse error, prints ERROR to stderr and raises an aq alert key inventory:parse-error (edge-
  triggered, resolves on next clean load). Consumers use fallback=True: a typo neither blanks monitoring
  nor resurrects a decommissioned node.
- Gatus 1-nodes block is GENERATED (gen_gatus_nodes.py) between markers
  `# BEGIN inventory-generated 1-nodes (do not hand-edit — run gen_gatus_nodes.py)` / `# END inventory-generated 1-nodes`.
  Validate before reload (fleet-ops playbook): start /home/lumen/bin/gatus against a temp copy of the config
  on a scratch port, require /health 200 within 10s, kill; only then cp into config/ (timestamped .bak first).
  Worker MUST verify from docs whether gatus v5.10.0 hot-reloads config; if not, `docker restart sentinel-gatus`
  (detached systemd-run, restore-verified).
- Serve: render.py writes www/fleet.html ATOMICALLY only on success; ALWAYS writes www/render_status.json
  {ok, ts, error, machines}. fleet.html carries a 10-line script that fetches render_status.json and shows a red
  banner when !ok (page = last-good, error surfaced). www/ contains only those two files.
  Units (system scope, User=lumen): fleet-inventory-render.service (oneshot) + fleet-inventory-render.path
  (PathChanged=machines dir + PathChanged=SCHEMA.md) + fleet-inventory-render.timer (5 min belt-and-braces)
  + fleet-inventory-web.service = python3 -m http.server --bind 100.117.66.102 8419 --directory www.
  Tailnet-only by bind address; no CF route, no LAN bind. Verify: curl from bes OK, curl 192.168.8.160:8419 refused.
- CCC tile: OPTIONAL and NOT in these slices (CCC lives on Thor, never-restart). Ticket as follow-up.

## Roster decisions (seed from lumen_state + Gatus evidence; every hardware field unknown -> VERIFY)
| node | status | monitor | alert | interval | note |
| lumen | active | true | false | - | gatus:false (self-ping meaningless; bes-awareness covers) |
| thor | active | true | false | 120 | laptop sleeps; never_restart |
| isis | active | true | true | 60 | roles +inference (isis-qwen3-32b) |
| hathor | standby | false | - | - | PARKED/off per lumen_state; roles inference (hathor-lfm-1b); power P3 |
| wadjet | standby | false | - | - | retired from vault-standby 08-02, powered off, no plug |
| nephthys | decommissioned | false | - | - | exists |
| apis | dead | false | - | - | DEAD 2026-08-13 |
| sobek, anubis, seshat, maat, bes, gameboy, sia | active | true | true | 60 | sia is NEW to Gatus (defect #2 above) |
| thoth | active | true | true | 300 | tailnet-offline since 09-02 = LIVE example for test (iv) |
| apep | active | true | true | 120 | |
| 3dprinter | active | true | false | 300 | AIO gets turned off |
| khepri | bench | true | false | 300 | Frank powers on/off |
| phone | active | true | false | 300 | roaming S26 Ultra |
| hapi-1 | (from lumen_state hapi.*) | per evidence | | | in storage_manifest today |
| aten | bench | false | - | - | provisioning, no IP |
| mirror, pbox | decommissioned | false | - | - | Frank 2026-08-28 |
| toilet | active | false | - | - | 192.168.50.111 — role unknown, record known_issue |
| be3600 | active | false | - | - | workbench router, roles [router] |

## Test-to-failure (must all run, must all be shown in ticket notes)
(i)  set sia (or any active node) monitor:false -> within one cycle: gone from Gatus 1-nodes, fleet-auth-ssh-probe
     NODES, storage_probe parked set, power-remediate eligibility, sentinel expects_online. Revert.
(ii) edit thor.yaml roles -> www/fleet.html shows it within 60s (path unit) without touching the web service.
(iii) break YAML syntax in one file -> render_status.json ok=false w/ file+line, fleet.html unchanged (sha before==after),
     banner visible; consumers keep last-good roster; aq inventory:parse-error opens; fix -> resolves. 
(iv) thoth (genuinely down on tailnet) still produces its Gatus alert after regeneration; sia now alerts if unplugged.

## Rollback
Every touched file gets a timestamped .bak sibling; Gatus config restore = cp .bak back (auto-reload/restart);
units: systemctl disable --now fleet-inventory-*; consumers: restore .bak.
