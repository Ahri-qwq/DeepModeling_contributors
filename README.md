# backup-data 分支

这是一个独立的孤儿分支（orphan branch），跟 `master` 完全没有共同历史，**不要合并回
`master`，也不要从 `master` 合并过来**。

## 这里存的是什么

`data/events.db` 的定期快照——GitHub 组织贡献者事件的历史数据库（谁在哪天对哪个仓库
做了什么，用于生成日报/周报/月报）。

`.cache/repos`（各仓库的 git 缓存，约550MB）**不备份**，因为它是纯派生数据，随时可以
从 GitHub 重新 `git clone` 完整恢复，不值得占用这个分支的空间。

## 为什么单独开分支

避免这些自动生成的数据快照跟真正的代码改动提交混在一条历史线上，污染 `git log`；
也避免这个仓库的正常 clone/checkout 因为数据库历史增长而变慢。

## 谁在推送

服务器上的 crontab（`scripts/backup_data.sh`），每天 fetch 完成后顺带推一次。
详见 `docs/运维/服务器迁移进度-2026-09-03.md`（liuxiaogua仓库）。

## 怎么恢复

```bash
git clone --branch backup-data --single-branch \
  https://github.com/Ahri-qwq/DeepModeling_contributors.git backup
cp backup/events.db 目标位置/data/events.db
```
