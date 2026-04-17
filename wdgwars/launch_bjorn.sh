#!/bin/bash
# Title: Exit to Bjorn
# Requires: /root/payloads/user/reconnaissance/pager_bjorn/Bjorn.py

TARGET="/root/payloads/user/reconnaissance/pager_bjorn"
[ ! -f "$TARGET/Bjorn.py" ] && echo "Bjorn not installed" && exit 1

export PATH="/mmc/usr/bin:$PATH"
export PYTHONPATH="$TARGET/lib:$TARGET:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$TARGET/lib:$TARGET:$LD_LIBRARY_PATH"
export CRYPTOGRAPHY_OPENSSL_NO_LEGACY=1

if pgrep -x pineapple >/dev/null; then
    /etc/init.d/pineapplepager stop 2>/dev/null
    sleep 0.3
fi

for _p in wifman.py loki_menu.py run_pagergotchi.py wdgwars.py; do
    for _pid in $(ps | awk "/$_p/ && !/awk/ {print \$1}"); do
        kill $_pid 2>/dev/null
    done
done
sleep 0.2

# Bjorn wants BJORN_INTERFACE / BJORN_IP — auto-pick the first non-lo iface.
SELECTED_INTERFACE=""
SELECTED_IP=""
while IFS= read -r line; do
    if [[ "$line" =~ ^[0-9]+:\ ([^:]+): ]]; then
        CURRENT_IFACE="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ inet\ ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) ]]; then
        IP="${BASH_REMATCH[1]}"
        if [[ "$IP" != "127.0.0.1" && -n "$CURRENT_IFACE" && -z "$SELECTED_INTERFACE" ]]; then
            SELECTED_INTERFACE="$CURRENT_IFACE"
            SELECTED_IP="$IP"
        fi
    fi
done < <(ip addr 2>/dev/null)
export BJORN_INTERFACE="${SELECTED_INTERFACE:-br-lan}"
export BJORN_IP="${SELECTED_IP:-172.16.52.1}"

cd "$TARGET"
python3 Bjorn.py
exit $?
