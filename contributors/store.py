"""事件历史存储：SQLite 读写、去重、跨运行差集。

为什么是 SQLite 而不是追加 CSV：要按 event_key 去重，CSV 每次都得全表
扫；且以后网页看板要按时间范围查询，SQL 天然合适。SQLite 单文件、零
部署、标准库自带，不引入任何新依赖。

本模块不知道事件从哪里来，也不知道要推给谁 —— 它只管存和查。上游是
events.py，下游是 notify/。这个边界是为了以后加网页看板或多维表格时
不用改这里。
"""
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .events import Event, StateChange

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    since           TEXT,
    until           TEXT,
    repos_processed INTEGER DEFAULT 0,
    new_events      INTEGER DEFAULT 0,
    notified        INTEGER,        -- NULL=未尝试 0=失败 1=成功
    status          TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS events (
    event_key      TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    repo           TEXT NOT NULL,
    number         INTEGER,
    sha            TEXT,
    title          TEXT,
    url            TEXT,
    author_login   TEXT,
    event_time     TEXT,
    state          TEXT,
    first_seen_run INTEGER NOT NULL REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_events_first_seen ON events(first_seen_run);
CREATE INDEX IF NOT EXISTS idx_events_repo ON events(repo);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time);

CREATE TABLE IF NOT EXISTS event_state_changes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL REFERENCES events(event_key),
    old_state TEXT,
    new_state TEXT,
    run_id    INTEGER NOT NULL REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_changes_run ON event_state_changes(run_id);
"""


class UpsertResult:
    """一次写入的结果：哪些是新的、哪些状态变了。"""

    def __init__(self, new_events: list, state_changes: list):
        self.new_events = new_events
        self.state_changes = state_changes

    @property
    def is_empty(self) -> bool:
        return not self.new_events and not self.state_changes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._quarantine_if_corrupt()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _quarantine_if_corrupt(self) -> None:
        """损坏的库改名留存，重建空库。

        断电或磁盘满都可能损坏文件。统计是主产物，不能因为附带的事件库
        坏了就跑不动。改名而不删除 —— 万一还能救。
        """
        p = Path(self.path)
        if not p.exists() or p.stat().st_size == 0:
            return
        try:
            probe = sqlite3.connect(self.path)
            try:
                ok = probe.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                probe.close()
            if ok == "ok":
                return
            reason = f"integrity_check={ok}"
        except sqlite3.DatabaseError as exc:
            reason = str(exc)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = p.with_name(f"{p.name}.corrupt-{stamp}")
        p.rename(dest)
        log.error("事件库损坏（%s），已改名为 %s 并重建空库", reason, dest.name)

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 运行记录 ----

    def begin_run(self, since: str = "", until: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, since, until) VALUES (?,?,?)",
            (_now(), since, until))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, repos_processed: int = 0,
                   status: str = "ok") -> None:
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE first_seen_run=?",
            (run_id,)).fetchone()[0]
        self.conn.execute(
            "UPDATE runs SET finished_at=?, repos_processed=?, new_events=?, "
            "status=? WHERE run_id=?",
            (_now(), repos_processed, n, status, run_id))
        self.conn.commit()

    def mark_notified(self, run_id: int, ok: bool) -> None:
        self.conn.execute("UPDATE runs SET notified=? WHERE run_id=?",
                          (1 if ok else 0, run_id))
        self.conn.commit()

    def is_first_run(self) -> bool:
        """库里还没有跑完过的运行。

        首次运行会把今年至今几千条事件全判为新增，推出去毫无意义，
        所以此时只建库不推送。
        """
        row = self.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE finished_at IS NOT NULL"
        ).fetchone()
        return row[0] == 0

    def runs_since_last_notify(self) -> int:
        """距上次成功推送已过去几次运行。用于连续失败告警。"""
        last = self._last_notified_run()
        return self.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id > ? AND finished_at IS NOT NULL",
            (last,)).fetchone()[0]

    def _last_notified_run(self) -> int:
        row = self.conn.execute(
            "SELECT MAX(run_id) FROM runs WHERE notified=1").fetchone()
        return row[0] or 0

    # ---- 事件写入 ----

    def upsert_events(self, events: list, run_id: int) -> UpsertResult:
        """写入事件，返回新增的与状态变了的。

        同一批里 key 重复时以最后一条为准（同一 PR 不会在一批里出现
        两次，但防御性处理）。
        """
        new_events, changes = [], []

        for ev in events:
            row = self.conn.execute(
                "SELECT state FROM events WHERE event_key=?", (ev.key,)
            ).fetchone()

            if row is None:
                self.conn.execute(
                    "INSERT INTO events (event_key, kind, repo, number, sha, "
                    "title, url, author_login, event_time, state, first_seen_run) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ev.key, ev.kind, ev.repo, ev.number, ev.sha, ev.title,
                     ev.url, ev.author_login, ev.event_time, ev.state, run_id))
                new_events.append(ev)
                continue

            old = row["state"]
            if old == ev.state:
                # 状态没变就什么都不记。这是"每天重复推送同样内容"
                # 这个 bug 的唯一防线。
                continue

            self.conn.execute(
                "UPDATE events SET state=?, title=?, url=? WHERE event_key=?",
                (ev.state, ev.title, ev.url, ev.key))
            self.conn.execute(
                "INSERT INTO event_state_changes (event_key, old_state, "
                "new_state, run_id) VALUES (?,?,?,?)",
                (ev.key, old, ev.state, run_id))
            changes.append(StateChange(
                event_key=ev.key, kind=ev.kind, repo=ev.repo,
                number=ev.number, title=ev.title, url=ev.url,
                old_state=old, new_state=ev.state,
                author_login=ev.author_login))

        self.conn.commit()
        return UpsertResult(new_events, changes)

    # ---- 查询 ----

    def pending_since_last_notify(self) -> UpsertResult:
        """上次成功推送之后累积的全部变化。

        查的不是"本次 run"而是"上次成功推送之后" —— 这样昨天推送失败，
        今天会把两天的内容一起推出去，不需要额外的重试队列。
        """
        last = self._last_notified_run()

        rows = self.conn.execute(
            "SELECT * FROM events WHERE first_seen_run > ? ORDER BY event_time",
            (last,)).fetchall()
        new_events = [self._row_to_event(r) for r in rows]

        rows = self.conn.execute(
            "SELECT c.event_key, c.old_state, c.new_state, "
            "       e.kind, e.repo, e.number, e.title, e.url, e.author_login "
            "FROM event_state_changes c JOIN events e USING (event_key) "
            "WHERE c.run_id > ? ORDER BY c.id", (last,)).fetchall()
        changes = [StateChange(
            event_key=r["event_key"], kind=r["kind"], repo=r["repo"],
            number=r["number"], title=r["title"], url=r["url"],
            old_state=r["old_state"], new_state=r["new_state"],
            author_login=r["author_login"],
        ) for r in rows]

        return UpsertResult(new_events, changes)

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> Event:
        return Event(
            kind=r["kind"], repo=r["repo"], number=r["number"], sha=r["sha"],
            title=r["title"], url=r["url"], author_login=r["author_login"],
            event_time=r["event_time"], state=r["state"],
        )
