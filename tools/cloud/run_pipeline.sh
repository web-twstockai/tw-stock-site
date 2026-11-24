#!/usr/bin/env bash
# 【Actions 用】執行 pipeline，整體時間預算內。用法：run_pipeline.sh <job>
# job 白名單：daily｜evening｜tpex_volume_fix｜screen_results_migrate｜halt_fix｜next_ref_fix｜market_backfill｜corp_backfill｜
#            revenue_backfill｜financials_backfill｜halts_backfill｜delist_backfill｜calendar_backfill｜chips_backfill｜tdcc_backfill（名稱＝pipeline/run.py --job）
# 選用參數（環境變數，皆由 update.yml 的 workflow_dispatch 輸入帶入；空值＝不傳，用 pipeline 預設）：
#   IN_MAX_DATES→--max-dates  IN_BUDGET_MIN→--budget-min  IN_FIX_FROM→--fix-from  IN_FIX_TO→--fix-to
#   IN_MAX_PAGES→--max-pages  IN_MAX_ENRICH→--max-enrich  IN_MONTHS→--months  IN_QUARTERS→--quarters
#   IN_NO_FETCH=true→--no-fetch  IN_SYMBOLS（daily／evening／tdcc_backfill 的 --symbols：sample30｜all；預設 sample30）
#   IN_UNIVERSE→TWSITE_UNIVERSE（sample30｜all；預設 sample30）
# 參數只接受該 job 用得到的，給了用不到的直接拒絕（避免以為有生效）。格式不對也拒絕。
# 重試：daily／evening／tpex_volume_fix 最多 3 次；回補類 job 只跑 1 次（被擋或限流不重打）。
# 結束碼 0＝success 或 partial（與 pipeline.run 一致）；非 0＝失敗。結果寫入 $GITHUB_OUTPUT：attempts、rc、status。
set -uo pipefail
JOB="${1:?job}"
die(){ echo "$*" >&2; exit 2; }
ALLOW=""; MAX_ATTEMPTS=1; EXTRA=()
case "$JOB" in
  daily)               BUDGET_MIN=${BUDGET_MIN:-60};  MAX_ATTEMPTS=3; ALLOW="symbols budget_min";;
  evening)             BUDGET_MIN=${BUDGET_MIN:-170}; MAX_ATTEMPTS=3; ALLOW="symbols budget_min";;
  tpex_volume_fix)     BUDGET_MIN=${BUDGET_MIN:-60};  MAX_ATTEMPTS=3; ALLOW="fix_from fix_to max_dates";;
  screen_results_migrate) BUDGET_MIN=${BUDGET_MIN:-30}; ALLOW="";;
  halt_fix)            BUDGET_MIN=${BUDGET_MIN:-60};  ALLOW="fix_from fix_to max_dates no_fetch";;
  next_ref_fix)        BUDGET_MIN=${BUDGET_MIN:-60};  ALLOW="fix_from fix_to max_dates";;
  market_backfill)     BUDGET_MIN=${BUDGET_MIN:-100}; ALLOW="fix_from fix_to max_dates budget_min";;
  corp_backfill)       BUDGET_MIN=${BUDGET_MIN:-100}; ALLOW="fix_from max_enrich budget_min";;
  revenue_backfill)    BUDGET_MIN=${BUDGET_MIN:-100}; ALLOW="fix_from months max_pages budget_min";;
  financials_backfill) BUDGET_MIN=${BUDGET_MIN:-100}; ALLOW="fix_from quarters max_pages budget_min";;
  halts_backfill)      BUDGET_MIN=${BUDGET_MIN:-60};  ALLOW="fix_from no_fetch";;
  delist_backfill)     BUDGET_MIN=${BUDGET_MIN:-60};  ALLOW="fix_from";;
  calendar_backfill)   BUDGET_MIN=${BUDGET_MIN:-60};  ALLOW="fix_from";;
  chips_backfill)      BUDGET_MIN=${BUDGET_MIN:-100}; ALLOW="fix_from fix_to max_dates budget_min";;
  tdcc_backfill)       BUDGET_MIN=${BUDGET_MIN:-100}; ALLOW="symbols fix_from fix_to max_dates budget_min";;
  *) die "job 不在白名單：$JOB";;
esac
allowed(){ [[ " $ALLOW " == *" $1 "* ]]; }
re_int='^[0-9]{1,5}$'; re_num='^[0-9]{1,4}(\.[0-9]{1,2})?$'; re_date='^20[0-9]{2}-[01][0-9]-[0-3][0-9]$'
add(){ # add <名稱> <值> <正則> <CLI 旗標>
  local name="$1" val="$2" re="$3" flag="$4"
  [ -z "$val" ] && return 0
  allowed "$name" || die "job=$JOB 不接受參數 $name（=$val）"
  [[ "$val" =~ $re ]] || die "參數 $name 格式不對：$val"
  EXTRA+=("$flag" "$val")
}
add max_dates  "${IN_MAX_DATES:-}"  "$re_int"  --max-dates
add budget_min "${IN_BUDGET_MIN:-}" "$re_num"  --budget-min
add fix_from   "${IN_FIX_FROM:-}"   "$re_date" --fix-from
add fix_to     "${IN_FIX_TO:-}"     "$re_date" --fix-to
add max_pages  "${IN_MAX_PAGES:-}"  "$re_int"  --max-pages
add max_enrich "${IN_MAX_ENRICH:-}" "$re_int"  --max-enrich
add months     "${IN_MONTHS:-}"     "$re_int"  --months
add quarters   "${IN_QUARTERS:-}"   "$re_int"  --quarters
case "${IN_NO_FETCH:-}" in
  ""|false) ;;
  true) allowed no_fetch || die "job=$JOB 不接受參數 no_fetch"; EXTRA+=(--no-fetch);;
  *) die "參數 no_fetch 只能是 true／false：${IN_NO_FETCH}";;
esac
SYMBOLS="${IN_SYMBOLS:-}"
if [ -n "$SYMBOLS" ]; then
  [[ "$SYMBOLS" =~ ^(sample30|all)$ ]] || die "參數 symbols 只能是 sample30／all：$SYMBOLS"
  # market_backfill 一律是全市場（pipeline 內建），symbols=all 只是確認用；tdcc_backfill 接受 sample30｜all
  if [ "$JOB" = market_backfill ]; then [ "$SYMBOLS" = all ] || die "market_backfill 一律全市場，symbols 只能是 all"
  elif ! allowed symbols; then die "job=$JOB 不接受參數 symbols"; fi
fi
case "$JOB" in daily|evening|tdcc_backfill) EXTRA+=(--symbols "${SYMBOLS:-sample30}");; esac
UNIVERSE="${IN_UNIVERSE:-sample30}"
[[ "$UNIVERSE" =~ ^(sample30|all)$ ]] || die "參數 universe 只能是 sample30／all：$UNIVERSE"
# pipeline 的 --budget-min 之外，外層再留 15 分鐘收尾（寫摘要、停牌重算）
if [ -n "${IN_BUDGET_MIN:-}" ]; then BUDGET_MIN=$(( ${IN_BUDGET_MIN%.*} + 15 )); fi
[ "$BUDGET_MIN" -le 175 ] || die "時間預算過大（${BUDGET_MIN} 分）：step timeout 185 分"
if [ "${RUN_PIPELINE_DRY:-0}" = 1 ]; then echo "DRY python -m pipeline.run --job $JOB ${EXTRA[*]} | TWSITE_UNIVERSE=$UNIVERSE BUDGET_MIN=$BUDGET_MIN MAX_ATTEMPTS=$MAX_ATTEMPTS"; exit 0; fi
export TWSITE_UNIVERSE="$UNIVERSE" PYTHONUNBUFFERED=1
START=$(date +%s); DEADLINE=$((START + BUDGET_MIN*60)); rc=1; attempts=0
for i in $(seq 1 "$MAX_ATTEMPTS"); do
  now=$(date +%s); left=$((DEADLINE - now))
  if [ $left -lt 120 ]; then echo "剩餘時間不足（${left}s），不再嘗試"; break; fi
  attempts=$i
  echo "=== pipeline 第 $i/$MAX_ATTEMPTS 次嘗試（job=$JOB ${EXTRA[*]}，剩餘預算 $((left/60)) 分）$(date -u +%FT%TZ) ==="
  # 超時先送 SIGINT（pipeline 會把 job 標 interrupted 再結束），60 秒後強制結束
  timeout -s INT -k 60 "${left}s" python -m pipeline.run --job "$JOB" "${EXTRA[@]}"
  rc=$?
  echo "=== 第 $i 次結束 rc=$rc ==="
  [ $rc -eq 0 ] && break
  [ $i -lt "$MAX_ATTEMPTS" ] && { echo "等 60 秒後重試"; sleep 60; }
done
STATUS=$(python - <<'PY'
import sqlite3
c = sqlite3.connect("data/twstock.db")
r = c.execute("select status from job_runs order by run_id desc limit 1").fetchone()
print(r[0] if r else "unknown")
PY
)
echo "最後一次 job_runs.status=$STATUS rc=$rc attempts=$attempts 用時 $(( $(date +%s) - START )) 秒"
# 回補類 job：把 job_runs 摘要（請求數、耗時、新增列數）印到 log，方便從 Actions 直接檢查
case "$JOB" in *_backfill|halt_fix|next_ref_fix|screen_results_migrate)
python - <<'PY' || true
import sqlite3
c = sqlite3.connect("data/twstock.db")
r = c.execute("select run_id, job_name, status, started_at, finished_at, error_summary, success_items from job_runs order by run_id desc limit 1").fetchone()
if r:
    print("JOB_SUMMARY run_id=%s job=%s status=%s started=%s finished=%s err=%s" % r[:6])
    print("JOB_SUMMARY_ITEMS " + str(r[6])[:6000])
PY
;; esac
if [ -n "${GITHUB_OUTPUT:-}" ]; then { echo "attempts=$attempts"; echo "rc=$rc"; echo "status=$STATUS"; } >> "$GITHUB_OUTPUT"; fi
exit $rc
