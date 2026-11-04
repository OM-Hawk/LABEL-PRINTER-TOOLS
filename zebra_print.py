"""
Zebra ZT410 Label Printer GUI
Prints JPG images as 4x6 labels via USB with dithering options.

Install dependencies:
    pip install pillow pywin32 numpy

Usage: python zebra_print.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
from PIL import Image, ImageOps, ImageEnhance, ImageDraw, ImageFont
import numpy as np

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
HEIGHT_ADJUST_DOTS = 0

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

def prepare_image(path: str, dither_mode: str, contrast: float = 1.0,
                  orientation: str = 'portrait',
                  y_offset: int = Y_OFFSET_DOTS,
                  height_adjust: int = HEIGHT_ADJUST_DOTS) -> Image.Image:
    """Open a JPG, rotate if needed, fit to label (with optional height adjust), dither.

    Returns a 1-bit PIL image sized (LABEL_W_DOTS, LABEL_H_DOTS).
    The y_offset is encoded in the returned ZPL via image_to_zpl(), not in the bitmap.
    """
    img = Image.open(path).convert('RGB')
    w, h = img.size
    is_landscape = w > h

    # Determine target pixel dimensions for the render (may be taller than label for bleed)
    render_w = min(LABEL_W_DOTS, MAX_PRINT_WIDTH)
    render_h = LABEL_H_DOTS + height_adjust

    if orientation == 'portrait':
        if is_landscape:
            img = img.rotate(-90, expand=True)
        target = (render_w, render_h)
    else:
        if not is_landscape:
            img = img.rotate(90, expand=True)
        # Landscape: swap W/H
        target = (render_h, render_w)

    img = ImageOps.fit(img, target, Image.LANCZOS)
    img = img.convert('L')
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    img = _apply_dither(img, dither_mode)

    # For landscape, the bitmap is rotated — keep at render dimensions
    return img.convert('1')


# ---------------------------------------------------------------------------
# ZPL generation
# ---------------------------------------------------------------------------

def image_to_zpl(img1bit: Image.Image,
                 y_offset: int = Y_OFFSET_DOTS) -> str:
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
        "^XA"
        f"^PW{min(w, MAX_PRINT_WIDTH)}"   # print width guard
        "^LH0,0"
        f"^FO0,{fo_y}"
        f"^GFA,{total_bytes},{total_bytes},{bytes_per_row},{hex_data}"
        "^XZ"
    )
    return zpl


# ---------------------------------------------------------------------------
# Calibration test pattern
# ---------------------------------------------------------------------------

def make_test_pattern_zpl(y_offset: int = Y_OFFSET_DOTS,
                           height_adjust: int = HEIGHT_ADJUST_DOTS) -> str:
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
    return image_to_zpl(img1, y_offset=y_offset)


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


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

DARK_BG  = '#1e1e1e'
PANEL_BG = '#2a2a2a'
ACCENT   = '#d4a017'    # warm amber
TEXT     = '#e8e8e8'
SUBTEXT  = '#888888'
ENTRY_BG = '#333333'


class ZebraPrintApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Zebra ZT410 Label Printer")
        self.geometry("900x740")
        self.minsize(800, 640)
        self.configure(bg=DARK_BG)
        self.resizable(True, True)

        # State
        self.folder       = tk.StringVar()
        self.images: list[str] = []
        self.dither_mode  = tk.StringVar(value='Bayer 8x8')
        self.quantity     = tk.IntVar(value=1)
        self.contrast     = tk.DoubleVar(value=1.4)
        self.printer_var  = tk.StringVar()
        self.status_var   = tk.StringVar(value="Select a folder to begin.")
        self.progress_var = tk.DoubleVar(value=0)
        self.start_idx    = tk.IntVar(value=1)
        self.end_idx      = tk.IntVar(value=1)
        self.orientation  = tk.StringVar(value='portrait')
        self.tearoff_pause = tk.BooleanVar(value=False)
        self.use_range    = tk.BooleanVar(value=False)
        # Alignment controls (mirrors module-level config defaults)
        self.y_offset_var      = tk.IntVar(value=Y_OFFSET_DOTS)
        self.height_adjust_var = tk.IntVar(value=HEIGHT_ADJUST_DOTS)

        self._cancel_flag   = threading.Event()
        self._tearoff_event = threading.Event()
        self._print_thread: threading.Thread | None = None

        self._style()
        self._build_ui()
        self._refresh_printers()

    # ------------------------------------------------------------------
    def _style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TFrame',       background=DARK_BG)
        style.configure('Panel.TFrame', background=PANEL_BG)
        style.configure('TLabel',       background=DARK_BG, foreground=TEXT,    font=('Segoe UI', 10))
        style.configure('Sub.TLabel',   background=DARK_BG, foreground=SUBTEXT, font=('Segoe UI', 9))
        style.configure('Panel.TLabel', background=PANEL_BG, foreground=TEXT,   font=('Segoe UI', 10))
        style.configure('Head.TLabel',  background=DARK_BG, foreground=ACCENT,  font=('Segoe UI', 11, 'bold'))
        style.configure('TButton',      background='#444444', foreground=TEXT,
                        font=('Segoe UI', 10), borderwidth=0, focuscolor='none')
        style.map('TButton',
                  background=[('active', '#555555'), ('pressed', '#333333')])
        style.configure('Accent.TButton', background=ACCENT, foreground='#1e1e1e',
                        font=('Segoe UI', 10, 'bold'), borderwidth=0)
        style.map('Accent.TButton',
                  background=[('active', '#e8b420'), ('pressed', '#b88c10')])
        style.configure('TCombobox',    fieldbackground=ENTRY_BG, background=ENTRY_BG,
                        foreground=TEXT, selectbackground=ENTRY_BG,
                        selectforeground=TEXT, arrowcolor=TEXT)
        style.configure('TScale',       background=DARK_BG, troughcolor=ENTRY_BG, sliderthickness=14)
        style.configure('TProgressbar', background=ACCENT,  troughcolor=ENTRY_BG,
                        borderwidth=0, thickness=8)
        style.configure('TSpinbox',     fieldbackground=ENTRY_BG, background=ENTRY_BG,
                        foreground=TEXT, arrowcolor=TEXT)
        style.configure('TCheckbutton', background=DARK_BG, foreground=TEXT, font=('Segoe UI', 10))
        style.configure('TRadiobutton', background=DARK_BG, foreground=TEXT, font=('Segoe UI', 10))

    # ------------------------------------------------------------------
    def _build_ui(self):
        # ── Header ──────────────────────────────────────────────────────
        hdr = ttk.Frame(self)
        hdr.pack(fill='x', padx=16, pady=(14, 4))
        ttk.Label(hdr, text="Zebra ZT410 Label Printer", style='Head.TLabel').pack(side='left')

        # ── Main columns ────────────────────────────────────────────────
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True, padx=16, pady=4)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left  = ttk.Frame(body)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky='nsew')

        # ── Folder picker ───────────────────────────────────────────────
        fp = ttk.Frame(left)
        fp.pack(fill='x', pady=(0, 6))
        ttk.Label(fp, text="Image Folder").pack(anchor='w')
        row = ttk.Frame(fp)
        row.pack(fill='x')
        ttk.Entry(row, textvariable=self.folder, state='readonly',
                  font=('Segoe UI', 9)).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text="Browse…", command=self._browse).pack(side='left', padx=(6, 0))

        # ── Image list ──────────────────────────────────────────────────
        ttk.Label(left, text="Images in folder").pack(anchor='w', pady=(4, 0))
        listframe = ttk.Frame(left)
        listframe.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(listframe, orient='vertical')
        self.listbox = tk.Listbox(
            listframe,
            bg=ENTRY_BG, fg=TEXT, selectbackground=ACCENT,
            selectforeground='#1e1e1e', font=('Segoe UI', 9),
            relief='flat', borderwidth=0,
            yscrollcommand=scrollbar.set,
            activestyle='none',
        )
        scrollbar.config(command=self.listbox.yview)
        scrollbar.pack(side='right', fill='y')
        self.listbox.pack(fill='both', expand=True)

        # ── Range selectors ─────────────────────────────────────────────
        rng = ttk.Frame(left)
        rng.pack(fill='x', pady=(6, 0))
        ttk.Checkbutton(rng, text="Print range:", variable=self.use_range,
                        command=self._toggle_range).pack(side='left')
        self.start_spin = ttk.Spinbox(rng, textvariable=self.start_idx,
                                      from_=1, to=9999, width=6, font=('Segoe UI', 9))
        self.start_spin.pack(side='left', padx=(4, 6))
        ttk.Label(rng, text="to").pack(side='left')
        self.end_spin = ttk.Spinbox(rng, textvariable=self.end_idx,
                                    from_=1, to=9999, width=6, font=('Segoe UI', 9))
        self.end_spin.pack(side='left', padx=(4, 0))
        self.range_hint = ttk.Label(rng, text="  (inclusive)", style='Sub.TLabel')
        self.range_hint.pack(side='left')
        self._toggle_range()   # start with spinners disabled

        # ── RIGHT panel ─────────────────────────────────────────────────

        # Printer selector
        ttk.Label(right, text="Printer").pack(anchor='w')
        prow = ttk.Frame(right)
        prow.pack(fill='x', pady=(0, 8))
        self.printer_cb = ttk.Combobox(prow, textvariable=self.printer_var,
                                        state='readonly', font=('Segoe UI', 9))
        self.printer_cb.pack(side='left', fill='x', expand=True)
        ttk.Button(prow, text="↻", width=3, command=self._refresh_printers).pack(side='left', padx=(4, 0))

        # Dither mode
        ttk.Label(right, text="Dither Mode").pack(anchor='w')
        self.dither_cb = ttk.Combobox(right, textvariable=self.dither_mode,
                                       values=DITHER_MODES, state='readonly',
                                       font=('Segoe UI', 9))
        self.dither_cb.pack(fill='x', pady=(0, 8))

        # Contrast slider
        ttk.Label(right, text="Contrast").pack(anchor='w')
        cframe = ttk.Frame(right)
        cframe.pack(fill='x', pady=(0, 8))
        self.contrast_slider = ttk.Scale(cframe, variable=self.contrast,
                                          from_=0.5, to=3.0, orient='horizontal',
                                          command=self._update_contrast_label)
        self.contrast_slider.pack(side='left', fill='x', expand=True)
        self.contrast_label = ttk.Label(cframe, text=f"{self.contrast.get():.2f}", width=5)
        self.contrast_label.pack(side='left', padx=(6, 0))

        # Orientation
        ttk.Label(right, text="Orientation").pack(anchor='w')
        oframe = ttk.Frame(right)
        oframe.pack(fill='x', pady=(0, 8))
        ttk.Radiobutton(oframe, text="Portrait",  variable=self.orientation,
                        value='portrait').pack(side='left', padx=(0, 12))
        ttk.Radiobutton(oframe, text="Landscape", variable=self.orientation,
                        value='landscape').pack(side='left')

        # Quantity
        ttk.Label(right, text="Copies per image").pack(anchor='w')
        ttk.Spinbox(right, textvariable=self.quantity, from_=1, to=99,
                    width=6, font=('Segoe UI', 9)).pack(anchor='w', pady=(0, 8))

        # ── Alignment section ────────────────────────────────────────────
        sep = ttk.Frame(right, height=1)
        sep.pack(fill='x', pady=(2, 6))
        sep.configure(style='TFrame')

        ttk.Label(right, text="Alignment", style='Head.TLabel').pack(anchor='w', pady=(0, 4))

        arow1 = ttk.Frame(right)
        arow1.pack(fill='x', pady=(0, 4))
        ttk.Label(arow1, text="Y offset (dots)", width=18).pack(side='left')
        ttk.Spinbox(arow1, textvariable=self.y_offset_var,
                    from_=-200, to=200, width=7, font=('Segoe UI', 9)).pack(side='left')
        ttk.Label(arow1, text=f"  ({mm_to_dots(1)} dots = 1 mm)", style='Sub.TLabel').pack(side='left')

        arow2 = ttk.Frame(right)
        arow2.pack(fill='x', pady=(0, 8))
        ttk.Label(arow2, text="Height adjust (dots)", width=18).pack(side='left')
        ttk.Spinbox(arow2, textvariable=self.height_adjust_var,
                    from_=-200, to=200, width=7, font=('Segoe UI', 9)).pack(side='left')

        # Tear-off pause
        ttk.Checkbutton(right, text="Pause after each label (tear-off)",
                        variable=self.tearoff_pause).pack(anchor='w', pady=(0, 8))

        # Buttons
        ttk.Button(right, text="Preview Selected",
                   command=self._preview).pack(fill='x', pady=(0, 4))
        ttk.Button(right, text="🔲  Print Test Pattern",
                   command=self._print_test_pattern).pack(fill='x', pady=(0, 4))
        self.print_btn = ttk.Button(right, text="▶  Print Labels",
                                     style='Accent.TButton', command=self._start_print)
        self.print_btn.pack(fill='x', pady=(0, 4))
        self.cancel_btn = ttk.Button(right, text="✕  Cancel",
                                      command=self._cancel, state='disabled')
        self.cancel_btn.pack(fill='x')

        # ── Status / progress ───────────────────────────────────────────
        bot = ttk.Frame(self)
        bot.pack(fill='x', padx=16, pady=(6, 12))
        self.status_lbl = ttk.Label(bot, textvariable=self.status_var, style='Sub.TLabel')
        self.status_lbl.pack(anchor='w')
        self.progress_bar = ttk.Progressbar(bot, variable=self.progress_var,
                                             maximum=100, mode='determinate')
        self.progress_bar.pack(fill='x', pady=(4, 0))

    # ------------------------------------------------------------------
    def _toggle_range(self):
        state = 'normal' if self.use_range.get() else 'disabled'
        self.start_spin.config(state=state)
        self.end_spin.config(state=state)

    def _update_contrast_label(self, _=None):
        self.contrast_label.config(text=f"{self.contrast.get():.2f}")

    def _browse(self):
        folder = filedialog.askdirectory(title="Select Image Folder")
        if folder:
            self.folder.set(folder)
            self._load_images(folder)

    def _load_images(self, folder: str):
        exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}
        files = sorted(
            f for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in exts
        )
        self.images = [os.path.join(folder, f) for f in files]
        self.listbox.delete(0, 'end')
        for i, f in enumerate(files, 1):
            self.listbox.insert('end', f"  {i:>4}.  {f}")
        count = len(files)
        self.start_idx.set(1)
        self.end_idx.set(count)
        self.start_spin.config(to=max(1, count))
        self.end_spin.config(to=max(1, count))
        self._set_status(f"Loaded {count} image(s).")

    def _refresh_printers(self):
        printers = get_printers()
        self.printer_cb['values'] = printers
        if printers:
            self.printer_var.set(printers[0])
        else:
            self.printer_var.set('')
            self._set_status("No printers found. Is win32print installed?")

    def _preview(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("Preview", "Select an image from the list first.")
            return
        idx  = sel[0]
        path = self.images[idx]
        try:
            img = prepare_image(path, self.dither_mode.get(), self.contrast.get(),
                                self.orientation.get(),
                                y_offset=self.y_offset_var.get(),
                                height_adjust=self.height_adjust_var.get())
            preview_w = 406
            preview_h = int(preview_w * img.size[1] / img.size[0])
            img.convert('RGB').resize((preview_w, preview_h), Image.NEAREST).show()
        except Exception as e:
            messagebox.showerror("Preview Error", str(e))

    def _print_test_pattern(self):
        printer = self.printer_var.get()
        if not printer:
            messagebox.showwarning("No Printer", "Select a printer.")
            return
        try:
            zpl = make_test_pattern_zpl(
                y_offset=self.y_offset_var.get(),
                height_adjust=self.height_adjust_var.get(),
            )
            send_to_printer(zpl, printer)
            self._set_status("Test pattern sent — measure the white strip, then adjust Y offset / Height adjust.")
        except Exception as e:
            messagebox.showerror("Test Pattern Error", str(e))

    def _start_print(self):
        if not self.images:
            messagebox.showwarning("No Images", "Load a folder first.")
            return
        printer = self.printer_var.get()
        if not printer:
            messagebox.showwarning("No Printer", "Select a printer.")
            return

        if self.use_range.get():
            start = self.start_idx.get() - 1   # 0-based
            end   = self.end_idx.get()   - 1
            if start < 0 or end >= len(self.images) or start > end:
                messagebox.showwarning("Range Error", "Invalid start/end range.")
                return
        else:
            sel = self.listbox.curselection()
            if not sel:
                messagebox.showwarning("No Selection", "Select an image from the list first.")
                return
            start = end = sel[0]

        self._cancel_flag.clear()
        self.print_btn.config(state='disabled')
        self.cancel_btn.config(state='normal')
        self.progress_var.set(0)

        self._print_thread = threading.Thread(
            target=self._print_thread_fn,
            args=(start, end, printer,
                  self.dither_mode.get(), self.contrast.get(), self.quantity.get(),
                  self.orientation.get(), self.tearoff_pause.get(),
                  self.y_offset_var.get(), self.height_adjust_var.get()),
            daemon=True,
        )
        self._print_thread.start()

    def _print_thread_fn(self, start: int, end: int, printer: str,
                          dither: str, contrast: float, qty: int,
                          orientation: str, tearoff_pause: bool,
                          y_offset: int, height_adjust: int):
        subset = self.images[start:end + 1]
        total  = len(subset) * qty
        done   = 0
        for i, path in enumerate(subset):
            if self._cancel_flag.is_set():
                self._set_status("Cancelled.")
                break
            fname = os.path.basename(path)
            self._set_status(f"Processing {i + 1}/{len(subset)}: {fname}")
            try:
                img = prepare_image(path, dither, contrast, orientation,
                                    y_offset=y_offset, height_adjust=height_adjust)
                zpl = image_to_zpl(img, y_offset=y_offset)
            except Exception as e:
                self._set_status(f"Error on {fname}: {e}")
                self.after(0, lambda: self.print_btn.config(state='normal'))
                self.after(0, lambda: self.cancel_btn.config(state='disabled'))
                return
            for q in range(qty):
                if self._cancel_flag.is_set():
                    break
                self._set_status(f"Printing {fname}  (copy {q + 1}/{qty})")
                try:
                    send_to_printer(zpl, printer)
                except Exception as e:
                    self._set_status(f"Print error: {e}")
                    self.after(0, lambda: self.print_btn.config(state='normal'))
                    self.after(0, lambda: self.cancel_btn.config(state='disabled'))
                    return
                done += 1
                pct = done / total * 100
                self.after(0, lambda p=pct: self.progress_var.set(p))

                if tearoff_pause and (done < total) and not self._cancel_flag.is_set():
                    self._tearoff_event.clear()
                    self._set_status(f"Tear off label {done}/{total} — then click Continue.")
                    self.after(0, self._show_tearoff_dialog)
                    self._tearoff_event.wait()
        else:
            self._set_status(f"Done — printed {done} label(s).")
        self.after(0, lambda: self.print_btn.config(state='normal'))
        self.after(0, lambda: self.cancel_btn.config(state='disabled'))

    def _show_tearoff_dialog(self):
        win = tk.Toplevel(self)
        win.title("Tear Off Label")
        win.configure(bg=DARK_BG)
        win.resizable(False, False)
        win.grab_set()
        win.focus_force()
        ttk.Label(win, text="Tear off the label, then click Continue.",
                  style='TLabel', padding=20).pack()
        def on_continue():
            win.destroy(); self._tearoff_event.set()
        def on_cancel():
            win.destroy(); self._cancel_flag.set(); self._tearoff_event.set()
        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=(0, 16))
        ttk.Button(btn_frame, text="Continue ▶", style='Accent.TButton',
                   command=on_continue).pack(side='left', padx=(10, 6))
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side='left', padx=(6, 10))

    def _cancel(self):
        self._cancel_flag.set()
        self._set_status("Cancelling…")

    def _set_status(self, msg: str):
        self.after(0, lambda: self.status_var.set(msg))


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app = ZebraPrintApp()
    app.mainloop()
