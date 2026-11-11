from datetime import datetime

import pytest

from contributors.events import Event, StateChange
from contributors.notify import card, community, digest
from contributors.store import EventStore, UpsertResult


START = "2026-11-09T16:00:00+00:00"
END = "2026-11-10T16:00:00+00:00"


def _pr(repo, number, title, author="Ahri-qwq"):
    return Event("pr", repo, number, None, title,
                 f"https://github.com/deepmodeling/{repo}/pull/{number}", author,
                 "2026-11-10T01:00:00Z", "open")


def test_public_resource_reaches_editor_when_shortlist_misses_it(monkeypatch):
    current = community._event_records(UpsertResult([
        _pr("blog", 378, "papers(deepmd-kit): 补入 2025 年 53 篇论文"),
        _pr("deepmd-kit", 6129, "perf(train): batch EMA updates", "OutisLi"),
        _pr("private", 1, "confidential"),
    ], []), True)
    history = community._event_records(UpsertResult([], [StateChange(
        "pr:blog:375", "pr", "blog", 375, "papers(deepmd-kit): 补入 2025 年 18 篇论文",
        "https://github.com/deepmodeling/blog/pull/375", "open", "merged", "Ahri-qwq",
    )]), False)
    monkeypatch.setattr(community, "get_token", lambda: None)
    monkeypatch.setattr(community, "_public_repos", lambda *args: {"blog", "deepmd-kit"})

    def enrich(source, *args):
        result = {**source, "description": "public details"}
        counts = {375: (3, 21), 378: (21, 74)}.get(source["number"])
        if counts:
            result["resource_counts"] = {"before": counts[0], "after_this_change": counts[1], "net_change": counts[1] - counts[0]}
        return result

    def model(key, name, prompt, data, thinking):
        evidence = {record["id"]: record for record in data["records"]}
        assert "private#1" not in evidence
        if not thinking:
            assert evidence["blog#378"]["resource_counts"]["net_change"] == 53
            return {"candidates": [{"refs": ["deepmd-kit#6129"]}]}
        assert evidence["blog#375"]["resource_counts"]["before"] == 3
        assert evidence["blog#378"]["resource_counts"]["after_this_change"] == 74
        return {"items": [{"title": "官网拟补录 53 篇论文", "text": "上一批已合并，本次提案预计把 21 条扩至 74 条。",
                           "refs": ["blog#378", "blog#375"], "stage_source": "blog#378", "person_sources": ["blog#378"]}]}

    monkeypatch.setattr(community, "_enrich", enrich)
    monkeypatch.setattr(community, "_model_json", model)
    stories = community._generate(current, history, [], "deepmodeling", datetime.fromisoformat(START), datetime.fromisoformat(END), "key", "model")
    assert stories[0]["topic"] == "papers/deepmd-kit/2025"
    assert stories[0]["people"] == [{"login": "Ahri-qwq", "role": "提交改动"}]
    d = digest.build(UpsertResult([_pr("blog", 378, "title")], []))
    output = card.render_community(d, stories, "2026-11-10")["card"]
    assert output["header"]["title"]["content"].endswith("2026-11-10")
    body = output["elements"][0]["text"]["content"]
    assert body.startswith("**blog｜官网拟补录 53 篇论文**")
    assert "有修改提案" in body and "github.com/Ahri-qwq" in body


def test_papers_parser_counts_unique_entries_in_year():
    text = "## 2025\n{% publications %}\nA, B, A\n{% endpublications %}\n## 2024\n{% publications %}\nC\n{% endpublications %}"
    assert community._publication_count(text, "2025") == 2


def test_proposal_cannot_be_rendered_as_merged():
    records = community._event_records(UpsertResult([_pr("blog", 378, "papers(deepmd-kit): 补入 2025 年 53 篇")], []), True)
    with pytest.raises(ValueError, match="Merged claim"):
        community._items({"items": [{"title": "论文已合并", "text": "提案扩大了收录。", "refs": ["blog#378"], "stage_source": "blog#378"}]}, records)


def test_summary_falls_back_and_reuses_success(tmp_path, monkeypatch):
    pending = UpsertResult([_pr("blog", 378, "论文")], [])
    store = EventStore(tmp_path / "events.db")
    store.init_schema()
    try:
        monkeypatch.delenv("COMMUNITY_SUMMARY_ENABLED", raising=False)
        assert community.daily_summary(store, pending, "deepmodeling", START, END) == (None, None)
        monkeypatch.setenv("COMMUNITY_SUMMARY_ENABLED", "1")
        monkeypatch.setenv("COMMUNITY_DEEPSEEK_API_KEY", "test")

        def failure(*args):
            raise ValueError("bad response")

        monkeypatch.setattr(community, "_generate", failure)
        assert community.daily_summary(store, pending, "deepmodeling", START, END) == (None, None)
        story = {"topic": "papers/deepmd-kit/2025", "title": "更新论文", "stage": "已合并"}
        monkeypatch.setattr(community, "_generate", lambda *args: [story])
        stories, key = community.daily_summary(store, pending, "deepmodeling", START, END)
        assert stories == [story]
        monkeypatch.setattr(community, "_generate", failure)
        assert community.daily_summary(store, pending, "deepmodeling", START, END) == ([story], key)
        run = store.begin_run()
        digest_id = store.save_digest(run, {})
        store.link_community_summary(START, key, digest_id)
        assert not store.recent_community_topics(START, END)
        store.mark_digest_sent(digest_id, True)
        assert store.recent_community_topics(START, END)[0]["topic"] == story["topic"]
    finally:
        store.close()
