"""看板数据查询：只读 SQLite 连接 + 内存聚合。

不复用 EventStore：它的 __init__ 会 init_schema()（写库、可能 ALTER TABLE）、
_quarantine_if_corrupt()（损坏时改名重建空库），且连接没设
check_same_thread=False。看板是只读的，不该写，也不该跟每天跑的 fetch 任务
抢写锁或触发误判。这里用 sqlite3.connect(..., mode=ro) 另开只读连接，每请求
新建（本地文件开销可忽略）。

event_time 时区格式不统一（commit 混杂 Z 结尾与 +08:00 偏移，PR/issue 全是
Z）。字符串比较排序会排错，所以查出来后一律用 datetime.fromisoformat 解析
成 aware datetime 再比较/排序，不依赖 SQL 层面的字符串边界。
"""
import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

CST = timezone(timedelta(hours=8))

try:
    # 复用日报的排除名单，避免两处各维护一份而慢慢跑偏
    from contributors.repos import EXCLUDED_REPOS
except Exception:  # noqa: BLE001
    # contributors 包依赖 requests，看板本身只用标准库。真缺了也不该起不来，
    # 退回硬编码同一份内容（与 repos.py:EXCLUDED_REPOS 保持一致）
    EXCLUDED_REPOS = {".github"}


@dataclass
class EventRow:
    kind: str
    repo: str
    number: Optional[int]
    sha: Optional[str]
    title: str
    url: str
    author_login: Optional[str]
    event_time: str
    event_dt: datetime
    state: Optional[str]


def _parse_time(iso: str) -> Optional[datetime]:
    """统一解析成 aware UTC datetime。解析失败返回 None（调用方过滤掉）。

    Z 结尾不是 fromisoformat 原生支持的写法（3.11 之前），统一换成 +00:00。
    """
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def open_readonly(path: str) -> sqlite3.Connection:
    """只读打开事件库。文件不存在时 sqlite3 的 mode=ro 会直接报错，符合预期
    （看板没有数据就该显式失败，不该静默建一个空库）。
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def query_events(conn: sqlite3.Connection, start: datetime, end: datetime) -> list:
    """取全表事件，按 Python 侧解析后的 event_time 落在 [start, end) 内的。

    全表拉取而非 SQL WHERE 过滤：event_time 的字符串比较不可靠（时区格式
    不统一），必须解析后再比较。数据量 1.7 万行/9MB，整表读入内存可忽略不计，
    参见规划文档「没有的（要自己写）」一节。
    """
    rows = conn.execute(
        "SELECT kind, repo, number, sha, title, url, author_login, "
        "event_time, state FROM events"
    ).fetchall()
    out = []
    for r in rows:
        if r["repo"] in EXCLUDED_REPOS:
            continue  # .github 是组织元仓库，日报已排除，看板口径保持一致
        dt = _parse_time(r["event_time"])
        if dt is None or not (start <= dt < end):
            continue
        out.append(EventRow(
            kind=r["kind"], repo=r["repo"], number=r["number"], sha=r["sha"],
            title=r["title"], url=r["url"], author_login=r["author_login"],
            event_time=r["event_time"], event_dt=dt, state=r["state"],
        ))
    out.sort(key=lambda e: e.event_dt)
    return out


def parse_range(days: Optional[str], from_: Optional[str], to_: Optional[str],
                now: Optional[datetime] = None) -> tuple:
    """把 URL 参数解析成 UTC 半开区间 [start, end)。

    优先级：from/to > days > 默认 30 天。from/to 是 YYYY-MM-DD（东八区
    自然日，含两端），转换成 UTC 时按东八区日界算，避免比如 to=2026-09-04
    实际只包含到 UTC 前一天 16:00 就把当天事件切掉了。
    """
    ref = now or datetime.now(timezone.utc)

    if from_ or to_:
        end_day = _parse_day(to_) if to_ else ref.astimezone(CST).date()
        start_day = _parse_day(from_) if from_ else end_day - timedelta(days=29)
        start = datetime(start_day.year, start_day.month, start_day.day,
                         tzinfo=CST).astimezone(timezone.utc)
        end = (datetime(end_day.year, end_day.month, end_day.day, tzinfo=CST)
              + timedelta(days=1)).astimezone(timezone.utc)
        return start, end

    n = int(days) if days else 30
    end = ref
    start = end - timedelta(days=n)
    return start, end


def _parse_day(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def summary(events: list) -> dict:
    """KPI 行：总事件数、活跃贡献者、PR 合并率、活跃仓库数。

    活跃贡献者只含 author_login 非空的事件（PR/issue 作者），commit 在
    events.db 里没有作者，规划文档「坑 1」——UI 侧要标注这个口径限制。
    """
    authors = set()
    repos = set()
    pr_created = pr_merged = 0
    for ev in events:
        if ev.author_login:
            authors.add(ev.author_login)
        if ev.repo:
            repos.add(ev.repo)
        if ev.kind == "pr":
            pr_created += 1
            if ev.state == "merged":
                pr_merged += 1
    return {
        "total_events": len(events),
        "active_contributors": len(authors),
        "active_repos": len(repos),
        "pr_created": pr_created,
        "pr_merged": pr_merged,
        "pr_merge_rate": round(pr_merged / pr_created, 3) if pr_created else None,
    }


def repo_breakdown(events: list) -> list:
    """各仓库的 commit/pr/issue 计数，按总量降序。"""
    by_repo: dict = {}
    for ev in events:
        row = by_repo.setdefault(ev.repo, {"repo": ev.repo, "commit": 0, "pr": 0, "issue": 0})
        row[ev.kind] = row.get(ev.kind, 0) + 1
    out = list(by_repo.values())
    out.sort(key=lambda r: -(r["commit"] + r["pr"] + r["issue"]))
    return out


def contributor_ranking(events: list, limit: int = 10) -> list:
    """贡献者排行（只含 PR/Issue 作者，commit 无作者，见模块开头说明）。

    limit <= 0 表示不截断，返回全部——看板的「展开全部」要一次拿到完整名单，
    在前端切片，省得点一次展开再发一趟请求。
    """
    by_author: dict = {}
    for ev in events:
        if not ev.author_login:
            continue
        by_author[ev.author_login] = by_author.get(ev.author_login, 0) + 1
    ranked = sorted(by_author.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit > 0:
        ranked = ranked[:limit]
    return [{"login": login, "count": n} for login, n in ranked]


def timeline(events: list) -> list:
    """按天（东八区自然日）聚合的事件数，喂热力图。"""
    by_day: dict = {}
    for ev in events:
        day = ev.event_dt.astimezone(CST).date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1
    return [{"date": d, "count": n} for d, n in sorted(by_day.items())]


def event_stream(events: list, kind: Optional[str] = None,
                 repo: Optional[str] = None, limit: int = 50) -> list:
    """事件流明细，新的在前，可按类型/仓库筛选。"""
    filtered = events
    if kind:
        filtered = [e for e in filtered if e.kind == kind]
    if repo:
        filtered = [e for e in filtered if e.repo == repo]
    filtered = sorted(filtered, key=lambda e: e.event_dt, reverse=True)[:limit]
    return [{
        "kind": e.kind, "repo": e.repo, "number": e.number, "title": e.title,
        "url": e.url, "author_login": e.author_login, "event_time": e.event_time,
        "state": e.state,
    } for e in filtered]


def read_yearly_csv(path: str) -> list:
    """读年度全量排行（output/daily/summary.csv），含 commit 作者。

    这份 CSV 每次运行整表重写，固定"过去一年"窗口，没有时间参数可筛选
    （规划文档「取数决策」）。repo 字段为空表示这是跨仓库汇总行
    （output.py:summarize 的产出），看板只需要这些汇总行。
    """
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("repo"):
                continue
            rows.append(row)
    return rows


def repo_list(conn: sqlite3.Connection) -> list:
    """下拉框的仓库选项：全表 distinct repo，与时间范围无关。

    刻意不复用 repo_breakdown（那个只含当前时间窗内有事件的仓库）——选项列表
    跟着范围变会让用户选中的仓库在切换日期后突然消失，选项应当是稳定的全集。
    """
    rows = conn.execute(
        "SELECT DISTINCT repo FROM events WHERE repo IS NOT NULL AND repo != '' "
        "ORDER BY repo COLLATE NOCASE"
    ).fetchall()
    return [r["repo"] for r in rows if r["repo"] not in EXCLUDED_REPOS]


def filter_by_repo(events: list, repo: Optional[str]) -> list:
    """按仓库过滤事件。repo 为空/None 表示不过滤（全部仓库）。"""
    if not repo:
        return events
    return [e for e in events if e.repo == repo]


# 年度排行按仓库筛选时要累加的计数列。其余列（name/email/github_url 等）是
# 贡献者属性，取首次出现的值即可，不参与累加。
_YEARLY_NUM_COLS = (
    "commits", "commits_loose", "commits_not_in_upstream",
    "pr_created", "pr_merged", "pr_reviewed",
    "issue_created", "issue_commented",
)


def read_yearly_by_repo(path: str) -> list:
    """读年度「贡献者×仓库」逐行明细（output/daily/by_repo.csv）。

    summary.csv 里只有跨仓库汇总行（repo 为空），没法按仓库拆——所以年度排行
    的仓库筛选改读 by_repo.csv。已验证两份 CSV 口径一致：by_repo 按 login 累加
    后与 summary 的 450 个贡献者逐列完全相等，因此「全部仓库」用累加结果替代
    summary.csv 不会让现有数字发生变化。
    """
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("repo"):
                continue  # 防御：这份文件正常不含汇总行
            if row["repo"] in EXCLUDED_REPOS:
                continue
            rows.append(row)
    return rows


def yearly_repo_options(rows: list) -> list:
    """年度排行下拉框的仓库选项，按名字排序。"""
    return sorted({r["repo"] for r in rows if r.get("repo")}, key=str.lower)


def aggregate_yearly(rows: list, repo: Optional[str] = None,
                     include_anonymous: bool = False) -> list:
    """把逐行明细按 login 聚合成排行。repo 为空表示全部仓库（跨仓库累加）。

    以 login 为聚合键；login 为空的行（只有邮箱身份、无 GitHub 账号）用
    name+email 兜底，避免全部挤进同一个空键里互相覆盖。

    `include_anonymous` 默认 False：by_repo.csv 里有 83 个只有邮箱、没有 GitHub
    账号的 commit 作者，而 summary.csv（页面原先的数据源）把这些行整个丢掉了。
    默认排除是为了让「全部仓库」与改版前的数字逐列一致（已验证 450 人全等），
    不因为换数据源而悄悄改变口径。想连匿名作者一起看时再置 True。
    """
    if repo:
        rows = [r for r in rows if r.get("repo") == repo]
    if not include_anonymous:
        rows = [r for r in rows if r.get("login")]

    merged: dict = {}
    for r in rows:
        key = r.get("login") or f"\x00{r.get('name', '')}|{r.get('email', '')}"
        cur = merged.get(key)
        if cur is None:
            cur = {
                "login": r.get("login", ""),
                "name": r.get("name", ""),
                "email": r.get("email", ""),
                "github_url": r.get("github_url", ""),
                "is_bot": r.get("is_bot", ""),
                "is_ai_assistant": r.get("is_ai_assistant", ""),
            }
            for c in _YEARLY_NUM_COLS:
                cur[c] = 0
            merged[key] = cur
        for c in _YEARLY_NUM_COLS:
            try:
                cur[c] += int(r.get(c) or 0)
            except (TypeError, ValueError):
                pass  # 脏数据不该让整个排行 500，跳过这一格

    out = list(merged.values())
    out.sort(key=lambda r: (-r["commits"], (r["login"] or r["name"] or "").lower()))
    return out


def data_updated_at(csv_path: str, db_path: str) -> Optional[str]:
    """数据更新时间（东八区 YYYY-MM-DD），给页脚说明用。

    取 by_repo.csv 的 mtime——它是每晚 fetch 成功后重写的，比库里最后一条
    event_time 更能代表"这份数据跑到哪天了"（事件时间只反映最后一次有人提交，
    仓库安静几天不代表看板没更新）。CSV 不在时退回数据库文件的 mtime。
    """
    for p in (csv_path, db_path):
        try:
            ts = Path(p).stat().st_mtime
        except OSError:
            continue
        return datetime.fromtimestamp(ts, tz=CST).date().isoformat()
    return None
