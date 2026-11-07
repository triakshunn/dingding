#!/bin/sh
# Usage: run-suite.sh NAME <coverage run args...>
# Runs one test suite under coverage; on failure, posts the output tail as a
# GitHub annotation (visible on the run page and through the public API).
name="$1"; shift
log="$(mktemp)"
if uv run --with coverage==7.6.4 --with pytest coverage run "$@" >"$log" 2>&1; then
  tail -n 5 "$log"
  exit 0
fi
cat "$log"
msg="$(tail -c 3500 "$log" | sed -e 's/%/%25/g' -e 's/\r/%0D/g' | awk '{printf "%s%%0A", $0}')"
echo "::error title=$name tests failed::$msg"
exit 1
