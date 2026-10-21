#!/bin/bash
# Run with sudo. Tests manual fan control first; installs the fan curve only if the fan really responds.
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
SMM=$(grep -l '^dell_smm$' /sys/class/hwmon/hwmon*/name | head -1 | xargs dirname)
[ -n "$SMM" ] || { echo "no dell_smm fan sensor; can't control the fan"; exit 1; }

if systemctl is-active --quiet batsy-fan; then
  install -m 755 batsy-fan.sh /usr/local/bin/batsy-fan && systemctl restart batsy-fan
  echo "Fan curve is already installed and running (updated it). Undo: sudo systemctl disable --now batsy-fan"
  exit 0
fi
echo "Testing: fan to full speed for 8 seconds..."
before=$(cat "$SMM/fan1_input")
# Most Dells don't let us switch off BIOS fan control; then we keep re-setting the speed instead.
echo 1 > "$SMM/pwm1_enable" 2>/dev/null || echo "  (BIOS keeps fan control; re-setting the speed every second)"
for i in 1 2 3 4 5 6 7 8; do echo 255 > "$SMM/pwm1"; sleep 1; done
after=$(cat "$SMM/fan1_input")
echo 2 > "$SMM/pwm1_enable" 2>/dev/null || true
echo "  fan: $before RPM -> $after RPM"
if [ "$after" -le $((before + 500)) ] && [ "$after" -lt $(( $(cat "$SMM/fan1_max") * 85 / 100 )) ]; then
  echo "The BIOS didn't let the fan speed up, so the fan curve can't work here."
  echo "Setting the cooler power profile instead (less heat, a bit slower)."
  echo cool > /sys/firmware/acpi/platform_profile
  exit 0
fi
install -m 755 batsy-fan.sh /usr/local/bin/batsy-fan
install -m 644 batsy-fan.service /etc/systemd/system/batsy-fan.service
systemctl daemon-reload
systemctl enable --now batsy-fan.service
echo "Fan curve installed: full speed from 72°C, half speed below 58°C. Undo: sudo systemctl disable --now batsy-fan"
