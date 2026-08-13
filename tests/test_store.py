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


class TestPeriodQuery:
    """按事件真实时间查区间，供周报月报用。

    与日报的口径不同：日报按 first_seen_run（本次新看到的）判定以保证
    不漏报；周报问的是"上周发生了什么"，那是时间概念，必须按 event_time。
    两者混用会让七天日报之和对不上周报。
    """

    def _ev(self, key, when, kind="commit", author="alice", repo="r1"):
        return Event(kind=kind, repo=repo,
                     number=None if kind == "commit" else int(key[-2:]),
                     sha=key if kind == "commit" else None,
                     title=f"t{key}", url=f"https://x/{key}",
                     author_login=author, event_time=when,
                     state="merged" if kind == "pr" else None)

    def test_filters_by_event_time_not_first_seen(self, store):
        run = store.begin_run()
        store.upsert_events([
            self._ev("a1", "2026-08-03T10:00:00Z"),   # 区间前
            self._ev("a2", "2026-08-05T10:00:00Z"),   # 区间内
            self._ev("a3", "2026-08-12T10:00:00Z"),   # 区间后
        ], run)
        store.finish_run(run, repos_processed=1)

        got = store.events_between("2026-08-04T00:00:00Z",
                                   "2026-08-11T00:00:00Z")
        assert [e.sha for e in got] == ["a2"]

    def test_upper_bound_is_exclusive(self, store):
        run = store.begin_run()
        store.upsert_events([self._ev("b1", "2026-08-11T00:00:00Z")], run)
        store.finish_run(run, repos_processed=1)
        got = store.events_between("2026-08-04T00:00:00Z",
                                   "2026-08-11T00:00:00Z")
        assert got == []

    def test_empty_range_returns_empty(self, store):
        assert store.events_between("2026-01-01T00:00:00Z",
                                    "2026-01-02T00:00:00Z") == []


# ---- 抓取失败追踪 ----


def _fetch_run(store, failures=(), repos_processed=38):
    """模拟一次真正跑了采集的运行。"""
    rid = store.begin_run()
    store.finish_run(rid, repos_processed=repos_processed)
    store.record_failures(rid, {n: "连接超时" for n in failures})
    return rid


def test_consecutive_failures_accumulates(store):
    """同一仓库连续多次采集失败，计数累加。"""
    for _ in range(3):
        _fetch_run(store, failures=["abacus-develop"])
    assert store.consecutive_failures() == {"abacus-develop": 3}


def test_consecutive_failures_resets_after_success(store):
    """中间成功一次，连续计数归零。"""
    _fetch_run(store, failures=["abacus-develop"])
    _fetch_run(store, failures=[])                 # 这次成功了
    _fetch_run(store, failures=["abacus-develop"])
    assert store.consecutive_failures() == {"abacus-develop": 1}


def test_consecutive_failures_ignores_notify_runs(store):
    """推送步骤（repos_processed=0）不参与计数。

    拆分模式下库里过半数 run 是推送步骤，若算进分母，
    连续失败计数会被稀释成 1。
    """
    _fetch_run(store, failures=["abacus-develop"])
    rid = store.begin_run()                        # --only-notify 那步
    store.finish_run(rid, repos_processed=0)
    _fetch_run(store, failures=["abacus-develop"])
    assert store.consecutive_failures() == {"abacus-develop": 2}


def test_consecutive_failures_excludes_recovered(store):
    """最近一次采集已成功的仓库不出现在结果里。"""
    _fetch_run(store, failures=["deepmd-kit", "abacus-develop"])
    _fetch_run(store, failures=["abacus-develop"])
    assert store.consecutive_failures() == {"abacus-develop": 2}


def test_record_failures_is_idempotent(store):
    """同一 run 重复写不炸，也不会把计数翻倍。"""
    rid = store.begin_run()
    store.finish_run(rid, repos_processed=38)
    store.record_failures(rid, {"tiny": "错误一"})
    store.record_failures(rid, {"tiny": "错误二"})
    assert store.consecutive_failures() == {"tiny": 1}


def test_no_failures_means_empty(store):
    """从没失败过时返回空字典，而不是报错。"""
    _fetch_run(store, failures=[])
    assert store.consecutive_failures() == {}


def test_total_outage_still_counts(store):
    """所有仓库都失败时 repos_processed=0，但仍要算作一次采集。

    这正是最该告警的情况：若只认 repos_processed>0，全军覆没那几天
    会被当成"没跑过采集"，连续计数永远起不来。
    """
    for _ in range(3):
        rid = store.begin_run()
        store.finish_run(rid, repos_processed=0)
        store.record_failures(rid, {"tiny": "全挂了"})
    assert store.consecutive_failures() == {"tiny": 3}


def test_notify_only_run_does_not_break_streak(store):
    """--only-notify 那步既没处理仓库也没失败记录，不该打断连续计数。"""
    rid = store.begin_run()
    store.finish_run(rid, repos_processed=0)
    store.record_failures(rid, {"tiny": "挂了"})
    notify = store.begin_run()               # 推送步骤
    store.finish_run(notify, repos_processed=0)
    rid = store.begin_run()
    store.finish_run(rid, repos_processed=0)
    store.record_failures(rid, {"tiny": "又挂了"})
    assert store.consecutive_failures() == {"tiny": 2}


# ---- 按窗口取事件（日报口径） ----


def test_window_uses_ingest_time_not_event_time(store):
    """窗口切的是入库时间，不是 git 时间。

    这是日报口径的要害：本地攒两周才 push 的提交，author date 落在两周前，
    按 git 时间切窗会让它永远进不了任何一天的日报。
    """
    rid = store.begin_run()
    old = Event(kind="commit", repo="tiny", number=None, sha="old1",
                title="两周前写的代码", url="https://x/c/old1",
                author_login="alice",
                event_time="2026-07-30T00:00:00Z", state=None)
    store.upsert_events([old], rid)
    store.finish_run(rid, repos_processed=1)

    row = store.conn.execute(
        "SELECT first_seen_at FROM events WHERE sha='old1'").fetchone()
    seen = row["first_seen_at"]
    # 用入库时刻左右各留一秒作窗口
    got = store.pending_in_window(seen[:19], "2099-01-01T00:00:00+00:00")
    assert [e.sha for e in got.new_events] == ["old1"], \
        "按入库时间应取到；若按 event_time 切窗这条会永远漏掉"


def test_window_excludes_outside_events(store):
    rid = store.begin_run()
    store.upsert_events([_commit(sha="in1")], rid)
    store.finish_run(rid, repos_processed=1)
    got = store.pending_in_window("2020-01-01T00:00:00+00:00",
                                  "2020-01-02T00:00:00+00:00")
    assert got.new_events == []


def test_window_includes_state_changes(store):
    """状态变更（PR 合并等）也要按窗口切，不能只管新事件。"""
    rid = store.begin_run()
    store.upsert_events([_pr(number=7, state="open")], rid)
    store.finish_run(rid, repos_processed=1)
    rid2 = store.begin_run()
    store.upsert_events([_pr(number=7, state="merged")], rid2)
    store.finish_run(rid2, repos_processed=1)

    row = store.conn.execute(
        "SELECT changed_at FROM event_state_changes LIMIT 1").fetchone()
    got = store.pending_in_window(row["changed_at"][:19],
                                  "2099-01-01T00:00:00+00:00")
    assert len(got.state_changes) == 1
    assert got.state_changes[0].new_state == "merged"


# ---- 迁移 ----


def test_migration_is_idempotent(store):
    """跑第二次不重复回填、不报错。"""
    rid = store.begin_run()
    store.upsert_events([_commit(sha="m1")], rid)
    store.finish_run(rid, repos_processed=1)
    before = store.conn.execute(
        "SELECT first_seen_at FROM events WHERE sha='m1'").fetchone()[0]
    store.init_schema()
    after = store.conn.execute(
        "SELECT first_seen_at FROM events WHERE sha='m1'").fetchone()[0]
    assert before == after


def test_migration_backfills_from_run(store):
    """旧行（first_seen_at 为空）从 runs.started_at 回填。"""
    rid = store.begin_run()
    store.upsert_events([_commit(sha="b1")], rid)
    store.finish_run(rid, repos_processed=1)
    store.conn.execute("UPDATE events SET first_seen_at=NULL")
    store.conn.commit()
    store.init_schema()
    started = store.conn.execute(
        "SELECT started_at FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
    filled = store.conn.execute(
        "SELECT first_seen_at FROM events WHERE sha='b1'").fetchone()[0]
    assert filled == started


# ---- 数据缺失提醒的判定 ----


def test_missing_cleared_by_success_in_window(store):
    """窗口内补跑成功，提醒就该消失。"""
    rid = store.begin_run()
    store.finish_run(rid, repos_processed=0)
    store.record_failures(rid, {"tiny": "挂了"})
    rid2 = store.begin_run()
    store.finish_run(rid2, repos_processed=1)
    store.record_successes(rid2, ["tiny"])
    assert store.repos_missing_since("2020-01-01T00:00:00+00:00") == {}


def test_success_outside_window_does_not_clear(store):
    """窗口外补跑不算数 —— 那些数据要等下一天的日报，今天仍是缺的。

    若这里撤掉提醒，今天的日报既不提醒、也没有那个仓库的内容，
    比不改还隐蔽。
    """
    rid = store.begin_run()
    store.finish_run(rid, repos_processed=0)
    store.record_failures(rid, {"tiny": "挂了"})
    rid2 = store.begin_run()
    store.finish_run(rid2, repos_processed=1)
    store.record_successes(rid2, ["tiny"])
    # 显式设定两个时刻，不依赖执行快慢（同一微秒内跑完是常事）
    store.conn.execute(
        "UPDATE repo_failures SET failed_at='2026-08-13T01:00:00+00:00'")
    store.conn.execute(
        "UPDATE repo_successes SET succeeded_at='2026-08-13T05:00:00+00:00'")
    store.conn.commit()

    # 窗口在失败之后、补跑成功之前就关了
    assert store.repos_missing_since("2026-08-13T00:00:00+00:00",
                                     "2026-08-13T02:00:00+00:00") \
        == {"tiny": 1}


# ---- 建库基线：只挡首次录入的历史积压 ----


def test_baseline_blocks_bootstrap_backlog(store):
    """首次建库那一万多条不该被当成当天动态推出去。"""
    rid = store.begin_run()
    store.upsert_events([_commit(sha=f"h{i}") for i in range(5)], rid)
    store.finish_run(rid, repos_processed=1)
    store.mark_notify_baseline()

    got = store.pending_in_window("2020-01-01T00:00:00+00:00",
                                  "2099-01-01T00:00:00+00:00")
    assert got.new_events == [], "基线之前入库的一律不进日报"


def test_events_after_baseline_still_shown(store):
    """基线之后入库的照常进日报。"""
    rid = store.begin_run()
    store.upsert_events([_commit(sha="old")], rid)
    store.finish_run(rid, repos_processed=1)
    store.mark_notify_baseline()

    rid2 = store.begin_run()
    store.upsert_events([_commit(sha="new")], rid2)
    store.finish_run(rid2, repos_processed=1)
    # 内存库同一微秒内跑完，时间戳会撞在一起；把 new 显式推到基线之后。
    # 不能写死某个钟点 —— 基线时刻是"现在"，硬编码的时刻跑到下午就失效了。
    from datetime import datetime, timedelta
    later = (datetime.fromisoformat(store._baseline_at())
             + timedelta(seconds=1)).isoformat()
    store.conn.execute(
        "UPDATE events SET first_seen_at=? WHERE sha='new'", (later,))
    store.conn.commit()

    got = store.pending_in_window("2020-01-01T00:00:00+00:00",
                                  "2099-01-01T00:00:00+00:00")
    assert [e.sha for e in got.new_events] == ["new"]


def test_ordinary_push_does_not_become_baseline(store):
    """普通推送不该成为下限 —— 否则同一天推两次内容会不一样。

    这正是窗口锚定要保证的：同一天的日报，推几次都一样。
    """
    rid = store.begin_run()
    store.upsert_events([_commit(sha="x1")], rid)
    store.finish_run(rid, repos_processed=1)
    store.mark_notified(rid, True)          # 普通推送成功

    win = ("2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")
    first = store.pending_in_window(*win)
    second = store.pending_in_window(*win)
    assert [e.sha for e in first.new_events] == ["x1"]
    assert [e.sha for e in second.new_events] == ["x1"], \
        "重复取同一窗口必须得到相同内容"
