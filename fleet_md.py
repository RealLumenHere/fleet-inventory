#!/usr/bin/env python3
"""fleet_md — the FLEET_MAP.md roster TABLE, derived from machines/*.yaml (wq#1456, DESIGN_SOT T4).

    python3 render.py --md                       # print the table
    python3 render.py --md --update FLEET_MAP.md # replace the table in place (timestamped .bak first)
    python3 render.py --md --update FILE --dry-run   # unified diff only, no write

The table lives between BEGIN/END markers; prose around it is untouched. On the first run the
hand-kept legacy table (header '| Node | Hardware | Role | Status |') is replaced by the markers.
Legacy rows for boxes that are NOT in the inventory (planned hardware without a canonical name)
are carried over verbatim into a small hand-kept table right after the END marker, so nothing
is lost and the residue is visible.

Status markers keep the vocabulary sentinel_probes._fleet_map_expects_online() reads:
  [DEPLOYED] · [DEPLOYED-BUT-OFFLINE] · [RETIRED] · [DECOMMISSIONED]
so a row for an unmonitored box never reads as 'expected online'.
"""
import argparse
import datetime
import difflib
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_inventory as inv  # noqa: E402

BEGIN = ("<!-- BEGIN inventory-generated roster (do not hand-edit — run: "
         "python3 /home/lumen/fleet/inventory/render.py --md --update /home/lumen/FLEET_MAP.md) -->")
END = "<!-- END inventory-generated roster -->"
LEGACY_HEADER = "| Node | Hardware | Role | Status |"
RESIDUE_INTRO = ("_Not yet in the inventory (hand-kept until the box gets a canonical name and a "
                 "`machines/<name>.yaml`):_")
ORDER = {"active": 0, "standby": 1, "bench": 2, "in-storage": 3, "decommissioned": 4, "dead": 5}
HEADER = "| Node | Status | Make / model | Roles | Tailscale IP | LAN IP | Notes |"
SEP = "|---|---|---|---|---|---|---|"


def marker(rec):
    """Status cell. Monitored -> [DEPLOYED]; every unmonitored shape carries a marker sentinel
    treats as 'not expected online'."""
    st = rec.get("status") or {}
    s = st.get("status", "?")
    if inv._is_monitored_rec(rec):
        return "[DEPLOYED]" if s == "active" else f"[DEPLOYED] {s}"
    if s == "decommissioned":
        d = (st.get("decommissioned") or {}).get("date")
        return f"[DECOMMISSIONED] {d}" if d else "[DECOMMISSIONED]"
    if s in ("dead", "in-storage"):
        return f"[RETIRED] {s}"
    if s in ("standby", "bench") or str(st.get("power")) in ("off", "plug-cut"):
        return f"[DEPLOYED-BUT-OFFLINE] {s}"
    return f"[DEPLOYED] {s}, unmonitored"


def _cell(v):
    v = "" if v is None else str(v)
    return v.replace("|", "\\|").replace("\n", " ").strip() or "-"


def row(rec):
    ident, st, net = rec.get("identity") or {}, rec.get("status") or {}, rec.get("network") or {}
    notes = []
    if st.get("status") == "decommissioned":
        notes.append(str((st.get("decommissioned") or {}).get("reason") or ""))
    if not inv._is_monitored_rec(rec) and st.get("status") not in ("decommissioned", "dead", "in-storage"):
        notes.append("monitor: false")
    if (rec.get("governance") or {}).get("never_restart"):
        notes.append("never_restart")
    if (rec.get("governance") or {}).get("crown_jewel"):
        notes.append("crown_jewel")
    aliases = [str(a) for a in (ident.get("aliases") or [])]
    name = ident.get("name", "?") + (f" ({', '.join(aliases)})" if aliases else "")
    return "| " + " | ".join(_cell(x) for x in (
        name, marker(rec), ident.get("make_model"), ", ".join(rec.get("roles") or []),
        net.get("tailscale_ip"), net.get("lan_ip"), "; ".join(n for n in notes if n))) + " |"


def table(records=None, root=None, today=None):
    recs = sorted(inv.all(records, root),
                  key=lambda d: (ORDER.get((d.get("status") or {}).get("status"), 9), d["identity"]["name"]))
    today = today or datetime.date.today().isoformat()
    n_mon = sum(1 for r in recs if inv._is_monitored_rec(r))
    lines = [BEGIN,
             f"_Derived from `/home/lumen/fleet/inventory/machines/*.yaml` on {today}: {len(recs)} machines, "
             f"{n_mon} monitored. Edit the YAML, not this table; `fleet-inv list` shows the same data._",
             "", HEADER, SEP]
    lines += [row(r) for r in recs]
    lines.append(END)
    return "\n".join(lines) + "\n"


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _known_names(records):
    out = set()
    for rec in records.values():
        ident = rec.get("identity") or {}
        out.add(_norm(str(ident.get("name", ""))))
        out |= {_norm(str(a)) for a in (ident.get("aliases") or [])}
    return out


def replace(text, block, records):
    """Return text with the roster table replaced by `block`. Marker block if present, else the
    legacy table (rows not in inventory are preserved after END). Raises ValueError if neither."""
    lines = text.split("\n")
    if BEGIN in lines and END in lines:
        b, e = lines.index(BEGIN), lines.index(END)
        keep = lines[:b] + block.rstrip("\n").split("\n") + lines[e + 1:]
        return "\n".join(keep)
    try:
        h = lines.index(LEGACY_HEADER)
    except ValueError:
        raise ValueError(f"no marker block and no legacy table header {LEGACY_HEADER!r} in file")
    e = h + 1
    while e < len(lines) and lines[e].startswith("|"):
        e += 1
    known = _known_names(records)
    residue = []
    for ln in lines[h + 2:e]:
        cell = ln.split("|")[1].strip() if ln.count("|") >= 2 else ""
        if cell and _norm(cell.split("(")[0]) not in known:
            residue.append(ln)
    out = block.rstrip("\n").split("\n")
    if residue:
        out += ["", RESIDUE_INTRO, "", LEGACY_HEADER, "|---|---|---|---|"] + residue
    return "\n".join(lines[:h] + out + lines[e:])


def update(path, records, dry_run=False, today=None):
    """Rewrite `path` in place (timestamped .bak sibling first). Returns the unified diff ('' = no change)."""
    old = open(path, encoding="utf-8").read()
    new = replace(old, table(records, today=today), records)
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=path, tofile=path + " (inventory-derived)"))
    if not diff or dry_run:
        return diff
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy2(path, f"{path}.bak.{ts}")
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return diff


def main(argv=None):
    ap = argparse.ArgumentParser(prog="render.py --md", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--md", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--update", metavar="FILE", help="replace the roster table inside FILE (e.g. /home/lumen/FLEET_MAP.md)")
    ap.add_argument("--dry-run", action="store_true", help="with --update: print the diff, write nothing")
    ap.add_argument("--root", help="inventory root (default: this checkout / $FLEET_INVENTORY_ROOT)")
    args = ap.parse_args(argv)
    try:
        records = inv.load(strict=True, fallback=False, root=args.root)
    except inv.InventoryError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 1
    if not args.update:
        sys.stdout.write(table(records))
        return 0
    try:
        diff = update(args.update, records, dry_run=args.dry_run)
    except (OSError, ValueError) as e:
        print(f"ERROR {args.update}: {e}", file=sys.stderr)
        return 1
    if not diff:
        print(f"{args.update}: roster table already current", file=sys.stderr)
        return 0
    if args.dry_run:
        sys.stdout.write(diff)
    else:
        print(f"{args.update}: roster table replaced ({diff.count(chr(10))} diff lines); .bak written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
