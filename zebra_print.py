"""
Zebra ZT410 Label Printer GUI
Prints JPG/PNG images as 4x6 labels via USB, on ribbon or direct thermal media.

Install dependencies:
    pip install pillow pywin32 numpy tkinterdnd2

Usage: python zebra_print.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from dataclasses import asdict, dataclass
from datetime import datetime
import base64
import ctypes
import functools
import json
import queue
import re
import threading
import time
import os
from PIL import Image, ImageOps, ImageEnhance, ImageDraw, ImageFilter, ImageFont, ImageTk
import numpy as np

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_BASES = (TkinterDnD.DnDWrapper,)
except ImportError:     # drag and drop is optional; everything else still works
    DND_FILES, TkinterDnD, DND_BASES = None, None, ()

# ---------------------------------------------------------------------------
# Config — edit these values to tune label alignment
# ---------------------------------------------------------------------------

# Label dimensions
LABEL_W_DOTS = 812      # 4.0" × 203.2 dpi = 812.8, rounded down to stay on label
LABEL_H_DOTS = 1219     # 6.0" × 203.2 dpi = 1219.2

# ZT410 203 dpi max print width — never send wider than this or you print on the platen roller
MAX_PRINT_WIDTH = 832

# Y offset: shift the whole printed image down (+) or up (−), in dots.
# Use the calibration test print (Print Test Pattern button) to measure your white strip,
# then set this to that many dots.  Example: 16 dots = 2 mm down.
Y_OFFSET_DOTS = 0

# Height adjust: make the printed image taller (+) or shorter (−) than LABEL_H_DOTS, in dots.
# A positive value stretches the image to fill a bit more height for full-bleed designs.
# Keep within ±~24 dots (3 mm) to avoid distorting the image noticeably.
HEIGHT_ADJUST_DOTS = 14

# Contrast slider: every launch starts at the default, which sits at the slider's centre.
DEFAULT_CONTRAST = 1.4
CONTRAST_MIN = 0.4
CONTRAST_MAX = 2.4
CONTRAST_STEP = 0.05

# Brightness is a gamma curve (1.0 = unchanged). Sharpen is unsharp-mask strength in percent.
DEFAULT_BRIGHTNESS = 1.0
BRIGHTNESS_MIN = 0.5
BRIGHTNESS_MAX = 1.5
BRIGHTNESS_STEP = 0.05
DEFAULT_SHARPEN = 0
SHARPEN_MAX = 200
SHARPEN_STEP = 10

# Darkness, speed and media are sent only when chosen. Otherwise the printer's saved value is used.
PRINTER_SETTING = 'Printer setting'
RESTORE_SAVED_ZPL = "^XA^JUR^XZ"    # chosen values outlast the job, so this clears them first
DARKNESS_CHOICES = [PRINTER_SETTING] + [str(n) for n in range(31)]
SPEED_CHOICES = [PRINTER_SETTING, '2', '3', '4', '5', '6']     # inches per second
MEDIA_CHOICES = {PRINTER_SETTING: None, 'Ribbon': 'T', 'Direct thermal': 'D'}
SLOWEST_SPEED = 2                   # assumed when the printer's speed is not known

TEXT_POSITIONS = ['Bottom', 'Top']

# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def mm_to_dots(mm: float) -> int:
    """Convert millimetres to ZT410 203 dpi dots (8 dots/mm)."""
    return round(mm * 8)

def inches_to_dots(inches: float) -> int:
    """Convert inches to ZT410 203 dpi dots (203.2 dots/inch)."""
    return round(inches * 203.2)

# ---------------------------------------------------------------------------
# Dithering
# ---------------------------------------------------------------------------

DITHER_MODES = ['Bayer 8x8', 'Bayer 4x4', 'Floyd-Steinberg', 'Atkinson', 'Threshold']

BAYER4 = np.array([
    [ 0, 8, 2,10],
    [12, 4,14, 6],
    [ 3,11, 1, 9],
    [15, 7,13, 5],
], dtype=np.float32) / 16.0 * 255.0

BAYER8 = np.array([
    [ 0,32, 8,40, 2,34,10,42],
    [48,16,56,24,50,18,58,26],
    [12,44, 4,36,14,46, 6,38],
    [60,28,52,20,62,30,54,22],
    [ 3,35,11,43, 1,33, 9,41],
    [51,19,59,27,49,17,57,25],
    [15,47, 7,39,13,45, 5,37],
    [63,31,55,23,61,29,53,21],
], dtype=np.float32) / 64.0 * 255.0


def bayer_dither(img_gray: Image.Image, matrix_size: int = 8) -> Image.Image:
    matrix = BAYER8 if matrix_size == 8 else BAYER4
    arr = np.array(img_gray, dtype=np.float32)
    h, w = arr.shape
    mh, mw = matrix.shape
    tiled = np.tile(matrix, (h // mh + 1, w // mw + 1))[:h, :w]
    result = (arr > tiled).astype(np.uint8) * 255
    return Image.fromarray(result, mode='L')


def floyd_steinberg_dither(img_gray: Image.Image) -> Image.Image:
    arr = np.array(img_gray, dtype=np.float32)
    h, w = arr.shape
    for y in range(h):
        for x in range(w):
            old = arr[y, x]
            new = 255.0 if old > 127 else 0.0
            arr[y, x] = new
            err = old - new
            if x + 1 < w:
                arr[y, x + 1] += err * 7 / 16
            if y + 1 < h:
                if x > 0:
                    arr[y + 1, x - 1] += err * 3 / 16
                arr[y + 1, x] += err * 5 / 16
                if x + 1 < w:
                    arr[y + 1, x + 1] += err * 1 / 16
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode='L')


def atkinson_dither(img_gray: Image.Image) -> Image.Image:
    arr = np.array(img_gray, dtype=np.float32)
    h, w = arr.shape
    for y in range(h):
        for x in range(w):
            old = arr[y, x]
            new = 255.0 if old > 127 else 0.0
            arr[y, x] = new
            err = (old - new) / 8.0
            for dy, dx in [(0, 1), (0, 2), (1, -1), (1, 0), (1, 1), (2, 0)]:
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w:
                    arr[ny, nx] += err
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode='L')


def _apply_dither(img_l: Image.Image, dither_mode: str) -> Image.Image:
    """Apply chosen dither to a grayscale (L-mode) image. Returns L-mode result."""
    if dither_mode == 'Bayer 8x8':
        return bayer_dither(img_l, 8)
    elif dither_mode == 'Bayer 4x4':
        return bayer_dither(img_l, 4)
    elif dither_mode == 'Floyd-Steinberg':
        return floyd_steinberg_dither(img_l)
    elif dither_mode == 'Atkinson':
        return atkinson_dither(img_l)
    else:  # Threshold
        return img_l.point(lambda v: 255 if v > 127 else 0)


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImageSettings:
    dither: str = 'Bayer 8x8'
    contrast: float = DEFAULT_CONTRAST
    brightness: float = DEFAULT_BRIGHTNESS
    sharpen: int = DEFAULT_SHARPEN
    flip: bool = False
    height_adjust: int = HEIGHT_ADJUST_DOTS


@dataclass(frozen=True)
class TextOverlay:
    template: str = ''
    position: str = 'Bottom'
    size: int = 36          # dots
    band: bool = True       # white strip behind the text


@dataclass(frozen=True)
class PrinterOptions:
    darkness: int | None = None     # 0-30; None leaves the printer's setting alone
    speed: int | None = None        # None leaves the printer's setting alone
    media: str | None = None    # 'T' ribbon, 'D' direct thermal, None leaves it alone


@functools.lru_cache(maxsize=2)
def _load_rgb(path: str, _mtime: float) -> Image.Image:
    # Apply the EXIF orientation tag first so the size describes the image as it displays
    return ImageOps.exif_transpose(Image.open(path)).convert('RGB')


def load_image(path: str) -> Image.Image:
    """Decode an image, reusing recent results. Callers must not modify the returned image."""
    return _load_rgb(path, os.path.getmtime(path))


def crop_surplus(path: str, height_adjust: int) -> tuple[float, float]:
    """Return how many dots of image overhang the label in (x, y) once it fills the label."""
    w, h = load_image(path).size
    if w > h:
        w, h = h, w
    target_w, target_h = min(LABEL_W_DOTS, MAX_PRINT_WIDTH), LABEL_H_DOTS + height_adjust
    scale = max(target_w / w, target_h / h)
    return w * scale - target_w, h * scale - target_h


def prepare_image(path: str, settings: ImageSettings,
                  crop: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    """Open an image, rotate it to fill the portrait label, and dither it.

    Landscape images are turned 90° so they fill the label; flip turns the result 180°.
    crop chooses which part survives when the shapes differ: (0, 0) keeps the top left.
    Returns a 1-bit PIL image sized (LABEL_W_DOTS, LABEL_H_DOTS + height_adjust).
    """
    img = load_image(path)
    w, h = img.size
    if w > h:
        img = img.rotate(-90, expand=True)
    if settings.flip:
        img = img.rotate(180)

    render_w = min(LABEL_W_DOTS, MAX_PRINT_WIDTH)
    img = ImageOps.fit(img, (render_w, LABEL_H_DOTS + settings.height_adjust),
                       Image.LANCZOS, centering=crop)
    img = img.convert('L')
    if settings.contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(settings.contrast)
    if settings.brightness != 1.0:
        gamma = 1 / settings.brightness
        img = img.point([round(255 * (v / 255) ** gamma) for v in range(256)])
    if settings.sharpen:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=settings.sharpen, threshold=2))
    img = _apply_dither(img, settings.dither)

    return img.convert('1')


def uses_label_number(template: str) -> bool:
    return '{n}' in template


def format_text(template: str, path: str, number: int, total: int) -> str:
    replacements = {
        '{n}': str(number), '{total}': str(total),
        '{name}': os.path.splitext(os.path.basename(path))[0],
        '{date}': datetime.now().strftime('%Y-%m-%d'),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ('segoeuib.ttf', 'arialbd.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def add_text(img1bit: Image.Image, path: str, flip: bool, overlay: TextOverlay,
             number: int, total: int) -> Image.Image:
    """Draw the overlay onto a dithered bitmap so it stays sharp, upright with the photo."""
    if not overlay.template.strip():
        return img1bit
    text = format_text(overlay.template, path, number, total)

    w, h = load_image(path).size
    angle = (90 if w > h else 0) + (180 if flip else 0)
    canvas = img1bit.rotate(angle, expand=True) if angle else img1bit.copy()

    draw = ImageDraw.Draw(canvas)
    draw.fontmode = '1'     # hard edges: no grey pixels on a 1-bit label
    margin = 16
    size = overlay.size
    font = _load_font(size)
    while draw.textlength(text, font=font) > canvas.width - 2 * margin and size > 12:
        size -= 2
        font = _load_font(size)

    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = right - left, bottom - top
    x = (canvas.width - text_w) // 2 - left
    box_top = margin if overlay.position == 'Top' else canvas.height - margin - text_h
    y = box_top - top
    if overlay.band:
        pad = 10
        draw.rectangle([0, box_top - pad, canvas.width, box_top + text_h + pad], fill=255)
        draw.text((x, y), text, font=font, fill=0)
    else:
        draw.text((x, y), text, font=font, fill=0, stroke_width=2, stroke_fill=255)

    return canvas.rotate(-angle, expand=True) if angle else canvas


def label_preview(img1bit: Image.Image, y_offset: int = Y_OFFSET_DOTS) -> Image.Image:
    """Place a prepared bitmap on a blank label the way image_to_zpl() positions it.

    A positive height adjust makes the bitmap taller than LABEL_H_DOTS, and all of those
    rows print, so the preview grows to show them.
    """
    label = Image.new('L', (LABEL_W_DOTS, max(LABEL_H_DOTS, img1bit.height)), 255)
    label.paste(img1bit.convert('L'), (0, y_offset))
    return label


# ---------------------------------------------------------------------------
# ZPL generation
# ---------------------------------------------------------------------------

def label_print_seconds(speed: float) -> float:
    """How long one label takes to print at this speed, in inches per second."""
    return LABEL_H_DOTS / inches_to_dots(1) / speed


def image_to_zpl(img1bit: Image.Image,
                 y_offset: int = Y_OFFSET_DOTS,
                 options: PrinterOptions = PrinterOptions(),
                 copies: int = 1) -> str:
    """Convert a 1-bit PIL image to a complete ZPL label.

    y_offset > 0  → shift image down (^FO y parameter)
    y_offset < 0  → crop that many rows from the top of the bitmap instead
                     (^FO can't go negative; we just skip those rows)
    """
    # Handle negative offset by cropping the top of the image
    if y_offset < 0:
        crop_rows = min(-y_offset, img1bit.size[1] - 1)
        img1bit = img1bit.crop((0, crop_rows, img1bit.size[0], img1bit.size[1]))
        fo_y = 0
    else:
        fo_y = y_offset

    w, h = img1bit.size
    raw = img1bit.tobytes()
    # PIL '1' mode: 1=white, 0=black.  ZPL ^GFA: 1=black dot.  Invert.
    inverted = bytes(b ^ 0xFF for b in raw)
    bytes_per_row = (w + 7) // 8
    total_bytes = bytes_per_row * h
    hex_data = inverted.hex().upper()

    zpl = (
        (f"~SD{options.darkness:02d}" if options.darkness is not None else "")
        + "^XA"
        + (f"^MT{options.media}" if options.media else "")
        + (f"^PR{options.speed}" if options.speed else "")
        + f"^PW{min(w, MAX_PRINT_WIDTH)}"   # print width guard
        "^LH0,0"
        f"^FO0,{fo_y}"
        f"^GFA,{total_bytes},{total_bytes},{bytes_per_row},{hex_data}"
        + (f"^PQ{copies}" if copies > 1 else "")
        + "^XZ"
    )
    return zpl


# ---------------------------------------------------------------------------
# Calibration test pattern
# ---------------------------------------------------------------------------

def make_test_pattern_zpl(y_offset: int = Y_OFFSET_DOTS,
                           height_adjust: int = HEIGHT_ADJUST_DOTS,
                           options: PrinterOptions = PrinterOptions()) -> str:
    """Generate a ZPL test-pattern label for alignment calibration.

    The pattern includes:
    - 1-dot border rectangle at the label edges
    - Tick marks every 1 mm (8 dots) along top and bottom, labelled every 5 mm
    - Current Y_OFFSET_DOTS and HEIGHT_ADJUST_DOTS printed in the centre
    """
    W = LABEL_W_DOTS
    H = LABEL_H_DOTS + height_adjust

    # Build a white (255) canvas; we'll draw black (0) on it
    img = Image.new('L', (W, H), 255)
    draw = ImageDraw.Draw(img)

    # 1-dot border
    draw.rectangle([0, 0, W - 1, H - 1], outline=0, width=1)

    # Tick marks every 8 dots (1 mm) along top and bottom edges
    TICK_SHORT = 12   # dots tall for 1-mm ticks
    TICK_LONG  = 20   # dots tall for 5-mm ticks
    for x in range(0, W, 8):
        mm = x // 8
        tick_h = TICK_LONG if mm % 5 == 0 else TICK_SHORT
        draw.line([(x, 0), (x, tick_h)], fill=0, width=1)
        draw.line([(x, H - 1), (x, H - 1 - tick_h)], fill=0, width=1)
        if mm % 5 == 0 and x > 0 and x < W - 20:
            # Label every 5 mm — tiny text using PIL default bitmap font
            label = f"{mm}"
            draw.text((x + 2, TICK_LONG + 2), label, fill=0)
            draw.text((x + 2, H - TICK_LONG - 14), label, fill=0)

    # Centre text showing current settings
    cx, cy = W // 2, H // 2
    lines = [
        "CALIBRATION TEST",
        f"Y_OFFSET_DOTS = {y_offset}",
        f"HEIGHT_ADJUST_DOTS = {height_adjust}",
        f"Label: {W} x {H} dots",
        f"({W/203.2:.3f}\" x {H/203.2:.3f}\")",
    ]
    line_h = 18
    y_start = cy - (len(lines) * line_h) // 2
    for i, line in enumerate(lines):
        draw.text((cx - len(line) * 3, y_start + i * line_h), line, fill=0)

    # Cross-hair at centre
    draw.line([(cx - 20, cy), (cx + 20, cy)], fill=0, width=1)
    draw.line([(cx, cy - 20), (cx, cy + 20)], fill=0, width=1)

    img1 = img.convert('1')
    return image_to_zpl(img1, y_offset=y_offset, options=options)


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def send_to_printer(zpl: str, printer_name: str) -> None:
    import win32print
    hprinter = win32print.OpenPrinter(printer_name)
    try:
        hjob = win32print.StartDocPrinter(hprinter, 1, ("ZPL Label", None, "RAW"))
        try:
            win32print.StartPagePrinter(hprinter)
            win32print.WritePrinter(hprinter, zpl.encode('ascii'))
            win32print.EndPagePrinter(hprinter)
        finally:
            win32print.EndDocPrinter(hprinter)
    finally:
        win32print.ClosePrinter(hprinter)


def get_printers() -> list:
    try:
        import win32print
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        names = [p[2] for p in printers]
        zebra = [n for n in names if any(k in n.lower() for k in ('zebra', 'zt', 'zdesigner', 'zp'))]
        others = [n for n in names if n not in zebra]
        return zebra + others
    except Exception:
        return []


# Runs elevated. The spooler is always started again, even if deleting a file fails.
SPOOLER_RESET_PS = r"""
$ErrorActionPreference = 'Stop'
$code = 0
try {
    Stop-Service -Name Spooler -Force
    Get-ChildItem -Path "$env:SystemRoot\System32\spool\PRINTERS" -File -Force | Remove-Item -Force
} catch {
    $code = 1
} finally {
    Start-Service -Name Spooler
}
exit $code
"""

ERROR_CANCELLED = 1223      # the Windows permission prompt was declined
SPOOLER_RESET_TIMEOUT_MS = 60_000


def count_queued_jobs() -> dict:
    """Return {printer name: job count} for printers with jobs waiting."""
    import win32print
    counts = {}
    for name in get_printers():
        try:
            hprinter = win32print.OpenPrinter(name)
            try:
                jobs = len(win32print.EnumJobs(hprinter, 0, 999, 1))
            finally:
                win32print.ClosePrinter(hprinter)
        except Exception:
            continue
        if jobs:
            counts[name] = jobs
    return counts


def run_powershell(script: str, verb: str = 'runas') -> int:
    """Run a PowerShell script hidden and return its exit code. 'runas' prompts for admin."""
    import pythoncom
    import win32con
    import win32event
    import win32process
    from win32com.shell import shell, shellcon

    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    pythoncom.CoInitialize()
    try:
        info = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb=verb,
            lpFile='powershell.exe',
            lpParameters=f'-NoProfile -NonInteractive -EncodedCommand {encoded}',
            nShow=win32con.SW_HIDE,
        )
        process = info['hProcess']
        if win32event.WaitForSingleObject(process, SPOOLER_RESET_TIMEOUT_MS) != win32event.WAIT_OBJECT_0:
            raise TimeoutError("the spooler did not respond within 60 seconds")
        return win32process.GetExitCodeProcess(process)
    finally:
        pythoncom.CoUninitialize()


# ---------------------------------------------------------------------------
# Printer status (read straight from the USB device, without the spooler)
# ---------------------------------------------------------------------------

ZT410_USB_ID = 'VID_0A5F&PID_00F9'
USB_PRINTER_CLASS = '{28d78fad-5a12-11d1-ae5b-0000f803a8c2}'


def find_printer_device() -> str | None:
    """Return the ZT410's USB device path, or None if Windows has never seen one."""
    import winreg
    key_path = rf'SYSTEM\CurrentControlSet\Control\DeviceClasses\{USB_PRINTER_CLASS}'
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            index = 0
            while True:
                name = winreg.EnumKey(key, index)
                index += 1
                if ZT410_USB_ID in name.upper():
                    return '\\\\?\\' + name[4:]
    except OSError:
        return None


def usb_query(device: str, command: bytes, finished) -> bytes:
    """Send a command and collect the reply until finished(reply) is true.

    Replies can trail a query, so this keeps reading. It blocks if the printer goes silent.
    """
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    generic_read_write, share_read_write, open_existing = 0xC0000000, 3, 3
    handle = kernel32.CreateFileW(device, generic_read_write, share_read_write, None,
                                  open_existing, 0, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "printer not connected")
    handle = wintypes.HANDLE(handle)
    try:
        count = wintypes.DWORD()
        if not kernel32.WriteFile(handle, command, len(command), ctypes.byref(count), None):
            raise OSError(ctypes.get_last_error(), "printer did not accept the query")
        buffer = ctypes.create_string_buffer(4096)
        reply = b''
        for _ in range(30):
            if not kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise OSError(ctypes.get_last_error(), "printer did not answer")
            reply += buffer.raw[:count.value]
            if finished(reply):
                break
            time.sleep(0.05)
        return reply
    finally:
        kernel32.CloseHandle(handle)


def parse_host_status(reply: bytes) -> dict | None:
    """Pick the fields we use out of the printer's ~HS reply (three STX...ETX frames)."""
    frames = re.findall(rb'\x02([^\x03]*)\x03', reply)
    if len(frames) < 3:
        return None
    first, second = frames[-3].split(b','), frames[-2].split(b',')
    if len(first) < 4 or len(second) < 5:
        return None
    return {
        'paper_out': first[1] == b'1', 'paused': first[2] == b'1',
        'label_length': int(first[3]),
        'head_open': second[2] == b'1', 'ribbon_out': second[3] == b'1',
        'ribbon_mode': second[4] == b'1',
    }


def describe_status(status: dict) -> tuple[str, bool]:
    """Return (text for the status bar, whether the printer is good to go)."""
    if status['head_open']:
        return "Printhead open", False
    if status['paper_out']:
        return "Out of labels", False
    if status['ribbon_mode'] and status['ribbon_out']:
        return "Out of ribbon", False
    if status['paused']:
        return "Paused", False
    return f"Ready · {'Ribbon' if status['ribbon_mode'] else 'Direct thermal'}", True


def read_printer_state(device: str) -> dict:
    """Return the host status plus the printer's own darkness and speed."""
    def setting(reply: bytes, name: bytes) -> str | None:
        found = re.findall(rb'"' + re.escape(name) + rb'"\s*:\s*"([\d.]+)"', reply)
        return found[-1].decode('ascii') if found else None

    def finished(reply: bytes) -> bool:
        return (reply.count(b'\x03') >= 3 and setting(reply, b'print.tone') is not None
                and setting(reply, b'media.speed') is not None)

    reply = usb_query(device, b'~HS\r\n{}{"print.tone":null,"media.speed":null}\r\n', finished)
    status = parse_host_status(reply)
    if status is None:
        raise OSError("unreadable printer status")
    status['darkness'] = setting(reply, b'print.tone')
    status['speed'] = setting(reply, b'media.speed')
    return status


# ---------------------------------------------------------------------------
# Saved settings
# ---------------------------------------------------------------------------

SETTINGS_PATH = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'),
                             'ZebraPrint', 'settings.json')


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass    # losing saved settings shouldn't interrupt printing


HISTORY_PATH = os.path.join(os.path.dirname(SETTINGS_PATH), 'history.jsonl')


def load_history() -> list:
    """Return past print runs, oldest first. Damaged lines are skipped."""
    runs = []
    try:
        with open(HISTORY_PATH, encoding='utf-8') as f:
            for line in f:
                try:
                    runs.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return runs


def append_history(run: dict) -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(run) + '\n')
    except OSError:
        pass    # losing a history line shouldn't interrupt printing


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

BG          = '#0d111c'    # midnight navy
CARD        = '#161c2b'
FIELD       = '#222a3d'
FIELD_HOVER = '#2e3750'
ROW_ACTIVE  = '#3a2a3a'
ACCENT      = '#ff6f61'    # coral
ACCENT_HOVER = '#ff8a7e'
ACCENT_DOWN = '#e0564a'
ON_ACCENT   = '#1a0f12'
TEXT        = '#edf0f7'
SUBTEXT     = '#8e98b0'
DISABLED    = '#58617a'
GOOD        = '#4fd1a5'

FONT       = ('Segoe UI', 10)
FONT_SMALL = ('Segoe UI', 9)
FONT_BOLD  = ('Segoe UI Semibold', 10)
FONT_HEAD  = ('Segoe UI Semibold', 13)


def make_checkbox_image(size: int, checked: bool) -> Image.Image:
    """Draw a rounded checkbox at 4x and shrink it so the edges stay smooth."""
    s = size * 4
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    box = [s * 0.06, s * 0.06, s * 0.94, s * 0.94]
    radius = s * 0.2
    if checked:
        draw.rounded_rectangle(box, radius, fill=ACCENT)
        draw.line([(s * 0.27, s * 0.52), (s * 0.44, s * 0.69), (s * 0.74, s * 0.33)],
                  fill=ON_ACCENT, width=max(1, round(s * 0.12)), joint='curve')
    else:
        draw.rounded_rectangle(box, radius, outline=SUBTEXT, width=max(1, round(s * 0.07)))
    return img.resize((size, size), Image.LANCZOS)


def make_slider_images(size: int) -> tuple[Image.Image, Image.Image]:
    """Return (track, knob) images for the flat slider, drawn at 4x for smooth edges."""
    s = size * 4
    knob = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(knob).ellipse([s * 0.04, s * 0.04, s * 0.96, s * 0.96], fill=ACCENT)
    track = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(track).rounded_rectangle([0, s * 0.4, s - 1, s * 0.6], s * 0.1, fill=FIELD_HOVER)
    return (track.resize((size, size), Image.LANCZOS), knob.resize((size, size), Image.LANCZOS))


class ZebraPrintApp(tk.Tk, *DND_BASES):
    def __init__(self):
        super().__init__()
        self.title("Zebra ZT410 Label Printer")
        self.configure(bg=BG)
        self._scale = self.winfo_fpixels('1i') / 96
        self.minsize(self._px(980), self._px(640))

        saved = load_settings()
        try:
            self.geometry(saved.get('size') or f"{self._px(1200)}x{self._px(780)}")
        except tk.TclError:
            self.geometry(f"{self._px(1200)}x{self._px(780)}")

        # State
        self.folder        = tk.StringVar()
        self.images: list[str] = []
        self.checked: set[int] = set()
        self._last_toggled: int | None = None
        self.dither_mode   = tk.StringVar(value='Bayer 8x8')
        self.quantity      = tk.IntVar(value=1)
        self.contrast      = tk.DoubleVar(value=DEFAULT_CONTRAST)
        self.brightness    = tk.DoubleVar(value=DEFAULT_BRIGHTNESS)
        self.sharpen       = tk.DoubleVar(value=DEFAULT_SHARPEN)
        self.flip          = tk.BooleanVar(value=False)
        self.darkness      = tk.StringVar(value=PRINTER_SETTING)
        self.speed         = tk.StringVar(value=PRINTER_SETTING)
        self.media         = tk.StringVar(value=PRINTER_SETTING)
        self.text_template = tk.StringVar()
        self.text_position = tk.StringVar(value=TEXT_POSITIONS[0])
        self.text_size     = tk.IntVar(value=TextOverlay.size)
        self.text_band     = tk.BooleanVar(value=True)
        self.preset_var    = tk.StringVar()
        self.printer_var   = tk.StringVar()
        self.status_var    = tk.StringVar(value="Choose a folder to begin.")
        self.progress_var  = tk.DoubleVar(value=0)
        self.tearoff_pause = tk.BooleanVar(value=False)
        self.label_wait    = tk.IntVar(value=0)
        self.y_offset_var      = tk.IntVar(value=Y_OFFSET_DOTS)
        self.height_adjust_var = tk.IntVar(value=HEIGHT_ADJUST_DOTS)

        # Everything here is remembered between launches
        self._persisted = {
            'folder': self.folder, 'printer': self.printer_var,
            'dither': self.dither_mode, 'flip': self.flip,
            'copies': self.quantity, 'tearoff': self.tearoff_pause, 'label_wait': self.label_wait,
            'y_offset': self.y_offset_var, 'height_adjust': self.height_adjust_var,
            'speed': self.speed, 'media': self.media,
            'text_position': self.text_position, 'text_size': self.text_size,
            'text_band': self.text_band,
        }
        for key, var in self._persisted.items():
            if key in saved:
                try:
                    var.set(saved[key])
                except tk.TclError:
                    pass
        if self.dither_mode.get() not in DITHER_MODES:
            self.dither_mode.set('Bayer 8x8')
        if self.darkness.get() not in DARKNESS_CHOICES:
            self.darkness.set(PRINTER_SETTING)
        if self.speed.get() not in SPEED_CHOICES:
            self.speed.set(PRINTER_SETTING)
        if self.media.get() not in MEDIA_CHOICES:
            self.media.set(PRINTER_SETTING)
        # Unknown means assume yes: restoring saved values is harmless, a stale value is not
        self._printer_changed = saved.get('printer_changed', True)
        self.presets: dict = saved.get('presets') if isinstance(saved.get('presets'), dict) else {}
        self.crops: dict[str, tuple[float, float]] = {}
        self._history = load_history()

        # Tk may only be touched from the main thread; workers queue their UI updates here
        self._ui_calls: queue.Queue = queue.Queue()
        self._cancel_flag   = threading.Event()
        self._tearoff_event = threading.Event()
        self._printer_speed: float | None = None
        self._print_thread: threading.Thread | None = None
        self._clearing = False

        # Preview: the worker always renders the newest request and skips any in between
        self._preview_req: tuple | None = None
        self._preview_wake = threading.Event()
        self._preview_img: Image.Image | None = None
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._preview_scale = 1.0
        self._drag: dict | None = None
        self._change_job: str | None = None

        self._row_icons: dict[int, tuple] = {}
        self._thumb_run = 0
        self._device = find_printer_device()

        self._make_icons()
        self._style()
        self._build_ui()
        self._refresh_printers(saved.get('printer'))

        self._printer_name = self.printer_var.get()
        for var in (*self._persisted.values(), self.contrast, self.brightness, self.sharpen,
                    self.text_template):
            var.trace_add('write', self._on_setting_change)
        self.bind('<Configure>', self._on_window_configure)
        self.protocol('WM_DELETE_WINDOW', self._on_close)

        threading.Thread(target=self._preview_worker, daemon=True).start()
        threading.Thread(target=self._status_worker, daemon=True).start()
        self._drain_ui_calls()
        self._enable_file_drop()
        self._update_print_button()
        self._use_dark_title_bar()

        if self.folder.get() and os.path.isdir(self.folder.get()):
            self._load_images(self.folder.get())

    def _call_ui(self, fn, *args):
        """Run fn(*args) on the main thread. Safe to call from any thread."""
        self._ui_calls.put((fn, args))

    def _drain_ui_calls(self):
        while True:
            try:
                fn, args = self._ui_calls.get_nowait()
            except queue.Empty:
                break
            fn(*args)
        self.after(30, self._drain_ui_calls)

    def _px(self, n: int) -> int:
        """Scale a 96-dpi pixel size for this display."""
        return round(n * self._scale)

    def _use_dark_title_bar(self):
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value),
                                                       ctypes.sizeof(value))
        except Exception:
            pass    # cosmetic only

    # ------------------------------------------------------------------
    def _make_icons(self):
        size = self._px(16)
        self._box_images = {state: make_checkbox_image(size, state) for state in (False, True)}
        self._box_on  = ImageTk.PhotoImage(self._box_images[True], master=self)
        self._box_off = ImageTk.PhotoImage(self._box_images[False], master=self)
        track, knob = make_slider_images(size)
        self._slider_track = ImageTk.PhotoImage(track, master=self)
        self._slider_knob  = ImageTk.PhotoImage(knob, master=self)
        self._blank_row = (self._make_row_icon(False), self._make_row_icon(True))

    def _make_row_icon(self, checked: bool, thumb: Image.Image | None = None) -> ImageTk.PhotoImage:
        """A list row's image: its checkbox, then its thumbnail once that has loaded."""
        box = self._box_images[checked]
        thumb_size, gap = self._px(36), self._px(10)
        icon = Image.new('RGBA', (box.width + gap + thumb_size, thumb_size), (0, 0, 0, 0))
        icon.paste(box, (0, (thumb_size - box.height) // 2), box)
        if thumb is not None:
            icon.paste(thumb, (box.width + gap + (thumb_size - thumb.width) // 2,
                               (thumb_size - thumb.height) // 2))
        return ImageTk.PhotoImage(icon, master=self)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        flat = dict(borderwidth=0, relief='flat', focuscolor='', focusthickness=0)

        style.configure('.', background=CARD, foreground=TEXT, font=FONT,
                        bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
        style.configure('TFrame', background=CARD)
        style.configure('App.TFrame', background=BG)
        style.configure('TLabel', background=CARD, foreground=TEXT, font=FONT)
        style.configure('Sub.TLabel', background=CARD, foreground=SUBTEXT, font=FONT_SMALL)
        style.configure('Head.TLabel', background=CARD, foreground=TEXT, font=FONT_HEAD)
        style.configure('Section.TLabel', background=CARD, foreground=ACCENT, font=FONT_BOLD)
        style.configure('Status.TLabel', background=BG, foreground=SUBTEXT, font=FONT_SMALL)

        pad = (self._px(12), self._px(7))
        style.configure('TButton', background=FIELD, foreground=TEXT, font=FONT,
                        padding=pad, bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD, **flat)
        style.map('TButton',
                  background=[('disabled', CARD), ('pressed', FIELD), ('active', FIELD_HOVER)],
                  bordercolor=[('disabled', FIELD), ('active', FIELD_HOVER)],
                  lightcolor=[('disabled', CARD), ('active', FIELD_HOVER)],
                  darkcolor=[('disabled', CARD), ('active', FIELD_HOVER)],
                  foreground=[('disabled', DISABLED)])

        style.configure('Accent.TButton', background=ACCENT, foreground=ON_ACCENT,
                        font=FONT_BOLD, padding=(self._px(12), self._px(11)),
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, **flat)
        style.map('Accent.TButton',
                  background=[('disabled', FIELD), ('pressed', ACCENT_DOWN), ('active', ACCENT_HOVER)],
                  bordercolor=[('disabled', FIELD), ('active', ACCENT_HOVER)],
                  lightcolor=[('disabled', FIELD), ('active', ACCENT_HOVER)],
                  darkcolor=[('disabled', FIELD), ('active', ACCENT_HOVER)],
                  foreground=[('disabled', SUBTEXT)])

        style.configure('Link.TButton', background=CARD, foreground=SUBTEXT, font=FONT_SMALL,
                        padding=(self._px(6), self._px(2)), width=0,
                        bordercolor=CARD, lightcolor=CARD, darkcolor=CARD, **flat)
        style.map('Link.TButton',
                  background=[('disabled', CARD), ('active', CARD)],
                  foreground=[('disabled', DISABLED), ('active', ACCENT)],
                  bordercolor=[('active', CARD)], lightcolor=[('active', CARD)],
                  darkcolor=[('active', CARD)])

        for name, fg, bg in (('Tab.TButton', SUBTEXT, CARD), ('TabOn.TButton', ACCENT, FIELD)):
            style.configure(name, background=bg, foreground=fg, font=FONT_BOLD, width=0,
                            padding=(self._px(10), self._px(6)),
                            bordercolor=bg, lightcolor=bg, darkcolor=bg, **flat)
            style.map(name, background=[('active', bg)],
                      foreground=[('active', ACCENT if bg == FIELD else TEXT)],
                      bordercolor=[('active', bg)], lightcolor=[('active', bg)],
                      darkcolor=[('active', bg)])

        field = dict(fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                     bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD,
                     arrowcolor=SUBTEXT, insertcolor=TEXT,
                     selectbackground=FIELD, selectforeground=TEXT,
                     padding=(self._px(6), self._px(4)))
        field_map = dict(fieldbackground=[('readonly', FIELD), ('disabled', CARD)],
                         background=[('active', FIELD_HOVER)],
                         foreground=[('readonly', TEXT)],
                         selectbackground=[('readonly', FIELD), ('!disabled', ACCENT_DOWN)],
                         selectforeground=[('readonly', TEXT), ('!disabled', ON_ACCENT)],
                         arrowcolor=[('active', TEXT)],
                         bordercolor=[('!disabled', FIELD)],
                         lightcolor=[('!disabled', FIELD)],
                         darkcolor=[('!disabled', FIELD)])
        style.configure('TCombobox', **field)
        style.map('TCombobox', **field_map)
        style.configure('TSpinbox', **field)
        style.map('TSpinbox', **field_map)
        style.configure('TEntry', **field)
        style.map('TEntry', **field_map)
        self.option_add('*TCombobox*Listbox.background', FIELD)
        self.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
        self.option_add('*TCombobox*Listbox.selectForeground', ON_ACCENT)
        self.option_add('*TCombobox*Listbox.font', FONT)
        self.option_add('*TCombobox*Listbox.borderWidth', 0)

        edge = self._px(16) // 2 - 1
        style.element_create('Flat.Scale.trough', 'image', self._slider_track,
                             border=(edge, 0), sticky='ew')
        style.element_create('Flat.Scale.slider', 'image', self._slider_knob, sticky='')
        style.layout('Horizontal.TScale', [('Flat.Scale.trough', {'sticky': 'ew', 'children': [
            ('Flat.Scale.slider', {'side': 'left', 'sticky': ''})]})])
        style.configure('Horizontal.TScale', background=CARD)

        style.layout('Thin.Horizontal.TProgressbar', [
            ('Horizontal.Progressbar.trough', {'sticky': 'nswe', 'children': [
                ('Horizontal.Progressbar.pbar', {'side': 'left', 'sticky': 'ns'})]})])
        style.configure('Thin.Horizontal.TProgressbar', background=ACCENT, troughcolor=CARD,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT,
                        borderwidth=0)

        style.layout('Vertical.TScrollbar', [
            ('Vertical.Scrollbar.trough', {'sticky': 'ns', 'children': [
                ('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
        style.configure('Vertical.TScrollbar', background=FIELD_HOVER, troughcolor=CARD,
                        bordercolor=CARD, lightcolor=FIELD_HOVER, darkcolor=FIELD_HOVER,
                        borderwidth=0, width=self._px(8), arrowsize=0)
        style.map('Vertical.TScrollbar', background=[('active', SUBTEXT)])

        style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        style.layout('Treeview.Item', [('Treeitem.padding', {'sticky': 'nswe', 'children': [
            ('Treeitem.image', {'side': 'left', 'sticky': ''}),
            ('Treeitem.text', {'side': 'left', 'sticky': ''})]})])
        style.configure('Treeview', background=CARD, fieldbackground=CARD, foreground=TEXT,
                        font=FONT, rowheight=self._px(44), indent=0, borderwidth=0,
                        padding=0)
        style.configure('Treeview.Item', padding=(self._px(6), 0))
        style.map('Treeview', background=[('selected', ROW_ACTIVE)],
                  foreground=[('selected', TEXT)])

        style.configure('History.Treeview', rowheight=self._px(28))
        style.configure('Treeview.Heading', background=FIELD, foreground=SUBTEXT, font=FONT_SMALL,
                        bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD, relief='flat')
        style.map('Treeview.Heading', background=[('active', FIELD)])

        style.element_create('Flat.Checkbutton.indicator', 'image', self._box_off,
                             ('selected', self._box_on), sticky='')
        style.layout('TCheckbutton', [('Checkbutton.padding', {'sticky': 'nswe', 'children': [
            ('Flat.Checkbutton.indicator', {'side': 'left', 'sticky': ''}),
            ('Checkbutton.label', {'side': 'left', 'sticky': 'nswe'})]})])
        style.configure('TCheckbutton', background=CARD, foreground=TEXT, font=FONT,
                        padding=(0, self._px(3)))
        style.map('TCheckbutton', background=[('active', CARD)], foreground=[('active', TEXT)])

    # ------------------------------------------------------------------
    def _card(self, parent) -> ttk.Frame:
        return ttk.Frame(parent, padding=self._px(16))

    def _section(self, parent, text: str, first: bool = False):
        ttk.Label(parent, text=text, style='Section.TLabel').pack(
            anchor='w', pady=(0 if first else self._px(16), self._px(6)))

    def _field(self, parent, text: str) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=self._px(3))
        ttk.Label(row, text=text).pack(side='left')
        return row

    def _build_ui(self):
        root = ttk.Frame(self, style='App.TFrame', padding=self._px(12))
        root.pack(fill='both', expand=True)
        root.columnconfigure(0, weight=0, minsize=self._px(320))
        root.columnconfigure(1, weight=1)
        root.columnconfigure(2, weight=0, minsize=self._px(320))
        root.rowconfigure(0, weight=1)

        self._build_image_list(root)
        self._build_preview(root)
        self._build_settings(root)

        bot = ttk.Frame(root, style='App.TFrame')
        bot.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(self._px(10), 0))
        line = ttk.Frame(bot, style='App.TFrame')
        line.pack(fill='x')
        self.printer_state_lbl = ttk.Label(line, style='Status.TLabel')
        self.printer_state_lbl.pack(side='right')
        ttk.Label(line, textvariable=self.status_var, style='Status.TLabel').pack(side='left')
        bar = ttk.Frame(bot, style='App.TFrame', height=self._px(4))
        bar.pack(fill='x', pady=(self._px(6), 0))
        bar.pack_propagate(False)
        ttk.Progressbar(bar, variable=self.progress_var, maximum=100, mode='determinate',
                        style='Thin.Horizontal.TProgressbar').pack(fill='both', expand=True)

    def _build_image_list(self, root):
        card = self._card(root)
        card.grid(row=0, column=0, sticky='nsew', padx=(0, self._px(10)))

        head = ttk.Frame(card)
        head.pack(fill='x')
        ttk.Label(head, text="Images", style='Head.TLabel').pack(side='left')
        ttk.Button(head, text="Choose images…", command=self._choose_images).pack(side='right')

        self.folder_lbl = ttk.Label(card, text="No folder chosen", style='Sub.TLabel')
        self.folder_lbl.pack(fill='x', pady=(self._px(6), self._px(10)))

        tools = ttk.Frame(card)
        tools.pack(side='bottom', fill='x', pady=(self._px(10), 0))
        self.count_lbl = ttk.Label(tools, text="0 selected", style='Sub.TLabel')
        self.count_lbl.pack(side='left')
        ttk.Button(tools, text="None", style='Link.TButton',
                   command=lambda: self._check_all(False)).pack(side='right')
        ttk.Button(tools, text="All", style='Link.TButton',
                   command=lambda: self._check_all(True)).pack(side='right')

        listframe = ttk.Frame(card)
        listframe.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(listframe, orient='vertical')
        self.tree = ttk.Treeview(listframe, show='tree', selectmode='browse',
                                 yscrollcommand=scrollbar.set)
        scrollbar.config(command=self.tree.yview)
        scrollbar.pack(side='right', fill='y', padx=(self._px(4), 0))
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<space>', self._on_tree_space)
        self.tree.bind('<<TreeviewSelect>>', lambda _: self._request_preview())

    def _build_preview(self, root):
        card = self._card(root)
        card.grid(row=0, column=1, sticky='nsew', padx=(0, self._px(10)))

        ttk.Label(card, text="Preview", style='Head.TLabel').pack(anchor='w')
        self.preview_caption = ttk.Label(card, text="As it will print", style='Sub.TLabel')
        self.preview_caption.pack(fill='x', pady=(self._px(6), self._px(10)))

        self.preview_canvas = tk.Canvas(card, bg=BG, highlightthickness=0, bd=0,
                                        width=self._px(240), height=self._px(360))
        self.preview_canvas.pack(fill='both', expand=True)
        self.preview_canvas.bind('<Configure>', lambda _: self._draw_preview())
        self.preview_canvas.bind('<ButtonPress-1>', self._on_preview_press)
        self.preview_canvas.bind('<B1-Motion>', self._on_preview_drag)
        self.preview_canvas.bind('<ButtonRelease-1>', self._on_preview_release)
        self.preview_canvas.bind('<Double-Button-1>', self._on_preview_double_click)

    def _build_settings(self, root):
        card = self._card(root)
        card.grid(row=0, column=2, sticky='nsew')

        # Actions are packed first so they stay pinned to the bottom of the panel
        actions = ttk.Frame(card)
        actions.pack(side='bottom', fill='x', pady=(self._px(14), 0))
        tools = ttk.Frame(actions)
        tools.pack(fill='x', pady=(0, self._px(8)))
        tools.columnconfigure((0, 1), weight=1, uniform='tools')
        ttk.Button(tools, text="History", command=self._show_history).grid(
            row=0, column=0, sticky='ew', padx=(0, self._px(4)))
        self.reprint_btn = ttk.Button(tools, text="Reprint last", command=self._reprint_last)
        self.reprint_btn.grid(row=0, column=1, sticky='ew', padx=(self._px(4), 0))
        self.print_btn = ttk.Button(actions, style='Accent.TButton', command=self._start_print)
        self.print_btn.pack(fill='x')
        self.cancel_btn = ttk.Button(actions, text="Cancel printing", command=self._cancel)

        tabs = ttk.Frame(card)
        tabs.pack(fill='x', pady=(0, self._px(12)))
        body = ttk.Frame(card)
        body.pack(fill='both', expand=True)
        self._tabs = {}
        for name, build in (("Image", self._build_image_tab), ("Output", self._build_output_tab),
                            ("Text", self._build_text_tab), ("Align", self._build_align_tab)):
            frame = ttk.Frame(body)
            build(frame)
            button = ttk.Button(tabs, text=name, style='Tab.TButton',
                                command=lambda n=name: self._show_tab(n))
            button.pack(side='left', padx=(0, self._px(4)))
            self._tabs[name] = (button, frame)
        self._show_tab("Image")

    def _show_tab(self, name: str):
        for tab, (button, frame) in self._tabs.items():
            button.config(style='TabOn.TButton' if tab == name else 'Tab.TButton')
            if tab == name:
                frame.pack(fill='both', expand=True)
            else:
                frame.pack_forget()

    def _slider(self, parent, label: str, var: tk.DoubleVar, low: float, high: float,
                step: float, default: float, fmt: str):
        """A labelled slider: snaps to steps, follows the wheel, double-click resets it."""
        row = self._field(parent, label)
        value = ttk.Label(row, text=fmt.format(var.get()), style='Sub.TLabel')
        value.pack(side='right')

        def snap(raw):
            var.set(round(low + round((float(raw) - low) / step) * step, 2))

        def wheel(event):
            snap(min(high, max(low, var.get() + (step if event.delta > 0 else -step))))
            return 'break'

        def reset(_event):
            var.set(default)
            return 'break'

        scale = ttk.Scale(parent, variable=var, from_=low, to=high, orient='horizontal', command=snap)
        scale.pack(fill='x', pady=(self._px(2), self._px(6)))
        scale.bind('<Double-Button-1>', reset)
        for widget in (scale, row, *row.winfo_children()):
            widget.bind('<MouseWheel>', wheel)
        var.trace_add('write', lambda *_: value.config(text=fmt.format(var.get())))

    def _build_image_tab(self, tab):
        self._section(tab, "Preset", first=True)
        row = ttk.Frame(tab)
        row.pack(fill='x')
        ttk.Button(row, text="Delete", style='Link.TButton',
                   command=self._delete_preset).pack(side='right')
        ttk.Button(row, text="Save", style='Link.TButton',
                   command=self._save_preset).pack(side='right')
        self.preset_cb = ttk.Combobox(row, textvariable=self.preset_var, state='readonly', font=FONT)
        self.preset_cb.pack(side='left', fill='x', expand=True, padx=(0, self._px(4)))
        self.preset_cb.bind('<<ComboboxSelected>>', lambda _: self._apply_preset())
        self._refresh_presets()

        self._section(tab, "Image")
        row = self._field(tab, "Dither")
        ttk.Combobox(row, textvariable=self.dither_mode, values=DITHER_MODES, state='readonly',
                     font=FONT, width=16).pack(side='right')
        self._slider(tab, "Contrast", self.contrast, CONTRAST_MIN, CONTRAST_MAX, CONTRAST_STEP,
                     DEFAULT_CONTRAST, "{:.2f}")
        self._slider(tab, "Brightness", self.brightness, BRIGHTNESS_MIN, BRIGHTNESS_MAX,
                     BRIGHTNESS_STEP, DEFAULT_BRIGHTNESS, "{:.2f}")
        self._slider(tab, "Sharpen", self.sharpen, 0, SHARPEN_MAX, SHARPEN_STEP,
                     DEFAULT_SHARPEN, "{:.0f}%")
        ttk.Checkbutton(tab, text="  Flip 180°", variable=self.flip).pack(anchor='w')

    def _build_output_tab(self, tab):
        self._section(tab, "Printer", first=True)
        self.printer_cb = ttk.Combobox(tab, textvariable=self.printer_var,
                                       state='readonly', font=FONT)
        self.printer_cb.pack(fill='x', pady=(0, self._px(4)))
        row = self._field(tab, "Media")
        ttk.Combobox(row, textvariable=self.media, values=list(MEDIA_CHOICES), state='readonly',
                     font=FONT, width=14).pack(side='right')
        row = self._field(tab, "Darkness")
        ttk.Combobox(row, textvariable=self.darkness, values=DARKNESS_CHOICES, state='readonly',
                     font=FONT, width=14).pack(side='right')
        row = self._field(tab, "Speed (in/s)")
        ttk.Combobox(row, textvariable=self.speed, values=SPEED_CHOICES, state='readonly',
                     font=FONT, width=14).pack(side='right')
        self.printer_hint = ttk.Label(tab, style='Sub.TLabel')
        self.printer_hint.pack(anchor='w', pady=(self._px(2), self._px(8)))
        self.clear_btn = ttk.Button(tab, text="Clear stuck jobs", command=self._clear_stuck_jobs)
        self.clear_btn.pack(fill='x')

        self._section(tab, "Output")
        row = self._field(tab, "Copies per image")
        ttk.Spinbox(row, textvariable=self.quantity, from_=1, to=99, width=5,
                    font=FONT).pack(side='right')
        row = self._field(tab, "Wait between labels (s)")
        ttk.Spinbox(row, textvariable=self.label_wait, from_=0, to=30, width=5,
                    font=FONT).pack(side='right')
        ttk.Checkbutton(tab, text="  Pause after each label",
                        variable=self.tearoff_pause).pack(anchor='w')

    def _build_align_tab(self, tab):
        self._section(tab, "Alignment", first=True)
        row = self._field(tab, "Y offset (dots)")
        ttk.Spinbox(row, textvariable=self.y_offset_var, from_=-200, to=200, width=5,
                    font=FONT).pack(side='right')
        row = self._field(tab, "Height adjust (dots)")
        ttk.Spinbox(row, textvariable=self.height_adjust_var, from_=-200, to=200, width=5,
                    font=FONT).pack(side='right')
        ttk.Label(tab, text=f"{mm_to_dots(1)} dots = 1 mm", style='Sub.TLabel').pack(
            anchor='w', pady=(0, self._px(12)))
        ttk.Button(tab, text="Print test pattern", command=self._print_test_pattern).pack(fill='x')

    def _build_text_tab(self, tab):
        self._section(tab, "Text on label", first=True)
        ttk.Entry(tab, textvariable=self.text_template, font=FONT).pack(fill='x', pady=(0, self._px(4)))
        ttk.Label(tab, text="Leave blank for no text. These fill in for each label:\n"
                            "{n}  label number      {total}  labels in run\n"
                            "{name}  file name      {date}  today",
                  style='Sub.TLabel', justify='left').pack(anchor='w', pady=(0, self._px(8)))
        row = self._field(tab, "Position")
        ttk.Combobox(row, textvariable=self.text_position, values=TEXT_POSITIONS, state='readonly',
                     font=FONT, width=10).pack(side='right')
        row = self._field(tab, "Size (dots)")
        ttk.Spinbox(row, textvariable=self.text_size, from_=16, to=200, increment=2, width=5,
                    font=FONT).pack(side='right')
        ttk.Checkbutton(tab, text="  White strip behind text", variable=self.text_band).pack(anchor='w')

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @staticmethod
    def _int(var: tk.IntVar, default: int) -> int:
        """Read a spinbox value, which is briefly blank or invalid while someone types."""
        try:
            return var.get()
        except tk.TclError:
            return default

    def _read_image_settings(self) -> ImageSettings:
        return ImageSettings(self.dither_mode.get(), round(self.contrast.get(), 2),
                             round(self.brightness.get(), 2), int(self.sharpen.get()),
                             self.flip.get(), self._int(self.height_adjust_var, HEIGHT_ADJUST_DOTS))

    def _read_overlay(self) -> TextOverlay:
        return TextOverlay(self.text_template.get(), self.text_position.get(),
                           max(12, self._int(self.text_size, TextOverlay.size)), self.text_band.get())

    def _read_printer_options(self) -> PrinterOptions:
        def number(value: str) -> int | None:
            return None if value == PRINTER_SETTING else int(value)
        return PrinterOptions(number(self.darkness.get()), number(self.speed.get()),
                              MEDIA_CHOICES[self.media.get()])

    def _on_setting_change(self, *_):
        self._printer_name = self.printer_var.get()
        self._update_print_button()
        self._debounce_commit()

    def _on_window_configure(self, event):
        if event.widget is self:
            self._debounce_commit()

    def _debounce_commit(self):
        if self._change_job is not None:
            self.after_cancel(self._change_job)
        self._change_job = self.after(250, self._commit_settings)

    def _commit_settings(self):
        self._change_job = None
        self._save_settings()
        self._request_preview()

    def _save_settings(self):
        data = {'size': f"{self.winfo_width()}x{self.winfo_height()}", 'presets': self.presets,
                'printer_changed': self._printer_changed}
        for key, var in self._persisted.items():
            try:
                data[key] = var.get()
            except tk.TclError:
                pass
        save_settings(data)

    def _on_close(self):
        self._save_settings()
        self.destroy()

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------
    def _refresh_presets(self):
        self.preset_cb['values'] = ['Default', *sorted(self.presets)]
        if self.preset_var.get() not in self.preset_cb['values']:
            self.preset_var.set('Default')

    def _preset_values(self) -> dict:
        return {'dither': self.dither_mode.get(), 'contrast': round(self.contrast.get(), 2),
                'brightness': round(self.brightness.get(), 2), 'sharpen': int(self.sharpen.get()),
                'darkness': self.darkness.get(), 'speed': self.speed.get(),
                'media': self.media.get()}

    def _set_look(self, values: dict):
        """Apply a saved set of image and printer settings. Missing keys keep their defaults."""
        self.dither_mode.set(values.get('dither', 'Bayer 8x8'))
        self.contrast.set(values.get('contrast', DEFAULT_CONTRAST))
        self.brightness.set(values.get('brightness', DEFAULT_BRIGHTNESS))
        self.sharpen.set(values.get('sharpen', DEFAULT_SHARPEN))
        self.darkness.set(values.get('darkness', PRINTER_SETTING))
        self.speed.set(values.get('speed', PRINTER_SETTING))
        self.media.set(values.get('media', PRINTER_SETTING))

    def _apply_preset(self):
        self._set_look(self.presets.get(self.preset_var.get(), {}))

    def _save_preset(self):
        name = self._ask_text("Save preset", "Name this preset:",
                              "" if self.preset_var.get() == 'Default' else self.preset_var.get())
        if not name or name == 'Default':
            return
        self.presets[name] = self._preset_values()
        self.preset_var.set(name)
        self._refresh_presets()
        self._save_settings()
        self._set_status(f"Saved preset “{name}”.")

    def _delete_preset(self):
        name = self.preset_var.get()
        if name in self.presets and messagebox.askokcancel("Delete preset", f"Delete “{name}”?"):
            del self.presets[name]
            self.preset_var.set('Default')
            self._refresh_presets()
            self._save_settings()

    def _ask_text(self, title: str, prompt: str, initial: str = "") -> str | None:
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=CARD)
        win.resizable(False, False)
        win.transient(self)
        result = {}
        value = tk.StringVar(value=initial)
        body = ttk.Frame(win, padding=self._px(16))
        body.pack()
        ttk.Label(body, text=prompt).pack(anchor='w', pady=(0, self._px(6)))
        entry = ttk.Entry(body, textvariable=value, font=FONT, width=32)
        entry.pack(fill='x', pady=(0, self._px(12)))
        def accept(_event=None):
            result['text'] = value.get().strip()
            win.destroy()
        buttons = ttk.Frame(body)
        buttons.pack(anchor='e')
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side='right')
        ttk.Button(buttons, text="Save", style='Accent.TButton', command=accept).pack(
            side='right', padx=(0, self._px(6)))
        entry.bind('<Return>', accept)
        entry.bind('<Escape>', lambda _: win.destroy())
        self._center(win)
        win.grab_set()
        entry.focus_force()
        self.wait_window(win)
        return result.get('text')

    def _center(self, win):
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Image list
    # ------------------------------------------------------------------
    def _choose_images(self):
        picked = filedialog.askopenfilenames(
            parent=self, title="Choose images",
            initialdir=self.folder.get() or None,
            filetypes=[("JPEG and PNG images", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
        )
        if picked:
            self._open_paths(list(picked))

    def _open_paths(self, paths: list):
        """Load the folder of the given files. Several files are also ticked."""
        first = paths[0]
        folder = first if os.path.isdir(first) else os.path.dirname(first)
        self.folder.set(folder)
        self._load_images(folder)

        names = {os.path.basename(path) for path in paths if not os.path.isdir(path)}
        rows = [i for i, path in enumerate(self.images) if os.path.basename(path) in names]
        if not rows:
            return
        if len(rows) > 1:
            for i in rows:
                self._set_checked(i, True)
            self._update_print_button()
        self.tree.selection_set(str(rows[0]))
        self.tree.focus(str(rows[0]))
        self.tree.see(str(rows[0]))

    def _enable_file_drop(self):
        if TkinterDnD is None:
            return
        try:
            self.TkdndVersion = TkinterDnD._require(self)
            self.drop_target_register(DND_FILES)
            self.dnd_bind('<<Drop>>', self._on_file_drop)
        except (RuntimeError, tk.TclError):
            pass    # drag and drop is optional

    def _on_file_drop(self, event):
        paths = list(self.tk.splitlist(event.data))
        if paths:
            self._open_paths(paths)
        return event.action

    def _load_images(self, folder: str):
        exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}
        try:
            files = sorted(f for f in os.listdir(folder)
                           if os.path.splitext(f)[1].lower() in exts)
        except OSError as e:
            self._set_status(f"Couldn't open folder: {e}")
            return
        self.images = [os.path.join(folder, f) for f in files]
        self.checked.clear()
        self._last_toggled = None
        self.tree.delete(*self.tree.get_children())
        self._row_icons = {}
        for i, f in enumerate(files):
            self.tree.insert('', 'end', iid=str(i), text=f"   {f}", image=self._blank_row[0])
        self._thumb_run += 1
        threading.Thread(target=self._thumbnail_worker, daemon=True,
                         args=(self._thumb_run, list(self.images))).start()
        self.folder_lbl.config(text=os.path.basename(folder.rstrip('/\\')) or folder)
        self._update_print_button()
        self._set_status(f"Loaded {len(files)} image(s).")
        if files:
            self.tree.selection_set('0')
            self.tree.focus('0')
        else:
            self._request_preview()

    def _set_checked(self, idx: int, checked: bool):
        if checked:
            self.checked.add(idx)
        else:
            self.checked.discard(idx)
        self.tree.item(str(idx), image=self._row_icons.get(idx, self._blank_row)[checked])

    def _thumbnail_worker(self, run: int, paths: list):
        size = self._px(36)
        for idx, path in enumerate(paths):
            if run != self._thumb_run:
                return      # a different folder was opened
            try:
                with Image.open(path) as img:
                    img.draft('RGB', (size * 2, size * 2))
                    thumb = ImageOps.exif_transpose(img).convert('RGB')
                thumb.thumbnail((size, size))
            except Exception:
                continue
            self._call_ui(self._set_thumbnail, run, idx, thumb)

    def _set_thumbnail(self, run: int, idx: int, thumb: Image.Image):
        if run != self._thumb_run:
            return
        self._row_icons[idx] = (self._make_row_icon(False, thumb), self._make_row_icon(True, thumb))
        self.tree.item(str(idx), image=self._row_icons[idx][idx in self.checked])

    def _check_all(self, checked: bool):
        for i in range(len(self.images)):
            self._set_checked(i, checked)
        self._update_print_button()

    def _on_tree_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row or event.x > self._px(34):
            return      # a click on the name only previews
        idx = int(row)
        shift_held = event.state & 0x0001
        if shift_held and self._last_toggled is not None:
            state = self._last_toggled in self.checked
            low, high = sorted((self._last_toggled, idx))
            for i in range(low, high + 1):
                self._set_checked(i, state)
        else:
            self._set_checked(idx, idx not in self.checked)
        self._last_toggled = idx
        self._update_print_button()

    def _on_tree_space(self, _event):
        sel = self.tree.selection()
        if sel:
            idx = int(sel[0])
            self._set_checked(idx, idx not in self.checked)
            self._last_toggled = idx
            self._update_print_button()
        return 'break'

    def _update_print_button(self):
        count = len(self.checked)
        self.count_lbl.config(text=f"{count} of {len(self.images)} selected")
        printing = self._print_thread is not None and self._print_thread.is_alive()
        self.reprint_btn.config(state='disabled' if printing or not self._history else 'normal')
        labels = count * max(1, self._int(self.quantity, 1))
        if count == 0:
            self.print_btn.config(text="Select images to print", state='disabled')
        else:
            text = "Print 1 label" if labels == 1 else f"Print {labels} labels"
            self.print_btn.config(text=text, state='disabled' if printing else 'normal')

    def _refresh_printers(self, preferred: str | None = None):
        printers = get_printers()
        self.printer_cb['values'] = printers
        if preferred in printers:
            self.printer_var.set(preferred)
        elif printers:
            self.printer_var.set(printers[0])
        else:
            self.printer_var.set('')
            self._set_status("No printers found. Is win32print installed?")

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def _selected_path(self) -> str | None:
        sel = self.tree.selection()
        return self.images[int(sel[0])] if sel else None

    def _request_preview(self):
        path = self._selected_path()
        if path is None:
            self._preview_req = None
            self._preview_img = None
            self.preview_caption.config(text="As it will print")
            self._draw_preview()
            return
        total = max(1, len(self.checked) * max(1, self._int(self.quantity, 1)))
        req = (path, self._read_image_settings(), self.crops.get(path, (0.5, 0.5)),
               self._read_overlay(), self._int(self.y_offset_var, Y_OFFSET_DOTS), total)
        if req == self._preview_req:
            return
        self._preview_req = req
        if self._drag is None:
            self.preview_caption.config(text="Rendering…")
        self._preview_wake.set()

    def _preview_worker(self):
        while True:
            self._preview_wake.wait()
            self._preview_wake.clear()
            req = self._preview_req
            if req is None:
                continue
            path, settings, crop, overlay, y_offset, total = req
            try:
                img = prepare_image(path, settings, crop)
                img = add_text(img, path, settings.flip, overlay, 1, total)
                result = label_preview(img, y_offset)
                error = None
            except Exception as e:
                result, error = None, str(e)
            self._call_ui(self._show_preview, req, result, error)

    def _show_preview(self, req: tuple, img: Image.Image | None, error: str | None):
        if req != self._preview_req:
            return          # settings moved on; a newer render is coming
        self._preview_img = img
        name = os.path.basename(req[0])
        if error:
            caption = f"Can't preview {name}: {error}"
        elif max(crop_surplus(req[0], req[1].height_adjust)) >= 1:
            caption = f"{name}  ·  drag to adjust crop"
        else:
            caption = name
        self.preview_caption.config(text=caption)
        self._draw_preview()

    def _draw_preview(self):
        canvas = self.preview_canvas
        canvas.delete('all')
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if self._preview_img is None:
            canvas.create_text(w // 2, h // 2, text="Click an image to preview it",
                               fill=SUBTEXT, font=FONT)
            return
        margin = self._px(16)
        img_w, img_h = self._preview_img.size
        scale = min((w - 2 * margin) / img_w, (h - 2 * margin) / img_h)
        size = (max(1, int(img_w * scale)), max(1, int(img_h * scale)))
        self._preview_scale = scale
        self._preview_photo = ImageTk.PhotoImage(
            self._preview_img.resize(size, Image.LANCZOS), master=self)
        canvas.create_image(w // 2, h // 2, image=self._preview_photo)

    def _on_preview_press(self, event):
        path = self._selected_path()
        if path is None or self._preview_img is None:
            return
        try:
            surplus = crop_surplus(path, self._int(self.height_adjust_var, HEIGHT_ADJUST_DOTS))
        except OSError:
            return
        self._drag = {'path': path, 'x': event.x, 'y': event.y, 'surplus': surplus,
                      'crop': self.crops.get(path, (0.5, 0.5))}

    def _on_preview_drag(self, event):
        drag = self._drag
        if drag is None:
            return
        moved = (event.x - drag['x'], event.y - drag['y'])
        crop = []
        for start, pixels, surplus in zip(drag['crop'], moved, drag['surplus']):
            # Dragging the photo right shows more of its left side, hence the minus
            shift = pixels / self._preview_scale / surplus if surplus >= 1 else 0
            crop.append(min(1.0, max(0.0, start - shift)))
        self.crops[drag['path']] = tuple(crop)
        self._request_preview()

    def _on_preview_release(self, _event):
        self._drag = None

    def _on_preview_double_click(self, _event):
        path = self._selected_path()
        if path is not None:
            self.crops.pop(path, None)
            self._request_preview()
        return 'break'

    # ------------------------------------------------------------------
    # Printer status
    # ------------------------------------------------------------------
    def _status_worker(self):
        while True:
            printing = self._print_thread is not None and self._print_thread.is_alive()
            if printing:
                self._call_ui(self._show_printer_state, "Printing…", True, None)
            elif self._device is None or 'ZT4' not in self._printer_name.upper():
                self._call_ui(self._show_printer_state, "", True, None)
            else:
                self._poll_printer()
            time.sleep(3)

    def _poll_printer(self):
        result = {}
        def query():
            try:
                result['state'] = read_printer_state(self._device)
            except Exception:
                result['state'] = None
        thread = threading.Thread(target=query, daemon=True)
        thread.start()
        thread.join(4)
        if thread.is_alive():
            self._call_ui(self._show_printer_state, "Printer not responding", False, None)
            thread.join()   # never stack up queries behind one that is stuck
        elif result['state'] is None:
            self._call_ui(self._show_printer_state, "Printer not connected", False, None)
        else:
            text, good = describe_status(result['state'])
            self._call_ui(self._show_printer_state, text, good, result['state'])

    def _show_printer_state(self, text: str, good: bool, state: dict | None):
        self.printer_state_lbl.config(text=f"●  {text}" if text else "",
                                      foreground=GOOD if good else ACCENT)
        if state is not None and state.get('speed'):
            self._printer_speed = float(state['speed']) or None
        if state is not None and state.get('darkness') and state.get('speed'):
            self.printer_hint.config(
                text=f"Printer now: darkness {state['darkness']}, speed {state['speed']} in/s")

    # ------------------------------------------------------------------
    # Printing
    # ------------------------------------------------------------------
    def _prepare_printer(self, options: PrinterOptions, printer: str):
        """Clear values left by earlier jobs, so 'Printer setting' means the printer's saved one."""
        chosen = [value is not None for value in (options.darkness, options.speed, options.media)]
        if self._printer_changed and not all(chosen):
            send_to_printer(RESTORE_SAVED_ZPL, printer)
        if self._printer_changed != any(chosen):
            self._printer_changed = any(chosen)
            self._call_ui(self._save_settings)

    def _print_test_pattern(self):
        printer = self.printer_var.get()
        if not printer:
            messagebox.showwarning("No Printer", "Select a printer.")
            return
        try:
            options = self._read_printer_options()
            zpl = make_test_pattern_zpl(
                y_offset=self._int(self.y_offset_var, Y_OFFSET_DOTS),
                height_adjust=self._int(self.height_adjust_var, HEIGHT_ADJUST_DOTS),
                options=options,
            )
            self._prepare_printer(options, printer)
            send_to_printer(zpl, printer)
            self._set_status("Test pattern sent — measure the white strip, then adjust Y offset / Height adjust.")
        except Exception as e:
            messagebox.showerror("Test Pattern Error", str(e))

    def _start_print(self):
        printer = self.printer_var.get()
        if not printer:
            messagebox.showwarning("No Printer", "Select a printer.")
            return
        paths = [self.images[i] for i in sorted(self.checked)]
        if not paths:
            return
        self._run_job({
            'printer': printer, 'paths': paths,
            'copies': max(1, self._int(self.quantity, 1)),
            'settings': asdict(self._read_image_settings()),
            'overlay': asdict(self._read_overlay()),
            'options': asdict(self._read_printer_options()),
            'crops': {p: self.crops[p] for p in paths if p in self.crops},
            'y_offset': self._int(self.y_offset_var, Y_OFFSET_DOTS),
            'tearoff': self.tearoff_pause.get(),
            'label_wait': max(0, self._int(self.label_wait, 0)),
        })

    def _run_job(self, job: dict):
        self._cancel_flag.clear()
        self.progress_var.set(0)
        self.cancel_btn.pack(fill='x', pady=(self._px(8), 0))
        self._print_thread = threading.Thread(target=self._print_thread_fn, args=(job,), daemon=True)
        self._print_thread.start()
        self._update_print_button()

    def _print_thread_fn(self, job: dict):
        paths, qty, printer = job['paths'], job['copies'], job['printer']
        settings = ImageSettings(**job['settings'])
        overlay = TextOverlay(**job['overlay'])
        options = PrinterOptions(**job['options'])
        y_offset = job['y_offset']
        # Numbered or paused labels differ or stop between copies, so they go one at a time.
        # Everything else is a single job per image, with the printer repeating it.
        wait = job.get('label_wait', 0)
        one_at_a_time = job['tearoff'] or wait > 0 or uses_label_number(overlay.template)
        total = len(paths) * qty
        done  = 0

        def send(bitmap, copies):
            nonlocal done
            labelled = add_text(bitmap, path, settings.flip, overlay, done + 1, total)
            send_to_printer(image_to_zpl(labelled, y_offset, options, copies), printer)
            done += copies
            self._call_ui(self.progress_var.set, done / total * 100)

        try:
            try:
                self._prepare_printer(options, printer)
            except Exception as e:
                self._set_status(f"Print error: {e}")
                return
            for i, path in enumerate(paths):
                if self._cancel_flag.is_set():
                    break
                fname = os.path.basename(path)
                self._set_status(f"Processing {i + 1}/{len(paths)}: {fname}")
                try:
                    crop = tuple(job['crops'].get(path, (0.5, 0.5)))
                    img = prepare_image(path, settings, crop)
                except Exception as e:
                    self._set_status(f"Error on {fname}: {e}")
                    return
                try:
                    if not one_at_a_time:
                        self._set_status(f"Printing {fname}  ({qty} copies)" if qty > 1
                                         else f"Printing {fname}")
                        send(img, qty)
                        continue
                    for q in range(qty):
                        if self._cancel_flag.is_set():
                            break
                        self._set_status(f"Printing {fname}  (copy {q + 1}/{qty})")
                        send(img, 1)
                        if job['tearoff'] and done < total and not self._cancel_flag.is_set():
                            self._tearoff_event.clear()
                            self._set_status(f"Tear off label {done}/{total} — then click Continue.")
                            self._call_ui(self._show_tearoff_dialog)
                            self._tearoff_event.wait()
                        elif wait and done < total and not self._cancel_flag.is_set():
                            # Sending returns long before the label is out, so allow for printing
                            speed = options.speed or self._printer_speed or SLOWEST_SPEED
                            self._set_status(f"Waiting {wait} s after label {done}/{total}")
                            self._cancel_flag.wait(wait + label_print_seconds(speed))
                except Exception as e:
                    self._set_status(f"Print error: {e}")
                    return
            if done < total:
                self._set_status(f"Cancelled — printed {done} of {total} label(s).")
            else:
                self._set_status(f"Done — printed {done} label(s).")
        finally:
            self._call_ui(self._record_run, job, done, total)
            self._call_ui(self._print_finished)

    def _record_run(self, job: dict, printed: int, total: int):
        if printed:
            run = {**job, 'time': datetime.now().isoformat(timespec='seconds'),
                   'printed': printed, 'total': total}
            self._history.append(run)
            append_history(run)

    def _print_finished(self):
        if self._print_thread is not None:
            self._print_thread.join()
        self.cancel_btn.pack_forget()
        self._update_print_button()

    def _show_tearoff_dialog(self):
        win = tk.Toplevel(self)
        win.title("Tear Off Label")
        win.configure(bg=CARD)
        win.resizable(False, False)
        win.transient(self)
        ttk.Label(win, text="Tear off the label, then click Continue.",
                  padding=self._px(20)).pack()
        def on_continue():
            win.destroy(); self._tearoff_event.set()
        def on_cancel():
            win.destroy(); self._cancel_flag.set(); self._tearoff_event.set()
        # Closing with the X must release the print thread too, or it waits forever
        win.protocol("WM_DELETE_WINDOW", on_cancel)
        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=(0, self._px(16)))
        ttk.Button(btn_frame, text="Continue", style='Accent.TButton',
                   command=on_continue).pack(side='left', padx=(self._px(10), self._px(6)))
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(
            side='left', padx=(self._px(6), self._px(10)))
        self._center(win)
        win.grab_set()
        win.focus_force()

    def _cancel(self):
        self._cancel_flag.set()
        self._set_status("Cancelling…")

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def _reprint_last(self):
        if self._history:
            self._reprint(self._history[-1])

    def _reprint(self, run: dict):
        if self._print_thread is not None and self._print_thread.is_alive():
            messagebox.showinfo("Printing", "Wait for the current print run to finish.")
            return
        paths = [p for p in run['paths'] if os.path.exists(p)]
        if not paths:
            messagebox.showwarning("Reprint", "The images from that run are no longer there.")
            return
        labels = len(paths) * run['copies']
        missing = len(run['paths']) - len(paths)
        note = f"\n\n{missing} image(s) from that run are missing and will be skipped." if missing else ""
        if messagebox.askokcancel("Reprint", f"Print {labels} label(s) with the same settings?{note}"):
            self._run_job({**run, 'paths': paths, 'printer': self.printer_var.get() or run['printer']})

    def _use_run_settings(self, run: dict):
        """Copy a past run's settings into the app, so they can be used on other images."""
        settings = run['settings']
        options = run['options']
        media = {v: k for k, v in MEDIA_CHOICES.items()}
        def choice(value) -> str:
            return PRINTER_SETTING if value is None else str(value)
        self._set_look({**settings, 'darkness': choice(options['darkness']),
                        'speed': choice(options['speed']),
                        'media': media.get(options['media'], PRINTER_SETTING)})
        self.flip.set(settings['flip'])
        self.height_adjust_var.set(settings['height_adjust'])
        self.y_offset_var.set(run['y_offset'])
        self.quantity.set(run['copies'])
        self.label_wait.set(run.get('label_wait', 0))
        self.text_template.set(run['overlay']['template'])
        self.text_position.set(run['overlay']['position'])
        self.text_size.set(run['overlay']['size'])
        self.text_band.set(run['overlay']['band'])
        self.crops.update({p: tuple(c) for p, c in run['crops'].items()})
        self._set_status(f"Settings restored from {run['time'].replace('T', ' ')}.")

    def _show_history(self):
        win = tk.Toplevel(self)
        win.title("Print history")
        win.configure(bg=CARD)
        win.transient(self)
        win.geometry(f"{self._px(760)}x{self._px(460)}")
        body = ttk.Frame(win, padding=self._px(16))
        body.pack(fill='both', expand=True)

        buttons = ttk.Frame(body)
        buttons.pack(side='bottom', fill='x', pady=(self._px(12), 0))
        columns = {'time': ("When", 150), 'images': ("Images", 260),
                   'labels': ("Labels", 70), 'look': ("Settings", 250)}
        tree = ttk.Treeview(body, columns=list(columns), show='headings', selectmode='browse',
                            style='History.Treeview')
        for key, (title, width) in columns.items():
            tree.heading(key, text=title, anchor='w')
            tree.column(key, width=self._px(width), anchor='w', stretch=key == 'images')
        scrollbar = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y', padx=(self._px(4), 0))
        tree.pack(side='left', fill='both', expand=True)

        runs = list(reversed(self._history[-200:]))
        for i, run in enumerate(runs):
            names = [os.path.basename(p) for p in run['paths']]
            shown = ", ".join(names[:2]) + (f" +{len(names) - 2} more" if len(names) > 2 else "")
            s = run['settings']
            darkness = run['options']['darkness']
            look = f"{s['dither']}, contrast {s['contrast']:.2f}"
            if darkness is not None:
                look += f", darkness {darkness}"
            when = run['time'].replace('T', ' ')[:16]
            tree.insert('', 'end', iid=str(i), values=(when, shown, run['printed'], look))
        if runs:
            tree.selection_set('0')

        def selected() -> dict | None:
            sel = tree.selection()
            return runs[int(sel[0])] if sel else None

        def reprint():
            run = selected()
            if run:
                self._reprint(run)

        def use_settings():
            run = selected()
            if run:
                self._use_run_settings(run)
                win.destroy()

        ttk.Button(buttons, text="Close", command=win.destroy).pack(side='right')
        ttk.Button(buttons, text="Use these settings", command=use_settings).pack(
            side='right', padx=(0, self._px(6)))
        ttk.Button(buttons, text="Reprint", style='Accent.TButton', command=reprint).pack(
            side='right', padx=(0, self._px(6)))
        self._center(win)
        win.focus_force()

    # ------------------------------------------------------------------
    # Stuck jobs
    # ------------------------------------------------------------------
    def _clear_stuck_jobs(self):
        if self._print_thread is not None and self._print_thread.is_alive():
            messagebox.showinfo("Printing", "Cancel the current print run first.")
            return
        if self._clearing:
            return
        queued = count_queued_jobs()
        if queued:
            listing = "\n".join(f"    {name}: {n} job(s)" for name, n in queued.items())
            queue_text = f"Queued right now:\n{listing}"
        else:
            queue_text = "Windows lists no queued jobs right now."
        if not messagebox.askokcancel(
                "Clear stuck jobs",
                "This deletes every queued job for ALL printers on this PC, then restarts "
                f"the Windows print spooler.\n\n{queue_text}\n\n"
                "Windows will ask for permission next.",
                icon='warning'):
            return
        self._clearing = True
        self.clear_btn.config(state='disabled')
        self._set_status("Clearing print jobs and restarting the spooler…")
        threading.Thread(target=self._clear_thread_fn, daemon=True).start()

    def _clear_thread_fn(self):
        try:
            code = run_powershell(SPOOLER_RESET_PS)
            if code == 0:
                self._set_status("Print jobs cleared and spooler restarted.")
            else:
                self._set_status(f"Couldn't clear every job (error {code}). "
                                 "The spooler was restarted if possible.")
        except Exception as e:
            if getattr(e, 'winerror', None) == ERROR_CANCELLED:
                self._set_status("Clear stuck jobs cancelled.")
            else:
                self._set_status(f"Couldn't clear jobs: {e}")
        finally:
            self._call_ui(self._clear_finished)

    def _clear_finished(self):
        self._clearing = False
        self.clear_btn.config(state='normal')

    def _set_status(self, msg: str):
        self._call_ui(self.status_var.set, msg)


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # sharp text on scaled displays
    except Exception:
        pass
    app = ZebraPrintApp()
    app.mainloop()
