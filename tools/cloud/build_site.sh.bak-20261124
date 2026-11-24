#!/usr/bin/env bash
# 【Actions 用】匯出靜態站（v1，與公開站現行版相同）→ 自檢 → 範圍關卡 → 標記 github-actions → 移除 .github。
set -euo pipefail
OUT="${1:-build/site}"
( cd web && TWSITE_UNIVERSE=sample30 python -W ignore tools/export_static.py --mode v1 --out "../$OUT" )
( cd web && python -W ignore tools/check_export.py "../$OUT" )
python tools/cloud/scope_guard.py "$OUT" 30
python tools/cloud/stamp_build_info.py "$OUT"
rm -rf "$OUT/.github"
echo "pages=$(find "$OUT" -type f | wc -l) size=$(du -sh "$OUT" | cut -f1)"
