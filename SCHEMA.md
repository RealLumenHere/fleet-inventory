# Fleet inventory — schema (one YAML per machine in machines/<name>.yaml)

Single source of truth for every machine. Humans edit the YAML; agents/orchestrator/
worker/judge/supervisor/doctor/broker READ it (never guess node facts elsewhere).
Grounded in NetBox's device + inventory-item model.

## identity
  name:            canonical (egyptian-deity), e.g. nephthys
  aliases:         [informal names] e.g. thor -> ["Samsung"]
  make_model:      Dell T5810 / Mac Pro 6,1 / ASRock 4U12G / Galaxy Book Pro 360
  serial:          manufacturer serial / asset tag
  location:        rack / bench / shelf / where it physically is

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

## network
  tailscale_ip:    100.x
  lan_ip:          192.168.x
  ssh:             {user, method}      # method: key / ts-ssh check-mode / LAN-only
  nics:            [{iface, mac, note}] # note: "NEVER SPOOF" where it matters

## hardware   (the spec sheet — edit here on upgrades; keep history)
  cpu:             {model, cores, gen}
  ram:             {size_gb, type, speed, dimms_used, dimms_total, ceiling_gb}
  storage:         [{type, size, interface, serial, bay}]
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

## ops
  provisioned:     date
  last_verified:   date                # when specs were last confirmed against the box
  upgrades:        [{date, change}]    # spec-change history
  known_issues:    [ops-debt / quirks]
