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
