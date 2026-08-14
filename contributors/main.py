"""主流程编排。

核心原则：单仓库失败不影响整体，永不静默丢数据。
每个仓库包在独立 try 里，失败记入 failures 并继续。
"""
import csv
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api_stats import (
    GitHubGraphQL, RateLimitError, collect_api_events, collect_api_stats,
)
from .auth import AuthError, get_token
from .cache import CacheError, CacheManager
from .config import parse_args
from .git_stats import (
    GitError, clone_or_fetch, collect_ai_coauthors, collect_commit_events,
    collect_git_stats, count_upstream_excluded,
)
from .identity import IdentityResolver, is_ai_assistant, is_bot, is_marked_bot
from .models import ApiStats
from .notify import card, digest, feishu, period
from .output import (
    SUM_FIELDS, LINE_CAVEAT, Row, summarize, write_csv, write_json,
    write_markdown,
)
from .repos import fetch_repos, filter_repos
from .store import EventStore

# build_rows 中需要跨邮箱累加的行数字段
_LINE_FIELDS = ("additions", "deletions", "files_changed",
                "additions_raw", "deletions_raw")

# 失败仓库的重试预算。实测失败多是连接超时这类瞬时故障（一天内撞到
# RemoteDisconnected、SSLEOFError、connect timeout 三种），立刻重试一次
# 通常就好，不必等 —— 以前的固定轮间等待会让一个仓库的抖动拖着其余 37 个干等。
# 每轮内每个仓库试两次（失败即刻再来一次），共 3 轮，故最多 6 次。
DEFAULT_REPO_RETRIES = 3
ATTEMPTS_PER_ROUND = 2
# 只剩一个仓库待重试时的最小间隔秒数。此时没有别的仓库可以插空，
# 连着打同一个域名既无益也不礼貌。
LAST_REPO_RETRY_WAIT = 30
# 连续失败几次就在日报里升级为告警。定 3 而非 2：连着两个倒霉的夜晚
# 并不罕见，3 次基本可以排除网络抖动，指向改名、删除或权限变更。
CHRONIC_FAILURE_THRESHOLD = 3


def _new_bucket(login=None) -> dict:
    return {"login": login, "names": set(), "emails": set(),
            "git": None, "api": ApiStats(), "up": 0}


def build_rows(repo, git_stats: dict, api_stats: dict,
               resolver: IdentityResolver, cfg, upstream_counts=None) -> tuple:
    """把 git 侧与 API 侧按归并键合并成输出行。

    返回 (正常行, bot 行)。同一人的多个邮箱在能解析出 login 时自动合并；
    解析不出时退化为按邮箱分行并记入 unmatched，绝不丢弃。
    """
    # None 与空字典语义不同：None 表示上游对比失败或未做，无从判断；
    # 空字典表示对比成功但无人独立于上游。前者退化为 commits，后者记 0。
    upstream_unknown = upstream_counts is None
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
            # 两个字段口径各自恒定，都是可加量，直接分别求和
            b["git"].commits += gs.commits
            b["git"].commits_loose += gs.commits_loose
            for f in _LINE_FIELDS:
                a, c = getattr(b["git"], f), getattr(gs, f)
                if a is not None or c is not None:
                    setattr(b["git"], f, (a or 0) + (c or 0))
        b["names"].update(gs.names)
        if email:
            b["emails"].add(email)
        # fork 场景下上游可达的提交不计入本社区贡献。缺失即 0：
        # count_upstream_excluded 只列出上游不可达的人，实测
        # deepmodeling/GPUMD 窗口内 638 次提交全部上游可达而返回空字典，
        # 若在此兜底成 commits，上游原作者会被记成满额本社区贡献。
        # 非 fork 无上游概念，或上游对比失败无从判断时，才等于 commits。
        if repo.is_fork and repo.upstream and not upstream_unknown:
            b["up"] += upstream_counts.get(email, 0)
        else:
            b["up"] += gs.commits

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
        # id 里显式带 [bot] 后缀的直接不进主表：那是 GitHub 平台自己打的
        # 标记，可直接采信，不存在误判真人的风险。is_bot 里其余是黑名单
        # 推断（njzjz-bot、codecov），有误判风险，仍留在主表只作标注。
        marked = is_marked_bot(login, name)
        # AI 代码助手与 CI 机器人分列：前者是 AI 写的代码，后者是自动化
        # 流程。两类的人工判断依据不同，混成一列会让核对无从下手。
        ai = is_ai_assistant(login, name)
        row = Row(
            repo=repo.name,
            login=login or "",
            name=name,
            email=email,
            github_url=f"https://github.com/{login}" if login else "",
            commits=gs.commits if gs else 0,
            commits_loose=gs.commits_loose if gs else 0,
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
            is_ai_assistant=ai,
            additions=gs.additions if gs else None,
            deletions=gs.deletions if gs else None,
            files_changed=gs.files_changed if gs else None,
            additions_raw=gs.additions_raw if gs else None,
            deletions_raw=gs.deletions_raw if gs else None,
        )
        # id 里显式带 [bot] 的不进主表。黑名单推断的仍进，只作标注。
        if not marked:
            rows.append(row)
        # bots.csv 是对照表，与主表去留无关：无论是否留在主表，
        # 都要给出「哪些被识别为机器人」的清单供人工核对误判。
        # 存副本而非同一对象：两个列表各自要跑 merge_by_name，共享对象会
        # 让一侧的合并把另一侧的计数改坏（或让已合并掉的行重复计数）
        if bot:
            bots.append(replace(row))

    # 黑名单推断的 bot 默认保留（用户 Q9 裁决：删掉比加回去简单，
    # 推断有误判风险）。--exclude-bots 才一并移出，bots.csv 照旧保留全部
    if cfg.exclude_bots:
        rows = [r for r in rows if not r.is_bot]
    return rows, bots


def merge_by_name(rows: list) -> list:
    """把无 login 的行合并进同仓库中姓名相同的已知 login 行。

    实测背景：贡献者用未在 GitHub 登记的邮箱提交时反查不到 login，会被
    拆成独立条目。deepmd-kit 中 Han Wang 被拆成 1151+110 两行，
    abacus-develop 中 dyzheng 被拆成 128+278，榜单严重失真。这些条目的
    git 姓名与其 login 条目完全一致，可据此合并。

    保守规则，宁可漏合并也不错合并：
    - 只在同一仓库内合并（跨仓库重名风险高得多）
    - 姓名须完全相同（去空白、忽略大小写）；实测 abacus_fixer 与
      Mohan Chen 确为同一人但姓名不同，本规则刻意不合并
    - 同名对应多个 login 时不合并，因为无法判定归属
    - 两个都无 login 的行不互相合并，无从确认是同一人
    """
    # (repo, 归一化姓名) -> 该组合下的 login 行
    by_name: dict = {}
    for r in rows:
        if not r.login or not r.name.strip():
            continue
        key = (r.repo, " ".join(r.name.split()).lower())
        by_name.setdefault(key, []).append(r)

    merged_away = set()
    for r in rows:
        if r.login or not r.name.strip():
            continue
        key = (r.repo, " ".join(r.name.split()).lower())
        targets = by_name.get(key, [])
        if len(targets) != 1:
            # 无匹配或有歧义，保持独立并留在 unmatched 供人工确认
            continue
        t = targets[0]
        # 两个 commits 字段口径各自恒定，直接求和即可 —— 不需要在合并时
        # 调整口径。commits 侧本就不含 PR 分支提交，squash 的重复计数进不来；
        # commits_loose 侧本就该含，两行相加正是这个人的宽松总数。
        for f in SUM_FIELDS:
            setattr(t, f, getattr(t, f) + getattr(r, f))
        for f in _LINE_FIELDS:
            a, b = getattr(t, f), getattr(r, f)
            if a is not None or b is not None:
                setattr(t, f, (a or 0) + (b or 0))
        emails = {e for e in (t.email + ";" + r.email).split(";") if e}
        t.email = ";".join(sorted(emails))
        merged_away.add(id(r))

    return [r for r in rows if id(r) not in merged_away]


UNMATCHED_COLUMNS = ["identity", "name", "commits", "repos",
                     "pr_created", "issue_created", "guess"]


def build_unmatched_report(rows: list, unmatched: set) -> list:
    """生成待人工确认的身份清单，自带姓名与贡献量。

    只输出邮箱的话管理员无从认人：看到 2050325993@qq.com 既不知是谁、
    也不知该优先看哪几条。故从输出行提取姓名、提交数与所在仓库，
    并按提交数降序 —— 提交多的更值得先认。

    guess 列给出同名的已知账号作为线索。姓名完全相同的已由
    merge_by_name 自动合并，所以这里出现的是姓名不同但可能同人的情况，
    需要人工判断。
    """
    # 已知账号的姓名 -> login，供 guess 列提示
    known: dict = {}
    for r in rows:
        if r.login and r.name.strip():
            known.setdefault(" ".join(r.name.split()).lower(), set()).add(r.login)

    agg: dict = {}
    for r in rows:
        if r.login:
            continue
        key = r.email or f"name:{r.name}"
        a = agg.setdefault(key, {
            "identity": r.email or "(git 未配置邮箱)",
            "name": r.name, "commits": 0, "repos": set(),
            "pr_created": 0, "issue_created": 0,
        })
        a["commits"] += r.commits
        a["pr_created"] += r.pr_created
        a["issue_created"] += r.issue_created
        if r.repo:
            a["repos"].add(r.repo)
        if not a["name"]:
            a["name"] = r.name

    out = []
    for a in agg.values():
        hits = known.get(" ".join(a["name"].split()).lower(), set())
        out.append({**a, "repos": ";".join(sorted(a["repos"])),
                    "guess": ";".join(sorted(hits))})

    # resolver 记录了但输出行中找不到的身份也要列出，绝不静默丢弃。
    # 但已归并到某个 login 的邮箱要排除：它已有账号，不需人工确认
    # （同一邮箱可能先以未知身份记入 unmatched，之后才由 API 补上映射）
    resolved = {e for r in rows if r.login for e in r.email.split(";") if e}
    seen = {a["identity"] for a in out}
    for ident in sorted(unmatched):
        if ident in seen or ident in resolved:
            continue
        label = ident if "@" in ident else "(git 未配置邮箱)"
        if label in seen:
            continue
        out.append({"identity": ident, "name": "", "commits": 0, "repos": "",
                    "pr_created": 0, "issue_created": 0, "guess": ""})

    return sorted(out, key=lambda d: (-d["commits"], d["identity"]))


AI_ASSISTED_COLUMNS = ["repo", "name", "email", "ai_commits", "guess"]


def build_ai_assisted_report(ai_records: list, rows: list) -> list:
    """把 AI 助手提交追溯到的指派人整理成附表。

    只列追溯成功的条目；未追溯的总数记入 run_meta.json，不混进本表 ——
    附表是"谁指派了 AI"的线索，未追溯的部分无人可列。

    实测覆盖率约两成（deepmd-kit 231 次中 46 次），故本表不能当作
    AI 提交的完整归属，只作人工核对的参考。
    """
    known: dict = {}
    for r in rows:
        for e in r.email.split(";"):
            if e and r.login:
                known[e] = r.login

    agg: dict = {}
    for rec in ai_records:
        for (name, email), n in rec["traced"].items():
            key = (rec["repo"], name, email)
            agg[key] = agg.get(key, 0) + n

    out = []
    for (repo, name, email), n in agg.items():
        out.append({"repo": repo, "name": name, "email": email,
                    "ai_commits": n, "guess": known.get(email, "")})
    return sorted(out, key=lambda d: (-d["ai_commits"], d["repo"], d["name"]))


def process_repo(repo, cm, cfg, client, resolver) -> tuple:
    """采集单仓库两侧数据并合并。不传 token：git 层用匿名访问公开仓库。

    返回 (正常行, bot 行, AI 追溯记录, 事件列表)。
    """
    clone_or_fetch(repo, cm, cfg)

    api: dict = {}
    if client is not None:
        api, mapping = collect_api_stats(repo, cfg, client, cm)
        # GraphQL 顺带取回的 email→login 映射，供本仓库及后续仓库归并使用
        for email, login in mapping.items():
            resolver.add_mapping(email, login)

    gstats = collect_git_stats(repo, cm, cfg)

    ups = None
    if repo.is_fork and repo.upstream:
        try:
            ups = count_upstream_excluded(repo, cm, cfg)
        except GitError as exc:
            # 上游 fetch 失败不该让整个仓库失败。传 None 而非 {}：后者会被
            # 当成"无人独立于上游"而把所有人记 0，抹掉真实贡献
            print(f"  提示：{repo.name} 上游对比失败，"
                  f"commits_not_in_upstream 退化为等于 commits（{exc}）")
            ups = None

    # AI 指派人追溯（Q8 附表）。失败不影响主统计：它只是参考信息，
    # 而主名单不能因附表出错而整仓失败
    try:
        traced, untraced = collect_ai_coauthors(repo, cm, cfg)
    except GitError as exc:
        print(f"  提示：{repo.name} AI 指派人追溯失败，附表将缺该仓库（{exc}）")
        traced, untraced = {}, 0

    rows, bots = build_rows(repo, gstats, api, resolver, cfg, ups)
    ai = {"repo": repo.name, "traced": traced, "untraced": untraced}

    # 事件留存（第二期）。失败不影响统计：事件是旁路产物，CSV 才是主产物。
    # 与 AI 附表同样的隔离原则 —— 新功能不能拖累已经跑通的旧功能。
    events = []
    if getattr(cfg, "events", True):
        try:
            events.extend(collect_commit_events(repo, cm, cfg))
        except (GitError, OSError) as exc:
            print(f"  提示：{repo.name} 提交事件收集失败，战报将缺该仓库（{exc}）")
        if client is not None:
            try:
                events.extend(collect_api_events(repo, cfg, cm))
            except (OSError, ValueError) as exc:
                print(f"  提示：{repo.name} PR/Issue 事件收集失败（{exc}）")

    return rows, bots, ai, events


def _load_repos(cm, cfg, token):
    """取仓库清单。--no-fetch 时读缓存，无缓存返回 None 由调用方报错。

    清单必须缓存：--no-fetch 承诺零网络，而该模式下不取 token，
    联网拉清单会以 401 崩溃（实测确认）。
    """
    if cfg.no_fetch:
        return cm.load_repo_list()
    repos = fetch_repos(cfg.org, token)
    cm.save_repo_list(repos)
    return repos


def run(cfg, token: str) -> int:
    cm = CacheManager(cfg.cache_dir)
    out = Path(cfg.out_dir)
    started = time.time()

    # 拆分模式的推送那步：不跑采集管线，只算增量并推。秒级完成，
    # 所以计划任务可以准点触发而不必担心抓取拖时间。
    # 底部累计从上一步落盘的 by_repo.csv 读，不重算也不发 API 请求。
    if getattr(cfg, "only_notify", False):
        return _notify_only(cfg, out)

    # 周报 / 月报：同样只读库，不跑采集管线。秒级完成。
    if getattr(cfg, "weekly", False) or getattr(cfg, "monthly", False):
        return _notify_period(cfg)

    all_repos = _load_repos(cm, cfg, token)
    if all_repos is None:
        print("指定了 --no-fetch，但本地没有仓库清单缓存。"
              "请先不带该参数运行一次以建立缓存。")
        return 2
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
    rows, bots, failures, ai_records = [], [], {}, []
    all_events = []

    def _collect(r, b, ai, evs) -> None:
        rows.extend(merge_by_name(r))
        bots.extend(b)
        ai_records.append(ai)
        all_events.extend(evs)

    def _try_repo(repo, alone: bool, sleeper=time.sleep) -> bool:
        """跑一个仓库，失败就地重试一次。成功返回 True。

        为什么就地重试而不是排队等下一轮：失败多是瞬时连接故障，隔几秒
        再试往往就成了；而排队意味着这个仓库要等其余 37 个跑完才有第二次
        机会，白白拖长整体。

        限流是唯一的例外。撞上 RateLimitError 时立刻再打一次只会加深
        限流，所以它不就地重试，直接留到下一轮 —— 下一轮之前天然隔着
        几十个仓库的时间，比任何 sleep 都管用。
        """
        for attempt in range(1, ATTEMPTS_PER_ROUND + 1):
            try:
                _collect(*process_repo(repo, cm, cfg, client, resolver))
                failures.pop(repo.name, None)
                if attempt > 1:
                    print(f"  {repo.name} 重试成功")
                return True
            except RateLimitError as exc:
                failures[repo.name] = str(exc)[:500]
                print(f"  失败：{exc}（限流，留到下一轮）")
                return False
            except (GitError, RuntimeError, OSError) as exc:
                failures[repo.name] = str(exc)[:500]
                last = attempt >= ATTEMPTS_PER_ROUND
                print(f"  失败：{exc}" + ("" if last else "，立刻重试"))
                # 只剩它一个时没有别的仓库能插空，必须自己隔开
                if not last and alone:
                    sleeper(LAST_REPO_RETRY_WAIT)
        return False

    for i, repo in enumerate(kept, 1):
        print(f"[{i}/{len(kept)}] {repo.name} ...", flush=True)
        _try_repo(repo, alone=False)

    # 只重跑失败的那几个，而不是整个流程。38 个仓库跑一次 24 分钟，
    # 整体重试会让一个仓库的瞬时网络抖动拖着其余 37 个重新 fetch 一遍。
    retries = getattr(cfg, "repo_retries", DEFAULT_REPO_RETRIES)
    wait = getattr(cfg, "repo_retry_wait", LAST_REPO_RETRY_WAIT)
    for attempt in range(2, retries + 1):
        if not failures:
            break
        retry_names = list(failures.keys())
        print(f"\n{len(retry_names)} 个仓库失败，重试（第 {attempt}/{retries} 轮）："
              f"{'、'.join(retry_names)}")
        for repo in [r for r in kept if r.name in failures]:
            alone = len(failures) == 1
            if alone and wait:
                # 只剩一个仓库时，两次尝试之间保证间隔
                time.sleep(wait)
            _try_repo(repo, alone=alone)

    _write_outputs(rows, bots, ai_records, resolver, cfg, out)
    meta = _write_meta(all_repos, kept, skipped, failures, rows, bots,
                       ai_records, resolver, client, cfg, out, started)

    since_s, until_s = meta["window"]["since"], meta["window"]["until"]
    print(f"\n统计区间: {since_s} ~ {until_s}（UTC，含两端）")
    print(f"贡献者 {meta['contributors']} 人，输出目录 {out}")
    # 用报告实际条数，而非 resolver 的原始邮箱数：后者含已归并到某个
    # login 的邮箱，报告已排除，两处数字须一致，否则会让人以为漏了记录
    pending = build_unmatched_report(rows, resolver.unmatched_emails())
    if pending:
        print(f"注意：{len(pending)} 个身份未能关联 "
              "GitHub 账号，见 unmatched.csv")
    if failures:
        print(f"注意：{len(failures)} 个仓库处理失败，见 run_meta.json")

    # 事件留存与推送。整段包在 try 里：退出码只反映统计跑没跑成，
    # 计划任务与监控盯的是它。通知失败不是数据失败，不该触发报警 ——
    # 它有自愈机制（下次把两天的内容一起推），且群里没消息自然会发现。
    if getattr(cfg, "events", True):
        try:
            _record_and_notify(all_events, meta, cfg,
                               len(kept) - len(failures), rows)
        except Exception as exc:                       # noqa: BLE001
            print(f"注意：事件留存或推送失败（{exc}），统计结果不受影响")

    # --strict-repos：有仓库失败就非零退出，供计划任务据此跳过推送。
    # 默认仍返回 0，保持第一期"单仓库失败不中断整体"的原则不变。
    if failures and getattr(cfg, "strict_repos", False):
        print(f"注意：--strict-repos 已开启，{len(failures)} 个仓库失败，"
              "以非零退出。数据不完整，不应据此推送")
        return 1

    return 0


def _read_rows_csv(path: Path) -> list:
    """从上一步落盘的 by_repo.csv 读回行，供 --only-notify 算底部累计。

    重读 CSV 而非重跑管线：推送那步要秒级完成，且不该再花 API 点数。
    文件缺失时返回空列表 —— 底部四项会整体省略，好过让推送失败。
    """
    if not path.is_file():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append(Row(
                    repo=r.get("repo", ""), login=r.get("login", ""),
                    name=r.get("name", ""), email=r.get("email", ""),
                    github_url=r.get("github_url", ""),
                    commits=int(r.get("commits") or 0),
                    commits_loose=int(r.get("commits_loose") or 0),
                    commits_not_in_upstream=int(
                        r.get("commits_not_in_upstream") or 0),
                    pr_created=int(r.get("pr_created") or 0),
                    pr_merged=int(r.get("pr_merged") or 0),
                    pr_reviewed=int(r.get("pr_reviewed") or 0),
                    issue_created=int(r.get("issue_created") or 0),
                    issue_commented=int(r.get("issue_commented") or 0),
                    is_fork=r.get("is_fork") == "True",
                    upstream=r.get("upstream") or "",
                    upstream_family=r.get("upstream_family") or "",
                    is_bot=r.get("is_bot") == "True",
                    is_ai_assistant=r.get("is_ai_assistant") == "True"))
            except (ValueError, TypeError):
                continue          # 单行坏掉不该让整次推送失败
    return rows


def _read_run_meta(path: Path) -> dict:
    """读回 --fetch 那步写下的 run_meta.json。

    存在的理由：拆分模式下推送是独立一次运行，内存里没有采集结果。
    以前这里凭空造了个空 meta，于是 failures 恒为空 —— 日报永远不会
    点名失败仓库，而全流程模式却会。日常跑的正是拆分模式，等于这个
    提示从来没生效过。

    读不到就返回空字典：底部累计与失败行一起省略，好过让推送失败。
    """
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError) as exc:
        print(f"注意：{path} 读取失败（{exc}），失败仓库提示将省略")
        return {}


def _notify_only(cfg, out: Path) -> int:
    """只算增量并推送，不跑采集管线。

    退出码沿用既有原则：推送失败不改退出码。这里连统计都没跑，
    更没有"数据失败"可言，所以一律返回 0。
    """
    rows = _read_rows_csv(out / "by_repo.csv")
    if not rows:
        print(f"注意：{out / 'by_repo.csv'} 不存在或为空，"
              "底部累计将省略。请先跑一次 --no-notify")

    saved = _read_run_meta(out / "run_meta.json")
    meta = {"contributors": len(summarize(rows)) if rows else None,
            "repos_processed": saved.get("repos_processed"),
            "repos_in_scope": saved.get("repos_in_scope"),
            "failures": saved.get("failures", {}),
            "succeeded": saved.get("succeeded", []),
            "window": {"since": "", "until": ""}}
    if meta["failures"]:
        print(f"上次抓取有 {len(meta['failures'])} 个仓库失败，将在日报中说明")
    try:
        _record_and_notify([], meta, cfg, 0, rows)
    except Exception as exc:                       # noqa: BLE001
        print(f"注意：推送失败（{exc}）")
    return 0


def _notify_period(cfg) -> int:
    """推送周报或月报。只读事件库，不跑采集管线。

    退出码沿用既有原则：推送失败不改退出码。这里连统计都没跑，
    更没有"数据失败"可言，故一律返回 0。
    """
    monthly = getattr(cfg, "monthly", False)
    label = "上月" if monthly else "上周"
    start, end = (period.last_month_range() if monthly
                  else period.last_week_range())

    store = EventStore(cfg.events_db)
    try:
        store.init_schema()
        events = store.events_between(start, end)
        top_n = (period.TOP_N_MONTHLY if monthly else period.TOP_N_WEEKLY)
        d = period.build_period(events, label, period.range_text(start, end),
                                top_n=top_n)
        print(f"{label}（{d.range_text}）：{len(events)} 条事件，"
              f"{d.contributor_total} 位贡献者")

        if d.is_empty and not cfg.notify_empty:
            print(f"{label}无活动，跳过推送（--notify-empty 可改变）")
            return 0

        payload = period.render_period(d)
        if cfg.notify_dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        # 周月报不参与日报的增量基线：它只是换个视角看同一批事件，
        # 推送与否不该影响"上次成功推送"，否则会打乱日报的增量判定。
        feishu.load_env()
        try:
            feishu.send(payload)
            print(f"已推送{label}报到飞书群")
        except feishu.FeishuError as exc:
            print(f"注意：{label}报推送失败（{exc}）")
    finally:
        store.close()
    return 0


def _record_and_notify(events, meta, cfg, repos_processed: int,
                       rows: list) -> None:
    """把事件写进历史库，按需推送战报。"""
    store = EventStore(cfg.events_db)
    try:
        store.init_schema()

        # 重发最近一份存档：调通道、验排版时不必等真实增量
        if getattr(cfg, "resend_last", False):
            payload = store.latest_digest()
            if payload is None:
                print("库里没有战报存档，无法重发")
                return
            if cfg.notify_dry_run:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return
            feishu.load_env()
            try:
                feishu.send(payload)
                print("已重发最近一份战报")
            except feishu.FeishuError as exc:
                print(f"注意：重发失败（{exc}）")
            return

        first = store.is_first_run()

        run_id = store.begin_run(since=meta["window"]["since"],
                                 until=meta["window"]["until"])
        result = store.upsert_events(events, run_id)
        store.finish_run(run_id, repos_processed=repos_processed)
        # 失败要在 finish_run 之后写：连续计数靠 repo_failures 与
        # repos_processed 两者共同认出"这是采集 run"，而后者由 finish_run
        # 落盘。全军覆没时 repos_processed 正好是 0，靠的就是这张表。
        store.record_failures(run_id, meta.get("failures", {}))
        # 成功也要记：日报问的是"过去 24 小时内成功过没有"，只有失败记录
        # 的话，"没失败"既可能是成功、也可能是压根没跑，两者分不开。
        store.record_successes(run_id, meta.get("succeeded", []))
        print(f"事件库 {cfg.events_db}：本次新增 {len(result.new_events)} 条，"
              f"状态变更 {len(result.state_changes)} 条")

        # 基线在 finish_run 之后、查询增量之前，且不要求 --notify：
        # 它标记的是"截至本次运行的一切都算已汇报过"，之后跑的才是真正增量。
        if getattr(cfg, "mark_notified", False):
            marked = store.mark_notify_baseline()
            print(f"已把运行 {marked} 记为已推送基线，此前的历史积压不再推送")
            return

        if getattr(cfg, "no_notify", False):
            # 拆分模式的抓取那步：事件已入库，推送留给之后的 --only-notify
            print("已记录事件，本次不推送（--no-notify）")
            return

        if not (cfg.notify or cfg.notify_dry_run):
            return

        if first:
            # 首次运行会把今年至今几千条全判为新增，推出去毫无意义
            print("首次运行，已建库但不推送；下次运行起才有增量")
            return

        # 按锚定窗口取内容：8-13 的日报恒为 8-12 10:00 ~ 8-13 10:00（东八区），
        # 与实际推送时刻无关。补推、重跑抓取都不会改变已定日期的内容 ——
        # 窗口内补抓的自动进当天，窗口后补抓的自然进第二天。
        win_start, win_end = period.daily_range(_report_date(cfg))
        pending = store.pending_in_window(win_start, win_end)
        totals = _year_totals(rows)

        # 数据缺失提醒的判据是"这个窗口内成功过没有"，而不是"最后一次跑挂没挂"。
        # 7 点失败、8 点补跑成功，10 点推送时就不该再提醒。
        missing = store.repos_missing_since(win_start, win_end)
        chronic = {n: c for n, c in store.consecutive_failures().items()
                   if c >= CHRONIC_FAILURE_THRESHOLD and n in missing}
        # 告警文案按失败原因分流：网络故障和仓库消失要给不同的处置建议
        chronic_reasons = store.last_failure_reasons()

        # 无增量时准备一句近期活动量。群里一天没消息，第一反应是脚本挂了，
        # 所以宁可发一条"昨日无更新 + 本周至今 N 次提交"。
        recent_label, recent_counts = "", {}
        if not (pending.new_events or pending.state_changes):
            recent_label, recent_counts = period.pick_recent(
                store.events_between)

        d = digest.build(
            pending,
            last_notify_at=_last_notify_time(store),
            # 标题日期要跟着 --date 走：补推 8-14 的日报，标题不能写成
            # 生成那天。窗口终点就是那天的 10:00，拿它当参考时刻正合适。
            now=datetime.fromisoformat(win_end),
            total_contributors=meta.get("contributors"),
            total_commits=totals["commits"],
            total_prs_merged=totals["pr_merged"],
            total_prs_created=totals["pr_created"],
            total_issues=totals["issue_created"],
            repos_processed=meta.get("repos_processed"),
            repos_total=meta.get("repos_in_scope"),
            failed_repos=sorted(n for n in missing if n not in chronic),
            chronic_failures=chronic,
            chronic_reasons=chronic_reasons,
            recent_label=recent_label,
            recent_counts=recent_counts,
        )

        if d.is_empty and not cfg.notify_empty:
            # 每天推一条"今天没有新增"会很快让人把机器人静音
            print("无新增内容，跳过推送（--notify-empty 可改变）")
            return

        payload = card.render(d)
        if cfg.notify_dry_run:
            # dry-run 也存档：这样调通道时不必等真实增量，可以直接重发
            store.save_digest(run_id, payload, sent=None)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        digest_id = store.save_digest(run_id, payload, sent=None)
        feishu.load_env()
        try:
            feishu.send(payload)
            store.mark_notified(run_id, True)
            store.mark_digest_sent(digest_id, True)
            print("已推送到飞书群")
        except feishu.FeishuError as exc:
            store.mark_notified(run_id, False)
            store.mark_digest_sent(digest_id, False)
            n = store.runs_since_last_notify()
            msg = f"推送失败（{exc}）"
            if n >= 3:
                msg += f"；距上次成功推送已 {n} 次运行，请检查配置"
            print(f"注意：{msg}")
    finally:
        store.close()


def _report_date(cfg):
    """--date 指定的日报日期，未指定时返回 None（由 daily_range 取今天）。"""
    raw = getattr(cfg, "date", None)
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _last_notify_time(store) -> str:
    row = store.conn.execute(
        "SELECT finished_at FROM runs WHERE notified=1 "
        "ORDER BY run_id DESC LIMIT 1").fetchone()
    return row[0] if row and row[0] else ""


def _year_totals(rows: list) -> dict:
    """今年至今的四项累计，口径与 summary.csv 同名列一致。

    commits 取严格口径（而非 commits_loose），与 summary.csv 的排序口径
    一致 —— 战报底部这行是给人对照 CSV 用的，两处数字必须对得上。

    PR/issue 三项是按人累加的：某人在窗口前创建、窗口内仍有活动的 PR
    也会计入，故它不等于"窗口内新建的 PR 数"。保持这个口径是有意的，
    因为人们要核对的正是 summary.csv 本身。数据全部来自已落盘的统计，
    不额外发 API 请求。

    三项全为 0 时返回 None 而非 0：--no-fetch 或无当天 API 缓存时
    collect_api_stats 返回空，三项会被加成 0，而战报上方可能正列着几百个
    合并的 PR —— 底部写"今年 0 个 PR 合并"会让整条日报自相矛盾。
    区分不了"真的一个都没有"与"没取到数据"时，宁可不显示。
    """
    merged = summarize(rows)
    pr_created = sum(r.pr_created for r in merged)
    pr_merged = sum(r.pr_merged for r in merged)
    issue_created = sum(r.issue_created for r in merged)
    has_api = bool(pr_created or pr_merged or issue_created)
    return {
        "commits": sum(r.commits for r in merged),
        "pr_created": pr_created if has_api else None,
        "pr_merged": pr_merged if has_api else None,
        "issue_created": issue_created if has_api else None,
    }


def _write_outputs(rows, bots, ai_records, resolver, cfg, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    # 日常模式只维护两张总表。其余附表（per-repo、md、json、unmatched、
    # bots、ai_assisted）是全量统计时的人工核对材料，每天重写既慢又没人看。
    if getattr(cfg, "daily", False):
        write_csv(summarize(rows), out / "summary.csv", cfg)
        write_csv(rows, out / "by_repo.csv", cfg)
        return

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
        out / "unmatched.csv", UNMATCHED_COLUMNS,
        build_unmatched_report(rows, resolver.unmatched_emails()),
    )
    write_csv(bots, out / "bots.csv", cfg)
    # AI 指派人追溯附表（Q8）。即使为空也要落盘，让人知道跑过这一步
    _write_simple_csv(
        out / "ai_assisted.csv", AI_ASSISTED_COLUMNS,
        build_ai_assisted_report(ai_records, rows),
    )


def _write_meta(all_repos, kept, skipped, failures, rows, bots, ai_records,
                resolver, client, cfg, out: Path, started: float) -> dict:
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
        # 本次实际纳入统计范围的仓库数（--repos 过滤、跳过 fork/归档/超限
        # 之后剩下的）。战报的范围标注要和它比，而不是和组织下全部仓库比 ——
        # 全量跑本来就只覆盖 67 个里的 34 个，拿 67 当分母会让每次运行
        # 都显示成"局部"。
        "repos_in_scope": len(kept),
        "skipped": skipped,
        "failures": failures,
        # 成功抓取的仓库名。日报靠它判断"过去 24 小时内成功过没有" ——
        # 补跑成功后提醒就该消失，只有失败记录是判断不出来的。
        "succeeded": sorted(r.name for r in kept if r.name not in failures),
        "contributors": len(summarize(rows)),
        # bots.csv 是完整对照表，两类 bot 都在里面，故这里是「被标注的数量」
        "bots_flagged": len({r.login for r in bots}),
        # 两类 bot 的处置不同，必须分开报告：
        # id 里显式带 [bot] 的是 GitHub 平台标记，可直接采信，恒定剔除主表；
        # 黑名单推断的（如 njzjz-bot）有误判风险，仅 --exclude-bots 时才剔除
        "marked_bots_excluded_from_main": True,
        "inferred_bots_excluded_from_main": cfg.exclude_bots,
        "ai_assistants_flagged": len(
            {r.login for r in rows if r.is_ai_assistant}
        ),
        # 追溯覆盖率：实测约两成，两个数都要给，否则附表会被误当成完整归属
        "ai_commits_traced": sum(
            n for rec in ai_records for n in rec["traced"].values()
        ),
        "ai_commits_untraced": sum(rec["untraced"] for rec in ai_records),
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
