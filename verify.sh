#!/usr/bin/env bash
# wq#1455 — verification for slice C. Run as lumen on Lumen after install.sh.
#   /home/lumen/fleet/inventory/verify.sh            # all checks + tests (ii) and (iii), each run twice
#   /home/lumen/fleet/inventory/verify.sh --curl     # only the curl checks
# Prints PASS/FAIL per check; exit 1 if any FAIL. Never restarts the web service or
# runs render.py by hand — tests (ii)/(iii) rely on the .path unit alone.
set -uo pipefail

REPO=/home/lumen/fleet/inventory
WWW="$REPO/www"
URL=http://100.117.66.102:8419/fleet.html
STATUS_URL=http://100.117.66.102:8419/render_status.json
LAN=http://192.168.8.160:8419/fleet.html
REMOTE_NODE=${REMOTE_NODE:-bes}
FAIL=0

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAIL=1; }
sha()  { sha256sum "$WWW/fleet.html" | cut -d' ' -f1; }
status_ok() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["ok"])' "$WWW/render_status.json" 2>/dev/null; }
is_bad()  { [ "$(status_ok)" = False ]; }
is_good() { [ "$(status_ok)" = True ]; }
page_has() { curl -s --max-time 5 "$URL" | grep -q "$1"; }
page_lacks() { ! page_has "$1"; }
status_err() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["error"])' "$WWW/render_status.json" 2>/dev/null; }
# wait_for <seconds> <cmd...> : poll every 2s until cmd exits 0
wait_for() { local n=$1; shift; local t=0; while [ $t -lt "$n" ]; do "$@" && return 0; sleep 2; t=$((t+2)); done; return 1; }

echo "== units"
for u in fleet-inventory-render.path fleet-inventory-render.timer fleet-inventory-web.service; do
  if systemctl is-active --quiet "$u"; then pass "$u active"; else fail "$u not active"; fi
done
[ "$(ls -A "$WWW" | sort | tr '\n' ' ')" = "fleet.html render_status.json " ] \
  && pass "www/ contains only fleet.html render_status.json" \
  || fail "www/ contents: $(ls -A "$WWW" | tr '\n' ' ')"

echo "== curl"
if curl -s --max-time 5 "$URL" | grep -q "Lumen Fleet Inventory"; then pass "curl $URL from lumen"; else fail "curl $URL from lumen"; fi
if ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE_NODE" "curl -s --max-time 5 $URL" 2>/dev/null | grep -q "Lumen Fleet Inventory"; then
  pass "curl $URL from $REMOTE_NODE"; else fail "curl $URL from $REMOTE_NODE"; fi
if curl -s --max-time 5 "$LAN" >/dev/null 2>&1; then fail "LAN $LAN answered (must be refused)"; else pass "LAN $LAN refused"; fi
if curl -s --max-time 5 "$STATUS_URL" | grep -q '"ok"'; then pass "curl $STATUS_URL"; else fail "curl $STATUS_URL"; fi

[ "${1:-}" = "--curl" ] && { [ $FAIL -eq 0 ] && echo "ALL PASS" || echo "SOME FAIL"; exit $FAIL; }

WEB_PID_BEFORE=$(systemctl show -p MainPID --value fleet-inventory-web.service)
THOR="$REPO/machines/thor.yaml"
TOK="wq1455-probe"

test_ii() {   # edit thor.yaml roles -> served page reflects it within 60s, no manual render, no web restart
  local run=$1 before after
  echo "== test (ii) run $run: edit thor.yaml roles"
  cp -p "$THOR" "$THOR.verify.bak"
  before=$(sha)
  sed -i "s/^roles: \[/roles: [${TOK}-${run}, /" "$THOR"
  if wait_for 60 page_has "${TOK}-${run}"; then
    pass "(ii) run $run: served page shows ${TOK}-${run} (sha $before -> $(sha))"
  else
    fail "(ii) run $run: ${TOK}-${run} not in served page after 60s"
  fi
  mv "$THOR.verify.bak" "$THOR"
  wait_for 60 page_lacks "${TOK}-${run}" \
    && pass "(ii) run $run: revert propagated" || fail "(ii) run $run: revert did not propagate"
}

test_iii() {  # YAML syntax error -> ok=false w/ file:line, sha unchanged, banner served; fix -> ok=true
  local run=$1 before after err
  echo "== test (iii) run $run: YAML syntax error in thor.yaml"
  cp -p "$THOR" "$THOR.verify.bak"
  before=$(sha)
  printf '\nthis is: [not, valid: yaml\n' >> "$THOR"
  if wait_for 60 is_bad; then
    err=$(status_err)
    echo "      render_status.json error: $err"
    case "$err" in *thor.yaml:[0-9]*) pass "(iii) run $run: ok=false with file:line";; *) fail "(iii) run $run: error lacks file:line -> '$err'";; esac
  else
    fail "(iii) run $run: render_status.json never went ok=false"
  fi
  after=$(sha)
  [ "$before" = "$after" ] && pass "(iii) run $run: fleet.html sha unchanged ($before)" || fail "(iii) run $run: fleet.html changed $before -> $after"
  page_has 'id=rs-banner' && page_has 'render_status.json' \
    && pass "(iii) run $run: served page carries banner div + status poll script" || fail "(iii) run $run: banner script missing"
  echo "      served status JSON: $(curl -s --max-time 5 "$STATUS_URL" | tr -d '\n' | cut -c1-300)"
  mv "$THOR.verify.bak" "$THOR"
  if wait_for 60 is_good; then
    pass "(iii) run $run: fixed -> ok=true (error=$(status_err))"
  else
    fail "(iii) run $run: never recovered to ok=true"
  fi
  echo "      served status JSON: $(curl -s --max-time 5 "$STATUS_URL" | tr -d '\n' | cut -c1-200)"
}

for run in 1 2; do test_ii "$run"; done
for run in 1 2; do test_iii "$run"; done

WEB_PID_AFTER=$(systemctl show -p MainPID --value fleet-inventory-web.service)
[ "$WEB_PID_BEFORE" = "$WEB_PID_AFTER" ] && [ -n "$WEB_PID_BEFORE" ] && [ "$WEB_PID_BEFORE" != 0 ] \
  && pass "web service not restarted (MainPID $WEB_PID_BEFORE throughout)" \
  || fail "web service MainPID changed $WEB_PID_BEFORE -> $WEB_PID_AFTER"

echo "== unit-drift"
/home/lumen/bin/unit-drift check 2>&1 | tail -3 | grep -q "fleet-inventory" \
  && fail "fleet-inventory units still reported as drift" || pass "fleet-inventory units not in unit-drift output"

[ $FAIL -eq 0 ] && echo "ALL PASS" || echo "SOME FAIL"
exit $FAIL
