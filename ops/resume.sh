#!/bin/sh
# Turn the agent back on after the usage breaker (or a manual --pause) stopped it.
# Clears the latch and starts the OpenClaw container. Run: sh ~/dingding/ops/resume.sh
set -e
cd "$HOME/dingding"
.venv/bin/python ops/usage_monitor.py --resume
docker inspect -f 'openclaw: {{.State.Status}}' openclaw
