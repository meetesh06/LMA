#!/usr/bin/env python3
import argparse
import json
import sys
import fcntl
from pathlib import Path
import os
import subprocess
import shutil

if os.geteuid() != 0:
    print("This script must be run as root!")
    sys.exit(1)

LMA_VERSION = "1.0"

LMA_DATA = {
    "version": LMA_VERSION,
    "cpu": {
        "total": None,
        "shared": None,
        "isolated": None,
    },
    "coreGroupSizes": [],
    "setIRQAffinityToShared": False,
    "addLMAGroupToSudoers": False
}

SLICE_DIR = "/etc/systemd/system/"
SUDOERS_DIR = "/etc/sudoers.d"
LMA_PATH="/usr/sbin/lma"

DATABASE_FILE = "/usr/lib/lma/lmaAllocations.csv"
LMA_DATA_PATH = "/usr/lib/lma/lmaData"
RESET_CORE_ALLOC_SCRIPT_PATH = "/usr/lib/lma/lma-reset.sh"

AHOOK_SCRIPT_PATH = "/usr/lib/lma/lma-aHook.sh"
DHOOK_SCRIPT_PATH = "/usr/lib/lma/lma-dHook.sh"

RESET_CORE_ALLOC_SERVICE_PATH = "/etc/systemd/system/lma-reset.service"
GRUB_IRQ_PATH = "/etc/default/grub.d/cgroup.cfg"
SUDOERS_RULE = "/etc/sudoers.d/lma"

def readJSON(jsonPath):    
    if not os.path.exists(jsonPath):
        print(f"Error: Configuration file not found at '{jsonPath}'", file=sys.stderr)
        sys.exit(1)
    try:
        with open(jsonPath, 'r') as f:
            config = json.load(f)
        return config
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON format in '{jsonPath}'. Details: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred while reading the file: {e}", file=sys.stderr)
        sys.exit(1)

def ensureKey(config, key):
    if key not in config:
        raise KeyError(f'"{key}" key is required in the config')

def ensureType(v, t):
    if not isinstance(v, t):
        raise TypeError(f'"{v}" must be a {t} (got {type(v).__name__})')

def doDefaultAllocation():
    initScopeConfigData=f"""
[Slice]
AllowedCPUs={LMA_DATA["cpu"]["shared"][0]}-{LMA_DATA["cpu"]["shared"][1]}
CPUAffinity={LMA_DATA["cpu"]["shared"][0]}-{LMA_DATA["cpu"]["shared"][1]}
"""
    systemSliceConfigData=f"""
[Slice]
AllowedCPUs={LMA_DATA["cpu"]["shared"][0]}-{LMA_DATA["cpu"]["shared"][1]}
CPUAffinity={LMA_DATA["cpu"]["shared"][0]}-{LMA_DATA["cpu"]["shared"][1]}
"""

    userSliceConfigData=f"""
[Slice]
AllowedCPUs={LMA_DATA["cpu"]["total"][0]}-{LMA_DATA["cpu"]["total"][1]}
CPUAffinity={LMA_DATA["cpu"]["total"][0]}-{LMA_DATA["cpu"]["total"][1]}
"""

    userSliceMaskConfigData=f"""
[Slice]
AllowedCPUs={LMA_DATA["cpu"]["shared"][0]}-{LMA_DATA["cpu"]["shared"][1]}
CPUAffinity={LMA_DATA["cpu"]["shared"][0]}-{LMA_DATA["cpu"]["shared"][1]}
"""
    
    dirs_to_remove = [
        f"{SLICE_DIR}/init.scope.d",
        f"{SLICE_DIR}/system.slice.d",
        f"{SLICE_DIR}/user.slice.d",
        f"{SLICE_DIR}/user-.slice.d"
    ]

    for dir_path in dirs_to_remove:
        try:
            shutil.rmtree(dir_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error removing directory {dir_path}: {e}")
            exit(1)
    
    files_to_create = {
        f"{SLICE_DIR}/init.scope.d/40-cpulimit.conf": initScopeConfigData,
        f"{SLICE_DIR}/system.slice.d/40-cpulimit.conf": systemSliceConfigData,
        f"{SLICE_DIR}/user.slice.d/40-cpulimit.conf": userSliceConfigData,
        f"{SLICE_DIR}/user-.slice.d/40-cpulimit.conf": userSliceMaskConfigData, 
    }

    for file_path, content in files_to_create.items():
        dir_path = os.path.dirname(file_path)
        
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception as e:
            print(f"Error creating directory {dir_path}: {e}")
            exit(1)
        
        try:
            with open(file_path, "w") as f:
                f.write(content)
        except Exception as e:
            print(f"Error writing to file {file_path}: {e}")
            exit(1)


def doMakeResetUserCoreAllocationScript():
    resetCoreAllocation = f"""#!/bin/bash

SLICE_DIR="/etc/systemd/system/" 

# Reset core allocations
rm -f {DATABASE_FILE}

# Remove all user reservations
find "$SLICE_DIR" -maxdepth 1 -type d \
    -name 'user-*.slice.d' \
    ! -name 'user-.slice.d' \
    -exec bash -c 'echo "deleting {{}}" && rm -f {{}}/*' \;

# Reload the daemon
systemctl daemon-reload
systemctl restart user.slice
exit 0
    """

    with open(RESET_CORE_ALLOC_SCRIPT_PATH, 'w') as file:
        file.write(resetCoreAllocation)

def doMakeResetUserCoreAllocationService():
    resetCoreAllocationService = f"""[Unit]
Description=LMA cleanup user slices
After=multi-user.target

[Service]
Type=oneshot
ExecStart={RESET_CORE_ALLOC_SCRIPT_PATH}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
    """

    with open(RESET_CORE_ALLOC_SERVICE_PATH, 'w') as file:
        file.write(resetCoreAllocationService)

def addGrubFlagForIRQAffinity():
    data = f"""
GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT irqaffinity={LMA_DATA["cpu"]["shared"]}"
"""
    os.makedirs(os.path.dirname(GRUB_IRQ_PATH), exist_ok=True)
    with open(GRUB_IRQ_PATH, "w") as f:
        f.write(data)

def addLmaSudoRule():
    sudoers_dir = SUDOERS_DIR
    rule_file = SUDOERS_RULE
    rule_content = f"%lma ALL=(root) NOPASSWD: {LMA_PATH}\n"

    # Ensure directory exists (it always should, but we check)
    if not os.path.isdir(sudoers_dir):
        raise Exception(f"{sudoers_dir} does not exist on this system")

    # Write rule to a temporary file first
    tmp_file = "/tmp/lma_sudo_rule"
    with open(tmp_file, "w") as f:
        f.write(rule_content)

    # Validate using visudo
    try:
        subprocess.check_call(["visudo", "-cf", tmp_file])
    except subprocess.CalledProcessError:
        raise Exception("visudo validation failed! Not updating sudoers.")

    # Move into place with correct permissions
    subprocess.check_call(["sudo", "cp", tmp_file, rule_file])
    subprocess.check_call(["sudo", "chmod", "440", rule_file])

def getNumbers(s):
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError(f"Expected exactly two numbers separated by '-', got: {s}")
    return [int(x) for x in parts]

def init(jsonPath: Path):
    config = readJSON(jsonPath)

    # cpu
    ensureKey(config, "cpu")
    ensureType(config["cpu"], dict)
    ensureKey(config["cpu"], "total")
    ensureKey(config["cpu"], "shared")
    ensureKey(config["cpu"], "isolated")

    LMA_DATA["cpu"]["total"] = getNumbers(config["cpu"]["total"])
    LMA_DATA["cpu"]["shared"] = getNumbers(config["cpu"]["shared"])
    LMA_DATA["cpu"]["isolated"] = getNumbers(config["cpu"]["isolated"])

    # coreGroupSizes
    ensureKey(config, "coreGroupSizes")
    ensureType(config["coreGroupSizes"], list)
    for size in config["coreGroupSizes"]:
        ensureType(size, int)
        LMA_DATA["coreGroupSizes"].append(size)

    # setIRQAffinityToShared
    ensureKey(config, "setIRQAffinityToShared")
    ensureType(config["setIRQAffinityToShared"], bool)
    LMA_DATA["setIRQAffinityToShared"] = config["setIRQAffinityToShared"]

    # addLMAGroupToSudoers
    ensureKey(config, "addLMAGroupToSudoers")
    ensureType(config["addLMAGroupToSudoers"], bool)
    LMA_DATA["addLMAGroupToSudoers"] = config["addLMAGroupToSudoers"]

    # GENERATE
    # 1. Core allocation scripts
    doMakeResetUserCoreAllocationScript()
    doMakeResetUserCoreAllocationService()
    doDefaultAllocation()

    # 2. Set IRQ affinity in GRUB
    if LMA_DATA["setIRQAffinityToShared"]:
        addGrubFlagForIRQAffinity()
    
    # 3. Add sudoers rule
    if LMA_DATA["addLMAGroupToSudoers"]:
        addLmaSudoRule()

    try:
        subprocess.run(['sudo', 'chmod', '+x', RESET_CORE_ALLOC_SCRIPT_PATH], check=True)
        subprocess.run(['sudo', 'chmod', '+x', AHOOK_SCRIPT_PATH], check=True)
        subprocess.run(['sudo', 'chmod', '+x', DHOOK_SCRIPT_PATH], check=True)

        subprocess.run(['sudo', 'systemctl', 'daemon-reload'], check=True)
        subprocess.run(['sudo', 'systemctl', 'enable', 'lma-reset.service'], check=True)
        subprocess.run(['sudo', 'systemctl', 'start', 'lma-reset.service'], check=True)


        if LMA_DATA["setIRQAffinityToShared"]:
            subprocess.run(['sudo', 'update-grub'], check=True)
        
        if LMA_DATA["addLMAGroupToSudoers"]:
            subprocess.run(["sudo", "groupadd", "-f", "lma"], check=True)

        subprocess.run(["sudo", "rm", "-f", DATABASE_FILE], check=True)    

        with open(LMA_DATA_PATH, "w") as f:
            json.dump(LMA_DATA, f, indent=4)

        print("LMA setup successful.")
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {e}")


def main():
    parser = argparse.ArgumentParser(
        prog='lma-init',
        description='Leave Me Alone (LMA) - A simple utility for core isolation in cgroup v2.',
        epilog='Use "lma-init config.json -h" for more information on a command.'
    )
    parser.add_argument("path", type=Path)
    parser.set_defaults(func=init)
    args = parser.parse_args()
    args.func(args.path)

if __name__ == "__main__":
    if os.name != 'posix':
        print("Error: This script's locking mechanism relies on POSIX systems (Linux, macOS).", file=sys.stderr)
        sys.exit(1)
    main()