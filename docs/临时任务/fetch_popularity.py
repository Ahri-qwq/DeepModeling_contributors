"""采集 GitHub 仓库流行度指标:star/fork/watch、release 下载量。

产出:
- docs/临时任务/popularity_report.md
- docs/临时任务/popularity_report.csv
"""
import csv
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

from contributors.auth import get_token


@dataclass
class RepoPopularity:
    """单个仓库的流行度指标"""
    repo_full_name: str
    stars: int
    forks: int
    watchers: int
    open_issues: int
    size_kb: int
    created_at: str
    updated_at: str
    pushed_at: str
    default_branch: str
    license: str
    homepage: str
    description: str


@dataclass
class Release:
    """单个 release 的记录"""
    repo_full_name: str
    tag_name: str
    name: str
    published_at: str
    draft: bool
    prerelease: bool
    download_count: int  # 所有 assets 总下载量
    assets_count: int


# 目标仓库清单
REPOS = [
    "deepmodeling/deepmd-kit",  # 含 DPA-1/2/4
    "deepmodeling/dpgen",
    "dptech-corp/Uni-Dock",
    "deepmodeling/DMFF",
    "deepmodeling/Uni-Mol",
    "deepmodeling/Uni-Fold",
    "dptech-corp/Uni-Fold",
]


def fetch_repo_info(repo: str, token: str) -> Optional[RepoPopularity]:
    """拉取单个仓库元数据"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return RepoPopularity(
            repo_full_name=repo,
            stars=data.get("stargazers_count", 0),
            forks=data.get("forks_count", 0),
            watchers=data.get("subscribers_count", 0),
            open_issues=data.get("open_issues_count", 0),
            size_kb=data.get("size", 0),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            pushed_at=data.get("pushed_at", ""),
            default_branch=data.get("default_branch", "main"),
            license=(data.get("license") or {}).get("spdx_id", ""),
            homepage=data.get("homepage", ""),
            description=(data.get("description") or "").strip(),
        )
    except Exception as exc:
        print(f"拉取 {repo} 元数据失败:{exc}", file=sys.stderr)
        return None


def fetch_releases(repo: str, token: str) -> list[Release]:
    """拉取仓库所有 release(分页最多 300 条,超过的概率极低)"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    out = []
    page = 1
    while page <= 3:  # 100 条/页 × 3 = 300 条
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/releases",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for r in batch:
                assets = r.get("assets", [])
                download_count = sum(a.get("download_count", 0) for a in assets)
                out.append(Release(
                    repo_full_name=repo,
                    tag_name=r.get("tag_name", ""),
                    name=r.get("name", ""),
                    published_at=r.get("published_at", ""),
                    draft=bool(r.get("draft")),
                    prerelease=bool(r.get("prerelease")),
                    download_count=download_count,
                    assets_count=len(assets),
                ))
            page += 1
        except Exception as exc:
            print(f"拉取 {repo} releases 失败(page {page}):{exc}", file=sys.stderr)
            break
    return out


def write_markdown(repos_data: list[RepoPopularity], releases_data: list[Release], out_path: str):
    """生成 Markdown 报告"""
    lines = [
        "# GitHub 仓库流行度指标报告",
        "",
        f"> 采集时间:{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "## 仓库概览",
        "",
        "| 仓库 | Stars | Forks | Watchers | Open Issues | 创建时间 | 最后推送 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in repos_data:
        created = r.created_at[:10] if r.created_at else ""
        pushed = r.pushed_at[:10] if r.pushed_at else ""
        lines.append(
            f"| [{r.repo_full_name}](https://github.com/{r.repo_full_name}) "
            f"| {r.stars:,} | {r.forks:,} | {r.watchers:,} | {r.open_issues} "
            f"| {created} | {pushed} |"
        )

    lines.extend(["", "## Release 下载量统计", ""])

    repo_release_map = {}
    for rel in releases_data:
        repo_release_map.setdefault(rel.repo_full_name, []).append(rel)

    for repo in REPOS:
        rels = repo_release_map.get(repo, [])
        if not rels:
            lines.append(f"### {repo}")
            lines.append("")
            lines.append("无 release 记录")
            lines.append("")
            continue

        lines.append(f"### {repo}")
        lines.append("")
        lines.append("| Tag | 名称 | 发布时间 | 下载量 | Assets 数 | Draft | Prerelease |")
        lines.append("|---|---|---|---|---|---|---|")
        for rel in rels:
            published = rel.published_at[:10] if rel.published_at else ""
            draft_mark = "✓" if rel.draft else ""
            pre_mark = "✓" if rel.prerelease else ""
            name = rel.name or "(无标题)"
            lines.append(
                f"| {rel.tag_name} | {name} | {published} "
                f"| {rel.download_count:,} | {rel.assets_count} "
                f"| {draft_mark} | {pre_mark} |"
            )
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_csv(repos_data: list[RepoPopularity], releases_data: list[Release], out_dir: str):
    """生成 CSV:repos.csv 和 releases.csv"""
    repos_csv = f"{out_dir}/popularity_repos.csv"
    with open(repos_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(repos_data[0]).keys()))
        writer.writeheader()
        for r in repos_data:
            writer.writerow(asdict(r))

    if releases_data:
        releases_csv = f"{out_dir}/popularity_releases.csv"
        with open(releases_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(releases_data[0]).keys()))
            writer.writeheader()
            for r in releases_data:
                writer.writerow(asdict(r))


def main():
    token = get_token()
    print("开始采集仓库流行度指标...")

    repos_data = []
    for repo in REPOS:
        print(f"拉取 {repo} 元数据...")
        info = fetch_repo_info(repo, token)
        if info:
            repos_data.append(info)

    print()
    releases_data = []
    for repo in REPOS:
        print(f"拉取 {repo} releases...")
        rels = fetch_releases(repo, token)
        releases_data.extend(rels)
        print(f"  -> {len(rels)} 条 release 记录")

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = "docs/临时任务"
    md_path = f"{out_dir}/popularity_report_{timestamp}.md"
    write_markdown(repos_data, releases_data, md_path)

    repos_csv = f"{out_dir}/popularity_repos_{timestamp}.csv"
    releases_csv = f"{out_dir}/popularity_releases_{timestamp}.csv"

    with open(repos_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(repos_data[0]).keys()))
        writer.writeheader()
        for r in repos_data:
            writer.writerow(asdict(r))

    if releases_data:
        with open(releases_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(releases_data[0]).keys()))
            writer.writeheader()
            for r in releases_data:
                writer.writerow(asdict(r))

    print()
    print(f"采集完成。产出:")
    print(f"  - {md_path}")
    print(f"  - {repos_csv}")
    print(f"  - {releases_csv}")


if __name__ == "__main__":
    main()
