"""本地缓存管理、增量判断、断点续跑。

设计要点：git 历史缓存在本地长期有效，改时间窗时零网络重算；
API 缓存按天，因为 GraphQL 查的是当前状态。
"""
import json
import os
import shutil
import stat
from pathlib import Path

from .models import RepoInfo

# blobless 裸库实际占用远小于 GitHub 报告的 size，按经验取 0.6 并留 20% 余量
_SIZE_FACTOR = 0.6
_HEADROOM = 1.2


class CacheError(RuntimeError):
    """缓存目录不可用等需要用户干预的错误。"""


class CacheManager:
    def __init__(self, cache_dir: str) -> None:
        self.root = Path(cache_dir)
        self.repos_dir = self.root / "repos"
        self.api_dir = self.root / "api"
        self.progress_path = self.root / "progress.json"
        self._meta_path = self.root / "fetch_meta.json"

    # --- 路径 ---

    def repo_path(self, name: str) -> Path:
        return self.repos_dir / f"{name}.git"

    def api_path(self, name: str, day: str) -> Path:
        return self.api_dir / f"{name}-{day}.json"

    # --- 状态判断 ---

    def is_cloned(self, name: str) -> bool:
        d = self.repo_path(name)
        return ((d / "objects").is_dir() and (d / "refs").is_dir()
                and (d / "HEAD").is_file())

    def is_corrupt(self, name: str) -> bool:
        """目录存在但裸库标志不全 —— 通常是上次被 Ctrl-C 打断。

        注意与"从未克隆"区分：后者返回 False，因为它不需要先删除重建。
        """
        if not self.repo_path(name).exists():
            return False
        return not self.is_cloned(name)

    def discard(self, name: str) -> None:
        """删除某仓库的缓存。

        不能用 ignore_errors=True：Windows 上 git 的 pack 文件带只读位，
        rmtree 会部分失败而错误被静默吞掉，留下一个含 objects 的空壳目录，
        使后续 clone 报 "destination path already exists"（实测确认）。
        故用 onexc 回调清除只读位后重试。
        """
        path = self.repo_path(name)
        if not path.exists():
            return

        def _clear_readonly(func, target, _exc):
            os.chmod(target, stat.S_IWRITE)
            func(target)

        shutil.rmtree(path, onexc=_clear_readonly)

    def needs_fetch(self, repo: RepoInfo, refresh: bool, no_fetch: bool) -> bool:
        # --no-fetch 优先于 --refresh：用户明确要求零网络时不碰网络
        if no_fetch:
            return False
        if refresh:
            return True
        if not self.is_cloned(repo.name):
            return True
        last = self._load_meta().get(repo.name, {}).get("pushed_at")
        # pushed_at 未变说明远端无新提交，连 fetch 都省掉
        return last != repo.pushed_at

    # --- 元数据 ---

    def _load_meta(self) -> dict:
        return self._read_json(self._meta_path)

    def record_fetch(self, name: str, pushed_at: str) -> None:
        meta = self._load_meta()
        meta[name] = {"pushed_at": pushed_at}
        self._write_json(self._meta_path, meta)

    # --- 断点续跑 ---

    def load_progress(self) -> dict:
        return self._read_json(self.progress_path)

    def save_progress(self, data: dict) -> None:
        self._write_json(self.progress_path, data)

    # --- 磁盘 ---

    def estimate_needed_mb(self, repos: list) -> float:
        pending = [r for r in repos if not self.is_cloned(r.name)]
        return sum(r.size_mb for r in pending) * _SIZE_FACTOR * _HEADROOM

    def free_space_mb(self) -> float:
        """返回缓存目录所在位置的可用空间。

        缓存目录常常尚未创建，故逐级上溯到已存在的父目录再查询。
        但上溯到根仍不存在时（如 --cache-dir Z:/cache 而 Z 盘未挂载），
        Path("Z:/").parent == Path("Z:/") 使循环退出，disk_usage 会抛
        裸的 FileNotFoundError。这里转成带指引的 CacheError。
        """
        probe = self.root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            return shutil.disk_usage(probe).free / (1024 * 1024)
        except OSError as exc:
            raise CacheError(
                f"缓存目录所在位置不可用：{self.root}。"
                "请确认该盘符存在且已挂载（U 盘、网络驱动器需先连接），"
                "或用 --cache-dir 指定其他位置。"
            ) from exc

    # --- 内部工具 ---

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 缓存损坏不该让整次运行失败，退化为空
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
