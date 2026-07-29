"""CSV / Markdown / JSON 输出。

两条硬约束：
1. Markdown 表格单元格内禁止 ** 加粗（用户全局约定），已由测试固化。
2. CSV 写 UTF-8 BOM，否则 Excel 打开中文姓名会乱码。
"""
import csv
import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

BASE_COLUMNS = [
    "repo", "login", "name", "email", "github_url",
    "commits", "commits_not_in_upstream",
    "pr_created", "pr_merged", "pr_reviewed",
    "issue_created", "issue_commented",
    "is_fork", "upstream", "upstream_family", "is_bot",
]

LINE_COLUMNS = [
    "additions", "deletions", "files_changed",
    "additions_raw", "deletions_raw",
]

LINE_CAVEAT = (
    "代码行数仅供参考，不宜用于排名：易被生成文件与数据文件污染；"
    "删除冗余往往比新增更有价值；已排除合并提交。"
    "additions/deletions 为过滤后数值，additions_raw/deletions_raw 为原始值。"
)

# 跨仓库/跨邮箱累加时需要求和的计数字段。summarize 与 main.merge_by_name 共用
SUM_FIELDS = (
    "commits", "commits_not_in_upstream", "pr_created", "pr_merged",
    "pr_reviewed", "issue_created", "issue_commented",
)


@dataclass
class Row:
    repo: str
    login: str
    name: str
    email: str
    github_url: str
    commits: int
    commits_not_in_upstream: int
    pr_created: int
    pr_merged: int
    pr_reviewed: int
    issue_created: int
    issue_commented: int
    is_fork: bool
    upstream: str
    upstream_family: str
    is_bot: bool
    additions: Optional[int] = None
    deletions: Optional[int] = None
    files_changed: Optional[int] = None
    additions_raw: Optional[int] = None
    deletions_raw: Optional[int] = None


def _columns(cfg) -> list:
    return BASE_COLUMNS + (LINE_COLUMNS if cfg.count_lines else [])


def _window_text(cfg) -> tuple:
    # 内部为半开区间，对外显示闭区间
    return (cfg.since.date().isoformat(),
            (cfg.until - timedelta(days=1)).date().isoformat())


def summarize(rows: list) -> list:
    """跨仓库累加，一人一行。repo 字段留空表示汇总。

    fork 相关列在汇总视图里没有意义（同一人可能同时出现在 fork 与非 fork
    仓库），故一律清空，避免误读。需要 fork 口径时看 by_repo.csv。
    """
    merged: dict = {}
    emails: dict = {}
    for r in rows:
        key = r.login or f"email:{r.email}"
        if key not in merged:
            merged[key] = Row(**{**asdict(r), "repo": "", "upstream": "",
                                 "upstream_family": "", "is_fork": False})
            emails[key] = set()
        else:
            m = merged[key]
            for f in SUM_FIELDS:
                setattr(m, f, getattr(m, f) + getattr(r, f))
            for f in LINE_COLUMNS:
                a, b = getattr(m, f), getattr(r, f)
                # 两侧都是 None 时保持 None，否则输出会多出 0 列
                if a is not None or b is not None:
                    setattr(m, f, (a or 0) + (b or 0))
        emails[key].update(e for e in r.email.split(";") if e)

    for key, m in merged.items():
        m.email = ";".join(sorted(emails[key]))
    return sorted(merged.values(), key=lambda r: (-r.commits, r.login or ""))


def write_csv(rows: list, path: Path, cfg) -> None:
    cols = _columns(cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 需要 BOM 才能正确识别中文
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: v for k, v in asdict(r).items() if k in cols})


def _md_cell(v) -> str:
    if v is None:
        return ""
    # 转义竖线，否则破坏表格结构。禁止加粗（全局约定）
    return str(v).replace("|", r"\|").replace("\n", " ")


def write_markdown(rows: list, path: Path, cfg) -> None:
    cols = _columns(cfg)
    s, u = _window_text(cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [
        f"# {cfg.org} 贡献者名单",
        "",
        f"统计区间: {s} ~ {u}（含两端，UTC）",
        f"fork 口径: --include-forks={cfg.include_forks}",
        "",
    ]
    if cfg.count_lines:
        out += [f"提示: {LINE_CAVEAT}", ""]
    out.append("| " + " | ".join(cols) + " |")
    out.append("|" + "|".join(["---"] * len(cols)) + "|")
    for r in rows:
        d = asdict(r)
        out.append("| " + " | ".join(_md_cell(d[c]) for c in cols) + " |")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_json(rows: list, path: Path, cfg) -> None:
    cols = _columns(cfg)
    s, u = _window_text(cfg)
    meta = {
        "org": cfg.org, "since": s, "until": u, "timezone": "UTC",
        "include_forks": cfg.include_forks,
        "count_lines": cfg.count_lines,
    }
    if cfg.count_lines:
        meta["caveat"] = LINE_CAVEAT
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "contributors": [
            {k: v for k, v in asdict(r).items() if k in cols} for r in rows
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
