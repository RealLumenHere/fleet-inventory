"""wq#1611 — `fleet-inv drift` must not print the same line for "no drift" and "nothing checked".

Covers the coverage line (checked/matched/drifted/unverifiable) and the NO-FACT ratchet:
the baseline falls freely, grows only with --accept-baseline, and a silent growth exits 3.
Temp inventory roots and a stub lumen_state only, never the real ones.
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, TOP)

from test_roster_consumers import _rec  # noqa: E402

CLI = os.path.join(TOP, "fleet-inv")

# Prints what `lumen_state.py current <node>` prints, for the facts named in $STUB_FACTS.
STUB = '''import os, sys
node = sys.argv[2]
for row in os.environ.get("STUB_FACTS", "").split(";"):
    if row and row.split("=")[0].split(".")[0] == node:
        k, _, v = row.partition("=")
        print(f"  {k} = {v} (since 2026-01-01)")
'''


@pytest.fixture
def root(tmp_path):
    m = tmp_path / "machines"
    m.mkdir()
    (m / "hub.yaml").write_text(_rec("hub", ts="100.64.0.1"))
    (m / "leaf.yaml").write_text(_rec("leaf", ts="100.64.0.3"))
    (tmp_path / "stub_state.py").write_text(STUB)
    return tmp_path


def _drift(root, facts="", *args):
    env = dict(os.environ, FLEET_INVENTORY_ROOT=str(root), FLEET_INV_NO_AQ="1",
               FLEET_INV_HOSTNAME="hub", LUMEN_STATE_PY=str(root / "stub_state.py"),
               STUB_FACTS=facts)
    p = subprocess.run([sys.executable, CLI, "drift", *args], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def _cov(out):
    line = [x for x in out.splitlines() if x.startswith("coverage[")]
    assert len(line) == 1, out
    return line[0]


ALL_MATCH = ("hub.tailscale_ip=100.64.0.1;hub.lan_ip=192.168.8.1;hub.ssh_user=hub;"
             "leaf.tailscale_ip=100.64.0.3;leaf.lan_ip=192.168.8.3;leaf.ssh_user=leaf")

# wq#1631 added a 4th checked field per machine (declared-vs-store lifecycle). None of these
# stub fact strings carry a `<node>.status` row, so the lifecycle check is NO-FACT for both
# hub and leaf (+2) in every scenario below on top of the 3 original network fields (x2 = 6).


def test_nothing_checked_does_not_read_as_clean(root):
    """The §2.3 bug: 0 drift over 8 fields nobody could compare used to print success."""
    rc, out, _ = _drift(root, "")
    assert "drift: 0 disagreement(s)" in out
    assert _cov(out) == ("coverage[fleet-inv:inventory-vs-state]: checked 8 · matched 0"
                         " · drifted 0 · unverifiable 8 (no-fact 8)")
    assert rc == 0  # first run has no baseline to regress against
    assert json.loads((root / ".drift.coverage_baseline.json").read_text())["detail"]["no-fact"] == 8


def test_everything_matched_is_a_different_line(root):
    rc, out, _ = _drift(root, ALL_MATCH)
    assert rc == 0
    assert _cov(out) == ("coverage[fleet-inv:inventory-vs-state]: checked 8 · matched 6"
                         " · drifted 0 · unverifiable 2 (no-fact 2)")


def test_drift_still_exits_2_and_is_counted(root):
    rc, out, _ = _drift(root, ALL_MATCH.replace("leaf.lan_ip=192.168.8.3", "leaf.lan_ip=10.0.0.9"))
    assert rc == 2
    assert "· drifted 1 ·" in _cov(out)


def test_no_fact_growth_exits_3_and_keeps_the_old_baseline(root):
    _drift(root, ALL_MATCH)                      # baseline: no-fact 2 (lifecycle only, both nodes)
    rc, out, _ = _drift(root, "")                # every network field lost its fact too
    assert rc == 3
    assert "coverage: NO-FACT grew 2 -> 8" in out
    assert json.loads((root / ".drift.coverage_baseline.json").read_text())["detail"].get("no-fact", 0) == 2


def test_baseline_ratchets_down_but_never_up_on_its_own(root):
    _drift(root, "")                                      # baseline: no-fact 8
    rc, out, _ = _drift(root, ALL_MATCH)                  # network gap closed, lifecycle still NO-FACT
    assert rc == 0 and "coverage: NO-FACT 8 -> 2" in out
    rc, _, _ = _drift(root, "")                           # reopening it fails again
    assert rc == 3


def test_accept_baseline_is_the_only_way_up(root):
    _drift(root, ALL_MATCH)
    rc, _, _ = _drift(root, "", "--accept-baseline")
    assert rc == 0
    assert json.loads((root / ".drift.coverage_baseline.json").read_text())["detail"]["no-fact"] == 8


def test_no_baseline_reports_without_gating(root):
    _drift(root, ALL_MATCH)                       # baseline: no-fact 2
    rc, out, _ = _drift(root, "", "--no-baseline")  # would regress (2 -> 8), but --no-baseline skips the gate
    assert rc == 0
    assert "· unverifiable 8 " in _cov(out)
    assert json.loads((root / ".drift.coverage_baseline.json").read_text())["detail"].get("no-fact", 0) == 2


def test_missing_inventory_value_counts_as_unverifiable(root):
    (root / "machines" / "leaf.yaml").write_text(_rec("leaf", ts="VERIFY", user="VERIFY"))
    rc, out, _ = _drift(root, ALL_MATCH.replace("leaf.lan_ip=192.168.8.3;", ""), "--no-baseline")
    assert rc == 0
    assert _cov(out).endswith("· unverifiable 5 (no-fact 3, no-inventory 2)")
