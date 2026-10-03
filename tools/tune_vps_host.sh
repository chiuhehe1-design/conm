#!/usr/bin/env bash
# ==============================================================================
# PRIMENODE & ANTI HOST TUNING & SYSTEM HARDENING SCRIPT (P2-03)
# Applies production kernel sysctl, file descriptor limits, journald limits,
# and activates automated maintenance timer.
# ==============================================================================
set -euo pipefail

echo "[1/5] Applying kernel & network sysctl tuning..."
cat << 'EOF' > /etc/sysctl.d/99-primenode-tuning.conf
# PrimeNode & ANTI Autonomous VPS Production Tuning
vm.swappiness = 10
vm.vfs_cache_pressure = 50
vm.max_map_count = 262144

# Network socket backlog & connection limits
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 16384
net.core.netdev_max_backlog = 16384

# TCP keepalive and connection recycling
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_keepalive_time = 300
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 5

# Socket buffer window sizes
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# File descriptor & Inotify limits for agent workers
fs.file-max = 2097152
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 8192
EOF

sysctl -p /etc/sysctl.d/99-primenode-tuning.conf > /dev/null

echo "[2/5] Configuring security ulimits for antiworker and root..."
cat << 'EOF' > /etc/security/limits.d/99-antiworker.conf
* soft nofile 65536
* hard nofile 65536
* soft nproc 4096
* hard nproc 4096
antiworker soft nofile 65536
antiworker hard nofile 65536
antiworker soft nproc 4096
antiworker hard nproc 4096
root soft nofile 65536
root hard nofile 65536
EOF

echo "[3/5] Hardening systemd-journald log retention..."
mkdir -p /etc/systemd/journald.conf.d
cat << 'EOF' > /etc/systemd/journald.conf.d/00-journal-limit.conf
[Journal]
SystemMaxUse=500M
SystemKeepFree=2G
MaxRetentionSec=1month
EOF
systemctl restart systemd-journald
journalctl --vacuum-size=300M > /dev/null 2>&1 || true

echo "[4/5] Deploying hardened systemd service units..."
cp /opt/anti-agents/systemd/anti-control-gateway.service /etc/systemd/system/
cp /opt/anti-agents/systemd/anti-supervisor.service /etc/systemd/system/
cp /opt/anti-agents/systemd/anti-autonomous-daemon.service /etc/systemd/system/
cp /opt/anti-agents/systemd/anti-maintenance.service /etc/systemd/system/
cp /opt/anti-agents/systemd/anti-maintenance.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now anti-maintenance.timer

echo "[5/5] Restarting services with hardened resource constraints..."
systemctl restart anti-control-gateway anti-supervisor anti-autonomous-daemon

echo "✅ VPS tuning and hardening successfully applied!"
