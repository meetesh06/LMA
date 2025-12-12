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

*WIP, waiting for the package to be published...*
