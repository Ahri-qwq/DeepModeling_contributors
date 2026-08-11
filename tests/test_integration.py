"""端到端测试：现场造小仓库跑完整 git 管线，不碰网络。"""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contributors.cache import CacheManager
from contributors.config import Config, DEFAULT_EXCLUDE_PATHS
from contributors.git_stats import collect_git_stats
from contributors.identity import IdentityResolver
from contributors.main import build_rows
from contributors.models import ApiStats, RepoInfo


def git(args, cwd, when=None):
    """执行 git 命令。when 同时设置 author 与 committer 日期。

    必须同时设置两者：git log --since/--until 过滤的是 committer date，
    而 --date= 只改 author date。只设 --date 会让所有提交的 committer
    date 都停在"现在"，时间窗测试将全部失真（实测确认）。
    """
    env = None
    if when is not None:
        env = {**os.environ, "GIT_COMMITTER_DATE": when}
        args = args + [f"--date={when}"]
    subprocess.run(["git"] + args, cwd=str(cwd), check=True,
                   capture_output=True, text=True, env=env)


@pytest.fixture
def tiny_repo(tmp_path):
    """造一个含两位作者、一个未合并分支的裸库。"""
    work = tmp_path / "work"
    work.mkdir()
    git(["init", "-q", "-b", "main"], work)
    git(["config", "user.email", "alice@example.com"], work)
    git(["config", "user.name", "Alice"], work)
    (work / "a.py").write_text("print(1)\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m", "first"], work, when="2026-01-15T00:00:00Z")

    # 第二位作者，在未合并的分支上提交 —— 验证 --all 覆盖所有分支
    git(["checkout", "-q", "-b", "feature"], work)
    git(["config", "user.email", "bob@example.com"], work)
    git(["config", "user.name", "Bob"], work)
    (work / "b.py").write_text("print(2)\nprint(3)\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m", "second"], work, when="2026-02-20T00:00:00Z")

    # 窗口外的提交，必须被排除
    (work / "old.py").write_text("old\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m", "too old"], work, when="2020-01-01T00:00:00Z")

    bare = tmp_path / ".cache" / "repos" / "tiny.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--mirror", "-q", str(work), str(bare)], tmp_path)
    return tmp_path


def mk_cfg(tmp_path, **kw):
    base = dict(org="test",
                since=datetime(2025, 7, 28, tzinfo=timezone.utc),
                until=datetime(2026, 7, 29, tzinfo=timezone.utc),
                include_forks="all", max_repo_size=2048,
                # 必须显式传入：Config.exclude_paths 默认为空列表，
                # 只有 parse_args 才会填入 DEFAULT_EXCLUDE_PATHS。
                # 漏传会让 --count-lines 的过滤测试假通过。
                exclude_paths=list(DEFAULT_EXCLUDE_PATHS),
                cache_dir=str(tmp_path / ".cache"),
                out_dir=str(tmp_path / "output"),
                # 必须显式重定向：默认值是真实的 ./data/events.db，
                # 漏传会让走完整 run() 的测试把 tiny 的假数据写进真实事件库
                events_db=str(tmp_path / "events.db"))
    base.update(kw)
    return Config(**base)


def mk_repo():
    return RepoInfo(name="tiny", default_branch="main", size_mb=1.0,
                    pushed_at="2026-02-20T00:00:00Z", is_fork=False,
                    upstream=None, upstream_family=None, archived=False)


def test_collects_commits_from_all_branches(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    # bob 的提交只在未合并的 feature 分支上，--all 必须捕获
    assert stats["alice@example.com"].commits == 1
    assert stats["bob@example.com"].commits == 1


def test_excludes_commits_outside_window(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    total = sum(s.commits for s in stats.values())
    assert total == 2  # 2020 年那条被排除


def test_line_counts_when_enabled(tiny_repo):
    cfg = mk_cfg(tiny_repo, count_lines=True)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    assert stats["bob@example.com"].additions == 2


def test_no_fetch_recompute_is_offline(tiny_repo):
    # 已有缓存 + --no-fetch 应能纯本地重算
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    assert sum(s.commits for s in stats.values()) == 2


def test_narrowing_window_reduces_counts_without_network(tiny_repo):
    cfg = mk_cfg(tiny_repo, no_fetch=True,
                 since=datetime(2026, 2, 1, tzinfo=timezone.utc))
    cm = CacheManager(cfg.cache_dir)
    stats = collect_git_stats(mk_repo(), cm, cfg)
    assert "alice@example.com" not in stats
    assert stats["bob@example.com"].commits == 1


def test_build_rows_merges_git_and_api_by_login(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "alice")
    api = {"alice": ApiStats(pr_created=3, pr_merged=2, pr_reviewed=1)}
    rows, bots = build_rows(mk_repo(), gstats, api, resolver, cfg)
    alice = [r for r in rows if r.login == "alice"][0]
    assert alice.commits == 1
    assert alice.pr_created == 3
    assert alice.github_url == "https://github.com/alice"


def test_unmatched_email_still_appears_in_rows(tiny_repo):
    # bob 无 login 映射，绝不能被丢弃
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    rows, _ = build_rows(mk_repo(), gstats, {}, IdentityResolver(), cfg)
    assert any(r.email == "bob@example.com" for r in rows)


def test_api_only_contributor_appears_with_zero_commits(tiny_repo):
    # 只提 issue、从未提交代码的人也是贡献者，不能漏
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    api = {"reviewer": ApiStats(pr_reviewed=8, issue_created=2)}
    rows, _ = build_rows(mk_repo(), gstats, api, IdentityResolver(), cfg)
    r = [x for x in rows if x.login == "reviewer"][0]
    assert r.commits == 0
    assert r.pr_reviewed == 8


def test_marked_bots_excluded_from_main_rows(tiny_repo):
    # id 里显式带 [bot] 的是 GitHub 平台标记，可直接采信，默认剔除主表
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "dependabot[bot]")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert not any(r.login == "dependabot[bot]" for r in rows)
    # 但仍要出现在 bots.csv 对照表里
    assert any(r.login == "dependabot[bot]" for r in bots)


def test_inferred_bots_stay_in_main_rows_by_default(tiny_repo):
    # 黑名单推断的 bot（id 里无 [bot]）有误判风险，默认保留只作标注
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "njzjz-bot")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert any(r.login == "njzjz-bot" for r in rows)
    assert any(r.is_bot for r in rows if r.login == "njzjz-bot")


def test_bots_csv_still_lists_bots_when_kept_in_main(tiny_repo):
    # 主表保留不代表放弃对照表：bots.csv 必须照旧输出供人工核对，
    # 否则用户拿不到「哪些是机器人」的清单
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "dependabot[bot]")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert any(r.login == "dependabot[bot]" for r in bots)


def test_exclude_bots_removes_them_from_main_rows(tiny_repo):
    cfg = mk_cfg(tiny_repo, exclude_bots=True)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "dependabot[bot]")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert not any(r.login == "dependabot[bot]" for r in rows)
    assert any(r.login == "dependabot[bot]" for r in bots)


def test_ai_assistant_flagged_and_kept_in_main_rows(tiny_repo):
    # Q8：Copilot 保留在主表，is_ai_assistant 标注；不进 bots.csv，
    # 因为它不是 CI 自动化，人工判断依据不同
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "Copilot")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    cop = [r for r in rows if r.login == "Copilot"][0]
    assert cop.is_ai_assistant is True
    assert cop.is_bot is False
    assert not any(r.login == "Copilot" for r in bots)


def test_ai_assistant_survives_exclude_bots(tiny_repo):
    # --exclude-bots 只排 CI 机器人，不该顺手把 AI 助手也排掉
    cfg = mk_cfg(tiny_repo, exclude_bots=True)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "Copilot")
    rows, _ = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert any(r.login == "Copilot" for r in rows)


# --- 完整 run()，用 monkeypatch 隔离网络 ---

def _patch_network(monkeypatch, repos, cfg=None):
    """隔离联网部分。

    fetch_repos 仍要 patch（供非 --no-fetch 路径用），但 --no-fetch 下
    run() 读的是清单缓存，故还要把 repos 预写进缓存 —— 否则测试会绕过
    真实的 --no-fetch 代码路径，掩盖它是否真能零网络运行。

    必须 patch contributors.main 上的名字而非源模块，因为 main 用
    from ... import 把它们绑定到了自己的命名空间。
    """
    import contributors.main as m
    monkeypatch.setattr(m, "fetch_repos", lambda org, token: repos)
    monkeypatch.setattr(m, "collect_api_stats",
                        lambda repo, cfg, client, cm: ({}, {}))
    if cfg is not None:
        CacheManager(cfg.cache_dir).save_repo_list(repos)
    return m


def test_run_writes_all_output_files(tiny_repo, monkeypatch):
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    code = m.run(cfg, token="fake")
    assert code == 0
    out = Path(cfg.out_dir)
    for f in ["summary.csv", "by_repo.csv", "contributors.md",
              "contributors.json", "unmatched.csv", "bots.csv",
              "ai_assisted.csv", "run_meta.json"]:
        assert (out / f).is_file(), f"缺少输出文件 {f}"
    assert (out / "repos" / "tiny.csv").is_file()


# --- AI 指派人追溯附表（Q8）---

@pytest.fixture
def ai_repo(tmp_path):
    """造一个含 Copilot 提交的裸库：一条带指派人、一条不带。"""
    work = tmp_path / "work"
    work.mkdir()
    git(["init", "-q", "-b", "main"], work)
    git(["config", "user.email", "198982749+Copilot@users.noreply.github.com"],
        work)
    git(["config", "user.name", "Copilot"], work)

    (work / "a.py").write_text("print(1)\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m",
         "fix: something\n\nCo-authored-by: njzjz <njzjz@example.com>"],
        work, when="2026-03-01T00:00:00Z")

    (work / "b.py").write_text("print(2)\n")
    git(["add", "."], work)
    git(["commit", "-q", "-m", "fix: no coauthor"], work,
        when="2026-03-02T00:00:00Z")

    bare = tmp_path / ".cache" / "repos" / "tiny.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--mirror", "-q", str(work), str(bare)], tmp_path)
    return tmp_path


def test_ai_assisted_csv_lists_traced_assignee(ai_repo, monkeypatch):
    import csv as _csv
    cfg = mk_cfg(ai_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    m.run(cfg, token="fake")
    p = Path(cfg.out_dir) / "ai_assisted.csv"
    with p.open(encoding="utf-8-sig", newline="") as f:
        recs = list(_csv.DictReader(f))
    njz = [r for r in recs if r["name"] == "njzjz"]
    assert njz and njz[0]["ai_commits"] == "1"
    assert njz[0]["repo"] == "tiny"


def test_ai_assisted_csv_reports_untraced_count(ai_repo, monkeypatch):
    # 覆盖率有限（实测约两成），未追溯的数量必须一并给出，
    # 否则附表会让人误以为全部可追溯
    import json
    cfg = mk_cfg(ai_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    m.run(cfg, token="fake")
    meta = json.loads(
        (Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["ai_commits_traced"] == 1
    assert meta["ai_commits_untraced"] == 1


def test_ai_assisted_csv_is_empty_without_ai_commits(tiny_repo, monkeypatch):
    import csv as _csv
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    m.run(cfg, token="fake")
    p = Path(cfg.out_dir) / "ai_assisted.csv"
    with p.open(encoding="utf-8-sig", newline="") as f:
        assert list(_csv.DictReader(f)) == []


def test_run_meta_records_marked_bot_exclusion(tiny_repo, monkeypatch):
    # 显式 [bot] 的剔除是恒定行为，与 --exclude-bots 无关。
    # 两个字段必须分开，否则读 meta 的人会以为主表里还有 [bot] 账号
    import json
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    m.run(cfg, token="fake")
    meta = json.loads(
        (Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["marked_bots_excluded_from_main"] is True
    # 未加 --exclude-bots 时，推断出的 bot 仍在主表
    assert meta["inferred_bots_excluded_from_main"] is False


def test_run_meta_records_skipped_repos(tiny_repo, monkeypatch):
    import json
    big = RepoInfo(name="huge", default_branch="main", size_mb=99999.0,
                   pushed_at="2026-07-01T00:00:00Z", is_fork=False,
                   upstream=None, upstream_family=None, archived=False)
    # 显式设限才会触发体积跳过（默认 0 = 不限）
    cfg = mk_cfg(tiny_repo, no_fetch=True, max_repo_size=2048)
    m = _patch_network(monkeypatch, [mk_repo(), big], cfg)
    m.run(cfg, token="fake")
    meta = json.loads(
        (Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    assert "huge" in meta["skipped"]
    assert meta["window"]["since"] == "2025-07-28"


def test_single_repo_failure_does_not_abort_run(tiny_repo, monkeypatch):
    import json
    bad = RepoInfo(name="bad", default_branch="main", size_mb=1.0,
                   pushed_at="2026-07-01T00:00:00Z", is_fork=False,
                   upstream=None, upstream_family=None, archived=False)
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo(), bad], cfg)
    code = m.run(cfg, token="fake")
    meta = json.loads(
        (Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    # bad 无本地缓存且 no_fetch，应记入 failures 而非崩溃
    assert "bad" in meta["failures"]
    assert code == 0
    # tiny 的数据仍然产出
    assert (Path(cfg.out_dir) / "repos" / "tiny.csv").is_file()


def test_run_reports_unmatched_emails(tiny_repo, monkeypatch):
    import json
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    m.run(cfg, token="fake")
    meta = json.loads(
        (Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    # alice 与 bob 都无 login 映射，必须被记录而非静默丢弃
    assert meta["unmatched_emails"] == 2
    body = (Path(cfg.out_dir) / "unmatched.csv").read_text(encoding="utf-8-sig")
    assert "alice@example.com" in body



# --- 按姓名合并未归并身份 ---
#
# 实测背景（deepmd-kit / abacus-develop）：贡献者用未在 GitHub 登记的邮箱
# 提交时反查不到 login，会被拆成独立条目。实测 Han Wang 被拆成 1151+110、
# dyzheng 被拆成 128+278，榜单严重失真。这些条目的 git 姓名与其 login
# 条目的姓名完全一致，可据此合并。
#
# 只在同一仓库内、姓名完全相同（去空白、忽略大小写）时合并：跨仓库合并
# 风险更高，而重名在单个仓库内的概率很低。

def _row(login, name, email, commits, repo="tiny", **kw):
    from contributors.output import Row
    d = dict(repo=repo, login=login, name=name, email=email,
             github_url=f"https://github.com/{login}" if login else "",
             commits=commits, commits_loose=commits,
             commits_not_in_upstream=commits,
             pr_created=0, pr_merged=0, pr_reviewed=0,
             issue_created=0, issue_commented=0,
             is_fork=False, upstream="", upstream_family="", is_bot=False)
    d.update(kw)
    return Row(**d)


def test_year_totals_omits_pr_issue_when_api_data_absent():
    """离线路径下 PR/issue 三项必须是 None 而非 0。

    --no-fetch 或无当天 API 缓存时 collect_api_stats 返回空，三项若加成 0，
    日报底部会写"今年 0 个 PR 合并"，而上方分区正列着几百个合并的 PR。
    实测在真实 dry-run 中触发过。
    """
    from contributors.main import _year_totals
    rows = [_row("a", "A", "a@x.com", 10), _row("b", "B", "b@x.com", 5)]
    t = _year_totals(rows)
    assert t["commits"] == 15
    assert t["pr_merged"] is None
    assert t["pr_created"] is None
    assert t["issue_created"] is None


def test_year_totals_reports_pr_issue_when_present():
    """有 API 数据时照常汇总。"""
    from contributors.main import _year_totals
    rows = [_row("a", "A", "a@x.com", 10, pr_created=3, pr_merged=2,
                 issue_created=1),
            _row("b", "B", "b@x.com", 5, pr_created=1, pr_merged=1,
                 issue_created=4)]
    t = _year_totals(rows)
    assert t["pr_created"] == 4
    assert t["pr_merged"] == 3
    assert t["issue_created"] == 5


def test_merges_unlinked_row_into_matching_login_row():
    from contributors.main import merge_by_name
    rows = [_row("wanghan-iapcm", "Han Wang", "a@noreply", 110),
            _row("", "Han Wang", "wang_han@iapcm.ac.cn", 1151)]
    out = merge_by_name(rows)
    assert len(out) == 1
    assert out[0].login == "wanghan-iapcm"
    assert out[0].commits == 1261


def test_merge_sums_both_commit_fields_independently():
    """合并时两个字段各自求和，不需要调整口径。

    实测 wanghan-iapcm：主干 110 次，另一个邮箱只在 PR 分支上有 1165 次
    squash 前的过程提交（主干 0）。合并后 commits 仍是 110 —— 严格口径
    本就不含 PR 分支提交，squash 的重复计数进不来；宽松口径 110+1165
    则是这个人的全部痕迹。
    """
    from contributors.main import merge_by_name
    rows = [_row("wanghan-iapcm", "Han Wang", "a@noreply", 110,
                 commits_loose=110),
            _row("", "Han Wang", "wang_han@iapcm.ac.cn", 0,
                 commits_loose=1165)]
    out = merge_by_name(rows)
    assert len(out) == 1
    assert out[0].login == "wanghan-iapcm"
    assert out[0].commits == 110, "严格口径不受 PR 分支影响"
    assert out[0].commits_loose == 1275


def test_merge_keeps_main_commits_of_unlinked_row():
    """被合并行若自身也有主干提交，两个字段都要累加。"""
    from contributors.main import merge_by_name
    rows = [_row("alice", "Alice", "a@x.com", 10, commits_loose=10),
            _row("", "Alice", "b@y.com", 2, commits_loose=7)]
    out = merge_by_name(rows)
    assert out[0].commits == 12
    assert out[0].commits_loose == 17


def test_merged_row_keeps_both_emails():
    from contributors.main import merge_by_name
    rows = [_row("dyzheng", "dyzheng", "zhengdy@aisi.ac.cn", 278),
            _row("", "dyzheng", "zhengdy@bjaisi.com", 128)]
    out = merge_by_name(rows)
    assert set(out[0].email.split(";")) == {"zhengdy@aisi.ac.cn",
                                            "zhengdy@bjaisi.com"}


def test_name_match_ignores_case_and_whitespace():
    from contributors.main import merge_by_name
    rows = [_row("alice", "Alice Smith", "a@x.com", 5),
            _row("", "  alice smith ", "b@y.com", 3)]
    assert len(merge_by_name(rows)) == 1


def test_different_names_are_not_merged():
    from contributors.main import merge_by_name
    # 实测 abacus_fixer 与 Mohan Chen 是同一人，但姓名不同，
    # 本规则刻意不合并 —— 宁可漏合并，不可错合并
    rows = [_row("mohanchen", "Mohan Chen", "a@pku.edu.cn", 117),
            _row("", "abacus_fixer", "b@pku.eud.cn", 755)]
    assert len(merge_by_name(rows)) == 2


def test_does_not_merge_across_repos():
    from contributors.main import merge_by_name
    rows = [_row("alice", "Alice", "a@x.com", 5, repo="r1"),
            _row("", "Alice", "b@y.com", 3, repo="r2")]
    assert len(merge_by_name(rows)) == 2


def test_two_unlinked_rows_are_not_merged_with_each_other():
    # 都无 login 时无法确认是同一人，保持分开并留在 unmatched
    from contributors.main import merge_by_name
    rows = [_row("", "Alice", "a@x.com", 5), _row("", "Alice", "b@y.com", 3)]
    assert len(merge_by_name(rows)) == 2


def test_ambiguous_name_matching_two_logins_is_left_alone():
    """同名对应多个 login 时无法判定归属，不合并。"""
    from contributors.main import merge_by_name
    rows = [_row("alice1", "Alice", "a@x.com", 5),
            _row("alice2", "Alice", "b@x.com", 4),
            _row("", "Alice", "c@y.com", 3)]
    assert len(merge_by_name(rows)) == 3


def test_rows_without_name_are_never_merged():
    from contributors.main import merge_by_name
    rows = [_row("alice", "", "a@x.com", 5), _row("", "", "b@y.com", 3)]
    assert len(merge_by_name(rows)) == 2


def test_line_counts_are_summed_when_present():
    from contributors.main import merge_by_name
    a = _row("alice", "Alice", "a@x.com", 5)
    b = _row("", "Alice", "b@y.com", 3)
    a.additions, b.additions = 10, 7
    out = merge_by_name(rows := [a, b])
    assert out[0].additions == 17


def test_no_fetch_without_cached_list_exits_cleanly(tiny_repo, monkeypatch):
    """--no-fetch 且无清单缓存时应给出指引并退出，而非联网崩溃。

    实测缺陷：原实现在 --no-fetch 下仍调用 fetch_repos，而该模式不取
    token，空 token 请求 GitHub 返回 401 未捕获异常，使 README 承诺的
    "改时间窗零网络重算"完全不可用。
    """
    import contributors.main as m

    def must_not_be_called(org, token):
        raise AssertionError("--no-fetch 下不得联网拉取仓库清单")

    monkeypatch.setattr(m, "fetch_repos", must_not_be_called)
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    assert m.run(cfg, token="") == 2


def test_repo_list_is_cached_for_later_offline_runs(tiny_repo, monkeypatch):
    """联网运行应把清单写入缓存，供后续 --no-fetch 使用。"""
    import contributors.main as m
    monkeypatch.setattr(m, "fetch_repos", lambda org, token: [mk_repo()])
    monkeypatch.setattr(m, "collect_api_stats",
                        lambda repo, cfg, client, cm: ({}, {}))
    monkeypatch.setattr(m, "clone_or_fetch", lambda repo, cm, cfg: None)
    cfg = mk_cfg(tiny_repo)
    m.run(cfg, token="fake")
    assert CacheManager(cfg.cache_dir).load_repo_list() is not None


# --- unmatched 报告需自带上下文 ---
#
# 原实现只输出一列邮箱，管理员看到 2050325993@qq.com 无从认人：
# 缺姓名、缺提交量、缺所在仓库，必须与 summary.csv 对照才能查，
# 且不知该优先看哪几条。改为从输出行提取，按提交数降序。

def test_unmatched_report_includes_name_and_commits():
    from contributors.main import build_unmatched_report
    rows = [_row("", "Fei Yang", "2501213217@stu.pku.edu.cn", 60,
                 repo="abacus-develop")]
    rep = build_unmatched_report(rows, {"2501213217@stu.pku.edu.cn"})
    assert len(rep) == 1
    assert rep[0]["name"] == "Fei Yang"
    assert rep[0]["commits"] == 60
    assert rep[0]["repos"] == "abacus-develop"
    assert rep[0]["identity"] == "2501213217@stu.pku.edu.cn"


def test_unmatched_report_sorted_by_commits_desc():
    from contributors.main import build_unmatched_report
    rows = [_row("", "Low", "low@x.com", 1),
            _row("", "High", "high@x.com", 60)]
    rep = build_unmatched_report(rows, {"low@x.com", "high@x.com"})
    assert [r["name"] for r in rep] == ["High", "Low"]


def test_unmatched_report_aggregates_across_repos():
    from contributors.main import build_unmatched_report
    rows = [_row("", "Fei Yang", "f@x.com", 40, repo="r1"),
            _row("", "Fei Yang", "f@x.com", 20, repo="r2")]
    rep = build_unmatched_report(rows, {"f@x.com"})
    assert len(rep) == 1
    assert rep[0]["commits"] == 60
    assert set(rep[0]["repos"].split(";")) == {"r1", "r2"}


def test_unmatched_report_excludes_rows_with_login():
    """已关联账号的行不该出现在待确认名单里。"""
    from contributors.main import build_unmatched_report
    rows = [_row("alice", "Alice", "a@x.com", 5),
            _row("", "Bob", "b@x.com", 3)]
    rep = build_unmatched_report(rows, {"a@x.com", "b@x.com"})
    assert [r["name"] for r in rep] == ["Bob"]


def test_unmatched_report_keeps_emailless_identity():
    """git 未配邮箱的贡献者以姓名兜底，同样要能出现在报告里。"""
    from contributors.main import build_unmatched_report
    rows = [_row("", "a-006", "", 7)]
    rep = build_unmatched_report(rows, {"<无邮箱> a-006"})
    assert len(rep) == 1
    assert rep[0]["name"] == "a-006"
    assert rep[0]["identity"] == "(git 未配置邮箱)"


def test_unmatched_report_lists_orphan_identities_without_rows():
    """resolver 记录了但输出行里找不到的身份，仍要列出而非丢弃。"""
    from contributors.main import build_unmatched_report
    rep = build_unmatched_report([], {"ghost@nowhere.com"})
    assert len(rep) == 1
    assert rep[0]["identity"] == "ghost@nowhere.com"
    assert rep[0]["commits"] == 0


# --- fork 上游排除口径 -----------------------------------------------------
#
# 实测背景（deepmodeling/GPUMD，fork 自 brucefan1983/GPUMD）：窗口内 638 次
# 提交全部为上游可达，count_upstream_excluded 正确返回空字典。但 build_rows
# 用 upstream_counts.get(email, gs.commits) 兜底，把"上游可达故为 0"的人
# 抬成了等于 commits——上游原作者 Zheyong Fan 显示 192/192，实际应为 0/192。
# 该兜底只对非 fork 成立，fork 必须缺失即 0，否则 Q1 依赖的这列完全失效。

def _fork_repo(name="GPUMD"):
    from contributors.models import RepoInfo
    return RepoInfo(name=name, default_branch="master", size_mb=1.0,
                    pushed_at="2026-06-09T00:00:00Z", is_fork=True,
                    upstream="brucefan1983/GPUMD",
                    upstream_family="external", archived=False)


def test_fork_missing_from_upstream_counts_means_zero(tmp_path):
    """fork 里上游可达的提交必须记 0，不能兜底成 commits。"""
    from contributors.main import build_rows
    from contributors.models import GitStats
    gs = GitStats(commits=192, emails={"brucenju@gmail.com"},
                  names={"Zheyong Fan"})
    rows, _ = build_rows(_fork_repo(), {"brucenju@gmail.com": gs}, {},
                         IdentityResolver(), mk_cfg(tmp_path), {})
    assert len(rows) == 1
    assert rows[0].commits == 192
    assert rows[0].commits_not_in_upstream == 0


def test_fork_partial_upstream_counts_are_respected(tmp_path):
    """部分提交独立于上游时，按 count_upstream_excluded 的实际值记。"""
    from contributors.main import build_rows
    from contributors.models import GitStats
    gs = GitStats(commits=100, emails={"dev@x.com"}, names={"Dev"})
    rows, _ = build_rows(_fork_repo(), {"dev@x.com": gs}, {},
                         IdentityResolver(), mk_cfg(tmp_path),
                         {"dev@x.com": 7})
    assert rows[0].commits == 100
    assert rows[0].commits_not_in_upstream == 7


def test_non_fork_still_equals_commits(tmp_path):
    """非 fork 没有上游概念，该列等于 commits，此行为不能被改坏。"""
    from contributors.main import build_rows
    from contributors.models import GitStats, RepoInfo
    repo = RepoInfo(name="tiny", default_branch="main", size_mb=1.0,
                    pushed_at="2026-06-09T00:00:00Z", is_fork=False,
                    upstream=None, upstream_family=None, archived=False)
    gs = GitStats(commits=42, emails={"a@x.com"}, names={"A"})
    rows, _ = build_rows(repo, {"a@x.com": gs}, {},
                         IdentityResolver(), mk_cfg(tmp_path), {})
    assert rows[0].commits_not_in_upstream == 42


def test_fork_upstream_compare_failure_degrades_to_commits(tmp_path):
    """上游对比失败（None）要退化为 commits，不能与"全部上游可达"混淆。

    空字典表示对比成功但无人独立于上游，应记 0；None 表示 fetch 失败、
    无从判断，此时记 commits 更安全——宁可多算也不抹掉真实贡献。
    """
    from contributors.main import build_rows
    from contributors.models import GitStats
    gs = GitStats(commits=55, emails={"dev@x.com"}, names={"Dev"})
    rows, _ = build_rows(_fork_repo(), {"dev@x.com": gs}, {},
                         IdentityResolver(), mk_cfg(tmp_path), None)
    assert rows[0].commits_not_in_upstream == 55


# --- 日常模式：只维护两张总表 ---
#
# 用户的用法是每天跑一次更新总量，只看 by_repo.csv 与 summary.csv。
# 其余附表（per-repo、md、json、unmatched、bots、ai_assisted）是全量
# 统计时的人工核对材料，每天重写既慢又没人看。

def test_daily_mode_writes_only_two_tables(tiny_repo, monkeypatch):
    cfg = mk_cfg(tiny_repo, no_fetch=True, daily=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    m.run(cfg, token="fake")
    out = Path(cfg.out_dir)

    assert (out / "by_repo.csv").is_file()
    assert (out / "summary.csv").is_file()
    # run_meta.json 保留：排障与增量判定都要读它
    assert (out / "run_meta.json").is_file()

    for gone in ("contributors.md", "contributors.json", "unmatched.csv",
                 "bots.csv", "ai_assisted.csv"):
        assert not (out / gone).exists(), f"日常模式不该写 {gone}"
    assert not (out / "repos").exists(), "日常模式不该写 per-repo 目录"


def test_full_mode_still_writes_everything(tiny_repo, monkeypatch):
    """全量模式产出不变，日常模式是新增而非替换。"""
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    m = _patch_network(monkeypatch, [mk_repo()], cfg)
    m.run(cfg, token="fake")
    out = Path(cfg.out_dir)

    for name in ("by_repo.csv", "summary.csv", "contributors.md",
                 "contributors.json", "unmatched.csv", "bots.csv",
                 "ai_assisted.csv", "run_meta.json"):
        assert (out / name).is_file(), f"全量模式应写 {name}"
    assert (out / "repos" / "tiny.csv").is_file()
