"""主流程编排。

核心原则：单仓库失败不影响整体，永不静默丢数据。
每个仓库包在独立 try 里，失败记入 failures 并继续。
"""
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api_stats import GitHubGraphQL, RateLimitError, collect_api_stats
from .auth import AuthError, get_token
from .cache import CacheError, CacheManager
from .config import parse_args
from .git_stats import (
    GitError, clone_or_fetch, collect_git_stats, count_upstream_excluded,
)
from .identity import IdentityResolver, is_bot
from .models import ApiStats
from .output import (
    LINE_CAVEAT, Row, summarize, write_csv, write_json, write_markdown,
)
from .repos import fetch_repos, filter_repos

# build_rows 中需要跨邮箱累加的行数字段
_LINE_FIELDS = ("additions", "deletions", "files_changed",
                "additions_raw", "deletions_raw")


def _new_bucket(login=None) -> dict:
    return {"login": login, "names": set(), "emails": set(),
            "git": None, "api": ApiStats(), "up": 0}


def build_rows(repo, git_stats: dict, api_stats: dict,
               resolver: IdentityResolver, cfg, upstream_counts=None) -> tuple:
    """把 git 侧与 API 侧按归并键合并成输出行。

    返回 (正常行, bot 行)。同一人的多个邮箱在能解析出 login 时自动合并；
    解析不出时退化为按邮箱分行并记入 unmatched，绝不丢弃。
    """
    upstream_counts = upstream_counts or {}
    buckets: dict = {}

    # git 侧：以 email 为起点，尽力解析出 login
    for email, gs in git_stats.items():
        login = resolver.resolve(email)
        # 传入 name 使无邮箱贡献者能用姓名兜底，而非全部挤进 unknown 键
        name_hint = sorted(gs.names)[0] if gs.names else ""
        key = resolver.merge_key(email, login, name_hint)
        b = buckets.setdefault(key, _new_bucket(login))
        if b["git"] is None:
            b["git"] = gs
        else:
            b["git"].commits += gs.commits
            for f in _LINE_FIELDS:
                a, c = getattr(b["git"], f), getattr(gs, f)
                if a is not None or c is not None:
                    setattr(b["git"], f, (a or 0) + (c or 0))
        b["names"].update(gs.names)
        if email:
            b["emails"].add(email)
        # fork 场景下上游可达的提交不计入本社区贡献；非 fork 时等于 commits
        b["up"] += upstream_counts.get(email, gs.commits)

    # API 侧：以 login 为键。只提 issue、从未提交代码的人也是贡献者
    for login, ast in api_stats.items():
        key = f"login:{login.strip().lower()}"
        b = buckets.setdefault(key, _new_bucket(login))
        b["login"] = b["login"] or login
        b["api"] = ast

    rows, bots = [], []
    for b in buckets.values():
        gs = b["git"]
        login = b["login"]
        name = sorted(b["names"])[0] if b["names"] else (login or "")
        email = ";".join(sorted(b["emails"]))
        # 只依据 login 与 name 判定，不查邮箱（邮箱本地部分会误伤真人）
        bot = is_bot(login, name)
        row = Row(
            repo=repo.name,
            login=login or "",
            name=name,
            email=email,
            github_url=f"https://github.com/{login}" if login else "",
            commits=gs.commits if gs else 0,
            commits_not_in_upstream=b["up"] if gs else 0,
            pr_created=b["api"].pr_created,
            pr_merged=b["api"].pr_merged,
            pr_reviewed=b["api"].pr_reviewed,
            issue_created=b["api"].issue_created,
            issue_commented=b["api"].issue_commented,
            is_fork=repo.is_fork,
            upstream=repo.upstream or "",
            upstream_family=repo.upstream_family or "",
            is_bot=bot,
            additions=gs.additions if gs else None,
            deletions=gs.deletions if gs else None,
            files_changed=gs.files_changed if gs else None,
            additions_raw=gs.additions_raw if gs else None,
            deletions_raw=gs.deletions_raw if gs else None,
        )
        (bots if bot else rows).append(row)

    if cfg.include_bots:
        rows.extend(bots)
        bots = []
    return rows, bots


def process_repo(repo, cm, cfg, client, resolver) -> tuple:
    """采集单仓库两侧数据并合并。不传 token：git 层用匿名访问公开仓库。"""
    clone_or_fetch(repo, cm, cfg)

    api: dict = {}
    if client is not None:
        api, mapping = collect_api_stats(repo, cfg, client, cm)
        # GraphQL 顺带取回的 email→login 映射，供本仓库及后续仓库归并使用
        for email, login in mapping.items():
            resolver.add_mapping(email, login)

    gstats = collect_git_stats(repo, cm, cfg)

    ups = {}
    if repo.is_fork and repo.upstream:
        try:
            ups = count_upstream_excluded(repo, cm, cfg)
        except GitError as exc:
            # 上游 fetch 失败不该让整个仓库失败，退化为等于 commits
            print(f"  提示：{repo.name} 上游对比失败，"
                  f"commits_not_in_upstream 退化为等于 commits（{exc}）")
            ups = {}

    return build_rows(repo, gstats, api, resolver, cfg, ups)


def run(cfg, token: str) -> int:
    cm = CacheManager(cfg.cache_dir)
    out = Path(cfg.out_dir)
    started = time.time()

    all_repos = fetch_repos(cfg.org, token)
    kept, skipped = filter_repos(all_repos, cfg)

    if not cfg.no_fetch:
        need = cm.estimate_needed_mb(kept)
        free = cm.free_space_mb()
        if need > free:
            print(f"磁盘空间不足：预计需要 {need:.0f} MB，可用 {free:.0f} MB。"
                  "请清理磁盘，或用 --cache-dir 指定其他位置，"
                  "或用 --max-repo-size 缩小范围。")
            return 2

    print(f"共 {len(all_repos)} 个仓库，跳过 {len(skipped)} 个，"
          f"待处理 {len(kept)} 个")
    for name, reason in sorted(skipped.items()):
        print(f"  跳过 {name}：{reason}")

    client = None if cfg.no_fetch else GitHubGraphQL(token)
    resolver = IdentityResolver()
    rows, bots, failures = [], [], {}

    for i, repo in enumerate(kept, 1):
        print(f"[{i}/{len(kept)}] {repo.name} ...", flush=True)
        try:
            r, b = process_repo(repo, cm, cfg, client, resolver)
            rows.extend(r)
            bots.extend(b)
        except (GitError, RateLimitError, RuntimeError, OSError) as exc:
            failures[repo.name] = str(exc)[:500]
            print(f"  失败：{exc}")

    _write_outputs(rows, bots, resolver, cfg, out)
    meta = _write_meta(all_repos, kept, skipped, failures, rows, bots,
                       resolver, client, cfg, out, started)

    since_s, until_s = meta["window"]["since"], meta["window"]["until"]
    print(f"\n统计区间: {since_s} ~ {until_s}（UTC，含两端）")
    print(f"贡献者 {meta['contributors']} 人，输出目录 {out}")
    if resolver.unmatched_emails():
        print(f"注意：{len(resolver.unmatched_emails())} 个身份未能关联 "
              "GitHub 账号，见 unmatched.csv")
    if failures:
        print(f"注意：{len(failures)} 个仓库处理失败，见 run_meta.json")
    return 0


def _write_outputs(rows, bots, resolver, cfg, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    per_repo: dict = {}
    for r in rows:
        per_repo.setdefault(r.repo, []).append(r)

    want = cfg.fmt
    if want in ("csv", "all"):
        write_csv(summarize(rows), out / "summary.csv", cfg)
        write_csv(rows, out / "by_repo.csv", cfg)
        for name, rs in per_repo.items():
            write_csv(rs, out / "repos" / f"{name}.csv", cfg)
    if want in ("md", "all"):
        write_markdown(summarize(rows), out / "contributors.md", cfg)
    if want in ("json", "all"):
        write_json(summarize(rows), out / "contributors.json", cfg)

    # 未归并身份与 bot 永远单独输出，不受 --format 影响：
    # 前者需人工确认，漏掉一个真实贡献者比多算一个严重
    _write_simple_csv(
        out / "unmatched.csv", ["identity"],
        [{"identity": e} for e in sorted(resolver.unmatched_emails())],
    )
    write_csv(bots, out / "bots.csv", cfg)


def _write_meta(all_repos, kept, skipped, failures, rows, bots, resolver,
                client, cfg, out: Path, started: float) -> dict:
    since_s = cfg.since.date().isoformat()
    until_s = (cfg.until - timedelta(days=1)).date().isoformat()
    meta = {
        "org": cfg.org,
        "window": {"since": since_s, "until": until_s, "timezone": "UTC"},
        "include_forks": cfg.include_forks,
        "max_repo_size_mb": cfg.max_repo_size,
        "count_lines": cfg.count_lines,
        "line_caveat": LINE_CAVEAT if cfg.count_lines else None,
        "repos_total": len(all_repos),
        "repos_processed": len(kept) - len(failures),
        "skipped": skipped,
        "failures": failures,
        "contributors": len(summarize(rows)),
        "bots_excluded": len({r.login for r in bots}),
        "unmatched_emails": len(resolver.unmatched_emails()),
        "api_points_spent": getattr(client, "spent", 0),
        "api_points_remaining": getattr(client, "remaining", None),
        "elapsed_seconds": round(time.time() - started, 1),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def _write_simple_csv(path: Path, cols: list, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(records)


def main(argv=None) -> int:
    cfg = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        token = "" if cfg.no_fetch else get_token()
    except AuthError as exc:
        print(f"认证失败：{exc}")
        return 2
    try:
        return run(cfg, token)
    except CacheError as exc:
        print(f"缓存不可用：{exc}")
        return 2
    except KeyboardInterrupt:
        print("\n已中断。已完成仓库的缓存保留，下次运行将复用。")
        return 130
