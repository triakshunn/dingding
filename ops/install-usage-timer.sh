#!/bin/sh
# Install the 5-minute usage-monitor timer (usage breaker) and show when it runs next.
set -e
cp "$HOME/dingding/ops/deploy/usage-monitor.timer" "$HOME/.config/systemd/user/usage-monitor.timer"
systemctl --user daemon-reload
systemctl --user restart usage-monitor.timer
systemctl --user list-timers usage-monitor.timer --no-pager
