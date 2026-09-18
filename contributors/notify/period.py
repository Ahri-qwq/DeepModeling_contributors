"""周报与月报：按事件真实时间聚合，汇总加排行榜。

与日报的口径不同，这是有意的：日报按 first_seen_run（本次运行新看到的）
判定以保证不漏报——author date 是代码写成时间，有人本地攒两周才推，按
事件时间判定就永远进不了任何一天的日报。而周报月报问的是"上周/上月发生
了什么"，那是时间概念，只能按 event_time 算。

两者混用会让七天日报之和对不上周报，那种数字打架最难向人解释。

不逐条列条目：一周几百条会把群消息刷屏，周月报的价值在趋势而非明细。
"""
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .card import SIZE_LIMIT, _escape

# 排行榜取前几名。周报取 5，月报取 10 —— 月度数据量大（实测一个月
# 1583 条事件、71 位贡献者），前 5 名的区分度不够。
TOP_N_WEEKLY = 5
TOP_N_MONTHLY = 10

from .classify import classify_title

CST = timezone(timedelta(hours=8))


@dataclass
class PeriodDigest:
    """一期周报或月报的全部内容。"""
    label: str = ""              # "上周" / "上月"
    range_text: str = ""         # "08-03 ~ 08-09"
    commit_count: int = 0
    merged_count: int = 0
    opened_count: int = 0
    issue_count: int = 0
    top_contributors: list = field(default_factory=list)   # [(login, n)]
    top_repos: list = field(default_factory=list)          # [(repo, n)]
    # 仓库 × 意图矩阵：[(repo, [(类别, n), ...]), ...]。
    # 口径与 top_repos 不同 —— 那个数全部事件，这个只数 PR，
    # 同一个仓库在两边的数字和排名都可能不一样。
    pr_breakdown: list = field(default_factory=list)
    contributor_total: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.commit_count or self.merged_count
                    or self.opened_count or self.issue_count)


def _cst_midnight_utc(d: datetime) -> str:
    """东八区某日零点对应的 UTC ISO 字符串。

    库里存 UTC，而"周一到周日"是本地概念。直接拿 UTC 日界当周界，
    会把周一早八点前的事件算到上一周去。
    """
    local = datetime(d.year, d.month, d.day, tzinfo=CST)
    return local.astimezone(timezone.utc).isoformat()


def last_week_range(now: Optional[datetime] = None) -> tuple:
    """上周一零点到本周一零点（东八区），返回 UTC 半开区间。"""
    ref = (now or datetime.now(timezone.utc)).astimezone(CST)
    this_monday = ref.date() - timedelta(days=ref.weekday())
    last_monday = this_monday - timedelta(days=7)
    return (_cst_midnight_utc(datetime(last_monday.year, last_monday.month,
                                       last_monday.day)),
            _cst_midnight_utc(datetime(this_monday.year, this_monday.month,
                                       this_monday.day)))


def last_month_range(now: Optional[datetime] = None) -> tuple:
    """上月一号零点到本月一号零点（东八区），返回 UTC 半开区间。"""
    ref = (now or datetime.now(timezone.utc)).astimezone(CST)
    first_this = datetime(ref.year, ref.month, 1)
    # 减一天落到上月最后一天，再取该月一号，跨年自然正确
    last_day_prev = first_this - timedelta(days=1)
    first_prev = datetime(last_day_prev.year, last_day_prev.month, 1)
    return (_cst_midnight_utc(first_prev), _cst_midnight_utc(first_this))


# 日报窗口的锚点小时（东八区）。窗口是"昨天这个点到今天这个点"。
# hour=0 即整个自然日 0:00~24:00。
DAILY_ANCHOR_HOUR = 0


def daily_range(day: Optional[date] = None, hour: int = DAILY_ANCHOR_HOUR,
                now: Optional[datetime] = None) -> tuple:
    """某日日报的窗口：前一天 hour 点到当天 hour 点（东八区），返回 UTC 半开区间。

    锚定整点而不是"此刻往前 24 小时"，是为了让补推可复现：0 点推送失败、
    1 点手动补推，若按"此刻往前 24 小时"算，昨天 0-1 点那一小时两个窗口
    都不覆盖，会永久漏掉。锚定后 8-13 的日报永远是同一个区间，跑几次都一样。

    切的是**入库时间**（events.first_seen_at），不是 git 时间 —— 理由见
    events 表 first_seen_at 列的注释。
    """
    if day is None:
        ref = (now or datetime.now(timezone.utc)).astimezone(CST)
        day = ref.date()
    end_local = datetime(day.year, day.month, day.day, hour, tzinfo=CST)
    start_local = end_local - timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat())


def this_week_range(now: Optional[datetime] = None) -> tuple:
    """本周一零点到此刻（东八区），返回 UTC 半开区间。"""
    ref = (now or datetime.now(timezone.utc)).astimezone(CST)
    monday = ref.date() - timedelta(days=ref.weekday())
    return (_cst_midnight_utc(datetime(monday.year, monday.month, monday.day)),
            ref.astimezone(timezone.utc).isoformat())


def this_month_range(now: Optional[datetime] = None) -> tuple:
    """本月一号零点到此刻（东八区），返回 UTC 半开区间。"""
    ref = (now or datetime.now(timezone.utc)).astimezone(CST)
    return (_cst_midnight_utc(datetime(ref.year, ref.month, 1)),
            ref.astimezone(timezone.utc).isoformat())


def count_kinds(events: list) -> dict:
    """按类型计数，供"无更新"时贴一句活动量用。只要数字，不要明细。"""
    out = {"commits": 0, "merged": 0, "opened": 0, "issues": 0}
    for ev in events:
        if ev.kind == "commit":
            out["commits"] += 1
        elif ev.kind == "pr":
            out["merged" if ev.state == "merged" else "opened"] += 1
        elif ev.kind == "issue":
            out["issues"] += 1
    return {k: v for k, v in out.items() if v}


def pick_recent(fetch, now: Optional[datetime] = None) -> tuple:
    """逐级回退找一个非零区间：本周至今 → 上周 → 本月至今。

    昨日无更新时，群里第一反应是"脚本挂了"。贴一个非零的近期活动量
    既能排除故障怀疑，也说明系统在正常工作。三级都为零时返回空标签，
    由调用方明说"近期无活动"——那本身也是有效信息。

    fetch 是 (start, end) -> events 的可调用对象，实际传的是
    store.events_between。纯本地 SQLite 查询，秒级返回，不发网络请求。
    """
    for label, rng in (("本周至今", this_week_range(now)),
                       ("上周", last_week_range(now)),
                       ("本月至今", this_month_range(now))):
        events = fetch(*rng)
        if events:
            return label, count_kinds(events)
    return "", {}


def range_text(start_iso: str, end_iso: str) -> str:
    """把 UTC 区间转成东八区的可读范围，右端闭区间显示。

    库里是半开区间 [start, end)，但写给人看时"08-03 ~ 08-10"会让人以为
    含 10 号，故显示 end 的前一天。
    """
    s = datetime.fromisoformat(start_iso).astimezone(CST)
    e = datetime.fromisoformat(end_iso).astimezone(CST) - timedelta(days=1)
    return f"{s.strftime('%m-%d')} ~ {e.strftime('%m-%d')}"


def build_period(events: list, label: str, rng: str,
                 top_n: int = TOP_N_WEEKLY) -> PeriodDigest:
    """把区间内的事件聚合成周报/月报结构。"""
    d = PeriodDigest(label=label, range_text=rng)
    by_author: dict = {}
    by_repo: dict = {}

    for ev in events:
        if ev.kind == "commit":
            d.commit_count += 1
        elif ev.kind == "pr":
            if ev.state == "merged":
                d.merged_count += 1
            else:
                d.opened_count += 1
        elif ev.kind == "issue":
            d.issue_count += 1

        # author 为 null 的事件（已删号用户）不进榜，否则会冒出空名字
        if ev.author_login:
            by_author[ev.author_login] = by_author.get(ev.author_login, 0) + 1
        if ev.repo:
            by_repo[ev.repo] = by_repo.get(ev.repo, 0) + 1

    # PR 意图矩阵。只统计 PR：commit 前缀规范度 44%、issue 只有 9%，
    # 拉进来大半是噪音；而且 commit 是过程不是意图（一个 PR 里混着
    # 一堆 fix typo、address review），按它分类会让改错别字和重写求解器
    # 占同样的权重。
    pr_cats: dict = {}
    for ev in events:
        if ev.kind != "pr" or not ev.repo:
            continue
        cats = pr_cats.setdefault(ev.repo, {})
        cat = classify_title(ev.title)
        cats[cat] = cats.get(cat, 0) + 1
    d.pr_breakdown = [
        (repo, sorted(cats.items(), key=lambda kv: (-kv[1], kv[0])))
        for repo, cats in sorted(
            pr_cats.items(),
            key=lambda kv: (-sum(kv[1].values()), kv[0]))[:top_n]
    ]

    d.contributor_total = len(by_author)
    d.top_contributors = sorted(by_author.items(),
                                key=lambda kv: (-kv[1], kv[0]))[:top_n]
    d.top_repos = sorted(by_repo.items(),
                         key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return d


def render_text(d: PeriodDigest) -> str:
    """周报/月报正文的 markdown。"""
    lines = [f"{d.label}（{d.range_text}）社区动态"]

    parts = []
    if d.commit_count:
        parts.append(f"{d.commit_count} 次提交")
    if d.merged_count:
        parts.append(f"{d.merged_count} 个 PR 合并")
    if d.opened_count:
        parts.append(f"{d.opened_count} 个 PR 新建")
    if d.issue_count:
        parts.append(f"{d.issue_count} 个 issue")
    lines.append(" · ".join(parts) if parts else "无活动")

    # 看板链接：环境变量未配置时静默跳过。文案跟日报保持一致 —— 同一个群里
    # 日报周报交替出现，同一个看板在两种卡片里叫不同名字，读的人会以为是
    # 两个不同的东西。多维表格链接故意不放：那张表是滚动一年的快照，与
    # "上周/上月"的周期对不上，并排出现反而误导。
    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    if dashboard_url:
        lines.append(f"[查看可互动图表看板]({dashboard_url})")

    if d.contributor_total:
        lines.append(f"共 {d.contributor_total} 位贡献者参与")

    if d.top_contributors:
        lines.append("")
        lines.append("**活跃贡献者**")
        for i, (login, n) in enumerate(d.top_contributors, 1):
            lines.append(f"{i}. {_escape(login)} · {n} 次活动")

    if d.top_repos:
        lines.append("")
        lines.append("**活跃仓库**")
        for i, (repo, n) in enumerate(d.top_repos, 1):
            lines.append(f"{i}. {_escape(repo)} · {n} 次活动")


    # PR 动向：回答「这些仓库在干什么」，而上面的活跃榜只回答
    # 「谁在动」。故意不写序号且措辞用「个 PR」：两个榜口径不同，
    # 同一仓库两边数字不一样，写成同样的「1. xxx · N」会让人以为在打架。
    if d.pr_breakdown:
        lines.append("")
        lines.append("**PR 动向**")
        for repo, cats in d.pr_breakdown:
            total = sum(n for _, n in cats)
            lines.append(f"{_escape(repo)} · {total} 个 PR")
            lines.append("　" + " · ".join(f"{c} {n}" for c, n in cats))
    return "\n".join(lines)


def render_period(d: PeriodDigest) -> dict:
    """渲染成飞书卡片。

    条目数固定（榜单各 5 条），不会像日报那样撑爆体积，故无需降级逻辑。
    """
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"DeepModeling 社区{d.label}汇总"},
                "template": "turquoise",
            },
            "elements": [
                {"tag": "div",
                 "text": {"tag": "lark_md", "content": render_text(d)}},
            ],
        },
    }
