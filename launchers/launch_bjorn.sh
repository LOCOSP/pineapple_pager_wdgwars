#!/bin/bash
# Title: Exit to Bjorn
# Requires: /root/payloads/user/reconnaissance/pager_bjorn

TARGET="/root/payloads/user/reconnaissance/pager_bjorn"
[ ! -d "$TARGET" ] && echo "Bjorn not installed" && exit 1

export PATH="/mmc/usr/bin:$TARGET/bin:$PATH"
export PYTHONPATH="$TARGET/lib:$TARGET:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$TARGET/lib:$LD_LIBRARY_PATH"

cd "$TARGET"
bash payload.sh
exit $?
