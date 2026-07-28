# DeepModeling 贡献者统计脚本 — 设计文档

日期：2026-07-28
状态：已与用户确认，待生成实现计划

## 1. 目标与用途

爬取 GitHub 组织（默认 `deepmodeling`）下所有仓库的贡献者名单，按仓库分类，用于**感谢与表彰贡献者**。

统计内容：

- 贡献者身份：GitHub id（login）、姓名、邮箱、GitHub 链接
- 贡献量：commit 数、PR 数（创建/合并/评审）、issue 数（创建/评论）
- 时间范围：显式日期区间，默认最近一年

用途决定了两条设计原则：

1. 漏掉一个人比多算一个人严重 —— 未能归并的身份必须单独列出，不静默丢弃
2. 数据要可复现、可解释 —— 半年后有人问"为什么 XX 不在名单里"，要能回答

## 2. 关键调研结论

针对 `deepmodeling` 组织的实测数据（2026-07-28）：

| 项目 | 数值 |
|---|---|
| 公开仓库数 | 67 |
| 其中 fork | 17 |
| 其中 archived | 2 |
| 总体积 | 约 20.7 GB |
| `sciencepedia` 单仓库体积 | 17.6 GB（占 85%） |
| 除 sciencepedia 外总体积 | 约 3.2 GB |

### 2.1 fork 的三种类型

17 个 fork 不能一概而论，实测上游后分为三类：

类型 self —— DeepModeling 生态内部仓库，技术上标着 fork，应纳入统计：

| 仓库 | 上游 | 最近推送 | Star |
|---|---|---|---|
| abacus-develop | abacusmodeling/abacus-develop | 2026-07-27 | 281 |
| dpgen2 | dptech-corp/dpgen2 | 2026-07-27 | 42 |
| deepflame-dev | deepflameCFD/deepflame-dev | 2026-06-04 | 220 |
| dflow | dptech-corp/dflow | 2026-01-21 | 81 |
| Uni-Fold | dptech-corp/Uni-Fold-jax | 2024-04-11 | 93 |
| DeepH-pack | mzjb/DeepH-pack | 2024-04-11 | 14 |
| LibRI | abacusmodeling/LibRI | 2025-02-18 | 6 |

判定依据：上游账号（`abacusmodeling`、`dptech-corp`、`mzjb`）属于 DeepModeling 生态，这些不是外部项目的 fork，而是同一社区在不同账号间的仓库迁移。

类型 external —— 真正的外部上游项目，纳入与否需业务决策：

| 仓库 | 上游 | 最近推送 | 体积 |
|---|---|---|---|
| lammps | lammps/lammps | 2024-04-11 | 702 MB |
| GPUMD | brucefan1983/GPUMD | 2026-06-09 | 342 MB |
| plumed2 | plumed/plumed2 | 2024-04-11 | 177 MB |
| fealpy | weihuayi/fealpy | 2025-05-09 | 98 MB |
| msys | DEShawResearch/msys | 2024-04-11 | 23 MB |
| ADMP | Roy-Kid/ADMP | 2024-04-11 | 1 MB |

风险：LAMMPS、PLUMED 是有数百贡献者的国际项目，全量统计会让上游开发者占据榜首，淹没 DeepModeling 贡献者。

类型 tooling —— CI 流程工具的 fork，建议排除：

`abacus-code-reviewer`（上游 platisd/clang-tidy-pr-comments）、`get-changed-files`（上游 jitterbit/get-changed-files）、`argo-workflows`、`deepmd-kit-recipes`。Star 数 0–2，上游作者列入表彰名单无意义。

### 2.2 时间窗使 fork 争议大幅缩小

默认一年窗口（since = 2025-07-28）下，按 `pushed_at` 判断，以下 9 个仓库窗口内不可能有 commit：

lammps、plumed2、msys、ADMP、get-changed-files、abacus-code-reviewer、deepmd-kit-recipes（均 2024-04-11）、fealpy（2025-05-09）、argo-workflows（2024-10-31）

结论：6 个 external fork 中有 5 个在一年窗口内无数据，唯一活跃的是 GPUMD。争议远小于预期。

由此得到一项优化：`pushed_at < since` 的仓库直接跳过，连克隆都不做。一年窗口下省掉约 1.1 GB 无效克隆及对应的上游 fetch。该判断安全，因为 `pushed_at` 是仓库任意分支的最后推送时间。

### 2.3 分支范围

统计所有分支，不限主分支。使用 `git log --all`，它按 commit SHA 天然去重 —— 已合并的 feature 分支上的 commit 与主分支是同一对象，不会重复计数。

真正多出的数据来自未合并分支与长期并行分支，例如 `abacus-develop` 以 `develop` 为默认分支、`GROMACS` 默认分支为 `2020-bulk-rename-ci-images`。

## 3. 架构与数据流

核心思路：两条数据管线汇合成一张表。

```
                    ┌─ 仓库清单 (REST: orgs/<org>/repos)
                    │   + fork/upstream/pushed_at/size 元数据
                    │
                    ├─ pushed_at 预筛 ──> 跳过窗口外的静态仓库
                    │
      ┌─────────────┴─────────────┐
      │                           │
  [Git 管线]                  [API 管线]
  mirror clone/fetch          GraphQL 批量查询
  git log --all --since       PR/Issue/Review/Comment
      │                           │
  commit 数 + 邮箱            pr_created/merged/reviewed
  （按 SHA 去重）              issue_created/commented
      │                           │
      └─────────────┬─────────────┘
                    │
              [身份归并层]
        email/login/name → 同一个人
                    │
              [输出层] CSV + Markdown + JSON
```

### 3.1 为什么需要两条管线

- 邮箱与 commit 数：只有 git 历史里有。GitHub API 的用户 profile 邮箱大多隐藏，返回 `null`，覆盖率仅 10–20%
- issue/PR/review/comment：只有 API 里有，git 历史中不存在

连接键为 GitHub login。git commit 的 author email 通过 GitHub 的 commit-to-user 映射反查 login，再与 API 侧对齐。

email 到 login 的反查按以下顺序，优先用零成本来源：

1. `xxx@users.noreply.github.com` 形式的邮箱直接解析出 login（`12345+name@` 与 `name@` 两种格式都要处理），无需请求
2. GraphQL 查询 commit 时一并取回 `author.user.login` 字段 —— PR/commit 查询本就要发，顺带拿到映射，不额外消耗额度
3. 以上都拿不到时，落到 `unmatched.csv` 供人工确认

明确不采用 `GET /search/commits?q=author-email:` 逐个反查：该端点限额仅 30 次/分钟，对上千个邮箱不可行。

### 3.2 身份归并（本脚本最易出错处）

同一个人常见的分裂情况：

- 多个邮箱（公司邮箱、私人邮箱、`xxx@users.noreply.github.com`）
- commit email 查不到 GitHub 账号（离职员工、未验证邮箱）
- bot 账号（`dependabot[bot]`、`pre-commit-ci[bot]`、`github-actions[bot]`）

归并策略：

1. 优先以 GitHub login 为主键（最可靠）
2. login 查不到时，退化为按 email 归并
3. 所有未能关联到 GitHub 账号的 email 输出到 `unmatched.csv`，供人工确认 —— 不静默丢数据
4. bot 用规则识别（`[bot]` 后缀 + 可配置黑名单），默认在主输出中排除，保留在 `bots.csv` 供核对误判

### 3.3 模块划分

| 模块 | 职责 |
|---|---|
| `config.py` | CLI 参数解析、时间窗计算 |
| `repos.py` | 仓库清单获取、fork 分类、pushed_at 预筛 |
| `git_stats.py` | mirror clone/fetch、git log 解析、commit 计数、行数统计 |
| `api_stats.py` | GraphQL 查询 PR/Issue/Review/Comment |
| `identity.py` | 身份归并、bot 识别 |
| `output.py` | CSV / Markdown / JSON 三种格式输出 |
| `cache.py` | 本地缓存管理、增量判断、断点续跑 |
| `main.py` | 编排流程 |

## 4. 缓存与增量策略

首次全量克隆一次，之后每次统计只做增量 fetch。

| 场景 | 网络开销 | 耗时（估） |
|---|---|---|
| 首次运行（一年窗口） | 约 3 GB（跳过 9 个静态仓库） | 10–20 分钟 |
| 隔天再跑 | 增量，几 MB | 十几秒 |
| 隔一个月再跑 | 增量，几十 MB | 1–2 分钟 |
| 改时间窗重跑 | 0 | 秒级 |

最后一行是选择"克隆 + git log"方案的重要收益：时间窗只是 `git log --since` 的过滤条件，本地历史已全部就位，改窗口纯本地重算，不碰网络。

实现要点：

- 缓存目录 `./.cache/repos/<repo>.git`，使用 `--mirror` 裸库（无工作区，省一半磁盘）
- 克隆使用 `--filter=blob:none` 减少传输量
- 每次运行判断本地是否已有仓库：有则 fetch，无则 clone
- 比对 `pushed_at` 与本地上次 fetch 时间，仓库未变动则连 fetch 都跳过
- API 响应按仓库落盘 `./.cache/api/<repo>-<日期>.json`，同日重跑读缓存

磁盘占用：一年窗口约 3 GB；纳入 `sciencepedia` 则约 20 GB。

API 侧无法像 git 那样长期复用，因为 GraphQL 查的是当前状态；但同日缓存可覆盖调试与改窗口重算的场景。

## 5. CLI 接口

### 5.1 基本用法

```bash
# 默认：最近一年到今天
python -m contributors

# 指定起始日期
python -m contributors --since 2025-07-28

# 完整闭区间：明确的考核周期
python -m contributors --since 2025-01-01 --until 2025-12-31

# 便捷写法
python -m contributors --months 6
```

### 5.2 参数表

| 参数 | 默认 | 说明 |
|---|---|---|
| `--org` | `deepmodeling` | 目标组织 |
| `--since` | 一年前的今天 | 起始日期 `YYYY-MM-DD`，含当天 |
| `--until` | 今天 | 结束日期 `YYYY-MM-DD`，含当天 |
| `--months N` | 无 | 便捷写法，等价 `--since <N 个月前>`；与 `--since` 互斥 |
| `--include-forks` | `all` | `all` / `self` / `none` |
| `--max-repo-size` | `2048` | MB，超过则跳过并警告 |
| `--repos` | 无 | 只跑指定仓库，逗号分隔 |
| `--count-lines` | 关 | 统计代码增删行数 |
| `--exclude-paths` | 内置列表 | 行数统计的额外排除模式 |
| `--no-fetch` | 关 | 纯本地缓存重算，零网络 |
| `--refresh` | 关 | 强制重新 fetch |
| `--include-bots` | 关 | 默认排除 bot |
| `--cache-dir` | `./.cache` | 缓存位置 |
| `--out-dir` | `./output` | 输出位置 |
| `--format` | `all` | `csv` / `md` / `json` / `all` |
| `--jobs` | `4` | 并发克隆/查询数 |
| `--verbose` | 关 | 详细日志 |

### 5.3 时间窗语义

- 默认值为一年内，内部立刻转成具体日期，运行时在日志与输出中打印实际区间（`统计区间: 2025-07-28 ~ 2026-07-28`），使报表口径明确
- 区间为闭区间，两端都含。`--until 2025-12-31` 内部转为 `< 2026-01-01 00:00`，避免漏掉最后一天
- 使用 `dateutil.relativedelta` 处理 `--months` 的月末回退（如 3 月 31 日退一个月），避免自写日期算术出错
- 时区统一按 UTC 判定并在输出中注明。git commit 时间戳带作者本地时区，GitHub API 返回 UTC，两者口径不一会使边界日期的提交错位；对有海外贡献者的社区，UTC 是唯一自洽选择

### 5.4 fork 筛选三档

- `all`（默认）—— 全部纳入，靠输出中的 `upstream_family` 与 `commits_not_in_upstream` 列事后筛选
- `self` —— 只要非 fork 加上游属于 DeepModeling 生态的仓库
- `none` —— 只要非 fork

设计意图：把"是否纳入上游贡献者"从一个需要事先拍板的决策，变成输出里的一列。一次爬取产出全量数据，业务口径变化时换参数即可，不必改代码或重爬。

### 5.5 认证

优先读 `GITHUB_TOKEN` / `GH_TOKEN` 环境变量，缺失则回退调用 `gh auth token`。本机 `gh` 2.96.0 已登录（scopes 含 `repo`、`read:org`），开箱即用。两者都无时报明确错误并提示解决方式。

## 6. 输出

### 6.1 字段

| 字段 | 来源 | 说明 |
|---|---|---|
| repo | REST | 仓库名 |
| login | API | GitHub 用户名（主键） |
| name | API/git | 姓名，优先 GitHub profile，回退 commit author name |
| email | git log | 邮箱，多个时以 `;` 分隔 |
| github_url | 拼接 | `https://github.com/<login>` |
| commits | git log | 窗口内 commit 数，按 SHA 去重、含所有分支 |
| commits_not_in_upstream | git log | 上游不可达的 commit 数；非 fork 时等于 commits |
| pr_created | GraphQL | 窗口内创建的 PR 数 |
| pr_merged | GraphQL | 其中已合并的 |
| pr_reviewed | GraphQL | 评审过的 PR 数 |
| issue_created | GraphQL | 创建的 issue 数 |
| issue_commented | GraphQL | 评论过的 issue 数 |
| is_fork | REST | 该仓库是否 fork |
| upstream | REST | 上游全名；非 fork 时为空 |
| upstream_family | 判定 | `self` / `external` / `tooling` / 空 |
| is_bot | 判定 | 是否 bot |

开启 `--count-lines` 时追加以下字段；关闭时这些列不出现在输出中，保持表格干净：

| 字段 | 说明 |
|---|---|
| additions | 新增行数，已过滤生成/数据文件 |
| deletions | 删除行数，已过滤 |
| files_changed | 涉及文件数（去重） |
| additions_raw | 未过滤的新增行数 |
| deletions_raw | 未过滤的删除行数 |

### 6.2 文件结构

```
output/
├── summary.csv              # 跨仓库汇总：一人一行，各项累加，按 commits 降序
├── by_repo.csv              # 主表：一人一仓库一行
├── repos/
│   ├── DeePMD-kit.csv       # 按仓库拆分，便于分发给各项目负责人
│   ├── abacus-develop.csv
│   └── ...
├── contributors.md          # Markdown 表格，可直接贴文档/issue 公示
├── contributors.json        # 完整结构化数据
├── unmatched.csv            # 未能关联 GitHub 账号的 email（需人工确认）
├── bots.csv                 # 被识别为 bot 的账号（供核对误判）
├── run_meta.json            # 运行参数、时间区间、跳过与失败的仓库、API 用量
└── run.log                  # 详细日志
```

`summary.csv` 面向表彰名单的主要使用场景；`by_repo.csv` 满足按仓库分类的需求；`repos/` 下单仓库文件支持按项目分别表彰。

Markdown 输出遵循用户全局约定：表格单元格内一律纯文本，不使用 `**` 加粗；需要强调时用列的语义或 emoji。

`run_meta.json` 记录本次运行的完整上下文：实际时间区间、fork 筛选档位、被体积阈值挡掉的仓库、被 pushed_at 预筛跳过的仓库、失败仓库及原因、API 消耗与剩余额度。目的是让数据可解释、可追溯。

### 6.3 代码行数指标的已知局限

该指标默认关闭，因为它容易被误读。实现时必须在输出表头注释与 `run_meta.json` 中写明以下提示：

1. 数字易被机器生成内容污染 —— `package-lock.json` 更新、导入的测试数据、vendored 第三方库、全仓库格式化都会造成虚高。科学计算仓库尤其常见：势函数参数、基组数据、体系构型文件动辄数十万行。默认按 `.gitattributes` 的 `linguist-generated` 规则加内置排除模式（`*.lock`、`*-lock.json`、`vendor/**`、`third_party/**`、`*.min.js` 及常见数据格式）过滤，并同时输出过滤前后两组数字以暴露差异
2. 合并提交必须跳过（`--no-merges`），否则合并者会被计入整个分支的改动
3. 行数与贡献价值关联很弱 —— 删除 2000 行冗余往往比新增 2000 行更有价值，3 行精准 bugfix 可能比 500 行样板更重要。不应据此排名

## 7. 错误处理与配额

核心原则：单仓库失败不影响整体，永不静默丢数据。

### 7.1 错误边界

每个仓库的处理包在独立错误边界内。克隆失败（网络中断、仓库删除、权限变更）时记录原因、继续下一个，最后在 `run_meta.json` 的 `failures` 中列出全部失败仓库及原因，并在终端结束时明确打印失败数量。不允许出现"跑完看起来正常、实际少了若干仓库"的情况。

### 7.2 API 配额

GitHub GraphQL 限额 5000 点/小时，按查询复杂度计费。67 个仓库查询 PR/Issue/Review/Comment 估算消耗 1500–2500 点，单次全量运行在额度内，但反复调试易撞限。措施：

- 每次请求后读取 `rateLimit` 剩余额度，低于 500 时主动暂停并打印倒计时至重置时间
- 遇 403/429（含 secondary rate limit）按指数退避重试，最多 5 次
- API 响应按仓库落盘缓存，同日重跑读缓存
- 结束时打印本次消耗点数与剩余额度

### 7.3 Git 操作

- 克隆超时（默认 30 分钟/仓库，可配）→ 标记失败、保留部分缓存供下次续传
- 缓存目录损坏（上次被 Ctrl-C 打断）→ 检测到裸库不完整时删除重建，而非在坏仓库上反复 fetch 失败
- 磁盘空间不足 → 开跑前预估所需空间并检查可用空间，不足时提前报错，而非跑到一半失败

### 7.4 可中断可续跑

Ctrl-C 时将已完成仓库的中间结果保存至 `.cache/progress.json`，下次运行自动跳过已完成仓库。对十几分钟量级的任务是必要能力。

### 7.5 日志

终端显示进度（第 N/67 个仓库、当前仓库名、已用时间），详细日志写入 `output/run.log`。默认精简，`--verbose` 展开细节。

## 8. 测试策略

难点在于脚本依赖网络与 git，测试不能每次都去爬 GitHub。分三层：

### 8.1 纯函数单元测试（无 IO，占多数）

- 时间窗计算 —— 闭区间边界、`--months` 与 `--since` 互斥报错、月末回退、非法日期格式
- 身份归并 —— 同 login 多 email 合并、无 login 时按 email 归并、`users.noreply.github.com` 处理、大小写与空白差异
- bot 识别 —— `dependabot[bot]` 命中；名字含 "bot" 的真人（如 `robotics-dev`）不误判
- fork 分类 —— `abacusmodeling/*`、`dptech-corp/*` 判 `self`；`lammps/lammps` 判 `external`；`jitterbit/*` 判 `tooling`
- `pushed_at` 预筛 —— 早于 since 则跳过；边界同日不跳过
- 输出格式 —— 断言 Markdown 表格单元格内无 `**`，将全局约定固化为测试

### 8.2 解析层测试（固定样本，不碰网络）

`git log` 输出与 GraphQL 响应各存若干真实样本于 `tests/fixtures/`。重点覆盖易错情况：commit message 含换行与特殊字符、author 与 committer 不同、合并提交、空 email、`--numstat` 中二进制文件显示为 `-`。

### 8.3 集成测试（少量，可选）

- `tests/fixtures/tiny-repo/` —— 测试内现场 `git init` 造小仓库，跑完整 git 管线验证端到端。不碰网络，但真实调用 git
- 标记 `@pytest.mark.network` 的少量真实 API 测试，默认 `pytest -m "not network"` 跳过，用于验证 GraphQL 查询未因 API 变更失效

工具：pytest 加 `responses`（mock HTTP）。开发采用 TDD，先写测试再写实现。

### 8.4 刻意的取舍

不为"完整 67 仓库全量运行"编写自动化测试 —— 那本质上在测 GitHub 而非本项目代码。替代方案是 `--repos <name>` 参数，支持对单仓库手动快速验证真实结果的合理性。

## 9. 环境

| 项 | 值 |
|---|---|
| 平台 | Windows 11，PowerShell |
| Python | 3.12.9 |
| gh CLI | 2.96.0，已登录账号 Ahri-qwq |
| token scopes | gist, read:org, repo, workflow |
| 工作目录 | `C:\MyCode\tools\github_contributors`（初始为空，非 git 仓库） |

依赖：`requests`、`python-dateutil`、`pytest`、`responses`。git 与 gh 为外部命令依赖。

## 10. 待确认事项

以下事项不阻塞实现，但会影响最终名单口径：

1. external 类 fork（一年窗口内仅 GPUMD 有数据）是否纳入表彰名单 —— 用户将与领导确认。设计已通过 `--include-forks` 与 `upstream_family` 列使该决策后置，无需重爬
2. `sciencepedia`（17.6 GB）是否纳入 —— 默认被 `--max-repo-size 2048` 挡掉并打印警告，需要时用 `--max-repo-size 20000` 纳入
3. 若 DeepModeling 内部已有贡献者名单或邮箱映射表，可作为身份归并的辅助输入以提升准确率

## 11. 实施记录

本节记录实现过程中的用户裁决与实测发现。原始过程记录在
`.superpowers/sdd/2026-07-28-github-contributors/progress.md`，但该目录已被
git 忽略、随时可能清除，故把有长期价值的内容固化在此。

### 11.1 实施进度

截至 2026-07-28，分支 `worktree-github-contributors`，测试 162 通过。

| Task | 内容 | 提交 | 状态 |
|---|---|---|---|
| 1 | 项目骨架与共享数据模型 | b7c0734 | 完成 |
| 2 | 时间窗与 CLI 参数解析 | e532ea7 | 完成 |
| 3 | token 获取与 fork 三分类 | 71cad0e, 8d0222a | 完成 |
| 4 | 身份归并与 bot 识别 | ec492ba, fd1ef88 | 完成 |
| 5 | 缓存管理与增量判断 | 16f478f | 完成 |
| 6 | git log 解析与 commit 统计 | 44a1001 | 完成 |
| 7 | GraphQL 查询 PR/Issue/Review | — | 未开始 |
| 8 | 三种格式输出 | — | 未开始 |
| 9 | 主流程编排与集成测试 | — | 未开始 |

已完成的是数据结构、参数解析、认证、仓库筛选、身份判定、缓存与 git 采集。
尚未打通端到端，故当前还产不出名单：缺 API 侧采集、输出层与主流程。

恢复方式：在仓库根目录开新会话，进入 `worktree-github-contributors` 分支，
从本设计文档与 `docs/superpowers/plans/2026-07-28-github-contributors.md`
的 Task 7 继续。计划文件中含 Task 7–9 的完整实现代码与测试。

### 11.2 用户裁决

| 议题 | 裁决 |
|---|---|
| fork 处理 | 纳入但标记，输出 `upstream_family` 与 `commits_not_in_upstream` 列，使口径可事后筛选而无需重爬 |
| 时间窗写法 | 用显式日期（`--since 2025-07-28`）而非滑动的 `--months N`，使考核周期可复现；`--months` 保留为便捷写法 |
| PR/Issue 口径 | 拆成 `pr_created`/`pr_merged`/`pr_reviewed` 与 `issue_created`/`issue_commented` 五列，因为「提 3 个 PR」与「评审 80 个 PR」是不同贡献画像 |
| 输出格式 | CSV、Markdown、JSON 全要 |
| 代码行数统计 | 默认关闭，需要时用 `--count-lines` 开启 |
| bot 判定依据 | 只看 `login` 与 `name`，不查邮箱。保留 name 检查，因 login 缺失时 name 为 `dependabot[bot]` 的 bot 仍需被识别 |
| 无邮箱贡献者 | 用姓名作后备归并键，并记入 `unmatched`，不得静默合并 |
| git 层 token | 不传。目标仓库均为公开，匿名访问即可 |
| 克隆策略 | 按 `--count-lines` 自动选择 blobless 或完整克隆 |
| 分页上限 | 加 `MAX_PAGES` 防御并打印中文警告，不静默截断 |

### 11.3 实测发现（推翻了设计中的若干假设）

| 假设 | 实测结果 |
|---|---|
| deepmodeling 有 100+ 仓库 | 错，实为 67 个（其中 17 fork、2 archived） |
| `git log --all` 会重复计数已合并分支 | 不会，按 SHA 天然去重（dpdata 473 条 = 473 唯一 SHA） |
| `%aI` 返回 UTC | 错，返回作者本地时区（实测有 +08:00）。故时间过滤交给 git，不自行解析 |
| 可逐用户查 `reviewed-by:` / `commenter:` | 不可行，约 100 人 × 67 仓库 = 数千请求会撞限额。改为按仓库分页并从节点读取，实测每页 cost 仅 1 |
| `[bot]` 后缀足以识别机器人 | 不足。`njzjz-bot`、`pre-commit-ci`、`codecov` 均无该后缀，须配黑名单 |
| 对 "bot" 子串匹配是安全的 | 不安全。`botelho`、`Botspot`、`bot50`、`BotBitmap` 是真实用户，会被误删出表彰名单 |
| 邮箱本地部分可用于 bot 判定 | 不可用。实测真人「Zhang San」邮箱为 `renovate@theirdomain.com` 时被误判为 bot；`devops+renovate@company.com` 同样中招 |
| `\x00` 可作 git log 字段分隔符 | Windows 不可用。`CreateProcess` 不允许参数含 NUL，必抛 `ValueError`。已改用 `\x1f` |
| blobless 克隆可配合 `--numstat` | 冲突。blobless 库需逐个回取 blob，实测约 2 分钟/仓库；完整克隆仅 0.20 秒，体积 25MB vs 3.6MB |
| `shutil.rmtree(ignore_errors=True)` 能删 git 缓存 | Windows 上不能。pack 文件带只读位，rmtree 部分失败且错误被吞，残留空壳目录使后续 clone 报 destination already exists。须用 `onexc` 回调清除只读位 |
| `--cache-dir` 指向无效盘符会给出有用提示 | 不会。`Path("Z:/").parent == Path("Z:/")` 使上溯循环退出后仍访问不存在的根，抛裸的 `FileNotFoundError`。已转为带指引的 `CacheError` |

一年窗口下另一项有用结论：`pushed_at` 早于窗口起点的仓库可直接跳过，
连克隆都省掉。一年窗口下命中 9 个仓库、约 1.1 GB 无效克隆，其中 5 个正是
需要业务裁决的 external fork —— 使该争议在默认窗口下大幅缩小。

### 11.4 已知遗留

| 项 | 说明 |
|---|---|
| `unknown:` 归并键 | 无邮箱且无姓名的多个贡献者仍共享同一键。已记入 `unmatched`，非静默丢弃；真实 git 历史几乎不出现此情形 |
| `fetch_repos` 无重试 | 每个 fork 的上游详情查询无退避重试，遇瞬时故障会抛错。单仓库失败不影响整体（Task 9 的错误边界负责） |

