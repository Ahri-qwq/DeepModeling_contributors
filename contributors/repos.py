"""仓库清单获取、fork 三分类、预筛。

fork 分类依据 spec 第 2.1 节的实测结论：
- self：上游属 DeepModeling 生态，是同社区跨账号迁移，应纳入
- external：真正的外部项目，全量统计会让上游开发者淹没本社区贡献者
- tooling：CI 流程工具 fork，上游作者列入表彰名单无意义
"""
import sys
from datetime import datetime, timezone
from typing import Optional

import requests

from .models import RepoInfo

# 上游账号属于 DeepModeling 生态
SELF_ORGS = {
    "deepmodeling",
    "abacusmodeling",
    "dptech-corp",
    "deepflamecfd",
    "mzjb",
}

# CI/流程工具类上游，按 full_name 精确匹配
TOOLING_UPSTREAMS = {
    "platisd/clang-tidy-pr-comments",
    "jitterbit/get-changed-files",
    "zjgemi/argo-workflows",
    "argoproj/argo-workflows",
    "deepmd-kit-recipes/deepmd-kit-recipes",
    "conda-forge/staged-recipes",
}

# 分页上限：100 页 × 100 个/页 = 1 万个仓库，远超任何真实组织
MAX_PAGES = 100


def classify_upstream(upstream: Optional[str]) -> Optional[str]:
    if not upstream:
        return None
    lower = upstream.lower()
    if lower in TOOLING_UPSTREAMS:
        return "tooling"
    owner = lower.split("/", 1)[0]
    if owner in SELF_ORGS:
        return "self"
    return "external"


def _parse_iso(s: str) -> datetime:
    # GitHub 返回 2026-07-27T14:34:09Z，fromisoformat 在 3.11+ 支持 Z
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def should_skip(repo: RepoInfo, cfg) -> Optional[str]:
    """返回跳过原因；不跳过返回 None。顺序即优先级。"""
    if cfg.repos and repo.name not in cfg.repos:
        return "不在 --repos 指定范围内"

    if repo.is_fork:
        if cfg.include_forks == "none":
            return "fork 已按 --include-forks=none 排除"
        if cfg.include_forks == "self" and repo.upstream_family != "self":
            return (
                f"fork 上游类型为 {repo.upstream_family}，"
                "已按 --include-forks=self 排除"
            )

    if repo.size_mb > cfg.max_repo_size:
        return (
            f"体积 {repo.size_mb:.0f} MB 超过 --max-repo-size "
            f"{cfg.max_repo_size} MB"
        )

    # pushed_at 早于起点 → 窗口内不可能有 commit，连克隆都省掉。
    # 用日期粒度比较，避免同日不同时刻被误跳。
    if _parse_iso(repo.pushed_at).date() < cfg.since.date():
        return "pushed_at 早于统计起点"

    return None


def filter_repos(repos: list, cfg) -> tuple:
    kept, skipped = [], {}
    for r in repos:
        reason = should_skip(r, cfg)
        if reason:
            skipped[r.name] = reason
        else:
            kept.append(r)
    return kept, skipped


def fetch_repos(org: str, token: str) -> list:
    """拉取组织全部仓库元数据。REST 比 GraphQL 简单且分页可靠。"""
    out, page = [], 1
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    while page <= MAX_PAGES:
        resp = requests.get(
            f"https://api.github.com/orgs/{org}/repos",
            params={"per_page": 100, "page": page, "type": "all"},
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for j in batch:
            upstream = (j.get("parent") or {}).get("full_name")
            # 列表端点不返回 parent，fork 的上游需单独查详情
            if j.get("fork") and not upstream:
                d = requests.get(
                    j["url"], headers=headers, timeout=30
                )
                d.raise_for_status()
                upstream = (d.json().get("parent") or {}).get("full_name")
            out.append(RepoInfo(
                name=j["name"],
                default_branch=j.get("default_branch") or "main",
                size_mb=round(j.get("size", 0) / 1024, 1),
                pushed_at=j.get("pushed_at") or "1970-01-01T00:00:00Z",
                is_fork=bool(j.get("fork")),
                upstream=upstream,
                upstream_family=classify_upstream(upstream),
                archived=bool(j.get("archived")),
            ))
        page += 1
    if page > MAX_PAGES:
        print(
            f"警告：仓库列表可能被截断。已达到 {MAX_PAGES} 页的上限。"
            f"组织 {org} 可能拥有超过 {MAX_PAGES * 100} 个仓库。",
            file=sys.stderr,
        )
    return out
