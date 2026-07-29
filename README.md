# GitHub 组织贡献者统计

整合 git commit 历史与 GitHub API（PR/Issue/Review/Comment），输出可用于感谢与表彰的贡献者名单。

## 安装

```bash
pip install -r requirements.txt
```

需要 **git** 和 **gh CLI**（或设置环境变量 `GITHUB_TOKEN` / `GH_TOKEN`）。
Token 仅用于 GitHub API；git clone 走匿名访问，凭据不会写入缓存目录。

## 用法

```bash
# 默认：最近一年
python -m contributors

# 指定日期区间（含两端）
python -m contributors --since 2025-01-01 --until 2025-12-31

# 便捷写法
python -m contributors --months 3

# 只跑指定仓库
python -m contributors --repos deepmd-kit,abacus-develop

# fork 筛选：只纳入生态内部 fork（如 abacus-develop 这类跨账号迁移的仓库）
python -m contributors --include-forks self

# 改时间窗重算 commit 口径，零网络，秒级完成（PR/Issue 列会全为 0）
python -m contributors --since 2026-01-01 --no-fetch

# 统计代码增删行数（默认关闭，需完整克隆约 7 倍磁盘）
python -m contributors --count-lines

# 纳入超大仓库
python -m contributors --max-repo-size 20000
```

### 参数表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--org` | `deepmodeling` | 目标组织 |
| `--since` | 一年前 | 起始日期 `YYYY-MM-DD`，含当天；与 `--months` 互斥 |
| `--until` | 今天 | 结束日期 `YYYY-MM-DD`，含当天 |
| `--months` | — | 等价 `--since <N 个月前>` |
| `--include-forks` | `all` | `all` / `self`（仅生态内部）/ `none` |
| `--max-repo-size` | `2048` | 仓库体积上限（MB），超出跳过 |
| `--repos` | — | 只跑指定仓库，逗号分隔 |
| `--count-lines` | 关 | 统计代码增删行数 |
| `--no-fetch` | 关 | 零网络，纯本地缓存重算。API 侧数据全部为空 |
| `--refresh` | 关 | 强制重新 fetch |
| `--include-bots` | 关 | 默认排除 bot；开启后并入主名单 |
| `--cache-dir` | `./.cache` | 缓存目录 |
| `--out-dir` | `./output` | 输出目录 |
| `--format` | `all` | `csv` / `md` / `json` / `all` |

> `--no-fetch` 完全跳过 GitHub API，PR/Issue/Review/Comment 各列全为 0，email→login 映射仅靠 noreply 邮箱正则解析。适用于改时间窗快速验证 commit 口径；出正式名单用完整跑法。

## 工作原理

两条独立管线分别采集数据，最后通过 email→login 映射归并：

```
           GitHub REST API → 仓库清单 → pushed_at 预筛
                    │
       ┌────────────┴────────────┐
       │                         │
   [Git 管线]                [API 管线]
   git clone --mirror        GraphQL 查询
   git log --all             PR/Issue/Review/Comment
   --no-merges               + email→login 映射
       │                         │
   commit 数、邮箱、姓名      创建/合并/评审/评论数
       │                         │
       └────────────┬────────────┘
                    │
            [身份归并] → 输出 CSV/MD/JSON
```

**为什么需要两条管线？** git log 有邮箱和 commit 数但没有 PR/Issue 数据；GitHub API 有 PR/Issue 数据但 profile 邮箱 80–90% 返回 null。两者通过 email→login 映射连接。

email→login 映射的来源（按优先级）：
1. `xxx@users.noreply.github.com` 格式直接正则解析（零成本）
2. GraphQL 查询 commit 时顺带取回 `author.user.login`（零额外额度）
3. 都拿不到 → 记入 `unmatched.csv` 供人工确认（**不丢弃**）

**fork 三分类**：上游属同一生态（白名单组织）→ `self`（纳入）；真正的外部项目 → `external`；CI 流程工具 → `tooling`（排除）。输出中用 `upstream_family` 列标注，可在输出层面事后筛选而不需重爬。fork 仓库额外提供 `commits_not_in_upstream` 列，排除上游已有提交后的数量。

## 输出

### 字段

| 列名 | 类型 | 说明 |
|------|------|------|
| `repo` | str | 仓库名（`summary.csv` 中为空） |
| `login` | str | GitHub 登录名 |
| `name` | str | 姓名（优先 GitHub profile，回退 commit author name） |
| `email` | str | 邮箱，多个以 `;` 分隔 |
| `github_url` | str | 个人主页链接 |
| `commits` | int | 窗口内 commit 数，按 SHA 去重，覆盖所有分支，排除合并提交 |
| `commits_not_in_upstream` | int | 仅 fork 有意义：排除上游已有提交后的数量。上游可达的提交记 0（实测 GPUMD 窗口内 638 次提交全部来自上游，该列为 0）。非 fork 时等于 `commits` |
| `pr_created` | int | 创建的 PR 数 |
| `pr_merged` | int | 其中已合并的 PR 数 |
| `pr_reviewed` | int | 评审过的 PR 数（同一 PR 多条 review 只记一次） |
| `issue_created` | int | 创建的 Issue 数 |
| `issue_commented` | int | Issue/PR 评论数 |
| `is_fork` | bool | 该仓库是否 fork |
| `upstream` | str | 上游仓库全名，非 fork 时为空 |
| `upstream_family` | str | fork 分类：`self` / `external` / `tooling` |
| `is_bot` | bool | 是否被识别为 bot |

开启 `--count-lines` 时追加：`additions`（新增，已过滤）、`deletions`（删除，已过滤）、`files_changed`（涉及文件数）、`additions_raw`（原始新增）、`deletions_raw`（原始删除）。关闭时这些列不出现。

### 输出文件

```
output/
├── summary.csv           # 跨仓库汇总，一人一行，按 commits 降序
├── by_repo.csv           # 主表，一人一仓库一行（最细粒度）
├── repos/<name>.csv      # 按仓库拆分，可分发各项目 maintainer
├── contributors.md       # Markdown 表格，可直接贴文档公示
├── contributors.json     # 完整结构化数据，供程序消费
├── unmatched.csv         # 未能关联 GitHub 账号的身份，需人工确认
├── bots.csv              # 被识别为 bot 的账号，供核对误判
└── run_meta.json         # 运行参数、跳过/失败仓库、API 点数消耗
```

## 缓存

首次运行克隆全部仓库到 `./.cache/repos/`（一年窗口约 3 GB），之后只做增量 fetch。
比对远端 `pushed_at` 与本地上次记录，仓库无变动时连 fetch 都跳过。
API 响应按天缓存（`./.cache/api/<repo>-<日期>.json`），同日重跑读缓存。

| 场景 | 网络开销 | 耗时 |
|------|----------|------|
| 首次运行 | 约 3 GB | 10–20 分钟 |
| 隔天再跑 | 几 MB | 十几秒 |
| 隔月再跑 | 几十 MB | 1–2 分钟 |
| 改时间窗重算（`--no-fetch`，仅 commit 口径） | 0 | 秒级 |
| 改时间窗重算（含 PR/Issue，走完整跑法） | 几 MB | 十几秒到几分钟 |

`--count-lines` 需要完整克隆（约 7 倍体积）；不带该参数用 blobless 部分克隆。
两者切换自动检测并重建缓存。

## 统计口径

- **时间**：闭区间（含两端），统一按 UTC 判定。提交以 **author date** 为准而非 committer date（rebase/cherry-pick/squash 会刷新后者，导致旧代码被算进新窗口）
- **分支**：统计所有分支（`git log --all`），按 commit SHA 天然去重，排除合并提交（`--no-merges`）
- **去重**：Git 侧按 SHA 去重；API 侧同一 PR 内多条 review 只记一次、PR 作者 review 自己的 PR 不计
- **bot 识别**：仅按 login 与 name 判定。规则：[bot] 后缀 + 显式黑名单（`codecov`、`github-actions` 等）。禁止子串匹配（`botelho` 是真人）。默认排除，`--include-bots` 可并入主名单，同时输出 `bots.csv` 供核对误判
- **未关联身份**：查不到 GitHub 账号的邮箱不会丢弃，记入 `unmatched.csv`。漏掉一个真实贡献者比多算一个严重
- **行数**：默认关闭，不宜用于排名。易被生成文件（lock 文件、vendored 代码、数据文件）污染；行数与贡献价值关联弱。已内置排除模式（`*.lock`、`vendor/**`、`*.npy` 等）并同时输出过滤前后两组数字

## 常见问题

### 为什么会有"未关联 GitHub 账号"的身份？

这些人在 git commit 里使用的邮箱无法对应到任何 GitHub 用户。实测常见成因：
邮箱域名拼写错误（如 `pku.eud.cn`，正确应为 `pku.edu.cn`）、未在 GitHub
验证的私人/公司邮箱、同一人在多台机器上用了不同邮箱、以及未配置 git 身份
留下的默认值（`root@localhost`、`you@example.com` 等）。

他们会作为独立条目出现在输出中并记入 `unmatched.csv`，不静默丢弃。
`unmatched.csv` 附带姓名、提交数、所在仓库，按提交数降序，便于优先确认
贡献量大的条目。同仓库内姓名完全相同且只对应一个已知账号时会自动合并；
姓名不同、同名对应多个账号、或两条都无账号时一律不合并，宁可漏合并也不
错合并。

### `--no-fetch` 和完整跑法有什么区别？

| | `--no-fetch` | 完整跑法 |
|--|-------------|----------|
| commit 统计 | ✓ | ✓ |
| PR/Issue/Review/Comment | ✗ 全为 0 | ✓ |
| email→login 映射 | 仅 noreply 正则解析 | ✓（GraphQL 顺带取回）|
| 能关联账号的比例 | 低 | 高 |

### 如何验证结果？

1. 用 `--repos <单个仓库>` 手动核查数据是否合理
2. 检查 `run_meta.json` 中 `skipped` 和 `failures` 确认无意外跳过/失败
3. 核对 `unmatched.csv` 和 `bots.csv` 确认无遗漏或误判
