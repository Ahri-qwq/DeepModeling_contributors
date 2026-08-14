"""GraphQL 侧统计：PR / Issue / Review / Comment。

为什么按仓库分页而不按用户查询：
reviewed-by: 与 commenter: 搜索需逐用户发一次请求，约 100 位贡献者
× 67 仓库 = 数千请求，必然撞限额。改为分页拉 PR/Issue 列表，在节点里
一并取回 reviews 与 comments 的作者，实测每页 cost 仅 1。

同时顺手取回 commit 的 author.email 与 author.user.login，
免费得到 email→login 映射（spec 第 3.1 节）。
"""
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from .models import ApiStats

GRAPHQL_URL = "https://api.github.com/graphql"
QUOTA_FLOOR = 500  # 剩余低于此值时主动暂停
# 分页上限防御。超出时打印警告而非静默截断（用户裁决）
MAX_PAGES = 400
# 分页早停的安全缓冲。判据是 createdAt，但老 PR 可能在窗口内才被合并
# 或评论，所以要往 since 之前多翻一段。90 天是权衡：开着超过三个月才
# 合并的 PR 极少，而这个余量已让 abacus-develop 从 306 次请求降到 63 次
# （实测 PR 45 页 + Issue 18 页）。觉得漏判就调大，觉得慢就调小 ——
# 只影响速度与漏判风险的取舍，不影响正确性。
EARLY_STOP_BUFFER = timedelta(days=90)

PR_QUERY = """
query($owner:String!,$name:String!,$cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    pullRequests(first:25, after:$cursor,
                 orderBy:{field:CREATED_AT,direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number createdAt merged title url
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
        number createdAt title url
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
            # 403/429 是限额，502/503 是服务端瞬时故障，都值得退避重试
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


def _oldest_created_at(nodes: list):
    """返回本页最早的 createdAt。任一条解析不了就返回 None。

    None 的语义是"不确定"，调用方据此选择继续翻页 —— 宁可多花请求
    也不漏数据。
    """
    oldest = None
    for n in nodes:
        raw = (n or {}).get("createdAt", "")
        if not raw:
            return None
        try:
            t = _parse_iso(raw)
        except ValueError:
            return None
        if oldest is None or t < oldest:
            oldest = t
    return oldest


def _paginate(client: GitHubGraphQL, query: str, org: str, name: str,
              root_key: str, log=print, since=None) -> list:
    """翻页取全部节点。传了 since 时在窗口外提前收手。

    早停的依据：两个查询都按 CREATED_AT DESC 排序，一旦某页最早的一条
    都早于窗口起点，后面只会更早。abacus-develop 实测 306 页里只有前
    六十几页落在窗口（含缓冲）内，其余两百多页拉回来立刻被 _in_window 丢掉。

    为什么要留 EARLY_STOP_BUFFER 而不是卡在 since 就停：判据是 createdAt，
    而一个几个月前创建的 PR 完全可能在窗口内才被合并或评论，那些事件同样
    要算。缓冲期就是为这种"老 PR 新动作"留的余量 —— 超过这个跨度还在
    活跃的 PR 极少，用一点额外请求换不漏判。
    """
    cutoff = since - EARLY_STOP_BUFFER if since is not None else None
    nodes, cursor = [], None
    for _ in range(MAX_PAGES):
        data = client.query(query, {"owner": org, "name": name, "cursor": cursor})
        conn = ((data.get("repository") or {}).get(root_key) or {})
        page = conn.get("nodes") or []
        nodes.extend(page)
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes
        if cutoff is not None and page:
            oldest = _oldest_created_at(page)
            # oldest 为 None 表示有条目日期解析不了，此时不敢断言后面更早
            if oldest is not None and oldest < cutoff:
                return nodes
        cursor = info.get("endCursor")
        client.guard_quota(log)
    # 到这里说明翻页未终止。绝不静默截断，明确告知用户
    log(f"警告：{name} 的 {root_key} 已达 {MAX_PAGES} 页上限，"
        "数据可能不完整。请缩小时间窗或提高 MAX_PAGES 后重跑")
    return nodes


def collect_api_stats(repo, cfg, client: GitHubGraphQL, cm) -> tuple:
    """返回 ({login: ApiStats}, {email: login})。同日缓存命中则读盘。"""
    day = datetime.now(timezone.utc).date().isoformat()
    cache_file = cm.api_path(repo.name, day)
    if cache_file.is_file() and not cfg.refresh:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        pr_nodes, issue_nodes = raw.get("prs", []), raw.get("issues", [])
    else:
        pr_nodes = _paginate(client, PR_QUERY, cfg.org, repo.name,
                             "pullRequests", since=cfg.since)
        issue_nodes = _paginate(client, ISSUE_QUERY, cfg.org, repo.name,
                                "issues", since=cfg.since)
        if not cfg.no_fetch:
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


def collect_api_events(repo, cfg, cm) -> list:
    """从当天的 API 缓存提取 PR / Issue 事件，返回 Event 列表。

    只读缓存不发请求：collect_api_stats 已经把当天的响应落盘了，事件
    提取纯属废物利用，API 点数消耗为零。缓存不在（比如 --no-fetch 且
    从未联网跑过）就返回空列表，不为了事件去额外发请求 —— 事件是附带
    产物，不值得增加限额压力。

    窗口过滤与 aggregate_prs 同口径，保证战报与 CSV 的量纲一致。
    """
    from .events import extract_issue_events, extract_pr_events

    day = datetime.now(timezone.utc).date().isoformat()
    cache_file = cm.api_path(repo.name, day)
    if not cache_file.is_file():
        return []

    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []

    pr_nodes = [n for n in raw.get("prs", [])
                if _in_window(n.get("createdAt", ""), cfg.since, cfg.until)]
    issue_nodes = [n for n in raw.get("issues", [])
                   if _in_window(n.get("createdAt", ""), cfg.since, cfg.until)]

    return (extract_pr_events(pr_nodes, repo.name, cfg.org)
            + extract_issue_events(issue_nodes, repo.name, cfg.org))

