"""防线：测试进程绝不许写真实数据目录。

背景：二期给 Config 加了 events_db 字段（默认 ./data/events.db），
但 test_integration.mk_cfg 只重定向了 cache_dir 与 out_dir，漏了这个新字段。
于是每跑一次集成测试，真实的 data/events.db 就被写进 tiny 仓库的假数据一次
（实测：跑一遍 test_integration.py，runs 表从 27 条涨到 36 条）。

污染真实事件库的后果不是测试变红，而是下一次带 --notify 的真实运行会基于
被污染的库算增量——这类错误完全静默。故用一条独立防线钉死：Config 的任何
落盘路径字段，默认值都不许在测试里生效。
"""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from contributors.config import Config


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_test_writes_real_data_dir():
    """真实事件库里不该出现测试夹具的数据。

    不能简单断言"文件不存在"：生产环境本来就该有 data/events.db，
    那是正常产物。要守的是它没被测试写脏 —— 判据是库里出现 tiny，
    那是 test_integration 现场造的小仓库，真实运行绝不会有这个名字。
    """
    real_db = REPO_ROOT / "data" / "events.db"
    if not real_db.exists():
        return  # 还没跑过真实运行，自然干净

    conn = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
    try:
        repos = {r[0] for r in conn.execute("SELECT DISTINCT repo FROM events")}
    except sqlite3.DatabaseError:
        return  # 库损坏是另一条防线的事，不在这里断言
    finally:
        conn.close()

    assert "tiny" not in repos, (
        f"测试污染了真实事件库 {real_db}：库里出现了夹具仓库 tiny。"
        "检查 conftest 的 isolate_real_paths fixture 是否失效。"
    )


def test_config_default_db_is_redirected_during_tests():
    """裸构造 Config 时，events_db 默认值必须已被指向临时目录。

    这条守的是兜底机制本身：即使以后有人新写的测试忘了显式传 events_db
    （正是本次污染的成因），也不会碰到真实路径。
    """
    cfg = Config(org="test",
                 since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                 until=datetime(2027, 1, 1, tzinfo=timezone.utc),
                 include_forks="all", max_repo_size=2048)
    # 必须先 resolve：cfg.events_db 是相对路径 "./data/events.db"，
    # 拿未解析的相对路径与绝对路径比 is_relative_to 恒为 False，断言会假通过
    resolved = Path(cfg.events_db).resolve()
    assert not resolved.is_relative_to(REPO_ROOT / "data"), (
        f"Config.events_db 默认值 {cfg.events_db} 仍指向真实 data/ 目录"
    )
