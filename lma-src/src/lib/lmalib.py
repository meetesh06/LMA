"""Shared helpers for LMA (Leave Me Alone).

Contains:
  * cpulist parsing / formatting ("0-7,64-71" <-> {0..7, 64..71})
  * CPU / socket / SMT / GPU topology discovery from sysfs
  * runtime config + allocation database handling
  * a small dependency-free terminal UI (arrow-key menus, colors, core map)
"""
import contextlib
import csv
import fcntl
import json
import os
import pwd
import re
import select
import shutil
import signal
import sys
import tempfile

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - non-POSIX
    termios = None
    tty = None

LMA_VERSION = "1.3.0"          # keep in sync with debian/changelog
LMA_DATA_VERSION = "2.0"
SUPPORTED_DATA_VERSIONS = ("1.0", "2.0")

LIB_DIR = "/usr/lib/lma"
LMA_DATA_PATH = f"{LIB_DIR}/lmaData"
DATABASE_FILE = f"{LIB_DIR}/lmaAllocations.csv"
AHOOK_SCRIPT_PATH = f"{LIB_DIR}/lma-aHook.sh"
DHOOK_SCRIPT_PATH = f"{LIB_DIR}/lma-dHook.sh"
LOCK_FILE = "/run/lma.lock"
CONFIG_DIR = "/etc/lma"
DEFAULT_CONFIG_PATH = f"{CONFIG_DIR}/config.json"

SYS_CPU = "/sys/devices/system/cpu"
SYS_PCI = "/sys/bus/pci/devices"
SYS_NODE = "/sys/devices/system/node"

ALLOCATION_UNITS = ("logical", "physical")
PLACEMENTS = ("compact", "spread")
MEMORY_BINDINGS = ("off", "optional", "local-by-default")
MEM_MODES = ("local", "any")


class LmaError(Exception):
    """An error that should be shown to the user without a traceback."""


class NotConfigured(LmaError):
    pass


# --------------------------------------------------------------------------
# cpulists
# --------------------------------------------------------------------------

def parse_cpulist(value):
    """Parse a Linux cpulist ("0-3,8,10-11") into a set of ints.

    Also accepts the legacy LMA 1.0 representation ``[start, end]``.
    """
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(v, int) for v in value):
            start, end = value
            if end < start:
                raise ValueError(f"invalid CPU range {value}")
            return set(range(start, end + 1))
        raise ValueError(f"invalid CPU list {value!r}")
    if not isinstance(value, str):
        raise ValueError(f"CPU list must be a string like '0-63', got {value!r}")
    cpus = set()
    for part in value.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"invalid CPU list element '{part}' in '{value}'")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        if end < start:
            raise ValueError(f"invalid CPU range '{part}' in '{value}'")
        cpus.update(range(start, end + 1))
    return cpus


def format_cpulist(cpus):
    """Format an iterable of ints as a compact cpulist string."""
    cpus = sorted(set(cpus))
    if not cpus:
        return ""
    runs = []
    start = prev = cpus[0]
    for c in cpus[1:]:
        if c == prev + 1:
            prev = c
            continue
        runs.append((start, prev))
        start = prev = c
    runs.append((start, prev))
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


# --------------------------------------------------------------------------
# Topology
# --------------------------------------------------------------------------

_PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}
# BMC / management display controllers, not interesting as accelerators.
_IGNORED_GPU_VENDORS = {"0x1a03", "0x102b"}


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


class Gpu:
    def __init__(self, bdf, vendor, socket):
        self.bdf = bdf
        self.vendor = vendor
        self.socket = socket

    @property
    def short(self):
        bdf = self.bdf[5:] if self.bdf.startswith("0000:") else self.bdf
        return f"{self.vendor} GPU {bdf}"


class Topology:
    """CPU topology: sockets, physical cores (SMT sibling groups) and GPUs."""

    def __init__(self, socket_of, siblings, gpus, l3_of=None, l3_kb=None):
        self.socket_of = socket_of            # cpu -> socket id
        self.siblings = siblings              # cpu -> tuple of thread siblings
        # cpu -> L3 domain key (lowest CPU sharing its L3). On AMD Zen 3/4 an L3
        # domain is one CCD. Without cache info, the whole socket is one domain.
        self.l3_of = l3_of or {c: -1 - s for c, s in socket_of.items()}
        self.l3_kb = l3_kb or {}              # L3 domain key -> size in KiB (if known)
        self.cpus = sorted(socket_of)
        self.sockets = sorted(set(socket_of.values()))
        self.node_of, self.node_distance = self._detect_nodes(set(self.cpus))
        self.gpus = gpus
        cores = {}
        for cpu in self.cpus:
            core = siblings[cpu]
            cores.setdefault(socket_of[core[0]], set()).add(core)
        # Cores of each socket, ordered by their first thread id.
        self._cores = {s: sorted(c, key=lambda t: t[0]) for s, c in cores.items()}
        self._ccds = {}
        for s, socket_cores in self._cores.items():
            groups = {}
            for core in socket_cores:
                groups.setdefault(self.l3_of[core[0]], []).append(core)
            self._ccds[s] = sorted(groups.values(), key=lambda g: g[0][0])
        # cpu -> CCD index within its socket (0, 1, ...)
        self.ccd_index = {c: i for s in self._ccds for i, g in enumerate(self._ccds[s])
                          for core in g for c in core}

    @classmethod
    def detect(cls):
        online = _read(f"{SYS_CPU}/online")
        cpus = sorted(parse_cpulist(online)) if online else list(range(os.cpu_count() or 1))
        socket_of, siblings = {}, {}
        for cpu in cpus:
            base = f"{SYS_CPU}/cpu{cpu}/topology"
            pkg = _read(f"{base}/physical_package_id")
            socket_of[cpu] = int(pkg) if pkg is not None and pkg.lstrip("-").isdigit() and int(pkg) >= 0 else 0
            sib = _read(f"{base}/thread_siblings_list")
            group = sorted(parse_cpulist(sib) & set(cpus)) if sib else [cpu]
            siblings[cpu] = tuple(group or [cpu])
        l3_of, l3_kb = cls._detect_l3(cpus)
        return cls(socket_of, siblings, cls._detect_gpus(socket_of), l3_of, l3_kb)

    @staticmethod
    def _detect_l3(cpus):
        """Returns ({cpu: L3 key}, {L3 key: KiB}), or (None, None) without cache info."""
        l3_of, l3_kb = {}, {}
        online = set(cpus)
        for cpu in cpus:
            base = f"{SYS_CPU}/cpu{cpu}/cache"
            try:
                indexes = sorted(d for d in os.listdir(base) if d.startswith("index"))
            except OSError:
                return None, None
            for idx in indexes:
                if _read(f"{base}/{idx}/level") == "3":
                    shared = _read(f"{base}/{idx}/shared_cpu_list")
                    try:
                        group = parse_cpulist(shared) & online if shared else set()
                    except ValueError:
                        group = set()
                    l3_of[cpu] = min(group) if group else cpu
                    size = _read(f"{base}/{idx}/size", "")
                    m = re.fullmatch(r"(\d+)([KMG]?)", size)
                    if m:
                        l3_kb[l3_of[cpu]] = int(m.group(1)) * {"": 1, "K": 1, "M": 1024, "G": 1 << 20}[m.group(2)]
                    break
            else:
                return None, None
        return l3_of, l3_kb

    @staticmethod
    def _detect_gpus(socket_of):
        gpus = []
        try:
            devices = sorted(os.listdir(SYS_PCI))
        except OSError:
            return gpus
        for dev in devices:
            path = os.path.join(SYS_PCI, dev)
            cls_ = _read(f"{path}/class", "")
            if not (cls_.startswith("0x0300") or cls_.startswith("0x0302")):
                continue
            vendor = _read(f"{path}/vendor", "")
            if vendor in _IGNORED_GPU_VENDORS:
                continue
            socket = None
            local = _read(f"{path}/local_cpulist")
            if local:
                try:
                    local_cpus = [c for c in sorted(parse_cpulist(local)) if c in socket_of]
                    # local_cpulist covering every CPU means "unknown" (no NUMA info).
                    if local_cpus and len(local_cpus) < len(socket_of):
                        socket = socket_of[local_cpus[0]]
                except ValueError:
                    pass
            gpus.append(Gpu(dev, _PCI_VENDORS.get(vendor, f"PCI {vendor}"), socket))
        return gpus

    def cores(self, socket):
        return self._cores.get(socket, [])

    def ccds(self, socket):
        """Physical cores of a socket grouped by L3 domain (CCD), in CPU order."""
        return self._ccds.get(socket, [])

    def cpus_in_socket(self, socket):
        return [c for c in self.cpus if self.socket_of[c] == socket]

    @staticmethod
    def _detect_nodes(online):
        """Returns ({cpu: NUMA node}, {node: {node: distance}}); empty without NUMA info."""
        node_of, distance = {}, {}
        try:
            names = [d for d in os.listdir(SYS_NODE) if re.fullmatch(r"node\d+", d)]
        except OSError:
            return {}, {}
        for name in names:
            node = int(name[4:])
            try:
                cpus = parse_cpulist(_read(f"{SYS_NODE}/{name}/cpulist", ""))
            except ValueError:
                continue
            for c in cpus & online:
                node_of[c] = node
            dist = (_read(f"{SYS_NODE}/{name}/distance", "") or "").split()
            if dist:
                distance[node] = {i: int(d) for i, d in enumerate(dist) if d.isdigit()}
        return node_of, distance

    def nodes_for(self, cpus):
        return sorted({self.node_of[c] for c in cpus if c in self.node_of})

    @property
    def numa_nodes(self):
        return sorted(set(self.node_of.values()))

    @staticmethod
    def node_memory_kb(node):
        """(total, free) KiB of a NUMA node, read live; (None, None) if unknown."""
        info = {}
        for line in (_read(f"{SYS_NODE}/node{node}/meminfo", "") or "").splitlines():
            parts = line.split()            # "Node 1 MemFree:  12345 kB"
            if len(parts) == 5 and parts[4] == "kB" and parts[3].isdigit():
                info[parts[2].rstrip(":")] = int(parts[3])
        return info.get("MemTotal"), info.get("MemFree")

    def ccd_l3_kb(self, socket, ccd):
        """L3 size (KiB) of one CCD of a socket, or None if unknown."""
        groups = self.ccds(socket)
        if not 0 <= ccd < len(groups):
            return None
        return self.l3_kb.get(self.l3_of[groups[ccd][0][0]])

    def gpus_on(self, socket):
        return [g for g in self.gpus if g.socket == socket]

    @property
    def threads_per_core(self):
        return max((len(s) for s in self.siblings.values()), default=1)

    def summary(self):
        cores = sum(len(self.cores(s)) for s in self.sockets)
        return (f"{len(self.sockets)} socket(s) · {cores} physical cores · "
                f"{self.threads_per_core} thread(s)/core · {len(self.cpus)} CPUs")


# --------------------------------------------------------------------------
# Runtime config (written by lma-setup, read by lma)
# --------------------------------------------------------------------------

class RuntimeConfig:
    def __init__(self, data):
        cpu = data["cpu"]
        self.total = parse_cpulist(cpu["total"])
        self.shared = parse_cpulist(cpu["shared"])
        self.isolated = parse_cpulist(cpu["isolated"])
        self.sizes = sorted({int(s) for s in data["coreGroupSizes"] if int(s) > 0})
        self.unit = data.get("allocationUnit", "logical")
        if self.unit not in ALLOCATION_UNITS:
            raise LmaError(f"invalid allocationUnit '{self.unit}' in {LMA_DATA_PATH}")
        self.memory_binding = data.get("memoryBinding", "optional")
        if self.memory_binding not in MEMORY_BINDINGS:
            raise LmaError(f"invalid memoryBinding '{self.memory_binding}' in {LMA_DATA_PATH}")
        self.placement = data.get("placement", "compact")
        if self.placement not in PLACEMENTS:
            raise LmaError(f"invalid placement '{self.placement}' in {LMA_DATA_PATH}")


def load_runtime_config():
    try:
        with open(LMA_DATA_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise NotConfigured("LMA is not configured yet. An administrator must run: sudo lma-setup")
    except (OSError, json.JSONDecodeError) as e:
        raise NotConfigured(f"Could not read {LMA_DATA_PATH} ({e}). Re-run: sudo lma-setup")
    if data.get("version") not in SUPPORTED_DATA_VERSIONS:
        raise NotConfigured(f"LMA data version {data.get('version')!r} is not supported. Re-run: sudo lma-setup")
    try:
        return RuntimeConfig(data)
    except (KeyError, TypeError, ValueError) as e:
        raise NotConfigured(f"{LMA_DATA_PATH} is malformed ({e}). Re-run: sudo lma-setup")


# --------------------------------------------------------------------------
# Allocation database
# --------------------------------------------------------------------------

class Allocation:
    def __init__(self, uid, cpus, socket, mems=frozenset()):
        self.uid = uid
        self.cpus = frozenset(cpus)
        self.socket = socket
        self.mems = frozenset(mems)          # NUMA nodes memory is bound to; empty = unbound

    @property
    def user_name(self):
        return user_name(self.uid)


def user_name(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def load_allocations(topo):
    if not os.path.exists(DATABASE_FILE):
        return []
    allocations = []
    with open(DATABASE_FILE, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    legacy = header[:3] == ["id", "start_core", "end_core"]
    for row in body:
        try:
            uid = int(row[0])
            if legacy:
                cpus = set(range(int(row[1]), int(row[2]) + 1))
            else:
                cpus = parse_cpulist(row[1])
            sockets = {topo.socket_of.get(c, -1) for c in cpus}
            socket = sockets.pop() if len(sockets) == 1 else -1
            mems = parse_cpulist(row[3]) if not legacy and len(row) > 3 and row[3] else set()
            allocations.append(Allocation(uid, cpus, socket, mems))
        except (ValueError, IndexError) as e:
            print(warn(f"Skipping invalid row in {DATABASE_FILE}: {row} ({e})"))
    return allocations


def save_allocations(allocations):
    """Atomically rewrite the allocation database."""
    directory = os.path.dirname(DATABASE_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".lmaAllocations.")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "cpus", "socket", "mems"])
            for a in sorted(allocations, key=lambda a: min(a.cpus)):
                writer.writerow([a.uid, format_cpulist(a.cpus), a.socket, format_cpulist(a.mems)])
        os.chmod(tmp, 0o644)
        os.replace(tmp, DATABASE_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def lma_lock():
    """Global exclusive lock shared by lma and lma-setup (blocks until free)."""
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(dim("Waiting for another LMA operation to finish..."))
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def no_interrupt():
    """Ignore Ctrl-C while committing changes (also inherited by hooks)."""
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old)


# --------------------------------------------------------------------------
# Terminal styling
# --------------------------------------------------------------------------

_color = (sys.stdout.isatty() and "NO_COLOR" not in os.environ
          and os.environ.get("TERM", "") != "dumb")
_unicode = (sys.stdout.encoding or "").lower().replace("-", "") in ("utf8",)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def set_color(enabled):
    global _color
    _color = enabled


def _sgr(code, text):
    return f"\x1b[{code}m{text}\x1b[0m" if _color else str(text)


def bold(t): return _sgr("1", t)
def dim(t): return _sgr("2", t)
def red(t): return _sgr("31", t)
def green(t): return _sgr("32", t)
def yellow(t): return _sgr("33", t)
def blue(t): return _sgr("34", t)
def magenta(t): return _sgr("35", t)
def cyan(t): return _sgr("36", t)
def warn(t): return yellow(("⚠ " if _unicode else "! ") + str(t))
def err(t): return red(("✖ " if _unicode else "x ") + str(t))
def ok(t): return green(("✔ " if _unicode else "* ") + str(t))


def visible_len(s):
    return len(_ANSI_RE.sub("", s))


def term_width():
    return max(40, min(shutil.get_terminal_size((100, 24)).columns, 120))


def box(lines, color=cyan):
    """Draw a rounded box around lines (which may contain ANSI codes)."""
    width = max(visible_len(l) for l in lines) + 2
    tl, tr, bl, br, h, v = ("╭", "╮", "╰", "╯", "─", "│") if _unicode else ("+", "+", "+", "+", "-", "|")
    out = [color(tl + h * width + tr)]
    for l in lines:
        out.append(color(v) + " " + l + " " * (width - 1 - visible_len(l)) + color(v))
    out.append(color(bl + h * width + br))
    return "\n".join(out)


def banner(subtitle):
    title = bold("LMA") + dim(f" {LMA_VERSION} · Leave Me Alone")
    print()
    print(box([title, dim(subtitle)]))


def interactive():
    return termios is not None and sys.stdin.isatty() and sys.stdout.isatty()


# --------------------------------------------------------------------------
# Menus and prompts
# --------------------------------------------------------------------------

class Option:
    def __init__(self, label, detail="", disabled=False):
        self.label = label
        self.detail = detail
        self.disabled = disabled


def _read_key(fd):
    ch = os.read(fd, 1)
    if ch == b"\x1b":
        seq = b""
        while len(seq) < 2 and select.select([fd], [], [], 0.05)[0]:
            seq += os.read(fd, 1)
        return {b"[A": "up", b"OA": "up", b"[B": "down", b"OB": "down", b"": "esc"}.get(seq)
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b"\x04":
        raise EOFError
    return ch.decode(errors="ignore")


def _option_line(i, opt, selected, width):
    pointer = ("❯" if _unicode else ">") if selected else " "
    num = f"{i + 1}." if i < 9 else "  "
    label = opt.label
    detail = opt.detail
    room = width - 8 - len(label)
    if detail and room < 4:
        detail = ""
    elif detail and len(detail) > room:
        detail = detail[: room - 1] + ("…" if _unicode else ".")
    if opt.disabled:
        return f"  {dim(num)} {dim(label)}  {dim(detail)}"
    if selected:
        return f"{cyan(pointer)} {cyan(num)} {bold(cyan(label))}  {detail and dim(detail)}"
    return f"  {dim(num)} {label}  {detail and dim(detail)}"


def clip(line, width):
    """Cut a line (which may contain ANSI codes) to ``width`` visible characters."""
    if visible_len(line) <= width:
        return line
    out, seen, i = [], 0, 0
    while i < len(line) and seen < width - 1:
        m = _ANSI_RE.match(line, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(line[i])
        seen += 1
        i += 1
    return "".join(out) + ("…" if _unicode else ".") + ("\x1b[0m" if _color else "")


def menu(title, options, default=0, preview=None):
    """Show a menu and return the selected index, or None if cancelled.

    ``preview(index)`` may return extra lines describing the highlighted option;
    they are redrawn live under the menu as the cursor moves.
    """
    enabled = [i for i, o in enumerate(options) if not o.disabled]
    if not enabled:
        return None
    if default not in enabled:
        default = enabled[0]
    print()
    print(bold(title))
    if not interactive():
        choice = _menu_plain(options, default)
        if choice is not None and preview:
            for line in preview(choice):
                print(line)
        return choice

    out = sys.stdout
    fd = sys.stdin.fileno()
    width = term_width()
    columns = shutil.get_terminal_size((100, 24)).columns
    hint = dim("  ↑/↓ or j/k move · 1-9 jump · enter select · q cancel" if _unicode
               else "  up/down or j/k move, 1-9 jump, enter select, q cancel")
    idx = default
    drawn = 0

    def draw():
        nonlocal drawn
        lines = [_option_line(i, o, i == idx, width) for i, o in enumerate(options)] + [hint]
        if preview:
            lines += preview(idx)
        if drawn:
            out.write(f"\x1b[{drawn}A")
        out.write("\r\x1b[J")
        for line in lines:
            out.write(clip(line, columns - 1) + "\n")
        out.flush()
        drawn = len(lines)

    old = termios.tcgetattr(fd)
    result = None
    try:
        out.write("\x1b[?25l")
        tty.setcbreak(fd)
        draw()
        while True:
            key = _read_key(fd)
            if key in ("up", "k"):
                idx = next((i for i in reversed(enabled) if i < idx), enabled[-1])
            elif key in ("down", "j"):
                idx = next((i for i in enabled if i > idx), enabled[0])
            elif key and key.isdigit() and key != "0" and int(key) - 1 in enabled:
                idx = int(key) - 1
                draw()
                result = idx
                break
            elif key == "enter":
                result = idx
                break
            elif key in ("q", "esc"):
                break
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # Collapse the menu (and its preview) into a one-line answer.
        out.write(f"\x1b[{drawn}A\x1b[J\x1b[?25h")
        out.flush()
    if result is None:
        print(dim("  (cancelled)"))
    else:
        print(f"  {green('✔' if _unicode else '*')} {options[result].label}")
    return result


def _menu_plain(options, default):
    for i, o in enumerate(options):
        suffix = f"  {o.detail}" if o.detail else ""
        mark = " (unavailable)" if o.disabled else ""
        print(f"  {i + 1}) {o.label}{suffix}{mark}")
    while True:
        try:
            raw = input(f"Choice [{default + 1}, q to cancel]: ").strip().lower()
        except EOFError:
            return None
        if raw == "":
            return default
        if raw in ("q", "quit"):
            return None
        if raw.isdigit() and 0 < int(raw) <= len(options) and not options[int(raw) - 1].disabled:
            return int(raw) - 1
        print(err("Invalid choice, try again."))


def ask_text(question, default=None, validate=None):
    """Ask for free text. ``validate`` returns the parsed value or raises ValueError."""
    print()
    while True:
        suffix = f" {dim('[' + default + ']')}" if default else ""
        raw = input(f"{bold(question)}{suffix}: ").strip()
        if not raw and default is not None:
            raw = default
        try:
            return validate(raw) if validate else raw
        except ValueError as e:
            print(err(e))


def confirm(question, default=True):
    choice = menu(question, [Option("Yes"), Option("No")], default=0 if default else 1)
    return choice == 0


# --------------------------------------------------------------------------
# Core map
# --------------------------------------------------------------------------

_USER_COLORS = [yellow, magenta, blue, red, lambda t: _sgr("93", t), lambda t: _sgr("95", t)]


def render_core_map(topo, cfg, allocations, me=None, preview=frozenset(), sockets=None):
    """Return lines visualising every CPU of each socket, one row per SMT thread."""
    owner = {}
    for a in allocations:
        for c in a.cpus:
            owner[c] = a.uid
    others = sorted({a.uid for a in allocations if a.uid != me})
    user_style = {uid: (_USER_COLORS[i % len(_USER_COLORS)], chr(ord("A") + i % 26))
                  for i, uid in enumerate(others)}

    def blocked(cpu):
        # In physical mode a free thread whose sibling is shared can never be handed out.
        return cfg.unit == "physical" and any(s not in cfg.isolated for s in topo.siblings[cpu])

    used = set()

    def cell(cpu):
        if cpu in preview:
            used.add("preview")
            return bold(yellow("▣" if _unicode else "+"))
        if cpu in owner:
            uid = owner[cpu]
            if uid == me:
                used.add("mine")
                return bold(cyan("■" if _unicode else "@"))
            color, letter = user_style[uid]
            used.add("other")
            return color("■" if (_unicode and _color) else letter)
        if cpu in cfg.isolated:
            if blocked(cpu):
                used.add("blocked")
                return dim(red("□" if _unicode else "x"))
            used.add("free")
            return green("□" if _unicode else "o")
        used.add("shared")
        return dim("·" if _unicode else ".")

    width = term_width()
    lines = []
    for s in (sockets if sockets is not None else topo.sockets):
        cores = topo.cores(s)
        if not cores:
            continue
        tpc = max(len(c) for c in cores)
        rows = [[core[t] if t < len(core) else None for core in cores] for t in range(tpc)]
        labels = [format_cpulist(c for c in row if c is not None) for row in rows]
        label_w = max(len(l) for l in labels)
        per_line = len(cores)
        n_ccds = len(topo.ccds(s))
        while per_line > 8 and 4 + label_w + 2 + per_line + n_ccds > width:
            per_line //= 2

        iso = [c for c in topo.cpus_in_socket(s) if c in cfg.isolated]
        free = [c for c in iso if c not in owner and not blocked(c)]
        header = f"{bold('Socket ' + str(s))}"
        gpus = topo.gpus_on(s)
        if gpus:
            header += "  " + "  ".join(magenta(("◆ " if _unicode else "") + g.short) for g in gpus)
        right = dim(f"{len(free)}/{len(iso)} isolated CPUs free")
        pad = max(2, width - visible_len(header) - visible_len(right) - 2)
        lines.append("")
        lines.append(" " + header + " " * pad + right)
        for chunk in range(0, len(cores), per_line):
            part_cores = cores[chunk:chunk + per_line]
            # CCD header row: index printed at the start of each CCD group.
            if n_ccds > 1:
                head, prev = "", None
                for i, core in enumerate(part_cores):
                    ccd = topo.ccd_index[core[0]]
                    if i and ccd != prev:
                        head += " "
                    head += str(ccd)[-1] if ccd != prev else " "
                    prev = ccd
                lines.append(f"   {dim('CCD'.rjust(label_w))}  {dim(head)}")
            for t, row in enumerate(rows):
                part = row[chunk:chunk + per_line]
                label = format_cpulist(c for c in part if c is not None)
                cells = ""
                for i, cpu in enumerate(part):
                    if i and topo.ccd_index[part_cores[i][0]] != topo.ccd_index[part_cores[i - 1][0]]:
                        cells += " "
                    cells += " " if cpu is None else cell(cpu)
                lines.append(f"   {dim(label.rjust(label_w if per_line == len(cores) else len(label)))}  {cells}")

    legend = []
    names = {
        "free": green("□" if _unicode else "o") + " free",
        "blocked": dim(red("□" if _unicode else "x")) + " sibling is shared (unusable)",
        "shared": dim("·" if _unicode else ".") + " shared/OS",
        "mine": bold(cyan("■" if _unicode else "@")) + " yours",
        "preview": bold(yellow("▣" if _unicode else "+")) + " selected",
    }
    for key in ("free", "mine", "preview", "shared", "blocked"):
        if key in used:
            legend.append(names[key])
    for uid in others:
        color, letter = user_style[uid]
        legend.append(color("■" if (_unicode and _color) else letter) + f" {user_name(uid)}")
    if legend:
        lines.append("")
        lines.append("   " + dim("  ").join(legend))
    return lines


def meter(level, total=8):
    """A small bar, e.g. ■■■■□□□□ for level 4 of 8."""
    level = max(0, min(total, level))
    full, empty = ("■", "□") if _unicode else ("#", ".")
    return cyan(full * level) + dim(empty * (total - level))


def render_ccd_strip(topo, cfg, allocations, socket, selected=frozenset(), me=None, selected_label="this choice"):
    """One compact row per socket: each CCD as a group of physical-core cells."""
    owner = {c: a.uid for a in allocations for c in a.cpus}
    groups = topo.ccds(socket)
    if not groups:
        return []

    half = False

    def cell(core):
        nonlocal half
        if all(c in selected for c in core):
            return bold(yellow("▣" if _unicode else "+"))
        if any(c in selected for c in core):
            half = True   # logical mode: only some hyperthreads of this core are in the choice
            return bold(yellow("◧" if _unicode else "/"))
        if not all(c in cfg.isolated for c in core):
            return dim("·" if _unicode else ".")
        users = {owner[c] for c in core if c in owner}
        if users:
            return bold(cyan("■" if _unicode else "@")) if users == {me} else magenta("■" if _unicode else "x")
        return green("□" if _unicode else "o")

    width = term_width() - 4
    lines, row_head, row_cells, used = [], "", "", 0
    for i, group in enumerate(groups):
        block = "".join(cell(core) for core in group)
        label = f"CCD {i}"
        w = max(len(group), len(label)) + 2
        if used and used + w > width:
            lines += ["   " + dim(row_head), "   " + row_cells]
            row_head, row_cells, used = "", "", 0
        row_head += label.ljust(w)
        row_cells += block + " " * (w - len(group))
        used += w
    lines += ["   " + dim(row_head), "   " + row_cells]
    legend = [bold(yellow("▣" if _unicode else "+")) + f" {selected_label}", green("□" if _unicode else "o") + " free",
              magenta("■" if _unicode else "x") + " other users", dim("·" if _unicode else ".") + " OS"]
    if half:
        legend.insert(1, bold(yellow("◧" if _unicode else "/")) + " one thread of the core")
    lines.append("   " + dim("  ").join(legend))
    return lines
