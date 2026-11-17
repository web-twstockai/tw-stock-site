#!/usr/bin/env bash
# 【Actions 用】pipeline 成功／partial 後：快照 DB → 檢查不比原本差 → db-prev 放原本那份 → 覆蓋 db-latest。
set -euo pipefail
REPO="${GITHUB_REPOSITORY:-web-twstockai/tw-stock-site}"
CACHE="${RUNNER_TEMP:-/tmp}/dbcache"; NEW="${RUNNER_TEMP:-/tmp}/dbnew"; mkdir -p "$NEW"
python - "$NEW/twstock.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect("data/twstock.db"); dst = sqlite3.connect(sys.argv[1]); src.backup(dst); dst.close(); src.close()
new = sqlite3.connect(sys.argv[1])
assert new.execute("pragma integrity_check").fetchone()[0] == "ok", "新 DB integrity_check 失敗"
nd, nn = new.execute("select max(price_date), count(*) from daily_prices").fetchone()
import gzip, shutil, tempfile, os
old_path = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "dbcache", "twstock.db.orig.gz")
with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as t, gzip.open(old_path) as g:
    shutil.copyfileobj(g, t)
old = sqlite3.connect(t.name)
od, on = old.execute("select max(price_date), count(*) from daily_prices").fetchone()
print(f"原本：{od}／{on} 列；新：{nd}／{nn} 列")
assert nd >= od and nn >= on, "新 DB 比原本少資料，不上傳"
PY
gzip -6 -c "$NEW/twstock.db" > "$NEW/twstock.db.gz"
DD=$(python -c "import sqlite3;print(sqlite3.connect('$NEW/twstock.db').execute('select max(price_date) from daily_prices').fetchone()[0])")
gh release view db-prev -R "$REPO" >/dev/null 2>&1 || gh release create db-prev -R "$REPO" --prerelease --target "${GITHUB_SHA:-main}" --title "db-prev（上一份資料庫）" --notes "上一次成功更新前的資料庫"
mkdir -p "$NEW/prev" && cp "$CACHE/twstock.db.orig.gz" "$NEW/prev/twstock.db.gz"
gh release upload db-prev -R "$REPO" "$NEW/prev/twstock.db.gz" --clobber
gh release upload db-latest -R "$REPO" "$NEW/twstock.db.gz" --clobber
gh release edit db-latest -R "$REPO" --notes "台股網站 SQLite 資料庫快照（gzip）。每次 update.yml 成功後覆蓋；上一份在 db-prev。資料日期 $DD；更新於 $(TZ=Asia/Taipei date '+%F %T')（台北）run ${GITHUB_RUN_ID:-local}。" >/dev/null
echo "db-latest 已更新：$(du -h "$NEW/twstock.db.gz" | cut -f1)，資料日期 $DD"
