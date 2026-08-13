"""事件历史存储：SQLite 读写、去重、跨运行差集。

为什么是 SQLite 而不是追加 CSV：要按 event_key 去重，CSV 每次都得全表
扫；且以后网页看板要按时间范围查询，SQL 天然合适。SQLite 单文件、零
部署、标准库自带，不引入任何新依赖。

本模块不知道事件从哪里来，也不知道要推给谁 —— 它只管存和查。上游是
events.py，下游是 notify/。这个边界是为了以后加网页看板或多维表格时
不用改这里。
"""
import json
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

-- 每次推送的战报存档。可溯源，也能拿出来重发（测试通道时不必等真实增量）。
-- 丢失无所谓：这是派生数据，重建不了也不影响统计与增量判定。
CREATE TABLE IF NOT EXISTS digests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    created_at TEXT NOT NULL,      -- UTC ISO
    payload    TEXT NOT NULL,      -- 完整卡片 JSON，原样可重发
    sent       INTEGER             -- NULL=未发（dry-run）0=失败 1=成功
);

CREATE INDEX IF NOT EXISTS idx_digests_run ON digests(run_id);

CREATE INDEX IF NOT EXISTS idx_changes_run ON event_state_changes(run_id);

-- 哪个仓库在哪次运行抓取失败。存在的理由：没有这张表，"连续挂 30 天"
-- 和"挂了一天"在记录里长得一模一样 —— 两者都只打印一行"N 个仓库失败"。
-- first_seen_run 的补齐机制保证只要哪天成功就不丢数据，但前提是"总有
-- 一天会成功"；仓库改名、删除、权限变更时永远不会自愈，必须有人去看。
CREATE TABLE IF NOT EXISTS repo_failures (
    run_id    INTEGER NOT NULL REFERENCES runs(run_id),
    repo      TEXT NOT NULL,
    error     TEXT,
    failed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, repo)
);

CREATE INDEX IF NOT EXISTS idx_repo_failures_repo ON repo_failures(repo);
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

    # ---- 抓取失败追踪 ----

    def record_failures(self, run_id: int, failures: dict) -> None:
        """记下本次运行有哪些仓库没抓到。failures 是 {仓库名: 错误摘要}。

        只在真正跑了采集的那步调用。拆分模式下 --only-notify 不写，
        否则会往表里插一堆空记录，把连续计数搅乱。
        """
        if not failures:
            return
        now = _now()
        self.conn.executemany(
            "INSERT OR REPLACE INTO repo_failures "
            "(run_id, repo, error, failed_at) VALUES (?,?,?,?)",
            [(run_id, name, str(err)[:500], now)
             for name, err in sorted(failures.items())])
        self.conn.commit()

    def _fetch_run_ids(self, limit: int = 30) -> list:
        """最近若干次真正跑了采集的 run，新的在前。

        判定标准是"这次运行有没有碰仓库"，而不是"有没有成功处理仓库"：
        repos_processed>0 或在 repo_failures 里留了记录，两者取并集。
        只看 repos_processed>0 会漏掉全军覆没的那次 —— 38 个仓库全挂时
        它正好是 0，而那恰恰是最该告警的情况。

        为什么要滤掉推送 run：日常是拆分模式，--fetch 与 --only-notify
        各占一个 run，库里过半数 run 是推送步骤、一个仓库都没碰。若把
        它们算进连续失败的分母，计数会被稀释 —— 昨天挂了、今天推送步骤
        "没挂"，连续数就断了。
        """
        rows = self.conn.execute(
            "SELECT run_id FROM runs WHERE finished_at IS NOT NULL "
            "AND (repos_processed > 0 OR run_id IN "
            "     (SELECT DISTINCT run_id FROM repo_failures)) "
            "ORDER BY run_id DESC LIMIT ?",
            (limit,)).fetchall()
        return [r["run_id"] for r in rows]

    def consecutive_failures(self) -> dict:
        """每个仓库最近连续失败了几次采集。返回 {仓库名: 连续次数}。

        从最近一次采集 run 往回数，一旦某次采集里它没出现（说明那次
        成功了）就归零。只返回当前仍在连续失败中的仓库 —— 早就修好的
        历史故障不该出现在今天的日报里。
        """
        run_ids = self._fetch_run_ids()
        if not run_ids:
            return {}

        rows = self.conn.execute(
            "SELECT run_id, repo FROM repo_failures WHERE run_id IN "
            f"({','.join('?' * len(run_ids))})", run_ids).fetchall()
        by_run = {}
        for r in rows:
            by_run.setdefault(r["run_id"], set()).add(r["repo"])

        # 最近一次采集就没失败的仓库，连续数为 0，不必再往回数
        streaks = {}
        for name in by_run.get(run_ids[0], set()):
            n = 0
            for rid in run_ids:
                if name in by_run.get(rid, set()):
                    n += 1
                else:
                    break
            streaks[name] = n
        return streaks

    # ---- 战报存档 ----

    def save_digest(self, run_id: int, payload: dict,
                    sent: Optional[bool] = None) -> int:
        """存一份战报，返回自增 id。

        存的是完整卡片 JSON 而非渲染前的结构：这样取出来可以原样重发，
        不依赖当时的代码版本 —— 渲染逻辑改了，旧存档照样发得出去。
        """
        cur = self.conn.execute(
            "INSERT INTO digests (run_id, created_at, payload, sent) "
            "VALUES (?,?,?,?)",
            (run_id, _now(), json.dumps(payload, ensure_ascii=False),
             None if sent is None else int(sent)))
        self.conn.commit()
        return cur.lastrowid

    def mark_digest_sent(self, digest_id: int, ok: bool) -> None:
        self.conn.execute("UPDATE digests SET sent=? WHERE id=?",
                          (1 if ok else 0, digest_id))
        self.conn.commit()

    def latest_digest(self) -> Optional[dict]:
        """最近一份战报的卡片 JSON。库里没有时返回 None。"""
        row = self.conn.execute(
            "SELECT payload FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            return None

    def list_digests(self, limit: int = 20) -> list:
        """战报存档列表，新的在前。用于排查"某天到底发了什么"。"""
        rows = self.conn.execute(
            "SELECT id, run_id, created_at, sent FROM digests "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_notify_baseline(self) -> int:
        """把最后一次跑完的运行标记为已推送，划掉此前的全部积压。

        存在的理由：is_first_run 只挡住第一次运行，而 pending_since_last_notify
        查的是"上次成功推送之后的全部"。从没成功推送过时它会把建库时录入的
        几千条历史一次性交出来 —— 而积压又只有推送成功才清空，形成死循环。

        典型用法是首次部署：先跑一次建库，用这个方法定基线，之后每天跑
        才是真正意义的增量。返回被标记的 run_id，库里没有跑完的运行时返回 0。
        """
        row = self.conn.execute(
            "SELECT MAX(run_id) FROM runs WHERE finished_at IS NOT NULL"
        ).fetchone()
        run_id = row[0] or 0
        if run_id:
            self.conn.execute("UPDATE runs SET notified=1 WHERE run_id=?",
                              (run_id,))
            self.conn.commit()
        return run_id

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

    def events_between(self, start: str, end: str) -> list:
        """按事件真实时间取区间内的事件，半开区间 [start, end)。

        与 pending_since_last_notify 的口径**不同**，这是有意的：
        日报按 first_seen_run 判定以保证不漏报（author date 是代码写成
        时间，本地攒两周才推的提交按事件时间会永远漏掉）；而周报月报问的
        是"上周/上月发生了什么"，那是时间概念，只能按 event_time。

        两者混用会让七天日报之和对不上周报，那种数字打架最难解释。

        参数是 UTC ISO 字符串。周月边界按东八区算好再转 UTC 传进来 ——
        库里一律存 UTC，时区换算是调用方的事。
        """
        rows = self.conn.execute(
            "SELECT * FROM events WHERE event_time >= ? AND event_time < ? "
            "ORDER BY event_time", (start, end)).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> Event:
        return Event(
            kind=r["kind"], repo=r["repo"], number=r["number"], sha=r["sha"],
            title=r["title"], url=r["url"], author_login=r["author_login"],
            event_time=r["event_time"], state=r["state"],
        )
