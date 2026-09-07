"""wq#1455 — render.py: atomic last-good page + always-written status. Temp root only."""
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import render  # noqa: E402
from test_fleet_inventory import _yaml  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_INV_NO_AQ", "1")
    m = tmp_path / "machines"
    m.mkdir()
    (m / "alpha.yaml").write_text(_yaml("alpha", roles=("worker", "gpu"),
                                        extra="  monitoring: {interval_s: 120, alert: false}\n"))
    (m / "gone.yaml").write_text(_yaml("gone", status="decommissioned", monitor=False, ts="100.64.0.2", lan="192.168.8.2"))
    return tmp_path


def _status(root):
    return json.loads((root / "www" / "render_status.json").read_text())


def _sha(root):
    return hashlib.sha256((root / "www" / "fleet.html").read_bytes()).hexdigest()


def test_success_writes_page_and_status(root):
    rc, st = render.render(str(root))
    assert rc == 0 and st["ok"] is True and st["machines"] == 2 and st["error"] is None
    assert sorted(os.listdir(root / "www")) == ["fleet.html", "render_status.json"]
    page = (root / "www" / "fleet.html").read_text()
    assert _status(root)["sha256"] == _sha(root)
    assert "re-renders automatically" in page
    assert "id=rs-banner" in page and "render_status.json" in page       # banner poll script
    assert "monitoring: interval 120s · alert off" in page             # monitoring block
    assert "decommissioned 2026-01-01: test" in page                   # status.decommissioned
    assert "monitoring: off" in page                                   # monitor:false card
    assert page.index(">alpha<") < page.index(">gone<")                # active sorts before decommissioned


def test_bad_yaml_keeps_last_good_and_reports_file_line(root):
    render.render(str(root))
    good = _sha(root)
    bad = root / "machines" / "alpha.yaml"
    bad.write_text(bad.read_text() + "\nthis is: [not, valid: yaml\n")
    rc, st = render.render(str(root))
    assert rc == 1 and st["ok"] is False
    assert st["error"].startswith(str(bad) + ":") and "YAML parse error" in st["error"]
    assert st["sha256"] == good and _sha(root) == good                  # page untouched
    assert _status(root)["ok"] is False
    assert not os.path.exists(root / "www" / "fleet.html.tmp")
    assert sorted(os.listdir(root / "www")) == ["fleet.html", "render_status.json"]


def test_validation_error_is_also_loud(root):
    render.render(str(root))
    good = _sha(root)
    (root / "machines" / "alpha.yaml").write_text(_yaml("alpha", status="bogus"))
    rc, st = render.render(str(root))
    assert rc == 1 and st["ok"] is False and "alpha.yaml:" in st["error"]
    assert _sha(root) == good


def test_fix_recovers(root):
    render.render(str(root))
    p = root / "machines" / "alpha.yaml"
    orig = p.read_text()
    p.write_text(orig + "\nthis is: [not, valid: yaml\n")
    assert render.render(str(root))[0] == 1
    p.write_text(orig.replace("roles: [worker, gpu]", "roles: [worker, gpu, fixed]"))
    rc, st = render.render(str(root))
    assert rc == 0 and st["ok"] is True and st["error"] is None
    assert "fixed" in (root / "www" / "fleet.html").read_text()
    assert st["sha256"] == _sha(root)


def test_first_render_failure_has_no_page(root):
    (root / "machines" / "alpha.yaml").write_text("identity: [broken\n")
    rc, st = render.render(str(root))
    assert rc == 1 and st["ok"] is False and st["sha256"] is None
    assert os.listdir(root / "www") == ["render_status.json"]
