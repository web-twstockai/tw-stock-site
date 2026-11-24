#!/usr/bin/env bash
# 重工作（完整 pytest、匯出）共用鎖：web/.run/heavy.lock。兩種慣例都相容：
# - 別人用 flock：這裡也 flock 同一個檔，會排隊等；
# - 別人用「建立檔案寫 PID、用完刪除」：檔案存在且 PID 還活著就等。
# 拿到鎖後寫入自己的 PID，指令結束（含失敗）刪除檔案。用法：tools/heavy_lock.sh <指令…>
set -uo pipefail
LOCK="$(cd "$(dirname "$0")/.." && pwd)/.run/heavy.lock"
mkdir -p "$(dirname "$LOCK")"
while :; do
  if [ -s "$LOCK" ]; then
    other=$(head -c 32 "$LOCK" | tr -dc '0-9')
    if [ -n "$other" ] && [ "$other" != "$$" ] && kill -0 "$other" 2>/dev/null; then
      echo "[heavy_lock] 等待中：$LOCK 由 PID $other 持有" >&2; sleep 15; continue
    fi
  fi
  exec 9>>"$LOCK"
  if flock -n 9; then break; fi
  exec 9>&-
  echo "[heavy_lock] 等待中：$LOCK 被 flock 持有" >&2; sleep 15
done
echo "$$" > "$LOCK"
echo "[heavy_lock] 取得 $LOCK（PID $$，$(date '+%F %T')）" >&2
"$@"; rc=$?
rm -f "$LOCK"; flock -u 9; exec 9>&-
echo "[heavy_lock] 釋放（$(date '+%F %T')），結束碼 $rc" >&2
exit $rc
