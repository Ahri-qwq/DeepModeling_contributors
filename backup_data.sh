#!/usr/bin/env bash
# 把日报项目的运行时数据备份到独立的 backup-data 孤儿分支。
# 备份对象：data/events.db。不备份密钥文件和可重新生成的派生数据。

set -u

cd "$(dirname "$0")"

BACKUP_DIR="../DeepModeling_contributors-backup-data"
DB_SOURCE="data/events.db"

if [ ! -d "$BACKUP_DIR" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份worktree不存在（$BACKUP_DIR），先初始化" >&2
    exit 1
fi

if [ ! -f "$DB_SOURCE" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $DB_SOURCE 不存在，跳过备份" >&2
    exit 1
fi

cp "$DB_SOURCE" "$BACKUP_DIR/events.db"

cd "$BACKUP_DIR"
git add events.db

if git diff --cached --quiet; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 无变化，跳过提交"
    exit 0
fi

git commit -q -m "backup: $(date '+%Y-%m-%d %H:%M:%S')"

# push 走 worktree 已配好的 SSH origin，不再写死 HTTPS URL：到 GitHub 的
# HTTPS 链路丢包 4-6%，2026-09-10 两套备份同时静默失败就是它。SSH 用密钥
# 认证，顺带去掉了对 ../DeepModeling_contributors/.token.env 的依赖。
#
# 必须显式判断 push 退出码。脚本只有 set -u 没有 set -e（前面 cp/find 有
# 依赖容错的地方，加全局 set -e 会引入意外退出），push 失败后原先会继续
# 打印"推送完成"、以退出码 0 结束，cron 完全看不出异地备份已经停了。
push_backup() {
    git push origin backup-data
}

if push_backup; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup-data 推送完成"
else
    # 链路是间歇性抖动而非全断，等 30 秒重试一次能挡掉大部分偶发失败
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup-data 推送失败，30 秒后重试" >&2
    sleep 30
    if push_backup; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup-data 推送完成（第 2 次尝试）"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup-data 推送失败，本地 commit 已生成，下次运行会一并推送" >&2
        exit 1
    fi
fi
