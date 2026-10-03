#!/usr/bin/env bash
# ==============================================================================
# PRIMENODE & ANTI VPS HOST SECURITY & SYSTEM HARDENING (FEATURE-018)
# - Deploys logrotate configuration for anti-agents
# - Installs and activates periodic snapshot backup timer (Every 12h)
# - Updates anti-iptables-restore with cgroup pre-creation guard
# - Applies UFW rate-limiting on SSH port 22
# ==============================================================================
set -euo pipefail

echo "[1/4] Installing logrotate configuration for /var/log/anti-agents..."
cp /opt/anti-agents/logrotate/anti-agents /etc/logrotate.d/anti-agents
chmod 644 /etc/logrotate.d/anti-agents
logrotate -d /etc/logrotate.d/anti-agents > /dev/null 2>&1 || true

echo "[2/4] Hardening anti-iptables-restore systemd service..."
cp /opt/anti-agents/systemd/anti-iptables-restore.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart anti-iptables-restore.service

echo "[3/4] Deploying automated database snapshot backup timer..."
cp /opt/anti-agents/systemd/anti-snapshot-backup.service /etc/systemd/system/
cp /opt/anti-agents/systemd/anti-snapshot-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now anti-snapshot-backup.timer

echo "[4/4] Hardening SSH port 22 with UFW rate-limiting..."
# UFW rate limit: rejects IPs that attempt 6+ connections within 30 seconds
ufw limit 22/tcp > /dev/null 2>&1 || true

echo "✅ VPS Host Security & Automation hardening applied successfully!"
