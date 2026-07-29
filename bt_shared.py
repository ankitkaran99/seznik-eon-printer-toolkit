"""
bt_shared.py — shared constants, databases, helpers
Used by both bt_scan.py and bt_print.py
"""

import os
import sys
import time
import asyncio
import platform
import subprocess
import shutil


def _try_enable_utf8_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        encoding = getattr(stream, "encoding", None) or ""
        if encoding.lower() == "utf-8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_try_enable_utf8_console()

try:
    from bleak import BleakScanner, BleakClient
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

RELAY_PORT        = 9100
RELAY_HOST        = "127.0.0.1"
PRINTER_NAME      = "BT-Thermal-Printer"
RELAY_CHUNK_SIZE  = 60        # Cat/P1-series printers prefer small BLE packets
RELAY_CHUNK_DELAY = 0.01      # 10ms between BLE packets
CONFIDENCE_THRESHOLD = 30
PAPER_WIDTH_DOTS  = 384       # 58mm @ 203dpi
PAPER_WIDTH_MM    = 58
PAPER_WIDTH_BYTES = 48        # 384 dots / 8 bits = 48 bytes per row (cat protocol)


def _config_dir():
    override = os.environ.get("SEZNIK_EON_CONFIG_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".seznik-eon-printer-toolkit")


def _legacy_config_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt_printer_config.json")


def _can_write_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test.tmp")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _resolve_config_dir():
    preferred = _config_dir()
    if _can_write_dir(preferred):
        return preferred

    legacy_dir = os.path.dirname(_legacy_config_file())
    if _can_write_dir(legacy_dir):
        return legacy_dir

    return preferred


CONFIG_DIR        = _resolve_config_dir()
CONFIG_FILE       = os.path.join(CONFIG_DIR, "bt_printer_config.json")

# ESC/POS command constants
ESCPOS_INIT    = bytes([0x1b, 0x40])
ESCPOS_STATUS  = bytes([0x10, 0x04, 0x01])
ESCPOS_TEST    = (
    b"\x1b\x40"
    b"\x1b\x61\x01"
    b"\x1b\x21\x30"
    b"== TEST PAGE ==\n"
    b"\x1b\x21\x00"
    b"\x1b\x61\x00"
    b"Printer : Seznik EON Printer Toolkit\n"
    b"Protocol: Direct BLE print\n"
    b"Config  : bt_printer_config.json\n"
    b"Status  : DIRECT OK\n"
    b"\n"
    b"\x1b\x61\x01"
    b"Direct print test successful\n"
    b"\n\n\n"
    b"\x1d\x56\x41\x03"
)


# ═══════════════════════════════════════════════════════════════════════════════
#  DETECTION DATABASES
# ═══════════════════════════════════════════════════════════════════════════════

PRINTER_KEYWORDS = {
    "thermal":40,"receipt":40,"escpos":40,"esc/pos":40,"printer":40,
    "print":35,"pos printer":40,"label":35,"inkless":35,"barcode":30,"ticket":30,
    "seznik":50,"eon":40,"paperang":50,"peripage":50,"phomemo":50,"goojprt":50,
    "munbyn":50,"nelko":50,"niimbot":50,"hprt":50,"xprinter":50,"rongta":50,
    "bixolon":50,"zebra":50,"tsc":45,"godex":45,"sato":45,"citizen":45,
    "starmicronics":50,"sewoo":50,"woosim":50,"datecs":50,"iposprinter":50,
    "snbc":50,"poooli":50,"memobird":50,"litu":50,"cat printer":50,
    "bear printer":50,"wep":45,"tvslp":45,"posiflex":45,
    "mtp-":45,"mpp-":45,"zj-":45,"gp-":40,"rp-":40,"xp-":40,
    "pt-":40,"sp-":35,"tp-":35,"mp-":35,"qr-":35,
    "p1-":50,"p2-":45,"p3-":45,
    "pos":25,"bill":25,"mini printer":35,"bt printer":35,
    "mobile printer":35,"pocket print":35,"cloud print":30,
    "hm-10":20,"hm10":20,"jdy-":20,"at-09":20,"mlm-":20,
    "bt":10,"mini":10,"qr":10,
}

PRINTER_UUIDS = {
    "00001101-0000-1000-8000-00805f9b34fb": ("SPP Serial Port Profile",          50, "SPP"),
    "00001200-0000-1000-8000-00805f9b34fb": ("PnP Information",                  15, None),
    "000018f0-0000-1000-8000-00805f9b34fb": ("BLE Generic Printer (18F0)",        50, "BLE_ESCPOS"),
    "e7810a71-73ae-499d-8c15-faa9aef0c3f2": ("Paperang / Generic BLE Thermal",   50, "BLE_ESCPOS"),
    "49535343-fe7d-4ae5-8fa9-9fafd205e455": ("Microchip ISSC BLE Serial",         45, "BLE_ESCPOS"),
    "49535343-1e4d-4bd9-ba61-23c647249616": ("Microchip ISSC BLE Serial Alt",     45, "BLE_ESCPOS"),
    "49535343-8841-43f4-a818-efb0667d8b06": ("Microchip ISSC TX Char",            40, "BLE_ESCPOS"),
    "0000ff00-0000-1000-8000-00805f9b34fb": ("Custom Printer Service FF00",       45, "BLE_ESCPOS"),
    "0000ff01-0000-1000-8000-00805f9b34fb": ("Custom Printer Char FF01",          40, "BLE_ESCPOS"),
    "0000ff02-0000-1000-8000-00805f9b34fb": ("Custom Printer Char FF02",          40, "BLE_ESCPOS"),
    "0000ffe0-0000-1000-8000-00805f9b34fb": ("BLE UART Service FFE0 (HM-10)",     40, "BLE_ESCPOS"),
    "0000ffe1-0000-1000-8000-00805f9b34fb": ("BLE UART Char FFE1 (HM-10)",        40, "BLE_ESCPOS"),
    "0000fff0-0000-1000-8000-00805f9b34fb": ("Custom Service FFF0",               35, "BLE_ESCPOS"),
    "0000fff1-0000-1000-8000-00805f9b34fb": ("Custom Char FFF1",                  30, "BLE_ESCPOS"),
    "0000fff2-0000-1000-8000-00805f9b34fb": ("Custom Char FFF2",                  30, "BLE_ESCPOS"),
    "0000ae30-0000-1000-8000-00805f9b34fb": ("iPrint / Cat Printer Service AE30", 50, "BLE_CAT"),
    "0000ae01-0000-1000-8000-00805f9b34fb": ("iPrint / Cat Write Char AE01",      45, "BLE_CAT"),
    "0000ae02-0000-1000-8000-00805f9b34fb": ("iPrint / Cat Notify Char AE02",     40, "BLE_CAT"),
    "be188000-0000-1000-8000-00805f9b34fb": ("Niimbot Service",                   50, "BLE_ESCPOS"),
    "be188001-0000-1000-8000-00805f9b34fb": ("Niimbot Write Char",                45, "BLE_ESCPOS"),
    "be188002-0000-1000-8000-00805f9b34fb": ("Niimbot Notify Char",               40, "BLE_ESCPOS"),
    "00001800-0000-1000-8000-00805f9b34fb": ("Generic Access (clone signal)",      10, None),
    "00001801-0000-1000-8000-00805f9b34fb": ("Generic Attribute Profile",           5, None),
    "38eb4a80-c570-11e3-9507-0002a5d5c51b": ("Zebra ZPL BLE Service",             50, "ZPL"),
    "0000180a-0000-1000-8000-00805f9b34fb": ("Device Information (Star hint)",     15, None),
}

OUI_DB = {
    "00:09:1f": ("Zebra Technologies",      45, "ZPL"),
    "00:1c:97": ("Zebra Technologies",      45, "ZPL"),
    "ac:3f:a4": ("Zebra Technologies",      45, "ZPL"),
    "00:07:4d": ("Star Micronics",          45, "STAR"),
    "00:12:f3": ("Star Micronics",          45, "STAR"),
    "00:01:90": ("Citizen Systems",         40, "BLE_ESCPOS"),
    "00:18:e4": ("Bixolon",                 40, "BLE_ESCPOS"),
    "c8:fd:19": ("Phomemo / Generic Clone", 40, "BLE_ESCPOS"),
    "a4:c1:38": ("Telink Semi clone",       30, "BLE_ESCPOS"),
    "00:02:5b": ("HPRT",                    40, "BLE_ESCPOS"),
    "3c:71:bf": ("Espressif ESP32 clone",   20, "BLE_ESCPOS"),
    "24:6f:28": ("Espressif ESP32 clone",   20, "BLE_ESCPOS"),
    "30:ae:a4": ("Espressif ESP32 clone",   20, "BLE_ESCPOS"),
    "40:f5:20": ("Espressif ESP32 clone",   20, "BLE_ESCPOS"),
}


# ═══════════════════════════════════════════════════════════════════════════════
#  DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

W = 64

def _supports_output(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return True
    except UnicodeEncodeError:
        return False

USE_UNICODE_UI = _supports_output("═✓•█▓→")
H_LINE = "─" if USE_UNICODE_UI else "-"
H_HEAVY = "═" if USE_UNICODE_UI else "="
OK_MARK = "✓" if USE_UNICODE_UI else "+"
FAIL_MARK = "✗" if USE_UNICODE_UI else "x"
INFO_MARK = "•" if USE_UNICODE_UI else "-"
BAR_FILLED = "█" if USE_UNICODE_UI else "#"
BAR_EMPTY = "░" if USE_UNICODE_UI else "."
SIGNAL_5 = "▓▓▓▓▓" if USE_UNICODE_UI else "#####"
SIGNAL_4 = "▓▓▓▓░" if USE_UNICODE_UI else "####."
SIGNAL_3 = "▓▓▓░░" if USE_UNICODE_UI else "###.."
SIGNAL_2 = "▓▓░░░" if USE_UNICODE_UI else "##..."
SIGNAL_1 = "▓░░░░" if USE_UNICODE_UI else "#...."

def sep(c=H_LINE): print(c * W)
def header(t):    sep(H_HEAVY); print(f"  {t}"); sep(H_HEAVY)
def section(t):   print(); sep(); print(f"  {t}"); sep()
def ok(msg):      print(f"  {OK_MARK}  {msg}")
def fail(msg):    print(f"  {FAIL_MARK}  {msg}")
def info(msg):    print(f"  {INFO_MARK}  {msg}")
def step(n, msg): print(f"\n  [{n}] {msg}")

def confidence_bar(score):
    filled = min(int(score / 5), 20)
    bar    = BAR_FILLED * filled + BAR_EMPTY * (20 - filled)
    level  = ("VERY HIGH" if score >= 80 else "HIGH"   if score >= 60
              else "MEDIUM"   if score >= 40 else "LOW")
    return f"[{bar}] {score}%  {level}"

def rssi_label(rssi):
    if rssi is None: return "N/A"
    if rssi >= -60:  q = f"{SIGNAL_5} Excellent"
    elif rssi >= -70: q = f"{SIGNAL_4} Good"
    elif rssi >= -80: q = f"{SIGNAL_3} Fair"
    elif rssi >= -90: q = f"{SIGNAL_2} Weak"
    else:             q = f"{SIGNAL_1} Very Weak"
    return f"{rssi} dBm  {q}"


# ═══════════════════════════════════════════════════════════════════════════════
#  SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def score_device(name, uuids, mfr_data, address):
    score, reasons, protos = 0, [], set()
    name_lower = (name or "").lower()

    for kw, pts in PRINTER_KEYWORDS.items():
        if kw in name_lower:
            score += pts
            reasons.append(f"name matches '{kw}' (+{pts})")
            if score >= 100:
                break   # already maxed — skip remaining keywords

    for uuid in uuids:
        info_ = PRINTER_UUIDS.get(uuid.lower())
        if info_:
            label, pts, proto = info_
            score += pts
            reasons.append(f"UUID {uuid[:8]}… = {label} (+{pts})")
            if proto: protos.add(proto)

    oui = ":".join(address.lower().split(":")[:3])
    if oui in OUI_DB:
        vendor, pts, proto = OUI_DB[oui]
        score += pts
        reasons.append(f"OUI {oui} = {vendor} (+{pts})")
        if proto: protos.add(proto)

    if mfr_data:
        for mfr_id in mfr_data:
            if mfr_id not in (0x004C, 0x0006, 0x0001):
                score += 10
                reasons.append(f"Manufacturer ID 0x{mfr_id:04X} present (+10)")

    if 1 <= len(uuids) <= 5:
        score += 5
        reasons.append(f"{len(uuids)} UUID(s) — typical printer count (+5)")
    elif len(uuids) > 8:
        score = max(0, score - 15)
        reasons.append(f"{len(uuids)} UUIDs — likely not a printer (-15)")

    if not name or name == "(unknown)":
        score += 5
        reasons.append("No broadcast name — some printers hide name (+5)")

    score = min(score, 100)

    # Deterministic protocol priority: first match wins
    primary = None
    for p in ("SPP", "ZPL", "STAR", "BLE_CAT", "BLE_ESCPOS"):
        if p in protos:
            primary = p
            break
    if primary is None and score >= CONFIDENCE_THRESHOLD:
        primary = "BLE_ESCPOS"

    # Return protos as a sorted list so callers get stable ordering
    return {"score": score, "reasons": reasons, "protos": sorted(protos), "protocol": primary}


# ═══════════════════════════════════════════════════════════════════════════════
#  GATT PROBE  (shared by scanner and printer)
# ═══════════════════════════════════════════════════════════════════════════════

async def _probe_escpos_write_mode(client, write_uuid, modes):
    response_modes = []
    if modes.get("without_response"):
        response_modes.append(False)
    if modes.get("with_response"):
        response_modes.append(True)
    if not response_modes:
        response_modes = [False, True]

    last_error = None
    for response_mode in response_modes:
        try:
            await client.write_gatt_char(write_uuid, ESCPOS_INIT, response=response_mode)
            await asyncio.sleep(0.4)
            await client.write_gatt_char(write_uuid, ESCPOS_STATUS, response=response_mode)
            await asyncio.sleep(0.4)
            return response_mode
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("No valid write mode available for ESC/POS probe")


async def probe_printer(device):
    addr = device["address"]
    name = device["name"]
    section(f"GATT Probe: {name}  [{addr}]")

    probe = {
        "connected": False, "mtu": None,
        "writable_chars": [], "notify_chars": [],
        "write_modes": {},
        "escpos_ok": False, "raw_values": {},
    }

    try:
        async with BleakClient(addr, timeout=15) as client:
            _ = client.mtu_size  # Access mtu_size to trigger MTU acquisition in Bleak 3.0+
            probe["connected"] = True
            probe["mtu"]       = client.mtu_size
            ok(f"Connected!   MTU: {client.mtu_size} bytes\n")

            for svc in client.services:
                svc_uuid = str(svc.uuid).lower()
                svc_info = PRINTER_UUIDS.get(svc_uuid, ("Unknown Service", 0, None))
                print(f"  Service  {svc.uuid}  ← {svc_info[0]}")

                for char in svc.characteristics:
                    props     = ", ".join(char.properties)
                    char_info = PRINTER_UUIDS.get(str(char.uuid).lower(), ("", 0, None))
                    lbl       = f"  ← {char_info[0]}" if char_info[0] else ""
                    print(f"    Char   {char.uuid}  [{props}]{lbl}")

                    if "write" in char.properties or "write-without-response" in char.properties:
                        char_uuid = str(char.uuid)
                        probe["writable_chars"].append(char_uuid)
                        probe["write_modes"][char_uuid] = {
                            "with_response": "write" in char.properties,
                            "without_response": "write-without-response" in char.properties,
                        }
                        print(f"           ↑ WRITABLE")

                    if "notify" in char.properties or "indicate" in char.properties:
                        probe["notify_chars"].append(str(char.uuid))
                        print(f"           ↑ NOTIFY")

                    if "read" in char.properties:
                        try:
                            val = await client.read_gatt_char(char.uuid)
                            if val:
                                try:
                                    decoded = val.decode("utf-8", errors="replace").strip()
                                except Exception:
                                    decoded = val.hex()
                                if decoded:
                                    print(f"           Value: {decoded}")
                                    probe["raw_values"][str(char.uuid)] = decoded
                        except Exception:
                            pass
                print()

            if probe["writable_chars"]:
                ae01_writes = [
                    u for u in probe["writable_chars"]
                    if u.lower().startswith("0000ae01")
                ]
                known_writes = [
                    u for u in probe["writable_chars"]
                    if u.lower() in PRINTER_UUIDS
                ]
                wc = (ae01_writes or known_writes or probe["writable_chars"])[0]
                wc_lower = wc.lower()
                if wc_lower.startswith("0000ae01") or wc_lower.startswith("0000ae30"):
                    device["protocol"] = "BLE_CAT"
                    if "BLE_CAT" not in device["protos"]:
                        device["protos"].append("BLE_CAT")
                    info(f"Detected iPrint/Cat protocol on {wc} — skipping ESC/POS probe")
                else:
                    print(f"  Probing ESC/POS on {wc} …")
                    try:
                        response_mode = await _probe_escpos_write_mode(
                            client,
                            wc,
                            probe["write_modes"].get(wc, {}),
                        )
                        probe["escpos_ok"] = True
                        device["protocol"] = "BLE_ESCPOS"
                        if "BLE_ESCPOS" not in device["protos"]:
                            device["protos"].append("BLE_ESCPOS")
                        ok("ESC/POS INIT accepted — printer speaks ESC/POS!")
                    except Exception as ex:
                        fail(f"ESC/POS write failed: {ex}")
            else:
                fail("No writable characteristics found.")

    except Exception as e:
        err = str(e).lower()
        fail(f"Connection failed: {e}")
        if "not found" in err or "unreachable" in err:
            info("Printer may be asleep. Wake it and retry.")
        elif "already connected" in err:
            info("Printer is paired elsewhere. Disconnect it first.")
        else:
            info("Make sure printer is in pairing mode (LED blinking).")

    device["probe"] = probe
    return device


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════

import json

def save_config(device, write_uuid):
    protocol = device.get("protocol")
    write_lower = (write_uuid or "").lower()
    if write_lower.startswith("0000ae01") or write_lower.startswith("0000ae30"):
        protocol = "BLE_CAT"

    probe = device.get("probe", {})
    write_modes = probe.get("write_modes", {})
    selected_modes = write_modes.get(write_uuid, {})

    cfg = {
        "address":    device["address"],
        "name":       device["name"],
        "write_uuid": write_uuid,
        "protocol":   protocol,
        "mtu":        probe.get("mtu"),
        "write_with_response": selected_modes.get("with_response", False),
        "write_without_response": selected_modes.get("without_response", True),
        "registered": True,
    }
    os.makedirs(CONFIG_DIR, exist_ok=True)
    temp_file = f"{CONFIG_FILE}.tmp"
    last_error = None
    for _ in range(5):
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(temp_file, CONFIG_FILE)
            last_error = None
            break
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.25)
        finally:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except OSError:
                pass
    if last_error is not None:
        raise last_error
    ok(f"Config saved → {CONFIG_FILE}")

def _migrate_legacy_config():
    legacy_file = _legacy_config_file()
    if legacy_file == CONFIG_FILE:
        return
    if os.path.exists(CONFIG_FILE) or not os.path.exists(legacy_file):
        return

    os.makedirs(CONFIG_DIR, exist_ok=True)
    shutil.copy2(legacy_file, CONFIG_FILE)
    info(f"Migrated printer config to {CONFIG_FILE}")

def load_config():
    _migrate_legacy_config()
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  POWERSHELL HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def hidden_subprocess_kwargs():
    if platform.system() != "Windows":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def ps(cmd):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=30,
        **hidden_subprocess_kwargs(),
    )

def is_admin():
    try:
        r = ps("([Security.Principal.WindowsPrincipal]"
               "[Security.Principal.WindowsIdentity]::GetCurrent()"
               ").IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")
        return r.stdout.strip().lower() == "true"
    except Exception:
        return False
