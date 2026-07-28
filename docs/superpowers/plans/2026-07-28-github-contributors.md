# DeepModeling 贡献者统计脚本 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 爬取 GitHub 组织下所有仓库的贡献者名单（id/姓名/邮箱/链接 + commit/PR/issue 数），按仓库分类，支持显式时间区间，用于感谢与表彰贡献者。

**Architecture:** 两条数据管线汇合成一张表。Git 管线用 `--mirror --filter=blob:none` 克隆到本地缓存，`git log --all` 取 commit 数与邮箱（所有分支，按 SHA 天然去重）；API 管线用 GraphQL 取 PR/Issue/Review/Comment 计数。两者以 GitHub login 为连接键，经身份归并层合并后输出 CSV/Markdown/JSON 三种格式。首次全量克隆，之后增量 fetch；改时间窗时零网络纯本地重算。

**Tech Stack:** Python 3.12、requests、python-dateutil、pytest、responses。外部命令依赖 git 与 gh CLI。

**Spec:** `docs/superpowers/specs/2026-07-28-github-contributors-design.md`

## Global Constraints

以下约束适用于每一个 task，不再逐条重复：

- Python 3.12.9，Windows 11 + PowerShell 环境（路径处理一律用 `pathlib.Path`，不手拼 `/`）
- 时区统一按 UTC 判定。git commit 时间戳带作者本地时区，GitHub API 返回 UTC，两者口径不一会使边界日期提交错位
- 时间区间为闭区间，两端都含。`--until 2025-12-31` 内部转为 `< 2026-01-01 00:00 UTC`
- Markdown 输出的表格单元格内一律纯文本，禁止使用 `**` 加粗（用户全局约定）。需要强调时用列的语义或 emoji
- 永不静默丢数据：任何无法归并的身份进 `unmatched.csv`，任何失败仓库进 `run_meta.json` 的 `failures` 并在终端打印数量
- bot 识别只允许 `[bot]` 后缀匹配 + 显式黑名单，禁止对 "bot" 做子串匹配（`botelho`、`Botspot` 是真实用户）
- 每个 task 结束时必须 commit，commit message 用中文，格式 `<type>: <描述>`
- 测试命令统一 `pytest -m "not network"`；真实网络测试打 `@pytest.mark.network` 标记，默认跳过
- 不使用 `GET /search/commits` 反查邮箱（限额 30 次/分钟，不可行）

## File Structure

```
contributors/
├── __init__.py           # 空，标记包
├── __main__.py           # 入口，python -m contributors
├── config.py             # CLI 解析、时间窗计算、Config dataclass
├── models.py             # 共享 dataclass：RepoInfo / Contributor / GitStats / ApiStats
├── auth.py              # token 获取（环境变量 → gh auth token）
├── repos.py              # 仓库清单、fork 三分类、pushed_at 与体积预筛
├── cache.py              # 缓存路径、增量判断、断点续跑 progress.json
├── git_stats.py          # mirror clone/fetch、git log 解析、行数统计
├── api_stats.py          # GraphQL 查询、配额管理、退避重试
├── identity.py           # 身份归并、bot 识别、email→login 反查
├── output.py             # CSV / Markdown / JSON 输出
└── main.py               # 编排流程、进度、错误边界

tests/
├── conftest.py
├── fixtures/
│   ├── git_log_sample.txt
│   ├── numstat_sample.txt
│   └── graphql_prs.json
├── test_config.py
├── test_repos.py
├── test_identity.py
├── test_git_stats.py
├── test_api_stats.py
├── test_output.py
└── test_integration.py

requirements.txt
README.md
```

拆分理由：`models.py` 单独放共享数据结构，避免 `git_stats` 与 `api_stats` 循环导入；`auth.py` 从 `config.py` 拆出，因为 token 获取涉及子进程调用，需独立 mock；其余按 spec 第 3.3 节的模块划分。

---

### Task 1: 项目骨架与共享数据模型

**Files:**
- Create: `requirements.txt`
- Create: `contributors/__init__.py`
- Create: `contributors/models.py`
- Create: `tests/conftest.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: 无（首个 task）
- Produces: `RepoInfo`、`GitStats`、`ApiStats`、`Contributor` 四个 dataclass，供后续所有 task 使用。字段与类型见下方 Step 3 代码

- [ ] **Step 1: 写失败测试**

创建 `tests/test_models.py`：

```python
from contributors.models import RepoInfo, GitStats, ApiStats, Contributor


def test_repoinfo_holds_fork_metadata():
    r = RepoInfo(
        name="abacus-develop",
        default_branch="develop",
        size_mb=165.3,
        pushed_at="2026-07-27T00:00:00Z",
        is_fork=True,
        upstream="abacusmodeling/abacus-develop",
        upstream_family="self",
        archived=False,
    )
    assert r.name == "abacus-develop"
    assert r.is_fork is True
    assert r.upstream_family == "self"


def test_gitstats_defaults_to_zero():
    g = GitStats()
    assert g.commits == 0
    assert g.commits_not_in_upstream == 0
    assert g.additions is None  # None 表示未开启 --count-lines
    assert g.emails == set()


def test_apistats_defaults_to_zero():
    a = ApiStats()
    assert a.pr_created == 0
    assert a.pr_merged == 0
    assert a.pr_reviewed == 0
    assert a.issue_created == 0
    assert a.issue_commented == 0


def test_contributor_github_url_derived_from_login():
    c = Contributor(login="njzjz", name="Jinzhe Zeng")
    assert c.github_url == "https://github.com/njzjz"


def test_contributor_without_login_has_empty_url():
    c = Contributor(login=None, name="Anon")
    assert c.github_url == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors'`

- [ ] **Step 3: 写最小实现**

创建 `requirements.txt`：

```
requests>=2.31
python-dateutil>=2.9
pytest>=8.0
responses>=0.25
```

创建空文件 `contributors/__init__.py`。

创建 `contributors/models.py`：

```python
"""共享数据结构。放在独立模块避免 git_stats 与 api_stats 循环导入。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RepoInfo:
    """单个仓库的元数据，来自 REST API。"""
    name: str
    default_branch: str
    size_mb: float
    pushed_at: str          # ISO8601 UTC 字符串，如 2026-07-27T00:00:00Z
    is_fork: bool
    upstream: Optional[str]        # 上游 full_name，非 fork 时为 None
    upstream_family: Optional[str]  # self / external / tooling，非 fork 时为 None
    archived: bool


@dataclass
class GitStats:
    """某人在某仓库的 git 侧统计。"""
    commits: int = 0
    commits_not_in_upstream: int = 0
    emails: set = field(default_factory=set)
    names: set = field(default_factory=set)
    # 以下仅在 --count-lines 开启时填充，否则保持 None（输出时整列省略）
    additions: Optional[int] = None
    deletions: Optional[int] = None
    files_changed: Optional[int] = None
    additions_raw: Optional[int] = None
    deletions_raw: Optional[int] = None


@dataclass
class ApiStats:
    """某人在某仓库的 API 侧统计。"""
    pr_created: int = 0
    pr_merged: int = 0
    pr_reviewed: int = 0
    issue_created: int = 0
    issue_commented: int = 0


@dataclass
class Contributor:
    """归并后的一个人。"""
    login: Optional[str]
    name: str = ""
    is_bot: bool = False

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.login}" if self.login else ""
```

创建 `tests/conftest.py`：

```python
import sys
from pathlib import Path

# 让测试无需安装即可 import contributors
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_models.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add requirements.txt contributors/__init__.py contributors/models.py tests/conftest.py tests/test_models.py
git commit -m "feat: 添加项目骨架与共享数据模型"
```

---

### Task 2: 时间窗与 CLI 参数解析

**Files:**
- Create: `contributors/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `resolve_window(since: str|None, until: str|None, months: int|None, today: date) -> tuple[datetime, datetime]` — 返回 UTC 起止时间，起点含当天 00:00，终点为次日 00:00（半开区间实现闭区间语义）
  - `Config` dataclass — 含 spec 5.2 全部参数字段
  - `parse_args(argv: list[str], today: date) -> Config`
  - `DEFAULT_EXCLUDE_PATHS: list[str]` — 行数统计内置排除模式

- [ ] **Step 1: 写失败测试**

创建 `tests/test_config.py`：

```python
from datetime import date, datetime, timezone
import pytest
from contributors.config import resolve_window, parse_args, DEFAULT_EXCLUDE_PATHS

TODAY = date(2026, 7, 28)


def test_default_window_is_one_year_back_to_today():
    since, until = resolve_window(None, None, None, TODAY)
    assert since == datetime(2025, 7, 28, tzinfo=timezone.utc)
    # 闭区间：含 2026-07-28 全天，故内部上界为次日零点
    assert until == datetime(2026, 7, 29, tzinfo=timezone.utc)


def test_explicit_since_overrides_default():
    since, until = resolve_window("2025-01-01", None, None, TODAY)
    assert since == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_closed_interval_includes_until_day_fully():
    since, until = resolve_window("2025-01-01", "2025-12-31", None, TODAY)
    assert until == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_months_shorthand_equals_since():
    since, _ = resolve_window(None, None, 6, TODAY)
    assert since == datetime(2026, 1, 28, tzinfo=timezone.utc)


def test_months_handles_month_end_rollback():
    # 3 月 31 日回退一个月应落到 2 月 28/29，而非报错
    since, _ = resolve_window(None, None, 1, date(2026, 3, 31))
    assert since == datetime(2026, 2, 28, tzinfo=timezone.utc)


def test_months_and_since_are_mutually_exclusive():
    with pytest.raises(ValueError, match="互斥"):
        resolve_window("2025-01-01", None, 6, TODAY)


def test_invalid_date_format_raises():
    with pytest.raises(ValueError, match="日期格式"):
        resolve_window("2025/01/01", None, None, TODAY)


def test_since_after_until_raises():
    with pytest.raises(ValueError, match="晚于"):
        resolve_window("2026-01-01", "2025-01-01", None, TODAY)


def test_parse_args_defaults_match_spec():
    c = parse_args([], TODAY)
    assert c.org == "deepmodeling"
    assert c.include_forks == "all"
    assert c.max_repo_size == 2048
    assert c.count_lines is False
    assert c.include_bots is False
    assert c.no_fetch is False
    assert c.refresh is False
    assert c.fmt == "all"
    assert c.jobs == 4
    assert c.verbose is False
    assert c.repos == []


def test_parse_args_repos_splits_on_comma():
    c = parse_args(["--repos", "dpdata,DeePMD-kit"], TODAY)
    assert c.repos == ["dpdata", "DeePMD-kit"]


def test_parse_args_exclude_paths_extends_builtin():
    c = parse_args(["--exclude-paths", "docs/**"], TODAY)
    assert "docs/**" in c.exclude_paths
    for p in DEFAULT_EXCLUDE_PATHS:
        assert p in c.exclude_paths


def test_parse_args_count_lines_flag_enables():
    c = parse_args(["--count-lines"], TODAY)
    assert c.count_lines is True


def test_builtin_excludes_cover_generated_files():
    joined = " ".join(DEFAULT_EXCLUDE_PATHS)
    assert "*.lock" in joined
    assert "vendor/**" in joined
    assert "third_party/**" in joined
    assert "*.min.js" in joined
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.config'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/config.py`：

```python
"""CLI 参数解析与时间窗计算。

时间窗语义：对外是闭区间（两端都含），内部用半开区间 [since, until) 实现，
因为 git log --until 与 GraphQL 的边界处理都更适合半开区间。
"""
import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from dateutil.relativedelta import relativedelta

DEFAULT_EXCLUDE_PATHS = [
    "*.lock",
    "*-lock.json",
    "package-lock.json",
    "poetry.lock",
    "vendor/**",
    "third_party/**",
    "thirdparty/**",
    "*.min.js",
    "*.min.css",
    "*.svg",
    "*.pdf",
    # 科学计算仓库常见的大体积数据文件
    "*.npy",
    "*.npz",
    "*.h5",
    "*.hdf5",
    "*.cif",
    "*.pdb",
    "*.xyz",
]


def _parse_day(s: str, label: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"{label} 日期格式必须为 YYYY-MM-DD，收到 {s!r}"
        ) from exc


def resolve_window(
    since: Optional[str],
    until: Optional[str],
    months: Optional[int],
    today: date,
) -> tuple[datetime, datetime]:
    """把三种时间写法归一成 UTC 半开区间 [since_dt, until_dt)。

    today 作为参数传入而非内部取 now()，使测试可复现。
    """
    if since is not None and months is not None:
        raise ValueError("--since 与 --months 互斥，只能给一个")

    if since is not None:
        since_day = _parse_day(since, "--since")
    elif months is not None:
        if months <= 0:
            raise ValueError("--months 必须为正整数")
        # relativedelta 自动处理月末回退：3-31 减一月 → 2-28/29
        since_day = today - relativedelta(months=months)
    else:
        since_day = today - relativedelta(years=1)

    until_day = _parse_day(until, "--until") if until is not None else today

    if since_day > until_day:
        raise ValueError(f"起始日期 {since_day} 晚于结束日期 {until_day}")

    since_dt = datetime.combine(since_day, time.min, tzinfo=timezone.utc)
    # 闭区间语义：含 until_day 全天，故上界取次日零点
    until_dt = datetime.combine(
        until_day + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return since_dt, until_dt


@dataclass
class Config:
    org: str
    since: datetime
    until: datetime
    include_forks: str
    max_repo_size: int
    repos: list = field(default_factory=list)
    count_lines: bool = False
    exclude_paths: list = field(default_factory=list)
    no_fetch: bool = False
    refresh: bool = False
    include_bots: bool = False
    cache_dir: str = "./.cache"
    out_dir: str = "./output"
    fmt: str = "all"
    jobs: int = 4
    verbose: bool = False


def parse_args(argv: list, today: Optional[date] = None) -> Config:
    if today is None:
        today = datetime.now(timezone.utc).date()

    p = argparse.ArgumentParser(
        prog="python -m contributors",
        description="爬取 GitHub 组织的贡献者名单，用于感谢与表彰",
    )
    p.add_argument("--org", default="deepmodeling", help="目标组织")
    p.add_argument("--since", help="起始日期 YYYY-MM-DD，含当天；默认一年前")
    p.add_argument("--until", help="结束日期 YYYY-MM-DD，含当天；默认今天")
    p.add_argument("--months", type=int, help="便捷写法，与 --since 互斥")
    p.add_argument(
        "--include-forks", choices=["all", "self", "none"], default="all"
    )
    p.add_argument("--max-repo-size", type=int, default=2048, help="MB")
    p.add_argument("--repos", default="", help="只跑指定仓库，逗号分隔")
    p.add_argument("--count-lines", action="store_true")
    p.add_argument(
        "--exclude-paths", default="", help="行数统计额外排除模式，逗号分隔"
    )
    p.add_argument("--no-fetch", action="store_true", help="零网络，纯本地缓存")
    p.add_argument("--refresh", action="store_true", help="强制重新 fetch")
    p.add_argument("--include-bots", action="store_true")
    p.add_argument("--cache-dir", default="./.cache")
    p.add_argument("--out-dir", default="./output")
    p.add_argument(
        "--format", dest="fmt", choices=["csv", "md", "json", "all"], default="all"
    )
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--verbose", action="store_true")

    a = p.parse_args(argv)
    since_dt, until_dt = resolve_window(a.since, a.until, a.months, today)

    extra = [s.strip() for s in a.exclude_paths.split(",") if s.strip()]
    return Config(
        org=a.org,
        since=since_dt,
        until=until_dt,
        include_forks=a.include_forks,
        max_repo_size=a.max_repo_size,
        repos=[s.strip() for s in a.repos.split(",") if s.strip()],
        count_lines=a.count_lines,
        exclude_paths=DEFAULT_EXCLUDE_PATHS + extra,
        no_fetch=a.no_fetch,
        refresh=a.refresh,
        include_bots=a.include_bots,
        cache_dir=a.cache_dir,
        out_dir=a.out_dir,
        fmt=a.fmt,
        jobs=a.jobs,
        verbose=a.verbose,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_config.py -v`
Expected: 13 passed

- [ ] **Step 5: 提交**

```bash
git add contributors/config.py tests/test_config.py
git commit -m "feat: 实现时间窗计算与 CLI 参数解析"
```

---

### Task 3: token 获取与 fork 三分类

**Files:**
- Create: `contributors/auth.py`
- Create: `contributors/repos.py`
- Test: `tests/test_repos.py`

**Interfaces:**
- Consumes: `RepoInfo`（Task 1）、`Config`（Task 2）
- Produces:
  - `get_token() -> str` — 环境变量优先，回退 `gh auth token`
  - `classify_upstream(upstream: str|None) -> str|None` — 返回 `self`/`external`/`tooling`/None
  - `SELF_ORGS: set[str]`、`TOOLING_UPSTREAMS: set[str]`
  - `should_skip(repo: RepoInfo, cfg: Config) -> str|None` — 返回跳过原因字符串，不跳过则 None
  - `filter_repos(repos: list[RepoInfo], cfg: Config) -> tuple[list[RepoInfo], dict[str,str]]` — 返回待处理仓库与 {仓库名: 跳过原因}
  - `fetch_repos(org: str, token: str) -> list[RepoInfo]` — REST 拉全量仓库元数据

- [ ] **Step 1: 写失败测试**

创建 `tests/test_repos.py`：

```python
from datetime import datetime, timezone
import pytest
from contributors.models import RepoInfo
from contributors.config import Config
from contributors.repos import classify_upstream, should_skip, filter_repos


def mk_repo(name="r", size_mb=10.0, pushed="2026-07-01T00:00:00Z",
            is_fork=False, upstream=None, archived=False, branch="main"):
    return RepoInfo(
        name=name, default_branch=branch, size_mb=size_mb, pushed_at=pushed,
        is_fork=is_fork, upstream=upstream,
        upstream_family=classify_upstream(upstream), archived=archived,
    )


def mk_cfg(**kw):
    base = dict(
        org="deepmodeling",
        since=datetime(2025, 7, 28, tzinfo=timezone.utc),
        until=datetime(2026, 7, 29, tzinfo=timezone.utc),
        include_forks="all", max_repo_size=2048,
    )
    base.update(kw)
    return Config(**base)


# --- fork 三分类，用 spec 第 2.1 节的真实数据 ---

@pytest.mark.parametrize("upstream", [
    "abacusmodeling/abacus-develop",
    "dptech-corp/dpgen2",
    "dptech-corp/Uni-Fold-jax",
    "mzjb/DeepH-pack",
    "abacusmodeling/LibRI",
    "deepflameCFD/deepflame-dev",
])
def test_ecosystem_upstreams_are_self(upstream):
    assert classify_upstream(upstream) == "self"


@pytest.mark.parametrize("upstream", [
    "lammps/lammps",
    "plumed/plumed2",
    "brucefan1983/GPUMD",
    "DEShawResearch/msys",
    "weihuayi/fealpy",
    "Roy-Kid/ADMP",
])
def test_external_project_upstreams_are_external(upstream):
    assert classify_upstream(upstream) == "external"


@pytest.mark.parametrize("upstream", [
    "platisd/clang-tidy-pr-comments",
    "jitterbit/get-changed-files",
    "zjgemi/argo-workflows",
    "deepmd-kit-recipes/deepmd-kit-recipes",
])
def test_ci_tooling_upstreams_are_tooling(upstream):
    assert classify_upstream(upstream) == "tooling"


def test_non_fork_has_no_family():
    assert classify_upstream(None) is None


# --- pushed_at 预筛 ---

def test_repo_pushed_before_since_is_skipped():
    # lammps 停在 2024-04-11，一年窗口内不可能有 commit
    r = mk_repo(name="lammps", pushed="2024-04-11T00:00:00Z")
    assert should_skip(r, mk_cfg()) == "pushed_at 早于统计起点"


def test_repo_pushed_on_boundary_day_is_kept():
    # 边界同日不跳过：当天可能有窗口内的提交
    r = mk_repo(pushed="2025-07-28T12:00:00Z")
    assert should_skip(r, mk_cfg()) is None


def test_repo_pushed_after_since_is_kept():
    r = mk_repo(pushed="2026-06-09T00:00:00Z")
    assert should_skip(r, mk_cfg()) is None


# --- 体积阈值 ---

def test_oversized_repo_is_skipped():
    r = mk_repo(name="sciencepedia", size_mb=17563.2)
    assert "体积" in should_skip(r, mk_cfg())


def test_oversized_repo_included_when_threshold_raised():
    r = mk_repo(name="sciencepedia", size_mb=17563.2)
    assert should_skip(r, mk_cfg(max_repo_size=20000)) is None


# --- fork 筛选三档 ---

def test_include_forks_none_skips_all_forks():
    r = mk_repo(is_fork=True, upstream="abacusmodeling/abacus-develop")
    assert should_skip(r, mk_cfg(include_forks="none")) == "fork 已按 --include-forks=none 排除"


def test_include_forks_self_keeps_ecosystem_fork():
    r = mk_repo(is_fork=True, upstream="abacusmodeling/abacus-develop")
    assert should_skip(r, mk_cfg(include_forks="self")) is None


def test_include_forks_self_skips_external_fork():
    r = mk_repo(is_fork=True, upstream="lammps/lammps",
                pushed="2026-07-01T00:00:00Z")
    assert should_skip(r, mk_cfg(include_forks="self")) is not None


def test_include_forks_all_keeps_external_fork():
    r = mk_repo(is_fork=True, upstream="brucefan1983/GPUMD",
                pushed="2026-06-09T00:00:00Z")
    assert should_skip(r, mk_cfg(include_forks="all")) is None


# --- --repos 白名单 ---

def test_repos_whitelist_skips_others():
    r = mk_repo(name="dpdata")
    assert should_skip(r, mk_cfg(repos=["DeePMD-kit"])) == "不在 --repos 指定范围内"


def test_repos_whitelist_keeps_listed():
    r = mk_repo(name="dpdata")
    assert should_skip(r, mk_cfg(repos=["dpdata"])) is None


# --- filter_repos 汇总 ---

def test_filter_repos_returns_kept_and_reasons():
    repos = [
        mk_repo(name="dpdata", pushed="2026-07-01T00:00:00Z"),
        mk_repo(name="lammps", pushed="2024-04-11T00:00:00Z"),
        mk_repo(name="sciencepedia", size_mb=17563.2),
    ]
    kept, skipped = filter_repos(repos, mk_cfg())
    assert [r.name for r in kept] == ["dpdata"]
    assert set(skipped) == {"lammps", "sciencepedia"}
    assert all(isinstance(v, str) and v for v in skipped.values())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_repos.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.repos'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/auth.py`：

```python
"""GitHub token 获取。独立模块因为涉及子进程，需单独 mock。"""
import os
import shutil
import subprocess


class AuthError(RuntimeError):
    pass


def get_token() -> str:
    """环境变量优先，回退 gh auth token。"""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val

    if shutil.which("gh") is None:
        raise AuthError(
            "未找到 token。请设置环境变量 GITHUB_TOKEN，"
            "或安装 gh CLI 并执行 gh auth login"
        )
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise AuthError(
            "gh auth token 执行失败。请先执行 gh auth login，"
            "或设置环境变量 GITHUB_TOKEN"
        ) from exc
    token = out.stdout.strip()
    if not token:
        raise AuthError("gh auth token 返回空值，请重新执行 gh auth login")
    return token
```

创建 `contributors/repos.py`：

```python
"""仓库清单获取、fork 三分类、预筛。

fork 分类依据 spec 第 2.1 节的实测结论：
- self：上游属 DeepModeling 生态，是同社区跨账号迁移，应纳入
- external：真正的外部项目，全量统计会让上游开发者淹没本社区贡献者
- tooling：CI 流程工具 fork，上游作者列入表彰名单无意义
"""
from datetime import datetime, timezone
from typing import Optional

import requests

from .models import RepoInfo

# 上游账号属于 DeepModeling 生态
SELF_ORGS = {
    "deepmodeling",
    "abacusmodeling",
    "dptech-corp",
    "deepflamecfd",
    "mzjb",
}

# CI/流程工具类上游，按 full_name 精确匹配
TOOLING_UPSTREAMS = {
    "platisd/clang-tidy-pr-comments",
    "jitterbit/get-changed-files",
    "zjgemi/argo-workflows",
    "argoproj/argo-workflows",
    "deepmd-kit-recipes/deepmd-kit-recipes",
    "conda-forge/staged-recipes",
}


def classify_upstream(upstream: Optional[str]) -> Optional[str]:
    if not upstream:
        return None
    lower = upstream.lower()
    if lower in TOOLING_UPSTREAMS:
        return "tooling"
    owner = lower.split("/", 1)[0]
    if owner in SELF_ORGS:
        return "self"
    return "external"


def _parse_iso(s: str) -> datetime:
    # GitHub 返回 2026-07-27T14:34:09Z，fromisoformat 在 3.11+ 支持 Z
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def should_skip(repo: RepoInfo, cfg) -> Optional[str]:
    """返回跳过原因；不跳过返回 None。顺序即优先级。"""
    if cfg.repos and repo.name not in cfg.repos:
        return "不在 --repos 指定范围内"

    if repo.is_fork:
        if cfg.include_forks == "none":
            return "fork 已按 --include-forks=none 排除"
        if cfg.include_forks == "self" and repo.upstream_family != "self":
            return (
                f"fork 上游类型为 {repo.upstream_family}，"
                "已按 --include-forks=self 排除"
            )

    if repo.size_mb > cfg.max_repo_size:
        return (
            f"体积 {repo.size_mb:.0f} MB 超过 --max-repo-size "
            f"{cfg.max_repo_size} MB"
        )

    # pushed_at 早于起点 → 窗口内不可能有 commit，连克隆都省掉。
    # 用日期粒度比较，避免同日不同时刻被误跳。
    if _parse_iso(repo.pushed_at).date() < cfg.since.date():
        return "pushed_at 早于统计起点"

    return None


def filter_repos(repos: list, cfg) -> tuple:
    kept, skipped = [], {}
    for r in repos:
        reason = should_skip(r, cfg)
        if reason:
            skipped[r.name] = reason
        else:
            kept.append(r)
    return kept, skipped


def fetch_repos(org: str, token: str) -> list:
    """拉取组织全部仓库元数据。REST 比 GraphQL 简单且分页可靠。"""
    out, page = [], 1
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    while True:
        resp = requests.get(
            f"https://api.github.com/orgs/{org}/repos",
            params={"per_page": 100, "page": page, "type": "all"},
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for j in batch:
            upstream = (j.get("parent") or {}).get("full_name")
            # 列表端点不返回 parent，fork 的上游需单独查详情
            if j.get("fork") and not upstream:
                d = requests.get(
                    j["url"], headers=headers, timeout=30
                )
                d.raise_for_status()
                upstream = (d.json().get("parent") or {}).get("full_name")
            out.append(RepoInfo(
                name=j["name"],
                default_branch=j.get("default_branch") or "main",
                size_mb=round(j.get("size", 0) / 1024, 1),
                pushed_at=j.get("pushed_at") or "1970-01-01T00:00:00Z",
                is_fork=bool(j.get("fork")),
                upstream=upstream,
                upstream_family=classify_upstream(upstream),
                archived=bool(j.get("archived")),
            ))
        page += 1
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_repos.py -v`
Expected: 29 passed（19 个测试函数，其中 3 个 parametrize 展开为 16 个用例）

- [ ] **Step 5: 提交**

```bash
git add contributors/auth.py contributors/repos.py tests/test_repos.py
git commit -m "feat: 实现 token 获取与 fork 三分类预筛"
```

---

### Task 4: 身份归并与 bot 识别

**Files:**
- Create: `contributors/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: `Contributor`（Task 1）
- Produces:
  - `parse_noreply_login(email: str) -> str|None` — 从 noreply 邮箱解析 login，零 API 成本
  - `is_bot(login: str|None, name: str, email: str) -> bool`
  - `BOT_BLOCKLIST: set[str]`
  - `normalize_email(email: str) -> str`
  - `IdentityResolver` 类，方法 `add_mapping(email, login)`、`resolve(email) -> str|None`、`merge_key(email, login) -> str`、`unmatched_emails() -> set[str]`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_identity.py`：

```python
import pytest
from contributors.identity import (
    parse_noreply_login, is_bot, normalize_email, IdentityResolver,
)


# --- noreply 邮箱解析（零成本路径，覆盖率最高）---

def test_parse_noreply_with_numeric_prefix():
    # 实测最常见格式
    assert parse_noreply_login(
        "49699333+dependabot[bot]@users.noreply.github.com"
    ) == "dependabot[bot]"


def test_parse_noreply_without_prefix():
    assert parse_noreply_login("njzjz@users.noreply.github.com") == "njzjz"


def test_parse_noreply_is_case_insensitive_on_domain():
    assert parse_noreply_login("njzjz@Users.NoReply.GitHub.com") == "njzjz"


def test_parse_non_noreply_returns_none():
    assert parse_noreply_login("someone@gmail.com") is None


def test_parse_empty_returns_none():
    assert parse_noreply_login("") is None


# --- bot 识别：只认后缀与黑名单，绝不子串匹配 ---

def test_bracket_bot_suffix_is_bot():
    assert is_bot("dependabot[bot]", "dependabot[bot]", "x@y.com") is True
    assert is_bot("pre-commit-ci[bot]", "", "x@y.com") is True
    assert is_bot("github-actions[bot]", "", "x@y.com") is True


def test_blocklisted_bot_without_suffix_is_bot():
    # 实测发现：njzjz-bot 是真实 bot 但无 [bot] 后缀
    assert is_bot("njzjz-bot", "njzjz-bot", "njzjz.bot@gmail.com") is True


@pytest.mark.parametrize("login", ["botelho", "Botspot", "bot50", "BotBitmap"])
def test_real_users_containing_bot_are_not_bots(login):
    # 实测的真实 GitHub 用户，子串匹配会误伤他们
    assert is_bot(login, login, f"{login}@example.com") is False


def test_normal_user_is_not_bot():
    assert is_bot("njzjz", "Jinzhe Zeng", "njzjz@example.com") is False


def test_none_login_is_not_bot_by_default():
    assert is_bot(None, "Someone", "a@b.com") is False


# --- 邮箱归一化 ---

def test_normalize_email_lowercases_and_strips():
    assert normalize_email("  Njzjz@Example.COM ") == "njzjz@example.com"


# --- 归并逻辑 ---

def test_resolver_maps_email_to_login():
    r = IdentityResolver()
    r.add_mapping("a@x.com", "alice")
    assert r.resolve("a@x.com") == "alice"


def test_resolver_resolves_noreply_without_mapping():
    r = IdentityResolver()
    assert r.resolve("12345+bob@users.noreply.github.com") == "bob"


def test_resolver_is_case_insensitive():
    r = IdentityResolver()
    r.add_mapping("A@X.com", "alice")
    assert r.resolve("a@x.com  ") == "alice"


def test_same_login_multiple_emails_share_merge_key():
    r = IdentityResolver()
    r.add_mapping("work@corp.com", "alice")
    r.add_mapping("home@gmail.com", "alice")
    assert r.merge_key("work@corp.com", None) == r.merge_key("home@gmail.com", None)


def test_unknown_email_falls_back_to_email_key():
    r = IdentityResolver()
    key = r.merge_key("ghost@nowhere.com", None)
    assert key == "email:ghost@nowhere.com"


def test_unknown_email_recorded_as_unmatched():
    r = IdentityResolver()
    r.merge_key("ghost@nowhere.com", None)
    assert "ghost@nowhere.com" in r.unmatched_emails()


def test_resolved_email_not_recorded_as_unmatched():
    r = IdentityResolver()
    r.add_mapping("a@x.com", "alice")
    r.merge_key("a@x.com", None)
    assert r.unmatched_emails() == set()


def test_explicit_login_takes_priority_over_email_lookup():
    r = IdentityResolver()
    r.add_mapping("a@x.com", "stale")
    assert r.merge_key("a@x.com", "fresh") == "login:fresh"


def test_login_merge_key_is_case_insensitive():
    r = IdentityResolver()
    assert r.merge_key("a@x.com", "Alice") == r.merge_key("b@y.com", "alice")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.identity'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/identity.py`：

```python
"""身份归并与 bot 识别。

这是本脚本最易出错的部分。两条铁律：
1. bot 识别只用 [bot] 后缀 + 显式黑名单。实测 botelho / Botspot / bot50
   都是真实用户，子串匹配会把他们从表彰名单里误删。
2. 无法归并的邮箱必须进 unmatched，绝不静默丢弃 —— 漏掉一个真实贡献者
   比多算一个严重。
"""
import re
from typing import Optional

NOREPLY_DOMAIN = "users.noreply.github.com"

# 无 [bot] 后缀但确实是机器人的账号。实测 dpdata 仓库大量提交来自 njzjz-bot。
BOT_BLOCKLIST = {
    "njzjz-bot",
    "dependabot",
    "dependabot-preview",
    "renovate",
    "renovate-bot",
    "codecov",
    "codecov-io",
    "pre-commit-ci",
    "github-actions",
    "web-flow",          # GitHub 网页端合并使用的虚拟账号
    "deepmodeling-bot",
}

_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9._-]+(?:\[bot\])?)@" + re.escape(NOREPLY_DOMAIN) + r"$",
    re.IGNORECASE,
)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def parse_noreply_login(email: str) -> Optional[str]:
    """从 xxx@users.noreply.github.com 解析 login，无需 API 请求。

    两种格式都要处理：
      12345+name@users.noreply.github.com
      name@users.noreply.github.com
    """
    m = _NOREPLY_RE.match(normalize_email(email))
    return m.group(1) if m else None


def is_bot(login: Optional[str], name: str = "", email: str = "") -> bool:
    """只认 [bot] 后缀与显式黑名单，禁止子串匹配。"""
    candidates = [c for c in (login, name) if c]
    for c in candidates:
        low = c.strip().lower()
        if low.endswith("[bot]"):
            return True
        if low in BOT_BLOCKLIST:
            return True
    # 邮箱本地部分也查一次黑名单，覆盖 login 缺失的情况
    local = normalize_email(email).split("@", 1)[0]
    # 去掉 12345+ 前缀
    local = local.split("+", 1)[-1]
    if local and local in BOT_BLOCKLIST:
        return True
    if local.endswith("[bot]"):
        return True
    return False


class IdentityResolver:
    """维护 email → login 映射，并产出归并键。

    归并键格式：
      login:<lower>  —— 能确定 GitHub 账号时（可靠，同一人多邮箱自动合并）
      email:<lower>  —— 无法确定时的退化路径，同时记入 unmatched
    """

    def __init__(self) -> None:
        self._email_to_login: dict = {}
        self._unmatched: set = set()

    def add_mapping(self, email: str, login: Optional[str]) -> None:
        e = normalize_email(email)
        if e and login:
            self._email_to_login[e] = login

    def resolve(self, email: str) -> Optional[str]:
        e = normalize_email(email)
        if not e:
            return None
        if e in self._email_to_login:
            return self._email_to_login[e]
        return parse_noreply_login(e)

    def merge_key(self, email: str, login: Optional[str] = None) -> str:
        """显式传入的 login 优先级最高（来自 API，比历史映射新）。"""
        chosen = login or self.resolve(email)
        if chosen:
            return f"login:{chosen.strip().lower()}"
        e = normalize_email(email)
        if e:
            self._unmatched.add(e)
        return f"email:{e}"

    def unmatched_emails(self) -> set:
        return set(self._unmatched)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_identity.py -v`
Expected: 23 passed（20 个测试函数，其中 1 个 parametrize 展开为 4 例）

- [ ] **Step 5: 提交**

```bash
git add contributors/identity.py tests/test_identity.py
git commit -m "feat: 实现身份归并与 bot 识别"
```

---

### Task 5: 缓存管理与增量判断

**Files:**
- Create: `contributors/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `RepoInfo`（Task 1）
- Produces:
  - `CacheManager(cache_dir: str)` 类
  - `.repo_path(name) -> Path`、`.api_path(name, day) -> Path`
  - `.is_cloned(name) -> bool`、`.is_corrupt(name) -> bool`
  - `.needs_fetch(repo: RepoInfo, refresh: bool, no_fetch: bool) -> bool`
  - `.record_fetch(name, pushed_at)`、`.load_progress() -> dict`、`.save_progress(dict)`
  - `.estimate_needed_mb(repos) -> float`、`.free_space_mb() -> float`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cache.py`：

```python
from datetime import datetime, timezone
from contributors.cache import CacheManager
from contributors.models import RepoInfo


def mk_repo(name="dpdata", pushed="2026-07-01T00:00:00Z", size_mb=10.0):
    return RepoInfo(name=name, default_branch="main", size_mb=size_mb,
                    pushed_at=pushed, is_fork=False, upstream=None,
                    upstream_family=None, archived=False)


def test_repo_path_uses_bare_git_suffix(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.repo_path("dpdata").name == "dpdata.git"


def test_api_path_includes_day(tmp_path):
    cm = CacheManager(str(tmp_path))
    p = cm.api_path("dpdata", "2026-07-28")
    assert "dpdata" in p.name and "2026-07-28" in p.name


def test_not_cloned_when_dir_absent(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.is_cloned("dpdata") is False


def test_cloned_when_bare_repo_markers_present(tmp_path):
    cm = CacheManager(str(tmp_path))
    d = cm.repo_path("dpdata")
    (d / "objects").mkdir(parents=True)
    (d / "refs").mkdir()
    (d / "HEAD").write_text("ref: refs/heads/main")
    assert cm.is_cloned("dpdata") is True
    assert cm.is_corrupt("dpdata") is False


def test_corrupt_when_markers_incomplete(tmp_path):
    # 模拟上次 Ctrl-C 打断留下的半成品
    cm = CacheManager(str(tmp_path))
    d = cm.repo_path("dpdata")
    (d / "objects").mkdir(parents=True)
    assert cm.is_corrupt("dpdata") is True


def test_needs_fetch_true_when_never_cloned(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.needs_fetch(mk_repo(), refresh=False, no_fetch=False) is True


def test_no_fetch_flag_always_returns_false(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.needs_fetch(mk_repo(), refresh=False, no_fetch=True) is False


def test_refresh_flag_forces_fetch(tmp_path):
    cm = CacheManager(str(tmp_path))
    cm.record_fetch("dpdata", "2026-07-01T00:00:00Z")
    assert cm.needs_fetch(mk_repo(), refresh=True, no_fetch=False) is True


def test_skips_fetch_when_pushed_at_unchanged(tmp_path):
    cm = CacheManager(str(tmp_path))
    d = cm.repo_path("dpdata")
    (d / "objects").mkdir(parents=True)
    (d / "refs").mkdir()
    (d / "HEAD").write_text("ref: refs/heads/main")
    cm.record_fetch("dpdata", "2026-07-01T00:00:00Z")
    assert cm.needs_fetch(mk_repo(pushed="2026-07-01T00:00:00Z"),
                          refresh=False, no_fetch=False) is False


def test_fetches_when_pushed_at_advanced(tmp_path):
    cm = CacheManager(str(tmp_path))
    d = cm.repo_path("dpdata")
    (d / "objects").mkdir(parents=True)
    (d / "refs").mkdir()
    (d / "HEAD").write_text("ref: refs/heads/main")
    cm.record_fetch("dpdata", "2026-07-01T00:00:00Z")
    assert cm.needs_fetch(mk_repo(pushed="2026-07-20T00:00:00Z"),
                          refresh=False, no_fetch=False) is True


def test_progress_roundtrip(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.load_progress() == {}
    cm.save_progress({"done": ["dpdata"]})
    assert cm.load_progress() == {"done": ["dpdata"]}


def test_progress_survives_corrupt_json(tmp_path):
    cm = CacheManager(str(tmp_path))
    cm.progress_path.parent.mkdir(parents=True, exist_ok=True)
    cm.progress_path.write_text("{not json")
    assert cm.load_progress() == {}


def test_estimate_needed_mb_sums_uncloned_only(tmp_path):
    cm = CacheManager(str(tmp_path))
    repos = [mk_repo("a", size_mb=100.0), mk_repo("b", size_mb=200.0)]
    est = cm.estimate_needed_mb(repos)
    # blobless 克隆约为仓库体积的一部分，但估算须为正且不低于最大单仓库
    assert est > 0


def test_free_space_mb_is_positive(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.free_space_mb() > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.cache'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/cache.py`：

```python
"""本地缓存管理、增量判断、断点续跑。

设计要点：git 历史缓存在本地长期有效，改时间窗时零网络重算；
API 缓存按天，因为 GraphQL 查的是当前状态。
"""
import json
import shutil
from pathlib import Path
from typing import Optional

from .models import RepoInfo

# blobless 裸库实际占用远小于 GitHub 报告的 size，按经验取 0.6 并留 20% 余量
_SIZE_FACTOR = 0.6
_HEADROOM = 1.2


class CacheManager:
    def __init__(self, cache_dir: str) -> None:
        self.root = Path(cache_dir)
        self.repos_dir = self.root / "repos"
        self.api_dir = self.root / "api"
        self.progress_path = self.root / "progress.json"
        self._meta_path = self.root / "fetch_meta.json"

    # --- 路径 ---

    def repo_path(self, name: str) -> Path:
        return self.repos_dir / f"{name}.git"

    def api_path(self, name: str, day: str) -> Path:
        return self.api_dir / f"{name}-{day}.json"

    # --- 状态判断 ---

    def is_cloned(self, name: str) -> bool:
        d = self.repo_path(name)
        return (d / "objects").is_dir() and (d / "refs").is_dir() and (d / "HEAD").is_file()

    def is_corrupt(self, name: str) -> bool:
        """目录存在但裸库标志不全 —— 通常是上次被 Ctrl-C 打断。"""
        d = self.repo_path(name)
        if not d.exists():
            return False
        return not self.is_cloned(name)

    def discard(self, name: str) -> None:
        shutil.rmtree(self.repo_path(name), ignore_errors=True)

    def needs_fetch(self, repo: RepoInfo, refresh: bool, no_fetch: bool) -> bool:
        if no_fetch:
            return False
        if refresh:
            return True
        if not self.is_cloned(repo.name):
            return True
        last = self._load_meta().get(repo.name, {}).get("pushed_at")
        # pushed_at 未变说明远端无新提交，连 fetch 都省掉
        return last != repo.pushed_at

    # --- 元数据 ---

    def _load_meta(self) -> dict:
        return self._read_json(self._meta_path)

    def record_fetch(self, name: str, pushed_at: str) -> None:
        meta = self._load_meta()
        meta[name] = {"pushed_at": pushed_at}
        self._write_json(self._meta_path, meta)

    # --- 断点续跑 ---

    def load_progress(self) -> dict:
        return self._read_json(self.progress_path)

    def save_progress(self, data: dict) -> None:
        self._write_json(self.progress_path, data)

    # --- 磁盘 ---

    def estimate_needed_mb(self, repos: list) -> float:
        pending = [r for r in repos if not self.is_cloned(r.name)]
        return sum(r.size_mb for r in pending) * _SIZE_FACTOR * _HEADROOM

    def free_space_mb(self) -> float:
        probe = self.root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        return shutil.disk_usage(probe).free / (1024 * 1024)

    # --- 内部工具 ---

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 缓存损坏不该让整次运行失败，退化为空
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_cache.py -v`
Expected: 14 passed

- [ ] **Step 5: 提交**

```bash
git add contributors/cache.py tests/test_cache.py
git commit -m "feat: 实现缓存管理与增量判断"
```

---

### Task 6: git log 解析与 commit 统计

**Files:**
- Create: `contributors/git_stats.py`
- Create: `tests/fixtures/git_log_sample.txt`
- Test: `tests/test_git_stats.py`

**Interfaces:**
- Consumes: `GitStats`（Task 1）、`CacheManager`（Task 5）、`Config`（Task 2）
- Produces:
  - `LOG_FORMAT: str`、`RECORD_SEP: str`
  - `parse_git_log(text: str, count_lines: bool, exclude_paths: list) -> dict[str, GitStats]` — 键为归一化 email
  - `is_excluded(path: str, patterns: list) -> bool`
  - `clone_or_fetch(repo, cm, cfg) -> None`
  - `collect_git_stats(repo, cm, cfg) -> dict[str, GitStats]`
  - `count_upstream_excluded(repo, cm, cfg) -> int`

- [ ] **Step 1: 写失败测试**

先创建 `tests/fixtures/git_log_sample.txt`（用 `\x00` 的字面写法便于人读，测试里替换）。内容取自实测输出，含四种易错情况：author 本地时区、noreply 邮箱、多文件、二进制文件显示为 `-`：

```
C|aef01ec67b9bef6b4d604dd854e91e7436c52e38|pre-commit-ci[bot]|66853113+pre-commit-ci[bot]@users.noreply.github.com|2026-07-27T20:30:41Z

1	1	.pre-commit-config.yaml
C|3ccd131d8ea3306ab1bb7593cd4d3b67b300c19a|njzjz-bot|njzjz.bot@gmail.com|2026-07-27T19:10:41+08:00

37	2	dpdata/plugins/cp2k.py
68	0	tests/test_cp2k_aimd_missing_inputs.py
C|f9885d217bc6d224c3696fe34d4cdf9a48650de9|Jinzhe Zeng|njzjz@example.com|2026-07-20T17:31:39+08:00

4	1	dpdata/formats/vasp/outcar.py
-	-	docs/logo.png
120	0	package-lock.json
C|acaef94fec2878700ab034eab937efcff4304505|Jinzhe Zeng|Njzjz@Example.com|2026-01-15T16:52:31Z

312	0	tests/test_serialization.py
```

创建 `tests/test_git_stats.py`：

```python
from pathlib import Path
import pytest
from contributors.git_stats import parse_git_log, is_excluded
from contributors.config import DEFAULT_EXCLUDE_PATHS

FIXTURE = Path(__file__).parent / "fixtures" / "git_log_sample.txt"


def load_sample() -> str:
    # fixture 用 | 便于人读，实际 git 输出用 \x00 分隔
    return FIXTURE.read_text(encoding="utf-8").replace("|", "\x00")


def test_parses_all_four_commits():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    total = sum(s.commits for s in stats.values())
    assert total == 4


def test_groups_commits_by_email():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert stats["njzjz.bot@gmail.com"].commits == 1
    # 同一人两种大小写邮箱应归一后合并为 2
    assert stats["njzjz@example.com"].commits == 2


def test_email_case_is_normalized():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert "Njzjz@Example.com" not in stats


def test_captures_author_names():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert "Jinzhe Zeng" in stats["njzjz@example.com"].names


def test_line_counts_none_when_disabled():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert stats["njzjz@example.com"].additions is None
    assert stats["njzjz@example.com"].additions_raw is None


def test_raw_counts_include_generated_files():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    s = stats["njzjz@example.com"]
    # 4 + 120(package-lock) + 312，二进制 - 不计
    assert s.additions_raw == 436


def test_filtered_counts_exclude_generated_files():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    s = stats["njzjz@example.com"]
    # 排除 package-lock.json(120) 与 logo.png(二进制)
    assert s.additions == 316
    assert s.deletions == 1


def test_binary_files_do_not_crash_parser():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    # docs/logo.png 显示为 - -，不应计入行数但文件计数可含
    assert stats["njzjz@example.com"].additions is not None


def test_files_changed_deduplicates():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    # outcar.py 与 test_serialization.py 各一次，共 2 个非排除文件
    assert stats["njzjz@example.com"].files_changed == 2


def test_empty_input_yields_empty_dict():
    assert parse_git_log("", count_lines=True, exclude_paths=[]) == {}


def test_commit_message_with_pipe_or_newline_does_not_break():
    # 格式里不含 message，故任何 message 内容都不影响解析
    text = ("C\x00abc\x00A B\x00a@b.com\x002026-01-01T00:00:00Z\n\n"
            "1\t0\tf.py\n")
    stats = parse_git_log(text, count_lines=True, exclude_paths=[])
    assert stats["a@b.com"].commits == 1


# --- 排除模式 ---

@pytest.mark.parametrize("path", [
    "package-lock.json",
    "poetry.lock",
    "vendor/lib/x.js",
    "third_party/foo/bar.c",
    "assets/app.min.js",
    "data/train.npy",
])
def test_excluded_paths_match(path):
    assert is_excluded(path, DEFAULT_EXCLUDE_PATHS) is True


@pytest.mark.parametrize("path", [
    "dpdata/plugins/cp2k.py",
    "tests/test_serialization.py",
    "README.md",
])
def test_source_paths_not_excluded(path):
    assert is_excluded(path, DEFAULT_EXCLUDE_PATHS) is False


def test_nested_vendor_matches_double_star():
    assert is_excluded("a/b/vendor/x.js", DEFAULT_EXCLUDE_PATHS) is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_git_stats.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.git_stats'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/git_stats.py`：

```python
"""git 侧统计：mirror clone/fetch、git log 解析、行数统计。

关键实现说明：
- 用 git log --all 覆盖所有分支。它按 commit SHA 天然去重，已合并的
  feature 分支不会让贡献者被重复计数（实测 473 条 = 473 个唯一 SHA）。
- --no-merges 排除合并提交，否则合并者会被算上整个分支的改动。
- 时间过滤交给 git 的 --since/--until，但注意 %aI 返回作者本地时区
  （实测有 +08:00），故 since/until 传入时统一用 UTC ISO 字符串。
"""
import fnmatch
import subprocess
from pathlib import Path
from typing import Optional

from .identity import normalize_email
from .models import GitStats

RECORD_SEP = "\x00"
# C 前缀标记提交行，与 numstat 数据行区分
LOG_FORMAT = f"C{RECORD_SEP}%H{RECORD_SEP}%aN{RECORD_SEP}%aE{RECORD_SEP}%aI"

CLONE_TIMEOUT = 1800  # 30 分钟/仓库


class GitError(RuntimeError):
    pass


def is_excluded(path: str, patterns: list) -> bool:
    p = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(p, pat):
            return True
        # vendor/** 需匹配任意层级下的 vendor 目录
        if "**" in pat:
            head = pat.split("/**")[0]
            if head and (p.startswith(head + "/") or f"/{head}/" in p):
                return True
        # *.lock 这类模式也要匹配子目录中的文件
        if pat.startswith("*.") and p.endswith(pat[1:]):
            return True
        if fnmatch.fnmatch(Path(p).name, pat):
            return True
    return False


def parse_git_log(text: str, count_lines: bool, exclude_paths: list) -> dict:
    """解析 git log 输出，按归一化 email 聚合。"""
    stats: dict = {}
    cur: Optional[GitStats] = None
    seen_files: dict = {}

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line:
            continue

        if line.startswith("C" + RECORD_SEP):
            parts = line.split(RECORD_SEP)
            if len(parts) < 5:
                continue
            _, _sha, name, email, _when = parts[:5]
            key = normalize_email(email)
            if not key:
                continue
            if key not in stats:
                stats[key] = GitStats()
                if count_lines:
                    stats[key].additions = 0
                    stats[key].deletions = 0
                    stats[key].files_changed = 0
                    stats[key].additions_raw = 0
                    stats[key].deletions_raw = 0
                seen_files[key] = set()
            cur = stats[key]
            cur.commits += 1
            # commits_not_in_upstream 由 count_upstream_excluded 单独覆盖，
            # 非 fork 场景下与 commits 相等
            cur.commits_not_in_upstream = cur.commits
            cur.emails.add(key)
            if name:
                cur.names.add(name.strip())
            continue

        if cur is None or not count_lines:
            continue

        cols = line.split("\t")
        if len(cols) < 3:
            continue
        add_s, del_s, path = cols[0], cols[1], "\t".join(cols[2:])
        # 二进制文件 numstat 显示为 - -
        add = 0 if add_s == "-" else int(add_s) if add_s.isdigit() else 0
        dele = 0 if del_s == "-" else int(del_s) if del_s.isdigit() else 0

        cur.additions_raw += add
        cur.deletions_raw += dele
        if add_s == "-" or is_excluded(path, exclude_paths):
            continue
        cur.additions += add
        cur.deletions += dele
        key = normalize_email(next(iter(cur.emails)))
        seen_files.setdefault(key, set()).add(path)
        cur.files_changed = len(seen_files[key])

    return stats


def _run_git(args: list, cwd: Optional[Path] = None, timeout: int = 300) -> str:
    try:
        r = subprocess.run(
            ["git"] + args, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git 超时：{' '.join(args[:3])}") from exc
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git 失败：{exc.stderr.strip()[:300]}") from exc
    return r.stdout


def clone_or_fetch(repo, cm, cfg, token: str = "") -> None:
    """首次 clone，之后增量 fetch。损坏的缓存删除重建。"""
    if cm.is_corrupt(repo.name):
        cm.discard(repo.name)

    url = f"https://github.com/{cfg.org}/{repo.name}.git"
    if token:
        url = f"https://x-access-token:{token}@github.com/{cfg.org}/{repo.name}.git"

    if not cm.is_cloned(repo.name):
        if cfg.no_fetch:
            raise GitError(f"{repo.name} 无本地缓存且指定了 --no-fetch")
        dest = cm.repo_path(repo.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["clone", "--mirror", "--filter=blob:none", url, str(dest)],
            timeout=CLONE_TIMEOUT,
        )
    elif cm.needs_fetch(repo, cfg.refresh, cfg.no_fetch):
        _run_git(["fetch", "--all", "--prune"], cwd=cm.repo_path(repo.name),
                 timeout=CLONE_TIMEOUT)

    if not cfg.no_fetch:
        cm.record_fetch(repo.name, repo.pushed_at)


def collect_git_stats(repo, cm, cfg) -> dict:
    args = [
        "log", "--all", "--no-merges",
        f"--since={cfg.since.isoformat()}",
        f"--until={cfg.until.isoformat()}",
        f"--format={LOG_FORMAT}",
    ]
    if cfg.count_lines:
        args.append("--numstat")
    text = _run_git(args, cwd=cm.repo_path(repo.name), timeout=600)
    return parse_git_log(text, cfg.count_lines, cfg.exclude_paths)


def count_upstream_excluded(repo, cm, cfg) -> dict:
    """统计上游不可达的 commit，按 email 计数。

    非 fork 仓库直接返回空字典（调用方令其等于 commits）。
    对 fork：添加上游 remote、fetch、用 ^upstream/* 排除上游可达提交。
    """
    if not repo.is_fork or not repo.upstream:
        return {}
    path = cm.repo_path(repo.name)
    up_url = f"https://github.com/{repo.upstream}.git"
    existing = _run_git(["remote"], cwd=path)
    if "upstream" not in existing.split():
        _run_git(["remote", "add", "upstream", up_url], cwd=path)
    if not cfg.no_fetch:
        _run_git(["fetch", "--filter=blob:none", "upstream",
                  "+refs/heads/*:refs/remotes/upstream/*"],
                 cwd=path, timeout=CLONE_TIMEOUT)
    text = _run_git([
        "log", "--all", "--no-merges",
        "--not", "--remotes=upstream",
        f"--since={cfg.since.isoformat()}",
        f"--until={cfg.until.isoformat()}",
        f"--format={LOG_FORMAT}",
    ], cwd=path, timeout=600)
    return {k: v.commits for k, v in parse_git_log(text, False, []).items()}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_git_stats.py -v`
Expected: 19 passed

- [ ] **Step 5: 提交**

```bash
git add contributors/git_stats.py tests/fixtures/git_log_sample.txt tests/test_git_stats.py
git commit -m "feat: 实现 git log 解析与 commit 统计"
```

---

### Task 7: GraphQL 查询 PR/Issue/Review/Comment

**Files:**
- Create: `contributors/api_stats.py`
- Create: `tests/fixtures/graphql_prs.json`
- Test: `tests/test_api_stats.py`

**Interfaces:**
- Consumes: `ApiStats`（Task 1）、`CacheManager`（Task 5）、`IdentityResolver`（Task 4）
- Produces:
  - `PR_QUERY: str`、`ISSUE_QUERY: str`
  - `aggregate_prs(nodes: list, since, until) -> tuple[dict[str, ApiStats], dict[str,str]]` — 返回 {login: ApiStats} 与 {email: login} 映射
  - `aggregate_issues(nodes: list, since, until) -> dict[str, ApiStats]`
  - `GitHubGraphQL(token)` 类，方法 `.query(q, variables) -> dict`（含配额管理与退避重试）
  - `collect_api_stats(repo, cfg, client, cm) -> tuple[dict, dict]`

**实测依据（写实现时不要改成逐用户查询）：** `reviewed-by:` / `commenter:` 搜索虽然可用，但需按用户逐个查询 —— 约 100 位贡献者 × 67 仓库 = 数千请求，会撞限额。改为按仓库分页拉取 PR/Issue 列表，在节点里一并取回 `reviews.nodes.author.login` 与 `comments.nodes.author.login`，实测每页 cost 仅 1，每仓库约 7 页。

- [ ] **Step 1: 写失败测试**

创建 `tests/fixtures/graphql_prs.json`（结构取自实测响应）：

```json
{
  "data": {
    "rateLimit": {"cost": 1, "remaining": 4991},
    "repository": {
      "pullRequests": {
        "pageInfo": {"hasNextPage": false, "endCursor": "Y3Vy"},
        "nodes": [
          {
            "number": 1050,
            "createdAt": "2026-07-01T00:00:00Z",
            "merged": false,
            "author": {"login": "alice"},
            "reviews": {"nodes": [{"author": {"login": "bob"}}, {"author": {"login": "bob"}}]},
            "comments": {"nodes": [{"author": {"login": "carol"}}]}
          },
          {
            "number": 1049,
            "createdAt": "2026-06-01T00:00:00Z",
            "merged": true,
            "author": {"login": "alice"},
            "reviews": {"nodes": [{"author": {"login": "carol"}}]},
            "comments": {"nodes": []}
          },
          {
            "number": 900,
            "createdAt": "2024-01-01T00:00:00Z",
            "merged": true,
            "author": {"login": "dave"},
            "reviews": {"nodes": []},
            "comments": {"nodes": []}
          },
          {
            "number": 1048,
            "createdAt": "2026-05-01T00:00:00Z",
            "merged": true,
            "author": null,
            "reviews": {"nodes": [{"author": null}]},
            "comments": {"nodes": []}
          }
        ]
      }
    }
  }
}
```

创建 `tests/test_api_stats.py`：

```python
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
import responses
from contributors.api_stats import (
    aggregate_prs, aggregate_issues, GitHubGraphQL, RateLimitError,
)

SINCE = datetime(2025, 7, 28, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 29, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "graphql_prs.json"


def pr_nodes():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["data"]["repository"]["pullRequests"]["nodes"]


def test_counts_pr_created_within_window():
    stats, _ = aggregate_prs(pr_nodes(), SINCE, UNTIL)
    assert stats["alice"].pr_created == 2


def test_excludes_pr_created_outside_window():
    stats, _ = aggregate_prs(pr_nodes(), SINCE, UNTIL)
    # dave 的 PR 在 2024 年，窗口外
    assert "dave" not in stats or stats["dave"].pr_created == 0


def test_counts_merged_subset():
    stats, _ = aggregate_prs(pr_nodes(), SINCE, UNTIL)
    assert stats["alice"].pr_merged == 1


def test_review_counted_once_per_pr_despite_multiple_reviews():
    # bob 在同一 PR 上有两条 review，应只算 1
    stats, _ = aggregate_prs(pr_nodes(), SINCE, UNTIL)
    assert stats["bob"].pr_reviewed == 1


def test_reviewer_across_two_prs_counted_twice():
    stats, _ = aggregate_prs(pr_nodes(), SINCE, UNTIL)
    assert stats["carol"].pr_reviewed == 1
    assert stats["carol"].issue_commented == 1


def test_null_author_is_ignored_not_crash():
    # 已删号用户 author 为 null
    stats, _ = aggregate_prs(pr_nodes(), SINCE, UNTIL)
    assert None not in stats


def test_self_review_not_double_counted_as_created():
    stats, _ = aggregate_prs(pr_nodes(), SINCE, UNTIL)
    assert stats["alice"].pr_reviewed == 0


def test_issue_aggregation_counts_created_and_commented():
    nodes = [{
        "createdAt": "2026-03-01T00:00:00Z",
        "author": {"login": "alice"},
        "comments": {"nodes": [{"author": {"login": "bob"}},
                               {"author": {"login": "bob"}}]},
    }]
    stats = aggregate_issues(nodes, SINCE, UNTIL)
    assert stats["alice"].issue_created == 1
    # 同一 issue 多条评论只算 1
    assert stats["bob"].issue_commented == 1


def test_issue_outside_window_excluded():
    nodes = [{"createdAt": "2020-01-01T00:00:00Z",
              "author": {"login": "alice"}, "comments": {"nodes": []}}]
    assert aggregate_issues(nodes, SINCE, UNTIL) == {}


def test_email_login_mapping_extracted_from_commit_authors():
    nodes = [{
        "createdAt": "2026-03-01T00:00:00Z", "merged": True,
        "author": {"login": "alice"},
        "reviews": {"nodes": []}, "comments": {"nodes": []},
        "commits": {"nodes": [
            {"commit": {"author": {"email": "A@X.com",
                                   "user": {"login": "alice"}}}}
        ]},
    }]
    _, mapping = aggregate_prs(nodes, SINCE, UNTIL)
    assert mapping["a@x.com"] == "alice"


# --- 客户端配额与重试 ---

@responses.activate
def test_client_raises_on_graphql_errors():
    responses.add(responses.POST, "https://api.github.com/graphql",
                  json={"errors": [{"message": "Bad credentials"}]}, status=200)
    c = GitHubGraphQL("tok", max_retries=1)
    with pytest.raises(RuntimeError, match="Bad credentials"):
        c.query("query{x}", {})


@responses.activate
def test_client_retries_on_429_then_succeeds():
    responses.add(responses.POST, "https://api.github.com/graphql",
                  json={"message": "rate limited"}, status=429)
    responses.add(responses.POST, "https://api.github.com/graphql",
                  json={"data": {"ok": 1, "rateLimit": {"cost": 1, "remaining": 4000}}},
                  status=200)
    c = GitHubGraphQL("tok", max_retries=3, backoff_base=0)
    assert c.query("query{x}", {})["ok"] == 1


@responses.activate
def test_client_gives_up_after_max_retries():
    for _ in range(4):
        responses.add(responses.POST, "https://api.github.com/graphql",
                      json={"message": "rate limited"}, status=429)
    c = GitHubGraphQL("tok", max_retries=2, backoff_base=0)
    with pytest.raises(RateLimitError):
        c.query("query{x}", {})


@responses.activate
def test_client_tracks_remaining_quota():
    responses.add(responses.POST, "https://api.github.com/graphql",
                  json={"data": {"rateLimit": {"cost": 3, "remaining": 4200}}},
                  status=200)
    c = GitHubGraphQL("tok")
    c.query("query{x}", {})
    assert c.remaining == 4200
    assert c.spent == 3
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_api_stats.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.api_stats'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/api_stats.py`：

```python
"""GraphQL 侧统计：PR / Issue / Review / Comment。

为什么按仓库分页而不按用户查询：
reviewed-by: 与 commenter: 搜索需逐用户发一次请求，约 100 位贡献者
× 67 仓库 = 数千请求，必然撞限额。改为分页拉 PR/Issue 列表，在节点里
一并取回 reviews 与 comments 的作者，实测每页 cost 仅 1。

同时顺手取回 commit 的 author.email 与 author.user.login，
免费得到 email→login 映射（spec 第 3.1 节）。
"""
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .models import ApiStats

GRAPHQL_URL = "https://api.github.com/graphql"
QUOTA_FLOOR = 500  # 剩余低于此值时主动暂停

PR_QUERY = """
query($owner:String!,$name:String!,$cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    pullRequests(first:25, after:$cursor,
                 orderBy:{field:CREATED_AT,direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number createdAt merged
        author { login }
        reviews(first:50) { nodes { author { login } } }
        comments(first:50) { nodes { author { login } } }
        commits(first:50) {
          nodes { commit { author { email user { login } } } }
        }
      }
    }
  }
}
"""

ISSUE_QUERY = """
query($owner:String!,$name:String!,$cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    issues(first:25, after:$cursor,
           orderBy:{field:CREATED_AT,direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number createdAt
        author { login }
        comments(first:50) { nodes { author { login } } }
      }
    }
  }
}
"""


class RateLimitError(RuntimeError):
    pass


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _in_window(created_at: str, since: datetime, until: datetime) -> bool:
    t = _parse_iso(created_at)
    return since <= t < until


def _login_of(node: Optional[dict]) -> Optional[str]:
    """已删号用户的 author 为 null，必须容忍。"""
    if not node:
        return None
    a = node.get("author")
    return (a or {}).get("login")


def _bucket(stats: dict, login: str) -> ApiStats:
    return stats.setdefault(login, ApiStats())


def aggregate_prs(nodes: list, since: datetime, until: datetime) -> tuple:
    """返回 ({login: ApiStats}, {email: login})。"""
    stats: dict = {}
    mapping: dict = {}

    for pr in nodes:
        if not _in_window(pr.get("createdAt", ""), since, until):
            continue

        author = _login_of(pr)
        if author:
            b = _bucket(stats, author)
            b.pr_created += 1
            if pr.get("merged"):
                b.pr_merged += 1

        # 同一 PR 内多条 review 只记一次，避免刷量
        reviewers = {
            _login_of(r) for r in (pr.get("reviews") or {}).get("nodes", [])
        }
        for r in reviewers - {None, author}:
            _bucket(stats, r).pr_reviewed += 1

        commenters = {
            _login_of(c) for c in (pr.get("comments") or {}).get("nodes", [])
        }
        for c in commenters - {None}:
            _bucket(stats, c).issue_commented += 1

        # 顺带收集 email → login 映射，零额外成本
        for cn in (pr.get("commits") or {}).get("nodes", []):
            ca = ((cn or {}).get("commit") or {}).get("author") or {}
            email = (ca.get("email") or "").strip().lower()
            login = (ca.get("user") or {}).get("login")
            if email and login:
                mapping[email] = login

    return stats, mapping


def aggregate_issues(nodes: list, since: datetime, until: datetime) -> dict:
    stats: dict = {}
    for issue in nodes:
        if not _in_window(issue.get("createdAt", ""), since, until):
            continue
        author = _login_of(issue)
        if author:
            _bucket(stats, author).issue_created += 1
        commenters = {
            _login_of(c) for c in (issue.get("comments") or {}).get("nodes", [])
        }
        for c in commenters - {None}:
            _bucket(stats, c).issue_commented += 1
    return stats


class GitHubGraphQL:
    def __init__(self, token: str, max_retries: int = 5,
                 backoff_base: float = 2.0, sleeper=time.sleep) -> None:
        self.token = token
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleeper
        self.remaining: Optional[int] = None
        self.spent = 0

    def query(self, query: str, variables: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            resp = requests.post(
                GRAPHQL_URL, json={"query": query, "variables": variables},
                headers=headers, timeout=60,
            )
            if resp.status_code in (403, 429, 502, 503):
                if attempt >= self.max_retries:
                    raise RateLimitError(
                        f"GitHub 返回 {resp.status_code}，已重试 "
                        f"{attempt} 次仍失败"
                    )
                self._sleep(self.backoff_base * (2 ** attempt))
                attempt += 1
                continue

            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                msgs = "; ".join(
                    e.get("message", "") for e in payload["errors"]
                )
                raise RuntimeError(f"GraphQL 错误：{msgs}")

            data = payload.get("data") or {}
            rl = data.get("rateLimit") or {}
            if rl:
                self.remaining = rl.get("remaining")
                self.spent += rl.get("cost", 0)
            return data

    def guard_quota(self, log=print) -> None:
        """剩余额度过低时主动暂停到重置时间。"""
        if self.remaining is not None and self.remaining < QUOTA_FLOOR:
            log(f"API 额度剩余 {self.remaining}，低于 {QUOTA_FLOOR}，暂停 60 秒")
            self._sleep(60)


def _paginate(client: GitHubGraphQL, query: str, org: str, name: str,
              root_key: str) -> list:
    nodes, cursor = [], None
    while True:
        data = client.query(query, {"owner": org, "name": name, "cursor": cursor})
        conn = ((data.get("repository") or {}).get(root_key) or {})
        nodes.extend(conn.get("nodes") or [])
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        client.guard_quota()
    return nodes


def collect_api_stats(repo, cfg, client: GitHubGraphQL, cm) -> tuple:
    """返回 ({login: ApiStats}, {email: login})。同日缓存命中则读盘。"""
    day = datetime.now(timezone.utc).date().isoformat()
    cache_file = cm.api_path(repo.name, day)
    if cache_file.is_file() and not cfg.refresh:
        import json
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        pr_nodes, issue_nodes = raw.get("prs", []), raw.get("issues", [])
    else:
        pr_nodes = _paginate(client, PR_QUERY, cfg.org, repo.name, "pullRequests")
        issue_nodes = _paginate(client, ISSUE_QUERY, cfg.org, repo.name, "issues")
        if not cfg.no_fetch:
            import json
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"prs": pr_nodes, "issues": issue_nodes},
                           ensure_ascii=False),
                encoding="utf-8",
            )

    pr_stats, mapping = aggregate_prs(pr_nodes, cfg.since, cfg.until)
    issue_stats = aggregate_issues(issue_nodes, cfg.since, cfg.until)

    for login, s in issue_stats.items():
        b = pr_stats.setdefault(login, ApiStats())
        b.issue_created += s.issue_created
        b.issue_commented += s.issue_commented
    return pr_stats, mapping
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_api_stats.py -v`
Expected: 14 passed

- [ ] **Step 5: 提交**

```bash
git add contributors/api_stats.py tests/fixtures/graphql_prs.json tests/test_api_stats.py
git commit -m "feat: 实现 GraphQL 查询 PR/Issue/Review/Comment"
```

---

### Task 8: 三种格式输出

**Files:**
- Create: `contributors/output.py`
- Test: `tests/test_output.py`

**Interfaces:**
- Consumes: `Contributor`、`GitStats`、`ApiStats`、`RepoInfo`（Task 1）、`Config`（Task 2）
- Produces:
  - `Row` dataclass — 一行输出记录，字段顺序即列顺序
  - `BASE_COLUMNS: list[str]`、`LINE_COLUMNS: list[str]`、`LINE_CAVEAT: str`
  - `summarize(rows: list[Row]) -> list[Row]` — 跨仓库累加，repo 字段为空，按 commits 降序
  - `write_csv(rows, path, cfg)`、`write_markdown(rows, path, cfg)`、`write_json(rows, path, cfg)`
  - 注意：`build_rows` 不在本 task，它需要 git 与 API 两侧数据，属编排职责，在 Task 9 的 `main.py` 中实现

- [ ] **Step 1: 写失败测试**

创建 `tests/test_output.py`：

```python
import csv
import json
from datetime import datetime, timezone
from contributors.output import (
    Row, BASE_COLUMNS, LINE_COLUMNS, summarize,
    write_csv, write_markdown, write_json,
)
from contributors.config import Config


def mk_cfg(tmp_path, **kw):
    base = dict(org="deepmodeling",
                since=datetime(2025, 7, 28, tzinfo=timezone.utc),
                until=datetime(2026, 7, 29, tzinfo=timezone.utc),
                include_forks="all", max_repo_size=2048,
                out_dir=str(tmp_path))
    base.update(kw)
    return Config(**base)


def mk_row(repo="dpdata", login="alice", commits=5, **kw):
    d = dict(repo=repo, login=login, name="Alice", email="a@x.com",
             github_url=f"https://github.com/{login}", commits=commits,
             commits_not_in_upstream=commits, pr_created=2, pr_merged=1,
             pr_reviewed=3, issue_created=1, issue_commented=4,
             is_fork=False, upstream="", upstream_family="", is_bot=False)
    d.update(kw)
    return Row(**d)


def test_base_columns_match_spec_order():
    assert BASE_COLUMNS[:6] == [
        "repo", "login", "name", "email", "github_url", "commits",
    ]
    assert "commits_not_in_upstream" in BASE_COLUMNS
    for c in ["pr_created", "pr_merged", "pr_reviewed",
              "issue_created", "issue_commented",
              "is_fork", "upstream", "upstream_family", "is_bot"]:
        assert c in BASE_COLUMNS


def test_line_columns_absent_from_base():
    for c in LINE_COLUMNS:
        assert c not in BASE_COLUMNS


def test_summarize_accumulates_across_repos():
    rows = [mk_row(repo="a", commits=3), mk_row(repo="b", commits=4)]
    out = summarize(rows)
    assert len(out) == 1
    assert out[0].commits == 7
    assert out[0].pr_created == 4
    assert out[0].repo == ""


def test_summarize_sorts_by_commits_desc():
    rows = [mk_row(login="low", commits=1), mk_row(login="high", commits=9)]
    out = summarize(rows)
    assert [r.login for r in out] == ["high", "low"]


def test_summarize_merges_emails_uniquely():
    rows = [mk_row(repo="a", email="a@x.com"), mk_row(repo="b", email="b@y.com")]
    out = summarize(rows)
    assert set(out[0].email.split(";")) == {"a@x.com", "b@y.com"}


def test_csv_omits_line_columns_when_disabled(tmp_path):
    p = tmp_path / "o.csv"
    write_csv([mk_row()], p, mk_cfg(tmp_path, count_lines=False))
    header = p.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "additions" not in header
    assert "commits" in header


def test_csv_includes_line_columns_when_enabled(tmp_path):
    p = tmp_path / "o.csv"
    r = mk_row(additions=10, deletions=2, files_changed=3,
               additions_raw=99, deletions_raw=5)
    write_csv([r], p, mk_cfg(tmp_path, count_lines=True))
    header = p.read_text(encoding="utf-8-sig").splitlines()[0]
    for c in LINE_COLUMNS:
        assert c in header


def test_csv_uses_utf8_bom_for_excel(tmp_path):
    p = tmp_path / "o.csv"
    write_csv([mk_row(name="张三")], p, mk_cfg(tmp_path))
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")


def test_csv_roundtrip_preserves_values(tmp_path):
    p = tmp_path / "o.csv"
    write_csv([mk_row()], p, mk_cfg(tmp_path))
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["login"] == "alice"
    assert rows[0]["commits"] == "5"


# --- 用户全局约定：表格单元格内禁止 ** 加粗 ---

def test_markdown_table_cells_have_no_bold(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row()], p, mk_cfg(tmp_path))
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            assert "**" not in line, f"表格单元格禁止加粗: {line}"


def test_markdown_has_header_and_separator(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row()], p, mk_cfg(tmp_path))
    lines = [l for l in p.read_text(encoding="utf-8").splitlines()
             if l.startswith("|")]
    assert "repo" in lines[0]
    assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-", ":"}


def test_markdown_states_the_window(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row()], p, mk_cfg(tmp_path))
    text = p.read_text(encoding="utf-8")
    # 报表口径必须写明，闭区间显示为 2025-07-28 ~ 2026-07-28
    assert "2025-07-28" in text and "2026-07-28" in text


def test_markdown_escapes_pipe_in_values(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row(name="a|b")], p, mk_cfg(tmp_path))
    body = [l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("|") and "alice" in l][0]
    assert r"a\|b" in body


def test_json_is_valid_and_includes_meta(tmp_path):
    p = tmp_path / "o.json"
    write_json([mk_row()], p, mk_cfg(tmp_path))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["meta"]["since"] == "2025-07-28"
    assert data["meta"]["until"] == "2026-07-28"
    assert data["contributors"][0]["login"] == "alice"


def test_json_notes_line_metric_caveat_when_enabled(tmp_path):
    p = tmp_path / "o.json"
    write_json([mk_row()], p, mk_cfg(tmp_path, count_lines=True))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "caveat" in json.dumps(data["meta"], ensure_ascii=False)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_output.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.output'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/output.py`：

```python
"""CSV / Markdown / JSON 输出。

两条硬约束：
1. Markdown 表格单元格内禁止 ** 加粗（用户全局约定），已由测试固化。
2. CSV 写 UTF-8 BOM，否则 Excel 打开中文姓名会乱码。
"""
import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

BASE_COLUMNS = [
    "repo", "login", "name", "email", "github_url",
    "commits", "commits_not_in_upstream",
    "pr_created", "pr_merged", "pr_reviewed",
    "issue_created", "issue_commented",
    "is_fork", "upstream", "upstream_family", "is_bot",
]

LINE_COLUMNS = [
    "additions", "deletions", "files_changed",
    "additions_raw", "deletions_raw",
]

LINE_CAVEAT = (
    "代码行数仅供参考，不宜用于排名：易被生成文件与数据文件污染；"
    "删除冗余往往比新增更有价值；已排除合并提交。"
    "additions/deletions 为过滤后数值，additions_raw/deletions_raw 为原始值。"
)


@dataclass
class Row:
    repo: str
    login: str
    name: str
    email: str
    github_url: str
    commits: int
    commits_not_in_upstream: int
    pr_created: int
    pr_merged: int
    pr_reviewed: int
    issue_created: int
    issue_commented: int
    is_fork: bool
    upstream: str
    upstream_family: str
    is_bot: bool
    additions: Optional[int] = None
    deletions: Optional[int] = None
    files_changed: Optional[int] = None
    additions_raw: Optional[int] = None
    deletions_raw: Optional[int] = None


def _columns(cfg) -> list:
    return BASE_COLUMNS + (LINE_COLUMNS if cfg.count_lines else [])


def _window_text(cfg) -> tuple:
    # 内部为半开区间，对外显示闭区间
    return (cfg.since.date().isoformat(),
            (cfg.until - timedelta(days=1)).date().isoformat())


def summarize(rows: list) -> list:
    """跨仓库累加，一人一行。repo 字段留空表示汇总。"""
    merged: dict = {}
    emails: dict = {}
    for r in rows:
        key = r.login or f"email:{r.email}"
        if key not in merged:
            merged[key] = Row(**{**asdict(r), "repo": "", "upstream": "",
                                 "upstream_family": "", "is_fork": False})
            emails[key] = set()
        else:
            m = merged[key]
            for f in ("commits", "commits_not_in_upstream", "pr_created",
                      "pr_merged", "pr_reviewed", "issue_created",
                      "issue_commented"):
                setattr(m, f, getattr(m, f) + getattr(r, f))
            for f in LINE_COLUMNS:
                a, b = getattr(m, f), getattr(r, f)
                if a is not None or b is not None:
                    setattr(m, f, (a or 0) + (b or 0))
        emails[key].update(e for e in r.email.split(";") if e)

    for key, m in merged.items():
        m.email = ";".join(sorted(emails[key]))
    return sorted(merged.values(), key=lambda r: (-r.commits, r.login or ""))


def write_csv(rows: list, path: Path, cfg) -> None:
    cols = _columns(cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 需要 BOM 才能正确识别中文
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: v for k, v in asdict(r).items() if k in cols})


def _md_cell(v) -> str:
    if v is None:
        return ""
    # 转义竖线，否则破坏表格结构。禁止加粗（全局约定）
    return str(v).replace("|", r"\|").replace("\n", " ")


def write_markdown(rows: list, path: Path, cfg) -> None:
    cols = _columns(cfg)
    s, u = _window_text(cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [
        f"# {cfg.org} 贡献者名单",
        "",
        f"统计区间: {s} ~ {u}（含两端，UTC）",
        f"fork 口径: --include-forks={cfg.include_forks}",
        "",
    ]
    if cfg.count_lines:
        out += [f"提示: {LINE_CAVEAT}", ""]
    out.append("| " + " | ".join(cols) + " |")
    out.append("|" + "|".join(["---"] * len(cols)) + "|")
    for r in rows:
        d = asdict(r)
        out.append("| " + " | ".join(_md_cell(d[c]) for c in cols) + " |")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_json(rows: list, path: Path, cfg) -> None:
    cols = _columns(cfg)
    s, u = _window_text(cfg)
    meta = {
        "org": cfg.org, "since": s, "until": u, "timezone": "UTC",
        "include_forks": cfg.include_forks,
        "count_lines": cfg.count_lines,
    }
    if cfg.count_lines:
        meta["caveat"] = LINE_CAVEAT
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "contributors": [
            {k: v for k, v in asdict(r).items() if k in cols} for r in rows
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_output.py -v`
Expected: 16 passed

- [ ] **Step 5: 提交**

```bash
git add contributors/output.py tests/test_output.py
git commit -m "feat: 实现 CSV/Markdown/JSON 三种格式输出"
```

---

### Task 9: 主流程编排、错误边界与端到端集成测试

**Files:**
- Create: `contributors/main.py`
- Create: `contributors/__main__.py`
- Create: `README.md`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: 全部前序模块
- Produces:
  - `build_rows(repo, git_stats: dict, api_stats: dict, resolver, cfg, upstream_counts: dict = None) -> tuple[list[Row], list[Row]]` — 返回 (正常行, bot 行)
  - `process_repo(repo, cm, cfg, client, resolver, token="") -> tuple[list[Row], list[Row]]`
  - `run(cfg, token) -> int` — 返回进程退出码
  - `main(argv=None) -> int`

**注意：** Task 9 的测试 monkeypatch 了 `contributors.main` 里的 `fetch_repos` 与 `collect_api_stats`，因此实现中必须用 `from .repos import fetch_repos` 这种形式导入到模块命名空间，不可写成 `repos.fetch_repos(...)` 的调用形式，否则 monkeypatch 不生效。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_integration.py`：

```python
"""端到端测试：现场造小仓库跑完整 git 管线，不碰网络。"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import pytest
from contributors.config import Config, DEFAULT_EXCLUDE_PATHS
from contributors.cache import CacheManager
from contributors.git_stats import collect_git_stats
from contributors.models import RepoInfo
from contributors.main import build_rows, run
from contributors.identity import IdentityResolver
from contributors.models import ApiStats


def git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def tiny_repo(tmp_path):
    """造一个含两位作者、一个未合并分支的裸库。"""
    work = tmp_path / "work"
    work.mkdir()
    git(["init", "-q", "-b", "main"], work)
    git(["config", "user.email", "alice@example.com"], work)
    git(["config", "user.name", "Alice"], work)
    (work / "a.py").write_text("print(1)\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m", "first", "--date=2026-01-15T00:00:00Z"], work)

    # 第二位作者，在未合并的分支上提交 —— 验证 --all 覆盖所有分支
    git(["checkout", "-q", "-b", "feature"], work)
    git(["config", "user.email", "bob@example.com"], work)
    git(["config", "user.name", "Bob"], work)
    (work / "b.py").write_text("print(2)\nprint(3)\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m", "second", "--date=2026-02-20T00:00:00Z"], work)

    # 窗口外的提交，必须被排除
    (work / "old.py").write_text("old\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m", "too old", "--date=2020-01-01T00:00:00Z"], work)

    bare = tmp_path / ".cache" / "repos" / "tiny.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--mirror", "-q", str(work), str(bare)], tmp_path)
    return tmp_path


def mk_cfg(tmp_path, **kw):
    base = dict(org="test",
                since=datetime(2025, 7, 28, tzinfo=timezone.utc),
                until=datetime(2026, 7, 29, tzinfo=timezone.utc),
                include_forks="all", max_repo_size=2048,
                # 必须显式传入：Config.exclude_paths 默认为空列表，
                # 只有 parse_args 才会填入 DEFAULT_EXCLUDE_PATHS。
                # 漏传会让 --count-lines 的过滤测试假通过。
                exclude_paths=list(DEFAULT_EXCLUDE_PATHS),
                cache_dir=str(tmp_path / ".cache"),
                out_dir=str(tmp_path / "output"))
    base.update(kw)
    return Config(**base)


def mk_repo():
    return RepoInfo(name="tiny", default_branch="main", size_mb=1.0,
                    pushed_at="2026-02-20T00:00:00Z", is_fork=False,
                    upstream=None, upstream_family=None, archived=False)


def test_collects_commits_from_all_branches(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    # bob 的提交只在未合并的 feature 分支上，--all 必须捕获
    assert stats["alice@example.com"].commits == 1
    assert stats["bob@example.com"].commits == 1


def test_excludes_commits_outside_window(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    total = sum(s.commits for s in stats.values())
    assert total == 2  # 2020 年那条被排除


def test_line_counts_when_enabled(tiny_repo):
    cfg = mk_cfg(tiny_repo, count_lines=True)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    assert stats["bob@example.com"].additions == 2


def test_no_fetch_recompute_is_offline(tiny_repo):
    # 已有缓存 + --no-fetch 应能纯本地重算
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    assert sum(s.commits for s in stats.values()) == 2


def test_narrowing_window_reduces_counts_without_network(tiny_repo):
    cfg = mk_cfg(tiny_repo, no_fetch=True,
                 since=datetime(2026, 2, 1, tzinfo=timezone.utc))
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    assert "alice@example.com" not in stats
    assert stats["bob@example.com"].commits == 1


def test_build_rows_merges_git_and_api_by_login(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "alice")
    api = {"alice": ApiStats(pr_created=3, pr_merged=2, pr_reviewed=1)}
    rows, bots = build_rows(mk_repo(), gstats, api, resolver, cfg)
    alice = [r for r in rows if r.login == "alice"][0]
    assert alice.commits == 1
    assert alice.pr_created == 3
    assert alice.github_url == "https://github.com/alice"


def test_unmatched_email_still_appears_in_rows(tiny_repo):
    # bob 无 login 映射，绝不能被丢弃
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    rows, _ = build_rows(mk_repo(), gstats, {}, IdentityResolver(), cfg)
    assert any(r.email == "bob@example.com" for r in rows)


def test_bots_are_separated_from_main_rows(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "dependabot[bot]")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert any(r.login == "dependabot[bot]" for r in bots)
    assert not any(r.login == "dependabot[bot]" for r in rows)


def test_run_writes_all_output_files(tiny_repo, monkeypatch):
    """完整 run() 但用 monkeypatch 掉网络部分。"""
    import contributors.main as m
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    monkeypatch.setattr(m, "fetch_repos", lambda org, token: [mk_repo()])
    monkeypatch.setattr(m, "collect_api_stats",
                        lambda repo, cfg, client, cm: ({}, {}))
    code = m.run(cfg, token="fake")
    assert code == 0
    out = Path(cfg.out_dir)
    for f in ["summary.csv", "by_repo.csv", "contributors.md",
              "contributors.json", "unmatched.csv", "bots.csv",
              "run_meta.json"]:
        assert (out / f).is_file(), f"缺少输出文件 {f}"
    assert (out / "repos" / "tiny.csv").is_file()


def test_run_meta_records_skipped_repos(tiny_repo, monkeypatch):
    import json
    import contributors.main as m
    big = RepoInfo(name="huge", default_branch="main", size_mb=99999.0,
                   pushed_at="2026-07-01T00:00:00Z", is_fork=False,
                   upstream=None, upstream_family=None, archived=False)
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    monkeypatch.setattr(m, "fetch_repos", lambda org, token: [mk_repo(), big])
    monkeypatch.setattr(m, "collect_api_stats",
                        lambda repo, cfg, client, cm: ({}, {}))
    m.run(cfg, token="fake")
    meta = json.loads((Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    assert "huge" in meta["skipped"]
    assert meta["window"]["since"] == "2025-07-28"


def test_single_repo_failure_does_not_abort_run(tiny_repo, monkeypatch):
    import json
    import contributors.main as m
    bad = RepoInfo(name="bad", default_branch="main", size_mb=1.0,
                   pushed_at="2026-07-01T00:00:00Z", is_fork=False,
                   upstream=None, upstream_family=None, archived=False)
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    monkeypatch.setattr(m, "fetch_repos", lambda org, token: [mk_repo(), bad])
    monkeypatch.setattr(m, "collect_api_stats",
                        lambda repo, cfg, client, cm: ({}, {}))
    code = m.run(cfg, token="fake")
    meta = json.loads((Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    # bad 无本地缓存且 no_fetch，应记入 failures 而非崩溃
    assert "bad" in meta["failures"]
    assert code == 0
    # tiny 的数据仍然产出
    assert (Path(cfg.out_dir) / "repos" / "tiny.csv").is_file()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_integration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contributors.main'`

- [ ] **Step 3: 写最小实现**

创建 `contributors/main.py`：

```python
"""主流程编排。

核心原则：单仓库失败不影响整体，永不静默丢数据。
每个仓库包在独立 try 里，失败记入 failures 并继续。
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api_stats import GitHubGraphQL, collect_api_stats
from .auth import AuthError, get_token
from .cache import CacheManager
from .config import parse_args
from .git_stats import (
    GitError, clone_or_fetch, collect_git_stats, count_upstream_excluded,
)
from .identity import IdentityResolver, is_bot
from .models import ApiStats
from .output import (
    LINE_CAVEAT, Row, summarize, write_csv, write_json, write_markdown,
)
from .repos import fetch_repos, filter_repos


def build_rows(repo, git_stats: dict, api_stats: dict,
               resolver: IdentityResolver, cfg, upstream_counts=None) -> tuple:
    """把 git 侧与 API 侧按归并键合并成输出行。"""
    upstream_counts = upstream_counts or {}
    buckets: dict = {}

    # git 侧：以 email 为起点，尽力解析出 login
    for email, gs in git_stats.items():
        login = resolver.resolve(email)
        key = resolver.merge_key(email, login)
        b = buckets.setdefault(key, {
            "login": login, "names": set(), "emails": set(),
            "git": None, "api": ApiStats(), "up": 0,
        })
        if b["git"] is None:
            b["git"] = gs
        else:
            b["git"].commits += gs.commits
            for f in ("additions", "deletions", "files_changed",
                      "additions_raw", "deletions_raw"):
                a, c = getattr(b["git"], f), getattr(gs, f)
                if a is not None or c is not None:
                    setattr(b["git"], f, (a or 0) + (c or 0))
        b["names"].update(gs.names)
        b["emails"].add(email)
        b["up"] += upstream_counts.get(email, gs.commits)

    # API 侧：以 login 为键
    for login, ast in api_stats.items():
        key = f"login:{login.strip().lower()}"
        b = buckets.setdefault(key, {
            "login": login, "names": set(), "emails": set(),
            "git": None, "api": ApiStats(), "up": 0,
        })
        b["login"] = b["login"] or login
        b["api"] = ast

    rows, bots = [], []
    for key, b in buckets.items():
        gs = b["git"]
        login = b["login"]
        name = sorted(b["names"])[0] if b["names"] else (login or "")
        email = ";".join(sorted(b["emails"]))
        bot = is_bot(login, name, email)
        row = Row(
            repo=repo.name,
            login=login or "",
            name=name,
            email=email,
            github_url=f"https://github.com/{login}" if login else "",
            commits=gs.commits if gs else 0,
            commits_not_in_upstream=b["up"] if gs else 0,
            pr_created=b["api"].pr_created,
            pr_merged=b["api"].pr_merged,
            pr_reviewed=b["api"].pr_reviewed,
            issue_created=b["api"].issue_created,
            issue_commented=b["api"].issue_commented,
            is_fork=repo.is_fork,
            upstream=repo.upstream or "",
            upstream_family=repo.upstream_family or "",
            is_bot=bot,
            additions=gs.additions if gs else None,
            deletions=gs.deletions if gs else None,
            files_changed=gs.files_changed if gs else None,
            additions_raw=gs.additions_raw if gs else None,
            deletions_raw=gs.deletions_raw if gs else None,
        )
        (bots if bot else rows).append(row)

    if cfg.include_bots:
        rows.extend(bots)
        bots = []
    return rows, bots


def process_repo(repo, cm, cfg, client, resolver, token="") -> tuple:
    clone_or_fetch(repo, cm, cfg, token)
    gstats = collect_git_stats(repo, cm, cfg)

    api: dict = {}
    if client is not None:
        api, mapping = collect_api_stats(repo, cfg, client, cm)
        for email, login in mapping.items():
            resolver.add_mapping(email, login)

    ups = {}
    if repo.is_fork and repo.upstream:
        try:
            ups = count_upstream_excluded(repo, cm, cfg)
        except GitError:
            # 上游 fetch 失败不该让整个仓库失败，退化为等于 commits
            ups = {}

    return build_rows(repo, gstats, api, resolver, cfg, ups)


def run(cfg, token: str) -> int:
    cm = CacheManager(cfg.cache_dir)
    out = Path(cfg.out_dir)
    started = time.time()

    all_repos = fetch_repos(cfg.org, token)
    kept, skipped = filter_repos(all_repos, cfg)

    need = cm.estimate_needed_mb(kept)
    free = cm.free_space_mb()
    if not cfg.no_fetch and need > free:
        print(f"磁盘空间不足：预计需要 {need:.0f} MB，可用 {free:.0f} MB")
        return 2

    print(f"共 {len(all_repos)} 个仓库，跳过 {len(skipped)} 个，"
          f"待处理 {len(kept)} 个")
    for name, reason in sorted(skipped.items()):
        print(f"  跳过 {name}：{reason}")

    client = None if cfg.no_fetch else GitHubGraphQL(token)
    resolver = IdentityResolver()
    rows, bots, failures = [], [], {}

    for i, repo in enumerate(kept, 1):
        print(f"[{i}/{len(kept)}] {repo.name} ...", flush=True)
        try:
            r, b = process_repo(repo, cm, cfg, client, resolver, token)
            rows.extend(r)
            bots.extend(b)
        except (GitError, RuntimeError, OSError) as exc:
            failures[repo.name] = str(exc)[:500]
            print(f"  失败：{exc}")

    # --- 输出 ---
    out.mkdir(parents=True, exist_ok=True)
    per_repo = {}
    for r in rows:
        per_repo.setdefault(r.repo, []).append(r)

    want = cfg.fmt
    if want in ("csv", "all"):
        write_csv(summarize(rows), out / "summary.csv", cfg)
        write_csv(rows, out / "by_repo.csv", cfg)
        for name, rs in per_repo.items():
            write_csv(rs, out / "repos" / f"{name}.csv", cfg)
    if want in ("md", "all"):
        write_markdown(summarize(rows), out / "contributors.md", cfg)
    if want in ("json", "all"):
        write_json(summarize(rows), out / "contributors.json", cfg)

    # 未归并邮箱与 bot 永远单独输出，不受 --format 影响
    _write_simple_csv(
        out / "unmatched.csv", ["email"],
        [{"email": e} for e in sorted(resolver.unmatched_emails())],
    )
    write_csv(bots, out / "bots.csv", cfg)

    since_s = cfg.since.date().isoformat()
    until_s = (cfg.until - timedelta(days=1)).date().isoformat()
    meta = {
        "org": cfg.org,
        "window": {"since": since_s, "until": until_s, "timezone": "UTC"},
        "include_forks": cfg.include_forks,
        "max_repo_size_mb": cfg.max_repo_size,
        "count_lines": cfg.count_lines,
        "line_caveat": LINE_CAVEAT if cfg.count_lines else None,
        "repos_total": len(all_repos),
        "repos_processed": len(kept) - len(failures),
        "skipped": skipped,
        "failures": failures,
        "contributors": len(summarize(rows)),
        "bots_excluded": len({r.login for r in bots}),
        "unmatched_emails": len(resolver.unmatched_emails()),
        "api_points_spent": getattr(client, "spent", 0),
        "api_points_remaining": getattr(client, "remaining", None),
        "elapsed_seconds": round(time.time() - started, 1),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n统计区间: {since_s} ~ {until_s}（UTC，含两端）")
    print(f"贡献者 {meta['contributors']} 人，输出目录 {out}")
    if resolver.unmatched_emails():
        print(f"注意：{len(resolver.unmatched_emails())} 个邮箱未能关联 "
              f"GitHub 账号，见 unmatched.csv")
    if failures:
        print(f"注意：{len(failures)} 个仓库处理失败，见 run_meta.json")
    return 0


def _write_simple_csv(path: Path, cols: list, records: list) -> None:
    import csv as _csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(records)


def main(argv=None) -> int:
    cfg = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        token = "" if cfg.no_fetch else get_token()
    except AuthError as exc:
        print(f"认证失败：{exc}")
        return 2
    try:
        return run(cfg, token)
    except KeyboardInterrupt:
        print("\n已中断。已完成仓库的缓存保留，下次运行将复用。")
        return 130
```

创建 `contributors/__main__.py`：

```python
import sys
from .main import main

if __name__ == "__main__":
    sys.exit(main())
```

创建 `README.md`：

```markdown
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
| unmatched.csv | 未能关联 GitHub 账号的邮箱，需人工确认 |
| bots.csv | 被识别为 bot 的账号，供核对是否误判 |
| run_meta.json | 运行参数、跳过与失败的仓库、API 用量 |

## 缓存

首次运行克隆全部仓库到 `./.cache/repos/`（一年窗口约 3 GB），之后只做增量
fetch。改时间窗时用 `--no-fetch` 可完全离线重算。

## 注意事项

- 时间口径统一为 UTC，区间含两端
- fork 分为 self / external / tooling 三类，用 `--include-forks` 控制纳入范围
- 代码行数指标默认关闭，且不宜用于排名（易被生成文件污染）
- 未能关联 GitHub 账号的贡献者不会被丢弃，会列入 unmatched.csv
```

- [ ] **Step 4: 运行全部测试确认通过**

Run: `pytest -m "not network" -v`
Expected: 全部通过（约 126 个测试）

- [ ] **Step 5: 真实单仓库验证**

Run: `python -m contributors --repos dpdata --since 2026-01-01`
Expected: 正常输出，`output/summary.csv` 含真实贡献者，`run_meta.json` 中 `failures` 为空

- [ ] **Step 6: 提交**

```bash
git add contributors/main.py contributors/__main__.py README.md tests/test_integration.py
git commit -m "feat: 实现主流程编排与端到端集成测试"
```

---

## Self-Review 结果

对照 spec 逐节检查覆盖情况：

| Spec 章节 | 覆盖 task | 说明 |
|---|---|---|
| 1 目标与用途 | Task 8, 9 | 输出面向表彰用途；unmatched 不丢数据 |
| 2.1 fork 三分类 | Task 3 | 用 spec 实测数据做参数化测试 |
| 2.2 pushed_at 预筛 | Task 3 | 含边界同日不跳过的测试 |
| 2.3 所有分支统计 | Task 6, 9 | 集成测试造未合并分支验证 |
| 3.1 两条管线 | Task 6, 7, 9 | build_rows 按 login 合并 |
| 3.1 email→login 反查 | Task 4, 7 | noreply 解析 + GraphQL 顺带取回 |
| 3.2 身份归并 | Task 4 | 含多邮箱、bot 误判测试 |
| 3.3 模块划分 | 全部 | 另拆 models.py 与 auth.py |
| 4 缓存与增量 | Task 5 | 含损坏检测、断点续跑 |
| 5.2 参数表 | Task 2 | 全部参数有默认值测试 |
| 5.3 时间窗语义 | Task 2 | 闭区间、月末回退、互斥 |
| 5.4 fork 三档 | Task 3 | 三档各有测试 |
| 5.5 认证 | Task 3 | 环境变量 → gh 回退 |
| 6.1 字段 | Task 8 | 列顺序与 count_lines 条件列 |
| 6.2 文件结构 | Task 8, 9 | 集成测试断言全部文件存在 |
| 6.3 行数局限 | Task 6, 8 | 过滤前后双数字 + caveat 文本 |
| 7.1 错误边界 | Task 9 | 单仓库失败不中断的测试 |
| 7.2 API 配额 | Task 7 | 退避重试 + 额度追踪测试 |
| 7.3 git 失败 | Task 5, 6 | 损坏重建、超时 |
| 7.4 可中断续跑 | Task 5, 9 | progress + KeyboardInterrupt |
| 7.5 日志 | Task 9 | 进度打印 + run_meta |
| 8 测试策略 | 全部 | 三层测试齐备 |

无占位符；类型与函数签名跨 task 一致（`Config`、`RepoInfo`、`GitStats`、`ApiStats`、`Row`、`IdentityResolver` 在各 task 中命名统一）。

自审中发现并已修正的三处问题：

1. `build_rows` 原本同时出现在 Task 8 与 Task 9 的 Interfaces 中，且签名不同。它需要 git 与 API 两侧数据，属编排职责，已明确归入 Task 9 的 `main.py`，并在 Task 8 加注说明。
2. Task 9 的集成测试 monkeypatch `contributors.main.fetch_repos`，故实现必须用 `from .repos import fetch_repos` 导入到模块命名空间。已在 Interfaces 中写明，否则 monkeypatch 静默失效、测试假通过。
3. `Config.exclude_paths` 默认为空列表（只有 `parse_args` 才填入 `DEFAULT_EXCLUDE_PATHS`），集成测试的 `mk_cfg` 漏传会让 `--count-lines` 的过滤测试假通过。已在测试辅助函数中显式传入并加注释。

一处 spec 未明确、实现时已决定的细节：spec 说 API 缓存按天，但未说 `--refresh` 是否绕过 API 缓存 —— Task 7 实现为绕过，与 git 侧 `--refresh` 语义一致。

## 实测验证过的技术假设

写计划前已在真实环境验证，实现时不要改动这些决策：

| 假设 | 验证结果 |
|---|---|
| `--mirror` 与 `--filter=blob:none` 可共用 | 成立，dpdata 克隆成功 |
| `git log --all` 按 SHA 天然去重 | 成立，473 条 = 473 个唯一 SHA |
| `%aI` 返回 UTC | 不成立，实测返回作者本地时区（如 +08:00），故过滤交给 git 的 `--since/--until` 并统一传 UTC |
| GraphQL 能取回 commit 的 `author.user.login` | 成立，cost 仅 1，是免费的 email→login 来源 |
| 逐用户查 `reviewed-by:` / `commenter:` 可行 | 不可行，约 100 人 × 67 仓库 = 数千请求，改为按仓库分页并从节点读取 |
| `[bot]` 后缀足以识别机器人 | 不足，实测 `njzjz-bot`、`pre-commit-ci`、`codecov` 均无后缀，必须配黑名单 |
| 对 "bot" 子串匹配安全 | 不安全，`botelho`、`Botspot`、`bot50` 是真实用户，会误伤表彰名单 |


