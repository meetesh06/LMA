#!/bin/bash
USER_ID=$1
START_ADDR=$2
END_ADDR=$3
SIZE=$4

echo "--- LMA allocation hook ---"
echo "User ID: ${USER_ID}"
echo "Address Range: ${START_ADDR} to ${END_ADDR}"
echo "Size: ${SIZE} units"

mkdir -p /etc/systemd/system/user-$USER_ID.slice.d
cat << EOF > "/etc/systemd/system/user-$USER_ID.slice.d/90-AllowedCPUs.conf"
[Unit]
Description=LMA allocation $START_ADDR-$END_ADDR

[Slice]
AllowedCPUs=
AllowedCPUs=$START_ADDR-$END_ADDR
EOF

systemctl daemon-reload
systemctl restart user-$USER_ID.slice
echo "--- Hook End ---"
