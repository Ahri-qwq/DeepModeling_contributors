"""共享数据结构。放在独立模块避免 git_stats 与 api_stats 循环导入。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RepoInfo:
    """单个仓库的元数据，来自 REST API。"""
    name: str
    default_branch: str
    size_mb: float
    pushed_at: str          # ISO8601 UTC 字符串，如 2026-07-27T00:00:00Z
    is_fork: bool
    upstream: Optional[str]        # 上游 full_name，非 fork 时为 None
    upstream_family: Optional[str]  # self / external / tooling，非 fork 时为 None
    archived: bool


@dataclass
class GitStats:
    """某人在某仓库的 git 侧统计。"""
    commits: int = 0
    commits_not_in_upstream: int = 0
    emails: set = field(default_factory=set)
    names: set = field(default_factory=set)
    # 以下仅在 --count-lines 开启时填充，否则保持 None（输出时整列省略）
    additions: Optional[int] = None
    deletions: Optional[int] = None
    files_changed: Optional[int] = None
    additions_raw: Optional[int] = None
    deletions_raw: Optional[int] = None


@dataclass
class ApiStats:
    """某人在某仓库的 API 侧统计。"""
    pr_created: int = 0
    pr_merged: int = 0
    pr_reviewed: int = 0
    issue_created: int = 0
    issue_commented: int = 0


@dataclass
class Contributor:
    """归并后的一个人。"""
    login: Optional[str]
    name: str = ""
    is_bot: bool = False

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.login}" if self.login else ""
