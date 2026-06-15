"""
bt_print.py — Direct Bluetooth Printer Controller
=================================================
Prints directly to the saved BLE printer config from bt_scan.py.

Usage:
    python bt_print.py --test-page
    python bt_print.py --print-text "Hi"
    python bt_print.py --print-image photo.jpg
    python bt_print.py --print-pdf file.pdf
"""

import asyncio
import sys
import os
import platform
import argparse
import re
import subprocess
from functools import lru_cache

from bt_shared import (
    CONFIG_FILE,
    RELAY_CHUNK_SIZE, RELAY_CHUNK_DELAY,
    PAPER_WIDTH_DOTS, PAPER_WIDTH_MM, PAPER_WIDTH_BYTES,
    ESCPOS_TEST,
    BLEAK_AVAILABLE,
    W, sep, header, section, ok, fail, info, step,
    load_config, hidden_subprocess_kwargs,
)

try:
    from bleak import BleakClient
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
#  FORMAT DETECTION & AUTO-CONVERSION PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def detect_format(data: bytes) -> str:
    """Classify print job bytes into a format string."""
    if len(data) < 4:
        return "UNKNOWN"
    head = data[:512]
    if data[:2] in (b"\x1b\x40", b"\x1b\x61", b"\x1d\x56") or (
        data[0] == 0x1b and data[1] in (0x40, 0x61, 0x21, 0x4d, 0x70)
    ):
        return "ESCPOS"
    if data[:4] == b"%PDF":
        return "PDF"
    if (
        head.startswith(b"%!PS")
        or b"PS-Adobe" in head
        or b"PScript5.dll" in head
        or b"%%Creator:" in head
    ):
        return "PS"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:2] == b"\xff\xd8":
        return "JPEG"
    if data[:4] in (b"\x01\x00\x00\x00", b"\x02\x00\x00\x00") and len(data) > 80:
        return "EMF"
    if data[:2] == b"\x1b\x45" or data[:9] == b"\x1b%-12345X":
        return "PCL"
    if data[:2] == b"PK":
        return "XPS"
    nul_ratio = data[:512].count(0) / max(1, len(data[:512]))
    if nul_ratio > 0.2:
        try:
            sample = data[:512].decode("utf-16-le", errors="strict")
            if any(c.isalnum() for c in sample):
                return "TEXT"
        except UnicodeDecodeError:
            pass
    try:
        sample = data[:512].decode("utf-8", errors="strict")
        if all(c.isprintable() or c in "\r\n\t" for c in sample):
            return "TEXT"
    except UnicodeDecodeError:
        pass
    try:
        sample = data[:512].decode("latin-1")
        if sum(1 for c in sample if c.isprintable() or c in "\r\n\t") / len(sample) > 0.85:
            return "TEXT"
    except Exception:
        pass
    return "UNKNOWN"


def convert_to_escpos(data: bytes, fmt: str) -> bytes:
    """Route format to correct converter. Returns ESC/POS bytes."""
    if fmt == "ESCPOS":
        payload = data if data[:2] == b"\x1b\x40" else b"\x1b\x40" + data
        return smart_trim_escpos(payload)   # trim blank trailing rows
    if fmt == "TEXT":
        return text_to_escpos(data)
    # PNG/JPEG/EMF/PDF/PS/PCL all go through raster conversion which trims
    # at pixel level — no need to also run smart_trim_escpos on top.
    if fmt in ("PNG", "JPEG"):
        return image_bytes_to_escpos(data)
    if fmt in ("EMF", "XPS"):
        result = emf_to_escpos(data)
        if result:
            return result
        info(f"{fmt} rasterization unavailable — extracting text…")
        return extract_text_escpos(data)
    if fmt == "PDF":
        result = pdf_to_escpos(data)
        if result:
            return result
        info("PDF rasterization unavailable — extracting text…")
        return extract_text_escpos(data)
    if fmt == "PS":
        result = ps_to_escpos(data)
        if result:
            return result
        info("PostScript rasterization unavailable — extracting text…")
        return extract_text_escpos(data)
    if fmt == "PCL":
        result = pcl_to_escpos(data)
        if result:
            return result
        info("PCL conversion unavailable — extracting text…")
        return extract_text_escpos(data)
    info("Unknown format — attempting text extraction…")
    return extract_text_escpos(data)


# DPI_MM: raster rows per mm at 203 dpi
_DPI_MM = 203 / 25.4   # ≈ 7.99 rows/mm

# GS V cut command total byte lengths keyed by subcommand byte
_GS_V_LEN = {
    0x00: 3,   # GS V 0   — full cut
    0x01: 3,   # GS V 1   — partial cut
    0x30: 3,   # GS V '0' — full cut (ASCII)
    0x31: 3,   # GS V '1' — partial cut (ASCII)
    0x41: 4,   # GS V A n — partial cut + feed
    0x42: 4,   # GS V B n — full cut + feed
}
_ESC_CUT = b"\x1d\x56\x41\x03"   # partial cut, 3-dot feed


def smart_trim_escpos(data: bytes) -> bytes:
    """
    Walk an ESC/POS byte stream, trim trailing blank rows from every
    GS v 0 raster block, strip all existing cut commands, then append
    a single clean partial cut at actual content height.

    Turns a 200mm Windows page into a receipt that cuts right after
    the last printed line, saving paper proportional to blank space.
    """
    MARGIN_ROWS = 8   # blank rows kept after last content (~1mm bottom margin)

    out     = bytearray()
    i       = 0
    trimmed = 0

    while i < len(data):

        # ── GS v 0 raster block ───────────────────────────────────────────────
        if (i + 8 <= len(data)
                and data[i]   == 0x1D
                and data[i+1] == 0x76
                and data[i+2] == 0x30):

            m          = data[i+3]
            xL, xH     = data[i+4], data[i+5]
            yL, yH     = data[i+6], data[i+7]
            bpr        = xL | (xH << 8)
            row_count  = yL | (yH << 8)
            hdr_end    = i + 8
            body_end   = hdr_end + bpr * row_count

            if bpr > 0 and row_count > 0 and body_end <= len(data):
                raster = data[hdr_end:body_end]

                # Scan backwards — O(n) with early exit on first content row
                last_content = -1
                for row in range(row_count - 1, -1, -1):
                    if any(raster[row * bpr : row * bpr + bpr]):
                        last_content = row
                        break

                if last_content == -1:
                    trimmed += row_count
                    info(f"Dropped fully blank raster block ({row_count} rows)")
                    i = body_end
                    continue

                keep = min(last_content + 1 + MARGIN_ROWS, row_count)
                cut  = row_count - keep
                trimmed += cut

                if cut > 0:
                    info(f"Trimmed {cut} blank rows ({cut / _DPI_MM:.1f}mm saved)")

                new_yL = keep & 0xFF
                new_yH = (keep >> 8) & 0xFF
                out += bytes([0x1D, 0x76, 0x30, m, xL, xH, new_yL, new_yH])
                out += raster[:keep * bpr]
                i = body_end
                continue

        # ── Strip all GS V cut variants — append one clean cut at the end ─────
        if (i + 3 <= len(data)
                and data[i]   == 0x1D
                and data[i+1] == 0x56):
            sub    = data[i+2]
            length = _GS_V_LEN.get(sub, 3)
            i += length
            continue

        out.append(data[i])
        i += 1

    if trimmed > 0:
        ok(f"Auto-cut: {trimmed} blank rows removed ({trimmed / _DPI_MM:.1f}mm paper saved)")

    out += _ESC_CUT
    return bytes(out)


# ── Text ──────────────────────────────────────────────────────────────────────

COLS = 32   # characters per line at 58mm
CAT_TEXT_FONT_SIZE = 24
CAT_TEXT_LINE_PAD = 4
CAT_TEXT_LEFT_PAD = 8
CAT_TEXT_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Trebuchet MS.ttf",
    "/System/Library/Fonts/Supplemental/Verdana.ttf",
    "arial.ttf",
    "Arial.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _wrap_text_line(line: str, cols: int) -> list[str]:
    """Wrap a line to a fixed column width, splitting long tokens when needed."""
    if cols <= 0:
        return [line]
    if len(line) <= cols:
        return [line]

    words = line.split()
    if not words:
        return [line[i:i + cols] for i in range(0, len(line), cols)] or [""]

    out: list[str] = []
    cur = ""
    for word in words:
        if len(word) > cols:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(word[i:i + cols] for i in range(0, len(word), cols))
            continue
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= cols:
            cur += " " + word
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out or [""]

def sanitize_text_payload(text: str) -> str:
    """
    Normalize decoded spool text before wrapping/rasterization.
    This removes control noise that can explode a short Windows text job into
    thousands of tiny rendered lines.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    text = text.replace("\f", "\n").replace("\v", "\n")

    cleaned = []
    for ch in text:
        if ch == "\n" or ch == "\t":
            cleaned.append(ch)
        elif ch.isprintable():
            cleaned.append(ch)

    text = "".join(cleaned)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = [line.strip() for line in text.split("\n")]
    short_lines = [line for line in lines if line]
    if short_lines:
        single_char_ratio = sum(1 for line in short_lines if len(line) == 1) / len(short_lines)
        if single_char_ratio >= 0.7 and len(short_lines) >= 12:
            rebuilt = []
            current = []
            for line in short_lines:
                if len(line) == 1:
                    current.append(line)
                    continue
                if current:
                    rebuilt.append("".join(current))
                    current = []
                rebuilt.append(line)
            if current:
                rebuilt.append("".join(current))
            text = "\n".join(rebuilt)

    return text.strip()

def decode_text_payload(data: bytes) -> str:
    """
    Decode spool text robustly.
    Firefox/Windows sometimes send UTF-16LE or NUL-padded text jobs, which
    otherwise render as one visible character per line after split/wrap.
    """
    sample = data[:1024]
    nul_ratio = sample.count(0) / max(1, len(sample))

    if nul_ratio > 0.2:
        try:
            text = data.decode("utf-16-le", errors="replace")
        except Exception:
            text = data.decode("latin-1", errors="replace")
    else:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("latin-1", errors="replace")

    return sanitize_text_payload(text)


def extract_printable_runs(data: bytes, min_run: int = 6) -> str:
    """Pull long printable runs out of mixed binary spool data."""
    import re as _re

    runs = _re.findall(rb"[ -~\t\r\n]{%d,}" % min_run, data)
    if not runs:
        return ""
    text = "\n".join(r.decode("latin-1", errors="replace") for r in runs)
    return sanitize_text_payload(text)


@lru_cache(maxsize=1)
def _load_cat_text_font():
    from PIL import ImageFont

    for candidate in CAT_TEXT_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, CAT_TEXT_FONT_SIZE), candidate
        except Exception:
            continue

    try:
        return ImageFont.load_default(size=CAT_TEXT_FONT_SIZE), "Pillow default"
    except TypeError:
        return ImageFont.load_default(), "Pillow default"


def _font_bbox(font, text: str) -> tuple[int, int, int, int]:
    if hasattr(font, "getbbox"):
        return font.getbbox(text)
    width, height = font.getsize(text)
    return (0, 0, width, height)

def text_to_escpos(data: bytes) -> bytes:
    text = decode_text_payload(data)
    wrapped = "\n".join(
        "\n".join(_wrap_text_line(line, COLS))
        for line in text.splitlines()
    )
    body    = wrapped.encode("latin-1", errors="replace")
    return (
        b"\x1b\x40"                 # init
        + b"\x1b\x61\x00"           # left align
        + b"\x1b\x4d\x00"           # font A
        + body
        + b"\n\n\n"
        + b"\x1d\x56\x41\x03"       # partial cut
    )


# ── Image ─────────────────────────────────────────────────────────────────────

def pil_1bit_to_escpos(img) -> bytes:
    """
    Convert a PIL 1-bit image to ESC/POS GS v 0 raster bytes.
    Trims trailing blank rows (bottom-up scan, early exit) so the
    cut happens at content height, not full page height.
    """
    MARGIN_ROWS = 16   # ~2mm bottom margin below last content row

    w, h   = img.size
    pixels = img.load()
    # Scan from bottom up — exits as soon as first content row is found
    last_content_row = -1
    for y in range(h - 1, -1, -1):
        for x in range(w):
            if pixels[x, y] == 0:       # 0 = black in PIL mode "1"
                last_content_row = y
                break
        if last_content_row != -1:
            break

    if last_content_row == -1:
        if False:
            return b""
        info("Image is entirely blank — skipping")
        info("Image is entirely blank — skipping")

    crop_h = min(last_content_row + 1 + MARGIN_ROWS, h)
    if crop_h < h:
        saved = h - crop_h
        info(f"Auto-trim: {saved} blank rows removed ({saved / _DPI_MM:.1f}mm saved)")
        img    = img.crop((0, 0, w, crop_h))
        h      = crop_h
        pixels = img.load()

    # Encode to GS v 0 packed raster
    bpr    = (w + 7) // 8
    xL, xH = bpr & 0xFF, (bpr >> 8) & 0xFF
    yL, yH = h   & 0xFF, (h   >> 8) & 0xFF
    hdr    = bytes([0x1D, 0x76, 0x30, 0x00, xL, xH, yL, yH])
    raster = bytearray(bpr * h)         # pre-allocate to avoid repeated resizing

    for y in range(h):
        for xb in range(bpr):
            byte = 0
            for bit in range(8):
                x = xb * 8 + bit
                if x < w and pixels[x, y] == 0:
                    byte |= (0x80 >> bit)
            raster[y * bpr + xb] = byte

    return (
        b"\x1b\x40"
        + b"\x1b\x61\x01"
        + hdr + bytes(raster)
        + b"\x1b\x61\x00"
        + b"\n\n\n"
        + _ESC_CUT
    )


def _open_and_scale_image(source) -> "object | None":
    """
    Shared helper: open from file path or bytes, greyscale, scale to paper
    width, auto-contrast, sharpen, dither to 1-bit. Returns PIL image or None.
    """
    try:
        from PIL import Image, ImageOps, ImageFilter
        import io as _io

        if isinstance(source, (bytes, bytearray)):
            with Image.open(_io.BytesIO(source)) as opened:
                img = opened.convert("L")
        else:
            with Image.open(source) as opened:
                img = opened.convert("L")

        w, h  = img.size
        new_w = PAPER_WIDTH_DOTS
        new_h = int(h * (new_w / w))
        img   = img.resize((new_w, new_h), Image.LANCZOS)
        img   = ImageOps.autocontrast(img, cutoff=2)
        img   = img.filter(ImageFilter.SHARPEN)
        return img.convert("1")
    except ImportError:
        fail("Pillow not installed. Run:  pip install Pillow")
        return None
    except Exception as e:
        fail(f"Image open/scale failed: {e}")
        return None


def image_bytes_to_escpos(data: bytes) -> bytes:
    img = _open_and_scale_image(data)
    return pil_1bit_to_escpos(img) if img is not None else b""


def image_file_to_escpos(path: str) -> bytes:
    img = _open_and_scale_image(path)
    if img is None:
        return b""
    ok(f"Image scaled to {img.size[0]}×{img.size[1]}px")
    return pil_1bit_to_escpos(img)


# ── EMF / GDI ─────────────────────────────────────────────────────────────────

def emf_to_escpos(data: bytes):

    tmp_path = None
    tmp_emf = None
    tmp_png = None

    # Method A: pywin32
    try:
        import win32ui, win32api
        from PIL import Image, ImageOps
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".emf", delete=False)
        tmp.write(data); tmp.close()
        tmp_path = tmp.name

        PX_W, PX_H = PAPER_WIDTH_DOTS, PAPER_WIDTH_DOTS * 3
        dc  = win32ui.CreateDC(); dc.CreateCompatibleDC(None)
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(dc, PX_W, PX_H)
        dc.SelectObject(bmp)
        dc.FillSolidRect((0, 0, PX_W, PX_H), win32api.RGB(255, 255, 255))
        win32api.PlayEnhMetaFile(dc.GetSafeHdc(), tmp.name, (0, 0, PX_W, PX_H))
        dc.DeleteDC()

        bmpinfo = bmp.GetInfo()
        img = Image.frombuffer("RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1).convert("L")
        img = ImageOps.autocontrast(img, cutoff=3).convert("1")
        ok("EMF rasterized via pywin32")
        return pil_1bit_to_escpos(img)
    except ImportError:
        pass
    except Exception as e:
        info(f"pywin32 EMF failed: {e}")

    # Method B: PowerShell .NET System.Drawing
    try:
        import tempfile
        from PIL import Image, ImageOps

        tmp_emf = tempfile.NamedTemporaryFile(suffix=".emf", delete=False)
        tmp_emf.write(data); tmp_emf.close()
        tmp_png = tmp_emf.name.replace(".emf", ".png")

        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
            f'Add-Type -AssemblyName System.Drawing;'
            f'$img=[System.Drawing.Image]::FromFile("{tmp_emf.name}");'
            f'$bmp=New-Object System.Drawing.Bitmap({PAPER_WIDTH_DOTS},'
            f'[int]($img.Height*{PAPER_WIDTH_DOTS}/$img.Width));'
            f'$g=[System.Drawing.Graphics]::FromImage($bmp);'
            f'$g.Clear([System.Drawing.Color]::White);'
            f'$g.DrawImage($img,0,0,$bmp.Width,$bmp.Height);'
            f'$bmp.Save("{tmp_png}");'
            f'$g.Dispose();$img.Dispose();$bmp.Dispose()'
        ], capture_output=True, timeout=15, **hidden_subprocess_kwargs())

        if r.returncode == 0 and os.path.exists(tmp_png):
            with Image.open(tmp_png) as opened:
                img = opened.convert("L")
            img = ImageOps.autocontrast(img, cutoff=3).convert("1")
            result = pil_1bit_to_escpos(img)
            ok("EMF rasterized via PowerShell/.NET")
            return result
    except Exception as e:
        info(f"PowerShell EMF failed: {e}")
    finally:
        _safe_unlink(tmp_path)
        _safe_unlink(getattr(tmp_emf, "name", None))
        _safe_unlink(tmp_png)

    return None


# ── PDF / PCL via Ghostscript ─────────────────────────────────────────────────

def _gs_rasterize(data: bytes, ext: str):
    import tempfile, shutil
    from PIL import Image, ImageOps

    gs = shutil.which("gswin64c") or shutil.which("gswin32c") or shutil.which("gs")
    if not gs:
        info("Ghostscript not found — install from https://ghostscript.com")
        return None

    tmp_in  = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_in.write(data); tmp_in.close()
    tmp_out = tmp_in.name.replace(ext, ".png")

    try:
        r = subprocess.run([
            gs, "-dBATCH", "-dNOPAUSE", "-dQUIET",
            "-sDEVICE=pnggray", "-r203",
            f"-dDEVICEWIDTHPOINTS={PAPER_WIDTH_MM * 2.835:.0f}",
            "-dFIXEDMEDIA", "-dPDFFitPage",
            f"-sOutputFile={tmp_out}", tmp_in.name
        ], capture_output=True, timeout=30, **hidden_subprocess_kwargs())

        if r.returncode == 0 and os.path.exists(tmp_out):
            with Image.open(tmp_out) as opened:
                img = opened.convert("L")
            img = ImageOps.autocontrast(img, cutoff=3).convert("1")
            result = pil_1bit_to_escpos(img)
            ok(f"Ghostscript rasterized {ext.upper()}")
            return result
        info(f"Ghostscript error: {r.stderr.decode(errors='replace')[:200]}")
    except Exception as e:
        info(f"Ghostscript failed: {e}")
    finally:
        _safe_unlink(tmp_in.name)
        _safe_unlink(tmp_out)
    return None


def pdf_to_escpos(data):   return _gs_rasterize(data, ".pdf")
def ps_to_escpos(data):    return _gs_rasterize(data, ".ps")
def pcl_to_escpos(data):   return _gs_rasterize(data, ".pcl")


# ── Text extraction fallback ──────────────────────────────────────────────────

def extract_text_escpos(data: bytes) -> bytes:
    import re as _re
    if data[:1024].count(0) / max(1, len(data[:1024])) > 0.2:
        text = decode_text_payload(data)
        if text.strip():
            info("Decoded NUL-padded text payload")
            return text_to_escpos(text.encode("utf-8", errors="replace"))
    runs = re.findall(rb"[ -~\t\r\n]{4,}", data)
    if not runs:
        fail("No readable text found in print job.")
        return b"\x1b\x40[No printable content]\n\n\n\x1d\x56\x41\x03"
    info(f"Extracted {len(runs)} text runs from binary job")
    return text_to_escpos(b"\n".join(runs))


def _safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass



# ═══════════════════════════════════════════════════════════════════════════════
#  CAT / GB01 / P1-SERIES NATIVE PROTOCOL
#  (used by any printer with write UUID 0000ae01 — these do NOT speak ESC/POS)
#
#  Full protocol documented at:
#  https://github.com/JJJollyjim/catprinter/blob/master/COMMANDS.md
#
#  Frame format:
#    [0x51, 0x78, opcode, 0x00, len_lo, 0x00, <data bytes>, crc8, 0xFF]
#
#  Print sequence:
#    1. latticeStart  (0xA6)
#    2. setEnergy     (0xAF)   — print darkness
#    3. setDrawMode   (0xBE)   — 0x00=image
#    4. setQuality    (0xA4)   — 0x33
#    5. For each row: printRow (0xA2) — 48 bytes, LSB-first
#    6. feedPaper     (0xA1)   — 25 blank rows
#    7. latticeEnd    (0xA6)
# ═══════════════════════════════════════════════════════════════════════════════

def _is_cat_printer(write_uuid: str) -> bool:
    """True for any printer that uses the ae01/ae30 cat-printer protocol."""
    u = write_uuid.lower()
    return u.startswith("0000ae01") or u.startswith("0000ae30")


# CRC-8 lookup table (CCITT polynomial 0x07)
_CRC8_TABLE = bytes([
    0x00,0x07,0x0e,0x09,0x1c,0x1b,0x12,0x15,0x38,0x3f,0x36,0x31,0x24,0x23,0x2a,0x2d,
    0x70,0x77,0x7e,0x79,0x6c,0x6b,0x62,0x65,0x48,0x4f,0x46,0x41,0x54,0x53,0x5a,0x5d,
    0xe0,0xe7,0xee,0xe9,0xfc,0xfb,0xf2,0xf5,0xd8,0xdf,0xd6,0xd1,0xc4,0xc3,0xca,0xcd,
    0x90,0x97,0x9e,0x99,0x8c,0x8b,0x82,0x85,0xa8,0xaf,0xa6,0xa1,0xb4,0xb3,0xba,0xbd,
    0xc7,0xc0,0xc9,0xce,0xdb,0xdc,0xd5,0xd2,0xff,0xf8,0xf1,0xf6,0xe3,0xe4,0xed,0xea,
    0xb7,0xb0,0xb9,0xbe,0xab,0xac,0xa5,0xa2,0x8f,0x88,0x81,0x86,0x93,0x94,0x9d,0x9a,
    0x27,0x20,0x29,0x2e,0x3b,0x3c,0x35,0x32,0x1f,0x18,0x11,0x16,0x03,0x04,0x0d,0x0a,
    0x57,0x50,0x59,0x5e,0x4b,0x4c,0x45,0x42,0x6f,0x68,0x61,0x66,0x73,0x74,0x7d,0x7a,
    0x89,0x8e,0x87,0x80,0x95,0x92,0x9b,0x9c,0xb1,0xb6,0xbf,0xb8,0xad,0xaa,0xa3,0xa4,
    0xf9,0xfe,0xf7,0xf0,0xe5,0xe2,0xeb,0xec,0xc1,0xc6,0xcf,0xc8,0xdd,0xda,0xd3,0xd4,
    0x69,0x6e,0x67,0x60,0x75,0x72,0x7b,0x7c,0x51,0x56,0x5f,0x58,0x4d,0x4a,0x43,0x44,
    0x19,0x1e,0x17,0x10,0x05,0x02,0x0b,0x0c,0x21,0x26,0x2f,0x28,0x3d,0x3a,0x33,0x34,
    0x4e,0x49,0x40,0x47,0x52,0x55,0x5c,0x5b,0x76,0x71,0x78,0x7f,0x6a,0x6d,0x64,0x63,
    0x3e,0x39,0x30,0x37,0x22,0x25,0x2c,0x2b,0x06,0x01,0x08,0x0f,0x1a,0x1d,0x14,0x13,
    0xae,0xa9,0xa0,0xa7,0xb2,0xb5,0xbc,0xbb,0x96,0x91,0x98,0x9f,0x8a,0x8d,0x84,0x83,
    0xde,0xd9,0xd0,0xd7,0xc2,0xc5,0xcc,0xcb,0xe6,0xe1,0xe8,0xef,0xfa,0xfd,0xf4,0xf3,
])

def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


def _cat_frame(opcode: int, data: bytes) -> bytes:
    """Build one framed cat-printer command packet."""
    length = len(data)
    return bytes([0x51, 0x78, opcode, 0x00, length & 0xFF, 0x00]) \
           + data \
           + bytes([_crc8(data), 0xFF])


# Constants for cat printer protocol
_CAT_LATTICE_START = _cat_frame(0xA6, bytes([
    0xAA,0x55,0x17,0x38,0x44,0x5F,0x5F,0x5F,0x44,0x38,0x2C]))
_CAT_LATTICE_END   = _cat_frame(0xA6, bytes([
    0xAA,0x55,0x17,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x17]))

def _cat_set_energy(energy: int = 12000) -> bytes:
    lo, hi = energy & 0xFF, (energy >> 8) & 0xFF
    return _cat_frame(0xAF, bytes([lo, hi]))

def _cat_get_state() -> bytes:
    return _cat_frame(0xA3, b"\x00")

def _cat_start_printing() -> bytes:
    return _cat_frame(0xA3, b"\x00")

def _cat_set_dpi_200() -> bytes:
    return _cat_frame(0xA4, b"\x32")

def _cat_set_speed(speed: int = 32) -> bytes:
    return _cat_frame(0xBD, bytes([speed & 0xFF]))

def _cat_apply_energy() -> bytes:
    return _cat_frame(0xBE, b"\x01")

def _cat_update_device() -> bytes:
    return _cat_frame(0xA9, b"\x00")

def _cat_feed(lines: int = 25) -> bytes:
    return _cat_frame(0xA1, bytes([lines, 0x00]))

def _cat_print_row(row_48_bytes: bytes) -> bytes:
    """
    Print one 384-dot raster row.  row_48_bytes must be exactly 48 bytes.
    Bit order: LSB of first byte = leftmost pixel; 1=print (dark).
    NOTE: This is the OPPOSITE of typical ESC/POS bit order (which is MSB-first).
    """
    assert len(row_48_bytes) == PAPER_WIDTH_BYTES
    return _cat_frame(0xA2, row_48_bytes)


_BIT_REVERSE_TABLE = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


def _cat_payload_from_rows(rows: list[bytes]) -> bytes:
    """Build a complete cat-printer payload from 384-dot raster rows."""
    if not rows:
        info("Image is entirely blank — skipping")
        return b""

    last_content = -1
    for idx in range(len(rows) - 1, -1, -1):
        if any(rows[idx]):
            last_content = idx
            break

    if last_content == -1:
        info("Image is entirely blank — skipping")
        return b""

    crop_h = min(last_content + 1 + 16, len(rows))
    if crop_h < len(rows):
        info(f"Auto-trim: {len(rows) - crop_h} blank rows removed")
        rows = rows[:crop_h]

    out = bytearray()
    out += _cat_get_state()
    out += _cat_start_printing()
    out += _cat_set_dpi_200()
    out += _cat_set_speed(32)
    out += _cat_set_energy(12000)
    out += _cat_apply_energy()
    out += _cat_update_device()
    out += _CAT_LATTICE_START

    for row in rows:
        out += _cat_print_row(row)

    out += _CAT_LATTICE_END
    out += _cat_set_speed(8)
    out += _cat_feed(128)
    out += _cat_get_state()
    return bytes(out)


def image_to_cat_protocol(img) -> bytes:
    """
    Convert a PIL 1-bit image (already scaled to 384px wide) to a complete
    cat-printer protocol byte sequence ready to send over BLE.

    Pixel value 0 = black (print), 1 = white (don't print) in PIL mode "1".
    Cat protocol: bit=1 means PRINT (dark). LSB of each byte = leftmost pixel.
    """
    w, h   = img.size
    pixels = img.load()
    if False:

        info("Image is entirely blank — skipping")
        return b""


    rows: list[bytes] = []

    for y in range(h):
        row = bytearray(PAPER_WIDTH_BYTES)
        for xb in range(PAPER_WIDTH_BYTES):
            byte = 0
            for bit in range(8):
                x = xb * 8 + bit
                if x < w and pixels[x, y] == 0:    # black → set bit
                    byte |= (1 << bit)              # LSB-first (cat protocol)
            row[xb] = byte
        rows.append(bytes(row))

    return _cat_payload_from_rows(rows)


def escpos_raster_to_cat_protocol(data: bytes) -> bytes:
    """Convert ESC/POS GS v 0 raster blocks into cat-printer protocol."""
    rows: list[bytes] = []
    i = 0

    while i + 8 <= len(data):
        if data[i] == 0x1D and data[i + 1] == 0x76 and data[i + 2] == 0x30:
            xL, xH = data[i + 4], data[i + 5]
            yL, yH = data[i + 6], data[i + 7]
            bpr = xL | (xH << 8)
            row_count = yL | (yH << 8)
            hdr_end = i + 8
            body_end = hdr_end + (bpr * row_count)
            if bpr <= 0 or row_count <= 0 or body_end > len(data):
                break

            raster = data[hdr_end:body_end]
            for row_idx in range(row_count):
                src = raster[row_idx * bpr:(row_idx + 1) * bpr]
                row = bytearray(PAPER_WIDTH_BYTES)
                usable = min(len(src), PAPER_WIDTH_BYTES)
                for xb in range(usable):
                    row[xb] = _BIT_REVERSE_TABLE[src[xb]]
                rows.append(bytes(row))
            i = body_end
            continue
        i += 1

    if not rows:
        return b""

    info(f"ESC/POS raster decode: {len(rows)} row(s)")
    return _cat_payload_from_rows(rows)


def text_to_cat_protocol(text: str) -> bytes:
    """
    Render plain text as a 1-bit bitmap and convert to cat-printer protocol.
    Uses Pillow to rasterize text into a 384px-wide image.
    """
    try:
        from PIL import Image as _PIL, ImageDraw, ImageOps
    except ImportError:
        fail("Pillow not installed — run: pip install Pillow")
        return b""

    font, font_name = _load_cat_text_font()
    sample_bbox = _font_bbox(font, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    sample_w = max(1, sample_bbox[2] - sample_bbox[0])
    avg_char_w = max(1, sample_w // 52)

    # Wrap text to fit the actual rasterized font width.
    cols = max(1, (PAPER_WIDTH_DOTS - CAT_TEXT_LEFT_PAD * 2) // avg_char_w)

    lines = []
    for raw_line in text.splitlines():
        lines.extend(_wrap_text_line(raw_line, cols) if raw_line.strip() else [""])
    if not lines:
        lines = [""]

    # Measure height needed
    line_bbox = _font_bbox(font, "Ag")
    line_h = max(CAT_TEXT_FONT_SIZE, line_bbox[3] - line_bbox[1]) + CAT_TEXT_LINE_PAD
    img_h  = max(line_h * len(lines) + CAT_TEXT_LINE_PAD * 2, 1)

    img  = _PIL.new("L", (PAPER_WIDTH_DOTS, img_h), 255)
    draw = ImageDraw.Draw(img)
    if font_name == "Pillow default":
        info("Using Pillow default text font; install a TrueType font for larger text rendering")
    else:
        info(f"Using text font: {font_name}")

    y = CAT_TEXT_LINE_PAD
    for line in lines:
        draw.text((CAT_TEXT_LEFT_PAD, y), line, fill=0, font=font)
        y += line_h

    img = ImageOps.autocontrast(img.convert("L"), cutoff=2).convert("1")
    return image_to_cat_protocol(img)


def _render_document_to_cat_via_ghostscript(data: bytes, suffix: str, label: str, scale_fn):
    import glob
    import os as _os
    import shutil
    import subprocess as _sp
    import tempfile
    from PIL import Image as _PIL

    gs = shutil.which("gswin64c") or shutil.which("gswin32c") or shutil.which("gs")
    if not gs:
        info(f"Ghostscript not found — required for {label}")
        return b""

    tmp_in = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp_in.write(data)
    tmp_in.close()
    tmp_png = tmp_in.name.replace(suffix, "_%03d.png")

    try:
        r = _sp.run([
            gs, "-dBATCH", "-dNOPAUSE", "-dQUIET",
            "-sDEVICE=pnggray", f"-r{int(PAPER_WIDTH_DOTS / (PAPER_WIDTH_MM / 25.4))}",
            f"-dDEVICEWIDTHPOINTS={PAPER_WIDTH_MM * 2.835:.0f}",
            "-dDEVICEHEIGHTPOINTS=3276",
            "-dFIXEDMEDIA", "-dPDFFitPage",
            "-dFitPage",
            f"-sOutputFile={tmp_png}", tmp_in.name
        ], capture_output=True, timeout=60, **hidden_subprocess_kwargs())

        pages_out = bytearray()
        for pg in sorted(glob.glob(tmp_png.replace("%03d", "*"))):
            with _PIL.open(pg) as opened:
                img = scale_fn(opened)
            pages_out += image_to_cat_protocol(img)
            _safe_unlink(pg)

        if pages_out:
            ok(f"{label} rendered via Ghostscript")
            return bytes(pages_out)

        err = r.stderr.decode(errors="replace")[:200].strip()
        if err:
            info(f"Ghostscript {label} error: {err}")
    except Exception as e:
        info(f"Ghostscript {label} failed: {e}")
    finally:
        _safe_unlink(tmp_in.name)

    return b""



def _to_cat_payload(data: bytes, fmt: str, job_num: int = 0) -> bytes:
    """
    Convert any incoming data format to cat-printer protocol bytes.
    Tries multiple strategies in order: PIL direct, PyMuPDF, Ghostscript, text fallback.
    Returns empty bytes on failure (caller should skip the job).
    """
    from PIL import Image as _PIL, ImageOps, ImageFilter
    import io as _io

    def _scale_to_cat(img):
        """Greyscale, trim whitespace, scale to paper width, sharpen, 1-bit dither."""
        img = img.convert("L")
        bbox = img.point(lambda p: 0 if p > 245 else 255, mode="1").getbbox()
        if bbox:
            img = img.crop(bbox)
        w, h = img.size
        new_w = PAPER_WIDTH_DOTS
        img = img.resize((new_w, int(h * new_w / w)), _PIL.LANCZOS)
        img = ImageOps.autocontrast(img, cutoff=2)
        img = img.filter(ImageFilter.SHARPEN)
        return img.convert("1")

    # ── PNG / JPEG — direct PIL open
    if fmt in ("PNG", "JPEG"):
        try:
            with _PIL.open(_io.BytesIO(data)) as opened:
                img = _scale_to_cat(opened)
            return image_to_cat_protocol(img)
        except Exception as e:
            info(f"PIL image open failed: {e}")
            return b""

    # ── PDF — try PyMuPDF first (pip install pymupdf), then Ghostscript
    if fmt == "PDF":
        # Method A: PyMuPDF (fast, no external deps)
        try:
            import fitz  # PyMuPDF
            doc  = fitz.open(stream=data, filetype="pdf")
            pages_out = bytearray()
            page_count = doc.page_count
            try:
                for page in doc:
                    # render at ~203 dpi scaled to paper width
                    zoom   = PAPER_WIDTH_DOTS / page.rect.width
                    mat    = fitz.Matrix(zoom, zoom)
                    pix    = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
                    img    = _PIL.frombytes("L", (pix.width, pix.height), pix.samples)
                    img    = ImageOps.autocontrast(img, cutoff=2).filter(ImageFilter.SHARPEN).convert("1")
                    pages_out += image_to_cat_protocol(img)
            finally:
                doc.close()
            if pages_out:
                ok(f"PDF rendered via PyMuPDF ({page_count} page(s))")
                return bytes(pages_out)
        except ImportError:
            info("PyMuPDF not installed — trying Ghostscript…")
            info("  (tip: pip install pymupdf  for faster PDF printing)")
        except Exception as e:
            info(f"PyMuPDF failed: {e} — trying Ghostscript…")

        pages = _render_document_to_cat_via_ghostscript(
            data, ".pdf", "PDF", _scale_to_cat
        )
        if pages:
            return pages

        fail(f"Job #{job_num}: could not render PDF — install PyMuPDF:  pip install pymupdf")
        return b""

    # ── PostScript — rasterize via Ghostscript
    if fmt == "PS":
        pages = _render_document_to_cat_via_ghostscript(
            data, ".ps", "PostScript", _scale_to_cat
        )
        if pages:
            return pages

        fail(f"Job #{job_num}: could not render PostScript — install Ghostscript")
        return b""

    # ── EMF (Windows GDI metafile from spooler) — rasterize via PowerShell .NET
    if fmt == "EMF":
        import tempfile, subprocess as _sp, os as _os
        tmp_emf = None
        tmp_png = None
        try:
            tmp_emf = tempfile.NamedTemporaryFile(suffix=".emf", delete=False)
            tmp_emf.write(data); tmp_emf.close()
            tmp_png = tmp_emf.name.replace(".emf", ".png")
            r = _sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                f'Add-Type -AssemblyName System.Drawing;'
                f'$img=[System.Drawing.Image]::FromFile("{tmp_emf.name}");'
                f'$bmp=New-Object System.Drawing.Bitmap({PAPER_WIDTH_DOTS},'
                f'[int]($img.Height*{PAPER_WIDTH_DOTS}/$img.Width));'
                f'$g=[System.Drawing.Graphics]::FromImage($bmp);'
                f'$g.Clear([System.Drawing.Color]::White);'
                f'$g.DrawImage($img,0,0,$bmp.Width,$bmp.Height);'
                f'$bmp.Save("{tmp_png}");'
                f'$g.Dispose();$img.Dispose();$bmp.Dispose()'
            ], capture_output=True, timeout=20, **hidden_subprocess_kwargs())
            if r.returncode == 0 and _os.path.exists(tmp_png):
                with _PIL.open(tmp_png) as opened:
                    img = _scale_to_cat(opened)
                result = image_to_cat_protocol(img)
                ok("EMF rasterized via PowerShell/.NET")
                return result
        except Exception as e:
            info(f"EMF rasterization failed: {e}")
        finally:
            _safe_unlink(getattr(tmp_emf, "name", None))
            _safe_unlink(tmp_png)

        # EMF fallback: extract readable text and render
        import re as _re
        runs = _re.findall(rb"[ -~\t\r\n]{6,}", data)
        if runs:
            text = "\n".join(r.decode("latin-1", errors="replace") for r in runs)
            info("EMF: falling back to text extraction")
            return text_to_cat_protocol(text)
        fail(f"Job #{job_num}: EMF conversion failed")
        return b""

    # ── Plain TEXT
    if fmt == "TEXT":
        text = decode_text_payload(data)
        return text_to_cat_protocol(text)

    # ── ESC/POS from spooler (raw-text style driver sends this)
    if fmt == "ESCPOS":
        raster_payload = escpos_raster_to_cat_protocol(data)
        if raster_payload:
            return raster_payload
        # Strip control bytes, keep printable lines
        raw = decode_text_payload(data)
        readable = "\n".join(
            line for line in raw.splitlines()
            if line.strip() and all(0x20 <= ord(c) <= 0x7E or c in "\r\n\t" for c in line)
        )
        if readable.strip():
            return text_to_cat_protocol(readable)
        extracted = extract_printable_runs(data)
        if extracted:
            info("ESC/POS text decode was empty; using printable-run fallback")
            return text_to_cat_protocol(extracted)
        preview = data[:48].hex(" ")
        info(f"ESC/POS fallback preview: {preview}")
        fail(f"Job #{job_num}: ESC/POS had no printable text")
        return b""

    # ── XPS / PCL / unknown — text extraction last resort
    if data[:1024].count(0) / max(1, len(data[:1024])) > 0.2:
        text = decode_text_payload(data)
        if text.strip():
            info("Unknown format but payload looks like UTF-16/NUL-padded text")
            return text_to_cat_protocol(text)
    runs = _re.findall(rb"[ -~\t\r\n]{6,}", data)
    if runs:
        text = "\n".join(r.decode("latin-1", errors="replace") for r in runs)
        info(f"Unknown format — extracted {len(runs)} text runs")
        return text_to_cat_protocol(text)

    fail(f"Job #{job_num}: unrecognised format '{fmt}' — cannot convert")
    return b""


# ═══════════════════════════════════════════════════════════════════════════════
#  TCP → BLE RELAY
# ═══════════════════════════════════════════════════════════════════════════════

async def send_direct_ble(payload: bytes, cfg: dict) -> bool:
    """Send payload directly over BLE. Returns True on success."""
    address = cfg.get("address")
    write_uuid = cfg.get("write_uuid")
    if not address or not write_uuid:
        fail("Saved printer config is missing address or write UUID.")
        return False

    mtu = cfg.get("mtu", 128) or 128
    chunk = max(20, min(RELAY_CHUNK_SIZE, mtu - 3))
    is_cat_printer = _is_cat_printer(write_uuid)
    supports_with_response = bool(cfg.get("write_with_response"))
    supports_without_response = cfg.get("write_without_response")
    if supports_without_response is None:
        supports_without_response = True

    def _response_modes():
        modes = []
        if supports_without_response:
            modes.append(False)
        if supports_with_response:
            modes.append(True)
        if not modes:
            modes.append(False)
        elif len(modes) == 1:
            modes.append(not modes[0])
        return modes

    async def _send_once(response_mode: bool):
        async with BleakClient(address, timeout=15) as client:
            for i in range(0, len(payload), chunk):
                await client.write_gatt_char(
                    write_uuid, payload[i:i+chunk], response=response_mode)
                if i + chunk < len(payload):
                    await asyncio.sleep(RELAY_CHUNK_DELAY)
                    if is_cat_printer:
                        await asyncio.sleep(0.01)

    try:
        last_error = None
        for response_mode in _response_modes():
            try:
                await _send_once(response_mode)
                return True
            except Exception as mode_error:
                last_error = mode_error
                err = str(mode_error).lower()
                if "not connected" in err or "unreachable" in err:
                    raise
                info(
                    "BLE write failed with response="
                    f"{response_mode}; trying alternate mode"
                )
        if last_error is not None:
            raise last_error
        return False
    except Exception as e:
        err = str(e).lower()
        if "not connected" in err or "unreachable" in err:
            info("BLE link dropped, retrying once")
            try:
                await asyncio.sleep(0.5)
                last_error = None
                for response_mode in _response_modes():
                    try:
                        await _send_once(response_mode)
                        return True
                    except Exception as mode_error:
                        last_error = mode_error
                        info(
                            "BLE retry failed with response="
                            f"{response_mode}; trying alternate mode"
                        )
                if last_error is not None:
                    raise last_error
                return False
            except Exception as retry_error:
                fail(f"Direct BLE failed: {retry_error}")
                return False
        fail(f"Direct BLE failed: {e}")
        return False


async def send_payload(payload: bytes, label: str = "job") -> bool:
    """Send payload directly over BLE using saved printer config."""
    if not payload:
        fail("Empty payload — nothing to send.")
        return False

    info(f"Sending {label} ({len(payload)} bytes)…")
    cfg = load_config()
    if not cfg:
        fail(f"No printer config at {CONFIG_FILE}")
        info("Run bt_scan.py --save first.")
        return False

    if await send_direct_ble(payload, cfg):
        ok(f"{label} sent via direct BLE → printer")
        return True
    else:
        fail(f"Could not deliver {label}.")
        info("Make sure the printer is on and in range.")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  REGISTER FLOW  (scan + probe + register + optional test page)
# ═══════════════════════════════════════════════════════════════════════════════

async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="bt_print.py — Direct Bluetooth Printer Controller",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Typical workflow:\n"
            "  python bt_scan.py --save\n"
            "  python bt_print.py --print-text \"Hello\"\n"
            "  python bt_print.py --print-image photo.jpg\n"
            "  python bt_print.py --print-pdf file.pdf\n"
        )
    )
    # Direct print
    parser.add_argument("--test-page",   action="store_true",
                        help="Print a direct BLE test page")
    parser.add_argument("--print-text",  metavar="TEXT",
                        help="Print a plain text string")
    parser.add_argument("--print-image", metavar="FILE",
                        help="Print an image file (requires Pillow)")
    parser.add_argument("--print-pdf",   metavar="FILE",
                        help="Print a PDF file (requires: pip install pymupdf)")
    args = parser.parse_args(argv)
    selected_actions = sum(
        bool(value)
        for value in (args.test_page, args.print_text, args.print_image, args.print_pdf)
    )
    if selected_actions > 1:
        parser.error("choose only one of --test-page, --print-text, --print-image, or --print-pdf")

    header("bt_print.py — Direct Bluetooth Printer Controller")
    print(f"  Platform : {platform.system()} {platform.release()}")
    print(f"  Python   : {sys.version.split()[0]}")
    print(f"  Config   : {CONFIG_FILE}")

    if not BLEAK_AVAILABLE:
        fail("'bleak' not installed. Run:  pip install bleak")
        return 1

    # ── Detect printer protocol from saved config ────────────────────────────
    cfg = load_config()
    if not cfg:
        fail(f"No printer config at {CONFIG_FILE}")
        info("Run bt_scan.py --save first.")
        return 1
    use_cat = cfg and _is_cat_printer(cfg.get("write_uuid", ""))
    success = True

    # ── Direct print (no relay needed) ───────────────────────────────────────
    if args.test_page:
        section("Test Page")
        if use_cat:
            payload = text_to_cat_protocol(
                "== TEST PAGE ==\n"
                f"Printer : {cfg.get('name','?')}\n"
                f"Address : {cfg.get('address','?')}\n"
                "Protocol: Cat/P1 native\n"
                "Status  : OK\n"
            )
        else:
            payload = ESCPOS_TEST
        success = await send_payload(payload, "test page")

    elif args.print_text:
        section("Print Text")
        if use_cat:
            payload = text_to_cat_protocol(args.print_text)
        else:
            payload = text_to_escpos(args.print_text.encode("utf-8"))
        success = await send_payload(payload, "text")

    elif args.print_image:
        section(f"Print Image: {args.print_image}")
        if not os.path.exists(args.print_image):
            fail(f"File not found: {args.print_image}")
            return 1
        if use_cat:
            try:
                from PIL import Image as _PIL, ImageOps, ImageFilter
                with _PIL.open(args.print_image) as opened:
                    img = opened.convert("L")
                w, h = img.size
                new_w = PAPER_WIDTH_DOTS
                img = img.resize((new_w, int(h * new_w / w)), _PIL.LANCZOS)
                img = ImageOps.autocontrast(img, cutoff=2).filter(ImageFilter.SHARPEN).convert("1")
                ok(f"Image scaled to {img.size[0]}×{img.size[1]}px")
                payload = image_to_cat_protocol(img)
            except ImportError:
                fail("Pillow not installed — run: pip install Pillow")
                return 1
        else:
            payload = image_file_to_escpos(args.print_image)
        success = await send_payload(payload, "image")

    elif args.print_pdf:
        section(f"Print PDF: {args.print_pdf}")
        if not os.path.exists(args.print_pdf):
            fail(f"File not found: {args.print_pdf}")
            return 1
        with open(args.print_pdf, "rb") as _f:
            pdf_bytes = _f.read()
        payload = _to_cat_payload(pdf_bytes, "PDF") if use_cat else convert_to_escpos(pdf_bytes, "PDF")
        if not payload:
            fail("PDF conversion failed — install PyMuPDF:  pip install pymupdf")
            return 1
        success = await send_payload(payload, "PDF")

    else:
        parser.print_help()
        return 0

    sep("═")


    return 0 if success else 1

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
