"""推送层测试：digest 聚合、card 渲染、feishu 签名与重试。全部离线。"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from contributors.events import Event, StateChange
from contributors.notify import card, feishu, period
from contributors.notify.digest import MAX_ITEMS, Digest, build, to_cst_text
from contributors.store import UpsertResult


def _pr(number, state="open", repo="deepmd-kit", title="示例"):
    return Event("pr", repo, number, None, title,
                 f"https://github.com/deepmodeling/{repo}/pull/{number}",
                 "alice", "2026-08-09T10:00:00+00:00", state)


def _commit(sha, repo="deepmd-kit"):
    return Event("commit", repo, None, sha, "修复",
                 f"https://github.com/deepmodeling/{repo}/commit/{sha}",
                 None, "2026-08-09T10:00:00+00:00", None)


def _issue(number, repo="abacus-develop"):
    return Event("issue", repo, number, None, "报个 bug",
                 f"https://github.com/deepmodeling/{repo}/issues/{number}",
                 "bob", "2026-08-09T10:00:00+00:00", None)


def _change(number, new_state="merged", repo="deepmd-kit", author="carol"):
    return StateChange(f"pr:{repo}:{number}", "pr", repo, number, "示例",
                       f"https://github.com/deepmodeling/{repo}/pull/{number}",
                       "open", new_state, author)


class TestAuthorDisplay:
    """条目显示提交者：拿到什么放什么，都没有就空着。"""

    def test_new_pr_line_shows_author(self):
        d = build(UpsertResult([_pr(1, "open")], []))
        assert "@alice" in card.render_text(d)

    def test_merged_pr_from_state_change_shows_author(self):
        """合并的 PR 走状态变更路径，作者同样要显示。

        这条最容易漏：StateChange 原本没 select author_login，
        只改 Item 会导致"合并的 PR"是唯一没作者的一栏。
        """
        d = build(UpsertResult([], [_change(7)]))
        assert "@carol" in card.render_text(d)

    def test_author_follows_title(self):
        """提交者放标题后面（用户明确要求）。

        新格式：仓库+编号占一行，标题+作者另起一行——作者仍在标题之后。
        """
        d = build(UpsertResult([_pr(1, "open", title="修复某问题")], []))
        lines = card.render_text(d).splitlines()
        ref_idx = next(i for i, l in enumerate(lines) if "#1" in l)
        # 标题和作者在编号行的下一行
        detail = lines[ref_idx + 1]
        assert "修复某问题" in detail
        assert detail.index("修复某问题") < detail.index("@alice")

    def test_missing_author_renders_nothing(self):
        """已删号用户 author 为 null，留空而不是写"未知"。"""
        ev = _pr(1, "open")
        ev.author_login = None
        text = card.render_text(build(UpsertResult([ev], [])))
        assert "@" not in text and "未知" not in text


class TestTimezone:
    def test_utc_is_converted_to_cst(self):
        """库里存 UTC，展示转东八区。"""
        assert to_cst_text("2026-08-09T02:00:00+00:00") == "08-09 10:00"

    def test_crossing_date_boundary(self):
        """UTC 8-09 20:00 是东八区 8-10 凌晨。"""
        assert to_cst_text("2026-08-09T20:00:00+00:00") == "08-10 04:00"

    def test_empty_and_malformed_input(self):
        assert to_cst_text("") == ""
        assert to_cst_text("不是时间") == ""


class TestBuild:
    def test_counts_commits_without_listing_them(self):
        """commit 一天几十条会刷屏，只给数字。"""
        pending = UpsertResult([_commit("a"), _commit("b")], [])
        d = build(pending)
        assert d.commit_count == 2

    def test_separates_merged_and_opened_prs(self):
        pending = UpsertResult([_pr(1, "open"), _pr(2, "merged")], [])
        d = build(pending)
        assert [i.number for i in d.opened_prs] == [1]
        assert [i.number for i in d.merged_prs] == [2]

    def test_state_change_to_merged_is_counted(self):
        """上周创建、今天合并的 PR 必须出现在今天的战报里。"""
        pending = UpsertResult([], [_change(7)])
        d = build(pending)
        assert [i.number for i in d.merged_prs] == [7]

    def test_merged_pr_is_not_double_counted(self):
        """同一 PR 既在新增又在变更里时只算一次。"""
        ev = _pr(9, "merged")
        chg = _change(9)
        pending = UpsertResult([ev], [chg])
        d = build(pending)
        assert len(d.merged_prs) == 1

    def test_non_merge_state_change_is_ignored(self):
        """关闭而非合并的 PR 不进战报。"""
        pending = UpsertResult([], [_change(3, new_state="closed")])
        d = build(pending)
        assert d.merged_prs == []

    def test_issues_are_collected(self):
        pending = UpsertResult([_issue(890)], [])
        d = build(pending)
        assert [i.number for i in d.issues] == [890]

    def test_empty_pending_yields_empty_digest(self):
        d = build(UpsertResult([], []))
        assert d.is_empty

    def test_any_content_makes_it_non_empty(self):
        assert not build(UpsertResult([_commit("a")], [])).is_empty
        assert not build(UpsertResult([], [_change(1)])).is_empty

    def test_totals_are_carried_through(self):
        d = build(UpsertResult([], []), total_contributors=366,
                  total_commits=2527)
        assert d.total_contributors == 366
        assert d.total_commits == 2527

    def test_generated_at_uses_cst_date(self):
        utc_late = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
        d = build(UpsertResult([], []), now=utc_late)
        assert d.generated_at == "2026-08-10"


class TestRenderText:
    def test_summary_line_lists_all_nonzero_kinds(self):
        d = build(UpsertResult(
            [_commit("a"), _pr(1, "open"), _pr(2, "merged"), _issue(9)], []))
        text = card.render_text(d)
        assert "1 次提交" in text
        assert "1 个 PR 合并" in text
        assert "1 个 PR 新建" in text
        assert "1 个 issue" in text

    def test_empty_digest_says_so(self):
        """无增量时明说，而不是只留一个标题。

        文案 2026-08-12 从"无新增"改为"昨日无更新"：日报不再因为空增量
        就跳过不发，而是明说无更新并贴一句近期活动量，见 TestQuietDayFallback。
        """
        assert "昨日无更新" in card.render_text(build(UpsertResult([], [])))

    def test_empty_section_is_not_rendered(self):
        d = build(UpsertResult([_commit("a")], []))
        text = card.render_text(d)
        assert "合并的 PR" not in text
        assert "新增 issue" not in text

    def test_items_are_truncated_with_remainder_note(self):
        prs = [_pr(n, "merged") for n in range(1, 26)]
        d = build(UpsertResult(prs, []))
        text = card.render_text(d)
        assert f"还有 {25 - MAX_ITEMS} 条" in text

    def test_max_items_is_five(self):
        """群里每类只列 5 条——10 条太长，刷屏且重点不突出。

        这是产品决策不是实现细节，所以单独锁一个值；改条数时连同
        这个测试一起改，避免有人顺手调常量而没人意识到卡片变长了。
        """
        assert MAX_ITEMS == 5

    def test_section_lists_at_most_max_items(self):
        prs = [_pr(n, "merged") for n in range(1, 26)]
        text = card.render_text(build(UpsertResult(prs, [])))
        # 列出来的条目行数就是 MAX_ITEMS，多的折叠进"还有 N 条"
        shown = [ln for ln in text.splitlines() if "deepmd-kit #" in ln]
        assert len(shown) == MAX_ITEMS

    def test_no_remainder_note_when_within_limit(self):
        prs = [_pr(n, "merged") for n in range(1, MAX_ITEMS + 1)]
        text = card.render_text(build(UpsertResult(prs, [])))
        assert "还有" not in text

    def test_item_line_contains_link(self):
        d = build(UpsertResult([_pr(4321, "merged")], []))
        text = card.render_text(d)
        assert "https://github.com/deepmodeling/deepmd-kit/pull/4321" in text
        assert "deepmd-kit #4321" in text

    def test_brackets_in_title_are_escaped(self):
        """PR 标题常见 "[BUG] xxx" 前缀，方括号会破坏 markdown 链接。"""
        ev = _pr(1, "merged", title="[BUG] 崩溃")
        text = card.render_text(build(UpsertResult([ev], [])))
        assert "\\[BUG\\]" in text

    def test_since_text_is_shown_when_known(self):
        d = build(UpsertResult([], []), last_notify_at="2026-08-09T02:00:00+00:00")
        assert "08-09 10:00" in card.render_text(d)

    def test_totals_line_is_appended(self):
        d = build(UpsertResult([], []), total_contributors=366,
                  total_commits=2527)
        text = card.render_text(d)
        assert "366 位贡献者" in text
        assert "2527 次提交" in text

    def test_totals_line_includes_pr_and_issue(self):
        """底部与顶部同样详细：提交 · PR 合并 · PR 新建 · issue。

        数字来自 summary.csv 已有的三列，不额外发 API 请求。
        """
        d = build(UpsertResult([], []), total_contributors=366,
                  total_commits=2527, total_prs_merged=1303,
                  total_prs_created=1860, total_issues=765)
        text = card.render_text(d)
        assert "1303 个 PR 合并" in text
        assert "1860 个 PR 新建" in text
        assert "765 个 issue" in text

    def test_totals_omits_pr_and_issue_when_unavailable(self):
        """拿不到 PR/issue 数据时整项省略，不能显示 0。

        --no-fetch 或无当天 API 缓存时 collect_api_stats 返回空，三项会被
        加成 0。而上方分区可能正列着几百个合并的 PR —— 底部写"0 个 PR 合并"
        会让整条日报自相矛盾。实测确认过这个场景。
        """
        d = build(UpsertResult([], []), total_contributors=51,
                  total_commits=506, total_prs_merged=None,
                  total_prs_created=None, total_issues=None)
        text = card.render_text(d)
        assert "51 位贡献者" in text and "506 次提交" in text
        assert "0 个 PR" not in text and "0 个 issue" not in text

    def test_totals_line_labels_partial_run_scope(self):
        """只跑部分仓库时必须标注范围。

        昨天正是这里误导了人：单仓库跑出的 20 位贡献者被写成"今年至今"，
        看起来像社区总量。
        """
        d = build(UpsertResult([], []), total_contributors=20,
                  total_commits=17, repos_processed=1, repos_total=34)
        assert "今年至今（1/34 个仓库）" in card.render_text(d)

    def test_full_run_scope_is_not_labeled(self):
        """跑全量时不加括号，避免噪音。"""
        d = build(UpsertResult([], []), total_contributors=366,
                  total_commits=2527, repos_processed=34, repos_total=34)
        text = card.render_text(d)
        assert "今年至今：" in text and "个仓库" not in text


class TestCategorySections:
    """日报按意图分区（新功能 / 修复 / 重构 ...）而不是按 PR 状态分区。

    运营关心的是「社区在干什么」，不是「PR 合并了还是新建」。
    状态信息不丢，挂在每条末尾。
    """

    def test_sections_are_named_by_category(self):
        evs = [_pr(1, "merged", title="feat: 新的东西"),
               _pr(2, "merged", title="fix: 修个 bug")]
        text = card.render_text(build(UpsertResult(evs, [])))
        assert "**新功能**" in text
        assert "**修复**" in text
        # 不再按状态分区
        assert "**合并的 PR**" not in text
        assert "**新建的 PR**" not in text

    def test_new_feature_section_comes_before_fix(self):
        evs = [_pr(1, "merged", title="fix: 修复"),
               _pr(2, "merged", title="feat: 新功能")]
        text = card.render_text(build(UpsertResult(evs, [])))
        assert text.index("**新功能**") < text.index("**修复**")

    def test_other_section_comes_last(self):
        evs = [_pr(1, "merged", title="认不出来的标题"),
               _pr(2, "merged", title="fix: 修复")]
        text = card.render_text(build(UpsertResult(evs, [])))
        assert text.index("**修复**") < text.index("**其他**")

    def test_empty_categories_are_hidden(self):
        evs = [_pr(1, "merged", title="feat: 只有新功能")]
        text = card.render_text(build(UpsertResult(evs, [])))
        assert "**新功能**" in text
        for absent in ("**修复**", "**重构**", "**性能**",
                       "**测试**", "**文档**", "**工程**", "**其他**"):
            assert absent not in text

    def test_merged_and_opened_prs_share_one_category(self):
        """同一意图的 PR 不因状态被拆到两个分区。"""
        evs = [_pr(1, "merged", title="feat: 已合并"),
               _pr(2, "opened", title="feat: 新建的")]
        text = card.render_text(build(UpsertResult(evs, [])))
        assert text.count("**新功能**") == 1

    def test_pr_state_is_still_visible(self):
        """按意图分组后，合并/新建仍要逐条看得出来。

        标题故意用中性词（alpha/beta）：用「已合并」这种标题会让断言
        命中标题本身，测不出状态标注到底在不在。
        """
        evs = [_pr(1, "merged", title="feat: alpha"),
               _pr(2, "opened", title="feat: beta")]
        text = card.render_text(build(UpsertResult(evs, [])))
        block = text.split("**新功能**", 1)[1]
        line1 = [ln for ln in block.splitlines() if "alpha" in ln][0]
        line2 = [ln for ln in block.splitlines() if "beta" in ln][0]
        assert "合并" in line1, f"合并状态丢了：{line1!r}"
        assert "新建" in line2, f"新建状态丢了：{line2!r}"

    def test_issue_lines_have_no_pr_state(self):
        """issue 不是 PR，不该被标上合并/新建。"""
        text = card.render_text(build(UpsertResult([_issue(100)], [])))
        block = text.split("**新增 issue**", 1)[1]
        for ln in block.splitlines():
            assert "合并" not in ln
            assert "新建" not in ln

    def test_each_category_capped_at_max_items(self):
        evs = [_pr(n, "merged", title=f"fix: 修复{n}") for n in range(1, 12)]
        text = card.render_text(build(UpsertResult(evs, [])))
        shown = [ln for ln in text.splitlines() if "deepmd-kit #" in ln]
        assert len(shown) == MAX_ITEMS
        assert f"还有 {11 - MAX_ITEMS} 条" in text

    def test_cap_applies_per_category_not_globally(self):
        """每类各自 5 条，不是全卡片共 5 条。"""
        evs = ([_pr(n, "merged", title=f"fix: 修{n}") for n in range(1, 8)]
               + [_pr(n, "merged", title=f"feat: 新{n}") for n in range(20, 27)])
        text = card.render_text(build(UpsertResult(evs, [])))
        shown = [ln for ln in text.splitlines() if "deepmd-kit #" in ln]
        assert len(shown) == MAX_ITEMS * 2

    def test_issues_stay_in_their_own_section(self):
        """issue 不参与意图分类（前缀规范度只有 9%）。"""
        evs = [_pr(1, "merged", title="feat: 功能"), _issue(100)]
        text = card.render_text(build(UpsertResult(evs, [])))
        assert "**新增 issue**" in text


class TestRenderCard:
    def test_produces_interactive_card(self):
        c = card.render(build(UpsertResult([_commit("a")], [])))
        assert c["msg_type"] == "interactive"
        assert c["card"]["header"]["title"]["content"].startswith(
            "DeepModeling 社区日报")

    def test_oversized_content_is_degraded_under_limit(self):
        """几百条事件时必须仍能发出去，而不是整条丢失。"""
        prs = [_pr(n, "merged", title="标" * 200) for n in range(1, 400)]
        c = card.render(build(UpsertResult(prs, [])))
        size = len(json.dumps(c, ensure_ascii=False).encode("utf-8"))
        assert size <= card.SIZE_LIMIT

    def test_normal_content_is_not_degraded(self):
        prs = [_pr(n, "merged") for n in range(1, 26)]
        c = card.render(build(UpsertResult(prs, [])))
        assert f"还有 {25 - MAX_ITEMS} 条" in c["card"]["elements"][0]["text"]["content"]


class TestSign:
    def test_matches_official_algorithm(self):
        """交叉验证：独立实现一遍飞书文档的算法，结果必须一致。

        这个算法反直觉（密钥当 key、对空串摘要），照抄容易抄错，
        所以这里不是自己验自己，而是按文档重写一遍做对照。
        """
        ts, secret = "1599360473", "test-secret"
        expected = base64.b64encode(
            hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"",
                     digestmod=hashlib.sha256).digest()).decode("utf-8")
        assert feishu.gen_sign(ts, secret) == expected

    def test_sign_changes_with_timestamp(self):
        assert feishu.gen_sign("1", "s") != feishu.gen_sign("2", "s")

    def test_sign_is_deterministic(self):
        assert feishu.gen_sign("1", "s") == feishu.gen_sign("1", "s")


class TestMask:
    def test_only_last_four_chars_survive(self):
        masked = feishu.mask("https://open.feishu.cn/open-apis/bot/v2/hook/abcd1234")
        assert masked == "...1234"
        assert "open.feishu.cn" not in masked

    def test_unconfigured_url(self):
        assert feishu.mask("") == "(未配置)"


class TestSend:
    def _ok(self):
        r = Mock()
        r.json.return_value = {"code": 0, "msg": "success"}
        return r

    def test_posts_payload_to_url(self):
        s = Mock()
        s.post.return_value = self._ok()
        feishu.send({"msg_type": "text"}, url="https://x/hook/ab", secret="",
                    session=s)
        assert s.post.call_count == 1
        assert s.post.call_args[0][0] == "https://x/hook/ab"

    def test_signature_fields_are_added_when_secret_set(self):
        s = Mock()
        s.post.return_value = self._ok()
        feishu.send({"msg_type": "text"}, url="https://x", secret="sec",
                    session=s)
        body = s.post.call_args[1]["json"]
        assert "timestamp" in body and "sign" in body

    def test_no_signature_fields_without_secret(self):
        s = Mock()
        s.post.return_value = self._ok()
        feishu.send({"msg_type": "text"}, url="https://x", secret="", session=s)
        body = s.post.call_args[1]["json"]
        assert "sign" not in body

    def test_network_error_is_retried(self, monkeypatch):
        monkeypatch.setattr(feishu.time, "sleep", lambda *_: None)
        s = Mock()
        s.post.side_effect = [ConnectionError("boom"), ConnectionError("boom"),
                              self._ok()]
        feishu.send({}, url="https://x", secret="", session=s)
        assert s.post.call_count == 3

    def test_gives_up_after_retries(self, monkeypatch):
        monkeypatch.setattr(feishu.time, "sleep", lambda *_: None)
        s = Mock()
        s.post.side_effect = ConnectionError("boom")
        with pytest.raises(feishu.FeishuError):
            feishu.send({}, url="https://x", secret="", session=s)
        assert s.post.call_count == feishu.RETRIES

    def test_application_error_is_not_retried(self):
        """重试一个签名错误没有意义，只会拖慢整次运行。"""
        s = Mock()
        r = Mock()
        r.json.return_value = {"code": 19021, "msg": "sign match fail"}
        s.post.return_value = r
        with pytest.raises(feishu.FeishuError) as exc:
            feishu.send({}, url="https://x", secret="s", session=s)
        assert s.post.call_count == 1
        assert "19021" in str(exc.value)

    def test_error_hint_is_included(self):
        s = Mock()
        r = Mock()
        r.json.return_value = {"code": 9499, "msg": "Bad Request"}
        s.post.return_value = r
        with pytest.raises(feishu.FeishuError) as exc:
            feishu.send({}, url="https://x", secret="", session=s)
        assert "20 KB" in str(exc.value)

    def test_missing_url_raises(self, monkeypatch):
        monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
        with pytest.raises(feishu.FeishuError):
            feishu.send({}, url="", secret="")


class TestLoadEnv:
    def test_reads_key_values(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("# 注释\n\nFOO=bar\nBAZ = qux \n", encoding="utf-8")
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)
        feishu.load_env(str(f))
        import os
        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_does_not_override_existing(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("FOO=fromfile\n", encoding="utf-8")
        monkeypatch.setenv("FOO", "fromenv")
        feishu.load_env(str(f))
        import os
        assert os.environ["FOO"] == "fromenv"

    def test_missing_file_is_ignored(self, tmp_path):
        feishu.load_env(str(tmp_path / "nope.env"))


class TestDailyHeadline:
    """固定每天跑时的文案：正常写"昨日社区动态"，异常时回退。

    增量按"本次运行新看到的"判定，不是按事件时间。固定 11 点跑保证的是
    运行时间固定，不保证内容都发生在昨天 —— 推送失败补推时，卡片里装的
    是两天的内容，此时再写"昨日"就是说谎。故按距上次成功推送的间隔切换。
    """

    def test_normal_daily_run_says_yesterday(self):
        d = build(UpsertResult([_commit("a")], []),
                  last_notify_at="2026-08-10T02:00:00+00:00",
                  now=datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc))
        text = card.render_text(d)
        assert "昨日社区动态" in text
        assert "自上次汇报" not in text

    def test_gap_longer_than_a_day_falls_back(self):
        """隔了三天（推送失败或停跑）→ 回退到带日期的说法。"""
        d = build(UpsertResult([_commit("a")], []),
                  last_notify_at="2026-08-08T02:00:00+00:00",
                  now=datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc))
        text = card.render_text(d)
        assert "自上次汇报" in text
        assert "昨日社区动态" not in text

    def test_slightly_late_run_still_says_yesterday(self):
        """机器起停、任务延迟让间隔浮动到 30 小时，仍算正常日报。"""
        d = build(UpsertResult([_commit("a")], []),
                  last_notify_at="2026-08-09T20:00:00+00:00",
                  now=datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc))
        assert "昨日社区动态" in card.render_text(d)

    def test_no_previous_notify_falls_back(self):
        """从没成功推送过时不能称"昨日"。"""
        d = build(UpsertResult([_commit("a")], []), last_notify_at="")
        assert "昨日社区动态" not in card.render_text(d)


class TestFailedRepos:
    """有仓库抓取失败时在卡片里如实标出。

    底部已有"36/38 个仓库"的范围标注，但只看数字不知道少了谁。
    列出名字，看日报的人才能判断自己关心的仓库在不在里面。
    """

    def test_failed_repos_are_listed(self):
        d = build(UpsertResult([_commit("a")], []),
                  failed_repos=["abacus-develop", "community"])
        text = card.render_text(d)
        assert "abacus-develop" in text and "community" in text

    def test_no_line_when_nothing_failed(self):
        d = build(UpsertResult([_commit("a")], []), failed_repos=[])
        assert "未能抓取" not in card.render_text(d)

    def test_many_failures_are_truncated(self):
        """失败很多时不该刷屏。"""
        d = build(UpsertResult([_commit("a")], []),
                  failed_repos=[f"repo{i}" for i in range(20)])
        text = card.render_text(d)
        assert "还有" in text


class TestFailedReposCarryOver:
    """失败仓库的文案要声明会累计到明天。

    这句是真的：增量按 first_seen_run > 上次成功推送的 run 判定，
    今天没抓到的仓库，明天抓到时那些事件才首次入库，自然进明天的日报。
    """

    def test_states_carry_over_to_tomorrow(self):
        d = build(UpsertResult([_commit("a")], []),
                  failed_repos=["abacus-develop"])
        text = card.render_text(d)
        assert "明天" in text and "abacus-develop" in text

    def test_no_carry_over_note_when_nothing_failed(self):
        d = build(UpsertResult([_commit("a")], []), failed_repos=[])
        assert "明天" not in card.render_text(d)


class TestPeriodBoundaries:
    """周月边界按东八区算，再转 UTC 查库。

    库里存 UTC，而"上周一到周日"是本地概念。直接用 UTC 日界会让
    周一早八点前的事件落到上一周去。
    """

    def test_last_week_range(self):
        from contributors.notify.period import last_week_range
        # 2026-08-12 是周三，上周应为 08-03(一) ~ 08-10(一，不含)
        s, e = last_week_range(datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc))
        assert s == "2026-08-02T16:00:00+00:00"   # 08-03 00:00 CST
        assert e == "2026-08-09T16:00:00+00:00"   # 08-10 00:00 CST

    def test_last_month_range(self):
        from contributors.notify.period import last_month_range
        s, e = last_month_range(datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc))
        assert s == "2026-06-30T16:00:00+00:00"   # 07-01 00:00 CST
        assert e == "2026-07-31T16:00:00+00:00"   # 08-01 00:00 CST

    def test_last_month_range_across_year(self):
        from contributors.notify.period import last_month_range
        s, e = last_month_range(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc))
        assert s == "2025-11-30T16:00:00+00:00"   # 12-01 00:00 CST
        assert e == "2025-12-31T16:00:00+00:00"   # 01-01 00:00 CST


class TestPeriodDigest:
    """周报月报：汇总加排行榜，不逐条列。"""

    def _mk(self, n, kind="commit", author="alice", repo="r1"):
        return Event(kind, repo, None if kind == "commit" else n,
                     f"sha{n}" if kind == "commit" else None,
                     f"标题{n}", f"https://x/{n}", author,
                     "2026-08-05T10:00:00Z",
                     "merged" if kind == "pr" else None)

    def test_counts_by_kind(self):
        from contributors.notify.period import build_period
        evs = [self._mk(1), self._mk(2),
               self._mk(3, "pr"), self._mk(4, "issue")]
        d = build_period(evs, "上周", "08-03 ~ 08-09")
        assert d.commit_count == 2
        assert d.merged_count == 1
        assert d.issue_count == 1

    def test_top_contributors_ranked(self):
        from contributors.notify.period import build_period
        evs = ([self._mk(i, author="bob") for i in range(5)]
               + [self._mk(i + 10, author="alice") for i in range(2)])
        d = build_period(evs, "上周", "范围")
        assert d.top_contributors[0] == ("bob", 5)
        assert d.top_contributors[1] == ("alice", 2)

    def test_top_repos_ranked(self):
        from contributors.notify.period import build_period
        evs = ([self._mk(i, repo="big") for i in range(4)]
               + [self._mk(10, repo="small")])
        d = build_period(evs, "上周", "范围")
        assert d.top_repos[0] == ("big", 4)

    def test_authorless_events_not_ranked(self):
        """author 为 null 的事件不该冒出一个空名字的贡献者。"""
        from contributors.notify.period import build_period
        e = self._mk(1)
        e.author_login = None
        d = build_period([e], "上周", "范围")
        assert all(name for name, _ in d.top_contributors)

    def test_renders_period_card(self):
        from contributors.notify.period import build_period, render_period
        evs = [self._mk(1), self._mk(2, "pr", author="bob")]
        payload = render_period(build_period(evs, "上周", "08-03 ~ 08-09"))
        text = payload["card"]["elements"][0]["text"]["content"]
        assert "上周" in text and "08-03 ~ 08-09" in text
        assert "bob" in text

    def test_empty_period_still_renders(self):
        from contributors.notify.period import build_period, render_period
        payload = render_period(build_period([], "上周", "范围"))
        assert payload["msg_type"] == "interactive"


class TestPeriodTopN:
    """月报榜单比周报长：月度数据量大，前 5 名区分度不够。"""

    def _mk(self, n, author):
        return Event("commit", "r1", None, f"sha{n}", f"t{n}",
                     f"https://x/{n}", author, "2026-08-05T10:00:00Z", None)

    def test_weekly_lists_five(self):
        from contributors.notify.period import build_period
        evs = [self._mk(i, f"u{i:02d}") for i in range(20)]
        d = build_period(evs, "上周", "范围", top_n=5)
        assert len(d.top_contributors) == 5

    def test_monthly_lists_ten(self):
        from contributors.notify.period import build_period
        evs = [self._mk(i, f"u{i:02d}") for i in range(20)]
        d = build_period(evs, "上月", "范围", top_n=10)
        assert len(d.top_contributors) == 10

    def test_fewer_than_limit_is_fine(self):
        from contributors.notify.period import build_period
        evs = [self._mk(i, f"u{i:02d}") for i in range(3)]
        d = build_period(evs, "上月", "范围", top_n=10)
        assert len(d.top_contributors) == 3


class TestCommitFallback:
    """整张卡片只有 commit 时才列出它们。

    平时 commit 只计数不列（一天几十条会刷屏），但当天没有任何 PR/issue
    时，卡片就只剩一个孤零零的数字，什么信息都没有 —— 2026-08-12 实际
    推出过这样一条："4 次提交" 加底部累计，仅此而已。
    """

    def test_commits_listed_when_no_pr_or_issue(self):
        d = build(UpsertResult([_commit("aaa1"), _commit("bbb2")], []))
        text = card.render_text(d)
        assert "修复" in text          # commit 标题
        assert "2 次提交" in text      # 数字仍在

    def test_commits_not_listed_when_pr_present(self):
        """有 PR 时保持原样，只计数不列 —— 否则忙时会刷屏。"""
        d = build(UpsertResult([_commit("aaa1"), _pr(1, "open")], []))
        text = card.render_text(d)
        assert "1 次提交" in text
        assert "修复" not in text

    def test_commit_line_has_no_author(self):
        """commit 事件没有 author_login（EVENT_LOG_FORMAT 不取作者）。"""
        d = build(UpsertResult([_commit("aaa1")], []))
        assert "@" not in card.render_text(d)

    def test_commit_list_is_truncated(self):
        """即使只有 commit，也不能无限列。"""
        d = build(UpsertResult([_commit(f"s{i}") for i in range(25)], []))
        text = card.render_text(d)
        assert f"还有 {25 - MAX_ITEMS} 条" in text

    def test_commit_line_links_to_commit(self):
        d = build(UpsertResult([_commit("abc123")], []))
        assert "commit/abc123" in card.render_text(d)


class TestQuietDayFallback:
    """昨日无更新时也要发，并贴一个非零区间的量。

    不发的话，群里第一反应是"脚本挂了"。发一条明说"无更新"再带上近期
    活动量，既排除了故障怀疑，也让人知道系统在正常工作。
    """

    def test_quiet_day_is_not_empty_when_fallback_given(self):
        d = build(UpsertResult([], []),
                  recent_label="本周至今", recent_counts={"commits": 12})
        assert not d.is_empty, "有回退数据时不该被当成空战报跳过"

    def test_states_no_update(self):
        d = build(UpsertResult([], []),
                  recent_label="本周至今", recent_counts={"commits": 12})
        text = card.render_text(d)
        assert "无更新" in text

    def test_shows_recent_counts_only_as_numbers(self):
        d = build(UpsertResult([], []), recent_label="本周至今",
                  recent_counts={"commits": 12, "merged": 3, "issues": 1})
        text = card.render_text(d)
        assert "本周至今" in text
        assert "12 次提交" in text
        assert "3 个 PR 合并" in text

    def test_no_recent_data_says_so(self):
        """三级回退都为零时明说，那本身也是有效信息。"""
        d = build(UpsertResult([], []), recent_label="", recent_counts={})
        text = card.render_text(d)
        assert "无更新" in text

    def test_normal_day_unaffected(self):
        """有增量时不该出现回退文案。"""
        d = build(UpsertResult([_commit("a")], []),
                  recent_label="本周至今", recent_counts={"commits": 12})
        text = card.render_text(d)
        assert "无更新" not in text
        assert "本周至今" not in text


class TestRecentActivity:
    """逐级回退找一个非零区间：本周至今 → 上周 → 本月至今。"""

    def _counts(self, n):
        return {"commits": n} if n else {}

    def _fake(self, n=1):
        return [Event("commit", "r", None, f"s{i}", "t", "u", None,
                      "2026-08-10T00:00:00Z", None) for i in range(n)]

    def test_prefers_this_week(self):
        from contributors.notify.period import pick_recent
        got = pick_recent(lambda s, e: self._fake(3),
                          now=datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc))
        assert got[0] == "本周至今"

    def test_falls_back_to_last_week(self):
        from contributors.notify.period import pick_recent
        calls = []

        def fetch(s, e):
            calls.append((s, e))
            return [] if len(calls) == 1 else self._fake(2)

        got = pick_recent(fetch,
                          now=datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc))
        assert got[0] == "上周"

    def test_falls_back_to_this_month(self):
        from contributors.notify.period import pick_recent
        calls = []

        def fetch(s, e):
            calls.append((s, e))
            return self._fake(1) if len(calls) == 3 else []

        got = pick_recent(fetch,
                          now=datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc))
        assert got[0] == "本月至今"

    def test_all_empty_returns_blank_label(self):
        from contributors.notify.period import pick_recent
        got = pick_recent(lambda s, e: [],
                          now=datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc))
        assert got[0] == ""
        assert got[1] == {}


class TestChronicFailures:
    """连续多次失败的仓库要单独告警。

    与"未能抓取"分行的理由：那条说的是"今天没抓到、明天会补"，
    对连挂三天的仓库不再成立 —— 改名、删除或权限变更永远不会自愈。
    """

    def test_chronic_repo_is_warned(self):
        d = build(UpsertResult([_commit("a")], []),
                  chronic_failures={"abacus-develop": 3})
        text = card.render_text(d)
        assert "连续抓取失败" in text
        assert "abacus-develop" in text and "3" in text

    def test_no_line_when_none_chronic(self):
        d = build(UpsertResult([_commit("a")], []), chronic_failures={})
        assert "连续抓取失败" not in card.render_text(d)


class TestChronicFailureReasons:
    """告警文案按失败原因分流。

    2026-08-14 的真实教训：abacus-develop 连挂三次，文案写死"可能是改名、
    删除或权限变更"，实际原因是 RemoteDisconnected（该仓库 PR+Issue 需
    306 次连续 GraphQL 请求，中途被掐断）。仓库好好的，三个猜测全不成立。
    """

    def test_network_reason_does_not_blame_repo(self):
        d = build(UpsertResult([_commit("a")], []),
                  chronic_failures={"abacus-develop": 3},
                  chronic_reasons={"abacus-develop": "network"})
        text = card.render_text(d)
        assert "连续抓取失败" in text
        assert "网络" in text
        # 关键：网络故障不该把人往"仓库出事了"的方向引
        assert "改名" not in text and "删除" not in text

    def test_permanent_reason_points_at_repo(self):
        d = build(UpsertResult([_commit("a")], []),
                  chronic_failures={"gone": 3},
                  chronic_reasons={"gone": "permanent"})
        text = card.render_text(d)
        assert "改名" in text or "删除" in text or "权限" in text

    def test_unknown_reason_keeps_neutral_wording(self):
        """认不出原因时既不甩锅网络也不甩锅仓库，只说去查。"""
        d = build(UpsertResult([_commit("a")], []),
                  chronic_failures={"x": 3},
                  chronic_reasons={"x": "unknown"})
        text = card.render_text(d)
        assert "连续抓取失败" in text
        assert "改名" not in text and "网络" not in text

    def test_missing_reason_falls_back_to_neutral(self):
        """没传 chronic_reasons 时不能崩，退化成中性文案。"""
        d = build(UpsertResult([_commit("a")], []),
                  chronic_failures={"x": 3})
        text = card.render_text(d)
        assert "连续抓取失败" in text

    def test_mixed_reasons_are_grouped(self):
        """网络类和永久类同时存在时分开说，不能混成一句。"""
        d = build(UpsertResult([_commit("a")], []),
                  chronic_failures={"neta": 3, "goneb": 4},
                  chronic_reasons={"neta": "network", "goneb": "permanent"})
        text = card.render_text(d)
        assert "neta" in text and "goneb" in text
        assert "网络" in text
        assert "改名" in text or "删除" in text or "权限" in text

    def test_chronic_repo_not_duplicated_in_failed_line(self):
        """达阈值的仓库只出现在告警行，不在"未能抓取"里重复。"""
        d = build(UpsertResult([_commit("a")], []),
                  failed_repos=["abacus-develop", "deepmd-kit"],
                  chronic_failures={"abacus-develop": 3})
        text = card.render_text(d)
        failed_line = [l for l in text.split("\n") if l.startswith("未能抓取")]
        assert failed_line and "abacus-develop" not in failed_line[0]
        assert "deepmd-kit" in failed_line[0]

    def test_chronic_shown_when_no_increment(self):
        """昨日无更新那条回退卡片里同样要告警。"""
        d = build(UpsertResult([], []),
                  chronic_failures={"abacus-develop": 5},
                  recent_label="本周至今", recent_counts={"commit": 3})
        assert "连续抓取失败" in card.render_text(d)

    def test_many_chronic_are_truncated(self):
        d = build(UpsertResult([_commit("a")], []),
                  chronic_failures={f"repo{i}": 3 for i in range(20)})
        assert "还有" in card.render_text(d)


class TestScopeVsFailure:
    """"只跑了子集"与"抓取失败"是两回事，底部标注只该管前者。

    底部那几个数字读的是全年累计的 CSV：失败仓库今年前面几百天的数据
    一条不少，缺的只是今天那一天。写成"36/38 个仓库"会让人以为这份
    年度统计整个不含那两个仓库 —— 把一天的缺口说成了全年的。
    """

    def test_failure_does_not_shrink_scope(self):
        d = build(UpsertResult([_commit("a")], []), total_commits=4768,
                  repos_processed=36, repos_total=38,
                  failed_repos=["abacus-develop", "deepmd-kit"])
        text = card.render_text(d)
        assert "今年至今：" in text, "失败不该让底部标注范围"
        assert "36/38" not in text

    def test_chronic_failure_also_does_not_shrink_scope(self):
        d = build(UpsertResult([_commit("a")], []), total_commits=4768,
                  repos_processed=37, repos_total=38,
                  chronic_failures={"abacus-develop": 3})
        assert "今年至今：" in card.render_text(d)

    def test_subset_run_still_labeled(self):
        """真的只跑了子集时，标注照旧 —— 那正是它存在的理由。"""
        d = build(UpsertResult([], []), total_contributors=20,
                  total_commits=17, repos_processed=1, repos_total=34)
        assert "今年至今（1/34 个仓库）" in card.render_text(d)

    def test_subset_with_failure_counts_both(self):
        """既跑子集又有失败：标注按"本来要跑几个"算，不把失败算进缺口。"""
        d = build(UpsertResult([], []), total_commits=17,
                  repos_processed=9, repos_total=34,
                  failed_repos=["x"])
        assert "今年至今（10/34 个仓库）" in card.render_text(d)


class TestTitleAnnouncesFailure:
    """标题要点一句数据缺失 —— 群里很多人只扫标题，不点开正文。"""

    def test_title_mentions_missing_repos(self):
        d = build(UpsertResult([_commit("a")], []),
                  failed_repos=["abacus-develop", "deepmd-kit"])
        title = card.render(d)["card"]["header"]["title"]["content"]
        assert "2 个仓库今日数据缺失" in title

    def test_title_counts_chronic_too(self):
        d = build(UpsertResult([_commit("a")], []),
                  failed_repos=["deepmd-kit"],
                  chronic_failures={"abacus-develop": 3})
        title = card.render(d)["card"]["header"]["title"]["content"]
        assert "2 个仓库今日数据缺失" in title

    def test_clean_run_title_unchanged(self):
        d = build(UpsertResult([_commit("a")], []), failed_repos=[])
        title = card.render(d)["card"]["header"]["title"]["content"]
        assert "缺失" not in title and "DeepModeling 社区日报" in title


class TestDailyWindowAnchoring:
    """日报窗口锚定当天 0:00~24:00（东八区整个自然日），由日期决定，与推送时刻无关。

    锚定的意义在于补推可复现：若按"此刻往前 24 小时"算，0 点推送失败、
    1 点手动补推，昨天 0-1 点那一小时两个窗口都不覆盖，会永久漏掉。
    """

    def test_window_is_previous_midnight_to_midnight(self):
        from datetime import date as _d
        start, end = period.daily_range(_d(2026, 8, 13))
        # 东八区 0:00 = UTC 前一天 16:00
        assert start == "2026-08-11T16:00:00+00:00"
        assert end == "2026-08-12T16:00:00+00:00"

    def test_window_independent_of_push_time(self):
        """同一天不论几点算，窗口都一样 —— 补推可复现的根据。"""
        from datetime import date as _d, datetime as _dt, timezone as _tz
        day = _d(2026, 8, 13)
        morning = period.daily_range(day, now=_dt(2026, 8, 13, 1, tzinfo=_tz.utc))
        evening = period.daily_range(day, now=_dt(2026, 8, 13, 23, tzinfo=_tz.utc))
        assert morning == evening

    def test_consecutive_days_do_not_overlap_or_gap(self):
        """相邻两天首尾相接：不重不漏。"""
        from datetime import date as _d
        _, end12 = period.daily_range(_d(2026, 8, 12))
        start13, _ = period.daily_range(_d(2026, 8, 13))
        assert end12 == start13

    def test_defaults_to_today_in_cst(self):
        from datetime import datetime as _dt, timezone as _tz
        # 东八区 8-13 09:00（UTC 8-13 01:00）时，"今天"是 8-13
        now = _dt(2026, 8, 13, 1, tzinfo=_tz.utc)
        start, end = period.daily_range(now=now)
        assert end == "2026-08-12T16:00:00+00:00"
        assert start == "2026-08-11T16:00:00+00:00"


class TestReportDate:
    """_report_date：未指定 --date 时取今天（东八区），作为 daily_range 的窗口结束日。

    daily_range(day) 返回 [day-1 0:00, day 0:00) 东八区半开区间，
    所以要覆盖昨天落库的事件，必须传今天。旧实现传昨天导致窗口落到
    前天（错位一天）。不能写死钟点——只验证「今天」。
    """

    def test_no_date_returns_today_in_cst(self):
        """未指定 --date 时，返回东八区的今天日期（daily_range 的窗口结束日）。"""
        from datetime import timezone as _tz
        from contributors.main import _report_date
        from contributors.config import Config
        from contributors.notify.period import CST
        import datetime as _dt_mod

        cfg = Config(org="x", since=_dt_mod.datetime(2026, 1, 1, tzinfo=_tz.utc),
                     until=_dt_mod.datetime(2026, 12, 31, tzinfo=_tz.utc),
                     include_forks="all", max_repo_size=0)
        result = _report_date(cfg)

        expected = _dt_mod.datetime.now(_tz.utc).astimezone(CST).date()
        assert result == expected

    def test_explicit_date_is_returned_as_is(self):
        """--date 显式指定时原样返回，不受当前时刻影响。"""
        from datetime import date as _d, timezone as _tz
        from contributors.main import _report_date
        from contributors.config import Config
        import datetime as _dt_mod

        cfg = Config(org="x", since=_dt_mod.datetime(2026, 1, 1, tzinfo=_tz.utc),
                     until=_dt_mod.datetime(2026, 12, 31, tzinfo=_tz.utc),
                     include_forks="all", max_repo_size=0, date="2026-08-14")
        assert _report_date(cfg) == _d(2026, 8, 14)


class TestPeriodPrBreakdown:
    """周月报的「PR 动向」分区：仓库 × 意图矩阵。

    和「活跃仓库」并列但口径不同 —— 那个数的是全部事件
    （commit+PR+issue），这个只数 PR。同一个仓库两边数字不一样是正常的。
    """

    def _build(self, evs, top_n=5):
        from contributors.notify.period import build_period
        return build_period(evs, "上周", "09-07 ~ 09-13", top_n=top_n)

    def test_breakdown_counts_by_repo_and_category(self):
        evs = [_pr(1, "merged", repo="deepmd-kit", title="fix: a"),
               _pr(2, "merged", repo="deepmd-kit", title="fix: b"),
               _pr(3, "merged", repo="deepmd-kit", title="feat: c")]
        d = self._build(evs)
        assert d.pr_breakdown[0][0] == "deepmd-kit"
        assert dict(d.pr_breakdown[0][1])["修复"] == 2
        assert dict(d.pr_breakdown[0][1])["新功能"] == 1

    def test_commits_and_issues_do_not_enter_breakdown(self):
        """只统计 PR：commit 前缀规范度 44%、issue 只有 9%。"""
        evs = [_pr(1, "merged", repo="r1", title="fix: x"),
               _commit("sha1", repo="r1"), _issue(9, repo="r1")]
        d = self._build(evs)
        total = sum(n for _, n in d.pr_breakdown[0][1])
        assert total == 1

    def test_repos_sorted_by_pr_count(self):
        evs = ([_pr(n, "merged", repo="few", title="fix: x") for n in range(1, 3)]
               + [_pr(n, "merged", repo="many", title="fix: y") for n in range(10, 15)])
        d = self._build(evs)
        assert [r for r, _ in d.pr_breakdown] == ["many", "few"]

    def test_breakdown_respects_top_n(self):
        evs = [_pr(n, "merged", repo=f"repo{n}", title="fix: x") for n in range(1, 9)]
        d = self._build(evs, top_n=5)
        assert len(d.pr_breakdown) == 5

    def test_repo_with_no_pr_is_absent(self):
        evs = [_commit("sha1", repo="only-commits")]
        d = self._build(evs)
        assert d.pr_breakdown == []

    def test_categories_sorted_by_count_desc(self):
        evs = ([_pr(n, "merged", repo="r", title="fix: x") for n in range(1, 4)]
               + [_pr(9, "merged", repo="r", title="feat: y")])
        d = self._build(evs)
        cats = [c for c, _ in d.pr_breakdown[0][1]]
        assert cats[0] == "修复"


class TestPeriodRenderBreakdown:
    def _text(self, evs, top_n=5):
        from contributors.notify.period import build_period, render_text
        return render_text(build_period(evs, "上周", "09-07 ~ 09-13", top_n=top_n))

    def test_section_is_rendered(self):
        evs = [_pr(1, "merged", repo="deepmd-kit", title="fix: a")]
        assert "**PR 动向**" in self._text(evs)

    def test_section_absent_when_no_pr(self):
        assert "**PR 动向**" not in self._text([_commit("s1")])

    def test_line_says_pr_not_activity(self):
        """措辞写「个 PR」，和上面「次活动」区分开，否则两个数字打架。"""
        evs = [_pr(1, "merged", repo="deepmd-kit", title="fix: a")]
        text = self._text(evs)
        block = text.split("**PR 动向**", 1)[1]
        assert "个 PR" in block
        assert "次活动" not in block

    def test_categories_listed_in_line(self):
        evs = [_pr(1, "merged", repo="r", title="fix: a"),
               _pr(2, "merged", repo="r", title="feat: b")]
        block = self._text(evs).split("**PR 动向**", 1)[1]
        assert "修复 1" in block
        assert "新功能 1" in block

    def test_appears_after_active_repos(self):
        evs = [_pr(1, "merged", repo="r", title="fix: a")]
        text = self._text(evs)
        assert text.index("**活跃仓库**") < text.index("**PR 动向**")

    def test_no_detail_lines(self):
        """周月报只给画像，不列 PR 明细（明细去看板）。"""
        evs = [_pr(1, "merged", repo="r", title="fix: 某个具体标题")]
        assert "某个具体标题" not in self._text(evs)
