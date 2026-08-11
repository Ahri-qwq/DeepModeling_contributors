import inspect
import sys
from pathlib import Path

import pytest

# 让测试无需安装即可 import contributors
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contributors import config as _config  # noqa: E402


# Config 里所有会落盘的字段 → 临时目录下的文件名。
# 以后新增落盘字段（多维表格缓存、看板快照等），在这里加一行即可。
_DISK_FIELDS = {"events_db": "events.db"}


@pytest.fixture(autouse=True)
def isolate_real_paths(tmp_path, monkeypatch):
    """把 Config 里会落盘的默认路径改到临时目录，兜住忘记显式传参的测试。

    为什么需要兜底而不是只改 mk_cfg：二期给 Config 新增 events_db 字段时，
    test_integration.mk_cfg 漏了同步重定向，于是集成测试把 tiny 仓库的假数据
    写进了真实的 data/events.db。同类疏忽——新增落盘字段没跟上隔离——会随每次
    加字段重演，靠人记得是不可靠的。

    实现上改的是 dataclass 生成的 __init__.__defaults__ 元组，而不是
    __dataclass_fields__ 里的 Field 对象：后者是只读的 slots 对象，且
    __init__ 在建类时就把默认值烤进了 __defaults__，改 Field 不生效（实测确认）。
    """
    params = [p for p in inspect.signature(_config.Config.__init__).parameters
              if p != "self"]
    defaults = list(_config.Config.__init__.__defaults__)
    # __defaults__ 只覆盖末尾这些带默认值的参数，故偏移量是参数总数减去它的长度
    offset = len(params) - len(defaults)

    for field, filename in _DISK_FIELDS.items():
        defaults[params.index(field) - offset] = str(tmp_path / filename)

    monkeypatch.setattr(_config.Config.__init__, "__defaults__",
                        tuple(defaults))
    yield
