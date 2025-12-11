#!/bin/bash

# --- 1. Slice Configuration Cleanup ---

echo "Cleaning up cgroup slice overrides..."
rm -f /etc/systemd/system/init.scope.d/40-cpulimit.conf
rm -f /etc/systemd/system/system.slice.d/40-cpulimit.conf
rm -f /etc/systemd/system/user.slice.d/40-cpulimit.conf
rm -f /etc/systemd/system/user-.slice.d/40-cpulimit.conf

# --- 2. Remove all dynamic user slice reservations ---

SLICE_DIR="/etc/systemd/system/" 
echo "Removing dynamic user slice reservations..."

find "$SLICE_DIR" -maxdepth 1 -type d \
    -name 'user-*.slice.d' \
    ! -name 'user-.slice.d' \
    -exec bash -c 'echo "deleting {}" && rm -f {}/*' \;

# --- 3. Other Configuration Cleanup ---

echo "Cleaning up GRUB configuration..."
rm -f /etc/default/grub.d/cgroup.cfg

echo "Removing sudoers directive..."
rm -f /etc/sudoers.d/lma

echo "Removing systemd service file..."
rm -f /etc/systemd/system/lma-reset.service

# --- 4. Reload Daemon and Restart Services ---

echo "Reloading systemd daemon and restarting slices..."
systemctl daemon-reload || true
systemctl restart user.slice || true

update-grub