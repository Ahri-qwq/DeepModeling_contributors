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

    @property
    def is_empty(self) -> bool:
        return (self.commit_count == 0 and not self.merged_prs
                and not self.opened_prs and not self.issues)

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
    d.total_contributors = total_contributors
    d.total_commits = total_commits
    d.total_prs_merged = total_prs_merged
    d.total_prs_created = total_prs_created
    d.total_issues = total_issues
    d.repos_processed = repos_processed
    d.repos_total = repos_total
    return d
