# 80% battery charge cap (ThinkPad, always plugged in)

Run in a terminal (asks for your password):
```
echo 80 | sudo tee /sys/class/power_supply/BAT0/charge_control_end_threshold
printf '[Unit]\nDescription=Battery charge cap 80%%\n[Service]\nType=oneshot\nExecStart=/bin/sh -c "echo 80 > /sys/class/power_supply/BAT0/charge_control_end_threshold"\n[Install]\nWantedBy=multi-user.target\n' | sudo tee /etc/systemd/system/battery-cap.service >/dev/null
sudo systemctl enable battery-cap.service
```
Check: `cat /sys/class/power_supply/BAT0/charge_control_end_threshold` → `80` (also after a reboot).
Undo: `sudo systemctl disable battery-cap.service && echo 100 | sudo tee /sys/class/power_supply/BAT0/charge_control_end_threshold`
