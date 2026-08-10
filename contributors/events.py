"""事件数据结构与从两条管线提取事件。

第二期新增。与第一期的计数逻辑完全并行 —— 事件只做旁路留存，不参与
任何统计。这是刻意的：第一期的计数踩了四个坑才修对（时间窗按 author
date、身份归并、fork 上游排除、PR 分支重复计数），把计数改成"从事件
表重算"等于把这些逻辑迁移一遍，回归风险不划算。

因此本模块的产出只用于"讲故事"（今天社区发生了什么），排名次仍然走
第一期的 CSV。两套数字在 squash 等场景下可能对不齐，这是已知且接受的。
"""
from dataclasses import dataclass
from typing import Optional

# 事件标题在库里和卡片上都不需要完整版本，超长的截断。
# 取 200 是因为 GitHub PR 标题上限 256，留点余量又不至于撑爆卡片。
MAX_TITLE = 200


@dataclass
class Event:
    """一条事件。kind 决定哪些字段有意义。

    commit：sha 有值，number 为 None
    pr / issue：number 有值，sha 为 None
    state 仅 pr 有意义（open / merged / closed）
    """
    kind: str
    repo: str
    number: Optional[int]
    sha: Optional[str]
    title: str
    url: str
    author_login: Optional[str]
    event_time: str          # UTC ISO8601
    state: Optional[str] = None

    @property
    def key(self) -> str:
        """全局唯一键。

        带 repo 是因为同一个 SHA 可能出现在多个仓库里（fork 场景下
        上游提交在两边都有），那是两条独立事件。
        """
        if self.kind == "commit":
            return f"commit:{self.repo}:{self.sha}"
        return f"{self.kind}:{self.repo}:{self.number}"


@dataclass
class StateChange:
    """PR 的状态变更。

    单独成表而非只看"新增行"，是因为 PR 不是一次性事件：上周创建、
    今天合并，event_key 没变但 state 变了。只把新增行当增量的话，
    今天的战报里根本不会出现这个 PR —— 而合并恰恰是社区最关心的事。
    """
    event_key: str
    kind: str
    repo: str
    number: Optional[int]
    title: str
    url: str
    old_state: Optional[str]
    new_state: Optional[str]


def clip_title(text: str) -> str:
    """取首行并截断。

    commit message 的首行才是标题，正文（含 Co-authored-by 等尾部
    元数据）不要。换行符若带进卡片会破坏排版。
    """
    if not text:
        return ""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(first) > MAX_TITLE:
        return first[:MAX_TITLE - 1] + "…"
    return first


def commit_event(repo: str, org: str, sha: str, title: str,
                 when: str, login: Optional[str] = None) -> Event:
    return Event(
        kind="commit", repo=repo, number=None, sha=sha,
        title=clip_title(title),
        url=f"https://github.com/{org}/{repo}/commit/{sha}",
        author_login=login, event_time=when, state=None,
    )


def extract_pr_events(nodes: list, repo: str, org: str) -> list:
    """从 GraphQL 的 pullRequests.nodes 提取事件。

    不做窗口过滤：窗口是第一期计数的概念，事件表要的是"这次看到了什么"。
    过滤交给调用方（只在窗口内的 PR 才被查回来）。

    容错原则与 api_stats._login_of 一致：已删号用户的 author 为 null，
    字段缺失一律降级为空值而非抛异常 —— 少一条事件比整次运行崩掉好。
    """
    out = []
    for pr in nodes or []:
        num = pr.get("number")
        if num is None:
            continue
        state = "merged" if pr.get("merged") else "open"
        out.append(Event(
            kind="pr", repo=repo, number=num, sha=None,
            title=clip_title(pr.get("title") or ""),
            url=pr.get("url") or f"https://github.com/{org}/{repo}/pull/{num}",
            author_login=(pr.get("author") or {}).get("login"),
            event_time=pr.get("createdAt") or "",
            state=state,
        ))
    return out


def extract_issue_events(nodes: list, repo: str, org: str) -> list:
    """从 GraphQL 的 issues.nodes 提取事件。"""
    out = []
    for it in nodes or []:
        num = it.get("number")
        if num is None:
            continue
        out.append(Event(
            kind="issue", repo=repo, number=num, sha=None,
            title=clip_title(it.get("title") or ""),
            url=it.get("url") or f"https://github.com/{org}/{repo}/issues/{num}",
            author_login=(it.get("author") or {}).get("login"),
            event_time=it.get("createdAt") or "",
            state=None,
        ))
    return out
