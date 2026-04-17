#!/bin/bash
# Title: Exit to Loki
# Requires: /root/payloads/user/reconnaissance/loki

TARGET="/root/payloads/user/reconnaissance/loki"
[ ! -d "$TARGET" ] && echo "Loki not installed" && exit 1

export PATH="/mmc/usr/bin:$TARGET/bin:$PATH"
export PYTHONPATH="$TARGET/lib:$TARGET:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$TARGET/lib:$LD_LIBRARY_PATH"

cd "$TARGET"
bash payload.sh
exit $?
