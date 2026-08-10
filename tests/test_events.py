"""事件提取测试。全部用固定样本，不联网。"""
from contributors.events import (
    MAX_TITLE, Event, clip_title, commit_event, extract_issue_events,
    extract_pr_events,
)


class TestClipTitle:
    def test_keeps_short_title_intact(self):
        assert clip_title("修复收敛问题") == "修复收敛问题"

    def test_takes_first_line_only(self):
        """commit message 的首行才是标题，正文与尾部元数据不要。"""
        msg = "修复收敛问题\n\n详细说明若干\n\nCo-authored-by: X <x@y.z>"
        assert clip_title(msg) == "修复收敛问题"

    def test_truncates_overlong_title(self):
        out = clip_title("字" * 500)
        assert len(out) == MAX_TITLE
        assert out.endswith("…")

    def test_empty_message_yields_empty_string(self):
        assert clip_title("") == ""
        assert clip_title("   \n  \n") == ""

    def test_strips_surrounding_whitespace(self):
        assert clip_title("  标题带空格  \n正文") == "标题带空格"


class TestEventKey:
    def test_commit_key_includes_repo(self):
        """同一 SHA 可能出现在多个仓库，fork 场景下那是两条事件。"""
        a = commit_event("repo-a", "deepmodeling", "ff01", "x", "t")
        b = commit_event("repo-b", "deepmodeling", "ff01", "x", "t")
        assert a.key != b.key

    def test_pr_and_issue_with_same_number_differ(self):
        pr = Event("pr", "r", 1, None, "t", "u", None, "w")
        issue = Event("issue", "r", 1, None, "t", "u", None, "w")
        assert pr.key != issue.key


class TestExtractPrEvents:
    def test_extracts_basic_fields(self):
        nodes = [{
            "number": 4321, "title": "支持导出", "merged": True,
            "url": "https://github.com/deepmodeling/deepmd-kit/pull/4321",
            "createdAt": "2026-08-09T10:00:00Z",
            "author": {"login": "alice"},
        }]
        (ev,) = extract_pr_events(nodes, "deepmd-kit", "deepmodeling")
        assert ev.kind == "pr"
        assert ev.number == 4321
        assert ev.title == "支持导出"
        assert ev.state == "merged"
        assert ev.author_login == "alice"

    def test_unmerged_pr_is_open(self):
        nodes = [{"number": 1, "title": "x", "merged": False,
                  "createdAt": "2026-08-09T10:00:00Z", "author": {"login": "a"}}]
        (ev,) = extract_pr_events(nodes, "r", "org")
        assert ev.state == "open"

    def test_deleted_account_author_is_tolerated(self):
        """已删号用户的 author 为 null，不能崩。"""
        nodes = [{"number": 1, "title": "x", "merged": False,
                  "createdAt": "2026-08-09T10:00:00Z", "author": None}]
        (ev,) = extract_pr_events(nodes, "r", "org")
        assert ev.author_login is None

    def test_missing_url_falls_back_to_constructed(self):
        nodes = [{"number": 42, "title": "x", "merged": False,
                  "createdAt": "2026-08-09T10:00:00Z", "author": {"login": "a"}}]
        (ev,) = extract_pr_events(nodes, "deepmd-kit", "deepmodeling")
        assert ev.url == "https://github.com/deepmodeling/deepmd-kit/pull/42"

    def test_node_without_number_is_skipped(self):
        assert extract_pr_events([{"title": "无编号"}], "r", "org") == []

    def test_empty_and_none_input(self):
        assert extract_pr_events([], "r", "org") == []
        assert extract_pr_events(None, "r", "org") == []

    def test_missing_title_becomes_empty(self):
        nodes = [{"number": 1, "merged": False,
                  "createdAt": "2026-08-09T10:00:00Z", "author": {"login": "a"}}]
        (ev,) = extract_pr_events(nodes, "r", "org")
        assert ev.title == ""


class TestExtractIssueEvents:
    def test_extracts_basic_fields(self):
        nodes = [{
            "number": 890, "title": "报个 bug",
            "url": "https://github.com/deepmodeling/abacus-develop/issues/890",
            "createdAt": "2026-08-09T10:00:00Z",
            "author": {"login": "bob"},
        }]
        (ev,) = extract_issue_events(nodes, "abacus-develop", "deepmodeling")
        assert ev.kind == "issue"
        assert ev.number == 890
        assert ev.state is None

    def test_missing_url_falls_back_to_issues_path(self):
        nodes = [{"number": 7, "title": "x",
                  "createdAt": "2026-08-09T10:00:00Z", "author": None}]
        (ev,) = extract_issue_events(nodes, "r", "org")
        assert ev.url.endswith("/r/issues/7")


class TestCommitEvent:
    def test_builds_commit_url(self):
        ev = commit_event("deepmd-kit", "deepmodeling", "abc123",
                          "修复问题", "2026-08-09T10:00:00Z")
        assert ev.url == "https://github.com/deepmodeling/deepmd-kit/commit/abc123"
        assert ev.sha == "abc123"
        assert ev.number is None
        assert ev.state is None

    def test_multiline_message_is_clipped(self):
        ev = commit_event("r", "org", "s", "标题\n\n正文", "t")
        assert ev.title == "标题"
