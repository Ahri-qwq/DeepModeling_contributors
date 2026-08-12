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
    exclude_bots: bool = False
    cache_dir: str = "./.cache"
    out_dir: str = "./output"
    fmt: str = "all"
    jobs: int = 4
    verbose: bool = False
    # 第二期：事件留存与飞书推送。默认留存但不推送 —— 推送有外部副作用，
    # 应显式开启，计划任务里才带 --notify。
    events: bool = True
    events_db: str = "./data/events.db"
    notify: bool = False
    notify_dry_run: bool = False
    notify_empty: bool = False
    # 把当前进度标记为已推送基线，划掉历史积压。首次部署时用一次。
    mark_notified: bool = False
    # 日常模式：只维护 by_repo.csv 与 summary.csv 两张总表。
    # 其余附表是全量统计时的人工核对材料，每天重写既慢又没人看。
    daily: bool = False
    # 重发库里最近一份战报存档。调通道、验排版时不必等真实增量。
    resend_last: bool = False
    # 拆分模式：抓取与推送分成两个计划任务。
    # no_notify   跑统计与事件留存，但不推送（抓取那步）
    # only_notify 只算增量并推送，跳过采集管线（推送那步）
    no_notify: bool = False
    only_notify: bool = False
    # 有仓库处理失败时以非零退出。默认关闭（第一期原则：单仓库失败不中断
    # 整体）。每天定时跑时打开，让计划任务据此跳过推送——否则数据不完整
    # 却照常推送，卡片底部写"1/2 个仓库"而没有任何警示。
    strict_repos: bool = False


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
    # 默认 0 = 不限。统计只需 commit 元数据，克隆走 --filter=blob:none，
    # 实际下载量与仓库标称体积无关（实测 sciencepedia 标称 17.5 GB，
    # 无 blob 克隆仅 412 MB），按标称体积设闸门是误伤。
    p.add_argument(
        "--max-repo-size", type=int, default=0,
        help="MB，超过则跳过；0 表示不限（默认）",
    )
    p.add_argument("--repos", default="", help="只跑指定仓库，逗号分隔")
    p.add_argument("--count-lines", action="store_true")
    p.add_argument(
        "--exclude-paths", default="", help="行数统计额外排除模式，逗号分隔"
    )
    p.add_argument("--no-fetch", action="store_true", help="零网络，纯本地缓存")
    p.add_argument("--refresh", action="store_true", help="强制重新 fetch")
    p.add_argument(
        "--exclude-bots", action="store_true",
        help="把 is_bot 账号移出主表（默认保留并标注，删掉比加回去简单）",
    )
    p.add_argument("--cache-dir", default="./.cache")
    p.add_argument("--out-dir", default=None,
                   help="输出目录。默认全量模式 ./output/all，"
                        "--daily 模式 ./output/daily")
    p.add_argument("--daily", action="store_true",
                   help="日常模式：只维护 by_repo.csv 与 summary.csv 两张总表，"
                        "跳过附表。适合每天跑一次更新总量")
    p.add_argument(
        "--format", dest="fmt", choices=["csv", "md", "json", "all"], default="all"
    )
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--verbose", action="store_true")
    p.add_argument(
        "--no-events", action="store_true",
        help="跳过事件留存，退回第一期行为",
    )
    p.add_argument("--events-db", default="./data/events.db",
                   help="事件历史库路径")
    p.add_argument("--notify", action="store_true",
                   help="跑完把增量战报推送到飞书群")
    p.add_argument("--notify-dry-run", action="store_true",
                   help="渲染卡片打到 stdout，不发送")
    p.add_argument("--notify-empty", action="store_true",
                   help="无新增时也推送（默认跳过，避免每天刷屏）")
    p.add_argument("--mark-notified", action="store_true",
                   help="把当前进度记为已推送基线，划掉历史积压。"
                        "首次建库后用一次，之后每天跑才是真正的增量")
    p.add_argument("--resend-last", action="store_true",
                   help="重发库里最近一份战报存档，不重新计算增量。"
                        "调通道或验排版时用，配合 --notify-dry-run 可只看不发")
    p.add_argument("--no-notify", action="store_true",
                   help="跑统计与事件留存但不推送。用于把抓取与推送拆成"
                        "两个计划任务时的抓取那一步")
    p.add_argument("--only-notify", action="store_true",
                   help="只计算增量并推送，跳过采集管线（秒级完成）。"
                        "用于拆分模式的推送那一步，须先跑过 --no-notify")
    p.add_argument("--strict-repos", action="store_true",
                   help="有仓库处理失败时以非零退出。每天定时跑时建议打开，"
                        "让计划任务据此跳过推送，避免推出不完整的数据")

    a = p.parse_args(argv)
    since_dt, until_dt = resolve_window(a.since, a.until, a.months, today)

    extra = [s.strip() for s in a.exclude_paths.split(",") if s.strip()]
    # 两种模式默认写到各自的目录，避免日常跑覆盖全量统计的产出
    out_dir = a.out_dir or ("./output/daily" if a.daily else "./output/all")
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
        exclude_bots=a.exclude_bots,
        cache_dir=a.cache_dir,
        out_dir=out_dir,
        fmt=a.fmt,
        jobs=a.jobs,
        verbose=a.verbose,
        events=not a.no_events,
        events_db=a.events_db,
        notify=a.notify,
        notify_dry_run=a.notify_dry_run,
        notify_empty=a.notify_empty,
        mark_notified=a.mark_notified,
        daily=a.daily,
        resend_last=a.resend_last,
        no_notify=a.no_notify,
        only_notify=a.only_notify,
        strict_repos=a.strict_repos,
    )
