"""wq#1454 — gen_gatus_nodes.py: block rendering, first-run splice, idempotent re-splice,
byte-preservation outside the markers. No gatus binary, no network."""
import os
import sys

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import fleet_inventory as inv  # noqa: E402
import gen_gatus_nodes as gen  # noqa: E402
from test_fleet_inventory import _yaml  # noqa: E402

LEGACY = """# header
web:
  port: 8418

storage:
  type: sqlite
  path: /data/gatus.db

endpoints:
  - name: wan-icmp
    group: 0-backhaul
    url: "icmp://1.1.1.1"
    interval: 30s
    conditions: ["[CONNECTED] == true"]

  # ── FLEET NODES (Tailscale ICMP) ──
  - name: old1
    group: 1-nodes
    url: "icmp://100.64.0.99"
    interval: 60s
    conditions: ["[CONNECTED] == true"]
    alerts: [{type: ntfy}]
# DISABLED 2026-07-18: gone — node powered down
#  - name: gone
#    group: 1-nodes
#    url: "icmp://100.64.0.98"
#    conditions: ["[CONNECTED] == true"]

  # ── LUMEN CORE SERVICES ──
  - name: old2
    group: 1-nodes
    url: "icmp://100.64.0.97"
    interval: 300s
    conditions: ["[CONNECTED] == true"]
    # history only
  - name: litellm
    group: 2-lumen-services
    url: "http://localhost:4000/health/liveliness"
    interval: 60s
    conditions: ["[STATUS] == 200"]

external-endpoints:
  - name: old1-sshauth
    group: 1-nodes
    token: "tok"
"""


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_INV_NO_AQ", "1")
    m = tmp_path / "machines"
    m.mkdir()
    (m / "alpha.yaml").write_text(_yaml("alpha", "active", True, ts="100.64.0.10",
                                        extra='  monitoring: {interval_s: 120, alert: true, alert_desc: "alpha down — vault"}\n'))
    (m / "bravo.yaml").write_text(_yaml("bravo", "active", True, ts="100.64.0.11",
                                        extra="  monitoring: {alert: false, interval_s: 300}\n"))
    (m / "charlie.yaml").write_text(_yaml("charlie", "active", False, ts="100.64.0.12"))       # monitor:false
    (m / "delta.yaml").write_text(_yaml("delta", "decommissioned", False, ts="100.64.0.13"))
    (m / "echo.yaml").write_text(_yaml("echo", "active", True, ts="100.64.0.14",
                                       extra="  monitoring: {gatus: false}\n"))                # self-ping off
    (m / "foxtrot.yaml").write_text(_yaml("foxtrot", "active", True, ts="VERIFY"))            # no ip -> no row
    (m / "golf.yaml").write_text(_yaml("golf", "bench", True, ts="100.64.0.16"))              # defaults
    return tmp_path


def _rows(root):
    return gen.node_rows(inv.load(root=root), inv)


def test_node_rows_rule(root):
    rows = _rows(root)
    assert [r[0] for r in rows] == ["alpha", "bravo", "golf"]
    assert rows[0] == ("alpha", "100.64.0.10", 120, True, "alpha down — vault")
    assert rows[1] == ("bravo", "100.64.0.11", 300, False, "bravo unreachable")
    assert rows[2] == ("golf", "100.64.0.16", 60, True, "golf unreachable")


def test_render_block_shape(root):
    block = gen.render_block(_rows(root))
    assert block[0] == gen.BEGIN and block[-1] == gen.END
    text = "\n".join(block[1:-1])
    doc = yaml.safe_load(text)
    assert [e["name"] for e in doc] == ["alpha", "bravo", "golf"]
    assert doc[0]["alerts"] == [{"type": "ntfy", "description": "alpha down — vault"}]
    assert "alerts" not in doc[1]                       # alert:false -> no alerts key
    assert doc[0]["url"] == "icmp://100.64.0.10" and doc[0]["interval"] == "120s"
    assert doc[0]["conditions"] == ["[CONNECTED] == true"]
    assert all(e["group"] == "1-nodes" for e in doc)
    assert "\\u2014" not in text                        # unicode kept literal, not escaped


def test_first_run_replaces_legacy_block_only(root):
    block = gen.render_block(_rows(root))
    new = gen.splice(LEGACY, block)
    lines = new.split("\n")
    b, e = lines.index(gen.BEGIN), lines.index(gen.END)
    old_lines = LEGACY.split("\n")
    hdr = old_lines.index("  # ── FLEET NODES (Tailscale ICMP) ──")
    lit = old_lines.index("  - name: litellm")
    assert lines[:hdr + 1] == old_lines[:hdr + 1]       # everything above the header untouched
    assert lines[e + 1:] == old_lines[lit:]             # everything from litellm on untouched
    assert b == hdr + 1
    assert "  - name: old1\n" not in new and "  - name: old2\n" not in new and "name: gone" not in new
    assert "LUMEN CORE SERVICES" not in new
    assert "old1-sshauth" in new                        # external-endpoints untouched
    doc = yaml.safe_load(new)
    assert [x["name"] for x in doc["endpoints"]] == ["wan-icmp", "alpha", "bravo", "golf", "litellm"]


def test_resplice_is_idempotent_and_updates_between_markers(root):
    block = gen.render_block(_rows(root))
    once = gen.splice(LEGACY, block)
    assert gen.splice(once, block) == once
    # park bravo -> only the block changes
    (root / "machines" / "bravo.yaml").write_text(_yaml("bravo", "active", False, ts="100.64.0.11"))
    twice = gen.splice(once, gen.render_block(_rows(root)))
    a, b = once.split("\n"), twice.split("\n")
    assert a[:a.index(gen.BEGIN) + 1] == b[:b.index(gen.BEGIN) + 1]
    assert a[a.index(gen.END):] == b[b.index(gen.END):]
    assert "bravo" not in twice and "alpha" in twice


def test_splice_refuses_half_markers(root):
    block = gen.render_block(_rows(root))
    with pytest.raises(SystemExit):
        gen.splice(LEGACY.replace("  # ── FLEET NODES (Tailscale ICMP) ──", gen.BEGIN), block)
    with pytest.raises(SystemExit):
        gen.splice(LEGACY.replace("FLEET NODES", "NOTHING HERE"), block)


def test_last_good_fallback_keeps_block(root):
    block_ok = gen.render_block(_rows(root))
    (root / "machines" / "alpha.yaml").write_text("identity: {name: alpha\nstatus: [broken")
    r = inv.load(root=root, fallback=True)               # last-good from the earlier clean load
    assert gen.render_block(gen.node_rows(r, inv)) == block_ok


def test_main_check_and_print(root, tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(LEGACY)
    assert gen.main(["--inventory", str(root), "--config", str(cfg), "--check"]) == 1
    assert gen.main(["--inventory", str(root), "--config", str(cfg), "--write", "--no-validate"]) == 0
    assert gen.main(["--inventory", str(root), "--config", str(cfg), "--check"]) == 0
    baks = [p for p in tmp_path.iterdir() if p.name.startswith("config.yaml.bak.")]
    assert len(baks) == 1 and baks[0].read_text() == LEGACY
    written = yaml.safe_load(cfg.read_text())
    assert [x["name"] for x in written["endpoints"]] == ["wan-icmp", "alpha", "bravo", "golf", "litellm"]
    capsys.readouterr()
    assert gen.main(["--inventory", str(root), "--config", str(cfg), "--print"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(gen.BEGIN) and out.endswith(gen.END) and "thoth" not in out
