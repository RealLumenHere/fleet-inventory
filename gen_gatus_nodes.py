#!/usr/bin/env python3
"""gen_gatus_nodes.py — regenerate the Gatus `1-nodes` ICMP block from the inventory (wq#1454).

The block between the two marker lines in /home/lumen/sentinel/config/config.yaml

    # BEGIN inventory-generated 1-nodes (do not hand-edit — run gen_gatus_nodes.py)
    # END inventory-generated 1-nodes

is owned by this script. Every other byte of the file is preserved verbatim.
On the very first run (no markers yet) the old hand-written block — everything from
the `# ── FLEET NODES (Tailscale ICMP) ──` header up to the first endpoint whose
group is not 1-nodes, including commented-out entries — is replaced by the markers.

Per monitored node (fleet_inventory.monitored(), i.e. status.monitor true and
status.status not decommissioned/dead/in-storage) with status.monitoring.gatus true
and a usable network.tailscale_ip:

    - name: <name>
      group: 1-nodes
      url: "icmp://<tailscale_ip>"
      interval: <status.monitoring.interval_s>s
      conditions: ["[CONNECTED] == true"]
      alerts: [{type: ntfy, description: "<alert_desc or '<name> unreachable'>"}]   # alert:true only

The inventory is read with fleet_inventory.load(fallback=True): a YAML typo serves
the last-good roster (ERROR on stderr, aq inventory:parse-error) instead of blanking
the block.

Validate-before-install (playbooks/fleet-ops.md "Validate Gatus config before reload"):
`--write` first starts /home/lumen/bin/gatus against a temp copy of the config dir on
a scratch web port (storage forced to memory), requires HTTP 200 on /health within
10s, kills it, and only then takes a timestamped .bak sibling and writes the file IN
PLACE (same inode — the container bind-mounts it; os.replace would strand the mount).

Reload: Gatus polls the loaded config file's mtime every 30s and restarts its
monitoring on change (main.go listenToConfigurationFileChanges /
HasLoadedConfigurationBeenModified, verified at tag v5.10.0 and present in the local
v5.36.0 binary), so no container restart is needed. `--wait` polls
/api/v1/endpoints/statuses until the 1-nodes ICMP set matches the generated block.

Usage:
  gen_gatus_nodes.py [--config PATH] [--inventory ROOT] [--gatus BIN]
                     (--check | --print | --write [--no-validate] [--wait [SECONDS]])
Exit codes: 0 ok / up to date; 1 changes pending (--check); 2 validation or I/O failure.
"""
import argparse
import difflib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

INVENTORY_ROOT = os.environ.get("FLEET_INVENTORY_ROOT", os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = "/home/lumen/sentinel/config/config.yaml"
GATUS_BIN = "/home/lumen/bin/gatus"
GATUS_API = "http://127.0.0.1:8418"
BEGIN = "# BEGIN inventory-generated 1-nodes (do not hand-edit — run gen_gatus_nodes.py)"
END = "# END inventory-generated 1-nodes"
LEGACY_HEADER_RE = re.compile(r"^\s*#.*FLEET NODES")
ENTRY_RE = re.compile(r"^  - name:\s*(\S+)")
GROUP_RE = re.compile(r"^\s*group:\s*(\S+)")
HEALTH_TIMEOUT_S = 10


def _inventory_module(root):
    sys.path.insert(0, root)
    import fleet_inventory  # noqa: E402
    return fleet_inventory


def _clean(v):
    return None if v in (None, "", "VERIFY", "n/a") else str(v)


# ----------------------------------------------------------------------- render
def node_rows(records, inv):
    """[(name, ip, interval_s, alert, desc)] for every node that gets a Gatus row."""
    rows = []
    for rec in inv.monitored(records):
        name = rec["identity"]["name"]
        mon = (rec.get("status") or {}).get("monitoring") or {}
        if mon.get("gatus", True) is False:
            continue
        ip = _clean((rec.get("network") or {}).get("tailscale_ip"))
        if not ip:
            print(f"WARN {name}: monitored but no tailscale_ip — no Gatus row", file=sys.stderr)
            continue
        desc = mon.get("alert_desc") or f"{name} unreachable"
        rows.append((name, ip, int(mon.get("interval_s", 60)), bool(mon.get("alert", True)), desc))
    return sorted(rows)


def render_block(rows):
    out = [BEGIN]
    for name, ip, interval, alert, desc in rows:
        out += [f"  - name: {name}",
                "    group: 1-nodes",
                f'    url: "icmp://{ip}"',
                f"    interval: {interval}s",
                '    conditions: ["[CONNECTED] == true"]']
        if alert:
            out.append(f"    alerts: [{{type: ntfy, description: {json.dumps(desc, ensure_ascii=False)}}}]")
    out.append(END)
    return out


# ----------------------------------------------------------------------- splice
def _legacy_span(lines):
    """(start, end) line indexes of the hand-written block: from the line after the
    FLEET NODES header to the first uncommented endpoint whose group is not 1-nodes."""
    start = next((i for i, ln in enumerate(lines) if LEGACY_HEADER_RE.match(ln)), None)
    if start is None:
        raise SystemExit("no markers and no '# ── FLEET NODES' header in config — refusing to guess where the block is")
    i = start + 1
    end = None
    while i < len(lines):
        if ENTRY_RE.match(lines[i]):
            grp = None
            for j in range(i + 1, min(i + 4, len(lines))):
                m = GROUP_RE.match(lines[j])
                if m:
                    grp = m.group(1)
                    break
            if grp != "1-nodes":
                end = i
                break
        i += 1
    if end is None:
        raise SystemExit("could not find the end of the legacy 1-nodes block")
    # indent-2 comment lines directly above the next entry belong to that entry
    while end - 1 > start and lines[end - 1].startswith("  #"):
        end -= 1
    return start + 1, end


def splice(text, block_lines):
    lines = text.split("\n")
    if BEGIN in lines and END in lines:
        b, e = lines.index(BEGIN), lines.index(END)
        if e < b:
            raise SystemExit("END marker precedes BEGIN marker")
        new = lines[:b] + block_lines + lines[e + 1:]
    elif BEGIN in lines or END in lines:
        raise SystemExit("only one of the two markers present — fix config.yaml by hand")
    else:
        s, e = _legacy_span(lines)
        new = lines[:s] + block_lines + lines[e:]
    return "\n".join(new)


def block_of(text):
    lines = text.split("\n")
    if BEGIN in lines and END in lines:
        return lines[lines.index(BEGIN):lines.index(END) + 1]
    return []


# --------------------------------------------------------------------- validate
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def validate(config_text, gatus_bin=GATUS_BIN):
    """Start gatus on a temp copy (scratch port, memory storage); require /health 200
    within HEALTH_TIMEOUT_S; kill it. Returns (ok, message)."""
    if not os.access(gatus_bin, os.X_OK):
        return False, f"gatus binary not executable: {gatus_bin}"
    port = _free_port()
    text = re.sub(r"^web:\n(?:  .*\n)*", f"web:\n  port: {port}\n  address: 127.0.0.1\n", config_text, count=1, flags=re.M)
    text = re.sub(r"^storage:\n(?:  .*\n)*", "storage:\n  type: memory\n", text, count=1, flags=re.M)
    tmp = tempfile.mkdtemp(prefix="gatus-validate-")
    try:
        cfg = os.path.join(tmp, "config.yaml")
        with open(cfg, "w") as fh:
            fh.write(text)
        env = dict(os.environ, GATUS_CONFIG_PATH=cfg)
        log = open(os.path.join(tmp, "gatus.log"), "w+")
        p = subprocess.Popen([gatus_bin], env=env, cwd=tmp, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        ok, deadline, code = False, time.time() + HEALTH_TIMEOUT_S, None
        try:
            while time.time() < deadline:
                if p.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                        code = r.status
                except Exception:
                    code = None
                if code == 200:
                    ok = True
                    break
                time.sleep(0.25)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        log.seek(0)
        tail = log.read()[-600:].strip()
        log.close()
        if ok:
            return True, f"gatus /health 200 on scratch port {port} within {HEALTH_TIMEOUT_S}s"
        return False, (f"gatus did not answer /health 200 within {HEALTH_TIMEOUT_S}s "
                       f"(exit={p.returncode}); log tail:\n{tail}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------ live
def live_icmp_nodes(api=GATUS_API):
    with urllib.request.urlopen(f"{api}/api/v1/endpoints/statuses", timeout=5) as r:
        data = json.load(r)
    return sorted(e["name"] for e in data if e.get("group") == "1-nodes" and not e["name"].endswith("-sshauth"))


def wait_for_reload(expected, seconds, api=GATUS_API):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            last = live_icmp_nodes(api)
            if last == expected:
                return True, last
        except Exception as e:
            last = f"api error: {e}"
        time.sleep(3)
    return False, last


# ------------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--inventory", default=INVENTORY_ROOT)
    ap.add_argument("--gatus", default=GATUS_BIN)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="diff only; exit 1 if the block would change")
    g.add_argument("--print", action="store_true", help="print the generated block")
    g.add_argument("--write", action="store_true", help="validate, .bak, write in place")
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--wait", nargs="?", const=90, type=int, default=None,
                    help="after --write, wait up to N s (default 90) for Gatus to reload and match")
    a = ap.parse_args(argv)

    inv = _inventory_module(a.inventory)
    try:
        records = inv.load(strict=True, fallback=True, root=a.inventory)
    except inv.InventoryError as e:
        print(f"ERROR inventory unreadable and no last-good cache: {e}", file=sys.stderr)
        return 2
    rows = node_rows(records, inv)
    block = render_block(rows)
    if a.print:
        print("\n".join(block))
        return 0

    try:
        with open(a.config) as fh:
            old = fh.read()
    except OSError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    new = splice(old, block)
    if new == old:
        print(f"up to date: {len(rows)} node rows between markers in {a.config}")
        if a.write and a.wait is not None:
            ok, got = wait_for_reload([r[0] for r in rows], a.wait)
            print(f"live 1-nodes {'match' if ok else 'MISMATCH'}: {got}")
            return 0 if ok else 2
        return 0
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), a.config, a.config, lineterm=""))
    if a.check:
        print("\n".join(diff))
        print(f"\n{len(rows)} node rows would be written; {len(diff)} diff lines", file=sys.stderr)
        return 1

    if not a.no_validate:
        ok, msg = validate(new, a.gatus)
        print(("VALIDATE OK: " if ok else "VALIDATE FAIL: ") + msg, file=sys.stderr)
        if not ok:
            print("ABORT: config not written", file=sys.stderr)
            return 2
    bak = f"{a.config}.bak.{time.strftime('%Y%m%d_%H%M%SZ', time.gmtime())}"
    shutil.copy2(a.config, bak)
    with open(a.config, "w") as fh:  # in place: keep the inode (docker bind mount)
        fh.write(new)
    print(f"wrote {a.config} ({len(rows)} node rows: {', '.join(r[0] for r in rows)}); backup {bak}")
    if a.wait is not None:
        ok, got = wait_for_reload([r[0] for r in rows], a.wait)
        print(f"live 1-nodes {'match' if ok else 'MISMATCH'} after {a.wait}s window: {got}")
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
