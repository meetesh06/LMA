# 💻 LMA (Leave Me Alone)

A simple utility for (cgroup v2) that allows users to:

1. Dedicate specific cpu cores for shared operation.
2. Isolate specific cpu cores for sensitive applications.
3. Allows users to enter/exit isolated core region.

---

## ✨ Features

* **Core Isolation:** Dedicates a specified range of CPU cores for exclusive application use, preventing kernel scheduling interference.
* **IRQ Affinity Management:** Set IRQ affinity to shared core region.
* **Sudoers Integration:** Optionally sets up a dedicated user group (`lma`) and configures `sudoers` for secure, controlled execution of LMA commands. This allows non-sudo users to execute `sudo lma`. 

## 🛠️ Configuration

LMA is configured using a single, structured JSON file, typically named `config.json`.

### Example `config.json`

```json
{
  "cpu": {
    "total": "0-255",
    "shared": "0-63",
    "isolated": "64-255"
  },
  "coreGroupSizes": [
    8,
    16,
    32,
    64
  ],
  "setIRQAffinityToShared": true,
  "addLMAGroupToSudoers": true
}
```

| Parameter              | Type         | Description                                                                                                                                      |
|------------------------|--------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| cpu.total              | string       | Total available cores on the system (e.g., 0-255)                                                                                                |
| cpu.shared             | string       | Cores shared for the OS kernel, general background tasks, and I/O handling (e.g.,0-63)                                                           |
| cpu.isolated           | string       | Cores isolated from all system and general user processes.                                                                                       |
| coreGroupSizes         | array of int | The core allocation sizes the allocator is allowed to provide the users.                                                                         |
| setIRQAffinityToShared | boolean      | If true, the utility adjusts system IRQ affinity settings to route interrupts away from isolated cores and onto the cpu.shared cores             |
| addLMAGroupToSudoers   | boolean      | If true, a system group is created and configured in sudoers to allow specific users to run lma commands without needing a full password prompt. |

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

```
meetesh@server:~$ sudo lma
LMA Start.
   * Invoking User Name: **meetesh**
   * Invoking User UID: **1002**
--- LMA Core allocation for User ID: 1002 ---
Initializing database file: /usr/lib/lma/lmaAllocations.csv

==================================================
Isolated Cores: [64 - 255]
==================================================
  [ 64 - 255] -> FREE Cores (192 units)
==================================================


Choose an allocation size (Cores):
  (a) 8 cores
  (b) 16 cores
  (c) 32 cores
  (d) 64 cores
  (e) Keep existing cores and Exit
Enter your choice (e.g., 'a'): c

[Allocation Hook Output]
--- LMA allocation hook ---
User ID: 1002
Address Range: 64 to 95
Size: 32 units
--- Hook End ---

##################################################
✅ SUCCESSFULLY ALLOCATED 32 cores to User 1002
   Core ID Range: **[64 - 95]**
##################################################

==================================================
Isolated Cores: [64 - 255]
==================================================
  [ 64 -  95] -> ALLOCATED (ID: 1002, Cores: 32)
  [ 96 - 255] -> FREE Cores (160 units)
==================================================

meetesh@server:~$ 
```

**c. Exiting isolated cores**

```
meetesh@server:~$ sudo lma
LMA Start.
   * Invoking User Name: **meetesh**
   * Invoking User UID: **1002**
--- LMA Core allocation for User ID: 1002 ---

==================================================
Isolated Cores: [64 - 255]
==================================================
  [ 64 -  95] -> ALLOCATED (ID: 1002, Cores: 32)
  [ 96 - 255] -> FREE Cores (160 units)
==================================================

⚠️ User 1002 currently holds core block: **[64 - 95]** (Cores: 32)

Choose an action:
  (a) Deallocate existing cores
  (b) Keep existing cores and Exit
Enter your choice: a

[Deallocation Hook Output]
--- LMA deallocation hook ---
🗑️ Core block DEALLOCATED.
User ID: 1002
Core ID Range: 64 to 95
Number of Cores: 32
--- Hook End ---
```