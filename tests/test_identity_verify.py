"""wq#1688: fleet-inv verify delegates to lumen-verify; drift reads identity evidence."""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, TOP)
import fleet_inventory as inv  # noqa: E402


def _db(tmp_path, facts, checks):
    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE state_facts (id TEXT, subject TEXT, predicate TEXT, value TEXT, "
              "valid_to TEXT, ingested_at TEXT)")
    c.execute("CREATE TABLE fact_verifications (id TEXT, fact_id TEXT, subject TEXT, "
              "predicate TEXT, outcome TEXT, detail TEXT, ts TEXT)")
    c.executemany("INSERT INTO state_facts VALUES (?,?,?,?,?,?)",
                  [f + ("2026-01-01",) if len(f) == 5 else f for f in facts])
    c.executemany("INSERT INTO fact_verifications VALUES (?,?,?,?,?,?,?)",
                  [(v[0], v[1], "box", {"h1": "hardware", "s1": "storage_layout"}.get(v[1], "x"))
                   + v[2:] if len(v) == 5 else v for v in checks])
    c.commit()
    c.close()
    return str(db)


def test_identity_evidence_newest_check_of_open_fact(tmp_path, monkeypatch):
    monkeypatch.delenv("FLEET_INV_NO_STATE", raising=False)
    db = _db(tmp_path,
             [("h1", "box", "hardware", "sn A", None),
              ("s0", "box", "storage", "old sn Z", "2026-01-01"),        # closed: ignored
              ("s1", "box", "storage_layout", "sn B", None),               # alias honored
              ("n1", "box", "nic_config", "aa:bb:cc:dd:ee:ff", None)],
             [("v1", "h1", "unknown", "ssh box: timed out", "2026-09-01T00:00:00"),
              ("v2", "h1", "confirmed", None, "2026-09-26T00:00:00"),
              ("v3", "s1", "unknown", "MISMATCH: stated disk serial(s) ['B']", "2026-09-26T00:00:00")])
    ev = inv.identity_evidence("box", db=db)
    assert ev["hardware"][:2] == ("hardware", "confirmed")
    assert ev["storage"][0] == "storage_layout" and ev["storage"][2].startswith("MISMATCH")
    assert ev["nic"] == ("nic_config", None, None, None)                  # open, never checked
    assert "ram" not in ev and "kernel" not in ev                         # NO-FACT
    assert inv.last_verified("box", db=db) == "2026-09-26"


def test_identity_evidence_degrades_to_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_INV_NO_STATE", "1")
    assert inv.identity_evidence("box", db="/nonexistent") == {}
    monkeypatch.delenv("FLEET_INV_NO_STATE")
    assert inv.identity_evidence("box", db=str(tmp_path / "missing.db")) == {}


def _fake_verify(tmp_path, payload, rc=0):
    p = tmp_path / "fake-lumen-verify"
    p.write_text("#!/usr/bin/env python3\nimport sys, json\n"
                 f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                 f"print(json.dumps({payload!r}))\nsys.exit({rc})\n")
    p.chmod(0o755)
    return str(p)


def _run(tmp_path, payload, *args):
    env = dict(os.environ, LUMEN_VERIFY=_fake_verify(tmp_path, payload), FLEET_INV_NO_STATE="1")
    p = subprocess.run([sys.executable, os.path.join(TOP, "fleet-inv"), "verify", *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def _d(subject, pred, outcome, error=None, value="v"):
    return {"subject": subject, "predicate": pred, "probe": "node_identity", "value": value,
            "observed": None, "outcome": outcome, "error": error}


def test_verify_reports_mismatch_offline_and_exit_code(tmp_path):
    payload = {"due": 3, "counts": {"confirmed": 1, "unknown": 2},
               "detail": [_d("anubis", "hardware", "confirmed"),
                          _d("anubis", "nic", "unknown", "MISMATCH: stated MAC(s) ['aa:bb:cc:dd:ee:ff']"),
                          _d("apep", "hardware", "unknown", "ssh apep: connect timed out", "Whitebox")]}
    rc, out, err = _run(tmp_path, payload, "--all")
    assert rc == 2, err                       # a MISMATCH is a finding, not success
    assert "anubis:" in out and "MISMATCH" in out
    assert "apep: OFFLINE/unreachable" in out and "Whitebox" in out
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[0] == "node" and "--probes" in argv and "anubis" in argv and "apep" in argv


def test_verify_dry_run_passes_through_and_needs_a_target(tmp_path):
    payload = {"due": 1, "counts": {"confirmed": 1}, "detail": [_d("anubis", "hardware", "confirmed")]}
    rc, out, _ = _run(tmp_path, payload, "anubis", "--dry-run")
    assert rc == 0 and "--dry-run" in json.loads((tmp_path / "argv.json").read_text())
    rc, _, err = _run(tmp_path, payload)
    assert rc == 1 and "exactly one" in err
    rc, _, err = _run(tmp_path, payload, "not-a-node")
    assert rc == 1 and "not in inventory" in err


def test_verify_sudo_denied_is_answered_not_offline(tmp_path):
    payload = {"due": 2, "counts": {"confirmed": 1, "unknown": 1},
               "detail": [_d("sia", "hardware", "unknown", "sudo -n denied"),
                          _d("sia", "storage", "confirmed")]}
    rc, out, _ = _run(tmp_path, payload, "sia")
    assert rc == 0 and "OFFLINE" not in out and "sudo -n denied" in out


def test_superseded_row_counts_as_evidence_for_the_new_fact(tmp_path, monkeypatch):
    monkeypatch.delenv("FLEET_INV_NO_STATE", raising=False)
    db = _db(tmp_path,
             [("k0", "box", "kernel", "7.0.0-30", "2026-09-26T00:56:57", "2026-09-21T06:00:00"),
              ("k1", "box", "kernel", "7.0.0-31", None, "2026-09-26T00:56:57")],
             [("v1", "k0", "box", "kernel", "superseded", None, "2026-09-26T00:56:57"),
              ("v0", "k0", "box", "kernel", "confirmed", None, "2026-09-21T06:00:00")])
    assert inv.identity_evidence("box", db=db)["kernel"][1] == "confirmed"


def test_stale_supersession_is_not_evidence(tmp_path, monkeypatch):
    """A human re-ingested after the probe's supersession: the probe never saw this value."""
    monkeypatch.delenv("FLEET_INV_NO_STATE", raising=False)
    db = _db(tmp_path, [("k2", "box", "kernel", "7.0.0-32", None, "2026-09-27T00:00:00")],
             [("v2", "k1", "box", "kernel", "superseded", None, "2026-09-26T00:56:57")])
    assert inv.identity_evidence("box", db=db)["kernel"] == ("kernel", None, None, None)
