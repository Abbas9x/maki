"""
gui.py — Maki V9.2 UI (CustomTkinter, ChatGPT-style dark).

Near-black palette with an emerald accent, inspired by modern chat UIs:
  • Deep #0d0d0d base, layered #171717 / #212121 / #2a2a2a surfaces
  • Emerald (#10a37f) accent — orb, send button, name labels, focus ring
  • User messages: subtle right-aligned bubbles · Maki: clean left cards
  • Glowing status orb (layered rings + breathing core, colored by state)
  • Animated "thinking…" dots, timestamps, smooth autoscroll

Public API (unchanged — main.py depends on these):
  on_send_text / on_ptt_start / on_ptt_stop / on_pause / on_resume / on_quit
  set_status / set_state / set_mode / set_model / set_processing_info
  add_user_message / add_maki_message / add_system_message / add_clarify_message
  set_ptt_recording / run
"""

import math
import threading
import time
import tkinter as tk

import customtkinter as ctk
import config

# ── Theme ─────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ChatGPT-inspired near-black palette + emerald accent
BG          = "#0d0d0d"   # deep near-black — main background
PANEL       = "#171717"   # header + input panel
SURFACE     = "#212121"   # cards / Maki bubble
SURFACE_2   = "#2a2a2a"   # input field + user bubble
BORDER      = "#2f2f2f"
ACCENT      = "#10a37f"   # emerald
ACCENT_DIM  = "#0c7d61"
ACCENT_SOFT = "#3dd6ab"
TEXT        = "#ececec"
TEXT_DIM    = "#9a9a9a"
MUTED       = "#6a6a6a"
GREEN       = "#10a37f"
BLUE        = "#5b9dff"
AMBER       = "#e0a23c"
RED         = "#e5534b"
VIOLET      = "#a78bff"
USER_BUBBLE = "#2a2a2a"
MAKI_BUBBLE = "#1c1c1c"
CLAR_BUBBLE = "#2b2410"
SYS_COLOR   = "#5a5a5a"

# State → (label, color)
_STATES = {
    "idle":      ("Listening",          ACCENT),
    "listening": ("Listening",          ACCENT),
    "thinking":  ("Thinking",           VIOLET),
    "speaking":  ("Speaking",           BLUE),
    "tool":      ("Running tool",       AMBER),
    "clarify":   ("Waiting for you",    AMBER),
    "confirm":   ("Confirm? yes / no",  RED),
    "paused":    ("Microphone paused",  MUTED),
    "error":     ("Error",              RED),
}
_ANIMATED_STATES = {"thinking", "tool", "speaking"}


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _dim(h: str, factor: float) -> str:
    r, g, b = _hex_to_rgb(h)
    return f"#{int(r*factor):02x}{int(g*factor):02x}{int(b*factor):02x}"


class MakiWindow:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("Maki")
        self.root.geometry("700x840")
        self.root.minsize(560, 660)
        self.root.configure(fg_color=BG)

        # Callbacks
        self._on_send_text   = None
        self._on_ptt_start   = None
        self._on_ptt_stop    = None
        self._on_pause       = None
        self._on_resume      = None
        self._on_quit        = None
        # V18 — new callbacks
        self._on_think_toggle = None    # called with (enabled: bool)
        self._on_stop         = None    # called to abort current TTS + drop transcripts

        # State
        self._ptt_active   = False
        self._wake_on      = True
        self._think_on     = False      # V18 — deep reasoning mode toggle
        self._state_name   = "listening"
        self._orb_phase    = 0.0
        self._orb_color    = ACCENT
        self._anim_phase   = 0

        self._build()
        self._bind_keys()
        self._start_orb()
        self._start_label_anim()

    # ── Layout ───────────────────────────────────────────────────────────────
    def _build(self):
        r = self.root

        # ═══ Header ═══════════════════════════════════════════════════════════
        header = ctk.CTkFrame(r, fg_color=PANEL, corner_radius=0, height=78)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left", padx=22, pady=11)

        # Glowing orb
        self._orb = tk.Canvas(left, width=50, height=50, bg=PANEL,
                              bd=0, highlightthickness=0)
        self._orb.pack(side="left", padx=(0, 15))
        self._orb_glow = self._orb.create_oval(2, 2, 48, 48, fill="", outline="", width=0)
        self._orb_ring = self._orb.create_oval(10, 10, 40, 40, fill="", outline="", width=2)
        self._orb_core = self._orb.create_oval(17, 17, 33, 33, fill=ACCENT, outline="", width=0)

        name_col = ctk.CTkFrame(left, fg_color="transparent")
        name_col.pack(side="left")
        ctk.CTkLabel(name_col, text=config.ASSISTANT_NAME,
                     font=("Segoe UI Semibold", 19), text_color=TEXT,
                     anchor="w").pack(anchor="w")
        self._state_lbl = ctk.CTkLabel(name_col, text="Listening",
                                       font=("Segoe UI", 11), text_color=ACCENT,
                                       anchor="w")
        self._state_lbl.pack(anchor="w", pady=(1, 0))

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.pack(side="right", padx=22, pady=11)
        badge_row = ctk.CTkFrame(right, fg_color="transparent")
        badge_row.pack(anchor="e")
        self._mode_badge = ctk.CTkLabel(badge_row, text=" … ",
                                        font=("Segoe UI Semibold", 10),
                                        text_color="#0d0d0d", fg_color=ACCENT,
                                        corner_radius=8, padx=11, pady=3)
        self._mode_badge.pack(side="right")
        self._model_lbl = ctk.CTkLabel(right, text="",
                                       font=("Segoe UI", 9), text_color=TEXT_DIM,
                                       anchor="e")
        self._model_lbl.pack(anchor="e", pady=(5, 0))

        ctk.CTkFrame(r, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # ═══ Chat area ════════════════════════════════════════════════════════
        self._chat = ctk.CTkScrollableFrame(
            r, fg_color=BG, corner_radius=0,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT_DIM,
        )
        self._chat.pack(fill="both", expand=True)
        try:
            self._chat._scrollbar.configure(width=8)
        except Exception:
            pass

        self._add_system_card("Maki is starting up — voice, tools and memory warming up.")

        # ═══ Status strip ═════════════════════════════════════════════════════
        ctk.CTkFrame(r, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")
        strip = ctk.CTkFrame(r, fg_color=PANEL, height=30, corner_radius=0)
        strip.pack(fill="x")
        strip.pack_propagate(False)
        self._info_lbl = ctk.CTkLabel(strip, text="", font=("Segoe UI", 9),
                                      text_color=TEXT_DIM, anchor="w", padx=20)
        self._info_lbl.pack(side="left", fill="x", expand=True)
        self._hint_lbl = ctk.CTkLabel(strip, text="Space = talk    ·    Enter = send",
                                      font=("Segoe UI", 9), text_color=MUTED,
                                      anchor="e", padx=20)
        self._hint_lbl.pack(side="right")

        # ═══ Input row ════════════════════════════════════════════════════════
        ctk.CTkFrame(r, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")
        inp = ctk.CTkFrame(r, fg_color=PANEL, corner_radius=0)
        inp.pack(fill="x")
        inp_inner = ctk.CTkFrame(inp, fg_color="transparent")
        inp_inner.pack(fill="x", padx=18, pady=14)

        self._entry = ctk.CTkEntry(
            inp_inner,
            placeholder_text="Message Maki, or press Space to talk…",
            placeholder_text_color=MUTED,
            fg_color=SURFACE_2, border_color=BORDER, border_width=1,
            text_color=TEXT, font=("Segoe UI", 12),
            height=48, corner_radius=14,
        )
        self._entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self._entry.bind("<Return>", lambda e: self._send())

        self._send_btn = ctk.CTkButton(
            inp_inner, text="↑", font=("Segoe UI Semibold", 16),
            fg_color=ACCENT, hover_color=ACCENT_DIM, text_color="#0d0d0d",
            width=48, height=48, corner_radius=14, command=self._send)
        self._send_btn.pack(side="right")

        # ═══ Control row ══════════════════════════════════════════════════════
        ctrl = ctk.CTkFrame(r, fg_color=BG, corner_radius=0)
        ctrl.pack(fill="x")
        ctrl_inner = ctk.CTkFrame(ctrl, fg_color="transparent")
        ctrl_inner.pack(fill="x", padx=18, pady=(0, 15))

        self._ptt_btn = ctk.CTkButton(
            ctrl_inner, text="  🎤  Hold to Talk  ",
            font=("Segoe UI Semibold", 11),
            fg_color=SURFACE, hover_color=ACCENT_DIM,
            text_color=ACCENT_SOFT, height=40, corner_radius=12,
            border_width=1, border_color=BORDER)
        self._ptt_btn.pack(side="left", padx=(0, 8))
        self._ptt_btn.bind("<ButtonPress-1>",   lambda e: self._ptt_dn())
        self._ptt_btn.bind("<ButtonRelease-1>", lambda e: self._ptt_up())

        self._wake_btn = ctk.CTkButton(
            ctrl_inner, text="  ⏸  Wake  ", font=("Segoe UI Semibold", 11),
            fg_color=SURFACE, hover_color=SURFACE_2, text_color=TEXT_DIM,
            height=40, corner_radius=12, border_width=1, border_color=BORDER,
            command=self._toggle_wake)
        self._wake_btn.pack(side="left", padx=(0, 8))

        # V18 — Think mode toggle (deep reasoning via perception layer)
        self._think_btn = ctk.CTkButton(
            ctrl_inner, text="  🧠  Think  ", font=("Segoe UI Semibold", 11),
            fg_color=SURFACE, hover_color=SURFACE_2, text_color=TEXT_DIM,
            height=40, corner_radius=12, border_width=1, border_color=BORDER,
            command=self._toggle_think)
        self._think_btn.pack(side="left", padx=(0, 8))

        # V18 — Stop button (halts TTS + drops queue)
        self._stop_btn = ctk.CTkButton(
            ctrl_inner, text="  🛑  Stop  ", font=("Segoe UI Semibold", 11),
            fg_color=SURFACE, hover_color="#3a1a1a", text_color=AMBER,
            height=40, corner_radius=12, border_width=1, border_color=BORDER,
            command=self._stop_now)
        self._stop_btn.pack(side="left")

        self._quit_btn = ctk.CTkButton(
            ctrl_inner, text="✕", font=("Segoe UI Semibold", 12),
            fg_color=SURFACE, hover_color="#3a1a1a", text_color=RED,
            width=40, height=40, corner_radius=12,
            border_width=1, border_color=BORDER, command=self._quit)
        self._quit_btn.pack(side="right")

    def _bind_keys(self):
        self.root.bind("<KeyPress-space>",   self._kdn)
        self.root.bind("<KeyRelease-space>", self._kup)

    # ── Orb animation: layered glow + breathing core ─────────────────────────
    def _start_orb(self):
        self.root.after(40, self._tick_orb)

    def _tick_orb(self):
        try:
            self._orb_phase = (self._orb_phase + 0.07) % (2 * math.pi)
            pulse = 0.5 + 0.5 * math.sin(self._orb_phase)
            c = self._orb_color
            cx, cy = 25, 25
            cr = 7 + 3 * pulse
            self._orb.coords(self._orb_core, cx - cr, cy - cr, cx + cr, cy + cr)
            self._orb.itemconfigure(self._orb_core, fill=c)
            self._orb.itemconfigure(self._orb_ring, outline=_dim(c, 0.6))
            gr = 15 + 8 * pulse
            self._orb.coords(self._orb_glow, cx - gr, cy - gr, cx + gr, cy + gr)
            self._orb.itemconfigure(self._orb_glow,
                                    outline=_dim(c, 0.20 + 0.18 * pulse), width=2)
        except Exception:
            pass
        self.root.after(45, self._tick_orb)

    # ── Animated state label ─────────────────────────────────────────────────
    def _start_label_anim(self):
        self.root.after(400, self._tick_label)

    def _tick_label(self):
        try:
            label, color = _STATES.get(self._state_name, (self._state_name, MUTED))
            if self._state_name in _ANIMATED_STATES:
                self._anim_phase = (self._anim_phase + 1) % 4
                self._state_lbl.configure(text=f"{label}{'.' * self._anim_phase}",
                                          text_color=color)
            else:
                self._state_lbl.configure(text=label, text_color=color)
        except Exception:
            pass
        self.root.after(400, self._tick_label)

    # ── State / status setters ───────────────────────────────────────────────
    def set_state(self, name: str, extra: str = ""):
        label, color = _STATES.get(name, (name, MUTED))
        self._state_name = name
        self._orb_color  = color
        shown = f"{label}  {extra}" if extra else label
        if name not in _ANIMATED_STATES:
            self.root.after(0, lambda: self._state_lbl.configure(
                text=shown, text_color=color))

    def set_status(self, t, color=MUTED):
        self.root.after(0, lambda: self._state_lbl.configure(text=t, text_color=color))
        self._orb_color = color

    def set_mode(self, m):
        self.root.after(0, lambda: self._mode_badge.configure(text=f" {m} "))

    def set_model(self, model: str):
        self.root.after(0, lambda: self._model_lbl.configure(text=model))

    def set_processing_info(self, ms: int, tool: str = ""):
        parts = []
        if ms > 0:
            parts.append(f"⏱ {ms} ms")
        if tool and tool not in ("none", ""):
            parts.append(f"🔧 {tool}")
        txt = "       ".join(parts)
        self.root.after(0, lambda: self._info_lbl.configure(text=txt))

    def set_ptt_recording(self, active: bool):
        if active:
            self.root.after(0, lambda: self._ptt_btn.configure(
                text="  🔴  Recording…  ", fg_color="#3a0f0f",
                hover_color="#5a1414", text_color=RED, border_color="#5a1414"))
        else:
            self.root.after(0, lambda: self._ptt_btn.configure(
                text="  🎤  Hold to Talk  ", fg_color=SURFACE,
                hover_color=ACCENT_DIM, text_color=ACCENT_SOFT,
                border_color=BORDER))

    # ── Message rendering ────────────────────────────────────────────────────
    def add_user_message(self, t):
        self.root.after(0, lambda: self._add_bubble(t, role="user"))

    def add_maki_message(self, t):
        self.root.after(0, lambda: self._add_bubble(t, role="maki"))

    def add_system_message(self, t):
        self.root.after(0, lambda: self._add_system_card(t))

    def add_clarify_message(self, t):
        self.root.after(0, lambda: self._add_bubble(f"❓  {t}", role="clarify"))

    def _add_bubble(self, text: str, role: str):
        row = ctk.CTkFrame(self._chat, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(10, 0))

        if role == "user":
            bubble_bg, bubble_fg = USER_BUBBLE, TEXT
            who, who_c, side     = config.USER_NAME, TEXT_DIM, "right"
            ctk.CTkFrame(row, fg_color="transparent", width=80).pack(side="left", fill="y")
        elif role == "clarify":
            bubble_bg, bubble_fg = CLAR_BUBBLE, AMBER
            who, who_c, side     = config.ASSISTANT_NAME, AMBER, "left"
        else:
            bubble_bg, bubble_fg = MAKI_BUBBLE, TEXT
            who, who_c, side     = config.ASSISTANT_NAME, ACCENT_SOFT, "left"

        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side=side, anchor="e" if side == "right" else "w")

        head = ctk.CTkFrame(col, fg_color="transparent")
        head.pack(anchor="e" if side == "right" else "w", padx=12)
        ts = time.strftime("%H:%M")
        if side == "right":
            ctk.CTkLabel(head, text=ts, font=("Segoe UI", 8),
                         text_color=MUTED).pack(side="right", padx=(6, 0))
            ctk.CTkLabel(head, text=who, font=("Segoe UI Semibold", 9),
                         text_color=who_c).pack(side="right")
        else:
            ctk.CTkLabel(head, text=who, font=("Segoe UI Semibold", 9),
                         text_color=who_c).pack(side="left")
            ctk.CTkLabel(head, text=ts, font=("Segoe UI", 8),
                         text_color=MUTED).pack(side="left", padx=(6, 0))

        bubble = ctk.CTkFrame(col, fg_color=bubble_bg, corner_radius=16)
        bubble.pack(anchor="e" if side == "right" else "w", pady=(3, 0))
        ctk.CTkLabel(bubble, text=text, font=("Segoe UI", 12),
                     text_color=bubble_fg, wraplength=450, justify="left",
                     anchor="w").pack(padx=16, pady=12)

        self.root.after(60, self._scroll_bottom)

    def _add_system_card(self, text: str):
        row = ctk.CTkFrame(self._chat, fg_color="transparent")
        row.pack(fill="x", padx=26, pady=7)
        ctk.CTkLabel(row, text=text, font=("Segoe UI", 9, "italic"),
                     text_color=SYS_COLOR, wraplength=580,
                     justify="center", anchor="center").pack(fill="x")
        self.root.after(60, self._scroll_bottom)

    def _scroll_bottom(self):
        try:
            self._chat._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    # ── Actions ──────────────────────────────────────────────────────────────
    def _send(self):
        t = self._entry.get().strip()
        if not t:
            return
        self._entry.delete(0, "end")
        if self._on_send_text:
            threading.Thread(target=self._on_send_text, args=(t,), daemon=True).start()

    def _ptt_dn(self):
        if self._ptt_active:
            return
        self._ptt_active = True
        self.set_ptt_recording(True)
        if self._on_ptt_start:
            threading.Thread(target=self._on_ptt_start, daemon=True).start()

    def _ptt_up(self):
        if not self._ptt_active:
            return
        self._ptt_active = False
        if self._on_ptt_stop:
            self._on_ptt_stop()

    def _toggle_wake(self):
        self._wake_on = not self._wake_on
        if self._wake_on:
            self._wake_btn.configure(text="  ⏸  Wake  ", text_color=TEXT_DIM)
            if self._on_resume:
                self._on_resume()
        else:
            self._wake_btn.configure(text="  ▶  Wake  ", text_color=AMBER)
            if self._on_pause:
                self._on_pause()

    # V18 — Think mode (deep reasoning toggle)
    def _toggle_think(self):
        self._think_on = not self._think_on
        if self._think_on:
            self._think_btn.configure(text="  🧠  Thinking  ",
                                       text_color=VIOLET,
                                       border_color=VIOLET)
        else:
            self._think_btn.configure(text="  🧠  Think  ",
                                       text_color=TEXT_DIM,
                                       border_color=BORDER)
        if self._on_think_toggle:
            threading.Thread(target=self._on_think_toggle,
                             args=(self._think_on,), daemon=True).start()

    def set_think(self, enabled: bool):
        """Allow main.py to flip the toggle externally (e.g. voice trigger)."""
        if self._think_on != enabled:
            self._think_on = enabled
            if enabled:
                self._think_btn.configure(text="  🧠  Thinking  ",
                                           text_color=VIOLET,
                                           border_color=VIOLET)
            else:
                self._think_btn.configure(text="  🧠  Think  ",
                                           text_color=TEXT_DIM,
                                           border_color=BORDER)

    # V18 — Stop now (halt TTS + drop queue)
    def _stop_now(self):
        if self._on_stop:
            threading.Thread(target=self._on_stop, daemon=True).start()

    def _kdn(self, e):
        if self.root.focus_get() is self._entry:
            return
        if not self._ptt_active:
            self._ptt_dn()

    def _kup(self, e):
        if self.root.focus_get() is self._entry:
            return
        if self._ptt_active:
            self._ptt_up()

    def _quit(self):
        if self._on_quit:
            self._on_quit()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── Wire callbacks ───────────────────────────────────────────────────────
    def on_send_text(self, fn): self._on_send_text = fn
    def on_ptt_start(self, fn): self._on_ptt_start = fn
    def on_ptt_stop(self,  fn): self._on_ptt_stop  = fn
    def on_pause(self,     fn): self._on_pause     = fn
    def on_resume(self,    fn): self._on_resume    = fn
    def on_quit(self,      fn): self._on_quit      = fn
    # V18 — new callbacks
    def on_think_toggle(self, fn): self._on_think_toggle = fn
    def on_stop(self,         fn): self._on_stop         = fn

    def run(self):
        self.root.mainloop()
