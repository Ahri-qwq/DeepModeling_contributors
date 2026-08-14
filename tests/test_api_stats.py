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


def test_reviewer_and_commenter_counted_in_separate_columns():
    # carol 在 #1049 上 review、在 #1050 上评论，两种贡献分列计数
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


# --- 分页窗口早停 ---
#
# 起因（2026-08-14）：abacus-develop 有 4945 个 PR + 2691 个 issue，
# 按每页 25 条要连打 306 次 GraphQL 请求才翻到底。而查询按 CREATED_AT
# DESC 排序，窗口（近一年）内的数据全在最前面几十页，后面两百多页拉回来
# 的老数据立刻被 _in_window 丢掉 —— 纯属浪费，还把连续请求的时间拉长到
# 网络抖动几乎必然命中，该仓库因此连挂三天。


class _FakeClient:
    """按预设页面回放的假客户端，记录实际请求了几页。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0
        self.remaining = 5000

    def query(self, query, variables):
        page = self.pages[self.calls]
        self.calls += 1
        return {"repository": {"pullRequests": page}}

    def guard_quota(self, log=print):
        pass


def _page(dates, has_next=True):
    return {
        "nodes": [{"createdAt": d} for d in dates],
        "pageInfo": {"hasNextPage": has_next, "endCursor": "c"},
    }


def test_stops_once_page_falls_before_window():
    """整页都早于 since - 缓冲期时停止翻页，不再往下拉老数据。"""
    from contributors.api_stats import _paginate

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = _FakeClient([
        _page(["2026-06-01T00:00:00Z", "2026-05-01T00:00:00Z"]),
        _page(["2026-02-01T00:00:00Z", "2026-01-15T00:00:00Z"]),
        _page(["2020-01-01T00:00:00Z", "2019-01-01T00:00:00Z"]),  # 远早于窗口
        _page(["2018-01-01T00:00:00Z"]),                          # 不该请求到
    ])
    _paginate(client, "q", "org", "repo", "pullRequests", since=since)
    assert client.calls == 3


def test_keeps_going_within_buffer():
    """刚早于 since 但还在缓冲期内的要继续翻。

    理由：判据是 createdAt，而一个老 PR 可能在窗口内才被合并或评论。
    卡在 since 就停会漏掉"老 PR 新合并"这类事件。
    """
    from contributors.api_stats import _paginate

    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    client = _FakeClient([
        _page(["2026-07-01T00:00:00Z"]),
        _page(["2026-05-01T00:00:00Z"]),   # 早于 since，但在 90 天缓冲内
        _page(["2026-04-15T00:00:00Z"]),   # 仍在缓冲内
        _page(["2025-01-01T00:00:00Z"], has_next=False),  # 远超缓冲，到此为止
    ])
    _paginate(client, "q", "org", "repo", "pullRequests", since=since)
    assert client.calls == 4


def test_no_since_means_no_early_stop():
    """不传 since 时保持原行为，一路翻到底。"""
    from contributors.api_stats import _paginate

    client = _FakeClient([
        _page(["2020-01-01T00:00:00Z"]),
        _page(["2019-01-01T00:00:00Z"], has_next=False),
    ])
    _paginate(client, "q", "org", "repo", "pullRequests")
    assert client.calls == 2


def test_early_stop_keeps_nodes_it_already_read():
    """早停不能丢掉已经读到的节点。"""
    from contributors.api_stats import _paginate

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = _FakeClient([
        _page(["2026-06-01T00:00:00Z"]),
        _page(["2020-01-01T00:00:00Z"]),
        _page(["2019-01-01T00:00:00Z"]),
    ])
    nodes = _paginate(client, "q", "org", "repo", "pullRequests", since=since)
    assert [n["createdAt"] for n in nodes] == [
        "2026-06-01T00:00:00Z", "2020-01-01T00:00:00Z"]


def test_unparsable_date_does_not_stop_pagination():
    """日期解析不了时继续翻，宁可多花请求也不漏数据。"""
    from contributors.api_stats import _paginate

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = _FakeClient([
        _page(["", "not-a-date"]),
        _page(["2026-06-01T00:00:00Z"], has_next=False),
    ])
    _paginate(client, "q", "org", "repo", "pullRequests", since=since)
    assert client.calls == 2
