"""範圍關卡（建議版，取代 tools/cloud/scope_guard.py；由負責人套用）：
首頁 上漲＋下跌＋平盤(其餘) 與行情檔數必須 ≤ 實際追蹤範圍檔數（build-info.json 的 tracking_universe）；否則結束碼 1（不部署）。
用法：python tools/cloud/scope_guard.py <匯出目錄> [上限=auto]
  上限 auto（預設）＝追蹤範圍檔數（sample30 為 30、全市場約 1,950）；給數字時另外再套這個上限（相容舊用法 `… 30`）。"""
import json, re, sys
from pathlib import Path

out = Path(sys.argv[1]); arg = sys.argv[2] if len(sys.argv) > 2 else "auto"
html = (out / "index.html").read_text("utf-8")
m = re.search(r'aria-label="上漲 ([\d,]+) 檔、下跌 ([\d,]+) 檔、其餘 (-?[\d,]+) 檔，共 ([\d,]+) 檔有行情"', html)
if not m:
    print("範圍關卡失敗：首頁找不到漲跌比例條（上漲／下跌／其餘），無法確認範圍；不放行"); sys.exit(1)
up, down, flat, rows = (int(x.replace(",", "")) for x in m.groups())
info = json.loads((out / "build-info.json").read_text("utf-8"))
n = len(info.get("tracking_universe") or [])
limit = n if arg == "auto" else min(n, int(arg))
print(f"範圍關卡：上漲 {up}＋下跌 {down}＋平盤 {flat}＝{up+down+flat}（行情檔數 {rows}；"
      f"追蹤範圍 {n}（{info.get('universe_label', '')}）；上限 {limit}）")
bad = [x for x in (up + down + flat, rows) if x > limit or x < 0]
if bad or n == 0 or (arg != "auto" and n > int(arg)):
    print("範圍關卡失敗：超過上限或範圍異常；不部署"); sys.exit(1)
print("範圍關卡通過")
