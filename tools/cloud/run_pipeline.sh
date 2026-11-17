#!/usr/bin/env bash
# 【Actions 用】執行 pipeline，最多 3 次嘗試，整體時間預算內。用法：run_pipeline.sh daily|evening
# 結束碼 0＝success 或 partial（與 pipeline.run 一致）；非 0＝失敗。結果寫入 $GITHUB_OUTPUT：attempts、rc、status。
set -uo pipefail
JOB="${1:?daily|evening}"
case "$JOB" in daily) BUDGET_MIN=${BUDGET_MIN:-60};; evening) BUDGET_MIN=${BUDGET_MIN:-170};; *) echo "job 只能是 daily 或 evening" >&2; exit 2;; esac
export TWSITE_UNIVERSE=sample30 PYTHONUNBUFFERED=1
START=$(date +%s); DEADLINE=$((START + BUDGET_MIN*60)); rc=1; attempts=0
for i in 1 2 3; do
  now=$(date +%s); left=$((DEADLINE - now))
  if [ $left -lt 120 ]; then echo "剩餘時間不足（${left}s），不再嘗試"; break; fi
  attempts=$i
  echo "=== pipeline 第 $i/3 次嘗試（job=$JOB，剩餘預算 $((left/60)) 分）$(date -u +%FT%TZ) ==="
  # 超時先送 SIGINT（pipeline 會把 job 標 interrupted 再結束），60 秒後強制結束
  timeout -s INT -k 60 "${left}s" python -m pipeline.run --job "$JOB" --symbols sample30
  rc=$?
  echo "=== 第 $i 次結束 rc=$rc ==="
  [ $rc -eq 0 ] && break
  [ $i -lt 3 ] && { echo "等 60 秒後重試"; sleep 60; }
done
STATUS=$(python - <<'PY'
import sqlite3
c = sqlite3.connect("data/twstock.db")
r = c.execute("select status from job_runs order by run_id desc limit 1").fetchone()
print(r[0] if r else "unknown")
PY
)
echo "最後一次 job_runs.status=$STATUS rc=$rc attempts=$attempts 用時 $(( $(date +%s) - START )) 秒"
if [ -n "${GITHUB_OUTPUT:-}" ]; then { echo "attempts=$attempts"; echo "rc=$rc"; echo "status=$STATUS"; } >> "$GITHUB_OUTPUT"; fi
exit $rc
