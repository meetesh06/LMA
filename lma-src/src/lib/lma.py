#!/usr/bin/env python3
"""LMA allocator: hands out isolated CPU cores to the invoking user.

Invoked by /usr/sbin/lma as:
  lma.py <uid> [--socket N] [--size N] [--spread | --compact | --ccds N] [--mem local|any]
         [--release] [--status]
"""
import argparse
import os
import subprocess
import sys

# Run with 'python3 -I', which drops the script dir from sys.path; /usr/lib/lma is root-owned.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import lmalib as L  # noqa: E402


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------

def _universe(topo, cfg, socket):
    """Ordered allocation units of a socket. A unit is one CPU (logical mode)
    or all SMT threads of one physical core (physical mode)."""
    if cfg.unit == "physical":
        return [core for core in topo.cores(socket) if all(c in cfg.isolated for c in core)]
    return [(c,) for c in topo.cpus_in_socket(socket) if c in cfg.isolated]


class Placement:
    def __init__(self, cpus, contiguous, ccds, shared_with):
        self.cpus = frozenset(cpus)
        self.contiguous = contiguous
        self.ccds = sorted(ccds)              # CCD indices (within the socket) covered
        self.shared_with = shared_with        # uids of other users already on those CCDs

    @property
    def n_ccds(self):
        return len(self.ccds)


def _taken_by(allocations):
    return {c: a.uid for a in allocations for c in a.cpus}


def _make(topo, cpus, contiguous, owners):
    ccds = {topo.ccd_index[c] for c in cpus}
    socket = topo.socket_of[next(iter(cpus))]
    others = {owners[c] for g in ccds for core in topo.ccds(socket)[g] for c in core if c in owners}
    return Placement(cpus, contiguous, ccds, others)


def place(topo, cfg, allocations, socket, size, ccds=1):
    """Pick ``size`` free units on ``socket``; returns a Placement or None.

    ccds == 1 (compact): a block aligned to its own size (keeps L3/CCD locality and
    limits fragmentation), then any contiguous block, then any free units.

    ccds > 1 (spread, physical mode only): size/ccds whole cores on each of ``ccds``
    CCDs, for more memory bandwidth (each CCD has its own link to memory). CCDs
    not used by anyone else are preferred, then lowest index.
    """
    owners = _taken_by(allocations)
    if size <= 0 or ccds <= 0:
        return None
    if ccds == 1:
        universe = _universe(topo, cfg, socket)
        free = [not any(c in owners for c in u) for u in universe]
        if sum(free) < size:
            return None
        for step in (size, 1):
            for start in range(0, len(universe) - size + 1, step):
                if all(free[start:start + size]):
                    cpus = [c for u in universe[start:start + size] for c in u]
                    return _make(topo, cpus, True, owners)
        picked = [u for u, f in zip(universe, free) if f][:size]
        return _make(topo, [c for u in picked for c in u], False, owners)

    per, rem = divmod(size, ccds)
    if rem or cfg.unit != "physical":
        return None
    candidates = []
    for i, group in enumerate(topo.ccds(socket)):
        free = [core for core in group if all(c in cfg.isolated and c not in owners for c in core)]
        if len(free) >= per:
            busy = sum(1 for core in group for c in core if c in owners)
            candidates.append((busy > 0, busy, i, free[:per]))
    if len(candidates) < ccds:
        return None
    chosen = sorted(candidates)[:ccds]
    return _make(topo, [c for *_, cores in chosen for core in cores for c in core], True, owners)


def layouts(topo, cfg, allocations, socket, size):
    """All feasible (ccds, Placement) layouts for a size, compact first."""
    result = []
    n = len(topo.ccds(socket)) if cfg.unit == "physical" else 1
    for k in range(1, min(size, n) + 1):
        if size % k == 0:
            p = place(topo, cfg, allocations, socket, size, k)
            if p is not None:
                result.append((k, p))
    return result


def default_ccds(cfg, options, size):
    """Pick the layout used when the user did not choose one.

    compact -> 1 CCD. spread -> the most CCDs with at least 2 cores on each
    (about where a Zen 3 CCD's memory link saturates), else the most CCDs possible.
    """
    ks = [k for k, _ in options]
    if not ks:
        return None
    if cfg.placement == "compact" and 1 in ks:
        return 1
    return best_spread(ks, size) or ks[0]


def best_spread(ks, size):
    spread = [k for k in ks if k > 1]
    good = [k for k in spread if size // k >= 2]
    return max(good) if good else max(spread) if spread else None


def resolve(topo, cfg, allocations, socket, size, request):
    """request: None (config default), "spread" (best spread), or an int CCD count."""
    options = layouts(topo, cfg, allocations, socket, size)
    if request is None:
        k = default_ccds(cfg, options, size)
    elif request == "spread":
        k = best_spread([k for k, _ in options], size)
    else:
        k = request if any(k == request for k, _ in options) else None
    return None if k is None else (k, dict(options)[k])


def layout_label(k, size):
    if k == 1:
        return "compact block"
    return f"spread over {k} CCDs × {size // k} core{'s' if size // k != 1 else ''}"


def workload_label(k, best_k):
    """Plain-language name of a layout, for the menu and the summary."""
    if k == 1:
        return "Cache / latency-sensitive"
    if k == best_k:
        return "Memory-bound"
    return f"Memory-bound, {k} CCDs"


def _fmt_mb(kb):
    return f"{kb // 1024} MB" if kb % 1024 == 0 else f"{kb / 1024:.1f} MB"


def placement_preview(topo, cfg, allocations, socket, p, me, selected_label="this choice"):
    """CCD strip plus plain-language implications of a placement."""
    lines = [""] + L.render_ccd_strip(topo, cfg, allocations, socket, p.cpus, me, selected_label) + [""]
    if cfg.unit != "physical" and len(topo.ccds(socket)) <= 1:
        return lines
    n = p.n_ccds
    usable = len([g for g in topo.ccds(socket) if any(all(c in cfg.isolated for c in core) for core in g)])
    per = {}
    for core in {topo.siblings[c] for c in p.cpus}:
        per[topo.ccd_index[core[0]]] = per.get(topo.ccd_index[core[0]], 0) + 1

    bw = f"{n}× " + ("one CCD's memory link" if n == 1 else "a single CCD (one memory link per CCD)")
    if min(per.values()) < 2 and n > 1:
        bw += L.dim(" · 1 core/CCD may not fill a link")
    lines.append(f"   Memory bandwidth  {L.meter(round(8 * n / max(usable, n, 1)) or 1)}  {bw}")

    if n == 1:
        level, text = 8, "high · all cores share one L3 cache"
    elif n == 2:
        level, text = 4, "medium · split over 2 L3 caches"
    else:
        level, text = 2, f"low · split over {n} L3 caches, cross-CCD traffic is slower"
    lines.append(f"   Cache locality    {L.meter(level)}  {text}")

    sizes = [topo.ccd_l3_kb(socket, i) for i in p.ccds]
    if all(sizes):
        l3 = _fmt_mb(sum(sizes)) + ("" if n == 1 else f" ({_fmt_mb(sizes[0])} × {n})")
        if p.shared_with:
            l3 += " · " + L.yellow("shared with " + ", ".join(L.user_name(u) for u in sorted(p.shared_with)))
        else:
            full = all(c in p.cpus for i in p.ccds for core in topo.ccds(socket)[i] for c in core)
            l3 += " · private" if full else L.dim(" · yours for now (free cores on these CCDs can go to others)")
        lines.append(f"   L3 cache          {l3}")
    return lines


def ccd_text(p):
    return "CCD " + L.format_cpulist(p.ccds)


def free_units(topo, cfg, allocations, socket):
    taken = set().union(*(a.cpus for a in allocations)) if allocations else set()
    return sum(1 for u in _universe(topo, cfg, socket) if not any(c in taken for c in u))


def unit_word(cfg, n):
    if cfg.unit == "physical":
        return f"{n} physical core{'s' if n != 1 else ''}"
    return f"{n} core{'s' if n != 1 else ''}"


# --------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------

def run_hook(path, label, args):
    try:
        result = subprocess.run([path, *map(str, args)], check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise L.LmaError(f"{label} hook not found at {path}")
    except subprocess.CalledProcessError as e:
        raise L.LmaError(f"{label} hook failed (exit {e.returncode}):\n{(e.stderr or e.stdout).strip()}")
    output = result.stdout.strip()
    if output and os.environ.get("LMA_VERBOSE"):
        print(L.dim(output))


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def show_map(topo, cfg, allocations, me, preview=frozenset(), sockets=None):
    for line in L.render_core_map(topo, cfg, allocations, me=me, preview=preview, sockets=sockets):
        print(line)


# --------------------------------------------------------------------------
# Memory (NUMA) binding
# --------------------------------------------------------------------------

def _gb(kb):
    return f"{kb / 1048576:.0f} GB"


def can_bind_memory(topo, cfg):
    return cfg.memory_binding != "off" and len(topo.numa_nodes) > 1


def default_mem(topo, cfg, socket, interactive):
    """Strict binding is preselected in the menu for GPU sockets, but only applied
    without asking when the admin chose local-by-default."""
    if not can_bind_memory(topo, cfg):
        return "any"
    if cfg.memory_binding == "local-by-default":
        return "local"
    if interactive and topo.gpus_on(socket):
        return "local"
    return "any"


def _node_text(nodes):
    return f"NUMA node{'s' if len(nodes) > 1 else ''} {L.format_cpulist(nodes)}"


def memory_preview(topo, allocations, nodes, mode, me):
    """Plain-language implications of a memory choice for the given local nodes."""
    lines = [""]
    remote = [n for n in topo.numa_nodes if n not in nodes]
    d = topo.node_distance.get(nodes[0], {})
    d_local, d_remote = d.get(nodes[0]), min((d[n] for n in remote if n in d), default=None)
    dist = f" (NUMA distance {d_local} vs {d_remote} remote)" if d_local and d_remote else ""

    def mem(ns):
        vals = [topo.node_memory_kb(n) for n in ns]
        if any(t is None or f is None for t, f in vals):
            return None, None
        return sum(t for t, _ in vals), sum(f for _, f in vals)

    if mode == "local":
        total, free = mem(nodes)
        lines.append(f"   Memory latency    {L.meter(8)}  always local{dist}")
        if total:
            cap = f"{_gb(free)} free of {_gb(total)} on {_node_text(nodes)}"
            lines.append(f"   Capacity          {L.meter(round(8 * free / total))}  {cap}")
            if free < total * 0.10:
                lines.append("   " + L.warn(f"Only {_gb(free)} free: large jobs will be OOM-killed"))
        lines.append("   " + L.yellow("Needing more than that gets your processes OOM-killed, even if other nodes are free."))
        lines.append("   " + L.dim("Memory your running processes already hold elsewhere is moved to this node."))
        others = sorted({a.uid for a in allocations if a.uid != me and a.mems & set(nodes)})
        if others:
            lines.append(f"   Also bound here   {', '.join(L.user_name(u) for u in others)}")
    else:
        total, free = mem(topo.numa_nodes)
        lines.append(f"   Memory latency    {L.meter(6)}  local first; remote once {_node_text(nodes)} is full")
        if total:
            lines.append(f"   Capacity          {L.meter(round(8 * free / total))}  "
                         f"{_gb(free)} free of {_gb(total)} (whole machine)")
        lines.append("   " + L.dim("No binding: memory allocated earlier (e.g. on the shared cores) stays where it is."))
    return lines


def choose_memory(topo, cfg, allocations, socket, p, me):
    """Returns "local", "any", BACK, or None."""
    nodes = topo.nodes_for(p.cpus)
    if not can_bind_memory(topo, cfg) or not nodes:
        return "any"
    total, free = 0, 0
    for n in nodes:
        t, f = topo.node_memory_kb(n)
        total, free = total + (t or 0), free + (f or 0)
    local_detail = "strict" + (f" · {_gb(free)} free of {_gb(total)}" if total else "")
    options = [L.Option(f"Socket {socket} memory only ({_node_text(nodes)})", local_detail),
               L.Option("Local first, spill over when full", "kernel default, no binding"),
               _back_option("Choose another size")]
    values = ["local", "any", BACK]

    def preview(i):
        return [] if values[i] == BACK else memory_preview(topo, allocations, nodes, values[i], me)

    print()
    print(L.dim("Binding keeps all your memory next to your cores (and a local GPU), but your\n"
                "processes can only use that node's RAM."))
    idx = L.menu("Where should your memory come from?", options,
                 default=values.index(default_mem(topo, cfg, socket, True)), preview=preview)
    return None if idx is None else values[idx]


def mem_text(topo, mems):
    if not mems:
        return "local first, no binding"
    return f"{_node_text(sorted(mems))} only (strict)"


def release(uid, topo, cfg):
    with L.lma_lock(), L.no_interrupt():
        allocations = L.load_allocations(topo)
        mine = next((a for a in allocations if a.uid == uid), None)
        if mine is None:
            print(L.warn("You do not hold any cores."))
            return 0
        run_hook(L.DHOOK_SCRIPT_PATH, "Deallocation",
                 [uid, L.format_cpulist(mine.cpus), len(mine.cpus), mine.socket,
                  L.format_cpulist(mine.mems) or "none"])
        allocations = [a for a in allocations if a.uid != uid]
        L.save_allocations(allocations)
    show_map(topo, cfg, allocations, uid)
    print()
    print(L.box([L.ok(f"Released CPUs {L.format_cpulist(mine.cpus)}"),
                 L.dim("Your processes are back on the shared cores.")], color=L.green))
    return 0


def allocate(uid, topo, cfg, socket, size, ccds, mem="any"):
    with L.lma_lock(), L.no_interrupt():
        # Re-read under the lock: someone may have allocated while we were in the menus.
        allocations = L.load_allocations(topo)
        if any(a.uid == uid for a in allocations):
            raise L.LmaError("You already hold cores (allocated from another session).")
        p = place(topo, cfg, allocations, socket, size, ccds)
        if p is None:
            raise L.LmaError(f"Socket {socket} no longer has room for {unit_word(cfg, size)} "
                             f"({layout_label(ccds, size).lower()}). Please try again.")
        cpulist = L.format_cpulist(p.cpus)
        mems = frozenset(topo.nodes_for(p.cpus)) if mem == "local" else frozenset()
        run_hook(L.AHOOK_SCRIPT_PATH, "Allocation",
                 [uid, cpulist, len(p.cpus), socket, L.format_cpulist(mems) or "none"])
        allocations.append(L.Allocation(uid, p.cpus, socket, mems))
        L.save_allocations(allocations)

    print()
    print(L.bold(f"Your allocation on socket {socket}"))
    others = [a for a in allocations if a.uid != uid]
    for line in placement_preview(topo, cfg, others, socket, p, uid, "yours"):
        print(line)
    if can_bind_memory(topo, cfg) and topo.nodes_for(p.cpus):
        for line in memory_preview(topo, others, topo.nodes_for(p.cpus), mem, uid)[1:]:
            print(line)
    print()
    lines = [L.ok(bold_count(cfg, size, len(p.cpus)) + f" on socket {socket}"),
             f"  CPUs    {L.bold(L.cyan(cpulist))}",
             f"  Layout  {workload_label(ccds, ccds)} · {layout_label(ccds, size)} ({ccd_text(p)})"]
    if can_bind_memory(topo, cfg):
        lines.append(f"  Memory  {mem_text(topo, mems)}")
    for g in topo.gpus_on(socket):
        lines.append(f"  Local   {L.magenta(g.short)}")
    if p.shared_with:
        lines.append(L.warn("Shares a CCD (L3 + memory link) with: "
                            + ", ".join(L.user_name(u) for u in sorted(p.shared_with))))
    if not p.contiguous:
        lines.append(L.warn("Block is fragmented (no contiguous range was free)."))
    lines += ["", L.dim("Applies to every process in your user slice."),
              L.dim("Check:   nproc   or   taskset -cp $$")]
    if mems:
        lines.append(L.dim("Memory:  numastat -p <pid>   (should be all on node " + L.format_cpulist(mems) + ")"))
    if cfg.unit == "physical":
        lines.append(L.dim("OpenMP:  OMP_PLACES=cores OMP_PROC_BIND=spread  (1 thread per core)"))
    lines.append(L.dim("Release: sudo lma --release"))
    print(L.box(lines, color=L.green))
    return 0


def bold_count(cfg, size, ncpus):
    text = unit_word(cfg, size)
    if ncpus != size:
        text += f" ({ncpus} CPUs)"
    return f"Allocated {text}"


def _fits(topo, cfg, allocations, socket, size, request):
    return resolve(topo, cfg, allocations, socket, size, request) is not None


def choose_socket(topo, cfg, allocations, request):
    """Interactive socket picker. Returns a socket id or None."""
    options, sockets = [], []
    for s in topo.sockets:
        n_free = free_units(topo, cfg, allocations, s)
        fits = [z for z in cfg.sizes if _fits(topo, cfg, allocations, s, z, request)]
        gpus = ", ".join(g.short for g in topo.gpus_on(s))
        detail = f"{unit_word(cfg, n_free)} free"
        if cfg.unit == "physical" and len(topo.ccds(s)) > 1:
            detail += f" · {len(topo.ccds(s))} CCDs"
        if gpus:
            detail += f" · {gpus}"
        if not fits:
            detail += " · no block size fits"
        options.append(L.Option(f"Socket {s}", detail, disabled=not fits))
        sockets.append(s)
    usable = [s for s, o in zip(sockets, options) if not o.disabled]
    if not usable:
        return None
    if len(usable) == 1:
        print()
        print(L.dim(f"Only socket {usable[0]} has room, using it."))
        return usable[0]
    # Default to the socket with a GPU if there is exactly one such socket, else most free.
    gpu_sockets = [s for s in usable if topo.gpus_on(s)]
    default_socket = gpu_sockets[0] if len(gpu_sockets) == 1 else max(
        usable, key=lambda s: free_units(topo, cfg, allocations, s))
    idx = L.menu("Which socket?", options, default=sockets.index(default_socket))
    return None if idx is None else sockets[idx]


BACK = "back"


def _back_option(text):
    return L.Option(("← " if L._unicode else "<- ") + text)


def choose_size(topo, cfg, allocations, socket, request, can_go_back, me):
    """Returns a size, BACK, or None."""
    options, values = [], []
    for z in cfg.sizes:
        r = resolve(topo, cfg, allocations, socket, z, request)
        if r:
            k, p = r
            detail = f"{len(p.cpus)} CPUs · " if cfg.unit == "physical" else ""
            detail += f"→ {L.format_cpulist(p.cpus)}" if k == 1 else f"→ {k} CCDs × {z // k}"
            if not p.contiguous:
                detail += " (fragmented)"
        else:
            detail = "not enough free cores"
        options.append(L.Option(unit_word(cfg, z), detail, disabled=r is None))
        values.append(z)
    if can_go_back:
        options.append(_back_option("Choose another socket"))
        values.append(BACK)

    def preview(i):
        if values[i] == BACK:
            return []
        r = resolve(topo, cfg, allocations, socket, values[i], request)
        return placement_preview(topo, cfg, allocations, socket, r[1], me) if r else []

    idx = L.menu(f"How many cores on socket {socket}?", options, preview=preview)
    return None if idx is None else values[idx]


def choose_layout(topo, cfg, allocations, socket, size, me):
    """Workload-based layout menu. Returns a CCD count, BACK, or None."""
    opts = layouts(topo, cfg, allocations, socket, size)
    if len(opts) <= 1:
        return opts[0][0] if opts else None
    best_k = best_spread([k for k, _ in opts], size)
    default_k = default_ccds(cfg, opts, size)
    # Compact first, then the recommended spread, then the other spreads.
    opts.sort(key=lambda kp: (kp[0] != 1, kp[0] != best_k, kp[0]))
    # Drop layouts that land on exactly the same CPUs as one listed earlier
    # (e.g. 16 cores "spread" over 2 CCDs == the compact block).
    seen, unique = set(), []
    for k, p in opts:
        if p.cpus not in seen:
            seen.add(p.cpus)
            unique.append((k, p))
    opts = unique
    if len(opts) == 1:
        return opts[0][0]
    if default_k not in [k for k, _ in opts]:
        default_k = opts[0][0]
    options, values = [], []
    for k, p in opts:
        detail = f"{layout_label(k, size)} · {ccd_text(p)} · {L.format_cpulist(p.cpus)}"
        if p.shared_with:
            detail = "shares a CCD with " + ",".join(L.user_name(u) for u in sorted(p.shared_with)) + " · " + detail
        options.append(L.Option(workload_label(k, best_k), detail))
        values.append(k)
    options.append(_back_option("Choose another size"))
    values.append(BACK)
    print()
    print(L.dim("Cache/latency-sensitive work runs best with its cores kept together.\n"
                "Memory-bound work (inference, large arrays, streaming) gets more memory bandwidth\n"
                "when its cores are spread over several CCDs (chiplets), since each has its own memory link."))
    placements = dict(opts)

    def preview(i):
        if values[i] == BACK:
            return []
        return placement_preview(topo, cfg, allocations, socket, placements[values[i]], me)

    idx = L.menu(f"What will you run on these {unit_word(cfg, size)}?", options,
                 default=values.index(default_k), preview=preview)
    return None if idx is None else values[idx]


def run(args, topo, cfg):
    uid = args.user_id
    L.banner(f"{L.user_name(uid)} (uid {uid}) · {topo.summary()}")
    allocations = L.load_allocations(topo)
    show_map(topo, cfg, allocations, uid)
    mine = next((a for a in allocations if a.uid == uid), None)

    if mine:
        ccds = L.format_cpulist({topo.ccd_index[c] for c in mine.cpus if c in topo.ccd_index})
        held = (f"CPUs {L.bold(L.cyan(L.format_cpulist(mine.cpus)))} "
                f"({len(mine.cpus)} CPUs, socket {mine.socket}, CCD {ccds}; memory: {mem_text(topo, mine.mems)})")
    if args.status:
        if mine:
            print("\n" + f"You hold {held}.")
        return 0

    if mine:
        print()
        print(f"You currently hold {held}.")
        if args.release:
            return release(uid, topo, cfg)
        if args.size is not None or args.socket is not None:
            raise L.LmaError("Release your current cores first: sudo lma --release")
        idx = L.menu("What do you want to do?",
                     [L.Option("Release my cores"), L.Option("Keep them and exit")], default=1)
        if idx == 0:
            return release(uid, topo, cfg)
        print(L.dim("Nothing changed."))
        return 0

    if args.release:
        print(L.warn("You do not hold any cores."))
        return 0

    if args.socket is not None and args.socket not in topo.sockets:
        raise L.LmaError(f"Socket {args.socket} does not exist (sockets: {', '.join(map(str, topo.sockets))}).")
    if args.size is not None and args.size not in cfg.sizes:
        raise L.LmaError(f"Size {args.size} is not allowed (allowed: {', '.join(map(str, cfg.sizes))}).")

    # Layout request from flags: None = ask (or config default), "spread", or a CCD count.
    request = None
    if args.ccds is not None:
        if args.ccds < 1:
            raise L.LmaError("--ccds must be at least 1.")
        if args.size is not None and args.size % args.ccds:
            raise L.LmaError(f"{args.size} cores cannot be split evenly over {args.ccds} CCDs.")
        request = args.ccds
    elif args.spread:
        request = "spread"
    elif args.compact:
        request = 1
    if args.mem == "local" and not can_bind_memory(topo, cfg):
        why = "disabled by the admin (memoryBinding: off)" if cfg.memory_binding == "off" \
            else "not possible: this machine has a single NUMA node"
        raise L.LmaError(f"Memory binding is {why}.")
    if request not in (None, 1) and cfg.unit != "physical":
        raise L.LmaError("Spread placement needs allocationUnit \"physical\" (ask your admin to re-run lma-setup).")

    # Non-interactive path: --size given (socket optional).
    if args.size is not None:
        # Without --socket, prefer the socket with the most free capacity.
        candidates = [args.socket] if args.socket is not None else sorted(
            topo.sockets, key=lambda s: -free_units(topo, cfg, allocations, s))
        for s in candidates:
            r = resolve(topo, cfg, allocations, s, args.size, request)
            if r:
                return allocate(uid, topo, cfg, s, args.size, r[0],
                                args.mem or default_mem(topo, cfg, s, interactive=False))
        where = f"socket {args.socket}" if args.socket is not None else "any socket"
        how = f" over {request} CCDs" if isinstance(request, int) and request > 1 else \
            " spread over several CCDs" if request == "spread" else ""
        msg = f"No room for {unit_word(cfg, args.size)}{how} on {where}."
        if isinstance(request, int) and request > 1:
            n = max(len([g for g in topo.ccds(s) if any(all(c in cfg.isolated for c in core) for core in g)])
                    for s in candidates)
            if request > n:
                msg += f" Only {n} CCDs have isolated cores."
        raise L.LmaError(msg)

    while True:
        if args.socket is not None:
            socket, socket_menu = args.socket, False
        else:
            socket = choose_socket(topo, cfg, allocations, request)
            if socket is None:
                if not any(_fits(topo, cfg, allocations, s, z, request) for s in topo.sockets for z in cfg.sizes):
                    raise L.LmaError("No free block of any allowed size is available right now.")
                print(L.dim("Nothing changed."))
                return 0
            socket_menu = len([s for s in topo.sockets
                               if any(_fits(topo, cfg, allocations, s, z, request) for z in cfg.sizes)]) > 1
        while True:
            size = choose_size(topo, cfg, allocations, socket, request, socket_menu, uid)
            if size == BACK:
                break
            if size is None:
                print(L.dim("Nothing changed."))
                return 0
            if request is None and cfg.unit == "physical":
                ccds = choose_layout(topo, cfg, allocations, socket, size, uid)
                if ccds == BACK:
                    continue
                if ccds is None:
                    print(L.dim("Nothing changed."))
                    return 0
            else:
                ccds = resolve(topo, cfg, allocations, socket, size, request)[0]
            mem = args.mem
            if mem is None:
                p = place(topo, cfg, allocations, socket, size, ccds)
                mem = choose_memory(topo, cfg, allocations, socket, p, uid)
                if mem == BACK:
                    continue
                if mem is None:
                    print(L.dim("Nothing changed."))
                    return 0
            return allocate(uid, topo, cfg, socket, size, ccds, mem)
        if args.socket is not None:
            # Socket fixed by flag; going back from the size menu means cancel.
            print(L.dim("Nothing changed."))
            return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lma",
        description="Allocate isolated CPU cores to yourself. Run without options for the interactive menu.")
    parser.add_argument("user_id", type=int, help=argparse.SUPPRESS)
    parser.add_argument("-s", "--socket", type=int, help="socket to allocate on (e.g. the one your GPU is attached to)")
    parser.add_argument("-n", "--size", type=int, help="number of cores; skips the menus")
    layout = parser.add_mutually_exclusive_group()
    layout.add_argument("--spread", action="store_true",
                        help="spread whole cores over several CCDs (more memory bandwidth)")
    layout.add_argument("--compact", action="store_true",
                        help="keep cores in one contiguous block (best cache locality)")
    layout.add_argument("--ccds", type=int, metavar="N",
                        help="spread over exactly N CCDs (size/N cores on each); implies --spread")
    parser.add_argument("-m", "--mem", choices=L.MEM_MODES,
                        help="local: bind memory to your socket's NUMA node (strict); any: no binding")
    parser.add_argument("-r", "--release", action="store_true", help="release your cores")
    parser.add_argument("--status", action="store_true", help="show the core map and exit")
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    args = parser.parse_args(argv)

    if args.no_color:
        L.set_color(False)
    if os.geteuid() != 0:
        print(L.err("lma must be run as root (use: sudo lma)."))
        return 1
    try:
        cfg = L.load_runtime_config()
        topo = L.Topology.detect()
        return run(args, topo, cfg)
    except L.LmaError as e:
        print()
        print(L.err(e))
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        print(L.dim("Cancelled. Nothing changed."))
        return 130


if __name__ == "__main__":
    sys.exit(main())
