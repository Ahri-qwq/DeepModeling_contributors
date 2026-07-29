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
                out_dir=str(tmp_path / "output"))
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


def test_bots_are_separated_from_main_rows(tiny_repo):
    cfg = mk_cfg(tiny_repo)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "dependabot[bot]")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert any(r.login == "dependabot[bot]" for r in bots)
    assert not any(r.login == "dependabot[bot]" for r in rows)


def test_include_bots_puts_them_back_in_main_rows(tiny_repo):
    cfg = mk_cfg(tiny_repo, include_bots=True)
    cm = CacheManager(cfg.cache_dir)
    gstats = collect_git_stats(mk_repo(), cm, cfg)
    resolver = IdentityResolver()
    resolver.add_mapping("alice@example.com", "dependabot[bot]")
    rows, bots = build_rows(mk_repo(), gstats, {}, resolver, cfg)
    assert any(r.login == "dependabot[bot]" for r in rows)
    assert bots == []


# --- 完整 run()，用 monkeypatch 隔离网络 ---

def _patch_network(monkeypatch, repos):
    """替换 main 命名空间里的两个联网函数。

    必须 patch contributors.main 上的名字而非源模块，因为 main 用
    from ... import 把它们绑定到了自己的命名空间。
    """
    import contributors.main as m
    monkeypatch.setattr(m, "fetch_repos", lambda org, token: repos)
    monkeypatch.setattr(m, "collect_api_stats",
                        lambda repo, cfg, client, cm: ({}, {}))
    return m


def test_run_writes_all_output_files(tiny_repo, monkeypatch):
    m = _patch_network(monkeypatch, [mk_repo()])
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    code = m.run(cfg, token="fake")
    assert code == 0
    out = Path(cfg.out_dir)
    for f in ["summary.csv", "by_repo.csv", "contributors.md",
              "contributors.json", "unmatched.csv", "bots.csv",
              "run_meta.json"]:
        assert (out / f).is_file(), f"缺少输出文件 {f}"
    assert (out / "repos" / "tiny.csv").is_file()


def test_run_meta_records_skipped_repos(tiny_repo, monkeypatch):
    import json
    big = RepoInfo(name="huge", default_branch="main", size_mb=99999.0,
                   pushed_at="2026-07-01T00:00:00Z", is_fork=False,
                   upstream=None, upstream_family=None, archived=False)
    m = _patch_network(monkeypatch, [mk_repo(), big])
    cfg = mk_cfg(tiny_repo, no_fetch=True)
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
    m = _patch_network(monkeypatch, [mk_repo(), bad])
    cfg = mk_cfg(tiny_repo, no_fetch=True)
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
    m = _patch_network(monkeypatch, [mk_repo()])
    cfg = mk_cfg(tiny_repo, no_fetch=True)
    m.run(cfg, token="fake")
    meta = json.loads(
        (Path(cfg.out_dir) / "run_meta.json").read_text(encoding="utf-8"))
    # alice 与 bob 都无 login 映射，必须被记录而非静默丢弃
    assert meta["unmatched_emails"] == 2
    body = (Path(cfg.out_dir) / "unmatched.csv").read_text(encoding="utf-8-sig")
    assert "alice@example.com" in body

