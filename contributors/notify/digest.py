"""把库里的变化聚合成战报结构。

不涉及任何飞书概念 —— 产出的 Digest 是纯数据，网页看板或多维表格
将来同样可以消费它。
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# 每类最多列几条。首次运行或长期未跑后重跑，可能一次涌进几百条事件，
# 不截断会撑爆群消息（飞书请求体上限 20 KB）。
MAX_ITEMS = 10

CST = timezone(timedelta(hours=8))


@dataclass
class Item:
    """战报里的一行。"""
    repo: str
    number: Optional[int]
    title: str
    url: str
    author: str = ""


@dataclass
class Digest:
    """一次战报的全部内容。"""
    commit_count: int = 0
    # commit 明细。平时不渲染，仅当没有任何 PR/issue 时兜底列出。
    commits: list = field(default_factory=list)
    merged_prs: list = field(default_factory=list)
    opened_prs: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    since_text: str = ""
    generated_at: str = ""
    total_contributors: Optional[int] = None
    total_commits: Optional[int] = None
    # 底部累计的 PR/issue，口径与 summary.csv 的同名列一致（按人累加）
    total_prs_merged: Optional[int] = None
    total_prs_created: Optional[int] = None
    total_issues: Optional[int] = None
    # 本次跑了几个仓库。跑子集时底部要标注范围，否则局部数字看起来
    # 像社区总量 —— 2026-08-10 的首条真实战报正是栽在这里。
    repos_processed: Optional[int] = None
    repos_total: Optional[int] = None
    # 距上次成功推送的小时数。None 表示从没成功推送过。
    # 用它决定文案：约一天内写"昨日社区动态"，超出则回退到
    # "自上次汇报（X）以来" —— 推送失败补推时卡片里装的是两天的内容。
    hours_since_notify: Optional[float] = None
    # 本次未能抓取的仓库。底部的"36/38 个仓库"只给数字，看不出少了谁 ——
    # 列出名字，读日报的人才能判断自己关心的仓库在不在里面。
    failed_repos: list = field(default_factory=list)
    # 昨日无更新时贴的近期活动量。空标签表示三级回退都没找到数据。
    # 只存数字不存明细 —— 这是"证明系统在正常工作"的旁证，不是内容。
    recent_label: str = ""
    recent_counts: dict = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """真正无内容可发。

        有回退数据时不算空：那条"昨日无更新 + 本周至今 N 次提交"是要发的，
        不发的话群里第一反应是脚本挂了。
        """
        no_increment = (self.commit_count == 0 and not self.merged_prs
                        and not self.opened_prs and not self.issues)
        return no_increment and not self.recent_counts

    @property
    def merged_count(self) -> int:
        return len(self.merged_prs)

    @property
    def opened_count(self) -> int:
        return len(self.opened_prs)

    @property
    def issue_count(self) -> int:
        return len(self.issues)


def to_cst_text(iso: str) -> str:
    """UTC ISO 转东八区显示文本。

    库里一律存 UTC，只在展示层转 —— 存储用绝对时间，展示用本地时间。
    """
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(CST).strftime("%m-%d %H:%M")


def _hours_between(iso: str, ref: datetime) -> Optional[float]:
    """上次成功推送到现在隔了多少小时。取不到时返回 None。

    解析失败一律返回 None（当作"没有上次"），这样文案会走保守的
    "自上次汇报以来"分支 —— 宁可啰嗦，不可说谎。
    """
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (ref - t).total_seconds() / 3600


def _item(ev) -> Item:
    return Item(repo=ev.repo, number=ev.number, title=ev.title, url=ev.url,
                author=ev.author_login or "")


def build(pending, last_notify_at: str = "",
          total_contributors: Optional[int] = None,
          total_commits: Optional[int] = None,
          total_prs_merged: Optional[int] = None,
          total_prs_created: Optional[int] = None,
          total_issues: Optional[int] = None,
          repos_processed: Optional[int] = None,
          repos_total: Optional[int] = None,
          failed_repos: Optional[list] = None,
          recent_label: str = "",
          recent_counts: Optional[dict] = None,
          now: Optional[datetime] = None) -> Digest:
    """把 store 查出的待推送变化聚合成战报。

    合并的 PR 有两个来源，都要算进去：
    1. 状态从 open 变 merged 的（上周创建、今天合并）
    2. 首次看到时就已经是 merged 的（创建与合并都发生在两次运行之间）
    只取其一都会漏。同一个 PR 不会同时出现在两处 —— 首次看到就走
    新增路径，之后状态变更才走变更路径。
    """
    d = Digest()

    merged_keys = set()
    for chg in pending.state_changes:
        if chg.new_state == "merged":
            d.merged_prs.append(Item(chg.repo, chg.number, chg.title, chg.url,
                                     chg.author_login or ""))
            merged_keys.add(chg.event_key)

    for ev in pending.new_events:
        if ev.kind == "commit":
            d.commit_count += 1
            # 明细也留着：平时不展示（一天几十条会刷屏），但当天没有任何
            # PR/issue 时，卡片就只剩一个数字，那时才拿出来列。
            d.commits.append(_item(ev))
        elif ev.kind == "pr":
            if ev.state == "merged":
                if ev.key not in merged_keys:
                    d.merged_prs.append(_item(ev))
            else:
                d.opened_prs.append(_item(ev))
        elif ev.kind == "issue":
            d.issues.append(_item(ev))

    d.since_text = to_cst_text(last_notify_at)
    ref = now or datetime.now(timezone.utc)
    d.generated_at = ref.astimezone(CST).strftime("%Y-%m-%d")
    d.hours_since_notify = _hours_between(last_notify_at, ref)
    d.total_contributors = total_contributors
    d.total_commits = total_commits
    d.total_prs_merged = total_prs_merged
    d.total_prs_created = total_prs_created
    d.total_issues = total_issues
    d.repos_processed = repos_processed
    d.repos_total = repos_total
    d.failed_repos = list(failed_repos or [])
    d.recent_label = recent_label
    d.recent_counts = dict(recent_counts or {})
    return d
