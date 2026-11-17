#!/usr/bin/env bash
# 【Actions 用】下載 Release 資產 db-latest → data/twstock.db，並驗證完整性。保留原始 gz 供 db-prev 輪替。
set -euo pipefail
REPO="${GITHUB_REPOSITORY:-web-twstockai/tw-stock-site}"
OUT="${RUNNER_TEMP:-/tmp}/dbcache"; mkdir -p "$OUT" data
gh release download db-latest -R "$REPO" -p twstock.db.gz -D "$OUT" --clobber
ls -l "$OUT/twstock.db.gz"
gunzip -c "$OUT/twstock.db.gz" > data/twstock.db
python - <<'PY'
import sqlite3
c = sqlite3.connect("data/twstock.db")
assert c.execute("pragma quick_check").fetchone()[0] == "ok", "quick_check 失敗"
d, n = c.execute("select max(price_date), count(*) from daily_prices").fetchone()
runs = c.execute("select count(*) from job_runs").fetchone()[0]
print(f"DB 下載完成：daily_prices {n} 列、最新行情日 {d}、job_runs {runs} 筆")
assert n > 1000 and d, "DB 內容異常（太少）"
PY
cp "$OUT/twstock.db.gz" "$OUT/twstock.db.orig.gz"
