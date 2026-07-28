from contributors.models import RepoInfo, GitStats, ApiStats, Contributor


def test_repoinfo_holds_fork_metadata():
    r = RepoInfo(
        name="abacus-develop",
        default_branch="develop",
        size_mb=165.3,
        pushed_at="2026-07-27T00:00:00Z",
        is_fork=True,
        upstream="abacusmodeling/abacus-develop",
        upstream_family="self",
        archived=False,
    )
    assert r.name == "abacus-develop"
    assert r.is_fork is True
    assert r.upstream_family == "self"


def test_gitstats_defaults_to_zero():
    g = GitStats()
    assert g.commits == 0
    assert g.commits_not_in_upstream == 0
    assert g.additions is None  # None 表示未开启 --count-lines
    assert g.emails == set()


def test_apistats_defaults_to_zero():
    a = ApiStats()
    assert a.pr_created == 0
    assert a.pr_merged == 0
    assert a.pr_reviewed == 0
    assert a.issue_created == 0
    assert a.issue_commented == 0


def test_contributor_github_url_derived_from_login():
    c = Contributor(login="njzjz", name="Jinzhe Zeng")
    assert c.github_url == "https://github.com/njzjz"


def test_contributor_without_login_has_empty_url():
    c = Contributor(login=None, name="Anon")
    assert c.github_url == ""
