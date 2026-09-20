# Fleet inventory — schema (one YAML per machine in machines/<name>.yaml)

Source of truth for DECLARED machine config: governance, maint_tier, monitoring policy, roles,
ops.known_issues. Humans edit the YAML for these; agents/orchestrator/worker/judge/supervisor/
doctor/broker READ it (never guess node facts elsewhere). network.tailscale_ip/lan_ip/ssh.user,
identity.tailnet and hardware.power_hw.plug_id are OBSERVED instead — state_facts is their authority, and
fleet_inventory.py reads them live (observed()); the YAML value there is a fallback for when
the store has no fact yet, not the truth (wq#1631, MINERVA_PIPELINE_MAP.md §4.2). `fleet-inv
drift` audits the declared YAML against the store.
Grounded in NetBox's device + inventory-item model.

## identity
  name:            canonical (egyptian-deity), e.g. nephthys
  aliases:         [informal names] e.g. thor -> ["Samsung"]
  make_model:      Dell T5810 / Mac Pro 6,1 / ASRock 4U12G / Galaxy Book Pro 360
  serial:          manufacturer serial / asset tag
  location:        rack / bench / shelf / where it physically is
  tailnet:         {hostname, tags}     # OBSERVED (wq#1692) — state_facts `<node>.tailnet_hostname` /
                                        # `<node>.tailnet_tags`, written by the verify probe from
                                        # `tailscale status --json`; the YAML value is the NO-FACT fallback
                   #   hostname  the node's HostName as tailscale reports it; NOT always identity.name
                   #             (be3600 = GL-BE3600, toilet = toilet-940X5N, phone = "Galaxy S26 Ultra").
                   #             `fleet-inv drift` compares it case-insensitively (MagicDNS lowercases).
                   #   tags      ACL tags, e.g. [tag:lumen-fleet] (bes, khepri, nephthys, sia, sobek,
                   #             wadjet). [] = a user-owned device — a real value, not "unknown"; omit the
                   #             key when nobody has read it. Each entry must match `tag:<name>`.

## status   (drives monitoring + dispatch — the nephthys fix lives here)
  status:          active | standby | bench | in-storage | decommissioned | dead
  power:           on | off | plug-cut
  monitor:         true | false        # false => NO liveness sweep, NO alerts
  monitoring:      {interval_s: 60, alert: true, alert_desc: "", gatus: true}   # OPTIONAL; defaults shown
                   #   interval_s  liveness poll period (Gatus ICMP interval)
                   #   alert       ntfy on unreachable (false = history only, e.g. laptops that sleep)
                   #   alert_desc  human text on the alert ("" = Gatus default title)
                   #   gatus       false = keep in roster but emit NO Gatus ICMP row (lumen self-ping is meaningless)
  decommissioned:  {date, reason}      # when status=decommissioned

  MONITORED(name) := status.monitor is true AND status.status not in {decommissioned, dead, in-storage}.
  A node ABSENT from inventory is NOT monitored (fail-closed on membership: consumers carry no name
  lists of their own — they call fleet_inventory.monitored()/is_monitored()). Loader: fleet_inventory.py;
  CLI: fleet-inv. Files under machines/ whose name starts with "_" are ignored.

## network  (OBSERVED — state_facts is authoritative; the YAML value is a NO-FACT fallback, wq#1631)
  tailscale_ip:    100.x
  lan_ip:          192.168.x
  ssh:             {user, method, key} # method: key / ts-ssh check-mode / LAN-only / n/a (= do not reach)
                                       # key: OPTIONAL per-node private key path (thoth); default id_ed25519
  nics:            [{iface, mac, note}] # note: "NEVER SPOOF" where it matters

  ssh_target(name) = user@tailscale_ip; unresolvable (VERIFY user/ip, method n/a) => the machine is
  simply absent from every ssh fan-out (fleet-inv ssh-list reports it on stderr). Consumers:
  fleet-keymesh / fleet-roll-reboot / rotate-fleet-pw (bash, via `fleet-inv ssh-list`), fleet-update-check /
  fleet-maint-* / drift-assert / ui-inventory (python, via fleet_inventory.select / ssh_targets). wq#1456.

## hardware   (the spec sheet — edit here on upgrades; keep history)
  cpu:             {model, cores, gen}
  ram:             {size_gb, type, speed, dimms_used, dimms_total, ceiling_gb}
  storage:         [{serial, type, size, model, interface, bay}]
                   #   serial is the REQUIRED key of every entry (the validator rejects a missing or
                   #   empty one, and a duplicate within a node); write VERIFY when nobody has read
                   #   it yet. /dev names (sda, nvme0n1, mmcblk1) are NOT stable — they reorder across
                   #   boots and hotplug — so never key or match a drive on them. `bay` is where the
                   #   drive sits (boot, bay3), a label for humans, not an identity.
  gpu:             [{model, vram_gb}]
  board:           {model, bios, quirks}   # quirks e.g. "restore-on-AC doesn't persist (no nvram)"
  power_hw:        {psu_w, ups, plug_id, ac_restore, recovery}  # recovery = how to bring it back

## roles   (drives what the fleet expects of it)
  roles:           [orchestrator|worker|inference|judge|supervisor|doctor|broker|media|vault|ha-primary|...]
  services:        [systemd/docker units it runs]
  serves:          [models/rungs it provides]

## governance / safety flags
  crown_jewel:     true|false          # Ma'at, Lumen: detached-only, Frank-present
  never_restart:   true|false          # Thor
  never_reboot_unless_recoverable: true|false
  off_limits:      true|false          # Surface Pro business — never AI-integrate

## software   (OBSERVED, volatile — a probe writes it, nobody hand-edits it; wq#1666)
  os:              PRETTY_NAME from /etc/os-release, e.g. "Ubuntu 26.04 LTS"
  kernel:          uname -r at last probe
  A kernel string is true for one moment; treat it as "as of ops.last_verified", never as a spec.

## ops
  provisioned:     date
  last_verified:   date                # when specs were last confirmed against the box
  upgrades:        [{date, change}]    # spec-change history
  known_issues:    [ops-debt / quirks]
  maint_tier:      number              # A11 §5 maintenance ordering law (wq#852/854), low runs first:
                                       #   1 leaf · 1.5 inference leaf · 2 service · 3 vault standby (the
                                       #   rehearsal) · 4 vault primary · 5 the runner host (Lumen, always last)
                                       # unset = "unlisted-but-reachable": sorts after 5, flagged for review.
                                       # fleet-maint-assemble / fleet-maint-run build NODE_ORDER from this.
                                       # governance.never_restart (Thor) excludes a node from maintenance entirely.

## hardware.power_hw.plug_id  (OBSERVED — state_facts `<node>.power_plug` is authoritative, wq#1631)
  The smart-plug id (P1..P14, E1) a box sits on. `fleet-inv plugs` feeds plug-ctl's labels from
  fleet_inventory.observed() first, falling back to this YAML value on NO-FACT. VERIFY/none = no
  plug known to either store.
