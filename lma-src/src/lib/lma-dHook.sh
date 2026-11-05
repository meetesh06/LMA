#!/bin/bash
# LMA deallocation hook.
# Usage: lma-dHook.sh <uid> <cpulist> <ncpus> <socket> [<numa-nodes>|none]
set -euo pipefail

USER_ID=$1
CPUS=$2
NCPUS=$3
SOCKET=$4
MEMS=${5:-none}

[[ "$USER_ID" =~ ^[0-9]+$ ]] || { echo "invalid uid: $USER_ID" >&2; exit 1; }

echo "--- LMA deallocation hook ---"
echo "User ID: ${USER_ID}"
echo "CPUs: ${CPUS} (${NCPUS} CPUs, socket ${SOCKET}), memory nodes: ${MEMS}"

# -f: a missing drop-in must not leave the user stuck with a phantom allocation.
rm -f "/etc/systemd/system/user-${USER_ID}.slice.d/90-AllowedCPUs.conf"
systemctl daemon-reload
systemctl restart "user-${USER_ID}.slice"

echo "--- Hook End ---"
