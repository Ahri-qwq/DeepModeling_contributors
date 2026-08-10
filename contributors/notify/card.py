"""战报结构渲染成飞书卡片 JSON。

纯函数：结构进、JSON 出，不发任何网络请求。这样卡片排版可以完全离线
测试，网络那部分单独隔离在 feishu.py。
"""
import json

from .digest import MAX_ITEMS, Digest

# 飞书请求体上限 20 KB（官方文档）。留 2 KB 余量给签名等顶层字段，
# 超过就逐级降级 —— 宁可少列几条，也不能整条消息发不出去。
SIZE_LIMIT = 18 * 1024


def _line(item, kind: str) -> str:
    """一行条目。标题做 markdown 转义，编号带链接。"""
    title = _escape(item.title) or "(无标题)"
    if item.number is None:
        return f"· {item.repo} {title}"
    return f"· [{item.repo} #{item.number}]({item.url}) {title}"


def _escape(text: str) -> str:
    """转义会破坏 markdown 排版的字符。

    方括号与反引号在飞书 markdown 里有语义，PR 标题里出现得不少
    （如 "[BUG] xxx" 这种前缀）。
    """
    if not text:
        return ""
    return (text.replace("[", "\\[").replace("]", "\\]")
                .replace("`", "\\`"))


def _section(title: str, items: list, limit: int) -> list:
    """一个分区。空列表不渲染，超限截断并说明还有多少。"""
    if not items:
        return []
    shown = items[:limit]
    lines = [f"**{title}**"] + [_line(i, title) for i in shown]
    if len(items) > limit:
        lines.append(f"　…还有 {len(items) - limit} 条")
    return lines


def _summary_line(d: Digest) -> str:
    """顶部汇总。群消息大多数人只看前两行，数字要放最上面。"""
    parts = []
    if d.commit_count:
        parts.append(f"{d.commit_count} 次提交")
    if d.merged_count:
        parts.append(f"{d.merged_count} 个 PR 合并")
    if d.opened_count:
        parts.append(f"{d.opened_count} 个 PR 新建")
    if d.issue_count:
        parts.append(f"{d.issue_count} 个 issue")
    return " · ".join(parts) if parts else "无新增"


def render_text(d: Digest, limit: int = MAX_ITEMS) -> str:
    """卡片正文的 markdown。"""
    lines = []

    # 写"自上次汇报以来"而非"今天"：增量按"本次运行新看到的"判定，
    # 昨天推送失败时今天会把两天的内容一起推出来，这句才诚实。
    if d.since_text:
        lines.append(f"自上次汇报（{d.since_text}）以来：")
    else:
        lines.append("自上次汇报以来：")
    lines.append(_summary_line(d))

    for title, items in (("合并的 PR", d.merged_prs),
                         ("新建的 PR", d.opened_prs),
                         ("新增 issue", d.issues)):
        sec = _section(title, items, limit)
        if sec:
            lines.append("")
            lines.extend(sec)

    if d.total_contributors is not None or d.total_commits is not None:
        lines.append("")
        tail = []
        if d.total_contributors is not None:
            tail.append(f"{d.total_contributors} 位贡献者")
        if d.total_commits is not None:
            tail.append(f"{d.total_commits} 次提交")
        lines.append(f"今年至今：{' · '.join(tail)}")

    return "\n".join(lines)


def _build(d: Digest, limit: int) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"DeepModeling 社区日报 {d.generated_at}"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div",
                 "text": {"tag": "lark_md", "content": render_text(d, limit)}},
            ],
        },
    }


def render(d: Digest) -> dict:
    """渲染卡片，超过体积上限时逐级降级。

    降级顺序是先减条数而非砍标题：少列几条 PR 仍然可读，而标题被砍
    到半截会让人看不懂在说什么。真到了 1 条还超限（几乎不可能，除非
    单条标题极长），至少消息本身还发得出去。
    """
    for limit in (MAX_ITEMS, 5, 3, 1):
        card = _build(d, limit)
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= SIZE_LIMIT:
            return card
    return card
