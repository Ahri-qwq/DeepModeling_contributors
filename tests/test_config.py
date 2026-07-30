from datetime import date, datetime, timezone
import pytest
from contributors.config import resolve_window, parse_args, DEFAULT_EXCLUDE_PATHS

TODAY = date(2026, 7, 28)


def test_default_window_is_one_year_back_to_today():
    since, until = resolve_window(None, None, None, TODAY)
    assert since == datetime(2025, 7, 28, tzinfo=timezone.utc)
    # 闭区间：含 2026-07-28 全天，故内部上界为次日零点
    assert until == datetime(2026, 7, 29, tzinfo=timezone.utc)


def test_explicit_since_overrides_default():
    since, until = resolve_window("2025-01-01", None, None, TODAY)
    assert since == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_closed_interval_includes_until_day_fully():
    since, until = resolve_window("2025-01-01", "2025-12-31", None, TODAY)
    assert until == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_months_shorthand_equals_since():
    since, _ = resolve_window(None, None, 6, TODAY)
    assert since == datetime(2026, 1, 28, tzinfo=timezone.utc)


def test_months_handles_month_end_rollback():
    # 3 月 31 日回退一个月应落到 2 月 28/29，而非报错
    since, _ = resolve_window(None, None, 1, date(2026, 3, 31))
    assert since == datetime(2026, 2, 28, tzinfo=timezone.utc)


def test_months_and_since_are_mutually_exclusive():
    with pytest.raises(ValueError, match="互斥"):
        resolve_window("2025-01-01", None, 6, TODAY)


def test_invalid_date_format_raises():
    with pytest.raises(ValueError, match="日期格式"):
        resolve_window("2025/01/01", None, None, TODAY)


def test_since_after_until_raises():
    with pytest.raises(ValueError, match="晚于"):
        resolve_window("2026-01-01", "2025-01-01", None, TODAY)


def test_parse_args_defaults_match_spec():
    c = parse_args([], TODAY)
    assert c.org == "deepmodeling"
    assert c.include_forks == "all"
    assert c.max_repo_size == 2048
    assert c.count_lines is False
    assert c.exclude_bots is False
    assert c.no_fetch is False
    assert c.refresh is False
    assert c.fmt == "all"
    assert c.jobs == 4
    assert c.verbose is False
    assert c.repos == []


def test_exclude_bots_flag_parsed():
    # Q9 裁决后语义反转：默认全部保留，需要时才显式排除
    assert parse_args(["--exclude-bots"], TODAY).exclude_bots is True


def test_parse_args_repos_splits_on_comma():
    c = parse_args(["--repos", "dpdata,DeePMD-kit"], TODAY)
    assert c.repos == ["dpdata", "DeePMD-kit"]


def test_parse_args_exclude_paths_extends_builtin():
    c = parse_args(["--exclude-paths", "docs/**"], TODAY)
    assert "docs/**" in c.exclude_paths
    for p in DEFAULT_EXCLUDE_PATHS:
        assert p in c.exclude_paths


def test_parse_args_count_lines_flag_enables():
    c = parse_args(["--count-lines"], TODAY)
    assert c.count_lines is True


def test_builtin_excludes_cover_generated_files():
    joined = " ".join(DEFAULT_EXCLUDE_PATHS)
    assert "*.lock" in joined
    assert "vendor/**" in joined
    assert "third_party/**" in joined
    assert "*.min.js" in joined
