"""事件存储层测试。全部用内存库，不落盘。"""
import sqlite3

import pytest

from contributors.events import Event
from contributors.store import EventStore


@pytest.fixture
def store():
    s = EventStore(":memory:")
    s.init_schema()
    yield s
    s.close()


def _pr(repo="deepmd-kit", number=1, state="open", title="示例 PR"):
    return Event(
        kind="pr", repo=repo, number=number, sha=None, title=title,
        url=f"https://github.com/deepmodeling/{repo}/pull/{number}",
        author_login="alice", event_time="2026-08-10T00:00:00+00:00",
        state=state,
    )


def _commit(repo="deepmd-kit", sha="abc123", title="修复某问题"):
    return Event(
        kind="commit", repo=repo, number=None, sha=sha, title=title,
        url=f"https://github.com/deepmodeling/{repo}/commit/{sha}",
        author_login=None, event_time="2026-08-10T00:00:00+00:00",
        state=None,
    )


class TestSchema:
    def test_init_schema_creates_tables(self, store):
        names = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"events", "runs", "event_state_changes"} <= names

    def test_init_schema_is_idempotent(self, store):
        store.init_schema()  # 二次调用不得报错
        assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


class TestUpsertDedup:
    def test_new_events_are_reported_as_new(self, store):
        run = store.begin_run(since="2026-01-01", until="2026-08-10")
        result = store.upsert_events([_pr(), _commit()], run)
        assert len(result.new_events) == 2

    def test_same_event_twice_is_not_new_again(self, store):
        r1 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr()], r1)
        store.finish_run(r1, repos_processed=1)

        r2 = store.begin_run(since="2026-01-01", until="2026-08-11")
        result = store.upsert_events([_pr()], r2)
        assert result.new_events == []

    def test_first_seen_run_records_the_first_run_only(self, store):
        r1 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr()], r1)
        store.finish_run(r1, repos_processed=1)

        r2 = store.begin_run(since="2026-01-01", until="2026-08-11")
        store.upsert_events([_pr()], r2)

        seen = store.conn.execute(
            "SELECT first_seen_run FROM events").fetchone()[0]
        assert seen == r1

    def test_same_sha_in_different_repos_are_distinct_events(self, store):
        """fork 场景：同一 SHA 出现在两个仓库，是两条事件。"""
        run = store.begin_run(since="2026-01-01", until="2026-08-10")
        result = store.upsert_events(
            [_commit(repo="a", sha="ff01"), _commit(repo="b", sha="ff01")], run)
        assert len(result.new_events) == 2


class TestStateChanges:
    def test_open_to_merged_is_recorded(self, store):
        r1 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(state="open")], r1)
        store.finish_run(r1, repos_processed=1)

        r2 = store.begin_run(since="2026-01-01", until="2026-08-11")
        result = store.upsert_events([_pr(state="merged")], r2)

        assert len(result.state_changes) == 1
        chg = result.state_changes[0]
        assert chg.old_state == "open"
        assert chg.new_state == "merged"

    def test_state_is_updated_on_the_event_row(self, store):
        r1 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(state="open")], r1)
        r2 = store.begin_run(since="2026-01-01", until="2026-08-11")
        store.upsert_events([_pr(state="merged")], r2)

        state = store.conn.execute("SELECT state FROM events").fetchone()[0]
        assert state == "merged"

    def test_unchanged_state_produces_no_change_record(self, store):
        """这是'每天重复推送同样内容'这个 bug 的唯一防线。"""
        r1 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(state="merged")], r1)
        store.finish_run(r1, repos_processed=1)

        r2 = store.begin_run(since="2026-01-01", until="2026-08-11")
        result = store.upsert_events([_pr(state="merged")], r2)

        assert result.state_changes == []
        n = store.conn.execute(
            "SELECT COUNT(*) FROM event_state_changes").fetchone()[0]
        assert n == 0

    def test_newly_created_merged_pr_is_new_not_a_change(self, store):
        """首次就以 merged 出现的 PR 算新增，不算状态变更。"""
        run = store.begin_run(since="2026-01-01", until="2026-08-10")
        result = store.upsert_events([_pr(state="merged")], run)
        assert len(result.new_events) == 1
        assert result.state_changes == []


class TestPendingSince:
    """digest 要查的是'上次成功推送之后的全部变化'，不是'本次 run'。"""

    def test_returns_changes_from_runs_after_last_notified(self, store):
        r1 = store.begin_run(since="2026-01-01", until="2026-08-09")
        store.upsert_events([_pr(number=1)], r1)
        store.finish_run(r1, repos_processed=1)
        store.mark_notified(r1, True)

        r2 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(number=2)], r2)
        store.finish_run(r2, repos_processed=1)

        pending = store.pending_since_last_notify()
        numbers = {e.number for e in pending.new_events}
        assert numbers == {2}

    def test_failed_notify_accumulates_into_next_run(self, store):
        """昨天推送失败，今天应把两天的内容一起推出去。"""
        r1 = store.begin_run(since="2026-01-01", until="2026-08-09")
        store.upsert_events([_pr(number=1)], r1)
        store.finish_run(r1, repos_processed=1)
        store.mark_notified(r1, False)

        r2 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(number=2)], r2)
        store.finish_run(r2, repos_processed=1)

        pending = store.pending_since_last_notify()
        numbers = {e.number for e in pending.new_events}
        assert numbers == {1, 2}

    def test_includes_state_changes(self, store):
        r1 = store.begin_run(since="2026-01-01", until="2026-08-09")
        store.upsert_events([_pr(number=7, state="open")], r1)
        store.finish_run(r1, repos_processed=1)
        store.mark_notified(r1, True)

        r2 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(number=7, state="merged")], r2)
        store.finish_run(r2, repos_processed=1)

        pending = store.pending_since_last_notify()
        assert pending.new_events == []
        assert len(pending.state_changes) == 1
        assert pending.state_changes[0].new_state == "merged"

    def test_no_successful_run_yet_means_first_run(self, store):
        assert store.is_first_run() is True

    def test_after_a_finished_run_it_is_no_longer_first(self, store):
        r1 = store.begin_run(since="2026-01-01", until="2026-08-09")
        store.finish_run(r1, repos_processed=1)
        assert store.is_first_run() is False


class TestRuns:
    def test_finish_run_records_counts(self, store):
        run = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(), _commit()], run)
        store.finish_run(run, repos_processed=34)

        row = store.conn.execute(
            "SELECT repos_processed, new_events, status FROM runs "
            "WHERE run_id=?", (run,)).fetchone()
        assert row[0] == 34
        assert row[1] == 2
        assert row[2] == "ok"

    def test_runs_since_last_notify_counts_consecutive_failures(self, store):
        for _ in range(3):
            r = store.begin_run(since="2026-01-01", until="2026-08-10")
            store.finish_run(r, repos_processed=1)
            store.mark_notified(r, False)
        assert store.runs_since_last_notify() == 3


class TestCorruption:
    def test_corrupt_db_is_quarantined_and_rebuilt(self, tmp_path):
        db = tmp_path / "events.db"
        db.write_bytes(b"this is not a sqlite file at all, not even close")

        s = EventStore(str(db))
        s.init_schema()

        # 库可用
        run = s.begin_run(since="2026-01-01", until="2026-08-10")
        assert s.upsert_events([_pr()], run).new_events
        s.close()

        # 损坏文件被留存而非删除
        quarantined = list(tmp_path.glob("events.db.corrupt-*"))
        assert len(quarantined) == 1

    def test_healthy_db_is_not_quarantined(self, tmp_path):
        db = tmp_path / "events.db"
        s = EventStore(str(db))
        s.init_schema()
        run = s.begin_run(since="2026-01-01", until="2026-08-10")
        s.upsert_events([_pr()], run)
        s.finish_run(run, repos_processed=1)
        s.close()

        s2 = EventStore(str(db))
        s2.init_schema()
        assert s2.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        s2.close()
        assert list(tmp_path.glob("events.db.corrupt-*")) == []


class TestNotifyBaseline:
    """把当前进度标记为已推送基线，划掉历史积压。

    背景：is_first_run 只挡住第一次运行。第二次跑时它返回 False，推送流程
    照常走，而 pending_since_last_notify 查的是"上次成功推送之后的全部"——
    从没成功推送过时 _last_notified_run 返回 0，于是把建库时录入的几千条
    历史全当成增量推出去。积压只有推送成功才清空，形成死循环。

    实测触发过：种子运行录入 1606 条，第二次跑的卡片把今年一整年的
    597 次提交 / 573 个 PR 合并当成"自上次汇报以来"。
    """

    def test_baseline_clears_backlog(self, store):
        run = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(number=1), _commit(sha="a1")], run)
        store.finish_run(run, repos_processed=1)

        assert len(store.pending_since_last_notify().new_events) == 2
        store.mark_notify_baseline()
        assert store.pending_since_last_notify().new_events == []

    def test_events_after_baseline_still_pending(self, store):
        run1 = store.begin_run(since="2026-01-01", until="2026-08-10")
        store.upsert_events([_pr(number=1)], run1)
        store.finish_run(run1, repos_processed=1)
        store.mark_notify_baseline()

        run2 = store.begin_run(since="2026-01-01", until="2026-08-11")
        store.upsert_events([_pr(number=2)], run2)
        store.finish_run(run2, repos_processed=1)

        pending = store.pending_since_last_notify()
        assert [e.number for e in pending.new_events] == [2]

    def test_baseline_on_empty_db_is_harmless(self, store):
        """库里没有任何运行时调用不应报错。"""
        store.mark_notify_baseline()
        assert store.pending_since_last_notify().new_events == []


class TestDigestArchive:
    """战报存档：可溯源，也能取出来重发。

    存完整卡片 JSON 而非渲染前的结构，这样旧存档不依赖当时的代码版本 ——
    渲染逻辑改了照样发得出去。
    """

    def _payload(self, n=1):
        return {"msg_type": "interactive",
                "card": {"header": {"title": {"content": f"日报 {n}"}}}}

    def test_saved_digest_can_be_read_back(self, store):
        run = store.begin_run(since="2026-08-10", until="2026-08-11")
        store.finish_run(run, repos_processed=1)
        store.save_digest(run, self._payload(1))
        assert store.latest_digest() == self._payload(1)

    def test_latest_returns_most_recent(self, store):
        run = store.begin_run()
        store.finish_run(run, repos_processed=1)
        store.save_digest(run, self._payload(1))
        store.save_digest(run, self._payload(2))
        assert store.latest_digest()["card"]["header"]["title"]["content"] == "日报 2"

    def test_latest_on_empty_db_is_none(self, store):
        assert store.latest_digest() is None

    def test_sent_flag_is_recorded(self, store):
        run = store.begin_run()
        store.finish_run(run, repos_processed=1)
        did = store.save_digest(run, self._payload(), sent=None)
        store.mark_digest_sent(did, True)
        assert store.list_digests()[0]["sent"] == 1

    def test_list_digests_newest_first(self, store):
        run = store.begin_run()
        store.finish_run(run, repos_processed=1)
        store.save_digest(run, self._payload(1))
        store.save_digest(run, self._payload(2))
        ids = [d["id"] for d in store.list_digests()]
        assert ids == sorted(ids, reverse=True)

    def test_schema_added_to_existing_db(self, tmp_path):
        """老库没有 digests 表，init_schema 应补建而非报错。

        CREATE TABLE IF NOT EXISTS 每次都跑，所以升级是自动的 ——
        用户的库里已经有 1900 多条真实事件，不能要求重建。
        """
        db = tmp_path / "events.db"
        s = EventStore(str(db))
        s.init_schema()
        s.conn.execute("DROP TABLE digests")
        s.conn.commit()
        s.close()

        s2 = EventStore(str(db))
        s2.init_schema()          # 应把表补回来
        run = s2.begin_run()
        s2.finish_run(run, repos_processed=1)
        s2.save_digest(run, {"a": 1})
        assert s2.latest_digest() == {"a": 1}
        s2.close()
