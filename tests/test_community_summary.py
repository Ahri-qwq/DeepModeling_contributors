import json
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests
import responses

from contributors.events import Event, StateChange
from contributors.main import _record_and_notify
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
        _pr("blog", 377, "What Can ABACUS+DP Do Too?"),
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
        if source["id"] == "blog#377":
            result["routine_blog_post"] = True
        counts = {375: (3, 21), 378: (21, 74)}.get(source["number"])
        if counts:
            result["resource_counts"] = {"before": counts[0], "after_this_change": counts[1], "net_change": counts[1] - counts[0]}
        return result

    def model(key, name, prompt, data, *, thinking, deadline, validate):
        evidence = {record["id"]: record for record in data["records"]}
        assert "private#1" not in evidence
        assert "blog#377" not in evidence
        if not thinking:
            assert evidence["blog#378"]["resource_counts"]["net_change"] == 53
            return validate({"candidates": [{"refs": ["deepmd-kit#6129"]}]})
        assert evidence["blog#375"]["resource_counts"]["before"] == 3
        assert evidence["blog#378"]["resource_counts"]["after_this_change"] == 74
        return validate({"items": [{"title": "官网拟补录 53 篇论文", "text": "上一批已合并，本次提案预计把 21 条扩至 74 条。",
                                    "refs": ["blog#378", "blog#375"], "stage_source": "blog#378", "person_sources": ["blog#378"]}]})

    monkeypatch.setattr(community, "_enrich", enrich)
    monkeypatch.setattr(community, "_model_json", model)
    stories = community._generate(current, history, [], "deepmodeling", datetime.fromisoformat(START), datetime.fromisoformat(END), "key", "model")
    assert stories[0]["topic"] == "papers/deepmd-kit/2025"
    assert stories[0]["people"] == [{"login": "Ahri-qwq", "role": "提交改动"}]
    d = digest.build(UpsertResult([_pr("blog", 378, "title")], []))
    output = card.render_community(d, stories, "2026-11-10")["card"]
    assert output["header"]["title"]["content"].endswith("2026-11-10")
    body = output["elements"][0]["text"]["content"]
    assert body.startswith("**官网 Papers｜官网拟补录 53 篇论文**")
    assert "有修改提案" in body and "github.com/Ahri-qwq" in body


def test_papers_parser_counts_unique_entries_in_year():
    text = "## 2025\n{% publications %}\nA, B, A\n{% endpublications %}\n## 2024\n{% publications %}\nC\n{% endpublications %}"
    assert community._publication_count(text, "2025") == 2


def test_community_card_shows_both_links_below_activity(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://example.com/dashboard")
    monkeypatch.setenv("FEISHU_BITABLE_VIEW_URL", "https://example.com/contributors")
    d = digest.build(UpsertResult([_pr("deepmd-kit", 1, "改动")], []))
    output = card.render_community(d, [], "2026-11-10", status="activity")
    lines = output["card"]["elements"][0]["text"]["content"].splitlines()
    assert "1 项新提案" in lines[-3]
    assert lines[-2:] == [
        "[📊 详细条目和看板](https://example.com/dashboard)",
        "[📋 近一年明细表格](https://example.com/contributors)",
    ]


def test_proposal_cannot_be_rendered_as_merged():
    records = community._event_records(UpsertResult([_pr("blog", 378, "papers(deepmd-kit): 补入 2025 年 53 篇")], []), True)
    with pytest.raises(ValueError, match="Merged claim"):
        community._items({"items": [{"title": "论文已合并", "text": "提案扩大了收录。", "refs": ["blog#378"], "stage_source": "blog#378"}]}, records)


@pytest.mark.parametrize("claim", ["现已发布", "已公开", "正式发布", "上线成功", "官网可见", "已在官网上线"])
def test_release_claims_require_release_evidence(claim):
    records = community._event_records(UpsertResult([_pr("blog", 378, "论文")], []), True)
    output = {"items": [{"title": claim, "text": "这是论文补录。", "refs": ["blog#378"], "stage_source": "blog#378"}]}
    with pytest.raises(ValueError, match="Published claim"):
        community._items(output, records)


@pytest.mark.parametrize("claim", ["合并完成", "已被合并", "已进入主干", "已经合入", "已进入开发主线"])
def test_proposal_cannot_claim_merging_using_synonyms(claim):
    records = community._event_records(UpsertResult([_pr("blog", 378, "论文")], []), True)
    output = {"items": [{"title": "论文补录", "text": "本批" + claim + "。", "refs": ["blog#378"], "stage_source": "blog#378"}]}
    with pytest.raises(ValueError, match="Merged claim"):
        community._items(output, records)


def test_proposal_can_mention_hypothesis_and_verified_historical_merge():
    records = community._event_records(UpsertResult([_pr("blog", 378, "论文")], []), True)
    records["blog#375"] = {**records["blog#378"], "current": False, "stage": "已合并"}
    output = {"items": [{"title": "拟继续补录论文", "text": "上一批已合并，本批若合并则增至 74 条，目前尚未正式发布。",
                         "refs": ["blog#378", "blog#375"], "stage_source": "blog#378"}]}
    assert community._items(output, records)[0]["stage"] == "有修改提案"


@pytest.mark.parametrize("text", [
    r"\[DeePMD-kit 官方下载\]\(https\://evil.example/malware\)",
    "请访问 evil.example.", "[下载](//evil.example)", "ｈｔｔｐｓ：／／ｅｖｉｌ．ｅｘａｍｐｌｅ",
    "https\u200b://evil.example", "&lt;a href=&quot;https://evil.example&quot;&gt;", "例子.测试",
])
def test_model_link_is_rejected_before_rendering(text):
    records = community._event_records(UpsertResult([_pr("blog", 378, "论文")], []), True)
    output = {"items": [{"title": "更新", "text": text, "refs": ["blog#378"], "stage_source": "blog#378"}]}
    with pytest.raises(ValueError, match="contains a link"):
        community._items(output, records)
    d = digest.build(UpsertResult([], []))
    assert card.render_community(d, [{"title": "更新", "text": text}], "2026-11-10") is None


def test_old_card_keeps_canonical_source_without_pr_title_injection():
    title = r"\[官方安装包\]\(https\://evil.example/download\)"
    d = digest.build(UpsertResult([_pr("blog", 378, title)], []))
    text = card.render_text(d)
    assert "evil.example" not in text
    assert "[blog #378](https://github.com/deepmodeling/blog/pull/378)" in text
    assert "查看原始标题" in text
    assert card._escape(r"\[普通文本]") == r"\\\[普通文本\]"


def test_papers_appendix_preserves_other_story_order():
    records = community._event_records(UpsertResult([
        _pr("blog", 378, "papers(deepmd-kit): 补入 2025 年 53 篇"),
        _pr("deepmd-kit", 6129, "训练更新"),
        _pr("Uni-Lab-OS", 359, "兼容性更新"),
    ], []), True)
    refs = ["blog#378", "deepmd-kit#6129", "Uni-Lab-OS#359"]
    items = [{"title": "改动提案", "text": "新增改动。", "refs": [ref], "stage_source": ref} for ref in refs]
    stories = community._items({"items": items}, records)
    assert [story["sources"][0]["label"] for story in stories] == [*refs[1:], refs[0]]


@pytest.mark.parametrize("count", [0, 2, 4, 5, 7])
def test_daily_story_count_can_follow_activity(count):
    records = community._event_records(UpsertResult([_pr("deepmd-kit", n, "工程更新") for n in range(1, count + 1)], []), True)
    items = [{"title": "改动提案", "text": "有新的改动。", "refs": [ref], "stage_source": ref} for ref in records]
    assert len(community._items({"items": items}, records)) == count


def test_daily_hard_limit_rejects_eight_stories():
    with pytest.raises(ValueError, match="Invalid story count"):
        community._items({"items": [{}] * 8}, {})


def test_recurring_blog_requires_complete_dated_post_files(monkeypatch):
    monkeypatch.setattr(community, "_get_json", lambda *args: [{"filename": "source/_posts/Other_2026_11_10.md"}])
    title = {"title": "What Can ABACUS+DP Do Too?"}
    assert community._routine_blog_post({"changed_files": 1}, title, "/pulls/377", None)
    assert not community._routine_blog_post({"changed_files": 2}, title, "/pulls/377", None)
    assert not community._routine_blog_post({"changed_files": 1, "labels": [{"name": "announcement"}]}, title, "/pulls/377", None)
    monkeypatch.setattr(community, "_get_json", lambda *args: [{"filename": "source/papers/deepmd-kit/index.md"}])
    assert not community._routine_blog_post({"changed_files": 1}, title, "/pulls/378", None)


def test_shortlist_keeps_each_current_candidate_before_background():
    refs = ["a#1", "b#2", "c#3", "d#4", "e#5", "f#6"]
    records = {ref: {"current": True} for ref in refs}
    records["a#0"] = {"current": False}
    output = {"candidates": [{"refs": [refs[0], "a#0"]}, *[{"refs": [ref]} for ref in refs[1:]]]}
    assert community._selected_refs(output, records) == [*refs, "a#0"]


def test_shortlist_skips_stale_candidates_without_losing_current_ones():
    records = {"old": {"current": False}, "today": {"current": True}}
    output = {"candidates": [{"refs": ["old"]}, {"refs": ["today"]}]}
    assert community._selected_refs(output, records) == ["today"]


def _reply(value):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


@pytest.fixture
def retry_clock(monkeypatch):
    now = [0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(community.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(community.time, "sleep", sleep)
    return sleeps


def _request_model(validate=lambda value: value, deadline=90):
    return community._model_json("test", "model", "prompt", {}, thinking=False, deadline=deadline, validate=validate)


@responses.activate
def test_transient_errors_retry_twice_then_return_to_original_card(retry_clock):
    responses.add(responses.POST, community.DEEPSEEK_API, body=requests.Timeout())
    responses.add(responses.POST, community.DEEPSEEK_API, status=503)
    responses.add(responses.POST, community.DEEPSEEK_API, status=500)
    with pytest.raises(requests.HTTPError):
        _request_model()
    assert retry_clock == [5, 15]
    assert len(responses.calls) == 3


@responses.activate
def test_rate_limit_retries_after_server_delay_when_budget_allows(retry_clock):
    responses.add(responses.POST, community.DEEPSEEK_API, status=429, headers={"Retry-After": "8"})
    responses.add(responses.POST, community.DEEPSEEK_API, json=_reply({"items": []}))
    assert _request_model() == {"items": []}
    assert retry_clock == [8]


@responses.activate
def test_rate_limit_does_not_wait_past_deadline(retry_clock):
    responses.add(responses.POST, community.DEEPSEEK_API, status=429, headers={"Retry-After": "120"})
    with pytest.raises(TimeoutError, match="retry budget"):
        _request_model()
    assert retry_clock == []
    assert len(responses.calls) == 1


@responses.activate
def test_auth_failure_is_not_retried(retry_clock):
    responses.add(responses.POST, community.DEEPSEEK_API, status=401)
    with pytest.raises(requests.HTTPError):
        _request_model()
    assert retry_clock == []
    assert len(responses.calls) == 1


@responses.activate
def test_invalid_evidence_gets_one_immediate_rewrite(retry_clock):
    records = {"blog#378": {"current": True}}
    responses.add(responses.POST, community.DEEPSEEK_API, json=_reply({"candidates": [{"refs": ["invented"]}]}))
    responses.add(responses.POST, community.DEEPSEEK_API, json=_reply({"candidates": [{"refs": ["blog#378"]}]}))
    assert _request_model(lambda value: community._selected_refs(value, records)) == ["blog#378"]
    assert retry_clock == []
    second_request = json.loads(responses.calls[1].request.body)
    assert "未通过校验" in second_request["messages"][-1]["content"]


@responses.activate
def test_repeated_invalid_output_falls_back_without_a_third_rewrite(retry_clock):
    responses.add(responses.POST, community.DEEPSEEK_API, json=_reply({"wrong": []}))
    responses.add(responses.POST, community.DEEPSEEK_API, json=_reply({"wrong": []}))
    with pytest.raises(KeyError):
        _request_model(lambda value: value["items"])
    assert len(responses.calls) == 2
    assert retry_clock == []


@pytest.mark.parametrize("empty_phase", ["selection", "writing"])
def test_empty_model_result_retains_pr_details_and_is_not_cached(empty_phase, tmp_path, monkeypatch):
    pending = UpsertResult([_pr("blog", 378, "改进教程入口"), _pr("blog", 379, "修复下载步骤")], [])
    monkeypatch.setenv("COMMUNITY_SUMMARY_ENABLED", "1")
    monkeypatch.setenv("COMMUNITY_DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(community, "get_token", lambda: None)
    monkeypatch.setattr(community, "_public_repos", lambda *args: {"blog"})
    monkeypatch.setattr(community, "_enrich", lambda row, *args: row)

    def model(*args, thinking, validate, **kwargs):
        if thinking:
            return validate({"items": []})
        return validate({"candidates": [] if empty_phase == "selection" else [{"refs": ["blog#378"]}]})

    monkeypatch.setattr(community, "_model_json", model)
    with closing(EventStore(tmp_path / "events.db")) as store:
        store.init_schema()
        assert community.daily_summary(store, pending, "deepmodeling", START, END) == (None, None)
        assert store.conn.execute("SELECT COUNT(*) FROM community_summaries").fetchone()[0] == 0
    body = card.render_text(digest.build(pending))
    assert "改进教程入口" in body and "修复下载步骤" in body


def test_verified_routine_updates_use_quiet_card_without_a_model(tmp_path, monkeypatch):
    pending = UpsertResult([_pr("blog", 377, "例行文章")], [])
    monkeypatch.setenv("COMMUNITY_SUMMARY_ENABLED", "1")
    monkeypatch.setenv("COMMUNITY_DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(community, "get_token", lambda: None)
    monkeypatch.setattr(community, "_public_repos", lambda *args: {"blog"})
    monkeypatch.setattr(community, "_enrich", lambda row, *args: {**row, "routine_blog_post": True})
    monkeypatch.setattr(community, "_model_json", lambda *a, **k: pytest.fail("quiet card does not call model"))
    with closing(EventStore(tmp_path / "events.db")) as store:
        store.init_schema()
        assert community.daily_summary(store, pending, "deepmodeling", START, END) == ([], None)
    payload = card.render_community(digest.build(pending), [], "2026-11-10")
    assert "本期只有例行更新" in payload["card"]["elements"][0]["text"]["content"]


def test_no_data_and_partial_coverage_get_distinct_wording():
    pending = UpsertResult([], [])
    full = digest.build(pending, recent_label="本周至今", recent_counts={"commits": 12})
    partial = digest.build(pending, failed_repos=["deepmd-kit"])
    full_text = card.render_community(full, [], "2026-11-10")["card"]["elements"][0]["text"]["content"]
    partial_text = card.render_community(partial, [], "2026-11-10")["card"]["elements"][0]["text"]["content"]
    assert "本期未采集到新增社区活动" in full_text and "本周至今：12 次提交" in full_text
    assert "部分仓库数据待补" in partial_text and "deepmd-kit" in partial_text
    assert "本期未采集到新增社区活动" not in partial_text


def test_push_pipeline_uses_short_card_when_summary_has_no_story(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "events.db"
    with closing(EventStore(db_path)) as store:
        store.init_schema()
        run_id = store.begin_run()
        store.upsert_events([_pr("blog", n, title) for n, title in [
            (377, "窗口前"), (378, "改进教程入口"), (379, "修复下载步骤"), (380, "窗口后"),
        ]], run_id)
        store.conn.executemany("UPDATE events SET first_seen_at=? WHERE number=?", [
            ("2026-11-11T21:59:59+00:00", 377),  # 11/12 05:59:59
            ("2026-11-11T22:00:00+00:00", 378),  # 11/12 06:00
            ("2026-11-12T18:30:00+00:00", 379),  # 11/13 02:30
            ("2026-11-12T22:00:00+00:00", 380),  # 11/13 06:00
        ])
        store.conn.commit()
        store.finish_run(run_id, repos_processed=1)
    monkeypatch.setenv("COMMUNITY_SUMMARY_ENABLED", "1")
    monkeypatch.setenv("COMMUNITY_DEEPSEEK_API_KEY", "test")
    def generate(current, *args):
        assert {r["number"] for r in current.values()} == {378, 379}
        return None

    monkeypatch.setattr(community, "_generate", generate)
    cfg = SimpleNamespace(events_db=db_path, date="2026-11-13", org="deepmodeling",
                          notify=False, notify_dry_run=True, notify_empty=False)
    _record_and_notify([], {"window": {"since": "", "until": ""}}, cfg, 0, [])
    output = capsys.readouterr().out
    payload = json.loads(output[output.index('{\n  "msg_type"'):])
    body = payload["card"]["elements"][0]["text"]["content"]
    assert payload["card"]["header"]["title"]["content"] == "DeepModeling 社区日报 2026-11-13"
    assert "2 项新提案" in body
    assert "改进教程入口" not in body and "修复下载步骤" not in body


def test_push_pipeline_reports_missing_coverage_without_recent_activity(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "events.db"
    with closing(EventStore(db_path)) as store:
        store.init_schema()
        run_id = store.begin_run()
        store.finish_run(run_id, repos_processed=1)
    monkeypatch.setenv("COMMUNITY_SUMMARY_ENABLED", "1")
    monkeypatch.setenv("COMMUNITY_DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(EventStore, "repos_missing_since", lambda *args: {"deepmd-kit"})
    cfg = SimpleNamespace(events_db=db_path, date="2026-11-11", org="deepmodeling",
                          notify=False, notify_dry_run=True, notify_empty=False)
    _record_and_notify([], {"window": {"since": "", "until": ""}}, cfg, 0, [])
    output = capsys.readouterr().out
    payload = json.loads(output[output.index('{\n  "msg_type"'):])
    body = payload["card"]["elements"][0]["text"]["content"]
    assert "部分仓库数据待补" in body and "deepmd-kit" in body


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
        assert community.daily_summary(store, pending, "deepmodeling", START, END) == (community.UNAVAILABLE, None)
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
