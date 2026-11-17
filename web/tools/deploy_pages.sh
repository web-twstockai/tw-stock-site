#!/usr/bin/env bash
# 【手動備援專用】正式更新與部署由 GitHub Actions（update.yml）負責；本腳本只在 Actions 不可用時手動使用。
# 公開靜態版部署：匯出（正式 SQLite 快照）→ commit → push 到 GitHub Pages（web-twstockai/tw-stock-site main 分支根目錄）。
# 用法：bash /workspace/tw-stock-site/web/tools/deploy_pages.sh [--no-verify]
#   - 冪等：內容沒變就不 commit、不 push（記錄「無變更」並以 0 結束）。
#   - 需在 pipeline 跑完之後呼叫；同一時間只允許一個部署（flock）。
#   - 需要環境變數 GITHUB_TOKEN（classic，repo 權限）；權杖不寫入檔案、remote URL 或 log。
#   - 失敗以非 0 結束，原因寫在 logs/deploy_pages.log。push 失敗最多重試 3 次。
set -uo pipefail
ROOT="/workspace/tw-stock-site"
WEB="$ROOT/web"
LOG="$ROOT/logs/deploy_pages.log"
OUT="$ROOT/build/site"
REPO_DIR="$ROOT/build/pages-repo"
OWNER="web-twstockai"; REPO="tw-stock-site"; BRANCH="main"
PUBLIC_URL="https://$OWNER.github.io/$REPO/"
REMOTE="https://github.com/$OWNER/$REPO.git"
VERIFY=1; [ "${1:-}" = "--no-verify" ] && VERIFY=0
mkdir -p "$ROOT/logs" "$ROOT/build"
export TZ=Asia/Taipei

log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')（台北）] $*" | tee -a "$LOG"; }
fail(){ log "失敗：$*"; log "===== 部署結束（失敗） ====="; exit 1; }
# 發布紀錄（web/logs/pages_last_deploy.json；資料狀態頁顯示「公開站資料日期」）。寫紀錄失敗不影響部署結果
record(){ ( cd "$WEB" && .venv/bin/python -W ignore tools/pages_record.py --out "${RECORD_OUT-$OUT}" --status "$1" --reason "${2:-}" --commit "${3:-}" ) >>"$LOG" 2>&1 || log "（發布紀錄寫入失敗）"; }

exec 9>"$ROOT/build/.deploy_pages.lock"
flock -n 9 || { log "失敗：已有另一個部署在執行（lock），本次不執行"; exit 3; }

log "===== 部署開始 ====="
[ -n "${GITHUB_TOKEN:-}" ] || fail "環境變數 GITHUB_TOKEN 未設定（需 classic token，repo 權限）"
export GIT_ASKPASS="$WEB/tools/git_askpass.sh" GIT_TERMINAL_PROMPT=0
API(){ curl -sS -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" "$@"; }

# 1) 匯出（模式：環境變數 TWSITE_EXPORT_MODE 優先，其次 web/export_mode 檔；預設 v1＝現行版）
MODE="${TWSITE_EXPORT_MODE:-$(cat "$WEB/export_mode" 2>/dev/null | tr -d '[:space:]')}"
case "$MODE" in v2) ;; *) MODE=v1 ;; esac
log "匯出靜態頁（模式 $MODE；TestClient，不開連接埠、不動 8765）"
if ! ( cd "$WEB" && TWSITE_UNIVERSE="${TWSITE_UNIVERSE:-sample30}" .venv/bin/python -W ignore tools/export_static.py --mode "$MODE" --out "$OUT" ) >>"$LOG" 2>&1; then
  RECORD_OUT= record export_failed "匯出失敗或匯出內建自檢未通過（見 logs/deploy_pages.log）；build/site 維持上一版"
  fail "匯出失敗（見上方輸出）；公開網站維持上一版"
fi
[ -f "$OUT/index.html" ] && [ -f "$OUT/.nojekyll" ] || fail "匯出結果缺 index.html 或 .nojekyll"
# 推送前關卡：首頁行情檔數 ≤ 追蹤範圍檔數、搜尋清單每檔都有個股頁（tools/check_export.py；不符就不推送）
if ! ( cd "$WEB" && .venv/bin/python -W ignore tools/check_export.py "$OUT" ) >>"$LOG" 2>&1; then
  record check_failed "$(cd "$WEB" && .venv/bin/python -W ignore tools/check_export.py "$OUT" 2>&1 | tr '\n' ' ' | cut -c1-400)"
  fail "匯出自檢失敗（見上方輸出）；不推送，公開網站維持上一版"
fi
DATA_DATE=$(python3 -c "import json;print(json.load(open('$OUT/build-info.json'))['data_date'])" 2>/dev/null || echo "?")
GEN=$(python3 -c "import json;print(json.load(open('$OUT/build-info.json'))['generated_at_taipei'])" 2>/dev/null || echo "?")

# 2) 準備部署用工作目錄（remote URL 不含權杖）
if [ ! -d "$REPO_DIR/.git" ]; then
  rm -rf "$REPO_DIR"; mkdir -p "$REPO_DIR"
  git -C "$REPO_DIR" init -q -b "$BRANCH" || fail "git init 失敗"
  git -C "$REPO_DIR" remote add origin "$REMOTE"
fi
git -C "$REPO_DIR" config user.name "tw-stock-site deploy bot"
git -C "$REPO_DIR" config user.email "$OWNER@users.noreply.github.com"
git -C "$REPO_DIR" remote set-url origin "$REMOTE"
if git -C "$REPO_DIR" ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  git -C "$REPO_DIR" fetch -q origin "$BRANCH" >>"$LOG" 2>&1 || fail "git fetch 失敗"
  git -C "$REPO_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH" >>"$LOG" 2>&1 || fail "checkout 失敗"
  git -C "$REPO_DIR" reset -q --hard "origin/$BRANCH"   # 只重設本機部署工作目錄，與遠端一致
fi

# 3) 換上新內容
find "$REPO_DIR" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$OUT/." "$REPO_DIR/" || fail "複製匯出檔失敗"
git -C "$REPO_DIR" add -A
# build-info.json 與 manifest.json、每頁頁首都含「產生時間」；只有時間變動不算內容變更
CHANGED=$(git -C "$REPO_DIR" diff --cached -U0 | grep -E '^[+-][^+-]' | grep -v -E '本頁產生於|generated_at' | wc -l)
# v2 的 .json.gz 是二進位檔，上面的文字 diff 看不到；有任何二進位檔變動也算內容變更（gzip 固定 mtime，內容不變則位元組不變）
BIN=$(git -C "$REPO_DIR" diff --cached --numstat | awk '$1=="-"' | wc -l)
CHANGED=$((CHANGED + BIN))
if git -C "$REPO_DIR" rev-parse -q --verify HEAD >/dev/null && [ "$CHANGED" -eq 0 ]; then
  git -C "$REPO_DIR" reset -q --hard HEAD
  log "無變更（資料日期 $DATA_DATE），不 commit、不 push"
  record no_change "內容與公開站相同"
  log "===== 部署結束（成功，無變更） ====="
  exit 0
fi
git -C "$REPO_DIR" commit -q -m "盤後資料更新：資料日期 $DATA_DATE（產生於 $GEN 台北）" || fail "git commit 失敗"

# 4) push（最多重試 3 次）
ok=0
for i in 1 2 3 4; do
  if git -C "$REPO_DIR" push -q origin "$BRANCH" >>"$LOG" 2>&1; then ok=1; break; fi
  [ $i -le 3 ] && { log "push 失敗，第 $i 次重試（30 秒後）"; sleep 30; }
done
[ $ok -eq 1 ] || { record push_failed "git push 失敗（已重試 3 次）"; fail "git push 失敗（已重試 3 次）"; }
SHA=$(git -C "$REPO_DIR" rev-parse --short HEAD)
record ok "" "$SHA"
log "已 push $SHA（資料日期 $DATA_DATE）"

# 5) 確認 Pages 已啟用，且由 repo 內的 .github/workflows/pages.yml 部署（build_type=workflow）
code=$(API -o /tmp/.pages_info.json -w "%{http_code}" "https://api.github.com/repos/$OWNER/$REPO/pages")
if [ "$code" = "404" ]; then
  code=$(API -o /dev/null -w "%{http_code}" -X POST "https://api.github.com/repos/$OWNER/$REPO/pages" -d '{"build_type":"workflow"}')
  log "啟用 GitHub Pages：HTTP $code"
  [ "$code" = "201" ] || fail "啟用 GitHub Pages 失敗（HTTP $code）"
elif ! grep -q '"build_type": *"workflow"' /tmp/.pages_info.json; then
  code=$(API -o /dev/null -w "%{http_code}" -X PUT "https://api.github.com/repos/$OWNER/$REPO/pages" -d '{"build_type":"workflow"}')
  log "GitHub Pages 改為 workflow 部署：HTTP $code"
fi
rm -f /tmp/.pages_info.json

# 5b) pages.yml 已無 push 觸發（避免推 main 時意外以舊靜態檔覆蓋 update.yml 的部署），改用 workflow_dispatch 手動觸發
code=$(API -o /dev/null -w "%{http_code}" -X POST "https://api.github.com/repos/$OWNER/$REPO/actions/workflows/pages.yml/dispatches" -d "{\"ref\":\"$BRANCH\"}")
log "觸發 pages.yml（workflow_dispatch）：HTTP $code"
[ "$code" = "204" ] || fail "觸發 pages.yml 失敗（HTTP $code）；已 push 但尚未部署"

# 6) 驗證公開網址已換成這一版（最多等 25 分鐘）
if [ $VERIFY -eq 1 ]; then
  for _ in $(seq 1 100); do
    live=$(curl -sS -L --max-time 20 "${PUBLIC_URL}build-info.json?t=$(date +%s)" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['generated_at_taipei'])" 2>/dev/null || true)
    if [ "$live" = "$GEN" ]; then
      for p in "" screener/ revenue/ status/ manifest.json; do
        c=$(curl -sS -o /dev/null -w "%{http_code}" -L --max-time 20 "$PUBLIC_URL$p")
        [ "$c" = "200" ] || fail "公開網址 $PUBLIC_URL$p 回 HTTP $c"
      done
      log "公開網址已更新並驗證 200：$PUBLIC_URL（產生於 $GEN）"
      log "===== 部署結束（成功） ====="
      exit 0
    fi
    sleep 15
  done
  fail "已 push，但 25 分鐘內公開網址仍未換成新版（目前 live=${live:-讀不到}）；GitHub Pages 可能延遲，請看 repo 的 Actions/Pages 狀態"
fi
log "===== 部署結束（成功，未驗證公開網址） ====="
