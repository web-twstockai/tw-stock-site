#!/usr/bin/env bash
# 【電腦上執行】把雲端更新需要的程式碼同步到 repo 的 `src` 分支（孤立分支，與 main 的靜態檔分開）。
# 內容：pipeline/、analysis/、web/app、web/tools、web/requirements*.txt、tools/cloud、.github/workflows/update.yml（參考副本）。
# 不含：資料庫、logs、build、測試截圖、web/tests、docs、.venv、任何權杖。冪等：沒變更就不 commit。
# 程式改動後要再跑一次，雲端才會用新版（在切換成「repo 為唯一來源」之前，電腦上的專案目錄是來源）。
set -euo pipefail
ROOT="/workspace/tw-stock-site"; cd "$ROOT"
OWNER="web-twstockai"; REPO="tw-stock-site"; BR="src"
W="$ROOT/build/src-repo"; mkdir -p "$ROOT/build"
export GIT_ASKPASS="$ROOT/web/tools/git_askpass.sh" GIT_TERMINAL_PROMPT=0
[ -n "${GITHUB_TOKEN:-}" ] || { echo "GITHUB_TOKEN 未設定" >&2; exit 1; }
if [ ! -d "$W/.git" ]; then
  rm -rf "$W"; mkdir -p "$W"; git -C "$W" init -q -b "$BR"; git -C "$W" remote add origin "https://github.com/$OWNER/$REPO.git"
fi
git -C "$W" config user.name "tw-stock-site deploy bot"; git -C "$W" config user.email "$OWNER@users.noreply.github.com"
if git -C "$W" ls-remote --exit-code --heads origin "$BR" >/dev/null 2>&1; then
  git -C "$W" fetch -q origin "$BR"; git -C "$W" checkout -q -B "$BR" "origin/$BR"; git -C "$W" reset -q --hard "origin/$BR"
fi
find "$W" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
EXC=(--exclude=__pycache__ --exclude='*.pyc' --exclude=.venv --exclude=.run --exclude=logs)
mkdir -p "$W/web" "$W/tools" "$W/.github/workflows"
# rsync 不一定有，用 tar 複製
tar "${EXC[@]}" -cf - pipeline analysis tools/cloud | tar -xf - -C "$W"
tar "${EXC[@]}" -cf - -C web app tools | tar -xf - -C "$W/web" || true
cp web/requirements.txt web/requirements-dev.txt "$W/web/"
rm -f "$W/tools/cloud/seed_db.sh"   # 只在電腦上用
cp web/tools/static_assets/update.yml "$W/.github/workflows/update.yml"
cat > "$W/README.md" <<'MD'
# src 分支：雲端更新用的程式碼

此分支只放 GitHub Actions 雲端更新（`update.yml`，位於 main 分支）要用的程式碼；main 分支放自動產生的靜態網站。
資料庫不在 repo 內，放在 Release 資產 `db-latest`（上一份 `db-prev`）。內容全部來自證交所、櫃買中心、公開資訊觀測站。
由電腦上的 `tools/cloud/sync_src.sh` 同步；請勿直接在此分支手改。僅供研究參考，不構成投資建議。
MD
printf '__pycache__/\n*.pyc\n.venv/\ndata/\nlogs/\nbuild/\n' > "$W/.gitignore"
git -C "$W" add -A
if git -C "$W" rev-parse -q --verify HEAD >/dev/null && git -C "$W" diff --cached --quiet; then echo "src 分支無變更"; exit 0; fi
git -C "$W" commit -q -m "同步雲端更新程式碼（$(TZ=Asia/Taipei date '+%F %T') 台北）"
git -C "$W" push -q origin "$BR"
echo "src 分支已推送：$(git -C "$W" rev-parse --short HEAD)，$(git -C "$W" ls-files | wc -l) 個檔案"
