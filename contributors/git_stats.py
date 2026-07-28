"""git 侧统计：mirror clone/fetch、git log 解析、行数统计。

关键实现说明（均经实测验证）：
- 用 git log --all 覆盖所有分支。它按 commit SHA 天然去重，已合并的
  feature 分支不会让贡献者被重复计数（实测 dpdata 473 条 = 473 个唯一 SHA）。
- --no-merges 排除合并提交，否则合并者会被算上整个分支的改动。
- 时间过滤交给 git 的 --since/--until，因为 %aI 返回的是作者本地时区
  （实测有 +08:00），自行解析比较容易错位；传入时统一用 UTC ISO 字符串。
- clone/fetch 不传 token：deepmodeling 全部仓库为公开（实测私有数为 0），
  匿名访问即可。token 拼进 URL 会明文写入 .git/config 并长期留在缓存目录。
"""
import fnmatch
import subprocess
from pathlib import Path
from typing import Optional

from .identity import normalize_email
from .models import GitStats

RECORD_SEP = "\x1f"
# C 前缀标记提交行，与 numstat 数据行区分。
# 分隔符用 \x1f（ASCII 单元分隔符）而非 \x00：Windows 的 CreateProcess
# 不允许命令行参数含 NUL 字节，用 \x00 会抛 ValueError（实测确认）。
# \x1f 同样不可能出现在邮箱、姓名或 SHA 中。
LOG_FORMAT = f"C{RECORD_SEP}%H{RECORD_SEP}%aN{RECORD_SEP}%aE{RECORD_SEP}%aI"

CLONE_TIMEOUT = 1800  # 30 分钟/仓库
LOG_TIMEOUT = 600


class GitError(RuntimeError):
    pass


def is_excluded(path: str, patterns: list) -> bool:
    """判断路径是否属于生成文件/数据文件，不计入行数统计。"""
    p = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(p, pat):
            return True
        # vendor/** 需匹配任意层级下的 vendor 目录
        if "**" in pat:
            head = pat.split("/**")[0]
            if head and (p.startswith(head + "/") or f"/{head}/" in p):
                return True
        # *.lock 这类模式也要匹配子目录中的文件
        if pat.startswith("*.") and p.endswith(pat[1:]):
            return True
        if fnmatch.fnmatch(Path(p).name, pat):
            return True
    return False


def _init_line_fields(gs: GitStats) -> None:
    gs.additions = 0
    gs.deletions = 0
    gs.files_changed = 0
    gs.additions_raw = 0
    gs.deletions_raw = 0


def parse_git_log(text: str, count_lines: bool, exclude_paths: list) -> dict:
    """解析 git log 输出，按归一化 email 聚合，返回 {email: GitStats}。"""
    stats: dict = {}
    seen_files: dict = {}
    cur: Optional[GitStats] = None
    cur_key: Optional[str] = None

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line:
            continue

        if line.startswith("C" + RECORD_SEP):
            parts = line.split(RECORD_SEP)
            if len(parts) < 5:
                # 字段不足，无法确定作者；同时清空当前作者，避免后续
                # numstat 行被错误累加到上一位贡献者
                cur, cur_key = None, None
                continue
            _, _sha, name, email, _when = parts[:5]
            key = normalize_email(email)
            if not key:
                cur, cur_key = None, None
                continue
            if key not in stats:
                stats[key] = GitStats()
                if count_lines:
                    _init_line_fields(stats[key])
                seen_files[key] = set()
            cur, cur_key = stats[key], key
            cur.commits += 1
            # commits_not_in_upstream 由 count_upstream_excluded 单独覆盖，
            # 非 fork 场景下与 commits 相等
            cur.commits_not_in_upstream = cur.commits
            cur.emails.add(key)
            if name:
                cur.names.add(name.strip())
            continue

        if cur is None or cur_key is None or not count_lines:
            continue

        cols = line.split("\t")
        if len(cols) < 3:
            continue
        # 路径本身可能含制表符，故只切前两列
        add_s, del_s, path = cols[0], cols[1], "\t".join(cols[2:])
        # 二进制文件 numstat 显示为 - -
        add = int(add_s) if add_s.isdigit() else 0
        dele = int(del_s) if del_s.isdigit() else 0

        cur.additions_raw += add
        cur.deletions_raw += dele
        if add_s == "-" or is_excluded(path, exclude_paths):
            continue
        cur.additions += add
        cur.deletions += dele
        seen_files[cur_key].add(path)
        cur.files_changed = len(seen_files[cur_key])

    return stats


def _run_git(args: list, cwd: Optional[Path] = None, timeout: int = 300) -> str:
    try:
        r = subprocess.run(
            ["git"] + args, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=True,
        )
    except FileNotFoundError as exc:
        raise GitError("未找到 git 命令，请确认已安装并在 PATH 中") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git 超时（{timeout} 秒）：{' '.join(args[:3])}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[:300]
        raise GitError(f"git 失败：{detail}") from exc
    return r.stdout


def is_blobless(path: Path) -> bool:
    """判断已有裸库是否为 blobless 部分克隆。

    blobless 库的 config 中 remote.origin.promisor = true（实测确认）。
    """
    try:
        out = _run_git(["config", "--get", "remote.origin.promisor"], cwd=path)
    except GitError:
        # 该键不存在时 git config 以退出码 1 结束，视为完整克隆
        return False
    return out.strip().lower() == "true"


def clone_or_fetch(repo, cm, cfg) -> None:
    """首次 clone，之后增量 fetch。损坏或类型不符的缓存删除重建。

    克隆类型按 --count-lines 自动选择（实测依据）：
    - 不统计行数：用 --filter=blob:none，体积约为完整克隆的 1/7
      （dpdata 实测 3.6 MB vs 25 MB），git log 仅需 0.1 秒。
    - 统计行数：必须完整克隆。--numstat 要 diff 文件内容，在 blobless
      库上会逐个从远端回取 blob，实测约 2 分钟/仓库；完整克隆仅 0.16 秒。

    不传 token：目标仓库为公开仓库，匿名访问即可，避免凭据明文写入
    .git/config 长期留存于缓存目录。私有仓库会在此处认证失败并提示。
    """
    if cm.is_corrupt(repo.name):
        cm.discard(repo.name)

    # 已有缓存的克隆类型与本次需求不符时必须重建，否则要么行数统计
    # 慢到不可用，要么白占 7 倍磁盘
    if cm.is_cloned(repo.name):
        blobless = is_blobless(cm.repo_path(repo.name))
        if cfg.count_lines and blobless:
            if cfg.no_fetch:
                raise GitError(
                    f"{repo.name} 的本地缓存是 blobless 部分克隆，"
                    "无法在 --no-fetch 下统计行数。请去掉 --no-fetch "
                    "重新运行以建立完整克隆，或去掉 --count-lines。"
                )
            cm.discard(repo.name)

    url = f"https://github.com/{cfg.org}/{repo.name}.git"

    if not cm.is_cloned(repo.name):
        if cfg.no_fetch:
            raise GitError(
                f"{repo.name} 无本地缓存，但指定了 --no-fetch。"
                "请先不带该参数运行一次以建立缓存。"
            )
        dest = cm.repo_path(repo.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--mirror"]
        if not cfg.count_lines:
            args.append("--filter=blob:none")
        args += [url, str(dest)]
        _run_git(args, timeout=CLONE_TIMEOUT)
    elif cm.needs_fetch(repo, cfg.refresh, cfg.no_fetch):
        _run_git(["fetch", "--all", "--prune"], cwd=cm.repo_path(repo.name),
                 timeout=CLONE_TIMEOUT)

    if not cfg.no_fetch:
        cm.record_fetch(repo.name, repo.pushed_at)


def collect_git_stats(repo, cm, cfg) -> dict:
    """统计窗口内所有分支的提交，返回 {email: GitStats}。"""
    args = [
        "log", "--all", "--no-merges",
        f"--since={cfg.since.isoformat()}",
        f"--until={cfg.until.isoformat()}",
        f"--format={LOG_FORMAT}",
    ]
    if cfg.count_lines:
        args.append("--numstat")
    text = _run_git(args, cwd=cm.repo_path(repo.name), timeout=LOG_TIMEOUT)
    return parse_git_log(text, cfg.count_lines, cfg.exclude_paths)


def count_upstream_excluded(repo, cm, cfg) -> dict:
    """统计上游不可达的 commit，返回 {email: 数量}。

    非 fork 仓库返回空字典（调用方令其等于 commits）。
    对 fork：添加上游 remote、fetch，用 --not --remotes=upstream 排除
    上游可达的提交，从而只留下本组织自己的工作。
    """
    if not repo.is_fork or not repo.upstream:
        return {}
    path = cm.repo_path(repo.name)
    up_url = f"https://github.com/{repo.upstream}.git"
    existing = _run_git(["remote"], cwd=path)
    if "upstream" not in existing.split():
        _run_git(["remote", "add", "upstream", up_url], cwd=path)
    if not cfg.no_fetch:
        # 与主克隆的类型保持一致：在完整克隆里混入 blob 过滤会把整个库
        # 标记成部分克隆，破坏 is_blobless 检测并让行数统计变慢
        fetch_args = ["fetch"]
        if not cfg.count_lines:
            fetch_args.append("--filter=blob:none")
        fetch_args += ["upstream", "+refs/heads/*:refs/remotes/upstream/*"]
        _run_git(fetch_args, cwd=path, timeout=CLONE_TIMEOUT)
    text = _run_git([
        "log", "--all", "--no-merges",
        "--not", "--remotes=upstream",
        f"--since={cfg.since.isoformat()}",
        f"--until={cfg.until.isoformat()}",
        f"--format={LOG_FORMAT}",
    ], cwd=path, timeout=LOG_TIMEOUT)
    return {k: v.commits for k, v in parse_git_log(text, False, []).items()}
