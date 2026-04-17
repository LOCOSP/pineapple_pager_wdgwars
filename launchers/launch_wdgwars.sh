#!/bin/sh
# Title: Exit to WDGoWars
# Requires: /root/payloads/user/reconnaissance/wdgwars/wdgwars.py

TARGET="/root/payloads/user/reconnaissance/wdgwars"
[ ! -f "$TARGET/wdgwars.py" ] && echo "WDGoWars not installed" && exit 1

export PATH="/mmc/usr/bin:$PATH"
export PYTHONPATH="$TARGET/lib:$TARGET:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$TARGET/lib:$TARGET:$LD_LIBRARY_PATH"
export CRYPTOGRAPHY_OPENSSL_NO_LEGACY=1
export WDGWARS_PAYLOAD_DIR="$TARGET"

if pgrep -x pineapple >/dev/null; then
    /etc/init.d/pineapplepager stop 2>/dev/null
    sleep 0.3
fi

for _p in wifman.py loki_menu.py run_pagergotchi.py Bjorn.py bjorn_menu.py; do
    for _pid in $(ps | awk "/$_p/ && !/awk/ {print \$1}"); do
        kill $_pid 2>/dev/null
    done
done
sleep 0.2

cd "$TARGET"
python3 wdgwars.py
exit $?
