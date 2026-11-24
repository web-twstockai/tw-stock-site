#!/usr/bin/env bash
# 【Actions 用】匯出靜態站（全市場 v2）→ 自檢 → 範圍關卡 → 標記 github-actions → 移除 .github。
# 全市場切換包（web/pending/fullmarket，2026-11-24）：取代 tools/cloud/build_site.sh。回滾：換回 build_site.sh.orig。
# 個股頁：符合任一篩選條件＋當日成交金額前 300 名（全市場預設；TWSITE_PAGE_EXTRA=none 關閉），其餘標「未收錄個股頁」。
set -euo pipefail
OUT="${1:-build/site}"
mkdir -p build
# 前一日模式快照（今天的變化）：先從公開站下載；失敗不影響建置（2026-11-24 21:51）。已有 TWSITE_MODE_SNAPSHOT 則不覆寫。
if [ -z "${TWSITE_MODE_SNAPSHOT:-}" ]; then
  SNAP="$(bash tools/cloud/fetch_mode_snapshot.sh build/prev-mode-snapshot.json || true)"
  if [ -n "$SNAP" ] && [ -f "$SNAP" ]; then export TWSITE_MODE_SNAPSHOT="$SNAP"; fi
fi
( cd web && TWSITE_UNIVERSE=all python -W ignore tools/peak_mem.py --json ../build/export_mem.json -- \
    python -W ignore tools/export_static.py --mode v2 --pages auto --out "../$OUT" )
( cd web && python -W ignore tools/check_export.py "../$OUT" )
python tools/cloud/scope_guard.py "$OUT" auto
python tools/cloud/stamp_build_info.py "$OUT"
rm -rf "$OUT/.github"
echo "pages=$(find "$OUT" -type f | wc -l) size=$(du -sh "$OUT" | cut -f1)"
echo "export_mem=$(cat build/export_mem.json 2>/dev/null | python -c 'import json,sys;d=json.load(sys.stdin);print(d["peak_pss_mb"],"MB",d["seconds"],"s")' 2>/dev/null || echo 無)"
