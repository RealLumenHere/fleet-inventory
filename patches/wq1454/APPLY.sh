#!/usr/bin/env bash
# wq#1454 APPLY.sh — orchestrator-applied rewiring of the monitoring consumers to the
# fleet inventory. Idempotent: an already-applied patch is detected (reverse dry-run)
# and skipped; the Gatus step reports "up to date" on a second run.
#
# Per file: timestamped .bak sibling, then `patch`. Then: py_compile / bash -n, the
# power-remediate selftest, storage_probe --roster, and the Gatus regeneration
# (gen_gatus_nodes.py --write: validate on a scratch port -> .bak -> write in place ->
# wait for the 30s config watcher to reload and confirm the live 1-nodes set).
#
# Pre-req: PR for /home/lumen/fleet/inventory (this ticket) landed, so that
# $INV/gen_gatus_nodes.py exists and /home/lumen/bin/fleet-inv resolves.
# Rollback: cp <file>.bak.<TS> <file> for each; cp config.yaml.bak.<TS> config.yaml.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
INV="${INV:-/home/lumen/fleet/inventory}"
TS="$(date -u +%Y%m%d_%H%M%SZ)"
GATUS_CONFIG="${GATUS_CONFIG:-/home/lumen/sentinel/config/config.yaml}"
RC=0

apply_one() {  # <patch-file> <target>
  local p="$HERE/$1" t="$2"
  if [ ! -f "$t" ]; then echo "SKIP $t: missing target"; RC=1; return; fi
  if patch --dry-run -R -N -s "$t" "$p" >/dev/null 2>&1; then
    echo "OK   $t: already applied"; return
  fi
  if ! patch --dry-run -N -s "$t" "$p" >/dev/null 2>&1; then
    echo "FAIL $t: patch does not apply cleanly (file drifted since 2026-09-07?) — left untouched"; RC=1; return
  fi
  cp -p "$t" "$t.bak.$TS"
  if patch -N -s "$t" "$p"; then echo "OK   $t: patched (backup $t.bak.$TS)"
  else echo "FAIL $t: patch failed — restoring backup"; cp -p "$t.bak.$TS" "$t"; RC=1; fi
}

echo "== APPLY wq#1454  $TS"
test -f "$INV/gen_gatus_nodes.py" || { echo "FAIL: $INV/gen_gatus_nodes.py missing — land the inventory PR first"; exit 1; }
test -x /home/lumen/bin/fleet-inv   || { echo "FAIL: /home/lumen/bin/fleet-inv missing"; exit 1; }

apply_one sentinel_probes.py.patch      /home/lumen/sentinel/probes/sentinel_probes.py
apply_one storage_probe.py.patch        /home/lumen/agents/storage-audit/storage_probe.py
apply_one fleet-auth-ssh-probe.sh.patch /home/lumen/bin/fleet-auth-ssh-probe.sh
apply_one power-remediate.patch         /home/lumen/bin/power-remediate

echo "-- syntax / selftests"
python3 -m py_compile /home/lumen/sentinel/probes/sentinel_probes.py && echo "OK   sentinel_probes.py compiles" || { echo "FAIL sentinel_probes.py compile"; RC=1; }
python3 -m py_compile /home/lumen/agents/storage-audit/storage_probe.py && echo "OK   storage_probe.py compiles" || { echo "FAIL storage_probe.py compile"; RC=1; }
python3 -m py_compile /home/lumen/bin/power-remediate && echo "OK   power-remediate compiles" || { echo "FAIL power-remediate compile"; RC=1; }
bash -n /home/lumen/bin/fleet-auth-ssh-probe.sh && echo "OK   fleet-auth-ssh-probe.sh parses" || { echo "FAIL fleet-auth-ssh-probe.sh parse"; RC=1; }
LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-unused}" python3 -c "import sys; sys.path.insert(0,'/home/lumen/sentinel/probes'); import sentinel_probes as s; print('OK   sentinel: _expects_online(isis)=%s inference=%s' % (s._expects_online('isis'), s._monitored_by_role('inference')))" || { echo "FAIL sentinel import"; RC=1; }
python3 /home/lumen/bin/power-remediate selftest | tail -1 | grep -q "ALL PASS" && echo "OK   power-remediate selftest ALL PASS" || { echo "FAIL power-remediate selftest"; RC=1; }
python3 /home/lumen/agents/storage-audit/storage_probe.py --roster | head -3
bash /home/lumen/bin/fleet-auth-ssh-probe.sh --list | awk '{print $1}' | tr '\n' ' ' | sed 's/^/OK   fleet-auth-ssh-probe NODES: /'; echo

echo "-- Gatus: regenerate 1-nodes (validate -> .bak -> write in place -> wait for reload)"
if python3 "$INV/gen_gatus_nodes.py" --config "$GATUS_CONFIG" --write --wait 150; then
  echo "OK   Gatus 1-nodes regenerated and live set confirmed"
else
  echo "FAIL Gatus regeneration/reload — if the file was written, restore: cp $GATUS_CONFIG.bak.* $GATUS_CONFIG"; RC=1
fi
echo "== APPLY done rc=$RC"
exit "$RC"
