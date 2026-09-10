"""飞书多维表格同步：把 by_repo.csv 的增量写入 Bitable。

与 notify 的 webhook 推送是两套独立机制：
- webhook（notify/feishu.py）推「卡片」到群聊，靠自定义机器人 URL + HMAC 签名。
- 多维表格（本模块）靠 tenant_access_token 调 Bitable API，读写「记录」。

两者互不依赖，鉴权方式完全不同。本模块只负责把 csv 行同步进表格，不关心
日报卡片内容；日后若要多维表格反向影响卡片，在调用方加，别在这里耦合。

设计边界：
- 纯逻辑与网络分层。read_csv 等是纯函数不碰网络；BitableClient
  限定在 HTTP 那一层。这样 csv 解析可离线测试，网络失败只影响写这一步。
- token 自动刷新：tenant_access_token 有效期 2 小时，剩余 <30 分钟再取会
  返回新 token。本模块持单个 client 实例，缓存 token 并在过期前重取。
- 同步粒度：贡献者×仓库，与 by_repo.csv 逐行对应（不做按仓库聚合）。
  唯一键是 repo+login+email 三元组——仅用 repo+login 时，96 个未关联 GitHub
  账号的贡献者（login 为空）在同一仓库内全部碰撞成同一个键，导致冗余行和
  陈旧数据（2026-09-10 修复）。已有行用 record_id 匹配更新，没有的行新增，
  表格里有而 csv 里没有的删除——统计窗口是滚动的一年，贡献者会陆续掉出窗口，
  不删会在表格里永久堆积。表格内容是近一年快照，不是累积档案。
  （曾短暂实现过按 repo 聚合成一行的版本，2026-09-09 按实际展示需求改回
  逐行同步，group_by_repo 保留供统计场景复用。）
- 失败只记日志不影响主流程：调用方（daily_report.sh）捕获异常打 log，绝不
  让同步失败拖垮 --fetch 的成功状态。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

CN_TZ = timezone(timedelta(hours=8))


def _today_cn() -> datetime:
    """当前东八区日期。表名按这个日期展示，与日报窗口逻辑（东八区）一致。"""
    return datetime.now(CN_TZ)

log = logging.getLogger(__name__)

# 飞书 Bitable API 域名。国际版要换 open.larkoffice.com。
BASE_URL = os.environ.get("FEISHU_BITABLE_URL", "https://open.feishu.cn")

# tenant_access_token 有效期 2 小时。剩余不足 30 分钟就重取，避免过期后
# 第一次调用才撞上 99991400（token 无效）。这个阈值保守，宁可多换一次。
TOKEN_TTL = 7200
TOKEN_REFRESH_BEFORE = 1800

# 批量写单次上限 1000 条（官方文档）。by_repo.csv 目前约 670 行，一次能满，
# 但留这个上限注释是为将来表格行数增长做准备——超了要分片。
BATCH_LIMIT = 1000

# Bitable 错误码：用于日志里给出可读原因，避免每次都让人查手册。
ERROR_HINTS = {
    1254003: "无效的 app_token（不在域内或已授权但无权限）",
    1254004: "无效的 table_id",
    1254015: "字段类型不匹配（数字列填了文本、日期列格式不对等）",
    1254104: "单次批量操作超过 1000 条",
    1254302: "无该表格管理权限（应用需被添加为该多维表格的协作者）",
    1254291: "并发写入冲突，可重试或设 ignore_consistency_check",
    99991400: "tenant_access_token 无效或过期",
}


class BitableError(RuntimeError):
    """多维表格 API 调用失败。调用方捕获后只记日志。"""


def _hint(code: int) -> str:
    return ERROR_HINTS.get(code, "")


class TokenClient:
    """持有 app_id/app_secret 并自动刷新 tenant_access_token。

    不缓存到磁盘：token 2 小时过期，不值得为省一次 HTTP 引入状态文件。
    缓存到内存即可，进程内多次调用共享同一个 token。
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str | None = None
        self._tok_expire_at: float = 0.0

    def _fetch_token(self) -> str:
        """调 internal 接口拿 tenant_access_token。

        internal 是「自建应用」取租户 token 的端点；isv 加密应用要
        app_access_token + app_ticket 那套，见文档，本模块用不到。
        """
        body = json.dumps(
            {"app_id": self.app_id, "app_secret": self.app_secret}
        ).encode("utf-8")
        req = Request(
            f"{BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            raise BitableError(
                f"获取 tenant_access_token 失败 code={data.get('code')} "
                f"msg={data.get('msg')}"
            )
        tok = data["tenant_access_token"]
        expire = data.get("expire", TOKEN_TTL)
        self._tok_expire_at = time.time() + expire - TOKEN_REFRESH_BEFORE
        return tok

    def get(self) -> str:
        """返回有效 token。已过期或临期就重取。"""
        if self._token is None or time.time() >= self._tok_expire_at:
            self._token = self._fetch_token()
        return self._token


class BitableClient:
    """读写多维表格记录。持一个 TokenClient 复用 token。"""

    def __init__(self, app_token: str, table_id: str, token: TokenClient):
        self.app_token = app_token
        self.table_id = table_id
        self.token = token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token.get()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str,
                 body: Optional[dict] = None) -> dict:
        url = f"{BASE_URL}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise BitableError(f"请求 {method} {path} 网络失败: {exc}") from exc

    def list_records(self, page_size: int = 500,
                     page_token: str = "") -> list[dict]:
        """分页拉全某张表的所有记录。返回记录列表（含 record_id 和 fields）。"""
        records: list[dict] = []
        token = page_token
        while True:
            path = (
                f"/open-apis/bitable/v1/apps/{self.app_token}"
                f"/tables/{self.table_id}/records?page_size={page_size}"
            )
            if token:
                path += f"&page_token={token}"
            data = self._request("GET", path)
            if data.get("code") != 0:
                raise BitableError(
                    f"list_records 失败 code={data.get('code')} "
                    f"msg={data.get('msg')}"
                )
            items = data.get("data", {}).get("items", [])
            records.extend(items)
            token = data.get("data", {}).get("page_token", "")
            if not token:
                break
        return records

    def batch_create(self, records: list[dict]) -> int:
        """批量新增。返回成功条数，失败抛 BitableError。"""
        if not records:
            return 0
        path = (
            f"/open-apis/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records/batch_create"
        )
        data = self._request("POST", path, {"records": records})
        if data.get("code") != 0:
            raise BitableError(
                f"batch_create 失败 code={data.get('code')} "
                f"msg={data.get('msg')}" + (f"（{_hint(data.get('code'))}）"
                                            if _hint(data.get('code')) else "")
            )
        return len(records)

    def batch_update(self, records: list[dict]) -> int:
        """批量更新。records 每项须带 record_id 字段。返回成功条数。"""
        if not records:
            return 0
        path = (
            f"/open-apis/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records/batch_update"
        )
        data = self._request("POST", path, {"records": records})
        if data.get("code") != 0:
            raise BitableError(
                f"batch_update 失败 code={data.get('code')} "
                f"msg={data.get('msg')}" + (f"（{_hint(data.get('code'))}）"
                                            if _hint(data.get('code')) else "")
            )
        return len(records)

    def batch_delete(self, record_ids: list[str]) -> int:
        """批量删除。返回成功条数。

        用于清掉「表格里有、CSV 里已没有」的残留行——统计窗口是滚动的一年，
        贡献者会随时间陆续掉出窗口，不删就会在表格里永久堆积。
        """
        if not record_ids:
            return 0
        path = (
            f"/open-apis/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records/batch_delete"
        )
        # batch_delete 的载荷是 {"records": [id, ...]}，不是 {"record_ids": ...}
        data = self._request("POST", path, {"records": record_ids})
        if data.get("code") != 0:
            raise BitableError(
                f"batch_delete 失败 code={data.get('code')} "
                f"msg={data.get('msg')}" + (f"（{_hint(data.get('code'))}）"
                                            if _hint(data.get('code')) else "")
            )
        return len(record_ids)

    def rename_table(self, name: str) -> None:
        """改数据表名字。用于每天把表名刷成含日期的展示名。

        失败不抛出到调用方之外的语义——记录数据本身已经写成功，改名
        只是展示层面，失败不该让整次同步算失败，由调用方决定是否吞掉。
        """
        path = f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{self.table_id}"
        data = self._request("PATCH", path, {"name": name})
        if data.get("code") != 0:
            raise BitableError(
                f"rename_table 失败 code={data.get('code')} "
                f"msg={data.get('msg')}"
            )


def read_csv(path: Path) -> list[dict]:
    """读 by_repo.csv，返回行列表（保持列顺序）。"""
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _to_field_value(v: Any) -> Any:
    """把 csv 字段转成 Bitable 字段值。

    Bitable 字段按类型赋值：文本直接给字符串、数字给 int/float。csv 读进来
    全是 str，这里做轻量类型推断——只对明确的数字转，其余保留原文。
    is_fork / is_bot / is_ai_assistant 这类布尔列建表时按文本处理（type 1），
    所以 True/False 保持字符串、不转 bool——转成 bool 会给文本列报
    1254060 TextFieldConvFail。日期、单选项等复杂类型不在此处理。
    """
    if v is None:
        return ""
    s = str(v).strip()
    if s == "":
        return ""
    # 纯数字（含负数、科学记数）转数字；带逗号的金额/浮点也转，但保留
    # 原文特征：只转能安全 round-trip 的，其余交给表格按文本处理。
    if _is_numeric(s):
        try:
            if "." in s or "e" in s.lower():
                return float(s)
            return int(s)
        except (ValueError, OverflowError):
            return s
    return s


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def rows_to_records(rows: list[dict]) -> list[dict]:
    """把 csv 行转成 batch_create 的 records 格式。"""
    records = []
    for r in rows:
        fields = {k: _to_field_value(v) for k, v in r.items() if v is not None}
        records.append({"fields": fields})
    return records


def group_by_repo(rows: list[dict]) -> list[dict]:
    """按 repo 去重聚合，一个仓库一行。

    任务要「每个仓库一行」。by_repo.csv 是贡献者×仓库粒度，同一 repo 下有
    多行。聚合规则：
    - 直接求和 repo 无关的计数列（commits / pr_created 等）。
    - login / name / email / github_url 取该仓库下贡献较多者的代表值
      （贡献最多的那一行），是「这个仓库的活跃贡献者」的呈现口径。
    - 其余列（is_fork、upstream 等）同一仓库应一致，取首行。
    首跑用全量行；每日增量只对变化行做 upsert，靠 record_id 匹配。
    """
    from collections import defaultdict

    SUM_COLS = {
        "commits", "commits_loose", "commits_not_in_upstream",
        "pr_created", "pr_merged", "pr_reviewed",
        "issue_created", "issue_commented",
    }
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in rows:
        repo = r.get("repo", "").strip() or "(未知repo)"
        if repo not in groups:
            groups[repo] = dict(r)
            order.append(repo)
        else:
            g = groups[repo]
            for k in SUM_COLS:
                g[k] = _add(g.get(k, 0), r.get(k, 0))
            # 贡献最多的行作为代表，刷新 login/name/email/github_url
            if _to_int(r.get("commits", 0)) > _to_int(g.get("commits", 0)):
                for k in ("login", "name", "email", "github_url"):
                    g[k] = r.get(k, "")
    return [groups[o] for o in order]


def _add(a: Any, b: Any) -> int:
    return _to_int(a) + _to_int(b)


def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _row_key(row: dict) -> str:
    """行的唯一键：repo + login + email 三元组。

    仅用 repo+login 会有碰撞：96 个未关联 GitHub 账号的贡献者 login 为空，
    同仓库多人挤成同一个键，导致互相覆盖和冗余行（GPUMD 21 人全挤成一个键
    就是典型案例，见 2026-09-10 修复记录）。

    email 单独也不够：login 有值时 email 可以为空（318 行机器人/CI 账号没有
    邮箱），且同一人在不同仓库可能有多个邮箱（分号拼接）。

    三元组 repo+login+email 在当前 by_repo.csv 的全部 749 行中经过验证完全
    唯一，而且两侧（CSV 行和表格 fields）都有这三列，可以对称计算。
    """
    repo = str(row.get("repo", "")).strip()
    login = str(row.get("login", "")).strip()
    email = str(row.get("email", "")).strip()
    return f"{repo}\x1f{login}\x1f{email}"


def sync(csv_path: Path, client: BitableClient) -> dict:
    """把 csv 内容增量同步进表。返回结果统计。

    流程：读现有记录（按 repo+login+email 建索引）→ 读 csv（逐行，不聚合）→
    比较 → 新增 / 更新 / 删除。表格若为空（首跑），全量新增。非空则按
    repo+login+email 匹配：有则更新字段，无则新增；表格里有而 csv 里没有的
    一并删除。

    删除是必要的：统计窗口是滚动的一年，贡献者会随时间陆续掉出窗口，只做
    upsert 的话这些行会在表格里永久堆积（2026-09-10 前实测已积累 2 行）。
    """
    rows = read_csv(csv_path)
    if not rows:
        # 空 csv 可能是 fetch 异常产出，此时不该把整张表删空
        log.warning("csv 为空，跳过同步（不删除表格内容）")
        return {"added": 0, "updated": 0, "deleted": 0, "skipped": 0}

    existing = client.list_records()
    by_key: dict[str, str] = {}
    for rec in existing:
        f = rec.get("fields", {})
        key = _row_key(f)
        # repo 字段为空的行是无意义记录，跳过（三元组里 repo 是第一段）
        if key.split("\x1f")[0]:
            by_key[key] = rec.get("record_id", "")

    to_create = []
    to_update = []
    matched_ids = set()
    for row in rows:
        key = _row_key(row)
        fields = {k: _to_field_value(v) for k, v in row.items() if v is not None}
        if key in by_key:
            to_update.append({"record_id": by_key[key], "fields": fields})
            matched_ids.add(by_key[key])
        else:
            to_create.append({"fields": fields})

    # 表格里有、csv 里没有 → 已掉出统计窗口，删除
    to_delete = [rid for key, rid in by_key.items() if rid not in matched_ids]

    added = client.batch_create(to_create) if to_create else 0
    updated = client.batch_update(to_update) if to_update else 0
    deleted = client.batch_delete(to_delete) if to_delete else 0

    skipped = len(existing) - len(to_update) - deleted
    return {"added": added, "updated": updated, "deleted": deleted,
            "skipped": skipped}


def _make_client_from_env() -> BitableClient:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    app_token = os.environ.get("FEISHU_BITABLE_APP_TOKEN", "")
    table_id = os.environ.get("FEISHU_BITABLE_TABLE_ID", "")
    missing = [
        name for name, val in [
            ("FEISHU_APP_ID", app_id),
            ("FEISHU_APP_SECRET", app_secret),
            ("FEISHU_BITABLE_APP_TOKEN", app_token),
            ("FEISHU_BITABLE_TABLE_ID", table_id),
        ] if not val
    ]
    if missing:
        raise BitableError(f"缺少环境变量: {', '.join(missing)}")
    token = TokenClient(app_id, app_secret)
    return BitableClient(app_token, table_id, token)


def main() -> int:
    """CLI 入口：python -m contributors.feishu_bitable [--dry-run] [csv路径]。

    --dry-run 只读 csv、打印将同步的行数，不发网络请求。
    """
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="同步 by_repo.csv 到飞书多维表格")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析 csv 并打印计划，不发网络请求")
    ap.add_argument("csv", nargs="?", default="output/daily/by_repo.csv",
                    help="by_repo.csv 路径（默认 output/daily/by_repo.csv）")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if args.dry_run:
        rows = read_csv(csv_path)
        print(f"读取 {len(rows)} 行（贡献者×仓库粒度，逐行同步）:")
        for r in rows[:10]:
            print(f"  {r.get('repo')} / {r.get('login')}: "
                  f"{r.get('commits')} commits, {r.get('pr_created')} pr_created")
        if len(rows) > 10:
            print(f"  ... 共 {len(rows)} 行")
        return 0

    try:
        client = _make_client_from_env()
        result = sync(csv_path, client)
    except BitableError as exc:
        log.warning("bitable sync failed (non-fatal): %s", exc)
        return 0
    log.info("同步完成: 新增 %s 行, 更新 %s 行, 删除 %s 行, 跳过 %s 行",
             result["added"], result["updated"], result["deleted"],
             result["skipped"])

    # 表名刷成含日期的展示名，与同步是否成功解耦——改名失败不影响
    # 已经写成功的数据，只记警告。
    table_name = f"社区贡献者-截止至{_today_cn():%Y.%m.%d}"
    try:
        client.rename_table(table_name)
        log.info("表名已更新: %s", table_name)
    except BitableError as exc:
        log.warning("rename_table failed (non-fatal): %s", exc)
    return 0


if __name__ == "__main__":
    main()
