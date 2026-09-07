#!/usr/bin/env python3
# Render machines/*.yaml -> fleet.html (the page). Also validates every file parses.
# `render.py --md [--update FILE]` -> the FLEET_MAP.md roster table instead (fleet_md.py, wq#1456).
import yaml, glob, os, html, datetime, sys
if "--md" in sys.argv[1:]:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fleet_md
    sys.exit(fleet_md.main(sys.argv[1:]))
rows=[]
for f in sorted(glob.glob(os.path.join(os.path.dirname(__file__),"machines","*.yaml"))):
    try:
        d=yaml.safe_load(open(f)) or {}
    except Exception as e:
        print("PARSE ERROR in %s: %s"%(f,e)); raise
    rows.append(d)
order={"active":0,"standby":1,"bench":2,"in-storage":3,"decommissioned":4,"dead":5}
rows.sort(key=lambda d: (order.get((d.get("status") or {}).get("status",""),9), (d.get("identity") or {}).get("name","")))
def g(d,*ks):
    for k in ks: d=(d or {}).get(k) if isinstance(d,dict) else None
    return d
def esc(x): return html.escape(str(x)) if x is not None else ""
cells=[]
for d in rows:
    i,s,n,hw,gov=d.get("identity",{}),d.get("status",{}),d.get("network",{}),d.get("hardware",{}),d.get("governance",{})
    st=s.get("status","?"); color={"active":"#1a7f37","decommissioned":"#8250df","dead":"#cf222e","standby":"#9a6700"}.get(st,"#57606a")
    flags=[]
    if gov.get("crown_jewel"): flags.append("👑crown")
    if gov.get("never_restart"): flags.append("⛔no-restart")
    if s.get("monitor")==False: flags.append("🔕no-monitor")
    if gov.get("off_limits"): flags.append("🚫off-limits")
    cpu=g(hw,"cpu","model"); ram=g(hw,"ram","size_gb")
    cells.append(f"""<div class=card style="border-left:5px solid {color}">
      <div class=hdr><b>{esc(i.get('name'))}</b> <span class=st style="color:{color}">{esc(st)}</span>
        <span class=fl>{' '.join(flags)}</span></div>
      <div class=mm>{esc(i.get('make_model'))}</div>
      <div class=meta>ts {esc(n.get('tailscale_ip'))} · lan {esc(n.get('lan_ip'))} · {esc(cpu)} · {esc(ram)}GB</div>
      <div class=roles>roles: {esc(', '.join(d.get('roles') or []) or '—')}</div>
      <div class=iss>{esc('; '.join((d.get('ops') or {}).get('known_issues') or []))}</div>
    </div>""")
pagetime=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
open(os.path.join(os.path.dirname(__file__),"fleet.html"),"w").write(f"""<!doctype html><meta charset=utf-8>
<title>Lumen Fleet Inventory</title><style>
body{{font:14px/1.5 system-ui;background:#0d1117;color:#c9d1d9;margin:0;padding:24px}}
h1{{font-size:20px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px;margin:8px 0}}
.hdr{{font-size:16px}} .st{{font-size:11px;text-transform:uppercase;margin-left:8px}}
.fl{{font-size:11px;color:#8b949e;margin-left:6px}} .mm{{color:#8b949e}} .meta{{color:#6e7681;font-size:12px;margin-top:4px}}
.roles{{font-size:12px;margin-top:4px}} .iss{{color:#d29922;font-size:12px;margin-top:4px}}
</style><h1>Lumen Fleet Inventory — {len(rows)} machines</h1>
<div class=sub>source of truth: /home/lumen/fleet/inventory/machines/*.yaml · rendered {pagetime} · edit YAML then run render.py</div>
{''.join(cells)}""")
print(f"OK: {len(rows)} machines rendered -> fleet.html; all YAML valid")
