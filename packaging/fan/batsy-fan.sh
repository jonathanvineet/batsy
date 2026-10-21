#!/bin/bash
# Fan curve for Dell laptops (dell_smm_hwmon) used with the lid closed, where the BIOS holds the fan at half speed
# and the CPU cooks. Full speed from HOT degrees C, back to half below COOL. Hands control back to the BIOS on exit.
HOT=${HOT:-72}
COOL=${COOL:-58}
SMM=$(grep -l '^dell_smm$' /sys/class/hwmon/hwmon*/name | head -1 | xargs dirname)
CPU=$(grep -l '^coretemp$' /sys/class/hwmon/hwmon*/name | head -1 | xargs dirname)
[ -n "$SMM" ] && [ -n "$CPU" ] || { echo "dell_smm or coretemp sensor not found" >&2; exit 1; }

restore() { echo 2 > "$SMM/pwm1_enable" 2>/dev/null; exit 0; }
trap restore EXIT INT TERM

echo 1 > "$SMM/pwm1_enable" 2>/dev/null  # take the fan from the BIOS if it allows; otherwise keep re-setting it
level=128
while true; do
  t=$(( $(cat "$CPU/temp1_input") / 1000 ))
  if [ "$t" -ge "$HOT" ]; then level=255; elif [ "$t" -le "$COOL" ]; then level=128; fi
  echo "$level" > "$SMM/pwm1"
  sleep 1
done
