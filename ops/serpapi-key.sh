#!/bin/sh
# Save the SerpApi key for flight price watches on the laptop (never paste it into a chat).
#   sh ~/dingding/ops/serpapi-key.sh
set -e
f="$HOME/dingding/data/serpapi_key"
printf "Paste your SerpApi key (hidden), then Enter: "
stty -echo; read -r key; stty echo; echo
[ -n "$key" ] || { echo "No key entered; nothing saved."; exit 1; }
umask 077
printf '%s\n' "$key" > "$f"
chmod 600 "$f"
echo "Saved to $f (only you can read it). Price checks start within 10 minutes."
