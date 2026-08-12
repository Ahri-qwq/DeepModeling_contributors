"""发一条全量联调测试消息到飞书群。

一次性脚本：内容写死，不读事件库、不算增量，只验证通道。
"""
import io
import sys

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from contributors.notify import feishu

TEXT = (
    "这是第一次跑全部仓库的日报（本次 38 个在范围内，36 个成功）。\n\n"
    "此前每日推送只覆盖 deepmd-kit 与 dpdata 两个仓库，其余仓库的"
    "历史事件此前从未入库，故本条包含它们近一年的累计数据，不是昨日增量。\n\n"
    "若看到本条，说明全量通道已打通。明早 11 点将测试自动发送，"
    "届时起才是真正的每日增量。"
)

PAYLOAD = {
    "msg_type": "interactive",
    "card": {
        "header": {
            "title": {"tag": "plain_text",
                      "content": "DeepModeling 社区日报（全量联调测试）"},
            "template": "blue",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": TEXT}},
        ],
    },
}

if __name__ == "__main__":
    feishu.load_env()
    feishu.send(PAYLOAD)
    print("已发送全量联调测试消息")
