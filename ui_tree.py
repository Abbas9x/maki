"""
ui_tree.py — V13 Maki UI Awareness via Windows UIAutomation.

Where vision_tools "looks" at pixels, ui_tree READS the structured
accessibility tree of the foreground window. This is more reliable for
known-good apps (Chrome, VSCode, Discord, Office, File Explorer) because:
  - Element names + roles are exact, not OCR'd
  - Bounding boxes come straight from the OS, not estimated from a screenshot
  - Works even if the window is partially covered

Public API:
  foreground_app_summary()       -> str   (short voice-friendly summary)
  list_focusable_elements(limit) -> list[dict]
  invoke_element_by_name(name)   -> str   (smart click via UIA Invoke pattern)
"""

from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    _UIA_OK = True
except Exception as e:
    logger.warning("uiautomation unavailable: %s", e)
    auto = None
    _UIA_OK = False


# ── Helpers ─────────────────────────────────────────────────────────────────
_INTERESTING_TYPES = {
    "ButtonControl", "EditControl", "HyperlinkControl", "ListItemControl",
    "MenuItemControl", "TabItemControl", "ComboBoxControl", "CheckBoxControl",
    "RadioButtonControl", "TreeItemControl", "DocumentControl",
}


def _walk(elem, depth=0, max_depth=4, out=None):
    """Walk the UIA tree breadth-limited; collect interesting elements."""
    if out is None: out = []
    if depth > max_depth: return out
    try:
        children = elem.GetChildren()
    except Exception:
        return out
    for c in children:
        try:
            ctype = c.ControlTypeName
            name  = (c.Name or "").strip()
            if ctype in _INTERESTING_TYPES and name:
                rect = c.BoundingRectangle
                out.append({
                    "name":  name[:80],
                    "type":  ctype.replace("Control", ""),
                    "bbox":  (rect.left, rect.top, rect.right, rect.bottom)
                              if rect else None,
                    "automation_id": (c.AutomationId or "")[:40],
                })
            _walk(c, depth + 1, max_depth, out)
        except Exception:
            continue
    return out


def foreground_app_summary() -> str:
    """One-line summary: app name + window title + count of clickable items."""
    if not _UIA_OK:
        return "UI tree reader isn't available (uiautomation not installed)."
    try:
        win = auto.GetForegroundControl()
        if not win:
            return "No foreground window detected."
        # Walk up to the top-level window for a clean title
        top = win
        while top and top.ControlTypeName != "WindowControl":
            try: top = top.GetParentControl()
            except Exception: break
        title = ""
        try: title = (top.Name if top else win.Name) or ""
        except Exception: pass

        elements = list_focusable_elements(limit=200)
        return (f"Foreground: {title or '(unknown)'} — "
                f"{len(elements)} interactive elements detected.")
    except Exception as e:
        return f"Couldn't read UI tree: {e}"


def list_focusable_elements(limit: int = 50) -> list[dict]:
    """Return up to `limit` interactive elements in the foreground window."""
    if not _UIA_OK:
        return []
    try:
        win = auto.GetForegroundControl()
        if not win: return []
        # Walk from the top-level window to capture menus/toolbars too
        top = win
        for _ in range(8):
            try:
                p = top.GetParentControl()
                if not p or p.ControlTypeName == "PaneControl":
                    break
                top = p
            except Exception:
                break
        items = _walk(top, max_depth=5)
        # de-dup by (name, type, bbox)
        seen, out = set(), []
        for it in items:
            k = (it["name"], it["type"], it["bbox"])
            if k in seen: continue
            seen.add(k); out.append(it)
            if len(out) >= limit: break
        return out
    except Exception as e:
        logger.info("UIA list failed: %s", e)
        return []


def invoke_element_by_name(name: str) -> str:
    """
    Find an element by display name in the foreground window and invoke it
    (UIA InvokePattern is more reliable than coord-clicking for buttons/links).
    Falls back to clicking the bbox center if Invoke isn't supported.
    """
    if not _UIA_OK:
        return "UI tree reader isn't available."
    if not name:
        return "Tell me which element to invoke."
    try:
        win = auto.GetForegroundControl()
        if not win: return "No foreground window."
        # Search by name (case-insensitive substring) breadth-first
        target = None
        stack = [win]
        for _ in range(2000):                       # safety cap
            if not stack: break
            cur = stack.pop(0)
            try:
                cname = (cur.Name or "").strip()
                if name.lower() in cname.lower() and cur.ControlTypeName in _INTERESTING_TYPES:
                    target = cur
                    break
                stack.extend(cur.GetChildren())
            except Exception:
                continue
        if target is None:
            return f"Couldn't find a UI element named '{name}'."

        # Try InvokePattern (buttons, links)
        try:
            ip = target.GetInvokePattern()
            if ip:
                ip.Invoke()
                logger.info("UIA invoked '%s' (%s)", target.Name, target.ControlTypeName)
                return f"Invoked '{target.Name}'."
        except Exception:
            pass
        # Fall back to coordinate click
        try:
            r = target.BoundingRectangle
            if r and r.right > r.left and r.bottom > r.top:
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
                import screen_control
                return screen_control.click_at(cx, cy)
        except Exception:
            pass
        return f"Found '{target.Name}' but couldn't invoke or click it."
    except Exception as e:
        return f"UI invoke failed: {e}"
