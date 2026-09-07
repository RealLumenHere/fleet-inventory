#!/usr/bin/env bash
# wq#1455 — install the fleet-inventory systemd units (run as root on Lumen).
#   sudo /home/lumen/fleet/inventory/install.sh
# Idempotent: re-running re-installs the unit files, reloads, and (re)starts.
set -euo pipefail

REPO=/home/lumen/fleet/inventory
SRC="$REPO/systemd"
DST=/etc/systemd/system
UNITS=(fleet-inventory-render.service fleet-inventory-render.path fleet-inventory-render.timer fleet-inventory-web.service)
BIND=100.117.66.102
PORT=8419

if [ "$(id -u)" -ne 0 ]; then echo "install.sh: must run as root" >&2; exit 2; fi

# Port must be free unless it is already our own web service.
if ss -ltn 2>/dev/null | grep -q ":${PORT} " && ! systemctl is-active --quiet fleet-inventory-web.service; then
  echo "install.sh: :${PORT} already in use by something other than fleet-inventory-web — aborting" >&2
  ss -ltnp 2>/dev/null | grep ":${PORT} " >&2 || true
  exit 3
fi

# Timestamped .bak of anything we overwrite (DESIGN_SOT.md "Rollback").
TS=$(date -u +%Y%m%dT%H%M%SZ)
for u in "${UNITS[@]}"; do
  if [ -f "$DST/$u" ] && ! cmp -s "$SRC/$u" "$DST/$u"; then
    cp -p "$DST/$u" "$DST/$u.bak.$TS"
  fi
  install -m0644 -o root -g root "$SRC/$u" "$DST/$u"
done

# www/ must exist and be writable by lumen before the first render / serve.
install -d -m0755 -o lumen -g lumen "$REPO/www"

systemctl daemon-reload
# First render synchronously so the web service has a page to serve.
systemctl start fleet-inventory-render.service || true
systemctl enable --now fleet-inventory-render.path fleet-inventory-render.timer fleet-inventory-web.service

# Register the new units in the unit-drift baseline (aq#1089 reads /home/lumen/fleet-units
# MANIFEST.json via /home/lumen/bin/unit-drift) so they do not become a standing drift alert.
if [ -x /home/lumen/bin/unit-drift ]; then
  sudo -u lumen /home/lumen/bin/unit-drift snapshot || echo "install.sh: unit-drift snapshot failed (non-fatal)" >&2
fi

echo "--- units"
systemctl --no-pager list-units 'fleet-inventory-*' --all
systemctl --no-pager list-timers 'fleet-inventory-*' --all
echo "--- listening"
ss -ltn | grep ":${PORT} " || echo "NOT LISTENING on :${PORT}"
echo "install.sh: done — verify with $REPO/verify.sh"
