from datetime import datetime, timezone
import pytest
from contributors.models import RepoInfo
from contributors.config import Config
from contributors.repos import classify_upstream, should_skip, filter_repos


def mk_repo(name="r", size_mb=10.0, pushed="2026-07-01T00:00:00Z",
            is_fork=False, upstream=None, archived=False, branch="main"):
    return RepoInfo(
        name=name, default_branch=branch, size_mb=size_mb, pushed_at=pushed,
        is_fork=is_fork, upstream=upstream,
        upstream_family=classify_upstream(upstream), archived=archived,
    )


def mk_cfg(**kw):
    base = dict(
        org="deepmodeling",
        since=datetime(2025, 7, 28, tzinfo=timezone.utc),
        until=datetime(2026, 7, 29, tzinfo=timezone.utc),
        include_forks="all", max_repo_size=2048,
    )
    base.update(kw)
    return Config(**base)


# --- fork 三分类，用 spec 第 2.1 节的真实数据 ---

@pytest.mark.parametrize("upstream", [
    "abacusmodeling/abacus-develop",
    "dptech-corp/dpgen2",
    "dptech-corp/Uni-Fold-jax",
    "mzjb/DeepH-pack",
    "abacusmodeling/LibRI",
    "deepflameCFD/deepflame-dev",
])
def test_ecosystem_upstreams_are_self(upstream):
    assert classify_upstream(upstream) == "self"


@pytest.mark.parametrize("upstream", [
    "lammps/lammps",
    "plumed/plumed2",
    "brucefan1983/GPUMD",
    "DEShawResearch/msys",
    "weihuayi/fealpy",
    "Roy-Kid/ADMP",
])
def test_external_project_upstreams_are_external(upstream):
    assert classify_upstream(upstream) == "external"


@pytest.mark.parametrize("upstream", [
    "platisd/clang-tidy-pr-comments",
    "jitterbit/get-changed-files",
    "zjgemi/argo-workflows",
    "deepmd-kit-recipes/deepmd-kit-recipes",
])
def test_ci_tooling_upstreams_are_tooling(upstream):
    assert classify_upstream(upstream) == "tooling"


def test_non_fork_has_no_family():
    assert classify_upstream(None) is None


# --- pushed_at 预筛 ---

def test_repo_pushed_before_since_is_skipped():
    # lammps 停在 2024-04-11，一年窗口内不可能有 commit
    r = mk_repo(name="lammps", pushed="2024-04-11T00:00:00Z")
    assert should_skip(r, mk_cfg()) == "pushed_at 早于统计起点"


def test_repo_pushed_on_boundary_day_is_kept():
    # 边界同日不跳过：当天可能有窗口内的提交
    r = mk_repo(pushed="2025-07-28T12:00:00Z")
    assert should_skip(r, mk_cfg()) is None


def test_repo_pushed_after_since_is_kept():
    r = mk_repo(pushed="2026-06-09T00:00:00Z")
    assert should_skip(r, mk_cfg()) is None


# --- 体积阈值 ---

def test_oversized_repo_is_skipped():
    r = mk_repo(name="sciencepedia", size_mb=17563.2)
    assert "体积" in should_skip(r, mk_cfg())


def test_oversized_repo_included_when_threshold_raised():
    r = mk_repo(name="sciencepedia", size_mb=17563.2)
    assert should_skip(r, mk_cfg(max_repo_size=20000)) is None


# --- fork 筛选三档 ---

def test_include_forks_none_skips_all_forks():
    r = mk_repo(is_fork=True, upstream="abacusmodeling/abacus-develop")
    assert should_skip(r, mk_cfg(include_forks="none")) == "fork 已按 --include-forks=none 排除"


def test_include_forks_self_keeps_ecosystem_fork():
    r = mk_repo(is_fork=True, upstream="abacusmodeling/abacus-develop")
    assert should_skip(r, mk_cfg(include_forks="self")) is None


def test_include_forks_self_skips_external_fork():
    r = mk_repo(is_fork=True, upstream="lammps/lammps",
                pushed="2026-07-01T00:00:00Z")
    assert should_skip(r, mk_cfg(include_forks="self")) is not None


def test_include_forks_all_keeps_external_fork():
    r = mk_repo(is_fork=True, upstream="brucefan1983/GPUMD",
                pushed="2026-06-09T00:00:00Z")
    assert should_skip(r, mk_cfg(include_forks="all")) is None


# --- --repos 白名单 ---

def test_repos_whitelist_skips_others():
    r = mk_repo(name="dpdata")
    assert should_skip(r, mk_cfg(repos=["DeePMD-kit"])) == "不在 --repos 指定范围内"


def test_repos_whitelist_keeps_listed():
    r = mk_repo(name="dpdata")
    assert should_skip(r, mk_cfg(repos=["dpdata"])) is None


# --- filter_repos 汇总 ---

def test_filter_repos_returns_kept_and_reasons():
    repos = [
        mk_repo(name="dpdata", pushed="2026-07-01T00:00:00Z"),
        mk_repo(name="lammps", pushed="2024-04-11T00:00:00Z"),
        mk_repo(name="sciencepedia", size_mb=17563.2),
    ]
    kept, skipped = filter_repos(repos, mk_cfg())
    assert [r.name for r in kept] == ["dpdata"]
    assert set(skipped) == {"lammps", "sciencepedia"}
    assert all(isinstance(v, str) and v for v in skipped.values())


# --- fetch_repos ---

@pytest.mark.parametrize("use_responses", [True])
def test_fetch_repos_single_page(use_responses):
    """Parse single page of repos correctly."""
    import responses
    from contributors.repos import fetch_repos

    @responses.activate
    def run():
        # First page returns 2 repos
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=[
                {
                    "name": "dpdata",
                    "default_branch": "main",
                    "size": 2048,  # KB
                    "pushed_at": "2026-07-01T10:00:00Z",
                    "fork": False,
                    "archived": False,
                },
                {
                    "name": "DeePMD-kit",
                    "default_branch": "master",
                    "size": 4096,  # KB
                    "pushed_at": "2026-07-02T10:00:00Z",
                    "fork": False,
                    "archived": False,
                },
            ],
            status=200,
        )
        # Second page returns empty to stop pagination
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=[],
            status=200,
        )
        repos = fetch_repos("deepmodeling", "fake_token")
        assert len(repos) == 2
        assert repos[0].name == "dpdata"
        assert repos[0].default_branch == "main"
        assert repos[0].size_mb == 2.0  # 2048 KB / 1024
        assert repos[0].pushed_at == "2026-07-01T10:00:00Z"
        assert repos[0].is_fork is False
        assert repos[0].archived is False
        assert repos[1].name == "DeePMD-kit"
        assert repos[1].default_branch == "master"
        assert repos[1].size_mb == 4.0

    run()


@pytest.mark.parametrize("use_responses", [True])
def test_fetch_repos_pagination(use_responses):
    """Correctly handle pagination across multiple pages."""
    import responses
    from contributors.repos import fetch_repos

    @responses.activate
    def run():
        # First page: 100 repos
        first_page = [
            {
                "name": f"repo-{i}",
                "default_branch": "main",
                "size": 1024,
                "pushed_at": "2026-07-01T10:00:00Z",
                "fork": False,
                "archived": False,
            }
            for i in range(100)
        ]
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=first_page,
            status=200,
        )

        # Second page: 3 repos
        second_page = [
            {
                "name": f"repo-{i}",
                "default_branch": "main",
                "size": 1024,
                "pushed_at": "2026-07-01T10:00:00Z",
                "fork": False,
                "archived": False,
            }
            for i in range(100, 103)
        ]
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=second_page,
            status=200,
        )

        # Third page: empty
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=[],
            status=200,
        )

        repos = fetch_repos("deepmodeling", "fake_token")
        assert len(repos) == 103

    run()


@pytest.mark.parametrize("use_responses", [True])
def test_fetch_repos_fork_parent_lookup(use_responses):
    """Fetch parent info for forks when not in list endpoint."""
    import responses
    from contributors.repos import fetch_repos

    @responses.activate
    def run():
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=[
                {
                    "name": "fork-repo",
                    "default_branch": "main",
                    "size": 1024,
                    "pushed_at": "2026-07-01T10:00:00Z",
                    "fork": True,
                    "archived": False,
                    "url": "https://api.github.com/repos/deepmodeling/fork-repo",
                    # parent not in list endpoint
                },
            ],
            status=200,
        )

        # Mock the detailed repo endpoint
        responses.add(
            responses.GET,
            "https://api.github.com/repos/deepmodeling/fork-repo",
            json={
                "name": "fork-repo",
                "fork": True,
                "parent": {
                    "full_name": "upstream/repo",
                },
            },
            status=200,
        )

        # Second page: empty (to stop pagination)
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=[],
            status=200,
        )

        repos = fetch_repos("deepmodeling", "fake_token")
        assert len(repos) == 1
        assert repos[0].name == "fork-repo"
        assert repos[0].is_fork is True
        assert repos[0].upstream == "upstream/repo"
        assert repos[0].upstream_family == "external"

    run()


@pytest.mark.parametrize("use_responses", [True])
def test_fetch_repos_missing_fields_use_defaults(use_responses):
    """Fallback defaults for missing fields."""
    import responses
    from contributors.repos import fetch_repos

    @responses.activate
    def run():
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=[
                {
                    "name": "minimal-repo",
                    # no default_branch
                    # no size
                    # no pushed_at
                    "fork": False,
                    "archived": False,
                },
            ],
            status=200,
        )

        # Second page: empty
        responses.add(
            responses.GET,
            "https://api.github.com/orgs/deepmodeling/repos",
            json=[],
            status=200,
        )

        repos = fetch_repos("deepmodeling", "fake_token")
        assert len(repos) == 1
        assert repos[0].default_branch == "main"
        assert repos[0].size_mb == 0.0
        assert repos[0].pushed_at == "1970-01-01T00:00:00Z"

    run()


def test_fetch_repos_max_pages_limit(capsys):
    """Pagination stops at MAX_PAGES and prints warning."""
    import responses
    from contributors.repos import fetch_repos, MAX_PAGES

    @responses.activate
    def run():
        # Mock MAX_PAGES + 1 pages, all with 1 repo to keep it simple
        for page_num in range(1, MAX_PAGES + 2):
            responses.add(
                responses.GET,
                "https://api.github.com/orgs/deepmodeling/repos",
                json=[
                    {
                        "name": f"repo-page-{page_num}",
                        "default_branch": "main",
                        "size": 1024,
                        "pushed_at": "2026-07-01T10:00:00Z",
                        "fork": False,
                        "archived": False,
                    }
                ],
                status=200,
            )

        repos = fetch_repos("deepmodeling", "fake_token")
        # Should have exactly MAX_PAGES worth of repos (one per page)
        assert len(repos) == MAX_PAGES
        # Check that warning was printed
        captured = capsys.readouterr()
        assert "警告" in captured.err
        assert str(MAX_PAGES) in captured.err

    run()
