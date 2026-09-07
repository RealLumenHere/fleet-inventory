#!/usr/bin/env bash
# wq#1456 — make the FLEET_MAP.md roster table inventory-derived. ORCHESTRATOR-applied:
# /home/lumen is outside a build worker's write scope, so the worker delivers this script plus
# patches/FLEET_MAP.patch (the diff as it looked at authoring time, for review) and the
# orchestrator runs:   bash /home/lumen/fleet/inventory/patches/APPLY.sh
#
# What it does (idempotent, re-runnable — this is also the REFRESH command after any YAML edit):
#   1. timestamped .bak of FLEET_MAP.md
#   2. fleet-inv check  (refuses to touch the map if the inventory does not validate)
#   3. render.py --md --update FLEET_MAP.md  (first run: legacy table -> marker block, rows for boxes
#      not in the inventory are carried into a hand-kept table after the END marker; later runs:
#      replace between markers only; prose untouched)
#   4. shows the diff head and the marker count
# The .patch is NOT applied by default: it goes stale the moment a YAML changes, while --update
# always reflects the live inventory. `APPLY.sh --patch` applies it instead (review/replay only).
# Rollback: cp FLEET_MAP.md.bak.<ts> FLEET_MAP.md
set -euo pipefail
INV=${INV:-/home/lumen/fleet/inventory}
MAP=${MAP:-/home/lumen/FLEET_MAP.md}
TS=$(date +%Y%m%dT%H%M%S)
[ -r "$MAP" ] || { echo "FATAL: $MAP unreadable" >&2; exit 1; }
cp -p "$MAP" "$MAP.bak.$TS"
echo "backup: $MAP.bak.$TS"
python3 "$INV/fleet-inv" check
if [ "${1:-}" = "--patch" ]; then
  patch --dry-run -p0 "$MAP" < "$INV/patches/FLEET_MAP.patch" >/dev/null
  patch -p0 "$MAP" < "$INV/patches/FLEET_MAP.patch"
else
  python3 "$INV/render.py" --md --update "$MAP"
fi
n=$(grep -c 'BEGIN inventory-generated roster' "$MAP")
[ "$n" = 1 ] || { echo "FATAL: expected exactly one marker block, found $n — restoring .bak" >&2; cp -p "$MAP.bak.$TS" "$MAP"; exit 2; }
diff -u "$MAP.bak.$TS" "$MAP" | head -60 || true
echo "OK: $MAP roster table is inventory-derived (refresh: bash $INV/patches/APPLY.sh)"
