"""V13 test suite — vision + screen control + UI tree."""
import os, sys, time, traceback

# Ensure .env is loaded so Tavily etc. work
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "—", detail[:120])

print("=" * 60)
print("V13 TEST SUITE — Vision + Screen Control + UI Tree")
print("=" * 60)

# ── 1. Module imports ─────────────────────────────────────────────────────
print("\n[1] IMPORTS")
try:
    import vision_tools
    check("import vision_tools", True, "")
except Exception as e:
    check("import vision_tools", False, str(e))

try:
    import screen_control
    check("import screen_control", True, "")
except Exception as e:
    check("import screen_control", False, str(e))

try:
    import ui_tree
    check("import ui_tree", True, "")
except Exception as e:
    check("import ui_tree", False, str(e))

try:
    import agent
    check("import agent (with V13 tools)", True, f"{len(agent._PY_TOOLS)} tools registered")
    expected = {"look_at_screen","describe_screen","read_text_on_screen",
                "scroll","type_text","press_keys","click_at","click_text",
                "browser_action","go_to_url","invoke_ui_element"}
    have = {fn.__name__ for fn in agent._PY_TOOLS}
    missing = expected - have
    check("all 11 V13 tools registered", not missing, f"missing={missing}")
except Exception as e:
    check("import agent", False, str(e))
    traceback.print_exc()

# ── 2. screen_control basics (no actual mouse/keyboard movement) ──────────
print("\n[2] SCREEN_CONTROL")
try:
    sw, sh = screen_control.get_screen_size()
    check("get_screen_size", sw > 0 and sh > 0, f"{sw}x{sh}")
    cx, cy = screen_control.get_cursor_position()
    check("get_cursor_position", cx >= 0 and cy >= 0, f"cursor at ({cx},{cy})")
except Exception as e:
    check("screen_control basic ops", False, str(e))

# Bad direction → graceful error
r = screen_control.scroll("sideways", 5)
check("scroll(invalid direction) handled", "don't know" in r.lower(), r)

# Off-screen click → guarded
r = screen_control.click_at(99999, 99999)
check("click_at off-screen guarded", "off-screen" in r.lower(), r)

# Bad coords
r = screen_control.click_at("foo", "bar")
check("click_at invalid coords guarded", "invalid" in r.lower(), r)

# press_keys parsing
import inspect
src = inspect.getsource(screen_control.press_keys)
check("press_keys uses _KEY_ALIASES", "_KEY_ALIASES" in src, "")

# ── 3. Vision tools — capture path (without making a real Ollama call) ────
print("\n[3] VISION_TOOLS (capture only, no model call)")
try:
    img = vision_tools._capture_full_screen()
    check("vision: capture full screen", img is not None, f"size={getattr(img,'size',None)}")
    b64 = vision_tools._capture_b64()
    check("vision: capture+downscale+b64", isinstance(b64, str) and len(b64) > 1000,
          f"b64 length={len(b64) if b64 else 0}")
    status = vision_tools.vision_provider_status()
    # V13: qwen2.5vl. V13.1+: qwen3-vl. Either is fine.
    model = status.get("ollama_model") or ""
    check("vision: provider_status", "vl" in model.lower(), str(status))
except Exception as e:
    check("vision capture", False, str(e))
    traceback.print_exc()

# ── 4. UI tree (just verify it doesn't crash; results depend on focus) ────
print("\n[4] UI_TREE")
try:
    summary = ui_tree.foreground_app_summary()
    check("ui_tree: foreground_app_summary", isinstance(summary, str) and summary, summary[:120])
    elems = ui_tree.list_focusable_elements(limit=10)
    check("ui_tree: list_focusable_elements", isinstance(elems, list),
          f"{len(elems)} elements found in foreground")
except Exception as e:
    check("ui_tree", False, str(e))

# ── 5. agent system prompt has V13 instructions ───────────────────────────
print("\n[5] AGENT SYSTEM PROMPT")
try:
    sp = agent.AGENT_SYSTEM
    check("system prompt has SEEING THE SCREEN", "SEEING THE SCREEN" in sp, "")
    check("system prompt has ACTING ON THE SCREEN", "ACTING ON THE SCREEN" in sp, "")
    check("system prompt has anti-stale-year rule", "stale year" in sp.lower() or "DO NOT append a year" in sp, "")
    check("system prompt has anti-fake-action rule", "NEVER lie" in sp or "never lie" in sp.lower(), "")
    check("system prompt has TODAY'S DATE", "TODAY'S DATE" in sp, "")
except Exception as e:
    check("system prompt checks", False, str(e))

# ── 6. Vision LIVE call (only if Ollama is up; may take ~15s on cold start) ─
print("\n[6] VISION LIVE CALL (Ollama qwen2.5vl)")
try:
    import requests
    # Quick health
    r = requests.get("http://localhost:11434/api/tags", timeout=4)
    # V15.x: model renamed qwen2.5vl → qwen3-vl
    has_vlm = "qwen3-vl" in r.text or "qwen2.5vl" in r.text
    check("Ollama up + vision model pulled", r.status_code == 200 and has_vlm,
          f"http={r.status_code}, has_vl={has_vlm}")
    if has_vlm:
        print("    (calling vision model — may take ~15s on cold load)…")
        t0 = time.time()
        out = vision_tools.look_at_screen("In ONE short sentence: what kind of app is in focus?")
        dt = time.time() - t0
        check("vision: live look_at_screen", isinstance(out, str) and len(out) > 5 and "couldn't" not in out.lower(),
              f"({dt:.1f}s) reply: {out[:140]}")
except Exception as e:
    check("vision live call", False, str(e))

# ── Summary ───────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL:
    print(f"  FAIL: {n} — {d[:140]}")
sys.exit(0 if not FAIL else 1)
