#!/usr/bin/env python3
"""lma-setup: validate a config (or create one with a guided wizard) and apply it."""
import argparse
import json
import os
import subprocess
import sys
import tempfile

# Run with 'python3 -I', which drops the script dir from sys.path; /usr/lib/lma is root-owned.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import lmalib as L  # noqa: E402

SLICE_DIR = "/etc/systemd/system"
SUDOERS_DIR = "/etc/sudoers.d"
SUDOERS_RULE = f"{SUDOERS_DIR}/lma"
LMA_PATH = "/usr/sbin/lma"
RESET_SCRIPT_PATH = f"{L.LIB_DIR}/lma-reset.sh"
RESET_SERVICE_PATH = f"{SLICE_DIR}/lma-reset.service"
GRUB_IRQ_PATH = "/etc/default/grub.d/cgroup.cfg"
SLICE_DROPIN = "40-cpulimit.conf"
USER_DROPIN = "90-AllowedCPUs.conf"


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------

def read_config(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise L.LmaError(f"Configuration file not found: {path}")
    except json.JSONDecodeError as e:
        raise L.LmaError(f"Invalid JSON in {path}: {e}")


def validate(config, topo):
    """Return (lma_data, warnings) or raise LmaError."""
    if not isinstance(config, dict):
        raise L.LmaError("config must be a JSON object")
    cpu = config.get("cpu")
    if not isinstance(cpu, dict):
        raise L.LmaError('"cpu" section is required')
    for key in ("shared", "isolated"):
        if key not in cpu:
            raise L.LmaError(f'"cpu.{key}" is required')
    try:
        online = set(topo.cpus)
        total = L.parse_cpulist(cpu["total"]) if "total" in cpu else online
        shared = L.parse_cpulist(cpu["shared"])
        isolated = L.parse_cpulist(cpu["isolated"])
    except ValueError as e:
        raise L.LmaError(f"cpu: {e}")

    if not shared:
        raise L.LmaError("cpu.shared must not be empty (the OS needs somewhere to run)")
    if not isolated:
        raise L.LmaError("cpu.isolated must not be empty")
    if shared & isolated:
        raise L.LmaError(f"cpu.shared and cpu.isolated overlap: {L.format_cpulist(shared & isolated)}")
    for name, s in (("shared", shared), ("isolated", isolated)):
        if not s <= total:
            raise L.LmaError(f"cpu.{name} has CPUs outside cpu.total: {L.format_cpulist(s - total)}")
    if not total <= online:
        raise L.LmaError(f"CPUs {L.format_cpulist(total - online)} are not online on this machine "
                         f"(online: {L.format_cpulist(online)})")

    unit = config.get("allocationUnit", "logical")
    if unit not in L.ALLOCATION_UNITS:
        raise L.LmaError(f'"allocationUnit" must be one of {", ".join(L.ALLOCATION_UNITS)}')

    placement = config.get("placement", "compact")
    if placement not in L.PLACEMENTS:
        raise L.LmaError(f'"placement" must be one of {", ".join(L.PLACEMENTS)}')
    if placement == "spread" and unit != "physical":
        raise L.LmaError('"placement": "spread" requires "allocationUnit": "physical" '
                         "(spread keeps both hyperthreads of every core together)")

    memory_binding = config.get("memoryBinding", "optional")
    if memory_binding not in L.MEMORY_BINDINGS:
        raise L.LmaError(f'"memoryBinding" must be one of {", ".join(L.MEMORY_BINDINGS)}')

    sizes = config.get("coreGroupSizes")
    if not isinstance(sizes, list) or not sizes:
        raise L.LmaError('"coreGroupSizes" must be a non-empty list of integers')
    for s in sizes:
        if not isinstance(s, int) or isinstance(s, bool) or s <= 0:
            raise L.LmaError(f'"coreGroupSizes" entries must be positive integers (got {s!r})')
    sizes = sorted(set(sizes))

    flags = {}
    for key in ("setIRQAffinityToShared", "addLMAGroupToSudoers"):
        value = config.get(key, False)
        if not isinstance(value, bool):
            raise L.LmaError(f'"{key}" must be true or false')
        flags[key] = value

    warnings = []
    unmanaged = total - shared - isolated
    if unmanaged:
        warnings.append(f"CPUs {L.format_cpulist(unmanaged)} are in neither shared nor isolated; "
                        "only user sessions without an allocation may use them.")
    noisy = {c for c in isolated if any(s in shared for s in topo.siblings[c])}
    if noisy:
        msg = (f"{len(noisy)} isolated CPUs ({L.format_cpulist(noisy)}) are SMT siblings of shared CPUs, "
               "so work placed on them shares a physical core with the OS.")
        if unit == "physical":
            msg += " In physical mode these can never be allocated."
        warnings.append(msg)
    for s in topo.sockets:
        units = [c for c in topo.cores(s) if all(t in isolated for t in c)] if unit == "physical" \
            else [c for c in topo.cpus_in_socket(s) if c in isolated]
        too_big = [z for z in sizes if z > len(units)]
        if units and too_big:
            warnings.append(f"Socket {s} only has {len(units)} allocatable units; size(s) "
                            f"{', '.join(map(str, too_big))} will never fit there.")
    if not any(min(sizes) <= len([c for c in topo.cpus_in_socket(s) if c in isolated]) for s in topo.sockets):
        raise L.LmaError("No socket has enough isolated CPUs for even the smallest coreGroupSize")

    data = {
        "version": L.LMA_DATA_VERSION,
        "cpu": {
            "total": L.format_cpulist(total),
            "shared": L.format_cpulist(shared),
            "isolated": L.format_cpulist(isolated),
        },
        "allocationUnit": unit,
        "placement": placement,
        "memoryBinding": memory_binding,
        "coreGroupSizes": sizes,
        **flags,
    }
    return data, warnings


# --------------------------------------------------------------------------
# Wizard
# --------------------------------------------------------------------------

def shared_from_counts(topo, counts):
    """First counts[socket] physical cores of each socket, with all their SMT siblings."""
    cpus = set()
    for s, n in counts.items():
        for core in topo.cores(s)[:n]:
            cpus.update(core)
    return cpus


def choose_split(topo, n):
    """Ask how to distribute n shared physical cores over the sockets. Returns {socket: n}."""
    sockets = topo.sockets
    if len(sockets) == 1:
        return {sockets[0]: n}
    options, values = [], []
    base, extra = divmod(n, len(sockets))
    even = {s: base + (1 if i < extra else 0) for i, s in enumerate(sockets)}
    if base > 0 and all(even[s] < len(topo.cores(s)) for s in sockets):
        per = " + ".join(str(even[s]) for s in sockets)
        options.append(L.Option(f"Split across sockets ({per})",
                                "each socket serves its own devices (recommended with GPUs)"))
        values.append(even)
    for s in sockets:
        fits = n < len(topo.cores(s))
        gpus = topo.gpus_on(s)
        detail = "" if fits else f"socket {s} only has {len(topo.cores(s))} cores"
        if fits and gpus:
            detail = f"local to {gpus[0].short}"
        options.append(L.Option(f"All on socket {s}", detail, disabled=not fits))
        values.append({s: n})
    idx = L.menu(f"Where should the OS's {n} cores be?", options, default=0)
    return None if idx is None else values[idx]


def wizard(topo):
    L.banner("Setup wizard · " + topo.summary())
    print()
    for s in topo.sockets:
        gpus = ", ".join(g.short for g in topo.gpus_on(s)) or L.dim("no GPU")
        print(f"  {L.bold('Socket ' + str(s))}  CPUs {L.format_cpulist(topo.cpus_in_socket(s))}  {L.magenta(gpus)}")
    unknown = [g for g in topo.gpus if g.socket is None]
    for g in unknown:
        print(f"  {L.magenta(g.short)} {L.dim('(socket unknown)')}")

    # 1. Shared region, asked in physical cores
    total_cores = sum(len(topo.cores(s)) for s in topo.sockets)
    print()
    print(L.dim("The OS keeps some cores for itself: daemons, interrupts and every user without an\n"
                "allocation run there. Everything else can be handed out with 'sudo lma'."))

    def parse_shared(raw):
        if raw.isdigit():
            n = int(raw)
            if not 0 < n < total_cores:
                raise ValueError(f"enter a number between 1 and {total_cores - 1}")
            return n
        cpus = L.parse_cpulist(raw)   # advanced: an explicit CPU list
        if not cpus or not cpus <= set(topo.cpus) or cpus == set(topo.cpus):
            raise ValueError(f"enter a core count, or a CPU list within {L.format_cpulist(topo.cpus)}")
        return cpus

    answer = L.ask_text("How many physical cores should the OS keep? "
                        + L.dim("(or a CPU list, e.g. 0-7,128-135)"),
                        default=str(min(8 * len(topo.sockets), total_cores - 1)), validate=parse_shared)
    if isinstance(answer, int):
        counts = choose_split(topo, answer)
        if counts is None:
            return None
        shared = shared_from_counts(topo, counts)
        ccd_sizes = {len(g) for s in topo.sockets for g in topo.ccds(s)}
        if len(ccd_sizes) == 1 and (size := ccd_sizes.pop()) > 1 and any(n % size for n in counts.values()):
            print(L.dim(f"  Tip: multiples of {size} per socket give the OS whole CCDs (chiplets), "
                        "so no user shares a CCD with it."))
    else:
        shared = answer
    n_cores = len({topo.siblings[c] for c in shared})
    print(L.dim(f"  OS keeps {n_cores} physical cores ({len(shared)} CPUs): {L.format_cpulist(shared)}"))
    isolated = set(topo.cpus) - shared
    if not isolated:
        raise L.LmaError("That leaves no CPUs to isolate.")
    print(L.dim(f"  Isolated: {L.format_cpulist(isolated)}"))

    # 2. Allocation unit
    unit_opts = [L.Option("Logical CPUs", "'8 cores' = 8 hardware threads (LMA 1.0 behaviour)")]
    if topo.threads_per_core > 1:
        unit_opts.append(L.Option("Physical cores",
                                  f"'8 cores' = 8 cores x {topo.threads_per_core} threads; nobody shares a core"))
    idx = L.menu("What does one 'core' in an allocation mean?", unit_opts, default=0)
    if idx is None:
        return None
    unit = L.ALLOCATION_UNITS[idx]

    placement = "compact"
    if unit == "physical" and max(len(topo.ccds(s)) for s in topo.sockets) > 1:
        idx = L.menu("What do most of your users run? (sets the default layout; users can still choose)", [
            L.Option("Cache / latency-sensitive work", "keeps each user's cores together (compact)"),
            L.Option("Memory-bound work", "e.g. inference, large arrays: spreads cores over chiplets for bandwidth"),
        ], default=0)
        if idx is None:
            return None
        placement = L.PLACEMENTS[idx]

    # 3. Sizes
    def parse_sizes(raw):
        try:
            sizes = sorted({int(x) for x in raw.replace(" ", "").split(",") if x})
        except ValueError:
            raise ValueError("enter comma-separated positive integers, e.g. 8,16,32")
        if not sizes or sizes[0] <= 0:
            raise ValueError("enter comma-separated positive integers, e.g. 8,16,32")
        return sizes
    per_socket = max(len([c for c in topo.cores(s) if all(t in isolated for t in c)]) if unit == "physical"
                     else len([c for c in topo.cpus_in_socket(s) if c in isolated]) for s in topo.sockets)
    suggested = [z for z in (8, 16, 32, 64) if z <= per_socket] or [per_socket]
    sizes = L.ask_text(f"Allowed allocation sizes (max {per_socket} per socket)",
                       default=",".join(map(str, suggested)), validate=parse_sizes)

    # 4. Flags
    memory_binding = "optional"
    if len(topo.numa_nodes) > 1:
        idx = L.menu("Let users bind their memory to their socket's NUMA node?", [
            L.Option("Optional", "users choose; preselected for GPU sockets"),
            L.Option("Local by default", "bound unless the user picks 'local first'"),
            L.Option("Off", "never bind memory"),
        ], default=0)
        if idx is None:
            return None
        memory_binding = L.MEMORY_BINDINGS[[1, 2, 0][idx]]

    irq = L.confirm("Route IRQs to the shared CPUs? (kernel irqaffinity=, needs a reboot)", default=True)
    sudo = L.confirm("Create the 'lma' group and let its members run 'sudo lma' without a password?", default=True)

    return {
        "cpu": {
            "total": L.format_cpulist(topo.cpus),
            "shared": L.format_cpulist(shared),
            "isolated": L.format_cpulist(isolated),
        },
        "allocationUnit": unit,
        "placement": placement,
        "memoryBinding": memory_binding,
        "coreGroupSizes": sizes,
        "setIRQAffinityToShared": irq,
        "addLMAGroupToSudoers": sudo,
    }


def write_config(config, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------

def write_file(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".lma.")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_slices(data):
    shared = data["cpu"]["shared"]
    total = data["cpu"]["total"]
    dropins = {
        "init.scope.d": shared,
        "system.slice.d": shared,
        "user.slice.d": total,
        "user-.slice.d": shared,   # default for every user-UID.slice without an allocation
    }
    for unit_dir, cpus in dropins.items():
        write_file(f"{SLICE_DIR}/{unit_dir}/{SLICE_DROPIN}", f"[Slice]\nAllowedCPUs={cpus}\n")


def write_reset_service():
    script = f"""#!/bin/bash
# Generated by lma-setup: clears all LMA allocations (runs at boot).
rm -f {L.DATABASE_FILE}
for f in {SLICE_DIR}/user-*.slice.d/{USER_DROPIN}; do
    [ -e "$f" ] || continue
    echo "deleting $f"
    rm -f "$f"
done
systemctl daemon-reload
systemctl restart user.slice
exit 0
"""
    write_file(RESET_SCRIPT_PATH, script, mode=0o755)
    service = f"""[Unit]
Description=LMA cleanup user slices
After=multi-user.target

[Service]
Type=oneshot
ExecStart={RESET_SCRIPT_PATH}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
    write_file(RESET_SERVICE_PATH, service)


def write_grub(data):
    """Returns True if the GRUB configuration changed."""
    if data["setIRQAffinityToShared"]:
        content = f'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT irqaffinity={data["cpu"]["shared"]}"\n'
        old = L._read(GRUB_IRQ_PATH)
        write_file(GRUB_IRQ_PATH, content)
        return old != content.strip()
    if os.path.exists(GRUB_IRQ_PATH):
        os.remove(GRUB_IRQ_PATH)
        return True
    return False


def write_sudoers(enabled):
    if not enabled:
        if os.path.exists(SUDOERS_RULE):
            os.remove(SUDOERS_RULE)
        return
    if not os.path.isdir(SUDOERS_DIR):
        raise L.LmaError(f"{SUDOERS_DIR} does not exist on this system")
    # Files in sudoers.d containing a '.' are ignored by sudo, so the temp file is inert.
    fd, tmp = tempfile.mkstemp(dir=SUDOERS_DIR, prefix=".lma.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"%lma ALL=(root) NOPASSWD: {LMA_PATH}\n")
        os.chmod(tmp, 0o440)
        if subprocess.run(["visudo", "-cf", tmp], capture_output=True).returncode != 0:
            raise L.LmaError("visudo validation failed; sudoers not updated")
        os.replace(tmp, SUDOERS_RULE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def step(msg, fn, *args):
    print(f"  {L.dim('•' if L._unicode else '-')} {msg}", flush=True)
    return fn(*args)


def run_cmd(*cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise L.LmaError(f"'{' '.join(cmd)}' failed:\n{(result.stderr or result.stdout).strip()}")


def apply(data, assume_yes):
    with L.lma_lock():
        topo = L.Topology.detect()
        existing = L.load_allocations(topo) if os.path.exists(L.DATABASE_FILE) else []
        if existing:
            print()
            print(L.warn(f"Applying will release {len(existing)} active allocation(s): "
                         + ", ".join(f"{a.user_name} ({L.format_cpulist(a.cpus)})" for a in existing)))
            if not assume_yes and not (L.interactive() and L.confirm("Continue?", default=False)):
                raise L.LmaError("Aborted; nothing changed.")

        print()
        print(L.bold("Applying configuration"))
        with L.no_interrupt():
            step("Writing systemd slice limits", write_slices, data)
            step("Installing boot-time reset service", write_reset_service)
            grub_changed = step("Updating GRUB IRQ affinity", write_grub, data)
            step("Updating sudoers rule", write_sudoers, data["addLMAGroupToSudoers"])
            if data["addLMAGroupToSudoers"]:
                step("Ensuring group 'lma' exists", run_cmd, "groupadd", "-f", "lma")
            for hook in (L.AHOOK_SCRIPT_PATH, L.DHOOK_SCRIPT_PATH):
                if os.path.exists(hook):
                    os.chmod(hook, 0o755)
            step("Saving runtime data", write_file, L.LMA_DATA_PATH, json.dumps(data, indent=4) + "\n")
            step("Reloading systemd", run_cmd, "systemctl", "daemon-reload")
            step("Enabling lma-reset.service", run_cmd, "systemctl", "enable", "lma-reset.service")
            # Run the reset now (not just 'start', which is a no-op once the service is active)
            # so stale per-user drop-ins and the allocation database are cleared together.
            step("Clearing existing allocations", run_cmd, RESET_SCRIPT_PATH)
            if grub_changed:
                step("Running update-grub", run_cmd, "update-grub")

    lines = [L.ok("LMA setup complete"),
             f"  shared   {L.format_cpulist(L.parse_cpulist(data['cpu']['shared']))}",
             f"  isolated {L.format_cpulist(L.parse_cpulist(data['cpu']['isolated']))}",
             f"  unit     {data['allocationUnit']}   layout {data['placement']}   "
             f"sizes {', '.join(map(str, data['coreGroupSizes']))}",
             f"  memory   binding {data['memoryBinding']}"]
    if grub_changed:
        lines.append(L.warn("Reboot required for the new IRQ affinity to take effect."))
    if data["addLMAGroupToSudoers"]:
        lines.append(L.dim("Add users with: sudo usermod -aG lma <user>"))
    print()
    print(L.box(lines, color=L.green))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lma-setup",
        description="Leave Me Alone (LMA) - core isolation for cgroup v2. "
                    f"Without a config path, uses {L.DEFAULT_CONFIG_PATH} or starts a guided wizard.")
    parser.add_argument("path", nargs="?", help="config JSON to apply")
    parser.add_argument("--wizard", action="store_true", help="create a new config interactively")
    parser.add_argument("--check", action="store_true", help="only validate the config, change nothing")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    args = parser.parse_args(argv)

    if args.no_color:
        L.set_color(False)
    if os.geteuid() != 0 and not args.check:
        print(L.err("lma-setup must be run as root."))
        return 1

    try:
        topo = L.Topology.detect()
        path = args.path
        config = None
        if path is None and not args.wizard and os.path.exists(L.DEFAULT_CONFIG_PATH):
            path = L.DEFAULT_CONFIG_PATH
            if L.interactive() and not args.yes and not args.check:
                idx = L.menu(f"Found {L.DEFAULT_CONFIG_PATH}",
                             [L.Option("Apply it"), L.Option("Create a new one with the wizard")])
                if idx is None:
                    return 0
                if idx == 1:
                    path = None
        if path is None:
            if not L.interactive():
                raise L.LmaError(f"No config given and {L.DEFAULT_CONFIG_PATH} does not exist. "
                                 "Pass a config path or run lma-setup in a terminal for the wizard.")
            config = wizard(topo)
            if config is None:
                print(L.dim("Cancelled. Nothing changed."))
                return 0
        else:
            config = read_config(path)

        data, warnings = validate(config, topo)
        for w in warnings:
            print(L.warn(w))

        if path is None:
            print()
            print(L.dim(json.dumps(config, indent=2)))
            idx = L.menu("Save and apply?", [
                L.Option("Save and apply", L.DEFAULT_CONFIG_PATH),
                L.Option("Save only", "apply later with: sudo lma-setup"),
                L.Option("Discard")])
            if idx in (None, 2):
                print(L.dim("Cancelled. Nothing changed."))
                return 0
            write_config(config, L.DEFAULT_CONFIG_PATH)
            print(L.ok(f"Saved {L.DEFAULT_CONFIG_PATH}"))
            if idx == 1:
                return 0
        elif args.check:
            print(L.ok(f"{path} is valid."))
            return 0

        apply(data, args.yes)
        return 0
    except L.LmaError as e:
        print(L.err(e))
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        print(L.dim("Cancelled."))
        return 130


if __name__ == "__main__":
    sys.exit(main())
