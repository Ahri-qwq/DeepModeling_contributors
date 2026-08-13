"""推送层测试：digest 聚合、card 渲染、feishu 签名与重试。全部离线。"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from contributors.events import Event, StateChange
from contributors.notify import card, feishu
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
        """提交者放标题后面（用户明确要求）。"""
        d = build(UpsertResult([_pr(1, "open", title="修复某问题")], []))
        line = [l for l in card.render_text(d).splitlines() if "#1" in l][0]
        assert line.index("修复某问题") < line.index("@alice")

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
        assert "还有 15 条" in text

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
        assert "还有 15 条" in c["card"]["elements"][0]["text"]["content"]


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

    增量按"本次运行新看到的"判定，不是按事件时间。固定 10 点跑保证的是
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
        assert "还有 15 条" in text

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
