from pathlib import Path

import pytest

from contributors.config import DEFAULT_EXCLUDE_PATHS
from contributors.git_stats import RECORD_SEP, is_excluded, parse_git_log

FIXTURE = Path(__file__).parent / "fixtures" / "git_log_sample.txt"

# fixture 里 njzjz@example.com 出现两次（一次大写变体），应合并
NJZJZ = "njzjz@example.com"


def load_sample() -> str:
    # fixture 用 | 便于人读，实际 git 输出用 \x00 分隔
    return FIXTURE.read_text(encoding="utf-8").replace("|", RECORD_SEP)


# --- 基本解析 ---

def test_parses_all_four_commits():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert sum(s.commits for s in stats.values()) == 4


def test_groups_commits_by_email():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert stats["njzjz.bot@gmail.com"].commits == 1
    # 同一人两种大小写邮箱应归一后合并为 2
    assert stats[NJZJZ].commits == 2


def test_email_case_is_normalized():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert "Njzjz@Example.com" not in stats


def test_captures_author_names():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert "Jinzhe Zeng" in stats[NJZJZ].names


def test_records_email_in_stats():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert NJZJZ in stats[NJZJZ].emails


def test_commits_not_in_upstream_defaults_to_commits():
    # 非 fork 场景下两者相等；fork 由 count_upstream_excluded 单独覆盖
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert stats[NJZJZ].commits_not_in_upstream == stats[NJZJZ].commits


def test_empty_input_yields_empty_dict():
    assert parse_git_log("", count_lines=True, exclude_paths=[]) == {}


def test_commit_message_content_cannot_break_parsing():
    # 格式里不含 message，故任何 message 内容都不影响解析
    text = (f"C{RECORD_SEP}abc{RECORD_SEP}A B{RECORD_SEP}a@b.com"
            f"{RECORD_SEP}2026-01-01T00:00:00Z\n\n1\t0\tf.py\n")
    stats = parse_git_log(text, count_lines=True, exclude_paths=[])
    assert stats["a@b.com"].commits == 1


def test_malformed_header_line_is_skipped():
    # 字段不足的行不该让解析崩溃
    text = f"C{RECORD_SEP}onlytwo\n"
    assert parse_git_log(text, count_lines=False, exclude_paths=[]) == {}


def test_commit_with_empty_email_is_skipped():
    text = (f"C{RECORD_SEP}sha{RECORD_SEP}No Email{RECORD_SEP}"
            f"{RECORD_SEP}2026-01-01T00:00:00Z\n")
    assert parse_git_log(text, count_lines=False, exclude_paths=[]) == {}


def test_numstat_after_skipped_commit_does_not_leak_to_previous_author():
    """空邮箱提交之后的 numstat 行不能算到上一个作者头上。

    这是控制方实测发现的隐患：若解析时不把当前作者一并置空，
    被跳过提交的改动会错误累加给前一位贡献者。
    """
    text = (
        f"C{RECORD_SEP}s1{RECORD_SEP}Real Person{RECORD_SEP}real@x.com"
        f"{RECORD_SEP}2026-01-01T00:00:00Z\n\n10\t0\ta.py\n"
        f"C{RECORD_SEP}s2{RECORD_SEP}No Email{RECORD_SEP}{RECORD_SEP}"
        f"2026-01-02T00:00:00Z\n\n999\t999\tb.py\n"
    )
    stats = parse_git_log(text, count_lines=True, exclude_paths=[])
    assert stats["real@x.com"].additions == 10
    assert stats["real@x.com"].additions_raw == 10


# --- 行数统计 ---

def test_line_counts_none_when_disabled():
    stats = parse_git_log(load_sample(), count_lines=False,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    assert stats[NJZJZ].additions is None
    assert stats[NJZJZ].additions_raw is None
    assert stats[NJZJZ].files_changed is None


def test_raw_counts_include_generated_files():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    # 4 + 120(package-lock) + 312，二进制 - 不计
    assert stats[NJZJZ].additions_raw == 436


def test_filtered_counts_exclude_generated_files():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    # 排除 package-lock.json(120) 与 logo.png(二进制)
    assert stats[NJZJZ].additions == 316
    assert stats[NJZJZ].deletions == 1


def test_binary_files_do_not_crash_parser():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    # docs/logo.png 显示为 - -，不计入行数
    assert stats[NJZJZ].additions is not None


def test_files_changed_deduplicates():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    # outcar.py 与 test_serialization.py 各一次，共 2 个非排除文件
    assert stats[NJZJZ].files_changed == 2


def test_files_changed_counts_per_author_not_globally():
    stats = parse_git_log(load_sample(), count_lines=True,
                          exclude_paths=DEFAULT_EXCLUDE_PATHS)
    # njzjz-bot 改了 2 个文件，与 njzjz 的 2 个互不干扰
    assert stats["njzjz.bot@gmail.com"].files_changed == 2


def test_same_file_touched_twice_counts_once():
    text = (
        f"C{RECORD_SEP}s1{RECORD_SEP}A{RECORD_SEP}a@x.com"
        f"{RECORD_SEP}2026-01-01T00:00:00Z\n\n5\t0\tsame.py\n"
        f"C{RECORD_SEP}s2{RECORD_SEP}A{RECORD_SEP}a@x.com"
        f"{RECORD_SEP}2026-01-02T00:00:00Z\n\n7\t0\tsame.py\n"
    )
    stats = parse_git_log(text, count_lines=True, exclude_paths=[])
    assert stats["a@x.com"].files_changed == 1
    assert stats["a@x.com"].additions == 12


def test_numstat_ignored_when_count_lines_disabled():
    text = (f"C{RECORD_SEP}s{RECORD_SEP}A{RECORD_SEP}a@x.com"
            f"{RECORD_SEP}2026-01-01T00:00:00Z\n\n50\t50\tf.py\n")
    stats = parse_git_log(text, count_lines=False, exclude_paths=[])
    assert stats["a@x.com"].additions is None


def test_path_with_tab_is_preserved():
    # git 会对含特殊字符的路径加引号，但制表符分割不该截断路径
    text = (f"C{RECORD_SEP}s{RECORD_SEP}A{RECORD_SEP}a@x.com"
            f"{RECORD_SEP}2026-01-01T00:00:00Z\n\n1\t0\tdir/sub\tfile.py\n")
    stats = parse_git_log(text, count_lines=True, exclude_paths=[])
    assert stats["a@x.com"].files_changed == 1


# --- 排除模式 ---

@pytest.mark.parametrize("path", [
    "package-lock.json",
    "poetry.lock",
    "vendor/lib/x.js",
    "third_party/foo/bar.c",
    "assets/app.min.js",
    "data/train.npy",
])
def test_excluded_paths_match(path):
    assert is_excluded(path, DEFAULT_EXCLUDE_PATHS) is True


@pytest.mark.parametrize("path", [
    "dpdata/plugins/cp2k.py",
    "tests/test_serialization.py",
    "README.md",
])
def test_source_paths_not_excluded(path):
    assert is_excluded(path, DEFAULT_EXCLUDE_PATHS) is False


def test_nested_vendor_matches_double_star():
    assert is_excluded("a/b/vendor/x.js", DEFAULT_EXCLUDE_PATHS) is True


def test_windows_backslash_path_is_normalized():
    assert is_excluded("vendor\\lib\\x.js", DEFAULT_EXCLUDE_PATHS) is True


def test_empty_pattern_list_excludes_nothing():
    assert is_excluded("package-lock.json", []) is False


# --- 克隆类型自动选择（实测：blobless 库上 --numstat 约 2 分钟/仓库，
#     完整克隆仅 0.16 秒；不统计行数时 blobless 体积仅 1/7）---

class FakeCache:
    """记录调用序列的假缓存，用于验证 clone_or_fetch 的决策而不碰网络。"""

    def __init__(self, tmp_path, cloned=False):
        self.root = tmp_path
        self._cloned = cloned
        self.discarded = []

    def repo_path(self, name):
        return self.root / f"{name}.git"

    def is_corrupt(self, name):
        return False

    def is_cloned(self, name):
        return self._cloned

    def discard(self, name):
        self.discarded.append(name)
        self._cloned = False

    def needs_fetch(self, repo, refresh, no_fetch):
        return False

    def record_fetch(self, name, pushed_at):
        pass


def _cfg(**kw):
    from contributors.config import Config
    from datetime import datetime, timezone
    base = dict(org="deepmodeling",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 7, 29, tzinfo=timezone.utc),
                include_forks="all", max_repo_size=2048,
                exclude_paths=list(DEFAULT_EXCLUDE_PATHS))
    base.update(kw)
    return Config(**base)


def _repo():
    from contributors.models import RepoInfo
    return RepoInfo(name="dpdata", default_branch="main", size_mb=20.0,
                    pushed_at="2026-07-01T00:00:00Z", is_fork=False,
                    upstream=None, upstream_family=None, archived=False)


def test_clone_uses_blob_filter_when_not_counting_lines(tmp_path, monkeypatch):
    import contributors.git_stats as gs
    calls = []
    monkeypatch.setattr(gs, "_run_git",
                        lambda args, **kw: calls.append(args) or "")
    gs.clone_or_fetch(_repo(), FakeCache(tmp_path), _cfg(count_lines=False))
    assert any("--filter=blob:none" in c for c in calls)


def test_clone_omits_blob_filter_when_counting_lines(tmp_path, monkeypatch):
    import contributors.git_stats as gs
    calls = []
    monkeypatch.setattr(gs, "_run_git",
                        lambda args, **kw: calls.append(args) or "")
    gs.clone_or_fetch(_repo(), FakeCache(tmp_path), _cfg(count_lines=True))
    clone = [c for c in calls if c and c[0] == "clone"][0]
    assert "--filter=blob:none" not in clone


def test_existing_blobless_cache_rebuilt_when_counting_lines(tmp_path, monkeypatch):
    """已有 blobless 缓存 + 要统计行数 → 必须重新完整克隆。"""
    import contributors.git_stats as gs
    cm = FakeCache(tmp_path, cloned=True)
    monkeypatch.setattr(gs, "is_blobless", lambda p: True)
    monkeypatch.setattr(gs, "_run_git", lambda args, **kw: "")
    gs.clone_or_fetch(_repo(), cm, _cfg(count_lines=True))
    assert cm.discarded == ["dpdata"]


def test_existing_blobless_cache_kept_when_not_counting_lines(tmp_path, monkeypatch):
    import contributors.git_stats as gs
    cm = FakeCache(tmp_path, cloned=True)
    monkeypatch.setattr(gs, "is_blobless", lambda p: True)
    monkeypatch.setattr(gs, "_run_git", lambda args, **kw: "")
    gs.clone_or_fetch(_repo(), cm, _cfg(count_lines=False))
    assert cm.discarded == []


def test_full_cache_not_rebuilt_when_counting_lines(tmp_path, monkeypatch):
    import contributors.git_stats as gs
    cm = FakeCache(tmp_path, cloned=True)
    monkeypatch.setattr(gs, "is_blobless", lambda p: False)
    monkeypatch.setattr(gs, "_run_git", lambda args, **kw: "")
    gs.clone_or_fetch(_repo(), cm, _cfg(count_lines=True))
    assert cm.discarded == []


def test_blobless_cache_with_no_fetch_and_count_lines_raises(tmp_path, monkeypatch):
    """无法重建又要行数统计时，必须明确报错而不是静默给出错误数字。"""
    import contributors.git_stats as gs
    cm = FakeCache(tmp_path, cloned=True)
    monkeypatch.setattr(gs, "is_blobless", lambda p: True)
    monkeypatch.setattr(gs, "_run_git", lambda args, **kw: "")
    with pytest.raises(gs.GitError, match="--no-fetch"):
        gs.clone_or_fetch(_repo(), cm, _cfg(count_lines=True, no_fetch=True))


def test_missing_cache_with_no_fetch_raises(tmp_path, monkeypatch):
    import contributors.git_stats as gs
    monkeypatch.setattr(gs, "_run_git", lambda args, **kw: "")
    with pytest.raises(gs.GitError, match="无本地缓存"):
        gs.clone_or_fetch(_repo(), FakeCache(tmp_path), _cfg(no_fetch=True))


def test_is_blobless_returns_false_when_config_key_absent(tmp_path, monkeypatch):
    import contributors.git_stats as gs

    def raise_git_error(args, **kw):
        raise gs.GitError("exit code 1")

    monkeypatch.setattr(gs, "_run_git", raise_git_error)
    assert gs.is_blobless(tmp_path) is False


def test_record_sep_is_not_nul(tmp_path):
    """Windows 的 CreateProcess 不允许参数含 NUL，实测会抛 ValueError。"""
    assert "\x00" not in RECORD_SEP
    from contributors.git_stats import LOG_FORMAT
    assert "\x00" not in LOG_FORMAT
