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
        assert "无新增" in card.render_text(build(UpsertResult([], [])))

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
