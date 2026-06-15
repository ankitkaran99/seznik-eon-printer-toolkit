"""
bt_scan.py — Bluetooth Printer Scanner
=======================================
Scans, detects, and identifies nearby Bluetooth printers.
Saves detected printer info to config for bt_print.py to use.

Usage:
    python bt_scan.py                  # scan + auto-detect
    python bt_scan.py --scan-time 20   # longer scan
    python bt_scan.py --all            # show all BLE devices
    python bt_scan.py --no-probe       # skip GATT deep probe
    python bt_scan.py --save           # save best result for bt_print.py

Requirements:
    pip install bleak
"""

import asyncio
import sys
import platform
import argparse

from bt_shared import (
    BLEAK_AVAILABLE, CONFIDENCE_THRESHOLD, PRINTER_UUIDS,
    W, sep, header, section, ok, fail, info,
    confidence_bar, rssi_label, score_device, probe_printer,
    save_config, load_config, hidden_subprocess_kwargs,
)

try:
    from bleak import BleakScanner
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
#  BLE SCAN  (multi-pass)
# ═══════════════════════════════════════════════════════════════════════════════

async def scan_ble(scan_seconds=12):
    section(f"BLE Scan  ({scan_seconds}s, multi-pass)")
    print("  Keep the printer powered ON and in pairing/advertising mode.\n")

    print("  Pass 1 of 2…", end="", flush=True)
    try:
        raw1 = await BleakScanner.discover(
            timeout=max(scan_seconds - 4, 5), return_adv=True)
    except Exception as e:
        _handle_bt_error(e)
        return []
    print(f" {len(raw1)} device(s)")

    print("  Pass 2 of 2…", end="", flush=True)
    try:
        raw2 = await BleakScanner.discover(timeout=4, return_adv=True)
    except Exception:
        raw2 = {}
    print(f" {len(raw2)} device(s)")

    merged = {**raw2, **raw1}
    print(f"\n  Total unique devices: {len(merged)}")

    results = []
    for addr, (device, adv) in merged.items():
        uuids    = [str(u).lower() for u in (adv.service_uuids or [])]
        name     = (device.name or adv.local_name or "").strip()
        rssi     = adv.rssi
        mfr_data = adv.manufacturer_data or {}
        scored   = score_device(name, uuids, mfr_data, addr)
        results.append({
            "address":  addr,
            "name":     name or "(unknown)",
            "rssi":     rssi,
            "uuids":    uuids,
            "mfr_data": mfr_data,
            **scored,
        })

    return sorted(results, key=lambda d: d["score"], reverse=True)


def _handle_bt_error(e):
    err = str(e).lower()
    print("\n  ✗  Bluetooth scan failed!\n")
    if any(x in err for x in ("powered_off","powered off","not powered on","radio is not")):
        print("  ⚠️   BLUETOOTH IS TURNED OFF")
        sep("─")
        print("  Fix:\n")
        print("  1.  Win + A  →  click Bluetooth tile")
        print("  2.  Settings → Bluetooth & devices → toggle ON")
        print("  3.  PowerShell (Admin):")
        print("        Set-Service bthserv -StartupType Automatic")
        print("        Start-Service bthserv")
    elif any(x in err for x in ("not available","no bluetooth","adapter","radio")):
        print("  ⚠️   NO BLUETOOTH ADAPTER")
        sep("─")
        print("  • Check Device Manager for BT adapter")
        print("  • Use USB BT dongle (CSR8510/RTL8761)")
    elif any(x in err for x in ("permission","access denied","unauthorized")):
        print("  ⚠️   PERMISSION DENIED")
        sep("─")
        print("  • Run terminal as Administrator")
    elif "timeout" in err:
        print("  ⚠️   SCAN TIMED OUT")
        sep("─")
        print("  • Wake printer, re-run with: --scan-time 20")
    else:
        print(f"  Unexpected error: {e}")
        sep("─")
        print("  • Confirm Bluetooth is ON")
        print("  • services.msc → Bluetooth Support Service → Restart")


# ═══════════════════════════════════════════════════════════════════════════════
#  WINDOWS CLASSIC BT SCAN
# ═══════════════════════════════════════════════════════════════════════════════

def scan_windows_classic():
    import subprocess, platform as _plat
    if _plat.system() != "Windows":
        return {"com_ports": [], "pnp_printers": []}

    section("Windows Classic Bluetooth / Paired Devices")
    result = {"com_ports": [], "pnp_printers": []}

    def _ps(cmd):
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
            **hidden_subprocess_kwargs())

    try:
        r = _ps(
            "Get-PnpDevice | Where-Object {"
            "  $_.Class -eq 'Bluetooth' -or $_.Class -eq 'Printer'"
            "} | Select-Object FriendlyName, Status, Class | Format-List")
        if r.stdout.strip():
            print("  Paired PnP Devices:\n")
            for line in r.stdout.strip().splitlines():
                print(f"  {line}")
                if "FriendlyName" in line:
                    result["pnp_printers"].append(line.split(":",1)[-1].strip())
        else:
            info("No paired BT devices found via PnP.")
    except Exception as e:
        info(f"PnP scan error: {e}")

    try:
        r2 = _ps(
            "Get-WmiObject Win32_SerialPort"
            " | Where-Object { $_.Description -like '*Bluetooth*'"
            "               -or $_.Name -like '*Bluetooth*' }"
            " | Select-Object Name, DeviceID | Format-List")
        if r2.stdout.strip():
            print("\n  Bluetooth COM Ports:\n")
            for line in r2.stdout.strip().splitlines():
                print(f"  {line}")
                if "Name" in line and "COM" in line:
                    result["com_ports"].append(line.split(":",1)[-1].strip())
        else:
            info("No Bluetooth COM ports found.")
            info("Pair printer → More BT settings → COM Ports → Add Outgoing")
    except Exception as e:
        info(f"COM scan error: {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════

def print_device_table(devices, printers, show_all=False):
    section(f"All BLE Devices  ({len(devices)} found)")
    show = devices if show_all else (printers if printers else devices[:5])
    for d in show:
        tag = "🖨️ " if d["score"] >= CONFIDENCE_THRESHOLD else "   "
        print(f"  {tag}{d['name']:<30}  {d['address']}")
        print(f"      RSSI       : {rssi_label(d['rssi'])}")
        print(f"      Confidence : {confidence_bar(d['score'])}")
        if d["protos"]:
            print(f"      Protocols  : {', '.join(d['protos'])}")
        print()


def print_printer_cards(printers):
    section(f"Identified Printers ({len(printers)})")
    for p in printers:
        print(f"  Name     : {p['name']}")
        print(f"  Address  : {p['address']}")
        print(f"  Signal   : {rssi_label(p['rssi'])}")
        print(f"  Score    : {confidence_bar(p['score'])}")
        print(f"  Protocol : {p['protocol'] or 'unknown'}")
        if p["uuids"]:
            print("  UUIDs    :")
            for u in p["uuids"]:
                info_ = PRINTER_UUIDS.get(u)
                lbl   = f"  ← {info_[0]}" if info_ else ""
                print(f"    • {u}{lbl}")
        print("  Reasons  :")
        for r in p["reasons"][:5]:
            print(f"    + {r}")
        print()


def print_suggestions(device, com_ports):
    addr     = device["address"]
    name     = device["name"]
    protocol = device.get("protocol", "BLE_ESCPOS")
    probe    = device.get("probe", {})
    w_chars  = probe.get("writable_chars", [])
    n_chars  = probe.get("notify_chars", [])
    escpos   = probe.get("escpos_ok", False)

    known_write = [u for u in w_chars if u.lower() in PRINTER_UUIDS]
    best_char   = (known_write or w_chars or ["<write-char-uuid>"])[0]

    section(f"How to use: {name}  [{addr}]")
    print(f"  Confidence  : {confidence_bar(device['score'])}")
    print(f"  Protocol    : {protocol or 'unknown'}")
    print(f"  ESC/POS test: {'✓ PASSED' if escpos else '— not tested'}")
    if w_chars:
        print(f"  Write UUID  : {best_char}")
    print()


    print("  Direct BLE print:")
    print("    python bt_print.py --test-page")
    print("    python bt_print.py --print-text 'Hello!'")
    print("    python bt_print.py --print-image photo.jpg")
    print("    python bt_print.py --print-pdf file.pdf")



# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="bt_scan.py — Bluetooth Printer Scanner",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python bt_scan.py                  # scan and detect\n"
            "  python bt_scan.py --scan-time 20   # longer scan\n"
            "  python bt_scan.py --all            # show all BLE devices\n"
            "  python bt_scan.py --save           # save result for bt_print.py\n"
            "  python bt_scan.py --no-probe       # skip GATT probe\n"
        )
    )
    parser.add_argument("--scan-time", type=int, default=12,
                        help="BLE scan duration in seconds (default: 12)")
    parser.add_argument("--all",       action="store_true",
                        help="Show all BLE devices, not just printers")
    parser.add_argument("--no-probe",  action="store_true",
                        help="Skip deep GATT probe")
    parser.add_argument("--save",      action="store_true",
                        help="Save detected printer config for bt_print.py")
    args = parser.parse_args(argv)

    header("bt_scan.py — Bluetooth Printer Scanner")
    print(f"  Platform  : {platform.system()} {platform.release()}")
    print(f"  Python    : {sys.version.split()[0]}")
    print(f"  Scan time : {args.scan_time}s")
    print(f"  GATT probe: {'disabled' if args.no_probe else 'enabled'}")

    if args.scan_time <= 0:
        fail("Scan time must be greater than 0.")
        return 1

    if not BLEAK_AVAILABLE:
        fail("'bleak' not installed. Run:  pip install bleak")
        return 1

    # BLE scan
    all_devices = await scan_ble(args.scan_time)
    printers    = [d for d in all_devices if d["score"] >= CONFIDENCE_THRESHOLD]

    print_device_table(all_devices, printers, args.all)

    if not printers:
        section("No printers identified")
        info("Printer is asleep — press power button to wake it")
        info("Not in pairing mode — hold button until LED blinks fast")
        info("Already connected to phone — disconnect from vendor app first")
        info("Too far away — bring within 1 metre")
        if all_devices:
            best = all_devices[0]
            print(f"\n  Closest candidate: {best['name']}  [{best['address']}]  score={best['score']}%")
            for r in best["reasons"][:5]:
                print(f"    + {r}")
    else:
        print_printer_cards(printers)

        # GATT probe (skipped when --no-probe)
        if not args.no_probe:
            for i, p in enumerate(printers):
                printers[i] = await probe_printer(p)

        # Save best result:
        #   • always attempt when --save is explicit
        #   • auto-save after a successful probe (no --no-probe)
        #   Never attempt when --no-probe without --save: probe dict would be
        #   empty and the "no write UUID" error message would be confusing.
        if args.save or not args.no_probe:
            best    = printers[0]
            probe   = best.get("probe", {})
            w_chars = probe.get("writable_chars", [])
            if w_chars:
                known      = [u for u in w_chars if u.lower() in PRINTER_UUIDS]
                write_uuid = (known or w_chars)[0]
                save_config(best, write_uuid)
                print()
                ok("Printer config saved → run bt_print.py to print")
            elif args.save:
                fail("No write UUID found — cannot save config (run without --no-probe)")

    # Windows classic BT
    win = scan_windows_classic()

    # Suggestions
    if printers:
        for p in printers:
            print_suggestions(p, win["com_ports"])
    sep("═")
    print("  Scan complete.  Next step:  python bt_print.py --help")
    sep("═")

    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
