#!/usr/bin/env bash
# 【Actions 用，build_site.sh 呼叫】下載公開站的前一日模式快照（data/mode-snapshot.json），供「今天的變化」比對。
# 用法：fetch_mode_snapshot.sh <輸出檔>；成功（HTTP 200 且是 JSON 物件）→ stdout 印出輸出檔絕對路徑。
# 任何失敗（逾時、404、非 JSON）→ 不印路徑、刪掉暫存檔，一律 exit 0：絕不讓建置失敗。
# 網址可用 MODE_SNAPSHOT_FETCH_URL 覆寫（測試用）；逾時 10 秒。
set -u
DEST="${1:-build/prev-mode-snapshot.json}"
URL="${MODE_SNAPSHOT_FETCH_URL:-https://web-twstockai.github.io/tw-stock-site/data/mode-snapshot.json}"
mkdir -p "$(dirname "$DEST")" 2>/dev/null || exit 0
TMP="$DEST.tmp"
rm -f "$TMP"
if curl -fsS --max-time 10 -o "$TMP" "$URL" 2>/dev/null \
   && python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); sys.exit(0 if isinstance(d,dict) else 1)' "$TMP" 2>/dev/null \
   && mv -f "$TMP" "$DEST" 2>/dev/null; then
  echo "$(cd "$(dirname "$DEST")" && pwd)/$(basename "$DEST")"
  echo "前一日模式快照：已下載（$(wc -c < "$DEST") bytes）" >&2
else
  rm -f "$TMP"
  echo "前一日模式快照：未取得（不影響建置；匯出端會改找本機快照或重算）" >&2
fi
exit 0
