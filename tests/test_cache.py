from datetime import datetime, timezone

import pytest

from contributors.cache import CacheError, CacheManager
from contributors.models import RepoInfo


def mk_repo(name="dpdata", pushed="2026-07-01T00:00:00Z", size_mb=10.0):
    return RepoInfo(name=name, default_branch="main", size_mb=size_mb,
                    pushed_at=pushed, is_fork=False, upstream=None,
                    upstream_family=None, archived=False)


def make_bare(cm, name="dpdata"):
    """造一个看起来完整的裸库骨架。"""
    d = cm.repo_path(name)
    (d / "objects").mkdir(parents=True)
    (d / "refs").mkdir()
    (d / "HEAD").write_text("ref: refs/heads/main")
    return d


# --- 路径 ---

def test_repo_path_uses_bare_git_suffix(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.repo_path("dpdata").name == "dpdata.git"


def test_api_path_includes_day(tmp_path):
    cm = CacheManager(str(tmp_path))
    p = cm.api_path("dpdata", "2026-07-28")
    assert "dpdata" in p.name and "2026-07-28" in p.name


# --- 克隆状态判断 ---

def test_not_cloned_when_dir_absent(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.is_cloned("dpdata") is False


def test_cloned_when_bare_repo_markers_present(tmp_path):
    cm = CacheManager(str(tmp_path))
    make_bare(cm)
    assert cm.is_cloned("dpdata") is True
    assert cm.is_corrupt("dpdata") is False


def test_corrupt_when_markers_incomplete(tmp_path):
    # 模拟上次 Ctrl-C 打断留下的半成品
    cm = CacheManager(str(tmp_path))
    (cm.repo_path("dpdata") / "objects").mkdir(parents=True)
    assert cm.is_corrupt("dpdata") is True


def test_absent_dir_is_not_corrupt(tmp_path):
    # 从未克隆 != 损坏，两者的处理方式不同
    cm = CacheManager(str(tmp_path))
    assert cm.is_corrupt("dpdata") is False


def test_discard_removes_cached_repo(tmp_path):
    cm = CacheManager(str(tmp_path))
    make_bare(cm)
    cm.discard("dpdata")
    assert cm.repo_path("dpdata").exists() is False


def test_discard_on_missing_repo_does_not_raise(tmp_path):
    cm = CacheManager(str(tmp_path))
    cm.discard("never-existed")


def test_discard_removes_readonly_files(tmp_path):
    """git 的 pack 文件带只读位，Windows 上 rmtree 会失败。

    实测：用 ignore_errors=True 时错误被静默吞掉，留下含 objects 的
    空壳目录，导致后续 clone 报 destination already exists。
    """
    import os
    import stat

    cm = CacheManager(str(tmp_path))
    d = make_bare(cm)
    pack = d / "objects" / "pack-abc.pack"
    pack.write_text("fake pack")
    os.chmod(pack, stat.S_IREAD)

    cm.discard("dpdata")
    assert d.exists() is False, "只读文件导致缓存目录残留，clone 会失败"


# --- 增量判断 ---

def test_needs_fetch_true_when_never_cloned(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.needs_fetch(mk_repo(), refresh=False, no_fetch=False) is True


def test_no_fetch_flag_always_returns_false(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.needs_fetch(mk_repo(), refresh=False, no_fetch=True) is False


def test_refresh_flag_forces_fetch(tmp_path):
    cm = CacheManager(str(tmp_path))
    cm.record_fetch("dpdata", "2026-07-01T00:00:00Z")
    assert cm.needs_fetch(mk_repo(), refresh=True, no_fetch=False) is True


def test_no_fetch_outranks_refresh(tmp_path):
    # 两个开关都给时，--no-fetch 优先（用户明确要求零网络）
    cm = CacheManager(str(tmp_path))
    assert cm.needs_fetch(mk_repo(), refresh=True, no_fetch=True) is False


def test_skips_fetch_when_pushed_at_unchanged(tmp_path):
    cm = CacheManager(str(tmp_path))
    make_bare(cm)
    cm.record_fetch("dpdata", "2026-07-01T00:00:00Z")
    assert cm.needs_fetch(mk_repo(pushed="2026-07-01T00:00:00Z"),
                          refresh=False, no_fetch=False) is False


def test_fetches_when_pushed_at_advanced(tmp_path):
    cm = CacheManager(str(tmp_path))
    make_bare(cm)
    cm.record_fetch("dpdata", "2026-07-01T00:00:00Z")
    assert cm.needs_fetch(mk_repo(pushed="2026-07-20T00:00:00Z"),
                          refresh=False, no_fetch=False) is True


def test_fetches_when_no_recorded_pushed_at(tmp_path):
    # 已克隆但无 fetch 记录（例如手工放进来的缓存）→ 保守起见要 fetch
    cm = CacheManager(str(tmp_path))
    make_bare(cm)
    assert cm.needs_fetch(mk_repo(), refresh=False, no_fetch=False) is True


def test_record_fetch_isolates_repos(tmp_path):
    cm = CacheManager(str(tmp_path))
    cm.record_fetch("a", "2026-01-01T00:00:00Z")
    cm.record_fetch("b", "2026-02-02T00:00:00Z")
    make_bare(cm, "a")
    make_bare(cm, "b")
    assert cm.needs_fetch(mk_repo("a", pushed="2026-01-01T00:00:00Z"),
                          refresh=False, no_fetch=False) is False
    assert cm.needs_fetch(mk_repo("b", pushed="2026-09-09T00:00:00Z"),
                          refresh=False, no_fetch=False) is True


# --- 断点续跑 ---

def test_progress_roundtrip(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.load_progress() == {}
    cm.save_progress({"done": ["dpdata"]})
    assert cm.load_progress() == {"done": ["dpdata"]}


def test_progress_survives_corrupt_json(tmp_path):
    # 上次被打断可能留下截断的 JSON，不该让整次运行失败
    cm = CacheManager(str(tmp_path))
    cm.progress_path.parent.mkdir(parents=True, exist_ok=True)
    cm.progress_path.write_text("{not json")
    assert cm.load_progress() == {}


def test_progress_preserves_non_ascii(tmp_path):
    # 跳过原因等中文内容要能原样存取
    cm = CacheManager(str(tmp_path))
    cm.save_progress({"skipped": {"lammps": "pushed_at 早于统计起点"}})
    assert cm.load_progress()["skipped"]["lammps"] == "pushed_at 早于统计起点"


# --- 磁盘估算 ---

def test_estimate_needed_mb_sums_uncloned_only(tmp_path):
    cm = CacheManager(str(tmp_path))
    repos = [mk_repo("a", size_mb=100.0), mk_repo("b", size_mb=200.0)]
    assert cm.estimate_needed_mb(repos) > 0


def test_estimate_excludes_already_cloned(tmp_path):
    cm = CacheManager(str(tmp_path))
    repos = [mk_repo("a", size_mb=100.0), mk_repo("b", size_mb=200.0)]
    before = cm.estimate_needed_mb(repos)
    make_bare(cm, "a")
    after = cm.estimate_needed_mb(repos)
    assert after < before


def test_estimate_zero_when_all_cloned(tmp_path):
    cm = CacheManager(str(tmp_path))
    make_bare(cm, "a")
    assert cm.estimate_needed_mb([mk_repo("a", size_mb=100.0)]) == 0


def test_free_space_mb_is_positive(tmp_path):
    cm = CacheManager(str(tmp_path))
    assert cm.free_space_mb() > 0


def test_free_space_probes_upward_for_missing_dirs(tmp_path):
    # 缓存目录尚未创建时也要能估算，靠逐级上溯到已存在的父目录
    cm = CacheManager(str(tmp_path / "a" / "b" / "c" / "not_yet"))
    assert cm.free_space_mb() > 0


def test_free_space_raises_cache_error_on_unavailable_location(tmp_path, monkeypatch):
    """盘符不存在时（U 盘已拔/网络盘未挂载）须给出可操作提示。

    实测：Path("Z:/").parent == Path("Z:/")，上溯循环退出后 probe 仍是
    不存在的根，shutil.disk_usage 抛裸的 FileNotFoundError [WinError 3]。
    用 monkeypatch 稳定复现，避免依赖测试机是否真有 Z 盘。
    """
    import shutil as _shutil

    cm = CacheManager("Z:/nonexistent_drive_xyz/cache")

    def boom(_path):
        raise FileNotFoundError(3, "系统找不到指定的路径。")

    monkeypatch.setattr(_shutil, "disk_usage", boom)
    with pytest.raises(CacheError) as exc:
        cm.free_space_mb()
    # Path 在 Windows 上会把 / 规范化成 \，故比较时统一分隔符
    msg = str(exc.value).replace("\\", "/")
    assert "Z:/nonexistent_drive_xyz/cache" in msg


def test_free_space_error_mentions_how_to_fix(tmp_path, monkeypatch):
    import shutil as _shutil

    cm = CacheManager("Z:/gone/cache")
    monkeypatch.setattr(
        _shutil, "disk_usage",
        lambda _p: (_ for _ in ()).throw(OSError("device not ready")),
    )
    with pytest.raises(CacheError, match="--cache-dir"):
        cm.free_space_mb()
