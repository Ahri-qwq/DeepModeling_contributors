"""仓库清单获取、fork 三分类、预筛。

fork 分类依据 spec 第 2.1 节的实测结论：
- self：上游属 DeepModeling 生态，是同社区跨账号迁移，应纳入
- external：真正的外部项目，全量统计会让上游开发者淹没本社区贡献者
- tooling：CI 流程工具 fork，上游作者列入表彰名单无意义
"""
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .models import RepoInfo

log = logging.getLogger(__name__)

# 上游账号属于 DeepModeling 生态。判定按 owner 小写匹配（classify_upstream）。
#
# 前四个是组织自己的账号。brucefan1983 是个人账号，收录理由不同：
# GPUMD（fork 自 brucefan1983/GPUMD）与社区有正式合作——社区为它办付费
# 课程、走立项审批，2026-11 的周会记录显示已完成两个季度的课程结算，
# 与武汉大学协同项目、育人项目并列为同类事务。
#
# 这条是对第一期判断的修正。2026-07-30 曾确认"只有 GPUMD 算外部项目"
# （见 docs/第一期工程/2026-07-29-fork清单待确认.md），依据是
# commits_not_in_upstream=0——fork 里没有社区独立开发的代码。那个技术
# 指标至今仍然成立，但它只能回答"代码是不是社区自己写的"，回答不了
# "这个项目与社区是什么关系"。当时那份文档自己留了口子：「若社区与这些
# 上游有正式协作关系，可以把对应账号加入白名单」。现在条件满足了。
#
# 注意：归为 self 只是让分类语义正确（不再被 --include-forks=self 误伤）。
# GPUMD 的提交绝大多数仍来自上游作者，对外做表彰名单时该用
# commits_not_in_upstream 这一列区分"社区独立开发"与"上游带入"。
SELF_ORGS = {
    "deepmodeling",
    "abacusmodeling",
    "dptech-corp",
    "deepflamecfd",
    "mzjb",
    "brucefan1983",
}

# 按仓库名硬排除的名单。值是写进日志的理由，各仓库原因不同，不能共用一句话。
# 优先级高于 --repos 白名单：显式指定也不会纳入。
#
# .github：组织元仓库，存 profile README / issue 模板 / github-metrics bot
# 自动提交。无真人贡献（实测一年 5583 条提交全是 bot、author_login 为空），
# 却因定时任务 pushed_at 长期最新而稳居活跃榜首，让外部读者困惑。
#
# lammps：fork 自 lammps/lammps 的 external fork。2026-10-15 上游同步使
# pushed_at 跳进一年窗口，于是被纳入统计并开始尝试全量克隆，此后连续失败
# 54 次（见下方「复查条件」）。它不该被统计的理由与克隆失败无关——
# 近一年 6461 条提交的作者是 Axel Kohlmeyer(3940)、Stan Moore(619)、
# jtclemm 等上游 LAMMPS 维护者，零个社区贡献者；而社区一年总提交才 6005 次，
# 抓进来会让数据翻倍且全是外部人。社区侧真正的 LAMMPS 开发在 deepmd-kit
# 里做（2026 年 149 个相关 PR），那个仓库已在统计内，不会漏。
#
# lammps 的复查条件：该 fork 历史上确有社区活动（njzjz、Yi-FanLi 在
# 2021 年提过 dp_pimd、deepmd plugin 相关 PR），但 4 个 PR 全部 closed
# 且无一 merged，最后一次活动是 2024-04-11 的 bot PR。若今后该 fork 出现
# **已合并的社区 PR**，说明开发重心搬了回来，应把它从本名单移除重新纳入
# （届时需一并解决 HTTPS 克隆不通的问题，见 README「lammps 排除」一节）。
EXCLUDED_REPOS = {
    ".github": "组织元仓库，已按 EXCLUDED_REPOS 排除",
    "lammps": "external fork 且无社区贡献，已按 EXCLUDED_REPOS 排除",
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

    if repo.name in EXCLUDED_REPOS:
        return EXCLUDED_REPOS[repo.name]

    if repo.is_fork:
        if cfg.include_forks == "none":
            return "fork 已按 --include-forks=none 排除"
        if cfg.include_forks == "self" and repo.upstream_family != "self":
            return (
                f"fork 上游类型为 {repo.upstream_family}，"
                "已按 --include-forks=self 排除"
            )

    # max_repo_size <= 0 表示不限，此闸门整体关闭
    if cfg.max_repo_size > 0 and repo.size_mb > cfg.max_repo_size:
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


class RepoFetchError(RuntimeError):
    """仓库清单拉取失败，且重试已用尽。"""


# 网络瞬时故障的重试次数与退避基数。与 api_stats.GitHubGraphQL 保持一致：
# 那边有 5 次退避重试，这里原本是裸 requests.get，一次连接被掐断就让
# 整次运行退出码 1 —— 实测踩到过 RemoteDisconnected。
FETCH_RETRIES = 5
FETCH_BACKOFF_BASE = 2.0

# 值得重试的服务端状态码。403/429 是限额，502/503/504 是瞬时故障。
# 401/404 这类不重试：重试一个鉴权错误没有意义。
RETRY_STATUS = frozenset({403, 429, 500, 502, 503, 504})


def _get_with_retry(url: str, headers: dict, params=None,
                    max_retries: int = FETCH_RETRIES,
                    backoff_base: float = FETCH_BACKOFF_BASE,
                    sleeper=time.sleep):
    """带退避重试的 GET。

    重试两类失败：连接层异常（超时、连接被掐断）与可恢复的状态码。
    其余状态码直接抛 —— 鉴权错、仓库不存在，重试只会拖慢运行。
    """
    last = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=30)
            if resp.status_code in RETRY_STATUS:
                last = RepoFetchError(f"GitHub 返回 {resp.status_code}")
                if attempt >= max_retries:
                    break
                log.warning("拉取 %s 返回 %s，%.0f 秒后重试",
                            url, resp.status_code,
                            backoff_base * (2 ** attempt))
                sleeper(backoff_base * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last = exc
            if attempt >= max_retries:
                break
            log.warning("拉取 %s 失败（%s），%.0f 秒后重试",
                        url, exc, backoff_base * (2 ** attempt))
            sleeper(backoff_base * (2 ** attempt))
    raise RepoFetchError(
        f"拉取 {url} 失败，已重试 {max_retries} 次：{last}") from last


def fetch_repos(org: str, token: str, max_retries: int = FETCH_RETRIES,
                sleeper=time.sleep) -> list:
    """拉取组织全部仓库元数据。REST 比 GraphQL 简单且分页可靠。

    网络瞬时故障会退避重试，重试用尽才抛 RepoFetchError —— 每天定时
    跑时偶发的连接掐断不该让整次运行失败。
    """
    out, page = [], 1
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    while page <= MAX_PAGES:
        resp = _get_with_retry(
            f"https://api.github.com/orgs/{org}/repos",
            headers=headers,
            params={"per_page": 100, "page": page, "type": "all"},
            max_retries=max_retries, sleeper=sleeper,
        )
        batch = resp.json()
        if not batch:
            break
        for j in batch:
            upstream = (j.get("parent") or {}).get("full_name")
            # 列表端点不返回 parent，fork 的上游需单独查详情
            if j.get("fork") and not upstream:
                d = _get_with_retry(j["url"], headers=headers,
                                    max_retries=max_retries, sleeper=sleeper)
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
