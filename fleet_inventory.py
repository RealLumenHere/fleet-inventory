#!/usr/bin/env python3
"""Fleet inventory loader — the ONE reader of machines/<name>.yaml (wq#1453, DESIGN_SOT.md).

Consumers import this and never carry a node list of their own. A decommission is
one YAML edit. Only dependency beyond the stdlib is pyyaml.

API
  load(strict=True, fallback=False, root=None) -> dict name -> record
  all() / monitored() / is_monitored(name) / get(name) / ssh_target(name)
  by_role(role) / validate(records=None, root=None) -> [errors]
  select(**filters) / ssh_targets(**filters) / is_local(rec) / maint_tier(rec) / plug_map()
      (wq#1456: the ops scripts' NODES/HOSTS lists come from these — no script keeps names)
  observed(name) -> live state_facts overrides {tailscale_ip, lan_ip, ssh_user, plug_id} (wq#1631:
      ssh_target()/plug_map() read this first; {} keys are NO-FACT and fall back to declared YAML)

Root resolution: explicit root= arg, else $FLEET_INVENTORY_ROOT, else this file's directory.
$FLEET_INV_HOSTNAME overrides the local hostname used by is_local() (tests, cross-host runs).

MONITORED(name) := status.monitor is true AND status.status not in
                   {decommissioned, dead, in-storage}.
A node absent from inventory is NOT monitored (fail-closed on membership).

LAST-GOOD: every clean strict load writes <root>/.roster.last_good.json atomically.
load(fallback=True) returns that cache on any parse/validation error, prints
"ERROR <file>:<line> ..." to stderr and opens aq key inventory:parse-error
(edge-triggered: a marker file <root>/.roster.parse_error remembers the open edge;
the next clean load resolves the aq row and removes the marker — same shape as the
sentinel_probes.py edge-clear from wq#548).
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys
import tempfile

import yaml

INVENTORY_ROOT = os.path.dirname(os.path.abspath(__file__))
MACHINES_SUBDIR = "machines"
LAST_GOOD_NAME = ".roster.last_good.json"
PARSE_ERROR_MARKER = ".roster.parse_error"
AQ_DIR = "/home/lumen/agents/alert-queue"
AQ_SOURCE = "inventory"
AQ_KEY = "inventory:parse-error"

# wq#1631 (MINERVA_PIPELINE_MAP.md §4.2): state_facts owns tailscale_ip, lan_ip, ssh user,
# and the power plug binding. This module is the one place that reads it for those keys —
# consumers call observed(), never lumen_state.py directly. $LUMEN_STATE_PY overrides for tests.
LUMEN_STATE = os.environ.get("LUMEN_STATE_PY", "/home/lumen/minerva/lumen_state.py")
_FACT_RE = re.compile(r"^\s*([a-z0-9_-]+)\.([a-z0-9_]+)\s+=\s+(.*?)\s*(?:\(since [^)]*\))?\s*$", re.I)
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

STATUS_ENUM = ("active", "standby", "bench", "in-storage", "decommissioned", "dead")
UNMONITORED_STATUSES = frozenset({"decommissioned", "dead", "in-storage"})
POWER_ENUM = ("on", "off", "plug-cut")
TOP_LEVEL_KEYS = frozenset({"identity", "status", "network", "hardware", "software", "roles",
                            "services", "serves", "governance", "ops"})
MONITORING_DEFAULTS = {"interval_s": 60, "alert": True, "alert_desc": "", "gatus": True}

_CACHE = {"sig": None, "records": None}


class InventoryError(Exception):
    """Raised on parse/validation failure (strict) or when no last-good cache exists."""


# --------------------------------------------------------------------------- paths
def _root(root):
    return os.path.abspath(root or os.environ.get("FLEET_INVENTORY_ROOT") or INVENTORY_ROOT)


def _machines_dir(root):
    return os.path.join(_root(root), MACHINES_SUBDIR)


def _last_good_path(root):
    return os.path.join(_root(root), LAST_GOOD_NAME)


def _marker_path(root):
    return os.path.join(_root(root), PARSE_ERROR_MARKER)


def _files(root):
    return sorted(f for f in glob.glob(os.path.join(_machines_dir(root), "*.yaml"))
                  if not os.path.basename(f).startswith("_"))


# ----------------------------------------------------------------------- validate
def _err(file, line, msg):
    return f"{file}:{line}: {msg}"


def _validate_record(file, rec):
    """Per-file structural checks. Returns list of 'file:line: msg' strings."""
    errs = []
    if not isinstance(rec, dict):
        return [_err(file, 1, "top level must be a mapping")]
    unknown = set(rec) - TOP_LEVEL_KEYS - {"_file"}
    if unknown:
        errs.append(_err(file, 1, f"unknown top-level keys: {sorted(unknown)}"))
    ident = rec.get("identity")
    if not isinstance(ident, dict) or not ident.get("name"):
        errs.append(_err(file, 1, "identity.name is required"))
    else:
        stem = os.path.splitext(os.path.basename(file))[0]
        if str(ident["name"]) != stem:
            errs.append(_err(file, 1, f"identity.name {ident['name']!r} != filename {stem!r}"))
    st = rec.get("status")
    if not isinstance(st, dict):
        errs.append(_err(file, 1, "status block is required"))
    else:
        if st.get("status") not in STATUS_ENUM:
            errs.append(_err(file, 1, f"status.status {st.get('status')!r} not in {list(STATUS_ENUM)}"))
        if not isinstance(st.get("monitor"), bool):
            errs.append(_err(file, 1, f"status.monitor must be a bool, got {st.get('monitor')!r}"))
        if "power" in st and st["power"] not in POWER_ENUM and st["power"] != "VERIFY":
            errs.append(_err(file, 1, f"status.power {st['power']!r} not in {list(POWER_ENUM)}"))
        mon = st.get("monitoring")
        if mon is not None:
            if not isinstance(mon, dict):
                errs.append(_err(file, 1, "status.monitoring must be a mapping"))
            else:
                bad = set(mon) - set(MONITORING_DEFAULTS)
                if bad:
                    errs.append(_err(file, 1, f"status.monitoring unknown keys: {sorted(bad)}"))
                iv = mon.get("interval_s", MONITORING_DEFAULTS["interval_s"])
                if not isinstance(iv, int) or isinstance(iv, bool) or iv <= 0:
                    errs.append(_err(file, 1, f"status.monitoring.interval_s must be a positive int, got {iv!r}"))
                for k in ("alert", "gatus"):
                    if k in mon and not isinstance(mon[k], bool):
                        errs.append(_err(file, 1, f"status.monitoring.{k} must be a bool, got {mon[k]!r}"))
                if "alert_desc" in mon and not isinstance(mon["alert_desc"], str):
                    errs.append(_err(file, 1, "status.monitoring.alert_desc must be a string"))
        if st.get("status") == "decommissioned" and not isinstance(st.get("decommissioned"), dict):
            errs.append(_err(file, 1, "status.decommissioned {date, reason} required when status=decommissioned"))
    net = rec.get("network")
    if net is not None:
        if not isinstance(net, dict):
            errs.append(_err(file, 1, "network must be a mapping"))
        else:
            for k in ("tailscale_ip", "lan_ip"):
                v = net.get(k)
                if v is not None and not isinstance(v, str):
                    errs.append(_err(file, 1, f"network.{k} must be a string or null, got {v!r}"))
            ssh = net.get("ssh")
            if ssh is not None and not isinstance(ssh, dict):
                errs.append(_err(file, 1, "network.ssh must be a mapping {user, method[, key]}"))
            elif ssh and "key" in ssh and ssh["key"] is not None and not isinstance(ssh["key"], str):
                errs.append(_err(file, 1, f"network.ssh.key must be a path string, got {ssh['key']!r}"))
    for k in ("roles", "services", "serves"):
        if k in rec and rec[k] is not None and not isinstance(rec[k], list):
            errs.append(_err(file, 1, f"{k} must be a list"))
    ops = rec.get("ops")
    if isinstance(ops, dict) and "maint_tier" in ops:
        t = ops["maint_tier"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            errs.append(_err(file, 1, f"ops.maint_tier must be a positive number (A11 §5 tier), got {t!r}"))
    return errs


def validate(records=None, root=None):
    """Validate a roster. With no args, parses <root>/machines/*.yaml itself.
    Returns a list of error strings ('file:line: message'); empty means valid."""
    if records is None:
        try:
            records = _parse(root)
        except InventoryError as e:
            return [str(e)]
    errs = []
    seen = {}
    for name, rec in records.items():
        file = rec.get("_file", name)
        errs.extend(_validate_record(file, rec))
        if name in seen:
            errs.append(_err(file, 1, f"duplicate machine name {name!r} (also {seen[name]})"))
        seen[name] = file
    return errs


# --------------------------------------------------------------------------- load
def _parse(root):
    """Parse every machines/*.yaml. Raises InventoryError('file:line: msg') on YAML error."""
    records = {}
    for f in _files(root):
        try:
            with open(f) as fh:
                data = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            line = (mark.line + 1) if mark is not None else 1
            raise InventoryError(_err(f, line, f"YAML parse error: {getattr(e, 'problem', e)}"))
        except OSError as e:
            raise InventoryError(_err(f, 1, f"read error: {e}"))
        if data is None:
            data = {}
        if isinstance(data, dict):
            data["_file"] = f
            # YAML 1.1 (pyyaml) reads bare `on`/`off` as booleans; SCHEMA.md spells the
            # power enum exactly that way, so map them back before validation.
            st = data.get("status")
            if isinstance(st, dict) and isinstance(st.get("power"), bool):
                st["power"] = "on" if st["power"] else "off"
            name = str(((data.get("identity") or {}) if isinstance(data.get("identity"), dict) else {})
                       .get("name") or os.path.splitext(os.path.basename(f))[0])
        else:
            name = os.path.splitext(os.path.basename(f))[0]
        if name in records:
            raise InventoryError(_err(f, 1, f"duplicate machine name {name!r} (also {records[name].get('_file')})"))
        records[name] = data
    return records


def _apply_defaults(records):
    for rec in records.values():
        st = rec.setdefault("status", {})
        if isinstance(st, dict):
            mon = dict(MONITORING_DEFAULTS)
            mon.update(st.get("monitoring") or {})
            st["monitoring"] = mon
    return records


def _signature(root):
    sig = []
    for f in _files(root):
        try:
            s = os.stat(f)
            sig.append((f, s.st_mtime_ns, s.st_size))
        except OSError:
            sig.append((f, None, None))
    return tuple(sig)


def _write_last_good(root, records):
    path = _last_good_path(root)
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".roster.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"machines": records}, fh, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_last_good(root):
    path = _last_good_path(root)
    try:
        with open(path) as fh:
            return json.load(fh)["machines"]
    except (OSError, ValueError, KeyError):
        return None


def load(strict=True, fallback=False, root=None):
    """Load the roster: dict name -> record (monitoring defaults applied).

    strict=True  : validate() must pass; on success the last-good cache is refreshed.
    strict=False : parse only (no schema validation, no cache write) — for tooling.
    fallback=True: on parse/validation error return the last-good cache, print
                   ERROR file:line to stderr and open aq inventory:parse-error.
                   Raises InventoryError only if no cache exists either.
    """
    sig = _signature(root)
    if strict and _CACHE["sig"] == sig and _CACHE["records"] is not None and root is None:
        return _CACHE["records"]
    try:
        records = _parse(root)
        if strict:
            errs = validate(records, root=root)
            if errs:
                raise InventoryError("\n".join(errs))
    except InventoryError as e:
        if not fallback:
            raise
        first = str(e).splitlines()[0]
        print(f"ERROR {first}", file=sys.stderr)
        if len(str(e).splitlines()) > 1:
            print(f"ERROR (+{len(str(e).splitlines()) - 1} more inventory error(s)) — serving last-good roster",
                  file=sys.stderr)
        _alert_open(root, str(e))
        cached = _read_last_good(root)
        if cached is None:
            raise InventoryError(f"{first} (and no last-good cache at {_last_good_path(root)})")
        return cached
    records = _apply_defaults(records)
    if strict:
        _write_last_good(root, records)
        _alert_clear(root)
        if root is None:
            _CACHE["sig"], _CACHE["records"] = sig, records
    return records


# --------------------------------------------------------------- aq edge trigger
def _aq():
    """Import the alert-queue module; None if unavailable. Overridable in tests."""
    if os.environ.get("FLEET_INV_NO_AQ"):
        return None
    try:
        if AQ_DIR not in sys.path:
            sys.path.insert(0, AQ_DIR)
        import aq  # noqa: E402
        return aq
    except Exception:
        return None


def _alert_open(root, detail):
    """Edge-triggered: open aq inventory:parse-error once per error episode."""
    marker = _marker_path(root)
    if os.path.exists(marker):
        return
    try:
        with open(marker, "w") as fh:
            fh.write(detail.splitlines()[0] + "\n")
    except OSError:
        pass
    aq = _aq()
    if aq is None:
        return
    try:
        aq.ingest(source=AQ_SOURCE, key=AQ_KEY, title="fleet inventory parse error",
                  message=detail[:2000], priority="high")
    except Exception as e:  # never let alerting break a consumer
        print(f"aq open failed (non-fatal): {e}", file=sys.stderr)


def _alert_clear(root):
    """Resolve the open aq row on the first clean load after an error (edge-clear, wq#548 shape)."""
    marker = _marker_path(root)
    if not os.path.exists(marker):
        return
    try:
        os.unlink(marker)
    except OSError:
        pass
    aq = _aq()
    if aq is None:
        return
    try:
        c = aq.db()
        ts = aq.now()
        rows = c.execute("SELECT id FROM alerts WHERE source=? AND key=? AND status='open'",
                         (AQ_SOURCE, AQ_KEY)).fetchall()
        for (rid,) in rows:
            c.execute("UPDATE alerts SET status='resolved', note=?, resolved_ts=? WHERE id=? AND status='open'",
                      (f"auto-cleared: inventory loaded clean on {ts}", ts, rid))
        c.commit()
        c.close()
    except Exception as e:
        print(f"aq edge-clear failed (non-fatal): {e}", file=sys.stderr)


# ------------------------------------------------------------------ query API
def _roster(records, root):
    return records if records is not None else load(strict=True, fallback=True, root=root)


def all(records=None, root=None):  # noqa: A001 — API name fixed by DESIGN_SOT.md
    """Every machine record, sorted by name."""
    r = _roster(records, root)
    return [r[k] for k in sorted(r)]


def _is_monitored_rec(rec):
    st = rec.get("status") or {}
    return st.get("monitor") is True and st.get("status") not in UNMONITORED_STATUSES


def monitored(records=None, root=None):
    """Records for which MONITORED(name) holds (see module docstring)."""
    return [rec for rec in all(records, root) if _is_monitored_rec(rec)]


def is_monitored(name, records=None, root=None):
    """MONITORED(name). A name absent from inventory is NOT monitored."""
    rec = _roster(records, root).get(name)
    return bool(rec) and _is_monitored_rec(rec)


def get(name, records=None, root=None):
    """Record for `name`, or None if not in inventory."""
    return _roster(records, root).get(name)


def _clean(v):
    return None if v in (None, "", "VERIFY", "n/a") else str(v)


def _state_facts(name):
    """{predicate: value} for `name` from `lumen_state.py current`. Read-only; {} on any failure
    or when FLEET_INV_NO_STATE is set (tests, offline) — a state-store outage must never break
    an inventory read, only make it fall back to the declared YAML value."""
    if os.environ.get("FLEET_INV_NO_STATE"):
        return {}
    try:
        out = subprocess.run([sys.executable, LUMEN_STATE, "current", name],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    facts = {}
    for line in out.splitlines():
        m = _FACT_RE.match(line)
        if m and m.group(1).lower() == name.lower():
            facts[m.group(2).lower()] = m.group(3).strip()
    return facts


def observed(name, state_fn=None):
    """Live-observed values for `name`, sourced from state_facts (wq#1631 ownership split):
    tailscale_ip, lan_ip, ssh user, power plug. Only predicates confirmed to exist in the store
    today — no hardware.ram/storage predicate exists yet, so those stay YAML-declared until one
    is ingested. A key absent here is NO-FACT: callers fall back to the declared YAML value."""
    facts = (state_fn or _state_facts)(name)
    out = {}
    ts = facts.get("tailscale_ip") or facts.get("ts_ip")
    if ts:
        m = _IP_RE.search(ts)
        if m:
            out["tailscale_ip"] = m.group(1)
    lan = facts.get("lan_ip")
    if lan:
        m = _IP_RE.search(lan)
        if m:
            out["lan_ip"] = m.group(1)
    user = facts.get("ssh_user")
    if user:
        m = re.match(r"[A-Za-z][A-Za-z0-9-]*", user)
        if m:
            out["ssh_user"] = m.group(0)
    elif facts.get("ssh"):
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9-]*)@", facts["ssh"])
        if m:
            out["ssh_user"] = m.group(1)
    plug = facts.get("power_plug")
    if plug:
        m = re.match(r"[A-Za-z]+\d+", plug)
        if m:
            out["plug_id"] = m.group(0)
    return out


def ssh_target(name, records=None, root=None, state_fn=None):
    """'user@tailscale_ip' for name, resolved from the live store first (wq#1631: state_facts
    owns observed network.*), falling back to the declared YAML network block on NO-FACT.
    Raises InventoryError if unknown or unresolvable (user/ip VERIFY, or network.ssh.method
    n/a = 'do not reach', e.g. a decommissioned box)."""
    rec = get(name, records, root)
    if rec is None:
        raise InventoryError(f"{name}: not in inventory")
    net = rec.get("network") or {}
    ssh = net.get("ssh") or {}
    if str(ssh.get("method") or "").lower() in ("n/a", "none", "no"):
        raise InventoryError(f"{name}: ssh method {ssh.get('method')!r} — not reachable by design")
    live = observed(name, state_fn)
    user = live.get("ssh_user") or _clean(ssh.get("user"))
    ip = live.get("tailscale_ip") or _clean(net.get("tailscale_ip"))
    if not user or not ip:
        raise InventoryError(f"{name}: no ssh target (user={user or 'VERIFY'}, tailscale_ip={ip or 'VERIFY'})")
    return f"{user}@{ip}"


def ssh_key(rec, default=None):
    """network.ssh.key (per-node key file override, e.g. thoth) or `default`."""
    return _clean(((rec.get("network") or {}).get("ssh") or {}).get("key")) or default


def by_role(role, records=None, root=None):
    """Records whose roles list contains `role`."""
    return [rec for rec in all(records, root) if role in (rec.get("roles") or [])]


def names(records=None, root=None):
    return [rec["identity"]["name"] for rec in all(records, root)]


# ------------------------------------------------------- consumer selectors (wq#1456)
def is_local(rec, hostname=None):
    """True when `rec` is the machine this code runs on (identity.name or an alias == hostname)."""
    h = (hostname or os.environ.get("FLEET_INV_HOSTNAME") or socket.gethostname()).split(".")[0].lower()
    ident = rec.get("identity") or {}
    cands = [ident.get("name")] + list(ident.get("aliases") or [])
    return h in {str(c).lower() for c in cands if c}


def _flag(rec, key):
    return bool((rec.get("governance") or {}).get(key))


def maint_tier(rec):
    """ops.maint_tier — the node's place in the A11 §5 maintenance ordering law (low runs first).
    6.0 when unset: 'unlisted-but-reachable, sort after everything, flag for review'."""
    t = (rec.get("ops") or {}).get("maint_tier")
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return 6.0
    return float(t)


def select(records=None, root=None, *, monitored=True, statuses=None, maintenance=False,
           no_crown_jewel=False, exclude_self=False, hostname=None):
    """Roster subset for a consumer. Filters AND together:
      monitored      MONITORED(name) only (default). False = every status.
      statuses       iterable of status.status values to keep (e.g. ('active',)).
      maintenance    drop governance.never_restart nodes (Thor) — for tooling that updates/reboots.
      no_crown_jewel drop governance.crown_jewel nodes (Ma'at, Lumen) — for blast-radius tooling.
      exclude_self   drop the machine this runs on (see is_local)."""
    out = []
    for rec in all(records, root):
        st = rec.get("status") or {}
        if monitored and not _is_monitored_rec(rec):
            continue
        if statuses is not None and st.get("status") not in set(statuses):
            continue
        if maintenance and _flag(rec, "never_restart"):
            continue
        if no_crown_jewel and _flag(rec, "crown_jewel"):
            continue
        if exclude_self and is_local(rec, hostname):
            continue
        out.append(rec)
    return out


def ssh_targets(records=None, root=None, *, local=None, skipped=None, order="name", hostname=None, **filters):
    """[(name, 'user@tailscale_ip')] for select(**filters) records that resolve.
    Unresolvable ones (VERIFY user/ip, method n/a) are dropped; appended to `skipped` as
    (name, reason) when a list is given. local=TEXT substitutes TEXT for this host's target
    (fleet-update-check uses '' = run locally, rotate-fleet-pw uses 'LOCAL').
    order='maint' sorts by (ops.maint_tier, name) — the A11 §5 ordering law."""
    r = _roster(records, root)
    out = []
    for rec in select(r, hostname=hostname, **filters):
        name = rec["identity"]["name"]
        if local is not None and is_local(rec, hostname):
            out.append((name, local))
            continue
        try:
            out.append((name, ssh_target(name, r)))
        except InventoryError as e:
            if skipped is not None:
                skipped.append((name, str(e)))
    if order == "maint":
        out.sort(key=lambda t: (maint_tier(r[t[0]]), t[0]))
    elif order == "name":
        out.sort(key=lambda t: t[0])
    return out


MAINTENANCE_FILTERS = dict(monitored=True, statuses=("active",), maintenance=True)


def maintenance_targets(records=None, root=None, *, local="", skipped=None, hostname=None):
    """The maintenance-window roster shared by fleet-update-check / fleet-maint-assemble /
    fleet-maint-run: MONITORED, status active, not governance.never_restart (Thor), in A11 §5
    order (ops.maint_tier, name). Standby/bench boxes are never maintenance targets — they are
    'declared absent' (see fleet-maint-assemble). local='' = run on this host without ssh."""
    return ssh_targets(records, root, local=local, skipped=skipped, order="maint", hostname=hostname,
                       **MAINTENANCE_FILTERS)


def plug_map(records=None, root=None, state_fn=None):
    """{plug_id: name} — live power_plug fact from state_facts first (wq#1631), falling back to
    the declared hardware.power_hw.plug_id on NO-FACT (e.g. 'P3' -> 'hathor'). Ids VERIFY/none
    skipped. Two machines claiming one plug is a data error: InventoryError."""
    out = {}
    for rec in all(records, root):
        name = rec["identity"]["name"]
        live = observed(name, state_fn)
        pid = live.get("plug_id") or _clean(((rec.get("hardware") or {}).get("power_hw") or {}).get("plug_id"))
        if not pid or pid.lower() == "none":
            continue
        if pid in out:
            raise InventoryError(f"plug {pid} claimed by both {out[pid]} and {name}")
        out[pid] = name
    return out
