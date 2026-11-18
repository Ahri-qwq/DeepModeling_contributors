import json
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests
import responses

from conftest import daily_card_text

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


@pytest.mark.parametrize("login, role, account_path", [
    ("njzjz-bot", "自动账号提交改动", "njzjz-bot"),
    ("dependabot[bot]", "自动账号提交改动", "apps/dependabot"),
    ("copilot-swe-agent[bot]", "自动账号提交改动", "apps/copilot-swe-agent"),
    ("botelho", "提交改动", "botelho"),
])
def test_story_retains_actual_human_or_automatic_author(login, role, account_path):
    pending = UpsertResult([_pr("deepmd-kit", 5834, "fix: exclude virtual atoms", login)], [])
    records = community._safe_sources(community._event_records(pending, True), "deepmodeling", {"deepmd-kit"})
    output = {"items": [{"title": "修正邻居统计", "text": "提案修正虚拟原子的统计。",
                         "refs": ["deepmd-kit#5834"], "stage_source": "deepmd-kit#5834"}]}
    stories = community._items(output, records)
    assert stories[0]["people"] == [{"login": login, "role": role}]
    body = daily_card_text(card.render_community(digest.build(pending), stories, "2026-11-18"))
    assert f"https://github.com/{account_path}" in body and role in body
    assert f"[@{card._escape(login)}]" in body
    from contributors.notify import period
    period_digest = period.build_period(pending.new_events, "上周", "范围", people_only=True)
    assert period_digest.contributor_total == (1 if login == "botelho" else 0)


def test_invalid_author_cannot_introduce_a_link():
    records = community._event_records(UpsertResult([_pr("deepmd-kit", 5834, "Fix", "x](https://evil.example)")], []), True)
    safe = community._safe_sources(records, "deepmodeling", {"deepmd-kit"})
    assert safe["deepmd-kit#5834"]["author"] == ""


def test_verified_short_story_keeps_automatic_author():
    ref = "example@v1"
    records = {ref: {"id": ref, "current": True, "kind": "release", "title": "v1.0", "repo": "example",
                     "stage": "已正式发布", "url": "https://github.com/deepmodeling/example/releases/tag/v1.0",
                     "author": "github-actions[bot]"}}
    story, = community.verified_short_stories(records)
    assert story["people"] == [{"login": "github-actions[bot]", "role": "自动账号发布版本"}]


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
    payload = card.render_community(d, stories, "2026-11-12")
    assert payload["card"]["header"]["title"]["content"] == "DeepModeling 社区日报 · 2026-11-12（周四）"
    body = daily_card_text(payload)
    assert body.startswith("**官网 Papers｜官网拟补录 53 篇论文**")
    assert "有修改提案：[@Ahri-qwq](https://github.com/Ahri-qwq)" in body
    assert "来源：[blog#378](https://github.com/deepmodeling/blog/pull/378)" in body


@pytest.mark.parametrize("model_unavailable", [False, True])
def test_papers_share_one_section_and_primary_authors_do_not_depend_on_model(monkeypatch, model_unavailable):
    pending = UpsertResult([
        _pr("blog", 380, "papers(dpgen): 补入 25 篇论文（2025Q1, 2025Q2toQ4）"),
        _pr("blog", 381, "papers(deepmd-kit): 补入 2026 年 76 篇论文"),
        _pr("blog", 382, "papers(ABACUS): 补入 2025-2026 年 33 篇论文"),
        _pr("blog", 383, "papers(uni-mol): 补入 2025-2026 年 26 篇论文"),
        _pr("blog", 384, "发布教程", "writer"),
    ], [StateChange("pr:blog:379", "pr", "blog", 379, "papers(ReacNetGenerator): 补入 2024-2026 年 8 篇论文",
                     "https://github.com/deepmodeling/blog/pull/379", "open", "merged", "Ahri-qwq")])
    records = community._event_records(pending, True)
    monkeypatch.setattr(community, "get_token", lambda: None)
    monkeypatch.setattr(community, "_public_repos", lambda *args: {"blog"})

    def enrich(row, *args):
        if row["id"] == "blog#383":
            return {**row, "resource_counts": {"before": 3, "after_this_change": 18, "net_change": 15}}
        return row

    def model(*args, thinking, deadline, validate):
        assert args[3]["max_items"] == 5
        assert args[3]["current_source_ids"] == ["blog#384"]
        if model_unavailable:
            raise TimeoutError("offline")
        if not thinking:
            return validate({"candidates": [{"refs": ["blog#384"]}]})
        return validate({"items": [{"title": "新增教程", "text": "提案增加了新的教程。",
                                    "refs": ["blog#384"], "stage_source": "blog#384", "person_sources": []}]})

    monkeypatch.setattr(community, "_enrich", enrich)
    monkeypatch.setattr(community, "_model_json", model)
    stories = community._generate(records, {}, [], "deepmodeling", datetime.fromisoformat(START),
                                  datetime.fromisoformat(END), "key", "model")
    payload = card.render_community(digest.build(pending), stories, "2026-11-13")
    body = daily_card_text(payload)
    assert body.count("**官网 Papers｜") == 1
    assert body.count("**社区博客｜") == (0 if model_unavailable else 1)
    assert body.count("@Ahri-qwq") == 1
    assert "本期推进 5 项论文目录补录，1 项已合并、4 项待合并。" in body
    assert "据 PR 说明，ReacNetGenerator 补录 8 篇。" in body
    assert "uni-mol 论文清单拟净增 15 篇。" in body
    assert "**ReacNetGenerator**" not in body
    assert "来源：[blog#379]" in body
    assert body.index("已合并与待合并：[@Ahri-qwq]") < body.index("来源：[blog#379]")
    assert "参与：" not in body
    for number in range(379, 384):
        assert f"https://github.com/deepmodeling/blog/pull/{number}" in body
    if not model_unavailable:
        assert "有修改提案：[@writer](https://github.com/writer)" in body


def test_daily_groups_issue_and_related_prs_but_keeps_independent_work(monkeypatch):
    repo = "example"
    issue = Event("issue", repo, 1, None, "Missing export output",
                  "https://github.com/deepmodeling/example/issues/1", "reporter", "2026-11-10T01:00:00Z", "open")
    pending = UpsertResult([
        issue, _pr(repo, 2, "Fix export output", "author"), _pr(repo, 3, "Add export checks", "second"),
        _pr(repo, 4, "Update documentation", "other"), _pr(repo, 5, "Add export test", "third"),
        _pr(repo, 6, "Update export examples", "fourth"), _pr(repo, 7, "Check old exports", "fifth"),
    ], [StateChange("pr:example:2", "pr", repo, 2, "Fix export output",
                    "https://github.com/deepmodeling/example/pull/2", "open", "merged", "author")])
    current = community._event_records(pending, True)
    monkeypatch.setattr(community, "get_token", lambda: None)
    monkeypatch.setattr(community, "_public_repos", lambda *args: {repo})
    bodies = {1: "Missing export output", 2: "Fixes: #1", 3: "Resolves deepmodeling/example#1",
              4: "Fixes other/example#1", 5: "Closes #1", 6: "Fixes #1", 7: "Closes #1"}
    monkeypatch.setattr(community, "_get_json", lambda path, token: {"body": bodies[int(path.rsplit('/', 1)[-1])]})

    def model(*args, thinking, deadline, validate):
        if not thinking:
            return validate({"candidates": [{"refs": [f"example#{n}"]} for n in (3, 2, 4)]})
        assert set(args[3]["linked_issue_groups"]["example#1"]) == {f"example#{n}" for n in (3, 2, 5, 6, 7)}
        assert "example#1" in args[3]["current_source_ids"]
        return validate({"items": [{"title": current[ref]["title"], "text": "涉及导出流程。",
                                    "refs": [ref], "stage_source": ref}
                                   for ref in ("example#1", "example#3", "example#2", "example#4")]})

    monkeypatch.setattr(community, "_model_json", model)
    stories = community._generate(current, {}, [], "deepmodeling", datetime.fromisoformat(START),
                                  datetime.fromisoformat(END), "key", "model")
    assert [story["topic"] for story in stories] == ["example#1", "example#4"]
    story = stories[0]
    assert story["stage"] == "已合并与待合并"
    assert [person["login"] for person in story["people"]][:3] == ["author", "reporter", "second"]
    labels = [source["label"] for source in story["sources"]]
    assert labels[:3] == ["example#2", "example#3", "example#1"]
    assert set(labels[3:5]) == {"example#5", "example#6"}
    body = daily_card_text(card.render_community(digest.build(pending), stories, "2026-11-13"))
    assert "已合并与待合并：[@author]" in body
    assert "等 6 人" in body and "等 6 条" in body
    assert "example#6" in body and "example#7" not in body


@pytest.mark.parametrize("body", ["Fixes #1 and fixes #2", "`Fixes #1`", "<!-- Fixes #1 -->", "> Fixes #1"])
def test_ambiguous_or_example_issue_references_do_not_group(body, monkeypatch):
    monkeypatch.setattr(community, "_get_json", lambda *args: {"body": body})
    current = community._event_records(UpsertResult([_pr("example", 7, "Export change")], []), True)
    evidence = community._enrich(current["example#7"], "deepmodeling", datetime.fromisoformat(END), None)
    assert "linked_issue" not in evidence


def test_papers_parser_counts_unique_entries_in_year():
    text = "## 2025\n{% publications %}\nA, B, A\n{% endpublications %}\n## 2024\n{% publications %}\nC\n{% endpublications %}"
    assert community._publication_count(text, "2025") == 2


def test_community_card_shows_both_links_below_activity(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://example.com/dashboard")
    monkeypatch.setenv("FEISHU_BITABLE_VIEW_URL", "https://example.com/contributors")
    d = digest.build(UpsertResult([_pr("deepmd-kit", 1, "改动")], []))
    output = card.render_community(d, [], "2026-11-10")
    elements = output["card"]["body"]["elements"]
    assert output["card"]["schema"] == "2.0"
    assert elements[-2]["tag"] == "hr"
    assert elements[-1]["background_style"] == "grey-50"
    stat, dashboard, bitable = elements[-1]["columns"][0]["elements"]
    assert stat["text"]["content"] == "1 个 PR 新建"
    assert stat["text"]["text_size"] == "notation"
    assert stat["text"]["text_color"] == "grey"
    assert dashboard["content"] == "[📊 详细条目和看板](https://example.com/dashboard)"
    assert bitable["content"] == "[📋 近一年明细表格](https://example.com/contributors)"


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


@pytest.mark.parametrize("count", [0, 2, 4, 5, 6])
def test_daily_story_count_can_follow_activity(count):
    records = community._event_records(UpsertResult([_pr("deepmd-kit", n, "工程更新") for n in range(1, count + 1)], []), True)
    items = [{"title": "改动提案", "text": "有新的改动。", "refs": [ref], "stage_source": ref} for ref in records]
    assert len(community._items({"items": items}, records)) == count


def test_daily_hard_limit_rejects_seven_stories():
    with pytest.raises(ValueError, match="Invalid story count"):
        community._items({"items": [{}] * 7}, {})


def test_daily_card_limits_visible_sections_and_preserves_papers_example():
    pending = UpsertResult([_pr("example", n, "工程更新", f"author{n}") for n in range(1, 7)], [])
    records = community._event_records(pending, True)
    items = [{"title": f"进展 {n}", "text": "有新的改动。", "refs": [ref, *[other for other in records if other != ref][:4]],
              "stage_source": ref} for n, ref in enumerate(records, start=1)]
    stories = community._validated_items({"items": items}, records, max_sources=5, max_people=None)
    papers = [{"paper_batch_member": True, "title": f"项目 {n}", "text": "", "stage": "有修改提案",
               "resource_counts": {"net_change": n}, "people": [{"login": f"paper{n}", "role": "提交改动"}],
               "sources": [{"label": f"blog#{n}", "url": f"https://github.com/deepmodeling/blog/pull/{n}"}]}
              for n in range(1, 7)]
    body = daily_card_text(card.render_community(digest.build(pending), [*stories, *papers], "2026-11-13"))
    assert body.count("**example｜") == 5
    assert body.count("**官网 Papers｜") == 1
    assert "项目 6 论文清单拟净增 6 篇" in body
    assert "来源：[blog#6]" in body and "[blog#5]" not in body
    assert "有修改提案：[@author1]" in body and "等 5 人" in body


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
    assert "本期只有例行更新" in daily_card_text(payload)


def test_no_data_and_partial_coverage_get_distinct_wording():
    pending = UpsertResult([], [])
    full = digest.build(pending, recent_label="本周至今", recent_counts={"commits": 12})
    partial = digest.build(pending, failed_repos=["deepmd-kit"])
    full_text = daily_card_text(card.render_community(full, [], "2026-11-10"))
    partial_text = daily_card_text(card.render_community(partial, [], "2026-11-10"))
    assert "本期未采集到新增社区活动" in full_text and "本周至今：12 次提交" in full_text
    assert "数据待补" in partial_text and "deepmd-kit" in partial_text
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
    body = daily_card_text(payload)
    assert payload["card"]["header"]["title"]["content"] == "DeepModeling 社区日报 · 2026-11-13（周五）"
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
    body = daily_card_text(payload)
    assert "数据待补" in body and "deepmd-kit" in body


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
