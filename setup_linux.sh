#!/usr/bin/env bash
# =============================================================================
# Seznik EON Printer Toolkit — Linux Setup Script
# Tested on Debian Trixie / Ubuntu 24.04, kernel 6+
# =============================================================================
set -euo pipefail

TOOLKIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="eon-printer-relay"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
BLUEZ_INFO_DIR="/var/lib/bluetooth"
RELAY_PORT="${RELAY_PORT:-9100}"

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  •${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
fail() { echo -e "${RED}  ✗${NC}  $*"; exit 1; }
header() { echo -e "\n${BOLD}${CYAN}══ $* ${NC}"; }

# ── helpers ───────────────────────────────────────────────────────────────────
need_root() {
    [[ $EUID -eq 0 ]] || fail "Run with sudo: sudo bash $0 $*"
}

python_bin() {
    for p in python3.13 python3.12 python3.11 python3; do
        command -v "$p" &>/dev/null && { echo "$p"; return; }
    done
    fail "No Python 3 found"
}

# ── steps ─────────────────────────────────────────────────────────────────────

install_system_deps() {
    header "System dependencies"
    apt-get update -qq
    apt-get install -y -qq \
        bluetooth bluez bluez-tools \
        python3 python3-pip python3-venv \
        libglib2.0-dev libdbus-1-dev \
        poppler-utils ghostscript \
        cups cups-client
    ok "System packages installed"
}

install_python_deps() {
    header "Python dependencies"
    local py; py=$(python_bin)
    info "Using $py ($(${py} --version 2>&1))"

    # prefer venv to avoid PEP 668 issues on Debian
    if [[ ! -d "${TOOLKIT_DIR}/.venv" ]]; then
        "$py" -m venv "${TOOLKIT_DIR}/.venv"
        ok "Virtualenv created at ${TOOLKIT_DIR}/.venv"
    fi

    local pip="${TOOLKIT_DIR}/.venv/bin/pip"
    "$pip" install -q --upgrade pip
    "$pip" install -q \
        bleak \
        pillow \
        pypdf2 \
        pdfplumber \
        dbus-next

    ok "Python packages installed"
}

patch_windows_checks() {
    header "Patching Windows-only guards"
    local py="${TOOLKIT_DIR}/.venv/bin/python"

    for script in bt_scan.py bt_print.py; do
        local f="${TOOLKIT_DIR}/${script}"
        [[ -f "$f" ]] || { warn "$script not found, skipping"; continue; }

        # Remove lines that bail on non-Windows (handles both 1-line and 2-line patterns)
        sed -i '/platform\.system() != ["\x27]Windows["\x27]/,/Windows only/d' "$f"
        # Also remove single-line variants
        sed -i '/if platform\.system() != ["\x27]Windows["\x27]:/d' "$f"

        ok "$script patched"
    done
}

fix_bluez_le_transport() {
    header "Configuring BlueZ for LE-only transport"

    # Enable experimental features so btmgmt LE commands work reliably
    local svc_file="/usr/lib/systemd/system/bluetooth.service"
    if ! grep -q "\-\-experimental" "$svc_file"; then
        sed -i 's|ExecStart=.*bluetoothd.*|& --experimental|' "$svc_file"
        systemctl daemon-reload
        ok "Experimental mode enabled in bluetoothd"
    else
        info "Experimental mode already enabled"
    fi

    systemctl enable bluetooth
    systemctl restart bluetooth
    sleep 2
    ok "BlueZ restarted"
}

scan_and_save_config() {
    header "Scanning for printer & saving config"

    local py="${TOOLKIT_DIR}/.venv/bin/python"

    # Detect adapter
    local adapter
    adapter=$(hciconfig 2>/dev/null | grep -oP 'hci\d+' | head -1)
    [[ -z "$adapter" ]] && fail "No Bluetooth adapter found"
    info "Adapter: $adapter"

    # Detect adapter MAC
    local adapter_mac
    adapter_mac=$(hciconfig "$adapter" 2>/dev/null | grep -oP '(?<=BD Address: )[A-F0-9:]+')
    info "Adapter MAC: $adapter_mac"

    # Pre-create the info file so BlueZ treats the printer as LE Public
    # (JL chipset advertises Simultaneous BR/EDR+LE but is LE-only in practice)
    echo ""
    warn "About to scan. Make sure the printer is powered ON."
    echo ""

    # Run bt_scan.py — it will connect, probe GATT, and save config
    info "Running bt_scan.py --save ..."
    cd "${TOOLKIT_DIR}"
    "$py" bt_scan.py --save || true

    local cfg_file="${HOME}/.seznik-eon-printer-toolkit/bt_printer_config.json"
    if [[ ! -f "$cfg_file" ]]; then
        warn "Config not saved automatically."
        warn "If the scan failed with a transport error, run fix_bluez_transport.sh"
        warn "and then: python bt_scan.py --save"
    else
        ok "Config saved: $cfg_file"
    fi
}

install_relay_service() {
    header "Installing relay systemd service"

    local py_abs="${TOOLKIT_DIR}/.venv/bin/python"
    local relay_abs="${TOOLKIT_DIR}/printer_relay.py"
    local user="${SUDO_USER:-$USER}"

    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Seznik EON BLE Printer Relay
After=bluetooth.target network.target
Wants=bluetooth.target

[Service]
Type=simple
User=${user}
WorkingDirectory=${TOOLKIT_DIR}
ExecStart=${py_abs} ${relay_abs} --port ${RELAY_PORT}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    sleep 2

    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        ok "Relay service running on port ${RELAY_PORT}"
    else
        warn "Relay service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 30"
    fi
}

install_cups_printer() {
    header "Registering CUPS virtual printer (optional)"

    local relay_host="127.0.0.1"
    local printer_name="BT-Thermal-Printer"

    # Check if CUPS is available
    if ! command -v lpadmin &>/dev/null; then
        warn "CUPS not found, skipping printer registration"
        return
    fi

    systemctl enable cups --now 2>/dev/null || true
    sleep 1

    # Remove old entry if exists
    lpadmin -x "$printer_name" 2>/dev/null || true

    # Add raw TCP printer pointing at relay
    lpadmin -p "$printer_name" \
        -E \
        -v "socket://${relay_host}:${RELAY_PORT}" \
        -m raw \
        -o printer-is-shared=false 2>/dev/null && \
        ok "CUPS printer '${printer_name}' registered → socket://${relay_host}:${RELAY_PORT}" || \
        warn "CUPS printer registration failed (non-fatal)"
}

create_cli_wrapper() {
    header "Creating eon-print CLI wrapper"

    local wrapper="/usr/local/bin/eon-print"
    local py_abs="${TOOLKIT_DIR}/.venv/bin/python"

    cat > "$wrapper" << EOF
#!/usr/bin/env bash
# eon-print — wrapper for bt_print.py
cd "${TOOLKIT_DIR}"
exec "${py_abs}" bt_print.py "\$@"
EOF
    chmod +x "$wrapper"
    ok "Installed: eon-print  (use eon-print --help)"
}

print_summary() {
    header "Setup complete"
    echo ""
    echo -e "  ${BOLD}Direct printing:${NC}"
    echo "    eon-print --test-page"
    echo "    eon-print --print-text 'Hello'"
    echo "    eon-print --print-image photo.jpg"
    echo "    eon-print --print-pdf file.pdf"
    echo ""
    echo -e "  ${BOLD}Relay service:${NC}"
    echo "    systemctl status ${SERVICE_NAME}"
    echo "    journalctl -u ${SERVICE_NAME} -f"
    echo ""
    echo -e "  ${BOLD}Re-scan printer:${NC}"
    echo "    cd ${TOOLKIT_DIR} && .venv/bin/python bt_scan.py --save"
    echo ""
    warn "If BlueZ connects via BR/EDR instead of LE, run:"
    echo "    sudo bash fix_bluez_le.sh"
    echo ""
}

# ── BlueZ LE fix helper script ────────────────────────────────────────────────
create_bluez_le_fix_script() {
    local fix="${TOOLKIT_DIR}/fix_bluez_le.sh"
    cat > "$fix" << 'FIXEOF'
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
FIXEOF
    chmod +x "$fix"
    ok "Created: fix_bluez_le.sh  (auto-discovers printer MAC)"
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
    echo -e "${BOLD}"
    echo "  ╔══════════════════════════════════════════════╗"
    echo "  ║   Seznik EON Printer Toolkit — Linux Setup  ║"
    echo "  ╚══════════════════════════════════════════════╝"
    echo -e "${NC}"

    need_root

    install_system_deps
    install_python_deps
    patch_windows_checks
    fix_bluez_le_transport
    create_bluez_le_fix_script
    scan_and_save_config
    install_relay_service
    install_cups_printer
    create_cli_wrapper
    print_summary
}

main "$@"
