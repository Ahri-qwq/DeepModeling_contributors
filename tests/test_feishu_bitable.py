"""feishu_bitable.sync 的增量逻辑。

重点覆盖删除路径——统计窗口是滚动的一年，贡献者会掉出窗口，表格里那些
行必须被清掉。删除涉及数据丢失，不能只靠"跑一次线上看看"来验证。

不打网络：用一个记录调用的假 client 替代 BitableClient。
"""
import csv
from pathlib import Path

import pytest

from contributors import feishu_bitable as fb


class FakeClient:
    """记录所有写操作，并按预设返回现有记录。"""

    def __init__(self, existing=()):
        self._existing = [dict(r) for r in existing]
        self.created = []
        self.updated = []
        self.deleted = []

    def list_records(self, page_size=500, page_token=""):
        return [dict(r) for r in self._existing]

    def batch_create(self, records):
        self.created.extend(records)
        return len(records)

    def batch_update(self, records):
        self.updated.extend(records)
        return len(records)

    def batch_delete(self, record_ids):
        self.deleted.extend(record_ids)
        return len(record_ids)


def _write_csv(path: Path, rows: list[dict]) -> Path:
    cols = ["repo", "login", "email", "name", "commits"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    return path


def _rec(record_id: str, repo: str, login: str, email: str) -> dict:
    return {
        "record_id": record_id,
        "fields": {"repo": repo, "login": login, "email": email},
    }


def test_creates_when_table_is_empty(tmp_path):
    """首跑：表格为空，csv 全部新增。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "alpha", "login": "ann", "email": "ann@x.com"},
        {"repo": "alpha", "login": "bob", "email": "bob@x.com"},
    ])
    client = FakeClient(existing=[])
    result = fb.sync(csv_path, client)

    assert result["added"] == 2
    assert result["updated"] == 0
    assert result["deleted"] == 0
    assert len(client.created) == 2


def test_updates_without_duplicating(tmp_path):
    """已存在的行走更新，不重复新增。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "alpha", "login": "ann", "email": "ann@x.com"},
    ])
    client = FakeClient(existing=[_rec("r1", "alpha", "ann", "ann@x.com")])
    result = fb.sync(csv_path, client)

    assert result["added"] == 0
    assert result["updated"] == 1
    assert result["deleted"] == 0
    assert client.updated[0]["record_id"] == "r1"


def test_deletes_rows_missing_from_csv(tmp_path):
    """表格里有、csv 里没有的行要被删除（掉出统计窗口）。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "alpha", "login": "ann", "email": "ann@x.com"},
    ])
    client = FakeClient(existing=[
        _rec("r1", "alpha", "ann", "ann@x.com"),
        _rec("r2", "alpha", "gone", "gone@x.com"),
    ])
    result = fb.sync(csv_path, client)

    assert result["updated"] == 1
    assert result["deleted"] == 1
    assert client.deleted == ["r2"]


def test_empty_login_rows_stay_distinct(tmp_path):
    """同一仓库下多个 login 为空的贡献者不能互相覆盖。

    这是 2026-09-10 修的键碰撞问题：改用 repo+login+email 三元组后，
    GPUMD 那 21 个未关联账号的贡献者各占一行。
    """
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "GPUMD", "login": "", "email": "a@x.com", "name": "A"},
        {"repo": "GPUMD", "login": "", "email": "b@x.com", "name": "B"},
        {"repo": "GPUMD", "login": "", "email": "c@x.com", "name": "C"},
    ])
    client = FakeClient(existing=[])
    result = fb.sync(csv_path, client)

    assert result["added"] == 3, "三个空-login 的人必须各自成行"
    assert len({r["fields"]["email"] for r in client.created}) == 3


def test_empty_login_rows_match_existing(tmp_path):
    """空-login 的行再次同步时匹配到已有记录，而不是重复新增。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "GPUMD", "login": "", "email": "a@x.com"},
    ])
    client = FakeClient(existing=[_rec("r1", "GPUMD", "", "a@x.com")])
    result = fb.sync(csv_path, client)

    assert result["added"] == 0
    assert result["updated"] == 1
    assert result["deleted"] == 0


def test_empty_csv_does_not_wipe_table(tmp_path):
    """空 csv 是 fetch 异常的信号，此时绝不能把整张表删空。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [])
    client = FakeClient(existing=[
        _rec("r1", "alpha", "ann", "ann@x.com"),
        _rec("r2", "alpha", "bob", "bob@x.com"),
    ])
    result = fb.sync(csv_path, client)

    assert result["deleted"] == 0
    assert client.deleted == []
    assert client.updated == []


def test_record_without_repo_is_ignored(tmp_path):
    """repo 为空的行是脏记录，不该匹配到任何 csv 行，也不该被删。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "alpha", "login": "ann", "email": "ann@x.com"},
    ])
    client = FakeClient(existing=[
        _rec("r1", "alpha", "ann", "ann@x.com"),
        _rec("r9", "", "", ""),
    ])
    result = fb.sync(csv_path, client)

    assert result["updated"] == 1
    assert result["deleted"] == 0, "repo 为空的行不该进入删除候选"
    assert client.deleted == []


def test_row_key_distinguishes_by_email(tmp_path):
    """_row_key 对 login 相同的不同邮箱要产出不同键。"""
    k1 = fb._row_key({"repo": "r", "login": "", "email": "a@x.com"})
    k2 = fb._row_key({"repo": "r", "login": "", "email": "b@x.com"})
    assert k1 != k2

    # login 有值但 email 为空的行也要能区分开（机器人场景）
    k3 = fb._row_key({"repo": "r", "login": "codecov", "email": ""})
    k4 = fb._row_key({"repo": "r", "login": "dependabot", "email": ""})
    assert k3 != k4


def test_text_column_keeps_numeric_looking_string(tmp_path):
    """文本列里长得像数字的值必须保持字符串。

    线上踩过：abacus-develop 有位贡献者 name 就叫「1」，被当成数字转成
    int 写进文本列 name，飞书回 1254060 TextFieldConvFail，整批 batch_create
    失败。而 batch_create 排在 batch_update 前面，异常一抛后面两步都不执行
    ——表格从此整体停更（2026-09-26 起两天没更新才发现）。
    """
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "abacus-develop", "login": "", "email": "a1@1demacbook-air.local",
         "name": "1", "commits": "0"},
    ])
    client = FakeClient()
    fb.sync(csv_path, client)

    fields = client.created[0]["fields"]
    assert fields["name"] == "1", "name 是文本列，不能转成数字"
    assert isinstance(fields["name"], str)


def test_numeric_column_still_converts_to_number(tmp_path):
    """计数列是真数字列（type=2），必须转成数字，不能退化成字符串。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "deepmd-kit", "login": "someone", "email": "a@b.c",
         "name": "Someone", "commits": "42"},
    ])
    client = FakeClient()
    fb.sync(csv_path, client)

    assert client.created[0]["fields"]["commits"] == 42


def test_numeric_looking_login_and_email_stay_text(tmp_path):
    """login / email 同为文本列，纯数字值也要保持字符串。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "r", "login": "123456", "email": "789", "name": "n",
         "commits": "1"},
    ])
    client = FakeClient()
    fb.sync(csv_path, client)

    fields = client.created[0]["fields"]
    assert fields["login"] == "123456"
    assert fields["email"] == "789"


def test_row_key_treats_missing_field_as_empty():
    """飞书对空文本列返回 None，不是缺键——默认值不生效。

    `row.get("login", "")` 只在键不存在时给默认值；键在而值为 None 时返回
    None，str(None) 得到字面量 "None"，于是表格侧算出
    `repo\x1fNone\x1f...`、csv 侧算出 `repo\x1f\x1f...`，两边永远匹配
    不上。后果是每次同步都把这些行当成"表格里没有"重建、把老行当成"csv
    里没有"删掉——实测 743 行里有 411 行这样全量删建（103 行 login 为空、
    308 行 email 为空）。
    """
    from_csv = fb._row_key({"repo": "r", "login": "", "email": "a@b.c"})
    from_table = fb._row_key({"repo": "r", "login": None, "email": "a@b.c"})
    assert from_csv == from_table

    csv_no_email = fb._row_key({"repo": "r", "login": "u", "email": ""})
    tbl_no_email = fb._row_key({"repo": "r", "login": "u", "email": None})
    assert csv_no_email == tbl_no_email


def test_sync_does_not_rebuild_rows_with_empty_text_fields(tmp_path):
    """空 login/email 的行已在表格里时，应走更新而不是删了重建。"""
    csv_path = _write_csv(tmp_path / "by_repo.csv", [
        {"repo": "abacus-develop", "login": "", "email": "a1@x.local",
         "name": "1", "commits": "7"},
    ])
    # 表格侧：飞书把空 login 返回成 None
    existing = [{
        "record_id": "rec1",
        "fields": {"repo": "abacus-develop", "login": None,
                   "email": "a1@x.local", "name": "1"},
    }]
    client = FakeClient(existing)
    result = fb.sync(csv_path, client)

    assert result["updated"] == 1, "应识别为已存在的行"
    assert result["added"] == 0
    assert result["deleted"] == 0, "不该把老行删掉重建"
