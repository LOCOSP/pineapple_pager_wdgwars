#!/bin/bash
# Title: Exit to PagerGotchi
# Requires: /root/payloads/user/reconnaissance/pagergotchi

TARGET="/root/payloads/user/reconnaissance/pagergotchi"
[ ! -d "$TARGET" ] && echo "PagerGotchi not installed" && exit 1

export PATH="/mmc/usr/bin:$TARGET/bin:$PATH"
export PYTHONPATH="$TARGET/lib:$TARGET:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$TARGET/lib:$LD_LIBRARY_PATH"

cd "$TARGET"
bash payload.sh
exit $?
