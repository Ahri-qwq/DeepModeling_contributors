"""看板新增的仓库筛选逻辑。

覆盖两块 2026-09-10 加的东西：主看板按 repo 过滤事件，年度排行按 repo
聚合 by_repo.csv。重点验「分区守恒」——各仓库分别筛出来的量加起来必须等于
不筛的总量，多算漏算都会在这里暴露。
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from dashboard import queries


def _mk_events():
    now = datetime.now(timezone.utc)
    def ev(repo, kind, login, hours_ago):
        return queries.EventRow(
            kind=kind, repo=repo, number=1, sha=None, title="t", url="u",
            author_login=login, event_time="", state="merged" if kind == "pr" else None,
            event_dt=now - timedelta(hours=hours_ago),
        )
    return [
        ev("alpha", "commit", None, 1),
        ev("alpha", "pr", "ann", 2),
        ev("beta", "pr", "bob", 3),
        ev("beta", "issue", "ann", 4),
        ev("gamma", "commit", None, 5),
    ]


class TestFilterByRepo:
    def test_none_or_empty_means_all(self):
        evs = _mk_events()
        assert queries.filter_by_repo(evs, None) == evs
        assert queries.filter_by_repo(evs, "") == evs

    def test_filters_to_single_repo(self):
        got = queries.filter_by_repo(_mk_events(), "alpha")
        assert len(got) == 2
        assert {e.repo for e in got} == {"alpha"}

    def test_unknown_repo_yields_nothing(self):
        assert queries.filter_by_repo(_mk_events(), "nope") == []

    def test_partition_is_conserved(self):
        """各仓库筛出来的事件数加起来 == 总数，不重不漏。"""
        evs = _mk_events()
        repos = {e.repo for e in evs}
        assert sum(len(queries.filter_by_repo(evs, r)) for r in repos) == len(evs)

    def test_summary_reports_single_repo(self):
        """选中单仓库时 active_repos 应为 1（看板此时只显示该仓库）。"""
        s = queries.summary(queries.filter_by_repo(_mk_events(), "alpha"))
        assert s["active_repos"] == 1
        assert s["total_events"] == 2


class TestRepoList:
    def test_distinct_sorted_ignoring_blanks(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE events (repo TEXT)")
        conn.executemany("INSERT INTO events VALUES (?)",
                         [("beta",), ("alpha",), ("beta",), ("",), (None,)])
        assert queries.repo_list(conn) == ["alpha", "beta"]
        conn.close()


@pytest.fixture
def by_repo_csv(tmp_path):
    p = tmp_path / "by_repo.csv"
    p.write_text(
        "repo,login,name,email,github_url,commits,pr_created,pr_merged,"
        "pr_reviewed,issue_created,issue_commented\n"
        "alpha,ann,Ann,a@x,ua,10,2,1,0,3,0\n"
        "beta,ann,Ann,a@x,ua,5,1,1,0,0,0\n"
        "beta,bob,Bob,b@x,ub,7,0,0,4,1,0\n"
        ",,Anon,anon@x,,99,0,0,0,0,0\n",   # 无 login 的匿名行
        encoding="utf-8")
    return str(p)


class TestYearlyByRepo:
    def test_skips_rows_without_repo(self, by_repo_csv):
        rows = queries.read_yearly_by_repo(by_repo_csv)
        assert len(rows) == 3
        assert all(r["repo"] for r in rows)

    def test_missing_file_returns_empty(self, tmp_path):
        assert queries.read_yearly_by_repo(str(tmp_path / "nope.csv")) == []

    def test_repo_options_sorted(self, by_repo_csv):
        rows = queries.read_yearly_by_repo(by_repo_csv)
        assert queries.yearly_repo_options(rows) == ["alpha", "beta"]

    def test_aggregate_all_repos_sums_per_login(self, by_repo_csv):
        rows = queries.read_yearly_by_repo(by_repo_csv)
        agg = {r["login"]: r for r in queries.aggregate_yearly(rows)}
        assert agg["ann"]["commits"] == 15      # alpha 10 + beta 5
        assert agg["ann"]["pr_created"] == 3
        assert agg["bob"]["commits"] == 7

    def test_aggregate_single_repo(self, by_repo_csv):
        rows = queries.read_yearly_by_repo(by_repo_csv)
        agg = {r["login"]: r for r in queries.aggregate_yearly(rows, "beta")}
        assert set(agg) == {"ann", "bob"}
        assert agg["ann"]["commits"] == 5       # 只算 beta 那行

    def test_per_repo_sums_back_to_total(self, by_repo_csv):
        """守恒：逐仓库 commits 之和 == 全部仓库 commits 之和。"""
        rows = queries.read_yearly_by_repo(by_repo_csv)
        total = sum(r["commits"] for r in queries.aggregate_yearly(rows))
        per = sum(
            sum(r["commits"] for r in queries.aggregate_yearly(rows, repo))
            for repo in queries.yearly_repo_options(rows)
        )
        assert total == per

    def test_anonymous_excluded_by_default(self, by_repo_csv):
        """默认排除无 login 行，保持与 summary.csv 口径一致。"""
        rows = queries.read_yearly_by_repo(by_repo_csv)
        assert all(r["login"] for r in queries.aggregate_yearly(rows))

    def test_sorted_by_commits_desc(self, by_repo_csv):
        rows = queries.read_yearly_by_repo(by_repo_csv)
        out = queries.aggregate_yearly(rows)
        assert [r["login"] for r in out] == ["ann", "bob"]


class TestExcludedRepos:
    """`.github` 是组织元仓库（profile README / bot 自动提交），日报早已排除，
    看板口径必须跟上，否则 bot 提交会把活跃度榜顶起来。"""

    def test_query_events_drops_excluded(self, tmp_path):
        db = tmp_path / "e.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE events (kind TEXT, repo TEXT, number INT, sha TEXT, "
            "title TEXT, url TEXT, author_login TEXT, event_time TEXT, state TEXT)")
        now = datetime.now(timezone.utc)
        stamp = now.isoformat().replace("+00:00", "Z")
        conn.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            [("commit", ".github", 1, "s", "t", "u", "bot", stamp, None),
             ("commit", "alpha", 1, "s", "t", "u", "ann", stamp, None)])
        conn.commit()
        conn.close()

        ro = queries.open_readonly(str(db))
        got = queries.query_events(ro, now - timedelta(days=1), now + timedelta(days=1))
        ro.close()
        assert [e.repo for e in got] == ["alpha"]

    def test_repo_list_drops_excluded(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE events (repo TEXT)")
        conn.executemany("INSERT INTO events VALUES (?)", [(".github",), ("alpha",)])
        assert queries.repo_list(conn) == ["alpha"]
        conn.close()

    def test_yearly_csv_drops_excluded(self, tmp_path):
        p = tmp_path / "by_repo.csv"
        p.write_text(
            "repo,login,name,email,github_url,commits\n"
            ".github,bot,Bot,b@x,ub,500\n"
            "alpha,ann,Ann,a@x,ua,10\n", encoding="utf-8")
        rows = queries.read_yearly_by_repo(str(p))
        assert [r["repo"] for r in rows] == ["alpha"]
        assert queries.yearly_repo_options(rows) == ["alpha"]


class TestContributorRankingLimit:
    def test_limit_zero_returns_all(self):
        evs = _mk_events()
        assert len(queries.contributor_ranking(evs, 0)) == 2   # ann + bob
        assert len(queries.contributor_ranking(evs, -1)) == 2

    def test_positive_limit_truncates(self):
        assert len(queries.contributor_ranking(_mk_events(), 1)) == 1

    def test_sorted_by_count_desc(self):
        got = queries.contributor_ranking(_mk_events(), 0)
        assert got[0]["login"] == "ann" and got[0]["count"] == 2


class TestDataUpdatedAt:
    def test_reads_csv_mtime_as_cst_date(self, tmp_path):
        p = tmp_path / "by_repo.csv"
        p.write_text("repo\n", encoding="utf-8")
        got = queries.data_updated_at(str(p), str(tmp_path / "none.db"))
        assert got == datetime.now(queries.CST).date().isoformat()

    def test_falls_back_to_db(self, tmp_path):
        db = tmp_path / "e.db"
        db.write_bytes(b"x")
        got = queries.data_updated_at(str(tmp_path / "missing.csv"), str(db))
        assert got == datetime.now(queries.CST).date().isoformat()

    def test_returns_none_when_nothing_exists(self, tmp_path):
        assert queries.data_updated_at(
            str(tmp_path / "a.csv"), str(tmp_path / "b.db")) is None
