# Detail of LMA Setup (Leave Me Alone)


This command reads config.json, performs core isolation setup, and handles system-level changes like IRQ affinity and sudoers file modification. This must be run with elevated privileges.

```bash
sudo lma-setup config.json
```

## Understanding the Setup Process

To better understand what happens during execution, let's look at the setup process in detail. The lma-setup command performs several system-level operations, including configuration validation, service creation, CPU allocation setup, and optional system modifications based on the provided configuration file. The following sections describe each step of this process.

## 1. Configuration Validation

Before applying any changes, lma-setup validates the structure of the provided config.json file to ensure it follows the expected format. This prevents invalid configurations from causing incorrect system behavior.



| Field | Expected Type |
|------|---------------|
| `cpu` | object (dictionary) |
| `coreGroupSizes` | list |
| `setIRQAffinityToShared` | boolean |
| `addLMAGroupToSudoers` | boolean |

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

## 2. Reset Script Creation

`lma-setup` generates a reset script (`lma-reset.sh`) that serves as the entry point for the systemd service responsible for restoring default CPU allocations.

This script resets **cgroup v2 cpuset constraints** that were applied through systemd slice configurations. It does this by rewriting the systemd slice drop-in configuration files, refreshing systemd’s internal configuration state, and restarting the `user.slice` service.

Restarting `user.slice` ensures that all user sessions, which run under this parent slice, inherit the updated CPU allocation rules.
```bash
<user>@server:/usr/lib/lma$ ls
lma-aHook.sh  lmaAllocations.csv  lmaData  lma-dHook.sh  lma.py  lma-reset.sh setup.py
```

## 3. Creating the Reset Service

`lma-setup` creates a systemd service called **`lma-reset.service`** (ResetUserCoreAllocationService) that is responsible for executing the reset script.

This service is configured as a **systemd `oneshot` service**, meaning it runs a specific task to completion and then exits instead of remaining active like a long-running daemon.

The service simply executes the generated **`lma-reset.sh` script**, which restores the default CPU allocation state by resetting the cpuset constraints applied through systemd slice configurations. The service is later enabled and started so that the reset logic can be applied when required.

```bash
<user>@server:/etc/systemd/system$ nano lma-reset.service 

[Unit]
Description=LMA cleanup user slices
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/lib/lma/lma-reset.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target

```
Once enabled, the service runs successfully and exits. A typical status output looks like:

```bash
<user>@server:systemctl status lma-reset
● lma-reset.service - LMA cleanup user slices
     Loaded: loaded (/etc/systemd/system/lma-reset.service; enabled; vendor preset: enabled)
     Active: active (exited) since Sun 2026-02-08 11:55:23 UTC; 3 weeks 2 days ago
   Main PID: 4343 (code=exited, status=0/SUCCESS)
        CPU: 36ms
```
The active (exited) state indicates that the oneshot service ran successfully and completed its task.

## 4. Systemd Slice Drop-in Configurations

`lma-setup` configures `default` CPU allocation policies by creating **systemd slice drop-in configuration files**. These files define which CPUs different system components are allowed to run on.

Systemd allows unit behavior to be modified using **drop-in directories** (`*.d/`). The setup script creates the following directories and configuration files:

| Slice | Drop-in Path | CPU Allocation |
|------|--------------|---------------|
| `init.scope` | `/etc/systemd/system/init.scope.d/40-cpulimit.conf` | shared CPUs |
| `system.slice` | `/etc/systemd/system/system.slice.d/40-cpulimit.conf` | shared CPUs |
| `user.slice` | `/etc/systemd/system/user.slice.d/40-cpulimit.conf` | total CPUs |
| `user-.slice` | `/etc/systemd/system/user-.slice.d/40-cpulimit.conf` | shared CPUs |

Each configuration file sets the following parameters:

- `AllowedCPUs`
- `CPUAffinity`

These drop-in configurations ensure that system services remain restricted to the shared CPU set while allowing controlled allocation of CPUs for user workloads.

Individual user login slices default to **shared CPUs unless explicitly overridden** by other allocation policies.

Below is an example of a generated drop-in configuration file:

```bash
/etc/systemd/system/user.slice.d/40-cpulimit.conf

[Slice]
AllowedCPUs=0-255
CPUAffinity=0-255
```

## 5. Configuring IRQ Affinity

If `setIRQAffinityToShared` is set in the config.json, `lma-setup` modifies the GRUB configuration to add the kernel parameter:
irqaffinity=<shared CPU range>

This parameter instructs the Linux kernel to **restrict hardware interrupt handling (IRQs)** to the specified shared CPU range. As a result, interrupt processing is prevented from running on the isolated cores, ensuring they remain free for dedicated workloads.

The setup script updates the GRUB configuration file that defines kernel boot parameters, typically located at :  `/etc/default/grub`
or
```/etc/default/grub.d/*.cfg```

## 6. Adding `lma` Group to Sudoers

If `addLMAGroupToSudoers` is enabled, the setup process creates a sudoers rule that allows members of the **`lma` group** to run the core-isolation command with root privileges without granting full sudo access.

This rule permits execution of the `lma` command as `root` without requiring a password, while restricting elevated privileges to that specific command.

The sudoers entry is written using **`visudo`** to ensure safe modification of sudo configuration files. `visudo` edits the sudoers configuration in a controlled manner by locking the file against simultaneous edits, performing basic validity checks, and verifying the syntax before the file is installed.

The resulting rule is placed under: `/etc/sudoers.d/lma`.

This approach follows the recommended practice of using the `/etc/sudoers.d/` directory for modular and maintainable sudo policy configuration.
