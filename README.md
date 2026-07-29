# GitHub 组织贡献者统计

爬取 GitHub 组织下所有仓库的贡献者名单，用于感谢与表彰贡献者。

## 功能

- 按仓库分类统计贡献者：GitHub id、姓名、邮箱、主页链接
- 贡献量：commit 数、PR（创建/合并/评审）、issue（创建/评论）
- 统计所有分支，不限主分支；按 commit SHA 去重
- 时间区间可指定，默认最近一年
- 输出 CSV / Markdown / JSON 三种格式

## 安装

```bash
pip install -r requirements.txt
```

需要 git 与 gh CLI。执行 `gh auth login` 完成认证，或设置环境变量 `GITHUB_TOKEN`。

token 仅用于 GitHub API；克隆走匿名访问，不会把凭据写入缓存目录。

## 用法

```bash
# 默认：最近一年
python -m contributors

# 指定起始日期
python -m contributors --since 2025-07-28

# 明确的考核周期
python -m contributors --since 2025-01-01 --until 2025-12-31

# 便捷写法
python -m contributors --months 3

# 改时间窗重算：零网络，秒级完成
python -m contributors --since 2026-01-01 --no-fetch

# 统计代码增删行数（默认关闭）
python -m contributors --count-lines
```

完整参数见 `python -m contributors --help`。

## 输出

| 文件 | 内容 |
|---|---|
| summary.csv | 跨仓库汇总，一人一行，按 commit 数降序 |
| by_repo.csv | 主表，一人一仓库一行 |
| repos/<name>.csv | 按仓库拆分，便于分发给各项目负责人 |
| contributors.md | Markdown 表格，可直接贴文档公示 |
| contributors.json | 完整结构化数据 |
| unmatched.csv | 未能关联 GitHub 账号的身份，需人工确认 |
| bots.csv | 被识别为 bot 的账号，供核对是否误判 |
| run_meta.json | 运行参数、跳过与失败的仓库、API 用量 |

## 缓存

首次运行克隆全部仓库到 `./.cache/repos/`（一年窗口约 3 GB），之后只做增量
fetch。改时间窗时用 `--no-fetch` 可完全离线重算。

`--count-lines` 需要完整克隆（约 7 倍体积）。不带该参数时用 blobless 部分
克隆。两者切换会触发重新克隆。

## 统计口径

- 时间区间含两端，一律按 UTC 判定
- 提交时间以 author date（代码写成时刻）为准，而非 committer date。
  rebase、cherry-pick、squash 合并会刷新后者，用它统计会把旧代码算进新窗口
- 统计所有分支（`--all`），未合并分支上的提交同样计入
- 排除合并提交，否则合并者会被算上整个分支的改动
- fork 分为 self / external / tooling 三类，用 `--include-forks` 控制纳入
  范围；`commits_not_in_upstream` 列给出排除上游可达提交后的数量
- 代码行数指标默认关闭，且不宜用于排名（易被生成文件污染）
- 未能关联 GitHub 账号的贡献者不会被丢弃，会列入 unmatched.csv
- bot 仅按 login 与 name 判定（`[bot]` 后缀或显式黑名单），不查邮箱：
  实测邮箱本地部分含 renovate 等词的真人会被误判
