import asyncio
import atexit
import csv
import ctypes
import ctypes.util
import http.server
import json
import os
import re
import shlex
import socket
import socketserver
import subprocess
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import iterm2

PORT = 9999
SAMPLE_INTERVAL = 2.0
TOP_N = 8
HOME = os.path.expanduser("~")

# Initial content of watched_folders.json (next to this script); the list is edited in the UI
DEFAULT_WATCHED = [
    "/private/tmp", "$TMPDIR", "/private/var/folders", "/private/var/log", "/Library/Logs", "/Library/Caches",
    "~/Library/Logs", "~/Library/Caches", "~/.cache", "~/.npm/_cacache", "~/.yarn/berry/cache",
    "~/Library/Caches/Yarn", "~/Library/pnpm/store", "~/.local/share/pnpm/store", "~/.cargo/registry/cache",
    "~/.cargo/registry/src", "~/.gradle/caches", "~/.m2/repository", "~/Library/Caches/Homebrew",
    "~/Library/Caches/CocoaPods", "~/Library/Caches/org.swift.swiftpm", "~/Library/Developer/Xcode/DerivedData",
    "~/Library/Developer/CoreSimulator/Caches",
]
WATCHED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watched_folders.json")
FOLDER_SCAN_INTERVAL = 300.0
FOLDER_WARN_BYTES = 5 * 1024**3

FILEOPS_ROOTS = ["/"]
FILEOPS_IGNORE = (
    "/.Spotlight-V100", "/.fseventsd", "/private/var/db/", "/System/Volumes/Data/.Spotlight-V100", "/dev/",
    f"{HOME}/Library/Biome/", f"{HOME}/Library/DuetExpertCenter/",
)
FILEOPS_WINDOW = 300
FILEOPS_MAX = 30000
FILEOPS_SHOW = 500
# Needs a sudoers entry for exactly this command, otherwise falls back to FSEvents (no process names)
FS_USAGE_CMD = ["/usr/bin/sudo", "-n", "/usr/bin/fs_usage", "-w", "-f", "filesys"]

NET_RANGES = (60, 300, 900, 1800, 3600, 43200, 86400)

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

_C_ENV = {**os.environ, "LC_ALL": "C"}
_libsys = ctypes.CDLL("/usr/lib/libSystem.B.dylib")

STATE = {}
STATE_LOCK = threading.Lock()
FILEOPS = deque(maxlen=FILEOPS_MAX)
FILEOPS_LOCK = threading.Lock()

# Per-process traffic buckets: 10 s resolution for the last hour, 1 min for 24 h
NET_LOCK = threading.Lock()
NET_FINE = deque(maxlen=361)
NET_COARSE = deque(maxlen=1441)
NET_SINCE = time.time()
NET_PID = {}

# Per-connection byte counters from the previous /api/conns call, for rates
CONN_LOCK = threading.Lock()
CONN_PREV = {}
DNS_CACHE = {}
DNS_POOL = ThreadPoolExecutor(max_workers=4)

# Per-process CPU seconds / peak RSS: 10 s buckets for 1 h, 5 min buckets for 24 h
PROC_LOCK = threading.Lock()
PROC_FINE = deque(maxlen=361)
PROC_COARSE = deque(maxlen=289)
PROC_NOW = {}
PROC_SINCE = time.time()
PROC_RANGES = (0,) + NET_RANGES
APPS = {}
ME = os.environ.get("USER") or os.path.basename(HOME)

FOLDERS_LOCK = threading.Lock()
RESCAN = threading.Event()
ITERM: dict = {"conn": None, "loop": None}

def _sh(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=_C_ENV).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _sysctl(name):
    return _sh(["sysctl", "-n", name]).strip()


# --- CPU (per core, via Mach host_processor_info) ---------------------------

_libsys.mach_host_self.restype = ctypes.c_uint
_libsys.host_processor_info.argtypes = [
    ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)), ctypes.POINTER(ctypes.c_uint),
]
_libsys.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
_HOST = _libsys.mach_host_self()
_TASK_SELF = ctypes.c_uint.in_dll(_libsys, "mach_task_self_").value


def cpu_ticks():
    """Per core: (user, system, idle, nice) ticks since boot."""
    ncpu = ctypes.c_uint()
    info = ctypes.POINTER(ctypes.c_uint)()
    ninfo = ctypes.c_uint()
    if _libsys.host_processor_info(_HOST, 2, ctypes.byref(ncpu), ctypes.byref(info), ctypes.byref(ninfo)) != 0:
        raise OSError("host_processor_info failed")
    try:
        return [tuple(info[i * 4 + j] for j in range(4)) for i in range(ncpu.value)]
    finally:
        _libsys.vm_deallocate(_TASK_SELF, ctypes.cast(info, ctypes.c_void_p).value, ninfo.value * 4)


def core_types(n):
    # Apple Silicon numbers efficiency cores first (cpu0..), performance cores after
    e_count = 0
    if _sysctl("hw.perflevel1.name") == "Efficiency":
        e_count = int(_sysctl("hw.perflevel1.logicalcpu") or 0)
    if not _sysctl("hw.perflevel0.name"):
        return ["C"] * n
    return ["E" if i < e_count else "P" for i in range(n)]


def read_gpu():
    out = _sh(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])

    def num(key):
        m = re.search(r'"%s"\s*=\s*(\d+)' % re.escape(key), out)
        return int(m.group(1)) if m else None

    return {
        "util": num("Device Utilization %"),
        "renderer": num("Renderer Utilization %"),
        "tiler": num("Tiler Utilization %"),
        "cores": num("gpu-core-count"),
        "mem": num("In use system memory"),
    }


# --- Memory ------------------------------------------------------------------

def read_memory():
    out = _sh(["vm_stat"])
    m = re.search(r"page size of (\d+) bytes", out)
    page = int(m.group(1)) if m else int(_sysctl("hw.pagesize") or 4096)
    pages = {}
    for line in out.splitlines()[1:]:
        key, _, val = line.partition(":")
        val = val.strip().rstrip(".")
        if val.isdigit():
            pages[key.strip().strip('"')] = int(val) * page

    # Same definition as Activity Monitor: App + Wired + Compressed
    app = max(pages.get("Anonymous pages", 0) - pages.get("Pages purgeable", 0), 0)
    wired = pages.get("Pages wired down", 0)
    compressed = pages.get("Pages occupied by compressor", 0)
    swap = re.search(r"used = ([\d.]+)M", _sysctl("vm.swapusage"))
    return {
        "total": int(_sysctl("hw.memsize") or 0),
        "used": app + wired + compressed,
        "app": app,
        "wired": wired,
        "compressed": compressed,
        "cached": pages.get("File-backed pages", 0) + pages.get("Pages purgeable", 0),
        "free": pages.get("Pages free", 0),
        "swap": int(float(swap.group(1)) * 1024**2) if swap else 0,
    }


# --- Processes ---------------------------------------------------------------

def _cputime(s):
    days, _, s = s.rpartition("-")
    secs, mult = 0.0, 1
    for part in reversed(s.split(":")):
        secs += float(part) * mult
        mult *= 60
    return secs + (int(days) * 86400 if days else 0)


def read_ps():
    procs = {}
    for line in _sh(["ps", "-Aceo", "pid=,ppid=,user=,rss=,time=,comm="]).splitlines():
        parts = line.split(None, 5)
        if len(parts) != 6:
            continue
        try:
            pid = int(parts[0])
            procs[pid] = {"pid": pid, "ppid": int(parts[1]), "user": parts[2], "mem": int(parts[3]) * 1024,
                          "cputime": _cputime(parts[4]), "name": parts[5]}
        except ValueError:
            pass
    return procs


def _proc_add(buckets, size, ts, entries):
    start = ts - ts % size
    if not buckets or buckets[-1][0] != start:
        buckets.append((start, {}))
    bucket = buckets[-1][1]
    for p in entries:
        acc = bucket.get(p["pid"])
        if acc is None:
            bucket[p["pid"]] = [p["d"], p["mem"], p["name"]]
        else:
            acc[0] += p["d"]
            acc[1] = max(acc[1], p["mem"])


def proc_record(ts, procs):
    global PROC_NOW
    # only keep active or memory-heavy processes in history to bound memory use
    top_mem = sorted(procs.values(), key=lambda p: p["mem"], reverse=True)[:60]
    entries = {p["pid"]: p for p in top_mem}
    entries.update({pid: p for pid, p in procs.items() if p["d"] > 0})
    with PROC_LOCK:
        _proc_add(PROC_FINE, 10, ts, entries.values())
        _proc_add(PROC_COARSE, 300, ts, entries.values())
        PROC_NOW = procs


def _proc_hist(rng, only_pid=None):
    buckets, size = (PROC_FINE, 10) if rng <= 3600 else (PROC_COARSE, 300)
    now = time.time()
    cutoff = now - rng
    agg = {}
    with PROC_LOCK:
        for start, bucket in buckets:
            if start + size <= cutoff:
                continue
            if only_pid is not None:
                items = [(only_pid, bucket[only_pid])] if only_pid in bucket else []
            else:
                items = bucket.items()
            for pid, (d, mem, name) in items:
                acc = agg.get(pid)
                if acc is None:
                    agg[pid] = [d, mem, name]
                else:
                    acc[0] += d
                    acc[1] = max(acc[1], mem)
    return agg, max(min(rng, now - PROC_SINCE), 1.0)


def focus_set(q):
    """PIDs and names of the focused process(es) plus all their descendants, or None without focus."""
    q = q.strip().lower()
    if not q:
        return None
    with PROC_LOCK:
        procs = PROC_NOW
    if q.isdigit():
        roots = [int(q)] if int(q) in procs else []
    else:
        roots = [pid for pid, p in procs.items() if q in p["name"].lower()]
    children = {}
    for pid, p in procs.items():
        children.setdefault(p["ppid"], []).append(pid)
    pids, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid not in pids:
            pids.add(pid)
            stack.extend(children.get(pid, ()))
    return {"pids": pids, "names": {procs[pid]["name"].lower() for pid in pids}}


def _focus_name(fs, name):
    return fs is None or any(_proc_name_match(name, n) for n in fs["names"])


def read_apps():
    """Dock apps (lsappinfo type Foreground): {pid: localized name}."""
    apps, name = {}, None
    for line in _sh(["lsappinfo", "list"]).splitlines():
        m = re.match(r'\s*\d+\)\s+"(.*)"\s+ASN:', line)
        if m:
            name = m.group(1)
            continue
        m = re.search(r'\bpid = (\d+)\b.*?type="(\w+)"', line)
        if m and name and m.group(2) == "Foreground":
            apps[int(m.group(1))] = name
    return apps


def _app_owners(now):
    """Maps every pid in an app's process tree to the app's root pid."""
    roots = {pid for pid in APPS if pid in now}
    children = {}
    for pid, p in now.items():
        children.setdefault(p["ppid"], []).append(pid)
    owner = {r: r for r in roots}
    for root in roots:
        stack = list(children.get(root, ()))
        while stack:
            pid = stack.pop()
            if pid in owner:
                continue
            owner[pid] = root
            stack.extend(children.get(pid, ()))
    return owner


def proc_summary(sort, desc, rng, q, fs=None, cat="all", group=False):
    with PROC_LOCK:
        now = PROC_NOW
    rows = {pid: {"pid": pid, "name": p["name"], "user": p["user"], "cpu": p.get("cpu", 0.0),
                  "mem": p["mem"], "alive": True} for pid, p in now.items()}
    if rng:
        agg, window = _proc_hist(rng)
        for r in rows.values():
            r["cpu"] = 0.0
        for pid, (d, mem, name) in agg.items():
            r = rows.get(pid)
            if r is None:
                r = rows[pid] = {"pid": pid, "name": name, "user": "", "cpu": 0.0, "mem": 0, "alive": False}
            r["cpu"] = round(100.0 * d / window, 1)
            r["mem"] = max(r["mem"], mem)
    owner = _app_owners(now)
    app_rows = {}
    for r in rows.values():
        root = owner.get(r["pid"])
        r["app"] = APPS[root] if root else ""
        r["isapp"] = r["pid"] == root
        if root:
            a = app_rows.setdefault(root, {"pid": root, "name": APPS[root], "user": now[root]["user"], "cpu": 0.0,
                                           "mem": 0, "alive": True, "procs": 0})
            a["cpu"] = round(a["cpu"] + r["cpu"], 1)
            a["mem"] += r["mem"]
            a["procs"] += 1
    q = q.strip().lower()
    key = (lambda r: r["name"].lower()) if sort == "name" else (lambda r: r[sort])
    out = [r for r in rows.values()
           if (fs is None or r["pid"] in fs["pids"])
           and (not q or q in r["name"].lower() or q == str(r["pid"]) or q == r["user"].lower())]
    if cat == "user":
        out = [r for r in out if r["user"] in (ME, "")]
    elif cat == "system":
        out = [r for r in out if r["user"] not in (ME, "")]
    # in grouped view, app processes are already counted inside their app row
    if group or cat == "app":
        out = [r for r in out if not r["app"]]
    if cat == "app":
        out = []
    apps = []
    if cat in ("all", "app") and (group or cat == "app"):
        apps = [a for a in app_rows.values()
                if (fs is None or a["pid"] in fs["pids"])
                and (not q or q in a["name"].lower() or q == str(a["pid"]))]
        apps.sort(key=key, reverse=desc)
    out.sort(key=key, reverse=desc)
    return {"range": rng, "since": PROC_SINCE, "total": len(out) + len(apps), "rows": out[:80], "apps": apps, "me": ME}


def _proc_name_match(a, b):
    # fs_usage/ps may truncate long names, so accept a prefix match for long names
    a, b = a.lower(), b.lower()
    return a == b or (len(a) >= 15 and b.startswith(a)) or (len(b) >= 15 and a.startswith(b))


def proc_detail(pid, rng):
    rng = rng or 300
    with PROC_LOCK:
        p = PROC_NOW.get(pid)
        children = sum(1 for c in PROC_NOW.values() if c["ppid"] == pid)
    agg, window = _proc_hist(rng, pid)
    d, peak, hist_name = agg.get(pid, (0.0, 0, None))
    name = p["name"] if p else hist_name
    if name is None:
        return {"pid": pid, "error": "Process is no longer available"}
    info = {
        "pid": pid, "name": name, "alive": p is not None, "range": rng, "children": children,
        "user": p["user"] if p else "", "ppid": p["ppid"] if p else None,
        "cpu_now": p.get("cpu", 0.0) if p else 0.0, "cpu_avg": round(100.0 * d / window, 1),
        "mem": p["mem"] if p else 0, "mem_peak": max(peak, p["mem"] if p else 0),
        "net": NET_PID.get(pid, (0, 0)), "command": "", "started": "",
    }
    if p:
        info["command"] = _sh(["ps", "-o", "command=", "-p", str(pid)]).strip()
        info["started"] = _sh(["ps", "-o", "lstart=", "-p", str(pid)]).strip()
        info["ports"] = [f"{r['proto']}/{r['port']}" for r in read_ports(True) if r["pid"] == pid]
    cutoff = time.time() - FILEOPS_WINDOW
    with FILEOPS_LOCK:
        ops = [o for o in reversed(FILEOPS) if o["t"] >= cutoff and o["proc"] and _proc_name_match(o["proc"], name)]
    info["fileops_count"] = sum(o["n"] for o in ops)
    info["fileops"] = [dict(o) for o in ops[:15]]
    with STATE_LOCK:
        if STATE.get("fileops_source") != "fs_usage":
            info["fileops_hint"] = STATE.get("fileops_hint") or "fs_usage is not active"
    return info


# --- Network -----------------------------------------------------------------

def default_iface():
    m = re.search(r"interface:\s*(\S+)", _sh(["route", "-n", "get", "default"]))
    return m.group(1) if m else "en0"


def network_ifaces():
    try:
        return [name for _, name in socket.if_nameindex()]
    except OSError:
        return []


def read_iface_bytes(iface):
    for line in _sh(["netstat", "-ibn", "-I", iface]).splitlines()[1:]:
        cols = line.split()
        # Address column is missing for some interfaces (utun), so index from the end
        if len(cols) >= 10 and cols[2].startswith("<Link#"):
            return int(cols[-5]), int(cols[-2])
    return 0, 0


NETTOP = {"seq": 0, "data": {}, "t": 0.0}
NETTOP_LOCK = threading.Lock()


def nettop_stream():
    # nettop needs 20-30 s for its first output after a cold start; one-shot calls with a timeout
    # kept killing it before it answered, so a single long-lived process feeds the samples instead
    while True:
        try:
            proc = subprocess.Popen(["nettop", "-P", "-L", "0", "-s", "2", "-J", "bytes_in,bytes_out", "-x"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    text=True, errors="replace", env=_C_ENV)
        except OSError:
            time.sleep(30)
            continue
        atexit.register(proc.terminate)
        block = {}
        for line in proc.stdout:
            if line.startswith(",bytes_in"):
                if block:
                    with NETTOP_LOCK:
                        NETTOP.update(seq=NETTOP["seq"] + 1, data=block, t=time.monotonic())
                block = {}
                continue
            cols = line.rstrip("\n").rsplit(",", 3)
            if len(cols) >= 3 and cols[1].isdigit() and cols[2].isdigit():
                block[cols[0]] = (int(cols[1]), int(cols[2]))
        proc.wait()
        time.sleep(5)


def nettop_snapshot():
    with NETTOP_LOCK:
        return NETTOP["seq"], NETTOP["data"], NETTOP["t"]


_NETTOP_FLOW = re.compile(r"^(tcp|udp)(4|6) (\S+)<->(\S+)$")
# Payloads are mostly TLS-encrypted, so the application protocol is only a guess from the port
SERVICES = {
    ("tcp", 80): "HTTP", ("tcp", 443): "HTTPS", ("udp", 443): "QUIC", ("tcp", 8080): "HTTP",
    ("tcp", 8443): "HTTPS", ("udp", 53): "DNS", ("tcp", 53): "DNS", ("tcp", 853): "DoT",
    ("udp", 5353): "mDNS", ("udp", 123): "NTP", ("tcp", 5223): "APNs", ("tcp", 22): "SSH",
    ("tcp", 993): "IMAPS", ("tcp", 465): "SMTPS", ("tcp", 587): "SMTP", ("tcp", 25): "SMTP",
    ("udp", 3478): "STUN", ("udp", 3479): "STUN", ("udp", 3480): "STUN", ("udp", 3481): "STUN",
    ("udp", 1900): "SSDP", ("tcp", 5432): "Postgres", ("tcp", 3306): "MySQL", ("tcp", 6379): "Redis",
    ("tcp", 27017): "MongoDB", ("tcp", 7000): "AirPlay", ("tcp", 5000): "AirPlay",
}


def _split_addr(s, v6):
    host, _, port = s.rpartition("." if v6 else ":")
    return host, int(port) if port.isdigit() else 0


def _rdns(ip):
    if ip in DNS_CACHE:
        return DNS_CACHE[ip]
    if len(DNS_CACHE) > 5000:
        DNS_CACHE.clear()
    DNS_CACHE[ip] = None

    def job():
        try:
            DNS_CACHE[ip] = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):
            DNS_CACHE[ip] = ""
    # resolved in the background; the next refresh shows the hostname
    DNS_POOL.submit(job)
    return None


def read_conns(name, fs=None):
    out = _sh(["nettop", "-L", "1", "-n", "-x", "-J", "state,bytes_in,bytes_out"])
    now = time.monotonic()
    conns, pid, hidden = [], None, 0
    with CONN_LOCK:
        for line in out.splitlines()[1:]:
            cols = line.rsplit(",", 4)
            if len(cols) < 5:
                continue
            ident, state, b_in, b_out = cols[:4]
            m = _NETTOP_FLOW.match(ident)
            if not m:
                pname, _, p = ident.rpartition(".")
                pid = int(p) if pname == name and p.isdigit() else None
                continue
            if pid is None or (fs is not None and pid not in fs["pids"]):
                continue
            proto, ver, local, remote = m.groups()
            rhost, rport = _split_addr(remote, ver == "6")
            if state == "Listen" or rhost == "*":
                hidden += 1
                continue
            lhost, lport = _split_addr(local, ver == "6")
            rx, tx = int(b_in or 0), int(b_out or 0)
            key = (pid, ident)
            prev = CONN_PREV.get(key)
            rate = None
            if prev and now - prev[2] >= 1 and rx >= prev[0] and tx >= prev[1]:
                dt = now - prev[2]
                rate = [(rx - prev[0]) / dt, (tx - prev[1]) / dt]
            CONN_PREV[key] = (rx, tx, now)
            conns.append({
                "pid": pid, "proto": proto + ver, "state": state,
                "service": SERVICES.get((proto, rport)) or SERVICES.get((proto, lport)) or "",
                "local": lhost, "lport": lport, "remote": rhost, "rport": rport,
                "host": _rdns(rhost), "rx": rx, "tx": tx, "rate": rate,
            })
        for k in [k for k, v in CONN_PREV.items() if now - v[2] > 600]:
            del CONN_PREV[k]
    conns.sort(key=lambda c: c["rx"] + c["tx"], reverse=True)
    return {"name": name, "total": len(conns), "hidden": hidden, "conns": conns[:150]}


# netstat -anv names the owning process for every socket, root-owned ones too (lsof would need sudo)
_NETSTAT_SOCK = re.compile(
    r"^(tcp|udp)\d*\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s+(?:([A-Z_]+)\s+)?\d+\s+\d+\s+\d+\s+\d+\s+(.+?):(\d+)\s+[0-9a-f]{5}\s")


def read_ports(udp):
    out = _sh(["netstat", "-anv", "-p", "tcp"]) + (_sh(["netstat", "-anv", "-p", "udp"]) if udp else "")
    ports = {}
    for line in out.splitlines():
        m = _NETSTAT_SOCK.match(line)
        if not m:
            continue
        proto, local, foreign, state, name, pid = m.groups()
        if (proto == "tcp" and state != "LISTEN") or (proto == "udp" and foreign != "*.*"):
            continue
        addr, _, port = local.rpartition(".")
        if not port.isdigit():
            continue
        e = ports.setdefault((proto, int(port), int(pid)),
                             {"proto": proto, "port": int(port), "pid": int(pid), "name": name.strip(), "addrs": set()})
        e["addrs"].add(addr)
    rows = list(ports.values())
    for e in rows:
        e["addrs"] = sorted(e["addrs"])
        e["local"] = all(a.startswith("127.") or a == "::1" for a in e["addrs"])
    rows.sort(key=lambda r: (r["port"], r["proto"]))
    return rows


# --- Watched folders ---------------------------------------------------------

def _expand(entry):
    return os.path.normpath(os.path.expandvars(os.path.expanduser(entry)))


def _save_watched(entries):
    tmp = WATCHED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, WATCHED_FILE)


def load_watched():
    with FOLDERS_LOCK:
        try:
            with open(WATCHED_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            _save_watched(DEFAULT_WATCHED)
            return list(DEFAULT_WATCHED)
        except (OSError, ValueError):
            # broken file: fall back to defaults without overwriting the user's edits
            return list(DEFAULT_WATCHED)
    if isinstance(data, list) and all(isinstance(e, str) for e in data):
        return data
    return list(DEFAULT_WATCHED)


def edit_watched(action, entry):
    entries = load_watched()
    if action == "add":
        path = _expand(entry)
        if not os.path.isabs(path) or not os.path.isdir(path):
            return {"error": "Not a folder: " + path}
        if any(_expand(e) == path for e in entries):
            return {"error": "Already in the list: " + path}
        entries.append(path.replace(HOME, "~", 1) if path.startswith(HOME + "/") else path)
    elif action == "remove":
        if entry not in entries:
            return {"error": "Not in the list: " + entry}
        entries.remove(entry)
    with FOLDERS_LOCK:
        _save_watched(entries)
    RESCAN.set()
    return {"entries": entries}


def _folders_total(folders):
    sized = [(i, os.path.realpath(f["path"]), f["size"]) for i, f in enumerate(folders) if f["size"] is not None]
    total = 0
    for i, real, size in sized:
        # nested or duplicate entries are already contained in another folder's size
        if not any((o == real and j < i) or real.startswith(o.rstrip("/") + "/") for j, o, _ in sized if j != i):
            total += size
    return total


def _publish_folders(results, **extra):
    with STATE_LOCK:
        STATE["folders"] = [dict(r) for r in results]
        STATE["folders_total"] = _folders_total(results)
        STATE.update(extra)


def folder_scanner():
    while True:
        RESCAN.clear()
        with STATE_LOCK:
            prev = {f["entry"]: f for f in STATE.get("folders", [])}
        results = [{"entry": e, "path": _expand(e), "display": e, "missing": False,
                    "size": prev.get(e, {}).get("size")} for e in load_watched()]
        _publish_folders(results, folders_scanning=True)
        for r in results:
            if RESCAN.is_set():
                break
            if not os.path.isdir(r["path"]):
                r["size"], r["missing"] = None, True
            else:
                out = _sh(["nice", "-n", "10", "du", "-sk", r["path"]], timeout=300)
                try:
                    r["size"] = int(out.split()[0]) * 1024
                except (IndexError, ValueError):
                    r["size"] = None
            _publish_folders(results)
        _publish_folders(results, folders_scanning=False, folders_at=time.time())
        RESCAN.wait(FOLDER_SCAN_INTERVAL)


# --- iTerm2 integration & export ---------------------------------------------

async def _iterm_cd(path):
    app = await iterm2.async_get_app(ITERM["conn"])
    window = app.current_terminal_window
    session = window.current_tab.current_session if window and window.current_tab else None
    if session is None:
        return False
    await session.async_send_text(f"cd {shlex.quote(path)}\n")
    await session.async_activate()
    return True


def iterm_cd(path):
    if ITERM["loop"] is None:
        return False
    try:
        return asyncio.run_coroutine_threadsafe(_iterm_cd(path), ITERM["loop"]).result(timeout=5)
    except Exception:
        return False


def _csv_cell(value):
    s = str(value)
    # paths/process names are untrusted: neutralise spreadsheet formulas
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_csv(section, header, rows):
    if not re.fullmatch(r"[a-z]{1,20}", str(section)) or not isinstance(header, list) or not isinstance(rows, list):
        raise ValueError("invalid export")
    path = os.path.join(HOME, "Downloads", f"sysmon-{section}-{time.strftime('%Y%m%d-%H%M%S')}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([_csv_cell(h) for h in header])
        for r in rows[:100000]:
            w.writerow([_csv_cell(c) for c in (r if isinstance(r, list) else [r])])
    return path


# --- File operations (FSEvents, no root needed; process info would need fs_usage + sudo) ---

_FS_OPS = (
    (0x200, "deleted"),
    (0x100, "created"),
    (0x800, "renamed"),
    (0x1000, "modified"),
    (0x400 | 0x2000 | 0x4000 | 0x8000, "attr"),
)
_FSEventCallback = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64),
)


def _on_fs_events(_stream, _info, num, paths, flags, _ids):
    with STATE_LOCK:
        if STATE.get("fileops_source") == "fs_usage":
            return
    now = time.time()
    for i in range(num):
        op = next((name for mask, name in _FS_OPS if flags[i] & mask), None)
        if not op:
            continue
        _add_fileop(now, op, paths[i].decode("utf-8", "replace"), "", bool(flags[i] & 0x20000))


FILEOP_TYPES = ("created", "modified", "write", "renamed", "deleted", "attr", "read")
DEFAULT_OPS = FILEOP_TYPES[:-1]
_fileop_recent = {}


def _add_fileop(t, op, path, proc, is_dir=False):
    if path.startswith(FILEOPS_IGNORE):
        return
    key = (proc, op, path)
    with FILEOPS_LOCK:
        ev = _fileop_recent.get(key)
        # collapse bursts (e.g. sqlite WAL writes) into one row with a counter
        if ev is not None and t - ev["t"] < 2:
            ev["n"] += 1
            ev["t"] = t
            return
        if len(_fileop_recent) > 20000:
            _fileop_recent.clear()
        ev = {"t": t, "op": op, "path": path, "proc": proc, "dir": is_dir, "n": 1}
        _fileop_recent[key] = ev
        FILEOPS.append(ev)


def _fileop_match(o, q):
    if q.startswith("p:"):
        return q[2:].strip() in o["proc"].lower()
    return q in o["path"].lower() or q in o["proc"].lower() or q in o["op"]


def query_fileops(q, ops, fs=None):
    cutoff = time.time() - FILEOPS_WINDOW
    with FILEOPS_LOCK:
        items = [o for o in reversed(FILEOPS) if o["t"] >= cutoff]
    if fs is not None:
        items = [o for o in items if o["proc"] and _focus_name(fs, o["proc"])]
    total = len(items)
    q = q.strip().lower()
    if q:
        items = [o for o in items if _fileop_match(o, q)]
    op_counts = Counter(o["op"] for o in items)
    matches = [o for o in items if o["op"] in ops]
    with STATE_LOCK:
        source, hint = STATE.get("fileops_source"), STATE.get("fileops_hint")
    return {
        "source": source, "hint": hint, "total": total, "matches": len(matches), "op_counts": op_counts,
        "ops": [dict(o) for o in matches[:FILEOPS_SHOW]],
    }


# fs_usage -w line: time, call, details [path], elapsed [W], process.thread
_FSU_LINE = re.compile(r"^\d\d:\d\d:\d\d\.\d+\s+(\S+)\s+(.*)\s(\d+\.\d+)(?:\s+W)?\s+(\S.*?)\s*$")
_FSU_PATH = re.compile(r"(?:^|\s)(/\S.*?)\s*$")
_FSU_FLAGS = re.compile(r"\(([A-Z_]+)\)")
_FSU_ERRNO = re.compile(r"\[\s*\d+\]")


def _fsu_op(call, details):
    c = call.split("[")[0].lower()
    if c.startswith(("open", "creat")):
        flags = _FSU_FLAGS.search(details)
        return "write" if flags and "W" in flags.group(1) else "read"
    if c.startswith(("unlink", "rmdir")):
        return "deleted"
    if c.startswith(("rename", "exchangedata")):
        return "renamed"
    if c.startswith(("mkdir", "link", "symlink", "clonefile")):
        return "created"
    if c.startswith(("write", "pwrite", "wrdata", "wrmeta", "truncate", "ftruncate")):
        return "write"
    if c.startswith(("chmod", "fchmod", "chown", "fchown", "utimes", "futimes", "setattrlist",
                     "fsetattrlist", "setxattr", "fsetxattr", "removexattr", "fremovexattr", "chflags")):
        return "attr"
    if c.startswith(("read", "pread", "rddata", "rdmeta", "getdirentries", "readlink", "execve", "posix_spawn")):
        return "read"
    return None  # stat/access/getattrlist & co: pure lookups, too noisy


def run_fs_usage():
    try:
        proc = subprocess.Popen(FS_USAGE_CMD, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", env=_C_ENV)
    except OSError as e:
        return str(e)
    atexit.register(proc.terminate)
    started = False
    for line in proc.stdout:
        m = _FSU_LINE.match(line)
        if not m:
            continue
        if not started:
            started = True
            with STATE_LOCK:
                STATE["fileops_source"], STATE["fileops_hint"] = "fs_usage", None
        call, details, _, who = m.groups()
        if _FSU_ERRNO.search(details):
            continue
        op = _fsu_op(call, details)
        pm = _FSU_PATH.search(details)
        if not op or not pm:
            continue
        path = pm.group(1)
        if path.startswith("/dev/disk") and " /" in path:
            path = path.split(None, 1)[1]
        name, _, tid = who.rpartition(".")
        _add_fileop(time.time(), op, path, name if name and tid.isdigit() else who)
    proc.wait()
    return proc.stderr.read().strip() or f"fs_usage exited ({proc.returncode})"


def fileops_monitor():
    fsevents_started = False
    while True:
        err = run_fs_usage()
        # only one ktrace session can exist system-wide, so retry later instead of giving up for good
        hint = "Process names require a sudoers rule for fs_usage" if "password" in err else err
        with STATE_LOCK:
            STATE["fileops_source"], STATE["fileops_hint"] = "fsevents", hint
        if not fsevents_started:
            fsevents_started = True
            try:
                start_fsevents()
            except OSError as e:
                with STATE_LOCK:
                    STATE["fileops_hint"] = f"FSEvents nicht verfügbar: {e}"
        time.sleep(30)

# --- Network history ---------------------------------------------------------

def _net_add(buckets, size, ts, traffic):
    start = ts - ts % size
    if not buckets or buckets[-1][0] != start:
        buckets.append((start, {}))
    bucket = buckets[-1][1]
    for name, (rx, tx) in traffic.items():
        acc = bucket.setdefault(name, [0, 0])
        acc[0] += rx
        acc[1] += tx


def net_record(ts, traffic):
    with NET_LOCK:
        _net_add(NET_FINE, 10, ts, traffic)
        _net_add(NET_COARSE, 60, ts, traffic)


def _unique_pid(by_name, name):
    pids = by_name.get(name.lower(), ())
    return pids[0] if len(pids) == 1 else None


def net_summary(rng, fs=None):
    with PROC_LOCK:
        procs = PROC_NOW
    by_name = {}
    for pid, p in procs.items():
        by_name.setdefault(p["name"].lower(), []).append(pid)
    buckets, size = (NET_FINE, 10) if rng <= 3600 else (NET_COARSE, 60)
    cutoff = time.time() - rng
    totals = {}
    with NET_LOCK:
        for start, bucket in buckets:
            if start + size <= cutoff:
                continue
            for name, (rx, tx) in bucket.items():
                acc = totals.setdefault(name, [0, 0])
                acc[0] += rx
                acc[1] += tx
    rows = [{"name": n, "rx": rx, "tx": tx, "pid": _unique_pid(by_name, n)}
            for n, (rx, tx) in totals.items() if _focus_name(fs, n)]
    rows.sort(key=lambda r: r["rx"] + r["tx"], reverse=True)
    return {"range": rng, "since": NET_SINCE, "rows": rows}


_fs_callback = _FSEventCallback(_on_fs_events)


def start_fsevents():
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFArrayCreate.restype = ctypes.c_void_p
    cf.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p]
    cs.FSEventStreamCreate.restype = ctypes.c_void_p
    cs.FSEventStreamCreate.argtypes = [
        ctypes.c_void_p, _FSEventCallback, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint64, ctypes.c_double, ctypes.c_uint32,
    ]
    cs.FSEventStreamSetDispatchQueue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cs.FSEventStreamStart.argtypes = [ctypes.c_void_p]
    cs.FSEventStreamStart.restype = ctypes.c_bool
    _libsys.dispatch_queue_create.restype = ctypes.c_void_p
    _libsys.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]

    roots = [cf.CFStringCreateWithCString(None, p.encode(), 0x08000100) for p in FILEOPS_ROOTS]
    array = cf.CFArrayCreate(
        None, (ctypes.c_void_p * len(roots))(*roots), len(roots),
        ctypes.addressof(ctypes.c_void_p.in_dll(cf, "kCFTypeArrayCallBacks")),
    )
    # flags: NoDefer | IgnoreSelf | FileEvents; sinceWhen = kFSEventStreamEventIdSinceNow
    stream = cs.FSEventStreamCreate(None, _fs_callback, None, array, 0xFFFFFFFFFFFFFFFF, 1.0, 0x02 | 0x08 | 0x10)
    if not stream:
        raise OSError("FSEventStreamCreate failed")
    cs.FSEventStreamSetDispatchQueue(stream, _libsys.dispatch_queue_create(b"sysmon.fsevents", None))
    if not cs.FSEventStreamStart(stream):
        raise OSError("FSEventStreamStart failed")


# --- Sampler -----------------------------------------------------------------

def sampler():
    global NET_PID, APPS
    prev_ticks = cpu_ticks()
    types = core_types(len(prev_ticks))
    iface = default_iface()
    iface_checked = time.monotonic()
    last_override = None
    prev_iface = read_iface_bytes(iface)
    # Remembers counters of vanished processes too, so a reappearing pid isn't double-counted
    net_seen, net_primed, last_seq, last_net_t, top_net = {}, False, 0, 0.0, []
    net_ready = False
    prev_t = time.monotonic()
    prev_procs = {}
    apps_checked = 0.0

    while True:
        time.sleep(SAMPLE_INTERVAL)
        now = time.monotonic()
        wall = time.time()
        dt = max(now - prev_t, 1e-3)
        prev_t = now
        update = {}

        try:
            ticks = cpu_ticks()
            cores, busy_all, total_all = [], 0, 0
            for kind, cur, old in zip(types, ticks, prev_ticks):
                d = [max(c - o, 0) for c, o in zip(cur, old)]
                total, busy = sum(d), sum(d) - d[2]
                busy_all += busy
                total_all += total
                cores.append({"type": kind, "pct": round(100.0 * busy / total, 1) if total else 0.0})
            prev_ticks = ticks
            update["cpu"] = {"total": round(100.0 * busy_all / total_all, 1) if total_all else 0.0, "cores": cores}
        except OSError as e:
            update["error"] = str(e)

        update["gpu"] = read_gpu()
        update["mem"] = read_memory()

        procs = read_ps()
        for pid, p in procs.items():
            old = prev_procs.get(pid)
            if old is not None and old["name"] == p["name"]:
                d = max(p["cputime"] - old["cputime"], 0.0)
            else:
                d = p["cputime"] if prev_procs else 0.0
            p["d"] = d
            p["cpu"] = round(100.0 * d / dt, 1)
        proc_record(wall, procs)
        if now - apps_checked > 5:
            apps_checked = now
            APPS = read_apps()
        prev_procs = procs

        with STATE_LOCK:
            selected_iface = STATE.get("iface_selected")
        if selected_iface != last_override:
            last_override = selected_iface
            iface_checked = 0
        if selected_iface:
            new_iface = selected_iface
        elif now - iface_checked > 30:
            iface_checked = now
            new_iface = default_iface()
        else:
            new_iface = iface
        if new_iface != iface:
            iface = new_iface
            prev_iface = read_iface_bytes(iface)
        rx, tx = read_iface_bytes(iface)
        seq, snap, snap_t = nettop_snapshot()
        if seq != last_seq:
            last_seq = seq
            if not net_primed:
                # the first sample is cumulative since boot, so it only serves as the baseline
                net_seen = {k: (b_in, b_out, wall) for k, (b_in, b_out) in snap.items()}
                net_primed, last_net_t = True, snap_t
            else:
                ndt = max(snap_t - last_net_t, 1e-3)
                last_net_t = snap_t
                top_net, traffic = [], {}
                for key, (b_in, b_out) in snap.items():
                    o_in, o_out, _ = net_seen.get(key, (0, 0, 0))
                    # counters below the last value mean a reset -> count everything since then
                    d_in = b_in - o_in if b_in >= o_in else b_in
                    d_out = b_out - o_out if b_out >= o_out else b_out
                    net_seen[key] = (b_in, b_out, wall)
                    if d_in or d_out:
                        name, _, pid = key.rpartition(".")
                        name = name or key
                        acc = traffic.setdefault(name, [0, 0])
                        acc[0] += d_in
                        acc[1] += d_out
                        top_net.append({"name": name, "pid": pid, "rx": d_in / ndt, "tx": d_out / ndt})
                top_net.sort(key=lambda p: p["rx"] + p["tx"], reverse=True)
                NET_PID = {int(p["pid"]): (p["rx"], p["tx"]) for p in top_net if p["pid"].isdigit()}
                net_record(wall, traffic)
                net_ready = True
                if len(net_seen) > 5000:
                    net_seen = {k: v for k, v in net_seen.items() if wall - v[2] < 3600}
        update["net"] = {
            "iface": iface,
            "rx_rate": max(rx - prev_iface[0], 0) / dt,
            "tx_rate": max(tx - prev_iface[1], 0) / dt,
            "rx_total": rx,
            "tx_total": tx,
            "top": top_net[:TOP_N],
            "ready": net_ready,
            "up": round(wall - NET_SINCE),
        }
        prev_iface = (rx, tx)

        with STATE_LOCK:
            STATE.update(update)


HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
    * { scrollbar-width: thin; scrollbar-color: #3c3c3c transparent; }
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background-color: #3c3c3c; border-radius: 4px; border: 2px solid transparent;
                                background-clip: padding-box; }
    ::-webkit-scrollbar-thumb:hover { background-color: #50fa7b99; }
    ::-webkit-scrollbar-corner { background: transparent; }
    body { font-family: -apple-system, sans-serif; font-size: 11px; background: #1e1e1e; color: #ddd; margin: 0 8px 8px; }
    #topbar { position: sticky; top: 0; z-index: 30; background: #1e1e1e; padding: 8px 0 2px; }
    #topbar .tools { margin-bottom: 2px; }
    #focus-note:empty { display: none; }
    .sec { margin-bottom: 12px; }
    .sec-head { display: flex; align-items: center; gap: 4px; padding: 2px 0 4px; border-bottom: 1px solid #444;
                cursor: pointer; user-select: none; position: sticky; top: var(--topbar-h, 0px); z-index: 25;
                background: #1e1e1e; }
    .sec-head .arrow { width: 10px; color: #888; flex: 0 0 auto; }
    .sec-head .title { font-weight: bold; color: #fff; white-space: nowrap; }
    .sec-head .net-iface { flex: 0 0 auto; max-width: 110px; padding: 0 2px; border: 1px solid #555; border-radius: 3px;
                           background: #2b2b2b; color: #8be9fd; font-size: 10px; }
    .sec-head .sum { flex: 1; min-width: 0; text-align: right; color: #50fa7b; white-space: nowrap; overflow: hidden;
                     text-overflow: ellipsis; padding-right: 3px; }
    .sec-head .btn { flex: 0 0 auto; }
    .sec-body { display: none; padding-top: 6px; }
    .sec.open > .sec-body { display: block; }
    .metric { position: relative; margin: 6px 0; cursor: pointer; user-select: none; }
    .label { font-weight: bold; color: #aaa; }
    .val { float: right; color: #50fa7b; }
    .bar-bg { background: #333; height: 6px; border-radius: 3px; margin-top: 2px; overflow: hidden; }
    .bar-fill { background: #50fa7b; height: 100%; width: 0%; transition: width 0.3s; }
    .bar-fill.warn { background: #f1fa8c; }
    .bar-fill.crit { background: #ff5555; }
    .folder .bar-fill { background: #8be9fd; }
    .folder .bar-fill.warn { background: #bd93f9; }
    .folder .bar-fill.crit { background: #ff79c6; }
    .pop { display: none; position: absolute; left: 10px; width: 65%; max-width: 260px; top: 100%; z-index: 10; margin-top: 2px;
           background: #2b2b2b; border: 1px solid #555; border-radius: 4px; padding: 6px; font-weight: normal;
           box-shadow: 0 4px 12px rgba(0,0,0,.6); pointer-events: none; }
    .metric:hover > .pop, .sec-head:hover > .pop { display: block; }
    .sec-head:hover { z-index: 40; }
    .row { display: flex; justify-content: space-between; align-items: center; gap: 6px; white-space: nowrap; line-height: 1.5; }
    .row .name { overflow: hidden; text-overflow: ellipsis; }
    .row .num { color: #8be9fd; margin-left: auto; }
    .sub { font-size: 10px; color: #888; margin: 6px 0 2px; text-transform: uppercase; }
    .sub:first-child { margin-top: 0; }
    .dim { color: #777; }
    .core { display: grid; grid-template-columns: 26px 1fr 40px; align-items: center; gap: 6px; line-height: 1.4; }
    .core .bar-bg { margin: 0; height: 5px; }
    .core span:last-child { text-align: right; color: #8be9fd; }
    .prow, .portrow { display: grid; gap: 4px; align-items: center; line-height: 1.7; cursor: pointer;
                      white-space: nowrap; border-radius: 3px; }
    .prow { grid-template-columns: 1fr 50px 60px 112px; }
    .portrow { grid-template-columns: 40px 30px 1fr 40px 112px; }
    .prow:hover, .portrow:hover { background: #2f2f2f; }
    .prow.sel, .portrow.sel { background: #353535; }
    .prow.dead .name { color: #777; text-decoration: line-through; }
    .prow .name, .portrow .name { overflow: hidden; text-overflow: ellipsis; padding-left: 2px; }
    .prow .num { text-align: right; color: #8be9fd; }
    .phead, .phead:hover { color: #888; cursor: default; background: none; }
    .phead .sortable { cursor: pointer; }
    .phead .sortable:hover, .phead .active { color: #ddd; }
    .phead.grp, .phead.grp:hover { cursor: pointer; color: #ddd; background: #262626; margin-top: 2px; }
    .portrow .port { color: #8be9fd; text-align: right; padding-right: 4px; }
    .badge { text-align: right; font-size: 10px; color: #777; }
    .badge.ext { color: #ffb86c; }
    .plist, #ports-list, #fileops { max-height: 320px; overflow-y: auto; }
    .kill { display: flex; gap: 2px; justify-content: flex-end; }
    .kill .btn { color: #ff5555; }
    .kill .btn.focus { color: #ff79c6; }
    .fop-head .kill { margin-left: auto; }
    .pdetail { margin: 0 0 6px 6px; padding: 4px 6px; border-left: 2px solid #ff79c6; background: #1e1e1e;
               user-select: text; cursor: auto; }
    .cmd { word-break: break-all; color: #bbb; margin: 4px 0; font-family: ui-monospace, Menlo, monospace; font-size: 10px; }
    .fop-mini { display: grid; grid-template-columns: 48px 52px 1fr; gap: 4px; line-height: 1.4; }
    .fop-mini .path { word-break: break-all; color: #bbb; }
    #toast { position: fixed; bottom: 8px; left: 8px; right: 8px; background: #50fa7b; color: #222; padding: 4px 6px;
             border-radius: 4px; opacity: 0; transition: opacity .2s; pointer-events: none; word-break: break-all; z-index: 20; }
    #toast.show { opacity: 1; }
    .fop { padding: 2px 3px; border-bottom: 1px solid #2a2a2a; line-height: 1.4; border-radius: 3px; }
    .fop:hover { background: #262626; }
    .fop.has-proc { cursor: pointer; }
    .fop-head { display: flex; gap: 6px; white-space: nowrap; }
    .fop-head .proc { color: #ff79c6; overflow: hidden; text-overflow: ellipsis; }
    .fop-path { word-break: break-all; user-select: text; color: #bbb; }
    .fop-path .file { color: #fff; }
    .folder { margin-bottom: 4px; padding: 1px 3px; border-radius: 3px; cursor: pointer; }
    .folder:hover { background: #262626; }
    .folder .btn { color: #ff5555; }
    .btn { background: #333; color: #ddd; border: 1px solid #555; border-radius: 3px; font-size: 10px;
           padding: 0 5px; line-height: 15px; cursor: pointer; }
    .btn.on { background: #f1fa8c; color: #222; border-color: #f1fa8c; }
    .ops { display: flex; flex-wrap: wrap; gap: 3px; margin-bottom: 4px; }
    .optoggle { opacity: .35; }
    .optoggle.active { opacity: 1; border-color: currentColor; }
    .op-created { color: #50fa7b; } .op-modified { color: #8be9fd; } .op-write { color: #8be9fd; }
    .op-renamed { color: #f1fa8c; } .op-deleted { color: #ff5555; } .op-attr { color: #bd93f9; } .op-read { color: #999; }
    .tools { display: flex; gap: 4px; margin-bottom: 4px; }
    .tools input, .tools select { flex: 1; min-width: 0; background: #2b2b2b; color: #ddd; border: 1px solid #555;
           border-radius: 3px; font-size: 11px; padding: 1px 4px; }
    .tools select { flex: 0 0 auto; }
    .focusbar { margin-bottom: 2px; }
    .focusbar input.active { border-color: #ff79c6; }
    #focus-note { margin-bottom: 6px; }
    #net-table-wrap { max-height: 260px; overflow-y: auto; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th { text-align: right; color: #888; font-weight: normal; position: sticky; top: 0; background: #1e1e1e; }
    th:first-child, td:first-child { text-align: left; width: 40%; }
    th:last-child { width: 112px; }
    td { text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #8be9fd; line-height: 1.5; }
    td:first-child { color: #ddd; }
    tr.total td { border-top: 1px solid #444; font-weight: bold; }
    tr.nrow { cursor: pointer; }
    tr.nrow:hover td { background: #2f2f2f; }
    tr.nrow.sel td { background: #353535; }
    tr.ndetail td { white-space: normal; text-align: left; padding: 0; color: #ddd; }
    .conn { padding: 2px 0; border-bottom: 1px solid #2a2a2a; line-height: 1.4; }
    .conn-head { display: flex; gap: 5px; white-space: nowrap; }
    .conn-head .host { overflow: hidden; text-overflow: ellipsis; flex: 1; color: #fff; }
    .conn-head .state { color: #777; font-size: 10px; }
    .conn-head .state.est { color: #50fa7b; }
    .svc { font-size: 10px; padding: 0 3px; border-radius: 3px; background: #333; color: #8be9fd; }
    .svc.udp { color: #f1fa8c; }
    .conn .dim { word-break: break-all; }
</style>
</head>
<body>
    <div id="topbar">
        <div class="tools focusbar">
            <input id="focus-input" placeholder="Focus: process (name / PID) and child processes…">
            <button class="btn" id="focus-clear" title="Clear focus">✕</button>
            <button class="btn" id="pause-all" title="Pause all sections">⏸</button>
        </div>
        <div class="dim" id="focus-note"></div>
    </div>
    <div class="sec" id="sec-sys">
        <div class="sec-head">
            <span class="arrow">▸</span><span class="title">CPU / RAM</span><span class="sum" id="sys-sum"></span>
            <button class="btn" data-export="sys" title="Export CSV to ~/Downloads">⤓</button>
            <button class="btn" data-pause="sys" title="Pause">⏸</button>
        </div>
        <div class="metric" id="cpu-metric">
            <span class="label">CPU</span><span id="cpu-val" class="val">–</span>
            <div class="bar-bg"><div id="cpu-bar" class="bar-fill"></div></div>
            <div class="pop" id="cpu-pop"></div>
        </div>
        <div class="metric" id="ram-metric">
            <span class="label">RAM</span><span id="ram-val" class="val">–</span>
            <div class="bar-bg"><div id="ram-bar" class="bar-fill"></div></div>
            <div class="pop" id="ram-pop"></div>
        </div>
        <div class="sec-body" id="sys-body"></div>
    </div>

    <div class="sec" id="sec-net">
        <div class="sec-head">
            <span class="arrow">▸</span><span class="title">Network</span>
            <select class="net-iface" id="net-iface" aria-label="Network interface" title="Select network interface">
                <option value="">Default</option>
            </select>
            <span class="sum" id="net-sum">–</span>
            <button class="btn" data-export="net" title="Export CSV to ~/Downloads">⤓</button>
            <button class="btn" data-pause="net" title="Pause">⏸</button>
            <div class="pop" id="net-pop"></div>
        </div>
        <div class="sec-body">
            <div class="tools">
                <input id="net-filter" placeholder="Filter process…">
                <select id="net-range">
                    <option value="60">1 min</option>
                    <option value="300" selected>5 min</option>
                    <option value="900">15 min</option>
                    <option value="1800">30 min</option>
                    <option value="3600">1 h</option>
                    <option value="43200">12 h</option>
                    <option value="86400">24 h</option>
                </select>
            </div>
            <div id="net-table-wrap"><table>
                <thead><tr><th>Process</th><th>↓</th><th>↑</th><th>Σ</th><th></th></tr></thead>
                <tbody id="net-table"></tbody>
            </table></div>
            <div class="dim" id="net-note"></div>
        </div>
    </div>

    <div class="sec" id="sec-ports">
        <div class="sec-head">
            <span class="arrow">▸</span><span class="title">Ports</span><span class="sum" id="ports-sum">–</span>
            <button class="btn" data-export="ports" title="Export CSV to ~/Downloads">⤓</button>
            <button class="btn" data-pause="ports" title="Pause">⏸</button>
        </div>
        <div class="sec-body">
            <div class="tools">
                <input id="ports-filter" placeholder="Filter port / process / PID…">
                <select id="ports-proto">
                    <option value="tcp" selected>TCP (LISTEN)</option>
                    <option value="all">TCP + UDP</option>
                </select>
            </div>
            <div id="ports-list"></div>
            <div class="dim">Click for process details · external = reachable on all interfaces</div>
        </div>
    </div>

    <div class="sec" id="sec-disk">
        <div class="sec-head">
            <span class="arrow">▸</span><span class="title">Disk Activity</span><span class="sum" id="disk-sum">–</span>
            <button class="btn" data-export="disk" title="Export CSV to ~/Downloads">⤓</button>
            <button class="btn" data-pause="disk" title="Pause">⏸</button>
        </div>
        <div class="sec-body">
            <div class="tools"><input id="fileops-filter" placeholder="Filter path/process… or p:process"></div>
            <div class="ops" id="disk-ops"></div>
            <div class="dim" id="fileops-hint"></div>
            <div id="fileops"><span class="dim">warte auf Events…</span></div>
        </div>
    </div>

    <div class="sec" id="sec-folders">
        <div class="sec-head">
            <span class="arrow">▸</span><span class="title">Watched Folders</span><span class="sum" id="folders-sum">–</span>
            <button class="btn" id="folders-edit" title="Edit list">✎</button>
            <button class="btn" id="folders-rescan" title="Rescan now">⟳</button>
            <button class="btn" data-export="folders" title="Export CSV to ~/Downloads">⤓</button>
        </div>
        <div class="sec-body">
            <div class="tools" id="folders-add" style="display: none">
                <input id="folders-add-input" placeholder="Add folder, e.g. ~/Library/Caches/pip">
                <button class="btn" id="folders-add-btn" title="Add">+</button>
            </div>
            <div id="folders"><span class="dim">scanning…</span></div>
        </div>
    </div>
    <div id="toast"></div>

<script>
    const $ = id => document.getElementById(id);
    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }
    function fmtBytes(b) {
        if (b == null) return '–';
        const u = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0;
        while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
        return b.toFixed(i < 2 ? 0 : 1) + ' ' + u[i];
    }
    const fmtRate = b => fmtBytes(b) + '/s';
    function bar(pct, warnAt = 70, critAt = 90) {
        const bg = el('div', 'bar-bg'), f = el('div', 'bar-fill');
        setFill(f, pct, warnAt, critAt);
        bg.append(f);
        return bg;
    }
    function setFill(f, pct, warnAt = 70, critAt = 90) {
        f.style.width = Math.min(Math.max(pct, 0), 100) + '%';
        f.classList.toggle('warn', pct >= warnAt && pct < critAt);
        f.classList.toggle('crit', pct >= critAt);
    }
    function row(name, num, title) {
        const r = el('div', 'row'), n = el('span', 'name', name);
        if (title) n.title = title;
        r.append(n, el('span', 'num', num));
        return r;
    }
    const fill = (id, nodes) => $(id).replaceChildren(...nodes);
    const hhmmss = t => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
    const RANGES = [[0, 'now'], [60, '1 min'], [300, '5 min'], [900, '15 min'], [1800, '30 min'],
                    [3600, '1 h'], [43200, '12 h'], [86400, '24 h']];
    const rangeLabel = r => (RANGES.find(x => x[0] === r) || [0, r + ' s'])[1];
    const getJSON = async url => (await fetch(url)).json();
    // global focus: all sections only show this process and its children
    const focusParam = () => '&focus=' + encodeURIComponent($('focus-input').value.trim());
    async function postJSON(url, body) {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Sysmon': '1' },
                                     body: JSON.stringify(body) });
        return r.json();
    }

    // don't re-render under an active text selection, otherwise copy & paste is impossible
    function hasSelectionIn(node) {
        const s = window.getSelection();
        return !!s && !s.isCollapsed && node.contains(s.anchorNode);
    }
    function toast(msg) {
        $('toast').textContent = msg;
        $('toast').classList.add('show');
        clearTimeout(toast.timer);
        toast.timer = setTimeout(() => $('toast').classList.remove('show'), 2200);
    }
    async function copyText(text, msg) {
        try {
            await navigator.clipboard.writeText(text);
        } catch (e) {
            const ta = el('textarea');
            ta.value = text;
            document.body.append(ta);
            ta.select();
            document.execCommand('copy');
            ta.remove();
        }
        toast(msg || 'Copied to clipboard: ' + text);
    }
    // Same Focus / kill / -9 trio on every row; without a unique pid it targets the process name
    function actionButtons(t) {
        const wrap = el('span', 'kill');
        const quoted = "'" + t.name.split("'").join("'" + String.fromCharCode(92) + "''") + "'";
        const cmd = sig => t.pid != null ? `kill ${sig}${t.pid}` : `pkill ${sig}-x ${quoted}`;
        const focus = el('button', 'btn focus', 'Focus');
        focus.title = `Filter all sections to ${t.name} and child processes`;
        focus.addEventListener('click', ev => {
            ev.stopPropagation();
            setFocus(String(t.pid != null ? t.pid : t.name));
            window.scrollTo({ top: 0 });
        });
        wrap.append(focus);
        for (const [label, sig] of [['kill', ''], ['-9', '-9 ']]) {
            const b = el('button', 'btn', label);
            b.title = `Copy "${cmd(sig)}" (${t.name})`;
            b.addEventListener('click', ev => { ev.stopPropagation(); copyText(cmd(sig)); });
            wrap.append(b);
        }
        return wrap;
    }

    // --- sections: collapse, pause, export ---------------------------------------
    const paused = { sys: false, net: false, ports: false, disk: false };
    const sections = {};
    let openState = {};
    try { openState = JSON.parse(localStorage.getItem('sysmon.open')) || {}; } catch (e) {}
    if (!Object.keys(openState).length) openState = { disk: true, folders: true };
    const isOpen = key => $('sec-' + key).classList.contains('open');
    function setOpen(key, open) {
        const sec = $('sec-' + key);
        sec.classList.toggle('open', open);
        sec.querySelector('.arrow').textContent = open ? '▾' : '▸';
        openState[key] = open;
        try { localStorage.setItem('sysmon.open', JSON.stringify(openState)); } catch (e) {}
        if (open && sections[key].onOpen) sections[key].onOpen();
    }
    for (const sec of document.querySelectorAll('.sec')) {
        const key = sec.id.slice(4);
        sec.querySelector('.sec-head').addEventListener('click', ev => {
            if (!ev.target.closest('button, select')) setOpen(key, !isOpen(key));
        });
    }
    function setPaused(key, value) {
        paused[key] = value;
        const b = document.querySelector(`[data-pause="${key}"]`);
        b.classList.toggle('on', value);
        b.textContent = value ? '▶' : '⏸';
        b.title = value ? 'Resume' : 'Pause';
        if (!value) { update(); sections[key].onOpen(); }
    }
    for (const b of document.querySelectorAll('[data-pause]')) {
        b.addEventListener('click', ev => {
            ev.stopPropagation();
            setPaused(b.dataset.pause, !paused[b.dataset.pause]);
            syncPauseAll();
        });
    }
    function syncPauseAll() {
        const all = Object.values(paused).every(Boolean);
        const b = $('pause-all');
        b.classList.toggle('on', all);
        b.textContent = all ? '▶' : '⏸';
        b.title = all ? 'Resume all' : 'Pause all sections';
    }
    $('pause-all').addEventListener('click', () => {
        const target = !Object.values(paused).every(Boolean);
        for (const key of Object.keys(paused)) if (paused[key] !== target) setPaused(key, target);
        syncPauseAll();
    });
    // section headers stick right below the top bar
    new ResizeObserver(() => document.documentElement.style.setProperty('--topbar-h', $('topbar').offsetHeight + 'px'))
        .observe($('topbar'));
    for (const b of document.querySelectorAll('[data-export]')) {
        b.addEventListener('click', async ev => {
            ev.stopPropagation();
            const key = b.dataset.export;
            const data = sections[key].exportRows();
            if (!data.rows.length) { toast('Nothing to export'); return; }
            try {
                const r = await postJSON('/api/export', { section: key, header: data.header, rows: data.rows });
                toast(r.path ? 'Exported: ' + r.path : 'Export failed: ' + r.error);
            } catch (e) { toast('Export failed'); }
        });
    }

    function renderProcDetail(i) {
        const box = el('div', 'pdetail');
        box.addEventListener('click', ev => ev.stopPropagation());
        if (!i) { box.append(el('span', 'dim', 'loading…')); return box; }
        if (i.error) { box.append(el('span', 'dim', i.error)); return box; }
        const r = rangeLabel(i.range);
        box.append(
            row('PID / PPID', `${i.pid} / ${i.ppid ?? '–'}`),
            row('User', i.user || '–'),
            row('Started', i.started || (i.alive ? '–' : 'ended')),
            row('CPU now / avg ' + r, `${i.cpu_now.toFixed(1)}% / ${i.cpu_avg.toFixed(1)}%`),
            row('RAM now / peak ' + r, `${fmtBytes(i.mem)} / ${fmtBytes(i.mem_peak)}`),
            row('Network now', `↓ ${fmtRate(i.net[0])} ↑ ${fmtRate(i.net[1])}`),
            row('Child processes', String(i.children)));
        if (i.ports && i.ports.length) box.append(row('Ports', i.ports.join(', ')));
        if (i.command) box.append(el('div', 'cmd', i.command));
        box.append(el('div', 'sub', `Disk operations, 5 min (${i.fileops_count})`));
        if (i.fileops_hint) box.append(el('div', 'dim', i.fileops_hint));
        for (const o of i.fileops) {
            const f = el('div', 'fop-mini');
            f.append(el('span', 'dim', hhmmss(o.t)), el('span', 'op-' + o.op, o.op + (o.n > 1 ? ' ×' + o.n : '')),
                     el('span', 'path', o.path));
            box.append(f);
        }
        return box;
    }

    // --- CPU / RAM: process table ------------------------------------------------
    const proc = { data: null, open: new Map(), seq: 0, sort: 'cpu', desc: true, cat: 'all', group: false, closed: new Set() };
    try { Object.assign(proc, JSON.parse(localStorage.getItem('sysmon.proc')) || {}); proc.closed = new Set(proc.closed); } catch (e) {}
    const saveProc = () => { try {
        localStorage.setItem('sysmon.proc', JSON.stringify({ cat: proc.cat, group: proc.group, closed: [...proc.closed] }));
    } catch (e) {} };
    const procFilter = el('input'), procRange = el('select');
    procFilter.placeholder = 'Filter Name / PID / User…';
    for (const [v, l] of RANGES) {
        const o = el('option', '', l);
        o.value = v;
        procRange.append(o);
    }
    const procTools = el('div', 'tools'), procHead = el('div', 'prow phead');
    const procList = el('div', 'plist'), procNote = el('div', 'dim');
    const procChips = el('div', 'ops'), chipButtons = {};
    for (const [c, label, title] of [['all', 'All', 'Apps and processes'], ['app', 'Apps', 'Open apps (including their processes)'],
                                     ['user', 'User', 'Processes owned by the current user'], ['system', 'System', 'Root and system processes']]) {
        const b = el('button', 'btn optoggle', label);
        b.title = title;
        b.addEventListener('click', () => { proc.cat = c; saveProc(); procRefresh(true); });
        chipButtons[c] = b;
        procChips.append(b);
    }
    const groupBtn = el('button', 'btn optoggle', 'Groups');
    groupBtn.title = 'Group apps, user, and system processes';
    groupBtn.addEventListener('click', () => { proc.group = !proc.group; saveProc(); procRefresh(true); });
    procChips.append(groupBtn);
    procTools.append(procFilter, procRange);
    $('sys-body').append(procTools, procChips, procHead, procList, procNote);

    async function procRefresh(force) {
        if (!isOpen('sys') || (paused.sys && !force)) return;
        const my = ++proc.seq;
        let data;
        try {
            data = await getJSON(`/api/procs?sort=${proc.sort}&dir=${proc.desc ? 'desc' : 'asc'}` +
                                 `&range=${procRange.value}&q=${encodeURIComponent(procFilter.value)}` +
                                 `&cat=${proc.cat}&group=${proc.group ? 1 : 0}` + focusParam());
            for (const pid of proc.open.keys()) proc.open.set(pid, await getJSON(`/api/proc?pid=${pid}&range=${procRange.value}`));
        } catch (e) { return; }
        if (my !== proc.seq) return;
        proc.data = data;
        $('sys-sum').textContent = data.total + ' processes';
        if (force || !hasSelectionIn($('sys-body'))) procRender();
    }
    function procSortBy(col) {
        if (proc.sort === col) proc.desc = !proc.desc;
        else { proc.sort = col; proc.desc = col !== 'name'; }
        paused.sys ? procRender() : procRefresh(true);
    }
    function procRender() {
        const r = +procRange.value;
        const cols = [['name', 'Process'], ['cpu', r ? 'Avg CPU' : 'CPU'], ['mem', r ? 'RAM peak' : 'RAM']];
        procHead.replaceChildren(...cols.map(([c, label], i) => {
            const active = proc.sort === c;
            const h = el('span', (i ? 'num' : 'name') + ' sortable' + (active ? ' active' : ''),
                         label + (active ? (proc.desc ? ' ▾' : ' ▴') : ''));
            h.title = 'Sort';
            h.addEventListener('click', () => procSortBy(c));
            return h;
        }), el('span'));
        if (!proc.data) return;
        for (const [c, b] of Object.entries(chipButtons)) b.classList.toggle('active', proc.cat === c);
        groupBtn.classList.toggle('active', proc.group);
        const q = procFilter.value.trim().toLowerCase();
        const dir = proc.desc ? -1 : 1;
        const sorted = list => list.slice().sort((a, b) =>
            proc.sort === 'name' ? dir * a.name.localeCompare(b.name) : dir * (a[proc.sort] - b[proc.sort]));
        let rows = proc.data.rows.slice();
        if (paused.sys && q) {
            rows = rows.filter(p => p.name.toLowerCase().includes(q) || String(p.pid) === q || p.user.toLowerCase() === q);
        }
        rows = sorted(rows);
        const apps = sorted(proc.data.apps || []);
        const procLine = (p, isApp) => {
            const line = el('div', 'prow' + (p.alive ? '' : ' dead') + (proc.open.has(p.pid) ? ' sel' : ''));
            const n = el('span', 'name', (isApp ? '' : p.isapp ? '● ' : '') + p.name + (isApp ? ` (${p.procs})` : ''));
            n.title = isApp ? `${p.procs} processes · main PID ${p.pid} · click for details`
                            : `PID ${p.pid}${p.user ? ' · ' + p.user : ''}${p.app ? ' · App: ' + p.app : ''} · click for details`;
            line.append(n, el('span', 'num', p.cpu.toFixed(1) + '%'), el('span', 'num', fmtBytes(p.mem)),
                        p.alive ? actionButtons({ pid: p.pid, name: p.name }) : el('span', 'dim', 'ended'));
            line.addEventListener('click', () => procToggle(p.pid));
            const out = [line];
            if (proc.open.has(p.pid)) out.push(renderProcDetail(proc.open.get(p.pid)));
            return out;
        };
        const nodes = [];
        const groupNodes = (id, title, list, isApp) => {
            if (!list.length) return;
            const open = !proc.closed.has(id);
            const sum = list.reduce((a, p) => [a[0] + p.cpu, a[1] + p.mem], [0, 0]);
            const head = el('div', 'prow phead grp');
            head.append(el('span', 'name', `${open ? '▾' : '▸'} ${title} (${list.length})`),
                        el('span', 'num', sum[0].toFixed(1) + '%'), el('span', 'num', fmtBytes(sum[1])), el('span'));
            head.title = 'Expand/collapse';
            head.addEventListener('click', () => {
                proc.closed.has(id) ? proc.closed.delete(id) : proc.closed.add(id);
                saveProc();
                procRender();
            });
            nodes.push(head);
            if (open) for (const p of list) nodes.push(...procLine(p, isApp));
        };
        if (proc.group && proc.cat !== 'app') {
            groupNodes('apps', 'Apps', apps, true);
            if (proc.cat !== 'system') groupNodes('user', 'User processes', rows.filter(p => p.user === '' || p.user === proc.data.me), false);
            if (proc.cat !== 'user') groupNodes('system', 'System', rows.filter(p => p.user !== '' && p.user !== proc.data.me), false);
        } else if (proc.cat === 'app') {
            for (const p of apps) nodes.push(...procLine(p, true));
        } else {
            for (const p of rows) nodes.push(...procLine(p, false));
        }
        procList.replaceChildren(...(nodes.length ? nodes : [el('div', 'dim', 'No matches')]));
        const shown = rows.length + apps.length;
        const notes = [`${shown} of ${proc.data.total}`, 'CPU: 100% = 1 core'];
        if (apps.length) notes.push('App rows include all app processes');
        if (r && proc.data.since > Date.now() / 1000 - r) notes.push('History since ' + hhmmss(proc.data.since));
        procNote.textContent = notes.join(' · ');
    }
    async function procToggle(pid) {
        if (proc.open.has(pid)) {
            proc.open.delete(pid);
            procRender();
            return;
        }
        proc.open.set(pid, null);
        procRender();
        try { proc.open.set(pid, await getJSON(`/api/proc?pid=${pid}&range=${procRange.value}`)); } catch (e) {}
        procRender();
    }
    let procTimer = null;
    procFilter.addEventListener('input', () => {
        if (paused.sys) { procRender(); return; }
        clearTimeout(procTimer);
        procTimer = setTimeout(() => procRefresh(true), 250);
    });
    procRange.addEventListener('change', () => procRefresh(true));
    function openSorted(col) {
        if (isOpen('sys') && proc.sort === col) { setOpen('sys', false); return; }
        proc.sort = col;
        proc.desc = true;
        isOpen('sys') ? procRefresh(true) : setOpen('sys', true);
    }
    $('cpu-metric').addEventListener('click', () => openSorted('cpu'));
    $('ram-metric').addEventListener('click', () => openSorted('mem'));
    sections.sys = {
        onOpen: () => procRefresh(true),
        exportRows: () => ({
            header: ['PID', 'Process', 'User', 'CPU %', 'RAM Bytes', 'Running'],
            rows: (proc.data ? proc.data.rows : []).map(p => [p.pid, p.name, p.user, p.cpu, p.mem, p.alive ? 'yes' : 'no']),
        }),
    };

    function renderCores(cpu, gpu) {
        const groups = { E: 'Efficiency cores', P: 'Performance cores', C: 'Cores' };
        const nodes = [];
        for (const [type, title] of Object.entries(groups)) {
            const cores = cpu.cores.filter(c => c.type === type);
            if (!cores.length) continue;
            const avg = cores.reduce((s, c) => s + c.pct, 0) / cores.length;
            nodes.push(el('div', 'sub', `${title} (${cores.length}) · Ø ${avg.toFixed(0)}%`));
            cores.forEach((c, n) => {
                const r = el('div', 'core');
                r.append(el('span', 'dim', type + n), bar(c.pct), el('span', '', c.pct.toFixed(0) + '%'));
                nodes.push(r);
            });
        }
        if (gpu && gpu.util != null) {
            nodes.push(el('div', 'sub', `GPU${gpu.cores ? ` (${gpu.cores} cores)` : ''}`));
            for (const [label, v] of [['Total', gpu.util], ['Render', gpu.renderer], ['Tiler', gpu.tiler]]) {
                if (v == null) continue;
                const r = el('div', 'core');
                r.append(el('span', 'dim', label.slice(0, 3)), bar(v), el('span', '', v + '%'));
                nodes.push(r);
            }
            if (gpu.mem != null) nodes.push(row('GPU memory', fmtBytes(gpu.mem)));
        }
        nodes.push(el('div', 'dim', 'Click to sort processes by CPU'));
        fill('cpu-pop', nodes);
    }

    // --- network -------------------------------------------------------------------
    let netSummary = null;
    let netWarm = null;
    // nettop needs 20-40 s after a restart before per-process traffic is available
    function updateNetNote() {
        if (netWarm && !netWarm.ready) {
            $('net-note').textContent = `Preparing per-process traffic… nettop takes about 20–40 s to start after a restart (running for ${netWarm.up} s)`;
        } else if (netSummary) {
            const from = Date.now() / 1000 - netSummary.range;
            $('net-note').textContent = netSummary.since > from ? 'Data since startup: ' + hhmmss(netSummary.since) : '';
        }
    }
    const netOpen = new Map();
    let netIfaceOptions = '';
    $('net-iface').addEventListener('change', async () => {
        try {
            const result = await postJSON('/api/interface', { iface: $('net-iface').value });
            if (result.error) toast(result.error);
        } catch (e) { toast('Could not change network interface'); }
    });
    async function netRefresh(force) {
        if (!isOpen('net') || (paused.net && !force)) return;
        try {
            netSummary = await getJSON('/api/net?range=' + $('net-range').value + focusParam());
            for (const name of netOpen.keys()) netOpen.set(name, await getJSON('/api/conns?name=' + encodeURIComponent(name) + focusParam()));
        } catch (e) { return; }
        if (!hasSelectionIn($('net-table'))) renderNetTable();
    }
    async function netToggle(name) {
        if (netOpen.has(name)) { netOpen.delete(name); renderNetTable(); return; }
        netOpen.set(name, null);
        renderNetTable();
        try { netOpen.set(name, await getJSON('/api/conns?name=' + encodeURIComponent(name) + focusParam())); } catch (e) {}
        renderNetTable();
    }
    function renderConns(d) {
        const td = el('td');
        td.colSpan = 5;
        const box = el('div', 'pdetail');
        td.append(box);
        if (!d) { box.append(el('span', 'dim', 'loading…')); return td; }
        box.append(el('div', 'sub', `Current connections: ${d.total}` + (d.hidden ? ` (+${d.hidden} listening)` : '')));
        if (!d.conns.length) box.append(el('div', 'dim', 'No active connections'));
        for (const c of d.conns) {
            const v = el('div', 'conn'), head = el('div', 'conn-head');
            const svc = el('span', 'svc' + (c.proto.startsWith('udp') ? ' udp' : ''), c.service || c.proto.toUpperCase());
            svc.title = c.proto + (c.service ? ' · Dienst aus Port geraten' : '');
            const host = el('span', 'host', (c.host || c.remote) + ':' + c.rport);
            host.title = c.host ? `${c.host} (${c.remote})` : c.remote;
            const st = el('span', 'state' + (c.state === 'Established' ? ' est' : ''), c.state || '');
            head.append(svc, host, st);
            const io = c.rate ? `↓ ${fmtRate(c.rate[0])} ↑ ${fmtRate(c.rate[1])}` : '';
            v.append(head, el('div', 'dim',
                `PID ${c.pid} · ${c.proto} · local :${c.lport} · ↓ ${fmtBytes(c.rx)} ↑ ${fmtBytes(c.tx)} total` + (io ? ' · ' + io : '') +
                (c.host ? ' · ' + c.remote : '')));
            box.append(v);
        }
        if (d.total > d.conns.length) box.append(el('div', 'dim', `… ${d.total - d.conns.length} more`));
        return td;
    }
    const netRows = () => {
        const q = $('net-filter').value.trim().toLowerCase();
        return netSummary ? netSummary.rows.filter(r => !q || r.name.toLowerCase().includes(q)) : [];
    };
    function renderNetTable() {
        if (!netSummary) return;
        const rows = netRows();
        const tr = (cls, name, rx, tx, target) => {
            const t = el('tr', cls), n = el('td', '', name);
            n.title = name;
            const act = el('td');
            if (target) act.append(actionButtons(target));
            t.append(n, el('td', '', fmtBytes(rx)), el('td', '', fmtBytes(tx)), el('td', '', fmtBytes(rx + tx)), act);
            return t;
        };
        const sum = rows.reduce((a, r) => [a[0] + r.rx, a[1] + r.tx], [0, 0]);
        const nodes = [];
        for (const r of rows) {
            const t = tr('nrow' + (netOpen.has(r.name) ? ' sel' : ''), r.name, r.rx, r.tx, { pid: r.pid, name: r.name });
            t.firstChild.title = r.name + ' · click for connections';
            t.addEventListener('click', () => netToggle(r.name));
            nodes.push(t);
            if (netOpen.has(r.name)) {
                const d = el('tr', 'ndetail');
                d.append(renderConns(netOpen.get(r.name)));
                nodes.push(d);
            }
        }
        fill('net-table', [...nodes, tr('total', `Total (${rows.length})`, sum[0], sum[1])]);
        updateNetNote();
    }
    $('net-range').addEventListener('change', () => netRefresh(true));
    $('net-filter').addEventListener('input', renderNetTable);
    sections.net = {
        onOpen: () => netRefresh(true),
        exportRows: () => ({ header: ['Process', 'Downloaded bytes', 'Uploaded bytes'],
                             rows: netRows().map(r => [r.name, r.rx, r.tx]) }),
    };

    function updateNetInterfaces(data) {
        const select = $('net-iface');
        const optionsKey = JSON.stringify([data.interfaces, data.net.iface]);
        if (optionsKey !== netIfaceOptions) {
            netIfaceOptions = optionsKey;
            const options = [el('option', '', `Default (${data.net.iface})`)];
            options[0].value = '';
            for (const iface of data.interfaces) {
                const option = el('option', '', iface);
                option.value = iface;
                options.push(option);
            }
            select.replaceChildren(...options);
        }
        select.value = data.iface_selected || '';
    }

    // --- ports -----------------------------------------------------------------------
    const ports = { data: null, open: new Map(), seq: 0 };
    const portRows = () => {
        const q = $('ports-filter').value.trim().toLowerCase();
        return ports.data ? ports.data.rows.filter(r => !q || String(r.port).startsWith(q) ||
                                                    r.name.toLowerCase().includes(q) || String(r.pid) === q) : [];
    };
    async function portsRefresh(force) {
        if (paused.ports && !force) return;
        const my = ++ports.seq;
        let data;
        try {
            data = await getJSON('/api/ports?udp=' + ($('ports-proto').value === 'all' ? 1 : 0) + focusParam());
            for (const [key, pid] of [...ports.open.keys()].map(k => [k, k.split(':')[2]])) {
                ports.open.set(key, await getJSON(`/api/proc?pid=${pid}&range=300`));
            }
        } catch (e) { return; }
        if (my !== ports.seq) return;
        ports.data = data;
        $('ports-sum').textContent = `${data.rows.length} open · ${data.rows.filter(r => !r.local).length} external`;
        if (isOpen('ports') && (force || !hasSelectionIn($('ports-list')))) portsRender();
    }
    function portsRender() {
        if (!ports.data) return;
        const rows = portRows();
        const head = el('div', 'portrow phead');
        head.append(el('span', 'port', 'Port'), el('span', '', 'Proto'), el('span', 'name', 'Process'),
                el('span', 'badge', 'Binding'), el('span'));
        const nodes = [head];
        for (const r of rows) {
            const key = `${r.proto}:${r.port}:${r.pid}`;
            const line = el('div', 'portrow' + (ports.open.has(key) ? ' sel' : ''));
            line.title = 'Addresses: ' + r.addrs.join(', ') + ' · click for process details';
            line.append(el('span', 'port', r.port), el('span', 'dim', r.proto), el('span', 'name', `${r.name} (${r.pid})`),
                        el('span', r.local ? 'badge' : 'badge ext', r.local ? 'local' : 'external'),
                        actionButtons({ pid: r.pid, name: r.name }));
            line.addEventListener('click', () => portsToggle(key, r.pid));
            nodes.push(line);
            if (ports.open.has(key)) nodes.push(renderProcDetail(ports.open.get(key)));
        }
        if (!rows.length) nodes.push(el('div', 'dim', 'No matches'));
        fill('ports-list', nodes);
    }
    async function portsToggle(key, pid) {
        if (ports.open.has(key)) {
            ports.open.delete(key);
            portsRender();
            return;
        }
        ports.open.set(key, null);
        portsRender();
        try { ports.open.set(key, await getJSON(`/api/proc?pid=${pid}&range=300`)); } catch (e) {}
        portsRender();
    }
    $('ports-filter').addEventListener('input', portsRender);
    $('ports-proto').addEventListener('change', () => portsRefresh(true));
    sections.ports = {
        onOpen: () => portsRefresh(true),
        exportRows: () => ({ header: ['Port', 'Proto', 'Addresses', 'Process', 'PID', 'Binding'],
                             rows: portRows().map(r => [r.port, r.proto, r.addrs.join(' '), r.name, r.pid,
                                                        r.local ? 'local' : 'external']) }),
    };

    // --- disk activity -----------------------------------------------------------------
    const OPS = ['created', 'modified', 'write', 'renamed', 'deleted', 'attr', 'read'];
    const activeOps = new Set(OPS.filter(o => o !== 'read'));
    const opButtons = {};
    for (const op of OPS) {
        const b = el('button', 'btn optoggle op-' + op + (activeOps.has(op) ? ' active' : ''), op);
        b.title = 'ein-/ausblenden';
        b.addEventListener('click', () => {
            activeOps.has(op) ? activeOps.delete(op) : activeOps.add(op);
            b.classList.toggle('active', activeOps.has(op));
            onFileOpsFilterChange();
        });
        opButtons[op] = b;
        $('disk-ops').append(b);
    }

    let lastOps = null, filterTimer = null, fileopsSeq = 0;
    function onFileOpsFilterChange() {
        if (paused.disk) { renderFileOps(lastOps); return; }
        clearTimeout(filterTimer);
        filterTimer = setTimeout(fetchFileOps, 250);
    }
    $('fileops-filter').addEventListener('input', onFileOpsFilterChange);
    function setProcFilter(name) {
        $('fileops-filter').value = 'p:' + name;
        onFileOpsFilterChange();
    }
    async function fetchFileOps() {
        if (paused.disk) return;
        // ignore responses that were overtaken by a newer request (e.g. filter typed meanwhile)
        const my = ++fileopsSeq;
        const q = encodeURIComponent($('fileops-filter').value);
        let data;
        try {
            data = await getJSON(`/api/fileops?ops=${[...activeOps].join(',')}&q=${q}` + focusParam());
        } catch (e) { return; }
        if (my !== fileopsSeq || paused.disk) return;
        lastOps = data;
        if (!hasSelectionIn($('fileops'))) renderFileOps(lastOps);
    }
    function fileOpMatch(o, q) {
        if (q.startsWith('p:')) return o.proc.toLowerCase().includes(q.slice(2).trim());
        return o.path.toLowerCase().includes(q) || o.proc.toLowerCase().includes(q) || o.op.includes(q);
    }
    function shownOps() {
        if (!lastOps) return [];
        if (!paused.disk) return lastOps.ops;
        // while paused, filter the frozen snapshot locally instead of refetching
        const q = $('fileops-filter').value.trim().toLowerCase();
        return lastOps.ops.filter(o => activeOps.has(o.op) && (!q || fileOpMatch(o, q)));
    }
    function renderFileOps(data) {
        if (!data) return;
        const ops = shownOps(), matches = paused.disk ? ops.length : data.matches;
        $('disk-sum').textContent = `${matches}${matches !== data.total ? ' / ' + data.total : ''} · 5 min`;
        $('fileops-hint').textContent = data.hint || '';
        for (const op of OPS) opButtons[op].textContent = `${op} ${data.op_counts[op] || 0}`;
        if (!ops.length) {
            fill('fileops', [el('span', 'dim', data.total ? 'No matches' : 'No events in the last 5 min')]);
            return;
        }
        const nodes = ops.map(o => {
            const r = el('div', 'fop' + (o.proc ? ' has-proc' : ''));
            r.title = o.proc ? `Process: ${o.proc} · click to filter this process's disk activity`
                             : 'Unknown process';
            const head = el('div', 'fop-head'), p = el('div', 'fop-path');
            head.append(el('span', 'dim', hhmmss(o.t)), el('span', 'op-' + o.op, o.op));
            if (o.proc) head.append(el('span', 'proc', o.proc));
            if (o.n > 1) head.append(el('span', 'dim', '×' + o.n));
            if (o.proc) head.append(actionButtons({ pid: null, name: o.proc }));
            const cut = o.path.lastIndexOf('/') + 1;
            p.append(el('span', '', o.path.slice(0, cut)), el('span', 'file', o.path.slice(cut) + (o.dir ? '/' : '')));
            r.append(head, p);
            r.addEventListener('click', () => {
                if (hasSelectionIn(r)) return;
                if (o.proc) setProcFilter(o.proc);
                else toast(data.hint || 'Unknown process');
            });
            return r;
        });
        if (matches > ops.length) nodes.push(el('div', 'dim', `… ${matches - ops.length} more; use a filter`));
        fill('fileops', nodes);
    }
    sections.disk = {
        onOpen: () => fetchFileOps(),
        exportRows: () => ({ header: ['Time', 'Operation', 'Process', 'Path', 'Count'],
                             rows: shownOps().map(o => [new Date(o.t * 1000).toISOString(), o.op, o.proc, o.path, o.n]) }),
    };

    // --- watched folders ---------------------------------------------------------------
    let editing = false, lastFolders = [];
    async function folderAction(action, entry) {
        let r;
        try { r = await postJSON('/api/folders', { action, entry }); } catch (e) { toast('Error'); return; }
        if (r.error) { toast(r.error); return; }
        toast(action === 'add' ? 'Added: ' + entry : action === 'remove' ? 'Removed: ' + entry : 'Rescan started');
        update();
    }
    async function cdTo(f) {
        let r = null;
        try { r = await postJSON('/api/cd', { entry: f.entry }); } catch (e) {}
        if (r && r.ok) toast('cd ' + f.path);
        else if (r && r.cmd) copyText(r.cmd, 'No active iTerm tab; copied: ' + r.cmd);
    }
    $('folders-edit').addEventListener('click', ev => {
        ev.stopPropagation();
        editing = !editing;
        $('folders-edit').classList.toggle('on', editing);
        $('folders-add').style.display = editing ? 'flex' : 'none';
        if (editing && !isOpen('folders')) setOpen('folders', true);
        update();
    });
    $('folders-rescan').addEventListener('click', ev => { ev.stopPropagation(); folderAction('rescan'); });
    function addFolder() {
        const v = $('folders-add-input').value.trim();
        if (!v) return;
        $('folders-add-input').value = '';
        folderAction('add', v);
    }
    $('folders-add-btn').addEventListener('click', addFolder);
    $('folders-add-input').addEventListener('keydown', ev => { if (ev.key === 'Enter') addFolder(); });
    function renderFolders(d) {
        if (!d.folders) return;
        lastFolders = d.folders;
        $('folders-sum').textContent = 'Σ ' + fmtBytes(d.folders_total) + (d.folders_scanning ? ' · scanning…' : '');
        if (hasSelectionIn($('folders'))) return;
        const nodes = d.folders.map(f => {
            const wrap = el('div', 'folder'), line = el('div', 'row');
            const name = el('span', 'name', f.display);
            wrap.title = f.path + ' · click to cd in the active terminal tab';
            line.append(name, el('span', 'num', f.missing ? 'missing' : (f.size == null ? '…' : fmtBytes(f.size))));
            if (editing) {
                const x = el('button', 'btn', '✕');
                x.title = 'Remove from list';
                x.addEventListener('click', ev => { ev.stopPropagation(); folderAction('remove', f.entry); });
                line.append(x);
            }
            wrap.append(line, bar(f.size == null ? 0 : 100 * f.size / d.folder_warn, 50, 100));
            wrap.addEventListener('click', () => cdTo(f));
            return wrap;
        });
        nodes.push(el('div', 'dim', (d.folders_at ? 'Scanned: ' + hhmmss(d.folders_at) + ' · ' : '') +
                        'Warning at ' + fmtBytes(d.folder_warn) + ' · click to cd in active tab'));
        fill('folders', nodes);
    }
    sections.folders = {
        exportRows: () => ({ header: ['Entry', 'Path', 'Bytes'],
                             rows: lastFolders.map(f => [f.entry, f.path, f.size == null ? '' : f.size]) }),
    };

    const focusNote = v => 'Focus active: showing only “' + v + '” and child processes (watched folders are not filtered)';
    function onFocusChange() {
        const v = $('focus-input').value.trim();
        $('focus-input').classList.toggle('active', !!v);
        $('focus-note').textContent = v ? focusNote(v) : '';
        try { localStorage.setItem('sysmon.focus', v); } catch (e) {}
        procRefresh(true);
        netRefresh(true);
        portsRefresh(true);
        fetchFileOps();
    }
    function setFocus(v) {
        $('focus-input').value = v;
        onFocusChange();
    }
    let focusTimer = null;
    $('focus-input').addEventListener('input', () => {
        clearTimeout(focusTimer);
        focusTimer = setTimeout(onFocusChange, 300);
    });
    $('focus-clear').addEventListener('click', () => setFocus(''));
    try { $('focus-input').value = localStorage.getItem('sysmon.focus') || ''; } catch (e) {}
    if ($('focus-input').value) {
        $('focus-input').classList.add('active');
        $('focus-note').textContent = focusNote($('focus-input').value);
    }

    async function update() {
        let d;
        try { d = await getJSON('/api/stats'); } catch (e) { return; }
        if (!paused.sys) {
            if (d.cpu) {
                $('cpu-val').textContent = d.cpu.total.toFixed(1) + '%';
                setFill($('cpu-bar'), d.cpu.total);
                renderCores(d.cpu, d.gpu);
            }
            if (d.mem && d.mem.total) {
                const m = d.mem;
                $('ram-val').textContent = fmtBytes(m.used) + ' / ' + fmtBytes(m.total);
                setFill($('ram-bar'), 100 * m.used / m.total, 75, 90);
                fill('ram-pop', [
                    el('div', 'sub', 'Breakdown'),
                    row('App', fmtBytes(m.app)), row('Wired', fmtBytes(m.wired)),
                    row('Compressed', fmtBytes(m.compressed)), row('Cache (reclaimable)', fmtBytes(m.cached)),
                    row('Swap', fmtBytes(m.swap)),
                    el('div', 'dim', 'Click to sort processes by RAM'),
                ]);
            }
            procRefresh();
        }
        if (d.net && !paused.net) {
            const n = d.net;
            updateNetInterfaces(d);
            $('net-sum').textContent = '↓ ' + fmtRate(n.rx_rate) + ' ↑ ' + fmtRate(n.tx_rate) + (n.ready ? '' : ' · ⏳');
            netWarm = { ready: n.ready, up: n.up };
            updateNetNote();
            fill('net-pop', [
                row('Total since boot', '↓ ' + fmtBytes(n.rx_total) + ' ↑ ' + fmtBytes(n.tx_total)),
                el('div', 'sub', 'Top processes'),
                ...(n.top.length ? n.top.map(p => row(p.name, '↓ ' + fmtRate(p.rx) + ' ↑ ' + fmtRate(p.tx), 'PID ' + p.pid))
                                 : [el('div', 'dim', 'No traffic')]),
            ]);
        }
        fetchFileOps();
        renderFolders(d);
    }
    for (const key of Object.keys(sections)) setOpen(key, !!openState[key]);
    setInterval(update, 2000);
    setInterval(netRefresh, 5000);
    setInterval(portsRefresh, 5000);
    update();
    portsRefresh();
</script>
</body>
</html>
"""


class HTMLHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away or aborted the fetch

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), code=code)

    def _host_ok(self):
        # Blocks DNS-rebinding: other sites must not read process lists / file paths
        if self.headers.get("Host") in ALLOWED_HOSTS:
            return True
        self.send_error(403)
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        arg = lambda name, default: qs.get(name, [default])[0]
        fs = focus_set(arg("focus", "")[:100])
        if url.path == "/api/stats":
            with STATE_LOCK:
                stats = dict(STATE)
            stats["folder_warn"] = FOLDER_WARN_BYTES
            stats["interfaces"] = network_ifaces()
            self._json(stats)
        elif url.path == "/api/fileops":
            ops = {o for o in arg("ops", ",".join(DEFAULT_OPS)).split(",") if o in FILEOP_TYPES}
            self._json(query_fileops(arg("q", "")[:200], ops, fs))
        elif url.path == "/api/net":
            rng = int(arg("range", "300")) if arg("range", "300").isdigit() else 300
            self._json(net_summary(rng if rng in NET_RANGES else 300, fs))
        elif url.path == "/api/ports":
            self._json({"rows": [r for r in read_ports(arg("udp", "0") == "1") if fs is None or r["pid"] in fs["pids"]]})
        elif url.path == "/api/conns":
            self._json(read_conns(arg("name", "")[:100], fs))
        elif url.path in ("/api/procs", "/api/proc"):
            try:
                rng = int(arg("range", "0"))
                pid = int(arg("pid", "-1"))
            except ValueError:
                self.send_error(400)
                return
            rng = rng if rng in PROC_RANGES else 0
            if url.path == "/api/proc":
                self._json(proc_detail(pid, rng))
            else:
                sort = arg("sort", "cpu") if arg("sort", "cpu") in ("name", "cpu", "mem") else "cpu"
                cat = arg("cat", "all") if arg("cat", "all") in ("all", "app", "user", "system") else "all"
                self._json(proc_summary(sort, arg("dir", "desc") != "asc", rng, arg("q", "")[:100], fs,
                                        cat, arg("group", "0") == "1"))
        else:
            self._send(HTML, "text/html; charset=utf-8")

    def do_POST(self):
        if not self._host_ok():
            return
        # Custom header forces a CORS preflight (never answered), so other websites cannot POST here
        if self.headers.get("X-Sysmon") != "1":
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 5_000_000:
                raise ValueError("body too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected object")
        except ValueError:
            self.send_error(400)
            return
        path = urlparse(self.path).path
        if path == "/api/interface":
            iface = body.get("iface", "")
            if not isinstance(iface, str) or (iface and iface not in network_ifaces()):
                self._json({"error": "Invalid network interface"}, 400)
                return
            with STATE_LOCK:
                STATE["iface_selected"] = iface or None
            self._json({"ok": True})
        elif path == "/api/cd":
            entry = body.get("entry")
            if entry not in load_watched():
                self._json({"error": "Unknown folder"}, 400)
                return
            target = _expand(entry)
            self._json({"ok": iterm_cd(target), "cmd": "cd " + shlex.quote(target)})
        elif path == "/api/folders":
            action, entry = body.get("action"), str(body.get("entry", ""))[:1000]
            if action == "rescan":
                RESCAN.set()
                self._json({"ok": True})
            elif action in ("add", "remove"):
                self._json(edit_watched(action, entry))
            else:
                self.send_error(400)
        elif path == "/api/export":
            try:
                self._json({"path": export_csv(body.get("section"), body.get("header"), body.get("rows"))})
            except (ValueError, OSError) as e:
                self._json({"error": str(e)}, 400)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass

def start_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), HTMLHandler) as httpd:
        httpd.daemon_threads = True
        httpd.serve_forever()

async def main(connection):
    ITERM["conn"], ITERM["loop"] = connection, asyncio.get_running_loop()
    threading.Thread(target=start_server, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=nettop_stream, daemon=True).start()
    threading.Thread(target=folder_scanner, daemon=True).start()
    threading.Thread(target=fileops_monitor, daemon=True).start()

    await iterm2.tool.async_register_web_view_tool(
        connection,
        "System Monitor",
        "com.example.system-monitor",
        True,
        f"http://127.0.0.1:{PORT}/"
    )

iterm2.run_forever(main)