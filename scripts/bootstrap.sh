#!/usr/bin/env bash
set -euo pipefail

echo "=========================================================="
echo "  PRIMENODE ANTI-AGENTS BASELINE PROVISIONER"
echo "=========================================================="

# 1. Check Root
if [ "$(id -u)" -ne 0 ]; then
    echo "[-] Error: bootstrap.sh must be run as root." >&2
    exit 1
fi

# 2. System Packages
echo "[*] Installing required system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv wireguard-tools acl curl jq

# 3. Open Policy Agent (OPA)
if ! command -v opa &>/dev/null; then
    echo "[*] Installing Open Policy Agent binary..."
    curl -sL -o /usr/local/bin/opa https://openpolicyagent.org/downloads/v0.68.0/opa_linux_amd64_static
    chmod 755 /usr/local/bin/opa
fi
echo "[+] OPA version: $(opa version | head -n 1)"

# 4. User and Directory Setup
echo "[*] Configuring unprivileged antiworker user..."
if ! id -u antiworker &>/dev/null; then
    useradd -r -s /usr/sbin/nologin -d /var/lib/anti-agents antiworker
fi

mkdir -p /var/lib/anti-agents/worktrees
chown -R antiworker:antiworker /var/lib/anti-agents
chmod 700 /var/lib/anti-agents/worktrees

mkdir -p /var/log/anti-agents
chown -R antiworker:antiworker /var/log/anti-agents
chmod 755 /var/log/anti-agents

mkdir -p /etc/anti-agents/policies
cp policies/anti_policy.rego /etc/anti-agents/policies/anti_policy.rego
chmod 644 /etc/anti-agents/policies/anti_policy.rego

# 5. Systemd Units and Sudo Helper
echo "[*] Installing systemd units and narrow sudo helper..."
cp systemd/anti-gateway-helper /etc/sudoers.d/anti-gateway-helper
chmod 440 /etc/sudoers.d/anti-gateway-helper

cp systemd/anti-supervisor.service /etc/systemd/system/anti-supervisor.service
cp systemd/anti-control-gateway.service /etc/systemd/system/anti-control-gateway.service
systemctl daemon-reload

echo "[+] PrimeNode anti-agents baseline environment successfully provisioned!"
