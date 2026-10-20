"""Проверка контейнера: Chromium и OCR должны работать в Linux."""
import os
import sys
import time

out = open("/tmp/container_check.txt", "w", encoding="utf-8")


def P(*a):
    print(*a, file=out, flush=True)


P("uid =", os.geteuid(), " (0 = root)")
P("PLAYWRIGHT_BROWSERS_PATH =", os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
P("zoneinfo:")
try:
    from zoneinfo import ZoneInfo
    P(" ", ZoneInfo("Asia/Almaty"))
except Exception as e:
    P(" FAIL", e)

# --- Chromium ---
t0 = time.time()
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True,
                              args=["--disable-blink-features=AutomationControlled"]
                              + (["--no-sandbox"] if hasattr(os, "geteuid")
                                 and os.geteuid() == 0 else []))
        page = b.new_page()
        page.set_content("<h1>Астана</h1>")
        txt = page.inner_text("h1")
        b.close()
    P("Chromium: OK, заголовок =", repr(txt), f"({time.time()-t0:.1f} c)")
except Exception as e:
    P("Chromium: FAIL", type(e).__name__, e)

# --- ddddocr ---
t0 = time.time()
try:
    import ddddocr
    ocr = ddddocr.DdddOcr(beta=True)
    P("ddddocr: OK, модуль загружен", f"({time.time()-t0:.1f} c)")
except Exception as e:
    P("ddddocr: FAIL", type(e).__name__, e)

# --- память процесса ---
try:
    with open("/proc/self/status", encoding="utf-8") as f:
        for line in f:
            if line.startswith("VmRSS"):
                P("RSS процесса:", line.strip())
except Exception as e:
    P("память: FAIL", e)

out.close()
print(open("/tmp/container_check.txt", encoding="utf-8").read())
