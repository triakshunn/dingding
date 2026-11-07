#!/bin/sh
# Keep the agent laptop running with the lid closed (it is a 24/7 server; battery is capped at 75-80%).
# Run once:  sudo sh ~/dingding/ops/lid.sh      Undo:  sudo rm /etc/systemd/logind.conf.d/agent-lid.conf
set -e
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/agent-lid.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
# Reload logind's settings without ending your login sessions.
systemctl kill -s HUP systemd-logind
echo "Done: closing the lid no longer suspends the laptop."
grep -h HandleLid /etc/systemd/logind.conf.d/agent-lid.conf
