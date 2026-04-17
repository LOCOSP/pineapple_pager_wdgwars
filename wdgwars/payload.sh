#!/bin/bash
# Title: WDGoWars Wardriver
# Description: WiFi + BLE wardriver for wdgwars.pl (offline-first, GPS required)
# Author: LOCOSP
# Version: 1.0
# Category: Reconnaissance
# Library: libpagerctl.so (pagerctl)

PAYLOAD_DIR="/root/payloads/user/reconnaissance/wdgwars"
WIFMAN_DIR="/root/payloads/user/reconnaissance/wifman"
DATA_DIR="$PAYLOAD_DIR/data"
NEXT_PAYLOAD_FILE="$DATA_DIR/.next_payload"
mkdir -p "$DATA_DIR"

cd "$PAYLOAD_DIR" || { LOG "red" "ERROR: $PAYLOAD_DIR not found"; exit 1; }

export PATH="/mmc/usr/bin:$PATH"
export PYTHONPATH="$PAYLOAD_DIR/lib:$PAYLOAD_DIR:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$PAYLOAD_DIR/lib:$PAYLOAD_DIR:$LD_LIBRARY_PATH"
export CRYPTOGRAPHY_OPENSSL_NO_LEGACY=1
export WDGWARS_PAYLOAD_DIR="$PAYLOAD_DIR"

# ============================================================
# CLEANUP (trap so service restarts even on error)
# ============================================================

cleanup() {
    if ! pgrep -x pineapple >/dev/null; then
        /etc/init.d/pineapplepager start 2>/dev/null &
    fi
}
trap cleanup EXIT

# ============================================================
# DEPENDENCIES — check, auto-copy pagerctl, opkg-install python
# ============================================================

# pagerctl: prefer payload's own lib/, else copy from installed wifman, else
# bail out with clear instructions. Never reaches opkg because these files
# don't live in any opkg feed.
if [ ! -f "$PAYLOAD_DIR/lib/libpagerctl.so" ] || [ ! -f "$PAYLOAD_DIR/lib/pagerctl.py" ]; then
    if [ -f "$WIFMAN_DIR/lib/libpagerctl.so" ] && [ -f "$WIFMAN_DIR/lib/pagerctl.py" ]; then
        mkdir -p "$PAYLOAD_DIR/lib"
        cp "$WIFMAN_DIR/lib/libpagerctl.so" "$PAYLOAD_DIR/lib/"
        cp "$WIFMAN_DIR/lib/pagerctl.py"     "$PAYLOAD_DIR/lib/"
        LOG "cyan" "pagerctl auto-copied from wifman"
    else
        LOG ""
        LOG "red" "=== MISSING DEPENDENCY: pagerctl ==="
        LOG ""
        LOG "libpagerctl.so / pagerctl.py not found in:"
        LOG "  $PAYLOAD_DIR/lib/"
        LOG "  $WIFMAN_DIR/lib/"
        LOG ""
        LOG "Install the wifman payload first, or drop"
        LOG "pagerctl.py + libpagerctl.so into $PAYLOAD_DIR/lib/."
        LOG ""
        LOG "Press any button to exit..."
        WAIT_FOR_INPUT >/dev/null 2>&1
        exit 1
    fi
fi

# python3 + ctypes: opkg-installable via mmc overlay (same as wifman does).
NEED_PY=false
NEED_CTYPES=false
if ! command -v python3 >/dev/null 2>&1; then
    NEED_PY=true; NEED_CTYPES=true
elif ! python3 -c "import ctypes" 2>/dev/null; then
    NEED_CTYPES=true
fi

if [ "$NEED_PY" = true ] || [ "$NEED_CTYPES" = true ]; then
    LOG ""
    LOG "red" "=== PYTHON3 REQUIRED ==="
    LOG ""
    if [ "$NEED_PY" = true ]; then
        LOG "Python3 is not installed."
    else
        LOG "python3-ctypes is not installed."
    fi
    LOG ""
    LOG "WDGoWars needs Python3 + ctypes for pagerctl."
    LOG ""
    LOG "green" "GREEN = Install via opkg (needs internet)"
    LOG "red"   "RED   = Exit"
    LOG ""
    while true; do
        BUTTON=$(WAIT_FOR_INPUT 2>/dev/null)
        case "$BUTTON" in
            "GREEN"|"A")
                LOG ""
                LOG "Updating package lists..."
                opkg update 2>&1 | while IFS= read -r line; do LOG "  $line"; done
                LOG ""
                LOG "Installing python3 + python3-ctypes to /mmc..."
                opkg -d mmc install python3 python3-ctypes 2>&1 \
                    | while IFS= read -r line; do LOG "  $line"; done
                if command -v python3 >/dev/null 2>&1 && python3 -c "import ctypes" 2>/dev/null; then
                    LOG ""
                    LOG "green" "Python3 + ctypes installed."
                    sleep 1
                    break
                else
                    LOG ""
                    LOG "red" "Install failed — check internet connection."
                    LOG "Press any button to exit..."
                    WAIT_FOR_INPUT >/dev/null 2>&1
                    exit 1
                fi
                ;;
            "RED"|"B")
                LOG "Exiting."
                exit 0
                ;;
        esac
    done
fi

# Soft deps — warn once but don't block. WiFi scan needs `iw`, BLE needs
# `bluetoothctl`, u-blox GPS needs the `cdc_acm` kernel module.
SOFT_WARN=""
command -v iw >/dev/null 2>&1 || SOFT_WARN="$SOFT_WARN iw"
command -v bluetoothctl >/dev/null 2>&1 || SOFT_WARN="$SOFT_WARN bluetoothctl"
[ -e /sys/module/cdc_acm ] || modprobe cdc_acm 2>/dev/null
[ -e /sys/module/cdc_acm ] || SOFT_WARN="$SOFT_WARN kmod-usb-acm"
if [ -n "$SOFT_WARN" ]; then
    LOG "yellow" "Optional deps missing:$SOFT_WARN"
    LOG "         Install with: opkg install$SOFT_WARN"
    LOG "         (payload will still start; affected features disabled)"
fi

# Loot dir
mkdir -p /mmc/root/loot/wdgwars/sessions

# ============================================================
# SPLASH + GREEN-GATE
# ============================================================

RINGTONE "wd:d=8,o=5,b=200:c6,e6,g6"

LOG ""
LOG "cyan" "====[ WDGoWars Wardriver ]===="
LOG "magenta" "by LOCOSP"
LOG ""
LOG "WiFi + BLE wardriver for wdgwars.pl"
LOG "Offline-first  ::  GPS required"
LOG ""
LOG "green" "GREEN = Start"
LOG "red"   "RED   = Exit"
LOG ""

while true; do
    BUTTON=$(WAIT_FOR_INPUT 2>/dev/null)
    case "$BUTTON" in
        "GREEN"|"A") break ;;
        "RED"|"B") LOG "Exiting."; exit 0 ;;
    esac
done

# ============================================================
# MAIN LOOP (with APP_HANDOFF)
# ============================================================

SPINNER_ID=$(START_SPINNER "Starting WDGoWars...")
/etc/init.d/pineapplepager stop 2>/dev/null
sleep 0.5
STOP_SPINNER "$SPINNER_ID" 2>/dev/null

# Reap any orphan peer-payload processes from a previous crash or exit 42
# chain. If wifman.py (or any peer python) is still alive, it will keep
# writing to the LCD and buttons, making our own UI look ghosted. Killing
# by match is fine here — our own wdgwars.py hasn't been spawned yet.
for _peer in wifman.py loki_menu.py run_pagergotchi.py Bjorn.py bjorn_menu.py; do
    for _pid in $(ps | awk "/$_peer/ && !/awk/ {print \$1}"); do
        kill $_pid 2>/dev/null
    done
done
sleep 0.2

while true; do
    cd "$PAYLOAD_DIR"
    python3 wdgwars.py 2>>/tmp/wdgwars.err
    EXIT_CODE=$?

    # After our python exits, reap any subprocess it might have left behind
    # (mirrors the pagergotchi pattern of `killall hcxdumptool` post-exit).
    # bluetoothctl is the only one that outlives us — scanner subprocess ran
    # under a pty so it doesn't get killed by its parent dying.
    killall bluetoothctl 2>/dev/null
    # Belt-and-suspenders: make damn sure no stray wdgwars.py instances are
    # still around (e.g. if the user opened wdgwars twice by accident).
    for _pid in $(ps | awk '/wdgwars\.py/ && !/awk/ {print $1}'); do
        [ "$_pid" != "$$" ] && kill $_pid 2>/dev/null
    done

    if [ "$EXIT_CODE" -eq 42 ] && [ -f "$NEXT_PAYLOAD_FILE" ]; then
        NEXT_SCRIPT=$(cat "$NEXT_PAYLOAD_FILE")
        rm -f "$NEXT_PAYLOAD_FILE"
        if [ -f "$NEXT_SCRIPT" ]; then
            sh "$NEXT_SCRIPT"
            [ $? -eq 42 ] && continue
        fi
    fi
    break
done

exit 0
