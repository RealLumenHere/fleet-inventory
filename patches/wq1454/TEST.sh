#!/usr/bin/env bash
# wq#1454 TEST.sh — test-to-failure for the inventory-driven monitoring consumers.
# Prints PASS/FAIL per step and exits non-zero if any step failed. Always restores
# the machine file it edits (trap), byte-for-byte, and regenerates Gatus afterwards.
#
# Steps (DESIGN_SOT.md "Test-to-failure"):
#   (i)   NODE monitor:false -> absent from Gatus 1-nodes, fleet-auth-ssh-probe NODES,
#         storage_probe polled set (moves to parked), power-remediate inventory-monitored
#         set, sentinel _expects_online; revert -> returns everywhere.
#   (iv)  thoth (tailnet-offline, real) keeps its alerting Gatus row after regeneration;
#         its Gatus/ntfy -> aq row is open.
#   (iii) YAML syntax error in NODE's file -> every consumer still runs on the
#         last-good roster (rc 0, roster non-blank, ERROR on stderr), aq
#         inventory:parse-error opens; fix -> resolves.
#
# Env (defaults = live paths): INV, SENTINEL, STORAGE, AUTHPROBE, POWER, GATUS_CONFIG,
#   GATUS_LIVE=1 (write config + poll :8418; 0 = --print only, for a scratch run),
#   NODE=sia, FLEET_INV_NO_AQ=1 to skip the aq assertions (scratch runs).
set -u
INV="${INV:-/home/lumen/fleet/inventory}"
SENTINEL="${SENTINEL:-/home/lumen/sentinel/probes/sentinel_probes.py}"
STORAGE="${STORAGE:-/home/lumen/agents/storage-audit/storage_probe.py}"
AUTHPROBE="${AUTHPROBE:-/home/lumen/bin/fleet-auth-ssh-probe.sh}"
POWER="${POWER:-/home/lumen/bin/power-remediate}"
GATUS_CONFIG="${GATUS_CONFIG:-/home/lumen/sentinel/config/config.yaml}"
GATUS_LIVE="${GATUS_LIVE:-1}"
NODE="${NODE:-sia}"
GEN="$INV/gen_gatus_nodes.py"
export FLEET_INVENTORY_ROOT="$INV"
export FLEET_INV="${FLEET_INV:-$INV/fleet-inv}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-unused}"
YAML="$INV/machines/$NODE.yaml"
BAK="$(mktemp "/tmp/$NODE.yaml.pristine.XXXXXX")"
cp "$YAML" "$BAK"
SHA0="$(sha256sum "$YAML" | cut -d' ' -f1)"
FAILS=0; N=0
ok()   { N=$((N+1)); echo "PASS $N: $*"; }
fail() { N=$((N+1)); FAILS=$((FAILS+1)); echo "FAIL $N: $*"; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else fail "$label"; fi; }
restore() { cp "$BAK" "$YAML"; }
trap 'restore' EXIT

# ── consumer views ─────────────────────────────────────────────────────────
gatus_nodes() {   # names in the generated block (and, live, in the running Gatus)
  if [ "$GATUS_LIVE" = "1" ]; then
    python3 "$GEN" --config "$GATUS_CONFIG" --write --wait 150 >/tmp/wq1454-gen.out 2>&1 || { cat /tmp/wq1454-gen.out; return 1; }
    curl -s -m 5 http://127.0.0.1:8418/api/v1/endpoints/statuses \
      | python3 -c 'import sys,json; print("\n".join(sorted(e["name"] for e in json.load(sys.stdin) if e["group"]=="1-nodes" and not e["name"].endswith("-sshauth"))))'
  else
    python3 "$GEN" --print 2>/dev/null | sed -n 's/^  - name: //p'
  fi
}
ssh_nodes()     { bash "$AUTHPROBE" --list 2>/dev/null | awk '{print $1}'; }
storage_line()  { python3 "$STORAGE" --roster 2>/dev/null | grep "^$1:" ; }
power_line()    { python3 "$POWER" status 2>/dev/null | grep '^inventory-monitored:'; }
sentinel_mon()  { python3 - "$1" <<EOF
import os, sys
sys.path.insert(0, os.path.dirname("$SENTINEL"))
import sentinel_probes as s
print(s._expects_online(sys.argv[1]))
EOF
}
has()  { grep -qx "$2" <<< "$1"; }
word() { grep -qw "$2" <<< "$1"; }
aq_open() { python3 - "$1" <<'EOF'
import sqlite3, sys
c = sqlite3.connect("/home/lumen/agents/alert-queue/alerts.db")
rows = c.execute("SELECT id FROM alerts WHERE key=? AND status='open'", (sys.argv[1],)).fetchall()
print(",".join(str(r[0]) for r in rows)); sys.exit(0 if rows else 1)
EOF
}

echo "== wq#1454 TEST.sh  INV=$INV NODE=$NODE GATUS_LIVE=$GATUS_LIVE  $(date -u +%FT%TZ)"
echo "-- baseline"
G0="$(gatus_nodes)";  check "baseline: Gatus 1-nodes has $NODE  [$(echo "$G0" | tr '\n' ' ')]" has "$G0" "$NODE"
S0="$(ssh_nodes)";    check "baseline: fleet-auth-ssh-probe NODES has $NODE" has "$S0" "$NODE"
P0="$(storage_line polled)"; check "baseline: storage_probe polls $NODE  [$P0]" word "$P0" "$NODE"
W0="$(power_line)";   check "baseline: power-remediate inventory-monitored has $NODE  [$W0]" word "$W0" "$NODE"
M0="$(sentinel_mon "$NODE")"; check "baseline: sentinel _expects_online($NODE) == True  [$M0]" test "$M0" = "True"

echo "-- (i) park: $NODE monitor:false"
sed -i 's/^  monitor: true/  monitor: false/' "$YAML"
grep -q '^  monitor: false' "$YAML" || fail "could not flip monitor in $YAML"
G1="$(gatus_nodes)";  check "(i) Gatus 1-nodes lacks $NODE  [$(echo "$G1" | tr '\n' ' ')]" test "$(has "$G1" "$NODE"; echo $?)" = 1
S1="$(ssh_nodes)";    check "(i) fleet-auth-ssh-probe NODES lacks $NODE" test "$(has "$S1" "$NODE"; echo $?)" = 1
P1="$(storage_line polled)"; K1="$(storage_line parked)"
check "(i) storage_probe no longer polls $NODE  [$P1]" test "$(word "$P1" "$NODE"; echo $?)" = 1
check "(i) storage_probe parks $NODE  [$K1]" word "$K1" "$NODE"
W1="$(power_line)";   check "(i) power-remediate inventory-monitored lacks $NODE  [$W1]" test "$(word "$W1" "$NODE"; echo $?)" = 1
M1="$(sentinel_mon "$NODE")"; check "(i) sentinel _expects_online($NODE) == False  [$M1]" test "$M1" = "False"

echo "-- (i) revert"
restore
G2="$(gatus_nodes)";  check "(i) revert: Gatus 1-nodes has $NODE again" has "$G2" "$NODE"
S2="$(ssh_nodes)";    check "(i) revert: fleet-auth-ssh-probe NODES has $NODE again" has "$S2" "$NODE"
P2="$(storage_line polled)"; check "(i) revert: storage_probe polls $NODE again" word "$P2" "$NODE"
W2="$(power_line)";   check "(i) revert: power-remediate inventory-monitored has $NODE again" word "$W2" "$NODE"
M2="$(sentinel_mon "$NODE")"; check "(i) revert: sentinel _expects_online($NODE) == True" test "$M2" = "True"

echo "-- (iv) thoth keeps an alerting row"
T="$(python3 "$GEN" --print 2>/dev/null | grep -A5 '^  - name: thoth$')"
check "(iv) generated thoth row carries alerts: [{type: ntfy ...}]  [$(echo "$T" | grep alerts | tr -s ' ')]" grep -q 'alerts: \[{type: ntfy' <<< "$T"
if [ "$GATUS_LIVE" = "1" ]; then
  TL="$(curl -s -m 5 http://127.0.0.1:8418/api/v1/endpoints/statuses | python3 -c 'import sys,json
for e in json.load(sys.stdin):
    if e["group"]=="1-nodes" and e["name"]=="thoth":
        r=e.get("results") or []; print("success=%s ts=%s" % (r[-1]["success"], r[-1]["timestamp"]) if r else "no results")')"
  check "(iv) live Gatus thoth last result is a failure (tailnet-offline)  [$TL]" grep -q 'success=False' <<< "$TL"
fi
if [ -z "${FLEET_INV_NO_AQ:-}" ]; then
  A="$(aq_open 'Gatus: 1-nodes/thoth')"; check "(iv) aq row open for Gatus: 1-nodes/thoth  [ids: $A]" test -n "$A"
fi

echo "-- (iii) YAML syntax error in $NODE.yaml -> last-good"
B_OK="$(python3 "$GEN" --print 2>/dev/null)"
printf '\nbroken: [unclosed\n' >> "$YAML"
B_BROKEN="$(python3 "$GEN" --print 2>/tmp/wq1454-check.err)"; RC=$?
check "(iii) gen_gatus_nodes runs (rc=$RC) and its block is byte-identical to the last-good block ($(echo "$B_OK" | grep -c '^  - name:') rows)" test "$RC" = 0 -a "$B_OK" = "$B_BROKEN" -a -n "$B_OK"
check "(iii) gen_gatus_nodes printed ERROR to stderr  [$(head -c 160 /tmp/wq1454-check.err)]" grep -q '^ERROR' /tmp/wq1454-check.err
if [ "$GATUS_LIVE" = "1" ]; then
  python3 "$GEN" --config "$GATUS_CONFIG" --check >/dev/null 2>&1; RC=$?
  check "(iii) live config still up to date on last-good (--check rc=$RC)" test "$RC" = 0
fi
S3="$(ssh_nodes)";    check "(iii) fleet-auth-ssh-probe NODES non-blank ($(echo "$S3" | grep -c .) nodes) and has $NODE" has "$S3" "$NODE"
P3="$(storage_line polled)"; check "(iii) storage_probe roster non-blank and polls $NODE  [$P3]" word "$P3" "$NODE"
W3="$(power_line)";   check "(iii) power-remediate inventory-monitored non-blank, has $NODE  [$W3]" word "$W3" "$NODE"
M3="$(sentinel_mon isis)"; check "(iii) sentinel _expects_online(isis) == True on last-good  [$M3]" test "$M3" = "True"
if [ -z "${FLEET_INV_NO_AQ:-}" ]; then
  A3="$(aq_open 'inventory:parse-error')"; check "(iii) aq inventory:parse-error OPEN  [ids: $A3]" test -n "$A3"
fi
restore
python3 "$FLEET_INV" check >/dev/null 2>&1; check "(iii) fix: fleet-inv check passes again" test "$?" = 0
if [ -z "${FLEET_INV_NO_AQ:-}" ]; then
  A4="$(aq_open 'inventory:parse-error' || true)"; check "(iii) fix: aq inventory:parse-error resolved  [still open: '${A4}']" test -z "$A4"
fi

echo "-- restoration"
SHA1="$(sha256sum "$YAML" | cut -d' ' -f1)"
check "restore: $NODE.yaml byte-identical to start  [$SHA0]" test "$SHA0" = "$SHA1"
G9="$(gatus_nodes)"; check "restore: Gatus 1-nodes set == baseline  [$(echo "$G9" | tr '\n' ' ')]" test "$G9" = "$G0"

echo "== $((N-FAILS))/$N PASS, $FAILS FAIL"
exit "$FAILS"
