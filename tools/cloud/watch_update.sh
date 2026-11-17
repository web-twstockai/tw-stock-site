#!/usr/bin/env bash
# 【監看 routine 用，只讀】檢查雲端更新：最近 5 次 update.yml run、目前是否有 run 在跑、公開站資料是否過期。
# 結束碼：0＝正常；1＝最近一次已完成的 run 失敗，或公開站資料落後；2＝無法查詢。只用 REST API＋curl；權杖只讀環境變數 GITHUB_TOKEN。
# 落後規則：Asia/Taipei 現在時間 ≥ 18:30 的交易日，公開站 data_date 應 = 今天；否則應 = 前一個交易日（build-info.json 的 trade_days）。
set -uo pipefail
REPO="web-twstockai/tw-stock-site"; PUB="https://web-twstockai.github.io/tw-stock-site"
export TZ=Asia/Taipei
H=(); [ -n "${GITHUB_TOKEN:-}" ] && H=(-H "Authorization: Bearer $GITHUB_TOKEN")
RUNS=$(curl -fsS "${H[@]}" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/actions/workflows/update.yml/runs?per_page=5") || { echo "查不到 runs"; exit 2; }
BI=$(curl -fsS "$PUB/build-info.json?t=$(date +%s)") || { echo "讀不到公開站 build-info.json"; exit 2; }
python3 - "$RUNS" "$BI" <<'PY'
import sys, json, datetime as dt
from zoneinfo import ZoneInfo
runs = json.loads(sys.argv[1])["workflow_runs"]; bi = json.loads(sys.argv[2])
tz = ZoneInfo("Asia/Taipei"); now = dt.datetime.now(tz)
def loc(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz).strftime("%m-%d %H:%M")
print("最近 update.yml runs（台北時間）：")
for r in runs:
    print(f"  {r['id']} {r['event']:<17} {r['status']:<11} {str(r['conclusion']):<9} 開始 {loc(r['created_at'])} 更新 {loc(r['updated_at'])} {r['html_url']}")
bad = 0
done = [r for r in runs if r["status"] == "completed"]
if done and done[0]["conclusion"] != "success":
    print(f"!! 最近一次已完成的 run {done[0]['id']} 結果是 {done[0]['conclusion']}"); bad = 1
running = [r for r in runs if r["status"] != "completed"]
if running: print(f"（目前有 {len(running)} 個 run 進行中，最早開始 {loc(running[-1]['created_at'])}）")
days = sorted(bi.get("trade_days") or []); today = now.strftime("%Y-%m-%d")
past = [d for d in days if d < today] ; expected = None
if days:
    expected = today if (today in days and now.strftime("%H:%M") >= "18:30") else (past[-1] if past else None)
dd = bi.get("data_date")
print(f"公開站：data_date={dd}，generated_at={bi.get('generated_at_taipei')}，source={bi.get('source')}；現在 {now:%F %H:%M}，應有資料日期={expected}")
if expected and dd and dd < expected:
    if running: print("（資料落後，但有 run 進行中，先不判定）")
    else: print("!! 公開站資料落後"); bad = 1
if bi.get("source") != "github-actions": print("!! 公開站不是由 github-actions 產生（可能被電腦端部署覆蓋）"); bad = 1
sys.exit(bad)
PY
