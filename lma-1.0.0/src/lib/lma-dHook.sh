#!/bin/bash
USER_ID=$1
START_CORE=$2
END_CORE=$3
SIZE=$4

echo "--- LMA deallocation hook ---"
echo "🗑️ Core block DEALLOCATED."
echo "User ID: ${USER_ID}"
echo "Core ID Range: ${START_CORE} to ${END_CORE}"
echo "Number of Cores: ${SIZE}"

rm "/etc/systemd/system/user-$USER_ID.slice.d/90-AllowedCPUs.conf"
systemctl daemon-reload
systemctl restart user-$USER_ID.slice

echo "--- Hook End ---"