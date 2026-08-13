# GitHub 组织贡献者统计

整合 git commit 历史与 GitHub API（PR/Issue/Review/Comment），输出可用于感谢与表彰的贡献者名单，
并每天把社区动态推送到飞书群。

> ## 只想调用它？看 [`快速开始.md`](快速开始.md)
>
> 那份文档是给**外部工具与 Agent** 用的调用手册：命令、参数、退出码、常见坑，
> 不讲实现原理。四条最常用的命令（在项目根目录执行，bat 用绝对路径）：
>
> ```bat
> daily_report.bat --dry      先看会发什么，不发送、无副作用
> daily_report.bat --fetch    抓取数据，约 25 分钟，不发送
> daily_report.bat --push     推送日报，数秒
> daily_report.bat --weekly   推送上周汇总，数秒
> ```
>
> 退出码 `0` 成功、`1` 抓取失败、`2` 参数错误。日志在 `logs\daily-<日期>.log`。
> **第一次调用请用 `--dry`**，它不会产生任何外部副作用。

第一次在新机器上配置环境，请看 [`docs/第一期工程/新设备快速开始.md`](docs/第一期工程/新设备快速开始.md)。

> **提交数有两个口径，分别成列。** `commits` 只数合并进主干的提交；
> `commits_loose` 额外计入 PR 分支上的原始提交。用 squash 合并时同一份工作
> 在两处各有一条记录，所以宽松口径含重复计数，而严格口径会漏掉署名邮箱与
> GitHub 账号对不上的人。详见[提交数的两个口径](#提交数的两个口径)。

> **适用时间范围：一年以内。**
> fork 仓库的分类（哪些算社区自己的项目、哪些算外部项目）只对最近一年活跃的仓库经过人工确认。
> 若把 `--since` 往前挪到一年以上，会有更多沉寂的 fork 进入统计流程，它们的 `upstream_family`
> 判定未经确认，需要重新走一遍确认流程。

## 环境要求

| 项 | 要求 | 原因 |
|------|--------|------|
| Python | 3.12 或更高 | `shutil.rmtree(onexc=...)` 在 3.12 才有 |
| git | 任意近期版本 | 需在 PATH 中 |
| GitHub token | `gh auth login` 或环境变量 | 仅用于 GitHub API |

## 安装

```bash
pip install -r requirements.txt          # 仅运行时依赖
pip install -r requirements-dev.txt      # 含测试依赖
```

也可以直接安装本项目，装完得到 `github-contributors` 命令：

```bash
pip install -e .        # 或 pip install -e ".[dev]" 带测试依赖
```

Token 查找顺序：环境变量 `GITHUB_TOKEN` → `GH_TOKEN` → `gh auth token`。
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

# 磁盘紧张时排除超大仓库（默认不限，全部纳入）
python -m contributors --max-repo-size 2048

# 每天跑一次并把增量战报推送到飞书群
python -m contributors --since 2026-01-01 --notify

# 先看看会推什么，不真发
python -m contributors --since 2026-01-01 --notify-dry-run
```

### 参数表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--org` | `deepmodeling` | 目标组织 |
| `--since` | 一年前 | 起始日期 `YYYY-MM-DD`，含当天；与 `--months` 互斥 |
| `--until` | 今天 | 结束日期 `YYYY-MM-DD`，含当天 |
| `--months` | — | 等价 `--since <N 个月前>` |
| `--include-forks` | `all` | `all` / `self`（仅生态内部）/ `none` |
| `--max-repo-size` | `0` | 仓库体积上限（MB），`0` 为不限；仅磁盘紧张时设值 |
| `--repos` | — | 只跑指定仓库，逗号分隔 |
| `--count-lines` | 关 | 统计代码增删行数 |
| `--no-fetch` | 关 | 零网络，纯本地缓存重算。API 侧数据全部为空 |
| `--refresh` | 关 | 强制重新 fetch |
| `--exclude-bots` | 关 | 把 `is_bot` 账号移出主表。默认保留并标注 |
| `--cache-dir` | `./.cache` | 缓存目录 |
| `--out-dir` | 见右 | 输出目录。默认全量 `./output/all`，`--daily` 为 `./output/daily` |
| `--daily` | 关 | 日常模式：只维护 `by_repo.csv` 与 `summary.csv`，跳过附表 |
| `--format` | `all` | `csv` / `md` / `json` / `all` |
| `--exclude-paths` | — | 行数统计的额外排除模式，逗号分隔，追加到内置列表 |
| `--jobs` | `4` | 尚未实现，传入无效果 |
| `--verbose` | 关 | 尚未实现，传入无效果 |
| `--no-events` | 关 | 跳过事件留存，退回纯统计行为 |
| `--events-db` | `./data/events.db` | 事件历史库路径 |
| `--notify` | 关 | 跑完把增量战报推送到飞书群 |
| `--notify-dry-run` | 关 | 渲染卡片打到 stdout，不发送 |
| `--notify-empty` | 关 | 无新增时也推送（默认跳过，避免刷屏） |
| `--mark-notified` | 关 | 把当前进度记为已推送基线，划掉历史积压。首次建库后用一次 |
| `--resend-last` | 关 | 重发库里最近一份战报存档，不重算增量。调通道或验排版时用 |
| `--no-notify` | 关 | 跑统计与事件留存但不推送。拆分模式的抓取那步 |
| `--only-notify` | 关 | 只算增量并推送，跳过采集管线（秒级）。拆分模式的推送那步 |
| `--strict-repos` | 关 | 有仓库处理失败时以非零退出。定时跑时建议打开 |
| `--repo-retries` | `3` | 失败仓库的重试轮数。每轮内失败会就地重试一次，故最多 6 次 |
| `--repo-retry-wait` | `30` | 只剩最后一个仓库待重试时的间隔秒数。还有别的仓库在跑时不等待 |
| `--date` | 今天 | 补推指定日期的日报（东八区）。窗口锚定当天 10:00，同一天推几次内容都一样 |
| `--weekly` | 关 | 推送上周汇总（周一跑）。按事件真实时间统计，只读库 |
| `--monthly` | 关 | 推送上月汇总（每月一号跑）。口径同 `--weekly` |

> `--no-fetch` 完全跳过 GitHub API，PR/Issue/Review/Comment 各列全为 0，email→login 映射仅靠 noreply 邮箱正则解析。适用于改时间窗快速验证 commit 口径；出正式名单用完整跑法。

## 项目结构

```
github_contributors/
├── contributors/           # 源码
│   ├── __main__.py         # 入口，python -m contributors
│   ├── main.py             # 主流程编排、身份合并、报告生成
│   ├── config.py           # CLI 参数解析、时间窗归一化
│   ├── auth.py             # token 获取（环境变量 / gh CLI）
│   ├── repos.py            # 仓库清单获取、fork 三分类、预筛
│   ├── git_stats.py        # git 管线：clone/fetch、log 解析、行数统计
│   ├── api_stats.py        # API 管线：GraphQL 查询、分页、限额退避
│   ├── identity.py         # 身份归并、bot / AI 助手识别
│   ├── cache.py            # 缓存管理、增量判断、磁盘预检
│   ├── models.py           # 共享数据结构（避免循环导入）
│   ├── output.py           # CSV / Markdown / JSON 输出
│   ├── events.py           # 事件数据结构、从两条管线提取事件
│   ├── store.py            # 事件历史库（SQLite）、去重、跨运行差集
│   └── notify/             # 飞书推送
│       ├── digest.py       # 库里的变化聚合成战报结构
│       ├── card.py         # 战报渲染成卡片 JSON（纯函数）
│       ├── period.py       # 周报月报：按事件真实时间聚合
│       └── feishu.py       # 签名、POST、重试
├── tests/                  # 449 个测试，与源码模块一一对应
├── daily_report.bat        # 计划任务入口，封装全部模式
├── 快速开始.md             # 调用手册：命令、参数、退出码
├── docs/                   # 设计文档与各期交接记录
├── .cache/                 # 运行时缓存（已忽略）
├── data/                   # 事件历史库（已忽略）
└── output/                 # 结果目录（已忽略）
    ├── daily/              #   日常模式：只有两张总表
    └── all/                #   全量模式：完整产出
```

### 模块职责与依赖方向

依赖是单向的，没有循环：

```
__main__ → main → ┬→ config
                  ├→ auth
                  ├→ repos    ─┐
                  ├→ git_stats ├→ models
                  ├→ api_stats ┤
                  ├→ identity ─┘
                  ├→ cache
                  ├→ output
                  ├→ events   ─┐
                  ├→ store    ─┘
                  └→ notify/ → (digest → card → feishu)
                               (period ──────↗)
```

`models.py` 只放数据类，被多个模块共用，本身不 import 任何业务模块 —— 
这是为了避免 `git_stats` 与 `api_stats` 互相引用。

第二期的三层同样单向：`store` 不知道飞书存在，`notify` 不知道 git 与 API 存在，
两边只通过库里的表通信。以后加网页看板或多维表格，是在 `notify/` 旁边加平级消费者，
`store` 不用改。

| 模块 | 关键职责 | 易错点 |
|------|----------|--------|
| `config.py` | 三种时间写法归一成 UTC 半开区间 `[since, until)` | 对外闭区间、对内半开，输出时须减一天还原 |
| `git_stats.py` | 镜像克隆、`git log` 解析、上游排除 | 时间过滤必须在 Python 侧按 author date 做 |
| `api_stats.py` | 按仓库分页而非按用户查询，顺带取 email→login | 子连接固定取 50 条，未检测截断 |
| `identity.py` | email→login 归并、bot 判定 | 严禁子串匹配，`botelho` 是真人 |
| `cache.py` | 增量判断、Windows 只读位处理 | `rmtree` 必须用 `onexc` 清只读位 |
| `main.py` | 编排、跨邮箱合并、同名合并、报告 | 单仓库失败不得中断整体 |
| `output.py` | 三种格式输出 | CSV 需 BOM，Markdown 表格内禁止加粗 |
| `events.py` | 事件提取，只存原始 login 不做归并 | 收集失败不得影响统计 |
| `store.py` | 去重、状态变更、跨运行差集 | 状态未变时不得记变更，否则每天重复推送 |
| `notify/` | 聚合、渲染、发送 | 签名是"密钥当 key、对空串摘要"，反直觉 |

### 主流程时序

`main.run()` 的执行顺序：

1. `_load_repos` 取仓库清单（`--no-fetch` 时读缓存）
2. `filter_repos` 按 fork 类型、体积、`pushed_at` 预筛
3. 磁盘空间预检
4. 逐仓库循环 `process_repo`，每个包在独立 `try` 里：
   - `clone_or_fetch` 建立或更新缓存
   - `collect_api_stats` 取 PR/Issue 并回填 email→login 映射
   - `collect_git_stats` 解析 commit
   - `count_upstream_excluded`（仅 fork）
   - `collect_ai_coauthors` 追溯 AI 指派人
   - `build_rows` 合并两侧数据成行
   - `merge_by_name` 同仓库内按姓名合并被拆开的身份
5. `_write_outputs` 落盘全部文件
6. `_write_meta` 写运行元数据

API 管线先于 git 管线执行是有意的：GraphQL 顺带取回的 email→login 映射
要供同一仓库的 git 侧归并使用，也累积供后续仓库使用。

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
| `commits` | int | 严格口径：只数合并进主干（分支与标签）的提交，排除合并提交，按 SHA 去重 |
| `commits_loose` | int | 宽松口径：主干 + PR 分支上主干不可达的提交。恒 ≥ `commits`，差值即 PR 分支提交数 |
| `commits_not_in_upstream` | int | 仅 fork 有意义：排除上游已有提交后的数量。上游可达的提交记 0（实测 GPUMD 窗口内 638 次提交全部来自上游，该列为 0）。非 fork 时等于 `commits` |
| `pr_created` | int | 创建的 PR 数 |
| `pr_merged` | int | 其中已合并的 PR 数 |
| `pr_reviewed` | int | 评审过的 PR 数（同一 PR 多条 review 只记一次） |
| `issue_created` | int | 创建的 Issue 数 |
| `issue_commented` | int | Issue/PR 评论数 |
| `is_fork` | bool | 该仓库是否 fork |
| `upstream` | str | 上游仓库全名，非 fork 时为空 |
| `upstream_family` | str | fork 分类：`self` / `external` / `tooling` |
| `is_bot` | bool | 是否被识别为 CI 机器人（升依赖、跑格式化等自动化） |
| `is_ai_assistant` | bool | 是否被识别为 AI 代码助手（Copilot 等） |

`is_bot` 与 `is_ai_assistant` 刻意分成两列：前者是 CI 自动化流程，后者是 AI 写的代码，
人工核对时判断依据完全不同。两类账号默认都保留在主表，只做标注不做排除 —— 从名单里
删掉一行比事后发现漏了一个人再加回去容易得多。需要移出时用 `--exclude-bots`。

开启 `--count-lines` 时追加：`additions`（新增，已过滤）、`deletions`（删除，已过滤）、`files_changed`（涉及文件数）、`additions_raw`（原始新增）、`deletions_raw`（原始删除）。关闭时这些列不出现。

### 提交数的两个口径

`commits` 与 `commits_loose` 是同一张表里的两列，每个人两列都有值，可以直接
并排比较。两列都不是"正确答案"，各有一处已知偏差，取哪列取决于名单用途。

差别来自 squash 合并。开发者在 PR 分支上提交若干次，合并时 GitHub 把它们
压成一条新提交进主干 —— 新 SHA，原始提交仍留在 `refs/pull/<n>/head` 上。
镜像克隆会把这些引用一并抓下来。

| | `commits` | `commits_loose` |
|---|---|---|
| 数据范围 | 分支与标签 | 分支、标签，加 PR 分支上主干不可达的提交 |
| squash 的工作 | 记 1 次（合并后那条） | 记 1 + N 次（合并后那条，加压缩前的 N 条） |
| 只在 PR 分支署名的人 | 0 | 实际提交数 |
| 偏差方向 | 偏低，可能漏人 | 偏高，含重复计数 |

为什么严格口径会漏人：squash 后的提交，作者字段由 GitHub 按账号设置填写，
常与开发者在本地 `git config` 里的邮箱不同。若这个邮箱没关联到 GitHub 账号，
这个人在严格口径里就只剩合并后那一条的署名，而那条的邮箱可能已归到别人名下。
实测 34 个仓库里有一半的邮箱在主干上零提交。

选哪个：

- 发感谢名单、排功劳先后 —— 用 `commits`，宁少算不错算。
- 找"有没有人被漏掉" —— 看 `commits_loose` 明显大于 `commits` 的行，
  这些人的工作大多发生在 PR 分支上，值得人工确认身份。
- 两列都为 0 但 PR/Issue 列不为 0 —— 参与方式是评审和讨论，不是写代码。

`summary.csv` 按 `commits` 降序排，也就是严格口径。

### 输出文件

```
output/all/                   # 全量模式（默认）
├── summary.csv           # 跨仓库汇总，一人一行，按 commits 降序
├── by_repo.csv           # 主表，一人一仓库一行（最细粒度）
├── repos/<name>.csv      # 按仓库拆分，可分发各项目 maintainer
├── contributors.md       # Markdown 表格，可直接贴文档公示
├── contributors.json     # 完整结构化数据，供程序消费
├── unmatched.csv         # 未能关联 GitHub 账号的身份，需人工确认
├── bots.csv              # 被识别为 bot 的账号，供核对误判
├── ai_assisted.csv       # AI 助手提交追溯到的指派人，供参考
└── run_meta.json         # 运行参数、跳过/失败仓库、API 点数消耗

output/daily/                 # 日常模式（--daily）
├── summary.csv           # 同上
├── by_repo.csv           # 同上
└── run_meta.json         # 同上
```

日常模式只维护两张总表：其余附表是全量统计时的人工核对材料，每天重写既慢
又没人看。两种模式默认写不同目录，日常跑不会覆盖全量统计的产出。

`ai_assisted.csv` 从 AI 助手提交的 `Co-authored-by` 追溯实际指派人，列为
`repo`、`name`、`email`、`ai_commits`（该指派人名下的 AI 提交数）、`guess`（同邮箱的已知账号）。
覆盖率有限：实测三个仓库共 326 次 AI 提交，138 次可追溯（约四成），其余 188 次提交信息里
没有指派人线索。两个数字都记在 `run_meta.json` 的 `ai_commits_traced` 与
`ai_commits_untraced`，所以这张表只能当人工核对的线索，不是 AI 提交的完整归属。

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

GitHub API 报的仓库体积与实际下载量无关：统计只读 commit 元数据，
blobless 克隆不拉文件内容。sciencepedia 标称 17.5 GB，缓存实占 412 MB。
因此 `--max-repo-size` 默认不限，不要用它来"省流量"。

## 每日增量与飞书推送

除了统计总量，工具还会把看到的每一条 commit / PR / issue 存进事件库
（`./data/events.db`，SQLite），据此算出"自上次汇报以来发生了什么"并推送到飞书群。

设计文档见 [`docs/第二期工程/2026-08-10-每日增量与飞书推送-设计.md`](docs/第二期工程/2026-08-10-每日增量与飞书推送-设计.md)。

### 配置

在项目根目录建 `.env`（已在 `.gitignore` 中，不会被提交）：

```
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
FEISHU_WEBHOOK_SECRET=
```

URL 来自飞书群「设置 → 群机器人 → 添加机器人 → 自定义机器人」，群主自己就能加，
不需要企业管理员审批。安全设置选「签名校验」时把飞书给的密钥填进 `SECRET`；
选「自定义关键词」时留空即可，卡片标题含 `DeepModeling` 天然命中该关键词。

### 增量怎么算

按「本次运行新看到的」判定，而不是按事件自身的时间戳。

原因是统计窗口按 author date 算（见[统计口径](#统计口径)），而 author date 是代码写成
时间，不是进入仓库时间。有人本地写了两周才推上来，这批提交的 author date 全是两周前，
按事件时间判定就永远不会出现在任何一天的战报里，但总量确实涨了。按「新看到」判定则
保证不漏，代价是战报里偶尔出现日期较早的提交。

PR 的合并是状态变更而非新增行，单独记录。上周创建、今天合并的 PR 照样会出现在今天的
战报里 —— 只看新增行的话会漏掉它，而合并恰恰是最值得说的事件。

commit 平时只计数不逐条列（一天几十条会刷屏），但当天没有任何 PR/issue 时会兜底
列出——否则整张卡片只剩一个数字，什么信息都没有。commit 行不带作者：
`EVENT_LOG_FORMAT` 只取 SHA、日期、标题三个字段。

### 行为要点

首次运行会把窗口内几千条事件全判为新增，此时只建库不推送，从第二次运行起才有意义。

昨日无更新时**照常推送**，卡片写「昨日无更新」并附一句近期活动量
（本周至今 → 上周 → 本月至今，逐级回退取第一个非零区间，只给数字不列明细）。
不发的话群里第一反应是脚本挂了。三级都为零才写「近期无活动记录」。

注意这句活动量与日报增量**不同源**：日报按「本次运行新看到的」判定，活动量按
事件真实时间查库，所以可能出现「昨日无更新」但「本周至今 113 次提交」——那些是
前几天看到的，两者不矛盾，故文案里明确写出区间名。
推送失败不影响退出码，也不会丢内容：下次运行会把两次的增量一起推出去。

事件库是派生数据，不进 git。旧事件能重爬，但「第几次运行首次看到」这个信息重建不了，
需要长期历史的话请自行备份。

### 周报与月报

除日报外还可推送区间汇总，内容是数字加活跃贡献者/仓库排行榜（周报各前 5，月报各前 10），不逐条列
（一周几百条会刷屏，周月报的价值在趋势）：

```
py -3.12 -m contributors --weekly     # 上周一 ~ 上周日
py -3.12 -m contributors --monthly    # 上月一号 ~ 月末
```

口径与日报**不同**且有意为之：日报按「本次新看到的」判定以保证不漏报，
而周月报问的是「上周发生了什么」，那是时间概念，按事件真实时间统计。
两者混用会让七天日报之和对不上周报。

周月边界按东八区算再转 UTC 查库——直接拿 UTC 日界当周界，会把周一
早八点前的事件算到上一周去。

两者都只读事件库，秒级完成，不跑采集管线，也不影响日报的增量基线。

### 计划任务

`daily_report.bat`（仓库根目录）封装了全部模式，日志写进
`logs/daily-<日期>.log`：

| 命令 | 作用 | 耗时 |
|---|---|---|
| `daily_report.bat --fetch` | 抓取并入库，不推送 | 约 25 分钟（38 个仓库） |
| `daily_report.bat --push` | 只算增量并推送 | 数秒 |
| `daily_report.bat --daily` | 抓取成功才推送 | 约 25 分钟 |
| `daily_report.bat --weekly` | 推上周汇总 | 数秒 |
| `daily_report.bat --monthly` | 推上月汇总 | 数秒 |
| `daily_report.bat --dry` | 只渲染不发送 | 约 25 分钟 |

四个计划任务：

```
schtasks /create /tn "DM日报-抓取" /tr "C:\...\daily_report.bat --fetch"   /sc daily   /st 10:00
schtasks /create /tn "DM日报-推送" /tr "C:\...\daily_report.bat --push"    /sc daily   /st 11:03
schtasks /create /tn "DM周报"      /tr "C:\...\daily_report.bat --weekly"  /sc weekly  /d MON /st 11:15
schtasks /create /tn "DM月报"      /tr "C:\...\daily_report.bat --monthly" /sc monthly /d 1   /st 11:30
```

抓取与推送分成两个任务是因为抓取耗时不可控（受当天新提交量与网络影响，
实测 24～28 分钟），而推送只读库、秒级完成，可以准点。两者之间留一小时
余量。若不在乎准点，用 `--daily` 一个任务串联即可。

必须以当前用户身份运行——token 来自 `gh` 的 keyring，换用户取不到。
机器关机或休眠时任务不会跑，这是本机方案的固有限制。

飞书对单个机器人限流 100 次/分钟、5 次/秒，官方文档建议避开 10:00、
17:30 这类整点半点。故推送时刻取 11:03 而非 11:00；抓取那步不发消息，
没有这个约束。

### 仓库失败时

失败的仓库会就地重试一次，然后立刻跑下一个仓库——不等待。一轮跑完再
回头重跑失败的那几个，共 3 轮，故每个仓库最多尝试 6 次。只重跑失败的
那几个而不是重来整个流程——38 个仓库跑一次 25 分钟，整体重试代价太大。

两个例外：撞上限流时不就地重试（立刻再打一次只会加深限流），直接留到
下一轮；只剩最后一个仓库待重试时，两次尝试之间隔 30 秒，因为此时没有
别的仓库能把它们隔开。

仍失败的仓库会写进卡片：「未能抓取：X（其增量将累计到明天的日报）」。
这句是真的：增量按首次看到判定，今天没抓到的仓库明天抓到时那些事件
才首次入库，自然进明天的日报，不丢也不重复。

连续 3 次抓取失败的仓库会升级为单独一行告警：「⚠️ 连续抓取失败：X（3 次），
可能是改名、删除或权限变更，请检查」。分开写是因为语义不同——「累计到明天」
对这类仓库不再成立：改名、删除或权限变更永远不会自愈，必须有人去看。
判定只看真正跑了采集的运行，拆分模式的推送步骤不参与计数。

数据缺失的提醒按窗口判定：窗口内补跑成功了就不再提醒；窗口结束后才补跑的
不算——那些数据要等第二天的日报，此时撤掉提醒会让当天的日报既不提醒、
也没有那个仓库的内容。

### 日报的时间窗

日报窗口锚定当天 10:00（东八区）：8-13 的日报固定是 8-12 10:00 ~ 8-13 10:00，
**由日期决定，与实际推送时刻无关**。所以抓取与推送可以分开跑，同一天推几次
内容都一样，窗口内的任何补跑都会自动计入。

```bash
python -m contributors --only-notify              # 推今天的日报
python -m contributors --only-notify --date 2026-08-12   # 补推那天的
```

窗口切的是**入库时间**（事件何时被抓到），不是 git 时间。这个区别很重要：
commit 的时间戳是 author date，即代码写成的时间。有人本地攒两周才 push，
按 git 时间切窗会让那些提交落进两周前的窗口——而那天的日报早发过了，
于是永远不会出现在任何一份日报里。按入库时间就没这个问题：不管代码什么时候
写的，今天首次看到就今天报。

代价是同一份日报里的事件，git 时间可能跨越十几个小时甚至几天。要按真实时间
统计请看周报月报——它们按 `event_time` 切窗，因为「上周发生了什么」本来就是
时间概念。两种口径不同是有意的，所以七天日报之和不等于周报。

首次建库那次会把整年历史一次性录入（实测 13915 条），它们的入库时刻都落在
建库当天，只按窗口取会被当成当天动态推出去。`--mark-notified` 划下的基线会
挡住这批，此后正常运行不受影响。普通的推送成功**不会**成为基线——否则同一天
推两次内容就不一样了。

一个仓库连不上不会拦住整条日报——那会让其余 37 个仓库的动态一起发不
出去。整次运行失败（拿不到仓库清单、断网）才会跳过推送。

## 统计口径

- **时间**：闭区间（含两端），统一按 UTC 判定。提交以 **author date** 为准而非 committer date（rebase/cherry-pick/squash 会刷新后者，导致旧代码被算进新窗口）
- **分支**：分两趟统计成两列。`commits` 走 `--branches --tags`（主干分支与标签）；`commits_loose` 额外一趟 `--all --not --branches --tags`，取 PR 分支上主干不可达的提交。两者均排除合并提交（`--no-merges`）。详见[提交数的两个口径](#提交数的两个口径)
- **去重**：Git 侧按 SHA 去重 —— 只保证同一趟内同一 SHA 不重复。squash/cherry-pick 产生新 SHA，跨趟无法去重，这正是两个口径分列而非相加的原因；API 侧同一 PR 内多条 review 只记一次、PR 作者 review 自己的 PR 不计
- **bot 识别**：仅按 login 与 name 判定。规则：[bot] 后缀 + 显式黑名单（`codecov`、`github-actions` 等）。禁止子串匹配（`botelho` 是真人）。默认保留在主表并标注 `is_bot`，`--exclude-bots` 可移出，同时始终输出 `bots.csv` 供核对误判
- **AI 代码助手**：与 bot 同样只按 login 与 name 精确判定，禁止子串匹配（`copilotkid` 是真人）。默认保留在主表并标注 `is_ai_assistant`，不受 `--exclude-bots` 影响
- **未关联身份**：查不到 GitHub 账号的邮箱不会丢弃，记入 `unmatched.csv`。漏掉一个真实贡献者比多算一个严重
- **行数**：默认关闭，不宜用于排名。易被生成文件（lock 文件、vendored 代码、数据文件）污染；行数与贡献价值关联弱。已内置排除模式（`*.lock`、`vendor/**`、`*.npy` 等）并同时输出过滤前后两组数字

## 已知问题

以下几条不影响数据正确性，但值得知道。

- `--jobs` 与 `--verbose` 已定义但未实现，传入无效果。
- 首次运行的磁盘预检按 GitHub 标称体积估算，实测偏差最大 26 倍，
  可能虚报需求把新用户挡下。绕法见快速开始文档。
- GraphQL 子连接固定取 50 条不翻页。实测 34 个仓库：`commits` 子连接
  408 处（2.3%）、`reviews` 18 处（0.1%）达到上限，两个 `comments`
  子连接零撞限。影响可忽略 —— `commits` 只用于抓 email→login 映射不参与
  计数，而 `pr_reviewed` 按 PR 内评审人去重，那 18 个 PR 去重后仅 2–7 人，
  截断处已覆盖全部评审人。
- `summarize` 跨仓库合并时姓名取首次出现值，少数人的显示名可能是 login
  或 `root` 而非真名。仅影响展示，计数字段不受影响。

## 开发

```bash
python -m pytest -q          # 449 个测试，不联网
```

Windows 上终端中文乱码时加 `PYTHONIOENCODING=utf-8` 前缀，文件内容不受影响。

改动生产代码前先写失败测试 —— 项目一直按 TDD 走，历史上几个只在真实数据
上才暴露的缺陷（时间窗按 committer date 过滤、身份被拆成两行、fork 上游
排除失效、PR 分支提交重复计数）都是先写失败测试再修的。

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
