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
