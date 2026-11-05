#!/bin/bash
# LMA allocation hook.
# Usage: lma-aHook.sh <uid> <cpulist> <ncpus> <socket> [<numa-nodes>|none]
set -euo pipefail

USER_ID=$1
CPUS=$2
NCPUS=$3
SOCKET=$4
MEMS=${5:-none}

[[ "$USER_ID" =~ ^[0-9]+$ ]] || { echo "invalid uid: $USER_ID" >&2; exit 1; }
[[ "$CPUS" =~ ^[0-9,-]+$ ]] || { echo "invalid cpulist: $CPUS" >&2; exit 1; }
[[ "$MEMS" == none || "$MEMS" =~ ^[0-9,-]+$ ]] || { echo "invalid NUMA node list: $MEMS" >&2; exit 1; }

echo "--- LMA allocation hook ---"
echo "User ID: ${USER_ID}"
echo "CPUs: ${CPUS} (${NCPUS} CPUs, socket ${SOCKET})"
echo "Memory nodes: ${MEMS}"

# Strict NUMA binding (cpuset.mems) when requested; empty resets to "all nodes".
MEM_LINES="AllowedMemoryNodes="
if [ "$MEMS" != none ]; then
    MEM_LINES="AllowedMemoryNodes=
AllowedMemoryNodes=${MEMS}"
fi

DIR="/etc/systemd/system/user-${USER_ID}.slice.d"
mkdir -p "$DIR"
cat << EOF2 > "$DIR/.90-AllowedCPUs.conf.tmp"
[Unit]
Description=LMA allocation ${CPUS} (socket ${SOCKET})

[Slice]
AllowedCPUs=
AllowedCPUs=${CPUS}
${MEM_LINES}
EOF2
mv -f "$DIR/.90-AllowedCPUs.conf.tmp" "$DIR/90-AllowedCPUs.conf"

systemctl daemon-reload
systemctl restart "user-${USER_ID}.slice"
echo "--- Hook End ---"
