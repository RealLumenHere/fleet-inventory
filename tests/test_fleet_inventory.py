"""wq#1453 — loader tests. Every test runs against a temp inventory root, never the real one."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import fleet_inventory as inv  # noqa: E402


def _yaml(name, status="active", monitor=True, ts="100.64.0.1", lan="192.168.8.1",
          user="lumen", roles=("worker",), extra=""):
    roles_s = "[" + ", ".join(roles) + "]"
    decom = "\n  decommissioned: {date: 2026-01-01, reason: test}" if status == "decommissioned" else ""
    return f"""identity: {{name: {name}, aliases: [], make_model: test, serial: VERIFY, location: bench}}
status:
  status: {status}
  power: on
  monitor: {str(monitor).lower()}{decom}
{extra}network:
  tailscale_ip: {ts}
  lan_ip: {lan}
  ssh: {{user: {user}, method: key}}
  nics: []
hardware: {{}}
roles: {roles_s}
services: []
serves: []
governance: {{crown_jewel: false, off_limits: false}}
ops: {{known_issues: []}}
"""


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_INV_NO_AQ", "1")
    monkeypatch.setenv("FLEET_INV_NO_STATE", "1")  # wq#1631: hermetic, never shells out to lumen_state
    m = tmp_path / "machines"
    m.mkdir()
    (m / "alpha.yaml").write_text(_yaml("alpha", "active", True, ts="100.64.0.10", user="alpha"))
    (m / "bravo.yaml").write_text(_yaml("bravo", "active", False, ts="100.64.0.11"))
    (m / "charlie.yaml").write_text(_yaml("charlie", "decommissioned", False, ts="100.64.0.12"))
    (m / "delta.yaml").write_text(_yaml("delta", "dead", True, ts="100.64.0.13"))
    (m / "echo.yaml").write_text(_yaml("echo", "in-storage", True, ts="100.64.0.14"))
    (m / "foxtrot.yaml").write_text(_yaml("foxtrot", "standby", True, ts="100.64.0.15", roles=("vault", "worker")))
    (m / "golf.yaml").write_text(_yaml("golf", "bench", True, ts="100.64.0.16",
                                       extra="  monitoring: {interval_s: 300, alert: false}\n"))
    (m / "_template.yaml").write_text("identity: {name: VERIFY}\nstatus: {status: VERIFY, monitor: VERIFY}\n")
    return tmp_path


def test_monitored_rule(root):
    r = inv.load(root=root)
    names = sorted(x["identity"]["name"] for x in inv.monitored(r))
    # active+true, standby+true, bench+true are in; decommissioned/dead/in-storage/monitor:false are out
    assert names == ["alpha", "foxtrot", "golf"]
    assert inv.is_monitored("alpha", r)
    assert not inv.is_monitored("bravo", r)      # monitor: false
    assert not inv.is_monitored("charlie", r)    # decommissioned
    assert not inv.is_monitored("delta", r)      # dead (monitor:true ignored)
    assert not inv.is_monitored("echo", r)       # in-storage
    assert not inv.is_monitored("nope", r)       # absent from inventory = not monitored


def test_template_is_skipped_and_defaults_applied(root):
    r = inv.load(root=root)
    assert "_template" not in r and "VERIFY" not in r
    assert r["alpha"]["status"]["monitoring"] == {"interval_s": 60, "alert": True, "alert_desc": "", "gatus": True}
    assert r["golf"]["status"]["monitoring"] == {"interval_s": 300, "alert": False, "alert_desc": "", "gatus": True}


def test_ssh_target_format(root):
    r = inv.load(root=root)
    assert inv.ssh_target("alpha", r) == "alpha@100.64.0.10"
    with pytest.raises(inv.InventoryError):
        inv.ssh_target("missing", r)
    (root / "machines" / "alpha.yaml").write_text(_yaml("alpha", user="VERIFY", ts="100.64.0.10"))
    r = inv.load(root=root)
    with pytest.raises(inv.InventoryError):
        inv.ssh_target("alpha", r)


def test_get_by_role_all(root):
    r = inv.load(root=root)
    assert inv.get("alpha", r)["network"]["tailscale_ip"] == "100.64.0.10"
    assert inv.get("missing", r) is None
    assert [x["identity"]["name"] for x in inv.by_role("vault", r)] == ["foxtrot"]
    assert [x["identity"]["name"] for x in inv.all(r)] == ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]


def test_last_good_written_atomically_on_clean_load(root):
    cache = root / inv.LAST_GOOD_NAME
    assert not cache.exists()
    inv.load(root=root)
    assert cache.exists()
    data = json.loads(cache.read_text())
    assert sorted(data["machines"]) == ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
    assert not [p for p in os.listdir(root) if p.endswith(".tmp")]


def test_parse_error_fallback_returns_last_good_and_prints_error(root, capsys):
    good = inv.load(root=root)
    (root / "machines" / "alpha.yaml").write_text("identity: {name: alpha\nstatus: [oops\n")
    r = inv.load(fallback=True, root=root)
    assert sorted(r) == sorted(good)
    assert r["alpha"]["network"]["tailscale_ip"] == "100.64.0.10"
    err = capsys.readouterr().err
    assert err.startswith("ERROR ")
    assert "alpha.yaml:" in err
    assert (root / inv.PARSE_ERROR_MARKER).exists()


def test_parse_error_strict_raises(root):
    inv.load(root=root)
    (root / "machines" / "alpha.yaml").write_text("identity: {name: alpha\nstatus: [oops\n")
    with pytest.raises(inv.InventoryError) as ei:
        inv.load(fallback=False, root=root)
    assert "alpha.yaml:" in str(ei.value)


def test_parse_error_without_cache_raises_even_with_fallback(root):
    (root / "machines" / "alpha.yaml").write_text(": : :\n  - [")
    with pytest.raises(inv.InventoryError):
        inv.load(fallback=True, root=root)


def test_validate_rejects_bad_status_and_monitor(root):
    (root / "machines" / "alpha.yaml").write_text(_yaml("alpha", status="retired"))
    errs = inv.validate(root=root)
    assert any("status.status 'retired'" in e for e in errs)
    (root / "machines" / "alpha.yaml").write_text(_yaml("alpha").replace("monitor: true", "monitor: yes-please"))
    errs = inv.validate(root=root)
    assert any("status.monitor must be a bool" in e for e in errs)
    (root / "machines" / "alpha.yaml").write_text(_yaml("alpha").replace("monitor: true", 'monitor: "true"'))
    errs = inv.validate(root=root)
    assert any("status.monitor must be a bool" in e for e in errs)
    with pytest.raises(inv.InventoryError):
        inv.load(root=root)


def test_validate_monitoring_block_and_name_mismatch(root):
    (root / "machines" / "alpha.yaml").write_text(
        _yaml("alpha", extra="  monitoring: {interval_s: -5, alert: maybe, bogus: 1}\n"))
    errs = "\n".join(inv.validate(root=root))
    assert "interval_s" in errs and "monitoring.alert must be a bool" in errs and "bogus" in errs
    (root / "machines" / "hotel.yaml").write_text(_yaml("india"))
    assert any("!= filename" in e for e in inv.validate(root=root))


def _errs(root, yaml_text):
    (root / "machines" / "alpha.yaml").write_text(yaml_text)
    return "\n".join(inv.validate(root=root))


def test_validate_identity_tailnet(root):
    base = _yaml("alpha")
    ok = base.replace("location: bench}", "location: bench, tailnet: {hostname: GL-BE3600, tags: [tag:lumen-fleet]}}")
    assert _errs(root, ok) == ""
    assert _errs(root, base.replace("location: bench}", "location: bench, tailnet: {hostname: x, tags: []}}")) == ""
    assert "must be a mapping" in _errs(root, base.replace("location: bench}", "location: bench, tailnet: nope}"))
    bad = base.replace("location: bench}", "location: bench, tailnet: {hostname: '', tags: lumen-fleet, extra: 1}}")
    errs = _errs(root, bad)
    assert "hostname must be a non-empty string" in errs and "tags must be a list" in errs and "unknown keys" in errs
    bad = base.replace("location: bench}", "location: bench, tailnet: {tags: [lumen-fleet, 'tag:']}}")
    assert _errs(root, bad).count("must look like 'tag:<name>'") == 2


def test_validate_storage_is_keyed_by_serial(root):
    def hw(entries):
        return _yaml("alpha").replace("hardware: {}", f"hardware: {{storage: {entries}}}")

    assert _errs(root, hw("[{type: nvme, serial: ABC123, bay: boot}, {type: ssd, serial: VERIFY}, {type: ssd, serial: VERIFY}]")) == ""
    assert "storage[0].serial is required" in _errs(root, hw("[{type: nvme, bay: boot}]"))
    assert "storage[0].serial is required" in _errs(root, hw("[{type: nvme, serial: ''}]"))
    assert "storage[0].serial is required" in _errs(root, hw("[{type: nvme, serial: 12345}]"))
    assert "serial 'ABC123' is duplicated" in _errs(root, hw("[{serial: ABC123}, {serial: ABC123}]"))
    assert "hardware.storage must be a list" in _errs(root, hw("{serial: ABC123}"))


def test_validation_error_with_fallback_serves_cache(root, capsys):
    inv.load(root=root)
    (root / "machines" / "alpha.yaml").write_text(_yaml("alpha", status="bogus"))
    r = inv.load(fallback=True, root=root)
    assert r["alpha"]["status"]["status"] == "active"
    assert "ERROR " in capsys.readouterr().err


def test_aq_edge_trigger_opens_once_and_clears_on_clean_load(root, monkeypatch):
    calls = {"ingest": 0, "resolved": 0}

    class _Cur:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows

    class _DB:
        def execute(self, sql, params=()):
            if sql.startswith("SELECT"):
                return _Cur([(1,)])
            calls["resolved"] += 1
            return _Cur([])
        def commit(self): pass
        def close(self): pass

    class FakeAQ:
        @staticmethod
        def ingest(**kw):
            assert kw["key"] == inv.AQ_KEY and kw["source"] == inv.AQ_SOURCE
            calls["ingest"] += 1
        @staticmethod
        def db(): return _DB()
        @staticmethod
        def now(): return "t"

    monkeypatch.delenv("FLEET_INV_NO_AQ")
    monkeypatch.setattr(inv, "_aq", lambda: FakeAQ)
    inv.load(root=root)
    (root / "machines" / "alpha.yaml").write_text("bad: [\n")
    inv.load(fallback=True, root=root)
    inv.load(fallback=True, root=root)
    assert calls["ingest"] == 1                      # edge-triggered, not level
    (root / "machines" / "alpha.yaml").write_text(_yaml("alpha"))
    inv.load(root=root)
    assert calls["resolved"] == 1                    # resolved on next clean load
    assert not (root / inv.PARSE_ERROR_MARKER).exists()
    inv.load(root=root)
    assert calls["resolved"] == 1                    # no repeat clear without a new edge


def test_observed_reads_state_facts_over_yaml(root):
    """wq#1631: observed() only trusts predicates it can parse; NO-FACT keys are simply absent
    so callers fall back to the declared YAML value."""
    r = inv.load(root=root)
    fake = {"tailscale_ip": "100.99.0.1 (since 2026-01-01)", "ssh_user": "opsuser",
            "power_plug": "P7 — definitively (since 2026-01-01)"}
    live = inv.observed("alpha", state_fn=lambda name: fake if name == "alpha" else {})
    assert live == {"tailscale_ip": "100.99.0.1", "ssh_user": "opsuser", "plug_id": "P7"}
    assert inv.observed("bravo", state_fn=lambda name: {}) == {}
    # wq#1692: hostname keeps its spaces and loses the store's trailing annotations; a present
    # tailnet_tags fact with no tag in it is [] (user-owned), an absent one is simply not there
    tn = {"tailnet_hostname": "Galaxy S26 Ultra  (since 2026-09-10)  [UNVERIFIABLE — no probe]",
          "tailnet_tags": "none"}
    assert inv.observed("alpha", state_fn=lambda name: tn) == {
        "tailnet_hostname": "Galaxy S26 Ultra", "tailnet_tags": []}
    tagged = {"tailnet_tags": "tag:lumen-fleet, tag:b (since 2026-09-10)"}
    assert inv.observed("alpha", state_fn=lambda name: tagged) == {"tailnet_tags": ["tag:b", "tag:lumen-fleet"]}
    # ssh_target prefers the live value; NO-FACT (empty state_fn) falls back to the YAML network block
    assert inv.ssh_target("alpha", r, state_fn=lambda name: fake) == "opsuser@100.99.0.1"
    assert inv.ssh_target("alpha", r, state_fn=lambda name: {}) == "alpha@100.64.0.10"


def test_real_inventory_records_tailnet_identity():
    """wq#1666/#1692: 6 nodes carry tag:lumen-fleet, 3 tailnet hostnames differ from the inventory name."""
    r = inv.load(strict=True, fallback=False, root=os.path.dirname(HERE))
    tn = {x["identity"]["name"]: x["identity"]["tailnet"] for x in inv.all(r)}
    assert len(tn) == 25
    assert sorted(n for n, t in tn.items() if t["tags"]) == ["bes", "khepri", "nephthys", "sia", "sobek", "wadjet"]
    assert all(t["tags"] == ["tag:lumen-fleet"] for t in tn.values() if t["tags"])
    assert {n: t["hostname"] for n, t in tn.items() if t["hostname"] != n} == {
        "be3600": "GL-BE3600", "toilet": "toilet-940X5N", "phone": "Galaxy S26 Ultra"}
    for x in inv.all(r):
        assert all(e["serial"] for e in (x.get("hardware") or {}).get("storage") or [])


def test_real_inventory_is_valid():
    """The checked-in roster must always pass strict validation."""
    real_root = os.path.dirname(HERE)
    assert inv.validate(root=real_root) == []
    r = inv.load(strict=True, fallback=False, root=real_root)
    assert not inv.is_monitored("nephthys", r)
    assert inv.is_monitored("lumen", r)
    assert r["lumen"]["status"]["monitoring"]["gatus"] is False
