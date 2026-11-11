import json
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests

from contributors.events import Event, StateChange
from contributors.main import _notify_period
from contributors.notify import community, feishu, period_summary
from contributors.store import EventStore, UpsertResult


START = "2026-11-01T16:00:00+00:00"
END = "2026-11-08T16:00:00+00:00"


def pr(repo, number, created, author="Developer"):
    return Event("pr", repo, number, None, "Fix output data", f"https://github.com/deepmodeling/{repo}/pull/{number}",
                 author, created, "merged")


def test_period_uses_exact_merge_time_and_filters_private_records(monkeypatch):
    old = pr("project", 1, "2026-10-01T00:00:00Z")
    future = pr("project", 2, "2026-11-04T00:00:00Z")
    private = pr("private", 3, "2026-11-03T00:00:00Z")
    observed = UpsertResult([], [StateChange(old.key, "pr", old.repo, old.number, old.title, old.url,
                                             "open", "merged", old.author_login)])
    store = SimpleNamespace(pending_in_window=lambda *args: observed, events_between=lambda *args: [])
    requested = []

    def github(path, token):
        requested.append(path)
        merge = "2026-11-04T00:00:00Z" if path.endswith("/1") else "2026-11-09T00:00:00Z"
        return {"merged_at": merge, "body": "Fix output"}

    monkeypatch.setattr(community, "_get_json", github)
    current, _, _ = period_summary._records(store, [future, private], "deepmodeling", datetime.fromisoformat(START),
                                           datetime.fromisoformat(END), None, {"project"}, [])
    assert current["project#1"]["stage"] == "已合并"
    assert current["project#2"]["stage"] == "有修改提案"
    assert all("private" not in path for path in requested)
    stats = period_summary._stats_events([future, private], current, {"project"})
    assert [(row.number, row.state) for row in stats] == [(2, "open"), (1, "merged")]


def test_beta_tag_and_release_cutoff(monkeypatch):
    release = {"tag_name": "v3.11.0-beta12", "published_at": "2026-11-05T00:00:00Z", "prerelease": False,
               "author": {"login": "Developer"}}
    future = {**release, "tag_name": "v3.11.0", "published_at": "2026-11-09T00:00:00Z"}
    monkeypatch.setattr(community, "_get_json", lambda *args: [release, future])
    rows, complete = period_summary._release_rows("deepmodeling", "project", datetime.fromisoformat(START),
                                                   datetime.fromisoformat(END), None)
    assert complete and list(rows) == ["project@v3.11.0-beta12"]
    ref, row = next(iter(rows.items()))
    assert row["stage"] == "已发布测试版"
    item = {"title": "已经正式发布", "text": "这是正式版。", "refs": [ref], "stage_source": ref}
    with pytest.raises(ValueError, match="Stable claim"):
        community._items({"items": [item]}, rows)
    valid = {**item, "title": "新增测试版", "text": "新版本已发布供测试。"}
    [story] = community._validated_items({"items": [item, valid]}, rows)
    assert story["stage"] == "已发布测试版" and story["title"] == valid["title"]


def test_public_repo_scope_retries_a_transient_read_timeout(monkeypatch):
    results = iter([requests.ReadTimeout(), [{"name": "Project", "private": False}]])

    def get(*args):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(community, "_get_json", get)
    monkeypatch.setattr(community.time, "sleep", lambda _: None)
    assert community._public_repos("deepmodeling", None) == {"project"}


def test_period_push_preview_uses_anchor_and_two_links(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    cfg = SimpleNamespace(events_db=db, org="deepmodeling", monthly=False, date="2026-11-11",
                          notify_empty=False, notify_dry_run=True)
    monkeypatch.setenv("COMMUNITY_SUMMARY_ENABLED", "1")
    monkeypatch.setenv("DASHBOARD_URL", "https://example.com/dashboard")
    monkeypatch.setenv("FEISHU_BITABLE_VIEW_URL", "https://example.com/contributors")
    monkeypatch.setattr(feishu, "send", lambda *args: pytest.fail("preview sent a message"))

    def summarize(store, events, org, start, end, monthly):
        assert (start, end, monthly) == (START, END, False)
        return period_summary.PeriodSummary(events=[pr("project", 1, START),
                                                   pr("project", 2, START, "dependabot[bot]")], unavailable=True)

    monkeypatch.setattr(period_summary, "summarize", summarize)
    assert _notify_period(cfg) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index('{\n  "msg_type"'):])
    body = payload["card"]["elements"][0]["text"]["content"]
    assert "共 1 位贡献者参与" in body and "dependabot" not in body
    assert "摘要暂未生成" in body
    assert "https://example.com/dashboard" in body and "https://example.com/contributors" in body


def test_monthly_context_excludes_a_week_ending_after_the_month(tmp_path):
    with closing(EventStore(tmp_path / "events.db")) as store:
        store.init_schema()
        story = {"repo": "project", "topic": "topic", "title": "Changes", "stage": "已合并"}
        store.save_community_summary("period:weekly:2026-10-18T16:00:00+00:00", "a", [story])
        store.save_community_summary("period:weekly:2026-10-25T16:00:00+00:00", "b", [{**story, "title": "Future"}])
        result = store.period_summary_context("2026-09-01T16:00:00+00:00", "2026-09-30T16:00:00+00:00",
                                              "2026-10-31T16:00:00+00:00", True)
    assert [row["title"] for row in result] == ["Changes"]
