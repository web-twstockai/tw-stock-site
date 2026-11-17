#!/usr/bin/env bash
# 【電腦上執行】把雲端的最新資料庫（Release 資產 db-latest）換成本機 data/twstock.db，讓 8765 與其他 Bot 看到最新資料。
# 步驟：拒絕在 pipeline.run 執行時進行 → 備份本機 DB（backup API）到 data/backup/（只留最近 2 份）→ 下載 db-latest
#       → gunzip → quick_check＋最新行情日檢查 → 原子換上（同一目錄 mv）→ 刪掉舊的 -wal／-shm。
# 用法：bash tools/cloud/pull_db.sh [--force-older]   （預設拒絕換成「行情日比本機舊」的資料庫）
# 只用 REST API＋curl；權杖只從環境變數 GITHUB_TOKEN 讀（不印出、不寫檔）。公開 repo，下載資產本來就不需權杖，權杖只用來避免被限流。
set -euo pipefail
ROOT="/workspace/tw-stock-site"; cd "$ROOT"
REPO="web-twstockai/tw-stock-site"
say(){ echo "[pull_db $(TZ=Asia/Taipei date '+%F %T')] $*"; }
if pgrep -f "pipeline.run" >/dev/null; then say "有 pipeline.run 在執行，拒絕換 DB"; exit 3; fi
exec 9>"$ROOT/data/.pull_db.lock"; flock -n 9 || { say "另一個 pull_db 在執行"; exit 3; }
mkdir -p data/backup; TMP="$(mktemp -d "$ROOT/data/.pull_XXXXXX")"; trap 'rm -rf "$TMP"' EXIT
AUTH=(); [ -n "${GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN")
META=$(curl -fsS "${AUTH[@]}" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/releases/tags/db-latest")
URL=$(echo "$META" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin)['assets'] if x['name']=='twstock.db.gz'];print(a[0]['url'] if a else '')")
[ -n "$URL" ] || { say "找不到 db-latest 的 twstock.db.gz"; exit 1; }
# 1) 備份本機 DB
BK="data/backup/twstock_$(TZ=Asia/Taipei date +%Y%m%d_%H%M%S).db"
.venv/bin/python - "$BK" <<'PY'
import sqlite3, sys
s = sqlite3.connect("file:data/twstock.db?mode=ro", uri=True); d = sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()
PY
gzip -f "$BK"; say "本機備份：$BK.gz（$(du -h "$BK.gz" | cut -f1)）"
ls -1t data/backup/twstock_*.db.gz 2>/dev/null | tail -n +3 | xargs -r rm -f
# 2) 下載＋驗證
curl -fsSL "${AUTH[@]}" -H "Accept: application/octet-stream" -o "$TMP/twstock.db.gz" "$URL"
gunzip -c "$TMP/twstock.db.gz" > "$TMP/twstock.db"
NEW=$(.venv/bin/python - "$TMP/twstock.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("pragma quick_check").fetchone()[0] == "ok", "quick_check 失敗"
d, n = c.execute("select max(price_date), count(*) from daily_prices").fetchone()
assert d and n > 1000, "內容異常"
print(d, n)
PY
)
OLD=$(.venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/twstock.db?mode=ro',uri=True);r=c.execute('select max(price_date),count(*) from daily_prices').fetchone();print(r[0],r[1])")
say "本機：$OLD；雲端：$NEW"
if [ "${1:-}" != "--force-older" ] && [[ "${NEW%% *}" < "${OLD%% *}" ]]; then say "雲端行情日比本機舊，拒絕（要強制請加 --force-older）"; exit 4; fi
# 3) 原子換上（先放同目錄再 mv；舊的 -wal／-shm 一併移除，避免套到新檔）
chmod --reference=data/twstock.db "$TMP/twstock.db" 2>/dev/null || true
mv -f "$TMP/twstock.db" data/twstock.db.new && mv -f data/twstock.db.new data/twstock.db
rm -f data/twstock.db-wal data/twstock.db-shm
say "完成：data/twstock.db 已換成雲端版（行情日／列數 $NEW）"
