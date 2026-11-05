# 💻 LMA (Leave Me Alone)

A simple utility for (cgroup v2) that allows users to:

1. Dedicate specific cpu cores for shared operation.
2. Isolate specific cpu cores for sensitive applications.
3. Allows users to enter/exit isolated core region.

---

## ✨ Features

* **Core Isolation:** Dedicates a specified set of CPU cores for exclusive application use, preventing kernel scheduling interference.
* **Socket aware:** Pick the socket to allocate on (e.g. the one your GPU is attached to). The core map shows which GPUs are local to each socket.
* **SMT aware:** Optionally allocate whole physical cores (all hyperthreads), so users never share a physical core.
* **Interactive UI:** Arrow-key menus and a live core map, plus flags (`--socket`, `--size`, `--release`, `--status`) for scripts.
* **Setup wizard:** `sudo lma-setup` without a config detects the topology and builds a config for you.
* **IRQ Affinity Management:** Set IRQ affinity to the shared core region.
* **Sudoers Integration:** Optionally sets up a dedicated user group (`lma`) and configures `sudoers` for secure, controlled execution of LMA commands. This allows non-sudo users to execute `sudo lma`.

## 🛠️ Configuration

LMA is configured using a single JSON file. The easiest way to create one is the wizard:

```bash
sudo lma-setup            # applies /etc/lma/config.json, or starts the wizard if it does not exist
sudo lma-setup --wizard   # always start the wizard
sudo lma-setup my.json    # apply a specific config
lma-setup --check my.json # validate only, change nothing
```

### Example `config.json`

CPU fields accept Linux cpulists (`"0-7,128-135"`), so they can follow the real socket/SMT layout
(`lscpu -e` shows which CPUs belong to which socket and core).

```json
{
  "cpu": {
    "total": "0-255",
    "shared": "0-7,64-71,128-135,192-199",
    "isolated": "8-63,72-127,136-191,200-255"
  },
  "allocationUnit": "physical",
  "placement": "compact",
  "memoryBinding": "optional",
  "coreGroupSizes": [8, 16, 32],
  "setIRQAffinityToShared": true,
  "addLMAGroupToSudoers": true
}
```

| Parameter              | Type         | Description                                                                                                                                      |
|------------------------|--------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| cpu.total              | cpulist      | All CPUs LMA manages (optional, defaults to every online CPU).                                                                                    |
| cpu.shared             | cpulist      | CPUs for the OS kernel, background tasks, I/O handling and users without an allocation.                                                           |
| cpu.isolated           | cpulist      | CPUs handed out to users. Must not overlap `cpu.shared`.                                                                                          |
| allocationUnit         | string       | `logical` (default, LMA 1.0 behaviour): a size of 8 means 8 hardware threads. `physical`: 8 physical cores including all their SMT threads.       |
| placement              | string       | `compact` (default): cores packed into as few CCDs as possible (best cache locality). `spread`: cores spread over several CCDs for more memory bandwidth. Requires `allocationUnit: physical`. Users can override per allocation. |
| memoryBinding          | string       | `optional` (default): users may bind their memory to their socket's NUMA node (preselected on GPU sockets). `local-by-default`: bound unless the user opts out. `off`: never. |
| coreGroupSizes         | array of int | The allocation sizes users may choose from (in units of `allocationUnit`). An allocation always stays within one socket.                         |
| setIRQAffinityToShared | boolean      | If true, adds `irqaffinity=<shared>` to the kernel command line (needs a reboot) so interrupts stay off isolated cores.                            |
| addLMAGroupToSudoers   | boolean      | If true, a system group is created and configured in sudoers to allow specific users to run lma commands without needing a full password prompt. |

The wizard asks how many **physical cores** the OS should keep (e.g. `32`) and whether to split them
across sockets (16 + 16) or put them all on one socket. It then builds the CPU list, including both
hyperthreads of every core. Typing a CPU list instead of a number is still possible.

`lma-setup` warns about common mistakes, for example isolated CPUs whose SMT sibling is shared.

## 📦 Installing the package

The package is available on Radio Banana PPA:

```
sudo add-apt-repository ppa:meetesh06/radio-banana
sudo apt update
sudo apt install lma
```


## ⚙️ Usage
LMA operation is split into a required setup phase and a runtime execution phase.

1. **Initialization (One-Time Setup)**

This command reads config.json, performs core isolation setup, and handles system-level changes like IRQ affinity and sudoers file modification. This must be run with elevated privileges.

```bash
sudo lma-setup config.json
```

```
admin@server:~$ sudo lma-setup lmaConfig.json 
LMA Setup Start
/tmp/lma_sudo_rule: parsed OK
Sourcing file `/etc/default/grub'
Sourcing file `/etc/default/grub.d/cgroup.cfg'
Sourcing file `/etc/default/grub.d/init-select.cfg'
Generating grub configuration file ...
Found linux image: /boot/vmlinuz-5.15.0-164-generic
Found initrd image: /boot/initrd.img-5.15.0-164-generic
Found linux image: /boot/vmlinuz-5.15.0-163-generic
Found initrd image: /boot/initrd.img-5.15.0-163-generic
Warning: os-prober will not be executed to detect other bootable partitions.
Systems on them will not be added to the GRUB boot configuration.
Check GRUB_DISABLE_OS_PROBER documentation entry.
Adding boot menu entry for UEFI Firmware Settings ...
done
LMA setup successful.

```

2. **Usage (entering and leaving isolated cores)**

**a. Adding users to the lma group**

```
sudo usermod -aG lma <username>
```

**b. Entering isolated cores**

```bash
sudo lma                       # interactive: pick a socket, then a size
sudo lma --socket 1            # skip the socket menu
sudo lma --socket 1 --size 16  # fully non-interactive
sudo lma --socket 1 --size 8 --ccds 4   # 8 cores as 2 cores on each of 4 CCDs
sudo lma --socket 1 --size 8 --spread   # spread, letting LMA pick the CCD count
sudo lma --socket 1 --size 8 --compact  # one contiguous block
sudo lma --socket 1 --size 8 --mem local  # also bind memory to socket 1's NUMA node
sudo lma --status              # show the core map
sudo lma --release             # give your cores back
```

### Compact vs spread (CCDs)

On AMD EPYC every CCD (8 cores sharing one L3 cache) has its own link to memory, which saturates at
roughly 40 GB/s (Zen 3) with only ~2 busy cores. A compact 8-core block therefore sits on one CCD and
gets ~40 GB/s, while the same 8 cores spread over 2 CCDs got ~72 GB/s in STREAM Triad.

* **Compact** — lowest latency and a private L3 if you fill the CCD. Good for cache-sensitive work.
* **Spread** — `--ccds N` puts `size/N` whole cores (both hyperthreads) on each of N CCDs. Good for
  memory-bound work. CCDs nobody else uses are preferred; if a CCD has to be shared with another user,
  LMA warns you, since you then share its L3 and memory link.

In the interactive menu, after choosing a size you are asked what you will run:
**Cache / latency-sensitive** (compact) or **Memory-bound** (spread over the most CCDs with at least
2 cores each). Other spread options, such as "Memory-bound, 2 CCDs", are listed too. Each option shows the
CCDs and CPUs it covers. While you move through the size and layout menus, a live preview shows the
socket's CCDs with the highlighted choice marked, plus what it means: memory bandwidth (in CCD memory links,
e.g. `1×` for one CCD), cache locality (high/medium/low), and L3 size and whether it is shared with other users. CCDs are detected from which CPUs share an L3 cache
(`/sys/devices/system/cpu/cpu*/cache/index3/shared_cpu_list`). On Zen 3/Zen 4 that is exactly one CCD;
on Zen 2 (Rome) it is half a CCD.

Inside a spread allocation, place one thread per core, e.g.
`OMP_PLACES=cores OMP_PROC_BIND=spread`, and bind memory to the socket (`numactl --membind=<node>`).

### Memory binding (NUMA)

After the layout, LMA asks where your memory should come from:

* **Socket memory only** — sets `AllowedMemoryNodes=` (cgroup `cpuset.mems`) on your user slice, so all your
  memory, including pinned host buffers for a GPU on that socket, stays local. Memory your running processes
  already hold elsewhere is migrated. **If you need more than that node has free, your processes are
  OOM-killed even when other nodes have free memory.** The menu shows the node's free memory and who else is bound to it.
* **Local first, spill over when full** — no binding. Linux already allocates on the node of the CPU that
  first touches the memory, so this is mostly local, falling back to the other socket when the node is full.

This option is preselected on sockets with a GPU. Without `--mem`, non-interactive runs only bind when the admin set
`memoryBinding: local-by-default`. Check with `numastat -p <pid>`.

The interactive mode shows a core map (one row per SMT thread, grouped in blocks of 8),
lists each socket's free capacity and local GPUs, and pre-selects the GPU's socket.

```
 Socket 0                                                     48/64 isolated CPUs free
      0-63  ········ ········ ········ ········ ········ ········ ········ ········
   128-191  AAAAAAAA AAAAAAAA □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□

 Socket 1  ◆ NVIDIA GPU a1:00.0                              120/128 isolated CPUs free
    64-127  ■■■■■■■■ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□
   192-255  □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□ □□□□□□□□

   □ free  ■ yours  · shared/OS  A alice

Which socket?
  1. Socket 0  48 cores free
❯ 2. Socket 1  120 cores free · NVIDIA GPU a1:00.0
  ↑/↓ or j/k move · 1-9 jump · enter select · q cancel
```

**c. Exiting isolated cores**

Run `sudo lma` again and choose **Release my cores**, or run `sudo lma --release`.

Allocations are cleared on every boot by `lma-reset.service`.

## 🚀 Releasing (maintainers)

```bash
(cd lma-src && dch -i)       # new changelog entry; also bump LMA_VERSION in lma-src/src/lib/lmalib.py
./push-deb.sh --build-only   # checks + signed source package, no upload
./push-deb.sh                # checks, build, confirm, upload to ppa:meetesh06/radio-banana
./clean.sh                   # remove build output (--dry-run to list it)
```

Before building, `push-deb.sh` checks:
* the tools are installed and a GPG key exists for the changelog maintainer;
* `LMA_VERSION` matches the changelog;
* the version is newer than everything already in the PPA (Launchpad never accepts a version twice).

It creates `lma_<version>.orig.tar.gz` (or, for a `-0ubuntuN` re-upload, downloads the published one) and
includes all changelog entries since the PPA's newest version.
