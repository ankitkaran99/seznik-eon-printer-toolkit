#!/usr/bin/env bash
# fix_bluez_le.sh — force BlueZ to treat a JL-chipset BLE printer as LE Public
# Run this if bt_scan.py fails with BREDR.ProfileUnavailable
#
# Usage:
#   sudo bash fix_bluez_le.sh                     # auto-discovers printer MAC
#   sudo bash fix_bluez_le.sh <PRINTER_MAC>        # use known MAC
#   sudo bash fix_bluez_le.sh <PRINTER_MAC> <NAME> # use known MAC + name
set -euo pipefail

ADAPTER=$(hciconfig 2>/dev/null | grep -oP 'hci\d+' | head -1)
ADAPTER_MAC=$(hciconfig "$ADAPTER" 2>/dev/null | grep -oP '(?<=BD Address: )[A-F0-9:]+')

echo "Adapter : $ADAPTER  ($ADAPTER_MAC)"

# ── discover printer MAC dynamically via LE scan ──────────────────────────────
discover_printer_mac() {
    echo "Scanning for BLE printer (10s) — keep printer powered ON..."
    # Use btmgmt LE-only scan, grab first non-phone-looking device with a name
    # that looks like a printer (short alphanumeric name, not a phone/watch)
    local raw
    raw=$(timeout 12 bash -c "btmgmt --index 0 find -l 2>/dev/null || btmgmt --index 1 find -l 2>/dev/null" || true)

    # Extract lines: "dev_found: XX:XX:XX:XX:XX:XX type LE ... name <NAME>"
    # Filter: has a name, name is short (≤16 chars), not obvious non-printers
    local mac name
    while IFS= read -r line; do
        if [[ "$line" =~ dev_found:\ ([0-9A-F:]{17}).*name\ (.+)$ ]]; then
            mac="${BASH_REMATCH[1]}"
            name="${BASH_REMATCH[2]}"
            # Skip known non-printers by name pattern
            if echo "$name" | grep -qiE '(phone|watch|band|headset|speaker|moments|wavecall|PBL)'; then
                continue
            fi
            echo "Found candidate: $mac  ($name)"
            PRINTER_MAC="$mac"
            PRINTER_NAME="$name"
            return 0
        fi
    done <<< "$raw"

    return 1
}

# Accept MAC as first arg, or discover
if [[ -n "${1:-}" ]]; then
    PRINTER_MAC="$1"
    PRINTER_NAME="${2:-BLE-Printer}"
    echo "Printer : $PRINTER_MAC  ($PRINTER_NAME)  [from argument]"
else
    PRINTER_MAC=""
    PRINTER_NAME="BLE-Printer"
    if ! discover_printer_mac; then
        echo ""
        echo "ERROR: Could not find a BLE printer automatically."
        echo "Usage: sudo bash fix_bluez_le.sh <PRINTER_MAC> [PRINTER_NAME]"
        exit 1
    fi
    echo "Printer : $PRINTER_MAC  ($PRINTER_NAME)  [auto-discovered]"
fi

echo ""

# ── write BlueZ info file ─────────────────────────────────────────────────────
# Stop bluetooth so it doesn't overwrite our file during startup
systemctl stop bluetooth

INFO_DIR="/var/lib/bluetooth/${ADAPTER_MAC}/${PRINTER_MAC}"
mkdir -p "$INFO_DIR"

# Remove immutable flag if previously set
chattr -i "${INFO_DIR}/info" 2>/dev/null || true

cat > "${INFO_DIR}/info" << EOF
[General]
Name=${PRINTER_NAME}
AddressType=public
SupportedTechnologies=LE;
PreferredBearer=le
Trusted=true
Blocked=false
EOF

# Lock so BlueZ advertisement parsing can't overwrite it
chattr +i "${INFO_DIR}/info"

systemctl start bluetooth
sleep 2

echo ""
echo "✓  BlueZ info file written and locked:"
echo "   ${INFO_DIR}/info"
echo ""
echo "Now run:  python bt_scan.py --save"
echo ""
echo "To unlock after successful scan:"
echo "  sudo chattr -i ${INFO_DIR}/info"
