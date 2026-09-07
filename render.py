#!/usr/bin/env python3
"""Render machines/*.yaml -> www/fleet.html (wq#1455, DESIGN_SOT.md "Serve").

Strict load through fleet_inventory (NO fallback): a bad edit fails loudly here.
  - www/fleet.html is replaced atomically (tmp + os.replace) ONLY on success.
  - www/render_status.json is ALWAYS written atomically:
      {ok, ts_utc, error (file:line message | null), machines, sha256}
    sha256 = digest of the fleet.html currently on disk (last-good on failure).
The served page polls render_status.json every 30 s and shows a red banner when
ok=false; the page below the banner is the last-good render.

Exit status: 0 on success, 1 on inventory error (status file still written).
"""
import datetime
import hashlib
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fleet_inventory  # noqa: E402

EDIT_HINT = "edit: /home/lumen/fleet/inventory/machines/<name>.yaml — re-renders automatically"

ORDER = {"active": 0, "standby": 1, "bench": 2, "in-storage": 3, "decommissioned": 4, "dead": 5}
COLOR = {"active": "#1a7f37", "decommissioned": "#8250df", "dead": "#cf222e", "standby": "#9a6700"}

# Kept deliberately short (~10 lines): poll status, show/hide the banner.
BANNER_SCRIPT = """<div id=rs-banner></div>
<script>
async function rs(){try{const r=await fetch('render_status.json?t='+Date.now(),{cache:'no-store'});
const s=await r.json();const b=document.getElementById('rs-banner');
if(s.ok){b.style.display='none';b.textContent='';}
else{b.style.display='block';b.textContent='RENDER FAILED '+(s.ts_utc||'')+' — page below is the LAST GOOD render. '+(s.error||'');}
}catch(e){}}
rs();setInterval(rs,30000);
</script>"""


def _g(d, *ks):
    for k in ks:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sha256(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        return None


def _monitoring_line(s):
    if s.get("monitor") is False:
        return "monitoring: off"
    m = s.get("monitoring") or {}
    interval = m.get("interval_s", fleet_inventory.MONITORING_DEFAULTS["interval_s"])
    alert = m.get("alert", fleet_inventory.MONITORING_DEFAULTS["alert"])
    parts = [f"interval {esc(interval)}s", "alert " + ("on" if alert else "off")]
    if m.get("alert_desc"):
        parts.append(f"“{esc(m['alert_desc'])}”")
    if m.get("gatus") is False:
        parts.append("no gatus row")
    return "monitoring: " + " · ".join(parts)


def card(d):
    i, s, n = d.get("identity") or {}, d.get("status") or {}, d.get("network") or {}
    hw, gov = d.get("hardware") or {}, d.get("governance") or {}
    st = s.get("status", "?")
    color = COLOR.get(st, "#57606a")
    flags = []
    if gov.get("crown_jewel"):
        flags.append("👑crown")
    if gov.get("never_restart"):
        flags.append("⛔no-restart")
    if s.get("monitor") is False:
        flags.append("🔕no-monitor")
    if gov.get("off_limits"):
        flags.append("🚫off-limits")
    decom = ""
    dc = s.get("decommissioned")
    if isinstance(dc, dict) and dc:
        decom = (f'<div class=decom>decommissioned {esc(dc.get("date"))}'
                 f'{": " + esc(dc.get("reason")) if dc.get("reason") else ""}</div>')
    cpu, ram = _g(hw, "cpu", "model"), _g(hw, "ram", "size_gb")
    issues = "; ".join((d.get("ops") or {}).get("known_issues") or [])
    return f"""<div class=card style="border-left:5px solid {color}">
      <div class=hdr><b>{esc(i.get('name'))}</b> <span class=st style="color:{color}">{esc(st)}</span>
        <span class=fl>{' '.join(flags)}</span></div>
      <div class=mm>{esc(i.get('make_model'))}</div>
      <div class=meta>ts {esc(n.get('tailscale_ip'))} · lan {esc(n.get('lan_ip'))} · {esc(cpu)} · {esc(ram)}GB</div>
      <div class=mon>{_monitoring_line(s)}</div>
      <div class=roles>roles: {esc(', '.join(d.get('roles') or []) or '—')}</div>
      {decom}
      <div class=iss>{esc(issues)}</div>
    </div>"""


def render_page(records, now_utc):
    rows = sorted(records.values(),
                  key=lambda d: (ORDER.get(_g(d, "status", "status") or "", 9), _g(d, "identity", "name") or ""))
    pagetime = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html><meta charset=utf-8>
<title>Lumen Fleet Inventory</title><style>
body{{font:14px/1.5 system-ui;background:#0d1117;color:#c9d1d9;margin:0;padding:24px}}
h1{{font-size:20px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px;margin:8px 0}}
.hdr{{font-size:16px}} .st{{font-size:11px;text-transform:uppercase;margin-left:8px}}
.fl{{font-size:11px;color:#8b949e;margin-left:6px}} .mm{{color:#8b949e}} .meta{{color:#6e7681;font-size:12px;margin-top:4px}}
.mon{{color:#8b949e;font-size:12px;margin-top:2px}} .roles{{font-size:12px;margin-top:4px}}
.decom{{color:#a371f7;font-size:12px;margin-top:4px}} .iss{{color:#d29922;font-size:12px;margin-top:4px}}
#rs-banner{{display:none;background:#cf222e;color:#fff;font-weight:600;padding:10px 14px;margin:-24px -24px 16px;white-space:pre-wrap}}
</style>{BANNER_SCRIPT}<h1>Lumen Fleet Inventory — {len(rows)} machines</h1>
<div class=sub>source of truth: /home/lumen/fleet/inventory/machines/*.yaml · rendered {pagetime}<br>{esc(EDIT_HINT)}</div>
{''.join(card(d) for d in rows)}"""


def render(root=None):
    """Render inventory at <root> (default: this repo) into <root>/www. Returns (rc, status)."""
    root = os.path.abspath(root or HERE)
    www = os.path.join(root, "www")
    page, status_path = os.path.join(www, "fleet.html"), os.path.join(www, "render_status.json")
    now = datetime.datetime.now(datetime.timezone.utc)
    os.makedirs(www, exist_ok=True)
    status = {"ok": False, "ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "error": None, "machines": None, "sha256": None}
    rc = 0
    try:
        # strict, NO fallback: a bad edit must fail loudly here, never render a cached roster.
        records = fleet_inventory.load(strict=True, fallback=False, root=None if root == HERE else root)
        _atomic_write(page, render_page(records, now))
        status.update(ok=True, machines=len(records))
    except fleet_inventory.InventoryError as e:
        status["error"] = str(e)
        print(f"ERROR {str(e).splitlines()[0]}", file=sys.stderr)
        rc = 1
    status["sha256"] = _sha256(page)  # last-good page's digest when rc != 0
    _atomic_write(status_path, json.dumps(status, indent=1, sort_keys=True) + "\n")
    if rc == 0:
        print(f"OK: {status['machines']} machines rendered -> {page}")
    return rc, status


def main(argv):
    root = None
    if len(argv) == 3 and argv[1] == "--root":
        root = argv[2]
    elif len(argv) != 1:
        print("usage: render.py [--root DIR]", file=sys.stderr)
        return 2
    return render(root)[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
