"""
screenshot_tools.py — V7.5 screenshot / snipping / clipboard image tools.

Free Python stack:
  • PIL.ImageGrab for full-screen capture + clipboard image reading
  • pyautogui as a backup capture path
  • win32clipboard for putting images onto the Windows clipboard
  • Windows shell URIs (ms-screenclip:) to invoke the native Snipping overlay

Public API used by brain.py:
  take_screenshot(copy=False)        -> {'path', 'copied'} or {'error'}
  take_screenshot_to_clipboard()     -> {'path', 'copied': True} or {'error'}
  open_snipping_tool()               -> {'result': str}
  snip_area_manual()                 -> {'result': str}
  save_clipboard_image()             -> {'path'} or {'error'}
  copy_last_screenshot()             -> {'path', 'copied': True} or {'error'}
  open_screenshot_folder()           -> {'result': str}
  get_last_screenshot()              -> str|None
"""

from __future__ import annotations
import datetime, io, logging, os, subprocess, threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Where screenshots go ──────────────────────────────────────────────────────
SCREENSHOT_DIR = Path(os.path.expanduser("~")) / "Pictures" / "MakiScreenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ── State (process-local) ─────────────────────────────────────────────────────
_last_path: str | None = None
_last_time: float | None = None
_last_clip_save: str | None = None
_state_lock = threading.Lock()


def get_last_screenshot() -> str | None:
    with _state_lock:
        return _last_path


def _set_last(path: str):
    global _last_path, _last_time
    import time as _t
    with _state_lock:
        _last_path = str(path)
        _last_time = _t.time()


# ── Image dependency probes ──────────────────────────────────────────────────
def _import_pil():
    try:
        from PIL import ImageGrab
        return ImageGrab
    except Exception as e:
        logger.error("Pillow ImageGrab unavailable: %s", e)
        return None


def _import_pyautogui():
    try:
        import pyautogui
        return pyautogui
    except Exception as e:
        logger.error("pyautogui unavailable: %s", e)
        return None


# ── Clipboard helpers (win32clipboard) ───────────────────────────────────────
def _copy_image_to_clipboard(pil_img) -> bool:
    """Put a PIL image on the Windows clipboard as CF_DIB."""
    try:
        import win32clipboard
    except ImportError:
        return False
    try:
        # DIB = BMP without the 14-byte BMP header
        buf = io.BytesIO()
        pil_img.convert("RGB").save(buf, "BMP")
        data = buf.getvalue()[14:]
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        logger.error("Clipboard copy failed: %s", e)
        return False


# ── Capture ──────────────────────────────────────────────────────────────────
def _capture_image():
    """Try PIL first, fall back to pyautogui. Returns PIL.Image or None."""
    ImageGrab = _import_pil()
    if ImageGrab is not None:
        try:
            img = ImageGrab.grab(all_screens=True)
            return img
        except Exception as e:
            logger.warning("ImageGrab.grab failed: %s — trying pyautogui", e)
    pyautogui = _import_pyautogui()
    if pyautogui is not None:
        try:
            return pyautogui.screenshot()
        except Exception as e:
            logger.error("pyautogui.screenshot failed: %s", e)
    return None


def _stamped_filename() -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return SCREENSHOT_DIR / f"maki_screenshot_{stamp}.png"


def take_screenshot(copy: bool = False) -> dict:
    """Full-screen capture, saved to disk. Optionally also copied to clipboard."""
    img = _capture_image()
    if img is None:
        return {"error": "Couldn't capture the screen. Pillow/pyautogui may be missing."}
    path = _stamped_filename()
    try:
        img.save(path, "PNG")
    except Exception as e:
        return {"error": f"Saved capture but couldn't write file: {e}"}
    _set_last(path)
    copied = False
    if copy:
        copied = _copy_image_to_clipboard(img)
    return {"path": str(path), "copied": copied}


def take_screenshot_to_clipboard() -> dict:
    """Capture + save + copy to clipboard in one shot."""
    return take_screenshot(copy=True)


# ── Snipping Tool / overlay ──────────────────────────────────────────────────
def open_snipping_tool() -> dict:
    """
    Open Windows snipping overlay. Tries the modern shell URI first
    (ms-screenclip:) which gives the rectangular selection bar at top.
    Falls back to legacy SnippingTool.exe.
    """
    tried = []
    # 1. Modern overlay (Win+Shift+S equivalent)
    try:
        subprocess.Popen(
            ["explorer.exe", "ms-screenclip:"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"result": "Opening the snipping overlay. Select the area you want."}
    except Exception as e:
        tried.append(f"ms-screenclip: {e}")
    # 2. Legacy SnippingTool.exe
    for exe in ("SnippingTool.exe", "snippingtool"):
        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"result": "Opening Snipping Tool."}
        except Exception as e:
            tried.append(f"{exe}: {e}")
    return {"error": "Couldn't open the snipping tool. " + " | ".join(tried)}


def snip_area_manual() -> dict:
    """Alias — same as open_snipping_tool but with a slightly different prompt."""
    res = open_snipping_tool()
    if "result" in res:
        res["result"] = ("Opening the snipping overlay. After you select an area "
                         "it will be copied to your clipboard.")
    return res


# ── Clipboard → file ─────────────────────────────────────────────────────────
def save_clipboard_image() -> dict:
    """If the clipboard contains an image (e.g. from snipping), save it to disk."""
    ImageGrab = _import_pil()
    if ImageGrab is None:
        return {"error": "Pillow isn't installed — can't read the clipboard image."}
    try:
        img = ImageGrab.grabclipboard()
    except Exception as e:
        return {"error": f"Couldn't read the clipboard: {e}"}
    if img is None:
        return {"error": "I don't see an image in the clipboard."}
    # ImageGrab may return a list (e.g. file paths) — guard for that
    if isinstance(img, list):
        # try first path
        try:
            from PIL import Image
            img = Image.open(img[0])
        except Exception:
            return {"error": "Clipboard has file paths, not a snipped image."}
    path = _stamped_filename()
    try:
        img.save(path, "PNG")
    except Exception as e:
        return {"error": f"Couldn't save the snip: {e}"}
    global _last_clip_save
    with _state_lock:
        _last_clip_save = str(path)
    _set_last(path)
    return {"path": str(path)}


def copy_last_screenshot() -> dict:
    """Re-copy the last saved screenshot to the clipboard."""
    last = get_last_screenshot()
    if not last or not os.path.exists(last):
        return {"error": "I don't have a recent screenshot to copy."}
    try:
        from PIL import Image
        img = Image.open(last)
    except Exception as e:
        return {"error": f"Couldn't read the saved screenshot: {e}"}
    ok = _copy_image_to_clipboard(img)
    if not ok:
        return {"error": "Couldn't put the image on the clipboard."}
    return {"path": last, "copied": True}


# ── Folder ───────────────────────────────────────────────────────────────────
def open_screenshot_folder() -> dict:
    try:
        subprocess.Popen(["explorer", str(SCREENSHOT_DIR)])
        return {"result": f"Opening {SCREENSHOT_DIR}."}
    except Exception as e:
        return {"error": str(e)}
