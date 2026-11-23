from contextlib import closing
from datetime import datetime, timezone

import pytest
import requests

from conftest import daily_card_text
from contributors.notify import card, community, daily_context
from contributors.notify.digest import Digest
from contributors.store import EventStore


def source(number, branch, *, current=False, stage="已合并", head="topic", body=""):
    return {"id": f"project#{number}", "repo": "project", "number": number, "kind": "pr",
            "stage": stage, "current": current, "author": "Developer", "title": "Change",
            "url": f"https://github.com/deepmodeling/project/pull/{number}",
            "base_repo": "deepmodeling/project", "base_branch": branch,
            "head_repo": "deepmodeling/project", "head_branch": head,
            "head_sha": f"head{number}", "merge_sha": f"merge{number}", "description": body}


def report(ref, stage="有修改提案"):
    return {"topic": ref, "title": "整合方案", "text": "此前的具体方案。", "stage": stage,
            "report_date": "2026-11-13", "sources": [{"label": ref}]}


def test_follow_up_and_later_squash_merge_have_distinct_context():
    parent = source(1, "dev", head="integration", stage="有修改提案")
    child = source(2, "integration", current=True)
    child["follows_prs"] = ["project#1"]
    records = {"project#1": parent, "project#2": child}
    daily_context.branch_context(records, {"project#1": report("project#1")}, lambda path: pytest.fail(path))
    assert child["report_context"][0]["kind"] == "follow_up"
    assert "先合入其整合分支" in child["report_context"][0]["note"]

    parent.update(current=True, stage="已合并")
    child["current"] = False
    requested = []

    def fetch(path):
        requested.append(path)
        if path.endswith("merge2...merge1"):
            return {"status": "diverged"}
        if path.endswith("merge2...head1"):
            return {"status": "ahead"}
        return {"tree": {"sha": "identical-tree"}}

    daily_context.branch_context(records, {"project#2": report("project#2", "已合并")}, fetch)
    assert parent["report_context"][0]["kind"] == "includes_reported"
    assert len(requested) == 4
    output = {"items": [{"title": "整合方案合入开发分支", "text": "包含库存修正和接口整合。",
                         "refs": ["project#1"], "stage_source": "project#1"}]}
    stories = daily_context.annotate(community._items(output, records), records)
    body = daily_card_text(card.render_community(Digest(commit_count=1), stories, "2026-11-18"))
    assert "已合入 dev：" in body and "包含11 月 13 日已报道的补充改动" in body
    assert "project#2" in body and stories[0]["primary_pr"]["base_branch"] == "dev"


@pytest.mark.parametrize("failure", ["different_tree", "unrelated_commits", "network", "future_merge", "different_fork"])
def test_unverified_containment_does_not_create_a_relationship(failure):
    parent = source(1, "dev", current=True, head="integration")
    child = source(2, "integration")
    if failure == "future_merge":
        child.pop("merge_sha")
    if failure == "different_fork":
        parent["head_repo"] = "someone/project"

    def fetch(path):
        if failure == "network":
            raise requests.Timeout()
        if "/compare/" in path:
            return {"status": "ahead" if path.endswith("...head1") and failure != "unrelated_commits" else "diverged"}
        return {"tree": {"sha": path}}

    daily_context.branch_context({"project#1": parent, "project#2": child}, {"project#2": report("project#2")}, fetch)
    assert "report_context" not in parent


def test_same_repo_and_branch_alone_do_not_mean_follow_up():
    parent = source(1, "main", head="dev")
    child = source(2, "dev", current=True)
    daily_context.branch_context({"project#1": parent, "project#2": child}, {"project#1": report("project#1")},
                                 lambda path: pytest.fail(path))
    assert "report_context" not in child


def test_branch_metadata_respects_cutoff_and_closed_pr_is_not_merged(monkeypatch):
    row = source(1, "dev", current=True, stage="有修改提案")
    row.pop("merge_sha")
    data = {"base": {"ref": "dev", "repo": {"full_name": "deepmodeling/project"}},
            "head": {"ref": "topic", "sha": "head1", "repo": {"full_name": "deepmodeling/project"}},
            "merged_at": "2026-11-18T08:00:00Z", "merge_commit_sha": "merge1", "body": "Stacked on #3"}
    monkeypatch.setattr(community, "_get_json", lambda *args: data)
    result = community._enrich(row, "deepmodeling", datetime(2026, 11, 18, 0, tzinfo=timezone.utc), None)
    assert result["base_branch"] == "dev" and result["stage"] == "有修改提案" and "merge_sha" not in result
    assert result["follows_prs"] == ["project#3"]
    data["merged_at"] = None
    assert "merged_at" not in daily_context.pr_facts(data, datetime.now(timezone.utc))


def test_sent_history_keeps_text_sources_and_date_with_legacy_rows(tmp_path):
    with closing(EventStore(tmp_path / "events.db")) as store:
        store.init_schema()
        run = store.begin_run()
        story = {"topic": "project#1", "title": "先前方案", "text": "具体前情。", "stage": "有修改提案",
                 "sources": [{"label": "project#1", "url": "https://github.com/deepmodeling/project/pull/1"}]}
        store.save_community_summary("2026-11-12T22:00:00+00:00", "key", [story])
        assert store.recent_community_topics("2026-11-01", "2026-11-19") == []
        digest = store.save_digest(run, {"card": {"header": {"title": {"content": "DeepModeling 社区日报 · 2026-11-13（周五）"}}}}, sent=True)
        store.link_community_summary("2026-11-12T22:00:00+00:00", "key", digest)
        saved, = store.recent_community_topics("2026-11-01", "2026-11-19")
        assert saved["text"] == story["text"] and saved["sources"] == story["sources"]
        assert saved["report_date"] == "2026-11-13" and saved["source_facts"] == {}


def test_drafting_has_no_total_deadline_after_github_relationship_checks(monkeypatch):
    clock = [0]
    monkeypatch.setattr(community.time, "monotonic", lambda: clock[0])
    row = source(1, "dev", current=True)
    monkeypatch.setattr(daily_context, "branch_context", lambda *args: clock.__setitem__(0, 100))

    def model(*args, thinking, deadline, validate):
        if not thinking:
            return validate({"candidates": [{"refs": [row["id"]]}]})
        assert deadline is None
        return validate({"items": [{"title": "修正导出流程", "text": "改动已合并。",
                                    "refs": [row["id"]], "stage_source": row["id"]}]})

    monkeypatch.setattr(community, "_model_json", model)
    records = {row["id"]: row}
    stories = community._generate_stories(records, {}, records, [], "deepmodeling", datetime(2026, 11, 17),
                                          datetime(2026, 11, 18), "key", "model", None, {"project"}, True, 6)
    assert stories[0]["primary_pr"]["base_branch"] == "dev"
