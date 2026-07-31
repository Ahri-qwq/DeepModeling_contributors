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
    # 宽松口径：主干 + PR 分支（主干不可达）上的提交。
    # 与 commits 并列而非替代 —— 两者口径各自恒定，同一行内可直接比较，
    # 跨行排序也不会混淆量纲。
    #
    # 差值 commits_loose - commits 是该邮箱在 PR 分支上的提交数。用 squash
    # 合并时同一份工作两处各有一条记录（PR 分支上是原始提交，主干上是新
    # SHA 的 squash 提交），所以宽松口径含重复计数，不能当"真实工作量"。
    #
    # 反过来 commits 也会漏人：squash 后主干 author 是账号绑定的 noreply
    # 邮箱，用其他邮箱署名的身份主干上可能一次提交都没有（实测 Zheyong Fan
    # 主干 0、PR 分支 206）。两个字段都给出，由使用方按用途选口径。
    commits_loose: int = 0
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
