# Zebra ZT410 Label Printer — Project Context

## What this is
A Python GUI app (`zebra_print.py`) that prints photos and art (JPG/PNG) as 4×6 labels on a
Zebra ZT410 industrial printer via USB, without going through Adobe Lightroom. Uses ZPL
(Zebra Programming Language) sent as raw bytes.

## Hardware
- **Printer:** Zebra ZT410, 203 DPI, peeler model. Currently thermal transfer with an ink ribbon;
  direct thermal may be used again, so both must keep working
- **Connection:** USB to Windows PC
- **Media:** 4×6 inch die-cut labels on a liner with gaps (Gap/Notch sensor, Transmissive)
- **Max print width:** 832 dots at 203 DPI
- **Roll core:** 1-inch ID (use Zebra adapter G32432M for 3-inch cores)

## Label dimensions
- Width:  812 dots  (4.0" × 203.2 = 812.8, rounded down to stay on label)
- Height: 1219 dots (6.0" × 203.2 = 1219.2)
- These are defined as `LABEL_W_DOTS` and `LABEL_H_DOTS` at the top of `zebra_print.py`

## Unit helpers
- 1 mm = 8 dots at 203 DPI
- `mm_to_dots(mm)` and `inches_to_dots(inches)` helpers are in the script

## ZPL approach
- Build a 1-bit bitmap of the full label using PIL
- Invert bytes (PIL 1=white, ZPL 1=black dot)
- Send via `^GFA` (graphic field) hex command
- `^PW812` guards against printing wider than the label
- `^FO0,{y_offset}` positions the image; negative offsets crop top rows instead
- Never send `^LT` — it conflicts with the Label Top value saved on the printer
- Printer is on Gap/Notch media; `^LL` not needed (printer measures label length during calibration)
- `~SD` (absolute darkness), `^PR` (speed) and `^MT` (T ribbon, D direct) are sent only when
  chosen. They stay in effect on the printer until changed or it restarts, so a run using
  "Printer setting" first sends `^XA^JUR^XZ` (load last saved) if the app changed anything
  (`_prepare_printer`). Darkness always starts on "Printer setting". Avoid `^MD`: it is relative. Guide values: darkness 0–30, speed 2–6 in/s
- Copies go as one job with `^PQ`, except when pausing or waiting between labels or when the
  text has `{n}`

## Printing method
Uses `win32print` to send RAW ZPL through the Windows spooler. `pyserial` is not an option:
the ZT410 appears as a USB printer on `USB002`, not as a COM port. Jobs sometimes get stuck,
so the "Clear stuck jobs" button deletes all queued jobs for every printer on the PC and
restarts the spooler. It needs admin rights, so Windows shows a permission prompt.

Status is read straight from the USB device (`find_printer_device`, `usb_query`), no spooler
and no admin rights. Replies can trail a query, so `usb_query` reads until the reply is complete
and fields are matched by name. Sending print data the same way would bypass the spooler; that
is possible but not built.

## Dithering pipeline
Images are processed in this order:
1. Open as RGB, apply the EXIF orientation tag, turn landscape images 90° to fit the label,
   rotate 180° if Flip is on
2. `ImageOps.fit()` to target size — fills label edge-to-edge, no bars or margins
   - Target height = `LABEL_H_DOTS + HEIGHT_ADJUST_DOTS` (for bleed correction)
   - `crop` picks which part survives; the Flip rotation happens before this step so dragging
     in the preview always matches the direction on screen
3. Convert to grayscale (L mode)
4. Apply contrast (default 1.4), brightness (gamma), sharpen (unsharp mask)
5. Dither to 1-bit
6. `add_text()` draws any text onto the bitmap, upright with the photo, so it stays sharp

Dither modes available:
- **Bayer 8×8** (default) — ordered halftone, good for photos
- **Bayer 4×4** — coarser halftone
- **Floyd-Steinberg** — error diffusion, sharp edges
- **Atkinson** — error diffusion, slightly lighter than FS
- **Threshold** — pure black/white cutoff at 127

## Alignment config (top of zebra_print.py)
```python
Y_OFFSET_DOTS    = 0    # shift image down (+) or up (-); 8 dots = 1 mm
HEIGHT_ADJUST_DOTS = 14 # make image taller (+) or shorter (-) than label height
```
These are first-launch defaults; after that the saved settings win. Use the "Test pattern"
button to calibrate. It prints a 1-dot border at exact label edges + 1 mm tick marks so you
can measure the white strip and set the right values.

## GUI features
Three panels: image list, preview, settings (tabs: Image, Output, Text, Align).
- **Choose images:** a file picker filtered to JPEG/PNG, or drag files or a folder onto the window.
  Any file loads its whole folder; one is highlighted, several are also ticked.
- **Image list:** thumbnails, plus a checkbox per image that decides what prints. Clicking a name
  only previews it. Shift-click a checkbox for a range, Space toggles the highlighted row.
- **Preview:** live render of the highlighted image. Drag to adjust the crop (kept per image for
  the session), double-click to recentre.
- **Image tab:** presets, dither, Contrast / Brightness / Sharpen sliders, Flip 180°. Sliders snap
  to steps, follow the mouse wheel, and double-click resets them. They always start at their
  defaults (contrast 1.4, the slider's centre), so they are deliberately not saved settings.
- **Presets:** save dither, sliders, darkness, speed and media under a name
- **Output tab:** printer, media, darkness, speed, Clear stuck jobs, copies, wait between labels, pause after
  each label. The wait is added to the estimated print time, because sending returns before the
  label is out. It helps when back-to-back labels streak on ribbon.
- **Text tab:** text on the label with `{n}`, `{total}`, `{name}`, `{date}`. The text itself is not
  saved between launches, so it can't end up on labels by accident.
- **Align tab:** Y offset, Height adjust, test pattern
- **History / Reprint last:** every run is logged with its settings; `history.jsonl` sits beside
  the settings file. History can reprint a run or copy its settings back into the app.
- Status bar shows live printer state (Ready, Printhead open, Out of labels, Out of ribbon…)

Other settings, last folder, and window size are saved to `%APPDATA%\ZebraPrint\settings.json`.

Background threads must not touch Tk directly. They queue updates with `_call_ui()`.

## Dependencies
```
pip install pillow pywin32 numpy tkinterdnd2
```

## Known issues / planned work
- [ ] PyInstaller build (`pyinstaller --onefile --windowed zebra_print.py`) to make a standalone EXE,
      once the UI settles

## File structure
```
LABEL PRINTER TOOLS/
├── zebra_print.py   # main app — all logic and GUI in one file
├── dev.py           # development launcher, restarts the app on save
└── CLAUDE.md        # this file
```

## Running
```
python dev.py           # while developing: restarts the app on every save
python zebra_print.py   # normal use
```
A save restarts the app immediately under `dev.py`, even in the middle of a print run.
