"""四頁 × 電腦/手機 截圖 → web/screenshots/。用法：.venv/bin/python tools/screenshots.py [base_url] [name=/path ...]"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parents[1] / "screenshots"
PAGES = {"home": "/", "screener": "/screener", "stock": "/stock/2330", "status": "/status",
         "screener-zero": "/screener?f=1&enable_trend=1&enable_breakout=1&enable_volume=1&enable_turnover=1&min_turnover_yi=9000",
         "stock-financial": "/stock/2881", "stock-short-history": "/stock/4105",
         "source-row": "/source?table=daily_prices&symbol=2330&price_date=2026-11-09",
         "source-agg": "/source?table=daily_prices&agg=sum%3Aamount_twd&price_date=2026-11-09"}
PAGES["screener-param-error"] = "/screener?f=1&enable_trend=1&enable_breakout=1&enable_volume=1&enable_turnover=1&volume_multiplier=0.5"
if len(sys.argv) > 2:  # 只拍指定頁：name=/path ...（例如另起 sqlite 暫存 DB 的伺服器拍覆蓋不足變體）
    PAGES = dict(a.split("=", 1) for a in sys.argv[2:])
VIEWS = {"desktop": dict(viewport={"width": 1366, "height": 900}),
         "mobile": dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)}

OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for vname, opts in VIEWS.items():
        ctx = b.new_context(locale="zh-TW", timezone_id="Asia/Taipei", **opts)
        page = ctx.new_page()
        for name, path in PAGES.items():
            page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(400)
            f = OUT / f"{name}-{vname}.png"
            page.screenshot(path=str(f), full_page=True)
            print(f)
        ctx.close()
    b.close()
