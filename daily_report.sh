#!/usr/bin/env bash
# ============================================================
# DeepModeling community daily report (Linux / crontab 版)
#
# 用法：
#   daily_report.sh --fetch    只抓取，失败会重试
#   daily_report.sh --push     只推送（几秒钟）
#   daily_report.sh --daily    抓取，抓取成功才推送（默认动作）
#   daily_report.sh            同 --daily
#   daily_report.sh --dry      只渲染，不发送
#   daily_report.sh --weekly   推送上周汇总（周一跑）
#   daily_report.sh --monthly  推送上月汇总（每月1号跑）
#
# 移植自 Windows 版 daily_report.bat，逻辑与注释保持一致：
#
# weekly/monthly 只读事件库（几秒钟，不抓取），按事件的真实时间戳计数，
# 不像 daily 报表是按"首次发现"计数。它们不碰 daily 的基线。
#
# 为什么 --daily 要链式调用：如果 fetch 和 push 是两个独立的定时任务，
# fetch 失败后 push 依然会跑，那样会在没有任何提示的情况下把上一次的
# 旧数据当作本次报告推送出去。链式调用让 push 依赖 fetch 成功。
#
# fetch 步骤会重试每个失败的仓库：失败先当场重试一次，然后转向下一个
# 仓库而不是死等。失败的仓库整体会被重跑 3 轮，所以每个仓库最多有 6 次
# 尝试机会。触发限流的错误不会当场重试（立刻再打只会让限流更深），
# 会等到下一轮。只剩最后一个仓库时，尝试间隔会拉开到 30 秒。仍然失败的
# 仓库会在卡片里点名列出，它们的增量会滚入次日报告，所以不会因为一个
# 仓库连不上就卡住整体推送。下面这层外层重试只在整个任务全垮时才触发
# （仓库列表本身取不到、或机器完全断网）。
#
# 生产定时任务（北京时间，由 ubuntu 用户的 crontab 触发）：
#   02:00  daily_report.sh --fetch，抓取成功后同步多维表格
#   10:01  daily_report.sh --push，标题使用报告日
#   11:00  backup_data.sh
#
# 自 2026-11-13 的日报起，取前一天 06:00 到报告日 06:00 之间的入库增量。
# 抓取若过 06:00 才入库，则进入下一份日报。旧报告补推沿用 00:00 锚点。
# 不另外安排 06:00 的任务。手工重渲染某份日报时，--date 指报告日：
#   .venv/bin/python3.12 -m contributors --daily --only-notify --notify-dry-run \
#     --date 2026-11-13
# ============================================================

set -u

cd "$(dirname "$0")"

# GITHUB_TOKEN 存在 .token.env 里（chmod 600，不在 git 里）
if [ -f .token.env ]; then
    source .token.env
fi

PYTHON="$(pwd)/.venv/bin/python3.12"

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# 目标仓库范围。留空 = 组织内所有通过 fork/archived/size 过滤的仓库。
# 要缩小范围，设 REPOS="deepmd-kit,dpdata" 之类。
# 给已有范围新增仓库后，先单独跑一次 --mark-notified：从未见过的仓库
# 会把它一整年的历史都算作"新增"，导致报告被刷屏。
REPOS=""
REPO_ARG=()
if [ -n "$REPOS" ]; then
    REPO_ARG=(--repos "$REPOS")
fi

# fetch 整体失败时的外层重试次数与等待时间。
# Python 内部已经对单次 HTTP 调用做了退避重试，也对单个失败仓库做了
# 3 轮重试（间隔 60 秒）。这层外层循环只覆盖"整个任务都失败"的情况：
# 仓库列表本身取不到，或机器完全断网。保持小值——完整跑一遍约 24
# 分钟，2 次尝试已经接近 50 分钟。
MAX_TRIES=2
RETRY_WAIT=300

mkdir -p logs
TODAY="$(date +%Y-%m-%d)"
LOG="logs/daily-${TODAY}.log"
STATUS="logs/last-fetch-status.txt"

ACTION="${1:---daily}"

log() {
    echo "$1" >> "$LOG"
}

fetch_with_retry() {
    local try=1
    local code=1
    while [ "$try" -le "$MAX_TRIES" ]; do
        log "=============================================="
        log "[$(date '+%Y-%m-%d %H:%M:%S')] start (fetch, attempt ${try} of ${MAX_TRIES})"
        "$PYTHON" -m contributors --daily "${REPO_ARG[@]}" --no-notify >> "$LOG" 2>&1
        code=$?
        log "[$(date '+%Y-%m-%d %H:%M:%S')] done (fetch attempt ${try}), exit code ${code}"

        if [ "$code" -eq 0 ]; then
            echo "ok $(date '+%Y-%m-%d %H:%M:%S')" > "$STATUS"
            # ------------------------------------------------------------------
            # fetch 成功后同步多维表格。失败只打日志，不影响 fetch 退出码。
            # 四个 FEISHU_* 键任一为空，feishu_bitable.py 自动跳过（exit 0）。
            # ------------------------------------------------------------------
            if [ -f .env ]; then set -a; source .env; set +a; fi
            bitable_csv="output/daily/by_repo.csv"
            if [ -f "$bitable_csv" ]; then
                log "[$(date '+%Y-%m-%d %H:%M:%S')] bitable sync start"
                if "$PYTHON" -m contributors.feishu_bitable "$bitable_csv" >> "$LOG" 2>&1; then
                    log "[$(date '+%Y-%m-%d %H:%M:%S')] bitable sync ok"
                else
                    log "[$(date '+%Y-%m-%d %H:%M:%S')] bitable sync failed (non-fatal)"
                fi
            else
                log "[$(date '+%Y-%m-%d %H:%M:%S')] bitable sync skipped: $bitable_csv not found"
            fi
            return 0
        fi

        try=$((try + 1))
        if [ "$try" -le "$MAX_TRIES" ]; then
            log "[$(date '+%Y-%m-%d %H:%M:%S')] fetch failed, waiting ${RETRY_WAIT}s before retry"
            sleep "$RETRY_WAIT"
        fi
    done

    echo "failed $(date '+%Y-%m-%d %H:%M:%S') exit=${code}" > "$STATUS"
    return "$code"
}

do_push() {
    log "=============================================="
    log "[$(date '+%Y-%m-%d %H:%M:%S')] start (push only)"
    "$PYTHON" -m contributors --daily "${REPO_ARG[@]}" --only-notify --notify >> "$LOG" 2>&1
    local code=$?
    log "[$(date '+%Y-%m-%d %H:%M:%S')] done (push only), exit code ${code}"
    return "$code"
}

do_dry() {
    log "=============================================="
    log "[$(date '+%Y-%m-%d %H:%M:%S')] start (dry run)"
    "$PYTHON" -m contributors --daily "${REPO_ARG[@]}" --notify-dry-run >> "$LOG" 2>&1
    local code=$?
    log "[$(date '+%Y-%m-%d %H:%M:%S')] done (dry run), exit code ${code}"
    return "$code"
}

do_weekly() {
    log "=============================================="
    log "[$(date '+%Y-%m-%d %H:%M:%S')] start (weekly)"
    "$PYTHON" -m contributors --weekly --notify >> "$LOG" 2>&1
    local code=$?
    log "[$(date '+%Y-%m-%d %H:%M:%S')] done (weekly), exit code ${code}"
    return "$code"
}

do_monthly() {
    log "=============================================="
    log "[$(date '+%Y-%m-%d %H:%M:%S')] start (monthly)"
    "$PYTHON" -m contributors --monthly --notify >> "$LOG" 2>&1
    local code=$?
    log "[$(date '+%Y-%m-%d %H:%M:%S')] done (monthly), exit code ${code}"
    return "$code"
}

do_chain() {
    fetch_with_retry
    local code=$?
    if [ "$code" -ne 0 ]; then
        log "[$(date '+%Y-%m-%d %H:%M:%S')] fetch failed after ${MAX_TRIES} tries, SKIPPING push"
        log "[$(date '+%Y-%m-%d %H:%M:%S')] no report was sent - fix the error above and rerun"
        return "$code"
    fi
    do_push
}

case "$ACTION" in
    --fetch)
        fetch_with_retry
        exit $?
        ;;
    --push)
        do_push
        exit $?
        ;;
    --dry)
        do_dry
        exit $?
        ;;
    --daily)
        do_chain
        exit $?
        ;;
    --weekly)
        do_weekly
        exit $?
        ;;
    --monthly)
        do_monthly
        exit $?
        ;;
    *)
        echo "Unknown option: $ACTION"
        echo "Use --fetch, --push, --daily, --dry, --weekly, or --monthly"
        exit 2
        ;;
esac
