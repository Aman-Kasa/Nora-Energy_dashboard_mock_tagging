#!/usr/bin/env bash
# Install the energy gateway on a fresh Raspberry Pi OS (Bookworm, 64-bit) on a Pi 5.
# Run from the gateway/ folder:  sudo ./install.sh
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

apt-get update
apt-get install -y python3-venv python3-lgpio sqlite3 ufw unattended-upgrades

# Service user with access to serial ports and GPIO only
id energy &>/dev/null || useradd --system --home /var/lib/energy-gateway --shell /usr/sbin/nologin energy
usermod -aG dialout,gpio energy

install -d -o energy -g energy -m 750 /var/lib/energy-gateway
install -d -m 755 /opt/energy-gateway /etc/energy-gateway
cp ./*.py requirements.txt /opt/energy-gateway/
[[ -f /etc/energy-gateway/config.yaml ]] || cp config.example.yaml /etc/energy-gateway/config.yaml
touch /etc/energy-gateway/mqtt.pass
chown root:energy /etc/energy-gateway/mqtt.pass /etc/energy-gateway/config.yaml
chmod 640 /etc/energy-gateway/mqtt.pass /etc/energy-gateway/config.yaml

python3 -m venv --system-site-packages /opt/energy-gateway/venv
/opt/energy-gateway/venv/bin/pip install -r /opt/energy-gateway/requirements.txt

cp systemd/energy-gateway.service /etc/systemd/system/

# Hardware watchdog: reboot the Pi if the OS itself hangs
if ! grep -q '^RuntimeWatchdogSec' /etc/systemd/system.conf; then
  echo 'RuntimeWatchdogSec=15' >> /etc/systemd/system.conf
fi

# Firewall: nothing inbound except SSH (ideally only over the VPN interface)
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0 to any port 22 proto tcp || true
ufw allow 22/tcp
ufw --force enable

systemctl daemon-reload
systemctl enable energy-gateway

cat <<'MSG'

Installed. Next:
  1. Edit /etc/energy-gateway/config.yaml (serial ports: ls -l /dev/serial/by-id/, shops, MQTT host)
  2. Put the MQTT password in /etc/energy-gateway/mqtt.pass and the broker CA in /etc/energy-gateway/ca.crt
  3. Test one bus by hand:  sudo -u energy /opt/energy-gateway/venv/bin/python /opt/energy-gateway/scan_bus.py --port <port>
  4. sudo systemctl start energy-gateway && journalctl -u energy-gateway -f
  5. After testing, remove 'ufw allow 22/tcp' so SSH works only over Tailscale.
MSG
