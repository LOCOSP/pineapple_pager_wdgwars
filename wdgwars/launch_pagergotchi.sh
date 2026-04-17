#!/bin/sh
# Title: Exit to PagerGotchi
# Requires: /root/payloads/user/reconnaissance/pagergotchi/run_pagergotchi.py

TARGET="/root/payloads/user/reconnaissance/pagergotchi"
[ ! -f "$TARGET/run_pagergotchi.py" ] && echo "PagerGotchi not installed" && exit 1

export PATH="/mmc/usr/bin:$PATH"
export PYTHONPATH="$TARGET/lib:$TARGET:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$TARGET/lib:$TARGET:$LD_LIBRARY_PATH"
export CRYPTOGRAPHY_OPENSSL_NO_LEGACY=1

if pgrep -x pineapple >/dev/null; then
    /etc/init.d/pineapplepager stop 2>/dev/null
    sleep 0.3
fi

for _p in wifman.py loki_menu.py Bjorn.py bjorn_menu.py wdgwars.py; do
    for _pid in $(ps | awk "/$_p/ && !/awk/ {print \$1}"); do
        kill $_pid 2>/dev/null
    done
done
sleep 0.2

cd "$TARGET"
python3 run_pagergotchi.py
exit $?
