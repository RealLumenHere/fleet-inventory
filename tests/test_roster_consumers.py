"""wq#1456 — consumer selectors (select / ssh_targets / plug_map), the fleet-inv ssh-list feed,
and the FLEET_MAP roster table (fleet_md). Temp inventory roots only, never the real one."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path.insert(0, TOP)
import fleet_inventory as inv  # noqa: E402
import fleet_md  # noqa: E402

from test_fleet_inventory import _yaml  # noqa: E402

CLI = os.path.join(TOP, "fleet-inv")


def _rec(name, status="active", monitor=True, ts="100.64.0.1", user=None, roles=("worker",),
         tier=None, crown=False, never_restart=False, plug=None, key=None, method="key", aliases=()):
    """One machine YAML with the wq#1456 fields."""
    user = name if user is None else user
    ops = "ops: {known_issues: []%s}\n" % (f", maint_tier: {tier}" if tier is not None else "")
    ssh = f"  ssh: {{user: {user}, method: {method}%s}}\n" % (f", key: {key}" if key else "")
    plug_s = f"hardware: {{power_hw: {{plug_id: {plug}}}}}\n" if plug else "hardware: {}\n"
    decom = "\n  decommissioned: {date: 2026-08-22, reason: retired}" if status == "decommissioned" else ""
    al = "[" + ", ".join(aliases) + "]"
    return (f"identity: {{name: {name}, aliases: {al}, make_model: box-{name}, serial: VERIFY, location: rack}}\n"
            f"status:\n  status: {status}\n  power: on\n  monitor: {str(monitor).lower()}{decom}\n"
            f"network:\n  tailscale_ip: {ts}\n  lan_ip: 192.168.8.{ts.split('.')[-1]}\n{ssh}  nics: []\n"
            f"{plug_s}roles: [{', '.join(roles)}]\nservices: []\nserves: []\n"
            f"governance: {{crown_jewel: {str(crown).lower()}, never_restart: {str(never_restart).lower()}, off_limits: false}}\n"
            f"{ops}")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_INV_NO_AQ", "1")
    monkeypatch.setenv("FLEET_INV_HOSTNAME", "hub")
    m = tmp_path / "machines"
    m.mkdir()
    (m / "hub.yaml").write_text(_rec("hub", ts="100.64.0.1", tier=5, crown=True, plug="P11",
                                     roles=("orchestrator-host",)))
    (m / "vault.yaml").write_text(_rec("vault", ts="100.64.0.2", user="vaultadm", tier=4, crown=True, plug="P4",
                                       aliases=("Va'ult",)))
    (m / "leaf.yaml").write_text(_rec("leaf", ts="100.64.0.3", tier=1, plug="P6"))
    (m / "serv.yaml").write_text(_rec("serv", ts="100.64.0.4", tier=2))
    (m / "infer.yaml").write_text(_rec("infer", ts="100.64.0.5", tier=1.5, roles=("inference",)))
    (m / "laptop.yaml").write_text(_rec("laptop", ts="100.64.0.6", never_restart=True))
    (m / "parked.yaml").write_text(_rec("parked", "standby", monitor=False, ts="100.64.0.7", tier=3))
    (m / "gone.yaml").write_text(_rec("gone", "decommissioned", monitor=False, ts="100.64.0.8", method="n/a", plug="P2"))
    (m / "noip.yaml").write_text(_rec("noip", ts="VERIFY", user="VERIFY"))
    (m / "bench.yaml").write_text(_rec("bench", "bench", ts="100.64.0.9", key="/tmp/bench_key"))
    return tmp_path


def _names(pairs):
    return [n for n, _ in pairs]


def test_select_filters(root):
    r = inv.load(root=root)
    assert [x["identity"]["name"] for x in inv.select(r)] == ["bench", "hub", "infer", "laptop", "leaf", "noip", "serv", "vault"]
    assert [x["identity"]["name"] for x in inv.select(r, monitored=False)] == sorted(r)
    assert [x["identity"]["name"] for x in inv.select(r, statuses=("active",), maintenance=True,
                                                       no_crown_jewel=True, exclude_self=True)] == ["infer", "leaf", "noip", "serv"]
    assert [x["identity"]["name"] for x in inv.select(r, monitored=False, statuses=("standby", "bench"))] == ["bench", "parked"]
    assert inv.is_local(r["hub"]) and not inv.is_local(r["leaf"])
    assert inv.is_local(r["vault"], hostname="va'ult")  # alias match, hostname param wins over env


def test_ssh_targets_resolution_and_skips(root):
    r = inv.load(root=root)
    skipped = []
    got = inv.ssh_targets(r, monitored=False, skipped=skipped)
    assert _names(got) == ["bench", "hub", "infer", "laptop", "leaf", "parked", "serv", "vault"]
    assert dict(got)["vault"] == "vaultadm@100.64.0.2"
    # decommissioned box with method n/a and the VERIFY box are skipped WITH a reason, never silently
    assert sorted(n for n, _ in skipped) == ["gone", "noip"]
    assert "not reachable by design" in dict(skipped)["gone"]
    with pytest.raises(inv.InventoryError):
        inv.ssh_target("gone", r)
    # local substitution + maintenance ordering (A11 §5): tier asc, unset tier (laptop) sorts last
    got = inv.ssh_targets(r, local="", order="maint", maintenance=False)
    assert got[0] == ("leaf", "leaf@100.64.0.3") and got[1][0] == "infer" and got[2][0] == "serv"
    assert got[-3:] == [("hub", ""), ("bench", "bench@100.64.0.9"), ("laptop", "laptop@100.64.0.6")]
    assert inv.maint_tier(r["laptop"]) == 6.0 and inv.maint_tier(r["infer"]) == 1.5
    assert inv.ssh_key(r["bench"]) == "/tmp/bench_key" and inv.ssh_key(r["leaf"], "/k") == "/k"


def test_plug_map_and_duplicate_detection(root):
    r = inv.load(root=root)
    assert inv.plug_map(r) == {"P11": "hub", "P4": "vault", "P6": "leaf", "P2": "gone"}
    (root / "machines" / "serv.yaml").write_text(_rec("serv", ts="100.64.0.4", tier=2, plug="P6"))
    with pytest.raises(inv.InventoryError):
        inv.plug_map(inv.load(root=root))


def test_validate_new_fields(root):
    (root / "machines" / "leaf.yaml").write_text(_rec("leaf", ts="100.64.0.3", tier="soon"))
    errs = inv.validate(root=root)
    assert any("maint_tier" in e for e in errs)
    (root / "machines" / "leaf.yaml").write_text(_rec("leaf", ts="100.64.0.3", tier=1).replace(
        "method: key}", "method: key, key: 7}"))
    assert any("ssh.key" in e for e in inv.validate(root=root))


def _cli(root, *args):
    env = dict(os.environ, FLEET_INVENTORY_ROOT=str(root), FLEET_INV_NO_AQ="1", FLEET_INV_HOSTNAME="hub")
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def test_cli_ssh_list_is_the_roster_feed(root):
    rc, out, err = _cli(root, "ssh-list")
    assert rc == 0
    assert out.splitlines() == ["bench\tbench@100.64.0.9", "hub\thub@100.64.0.1", "infer\tinfer@100.64.0.5",
                                "laptop\tlaptop@100.64.0.6", "leaf\tleaf@100.64.0.3", "serv\tserv@100.64.0.4",
                                "vault\tvaultadm@100.64.0.2"]
    assert "# noip:" in err and "gone" not in err  # gone is not monitored -> not even considered
    # fleet-roll-reboot shape: active, restartable, no crown jewels, not me
    rc, out, _ = _cli(root, "ssh-list", "--status", "active", "--maintenance", "--no-crown-jewel", "--exclude-self")
    assert out.split() == ["infer", "infer@100.64.0.5", "leaf", "leaf@100.64.0.3", "serv", "serv@100.64.0.4"]
    # rotate-fleet-pw shape: all live statuses, maint order, LOCAL for self, per-node key column
    rc, out, err = _cli(root, "ssh-list", "--all", "--status", "active,standby,bench", "--order", "maint",
                        "--self", "LOCAL", "--fields", "name,user,target,ssh_key")
    rows = [ln.split("\t") for ln in out.splitlines()]
    assert rows[0] == ["leaf", "leaf", "leaf@100.64.0.3", "-"]
    assert ["bench", "bench", "bench@100.64.0.9", "/tmp/bench_key"] in rows
    assert ["hub", "hub", "LOCAL", "-"] in rows
    assert [r[0] for r in rows][-3:] == ["hub", "bench", "laptop"]  # tier 5, then unset tiers by name
    assert "gone" not in out and "# noip:" in err
    rc, out, err = _cli(root, "ssh-list", "--status", "nope")
    assert rc == 1 and "--status" in err
    rc, out, _ = _cli(root, "plugs")
    assert out.splitlines() == ["P2\tgone\ton", "P4\tvault\ton", "P6\tleaf\ton", "P11\thub\ton"]


LEGACY = """# MAP

intro prose

## Core fleet

| Node | Hardware | Role | Status |
|---|---|---|---|
| Hub | big box | brain | [DEPLOYED] |
| Va'ult | small box | vault | [DEPLOYED] |
| Parked | mac | idle | [DEPLOYED-BUT-OFFLINE] |
| Sokar | i-view AIO | forensics | [PLANNED] |
| Guardian | Topton | firewall | [PLANNED] |

---

## Network

trailing prose
"""


def test_fleet_md_markers_and_legacy_conversion(root, tmp_path):
    r = inv.load(root=root)
    tbl = fleet_md.table(r, today="2026-09-07")
    lines = tbl.splitlines()
    assert lines[0] == fleet_md.BEGIN and lines[-1] == fleet_md.END
    by = {ln.split("|")[1].strip().split(" ")[0]: ln for ln in lines if ln.startswith("| ") and "Node" not in ln}
    assert "[DEPLOYED] |" in by["hub"] and "crown_jewel" in by["hub"] and "never_restart" in by["laptop"]
    assert "[DEPLOYED-BUT-OFFLINE] standby" in by["parked"] and "monitor: false" in by["parked"]
    assert "[DECOMMISSIONED] 2026-08-22" in by["gone"] and "retired" in by["gone"]
    assert "[DEPLOYED] bench" in by["bench"]
    assert by["vault"].startswith("| vault (Va'ult) |")
    # active rows first, then standby, bench, decommissioned (render.py order)
    order = [k for k in by]
    assert order.index("parked") > order.index("serv") and order[-1] == "gone"
    # sentinel_probes._fleet_map_expects_online vocabulary: an unmonitored row never reads as online
    for name, rec in r.items():
        if not inv._is_monitored_rec(rec):
            assert any(m in by[name] for m in ("[DEPLOYED-BUT-OFFLINE]", "[RETIRED]", "[DECOMMISSIONED]")), by[name]

    f = tmp_path / "MAP.md"
    f.write_text(LEGACY)
    diff = fleet_md.update(str(f), r, today="2026-09-07")
    new = f.read_text()
    assert diff.startswith("--- ") and "intro prose" in new and "trailing prose" in new and "## Network" in new
    assert fleet_md.BEGIN in new and fleet_md.END in new
    assert "| Hub | big box" not in new and "| Va'ult | small" not in new  # in inventory -> replaced
    assert "| Sokar | i-view AIO" in new and "| Guardian | Topton" in new  # not in inventory -> preserved
    assert new.index(fleet_md.RESIDUE_INTRO) > new.index(fleet_md.END)
    baks = [p for p in tmp_path.iterdir() if p.name.startswith("MAP.md.bak.")]
    assert len(baks) == 1 and baks[0].read_text() == LEGACY
    # idempotent: second run = no diff, no new .bak; marker block replaced in place (prose stays)
    assert fleet_md.update(str(f), r, today="2026-09-07") == ""
    assert len([p for p in tmp_path.iterdir() if p.name.startswith("MAP.md.bak.")]) == 1
    (root / "machines" / "leaf.yaml").write_text(_rec("leaf", "standby", monitor=False, ts="100.64.0.3", tier=1))
    r2 = inv.load(root=root)
    assert fleet_md.update(str(f), r2, today="2026-09-07", dry_run=True).count("[DEPLOYED-BUT-OFFLINE] standby") >= 1
    assert f.read_text() == new  # dry-run wrote nothing
    fleet_md.update(str(f), r2, today="2026-09-07")
    txt = f.read_text()
    assert txt.count(fleet_md.BEGIN) == 1 and "| Sokar | i-view AIO" in txt and "trailing prose" in txt
    with pytest.raises(ValueError):
        fleet_md.replace("no table here\n", "block", r)


def test_render_md_entrypoint(root):
    env = dict(os.environ, FLEET_INVENTORY_ROOT=str(root), FLEET_INV_NO_AQ="1")
    p = subprocess.run([sys.executable, os.path.join(TOP, "render.py"), "--md"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert p.stdout.startswith(fleet_md.BEGIN) and "| hub |" in p.stdout
    assert not (root / "fleet.html").exists()  # --md never renders the page


def test_real_inventory_feeds_are_sane():
    """Guards against the data regressions this ticket fixed: sia/sobek were VERIFY (silently absent
    from every ssh fan-out); nephthys must never resolve; thor is never a maintenance target."""
    r = inv.load(strict=True, fallback=False)
    t = dict(inv.ssh_targets(r))
    assert t["sia"] == "sia@100.111.15.120" and t["sobek"] == "sobek@100.86.156.91"
    assert "nephthys" not in dict(inv.ssh_targets(r, monitored=False))
    maint = dict(inv.ssh_targets(r, maintenance=True, local=""))
    assert "thor" not in maint and maint["lumen"] == "" or inv.is_local(r["lumen"]) is False
    assert inv.plug_map(r)["P11"] == "lumen" and inv.plug_map(r)["P3"] == "hathor"
    assert inv.ssh_key(r["thoth"]) == "/home/lumen/.ssh/thoth_ed25519"
    tiers = {n: inv.maint_tier(x) for n, x in r.items()}
    assert tiers["maat"] == 4 and tiers["lumen"] == 5 and tiers["bes"] == 2 and tiers["thor"] == 6
