#!/bin/sh
# Run once on the pager to fetch pagerctl bindings from the wifman payload
# and ensure runtime dependencies are installed.

set -e

PAYLOAD_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$PAYLOAD_DIR/lib"
mkdir -p "$LIB"

WIFMAN_BASE="https://raw.githubusercontent.com/LOCOSP/pineapple_pager_wifman/main/wifman/lib"

echo "[bootstrap] downloading pagerctl bindings"
for f in pagerctl.py libpagerctl.so; do
  if [ ! -f "$LIB/$f" ]; then
    wget -q -O "$LIB/$f.tmp" "$WIFMAN_BASE/$f" && mv "$LIB/$f.tmp" "$LIB/$f"
    echo "  ok $f"
  else
    echo "  skip $f (exists)"
  fi
done

echo "[bootstrap] installing runtime packages (best-effort)"
opkg update 2>/dev/null || true
for pkg in iw bluez-utils kmod-usb-acm; do
  opkg list-installed | grep -q "^$pkg " || opkg install "$pkg" || echo "  warn: could not install $pkg"
done

echo "[bootstrap] loading cdc_acm for u-blox"
modprobe cdc_acm 2>/dev/null || true

mkdir -p /mmc/root/loot/wdgwars/sessions

echo "[bootstrap] done"
