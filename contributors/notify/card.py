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
    """一行条目。标题做 markdown 转义，编号带链接，提交者缀在标题后。

    提交者拿到什么放什么（login / 姓名 / 邮箱皆可），拿不到就整个省略 ——
    已删号用户的 author 为 null，写"未知"只是噪音。
    """
    title = _escape(item.title) or "(无标题)"
    who = f" @{_escape(item.author)}" if getattr(item, "author", "") else ""
    if item.number is None:
        # commit 没有编号，但有 URL，链接挂在仓库名上仍可点开
        if item.url:
            return f"· [{item.repo}]({item.url}) {title}{who}"
        return f"· {item.repo} {title}{who}"
    return f"· [{item.repo} #{item.number}]({item.url}) {title}{who}"


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


def _totals_line(d: Digest) -> str:
    """底部累计。与顶部同样详细，数字全部来自已落盘的 CSV 口径。

    带范围标注：只跑部分仓库时写"（1/34 个仓库）"，否则局部数字看起来
    像社区总量。跑全量时不加括号，避免噪音。
    """
    tail = []
    if d.total_contributors is not None:
        tail.append(f"{d.total_contributors} 位贡献者")
    if d.total_commits is not None:
        tail.append(f"{d.total_commits} 次提交")
    if d.total_prs_merged is not None:
        tail.append(f"{d.total_prs_merged} 个 PR 合并")
    if d.total_prs_created is not None:
        tail.append(f"{d.total_prs_created} 个 PR 新建")
    if d.total_issues is not None:
        tail.append(f"{d.total_issues} 个 issue")
    if not tail:
        return ""

    scope = ""
    if (d.repos_processed is not None and d.repos_total
            and d.repos_processed < d.repos_total):
        scope = f"（{d.repos_processed}/{d.repos_total} 个仓库）"
    return f"今年至今{scope}：{' · '.join(tail)}"


# 距上次成功推送多久之内算"昨日"。定在 36 小时而非 24：日报固定每天同一
# 时刻跑，正常间隔就是 24 小时上下，机器起停、任务延迟都会让它浮动几小时。
# 卡死 24 会让大量正常运行被判成异常，反而天天显示回退文案。
YESTERDAY_MAX_HOURS = 36


def _headline(d: Digest) -> str:
    """正文第一行。

    固定每天跑时写"昨日社区动态"。但增量是按"本次运行新看到的"判定的，
    推送失败补推时卡片里装的是两天的内容，此时必须回退到带日期的说法 ——
    否则文案就在说谎。从没成功推送过时同样回退。
    """
    hours = d.hours_since_notify
    if hours is not None and hours <= YESTERDAY_MAX_HOURS:
        return "昨日社区动态"
    if d.since_text:
        return f"自上次汇报（{d.since_text}）以来："
    return "自上次汇报以来："


# 失败仓库最多列几个。全挂时不该让告警本身刷屏。
MAX_FAILED = 5


def _failed_line(d: Digest) -> str:
    """未能抓取的仓库。数据不全时必须让读日报的人看见。

    "累计到明天"不是安慰话：增量按 first_seen_run > 上次成功推送的 run
    判定，今天没抓到的仓库，明天抓到时那些事件才首次入库，自然会进
    明天的日报，不会丢。
    """
    if not d.failed_repos:
        return ""
    shown = [_escape(r) for r in d.failed_repos[:MAX_FAILED]]
    rest = len(d.failed_repos) - len(shown)
    tail = f"，还有 {rest} 个" if rest > 0 else ""
    return (f"未能抓取：{'、'.join(shown)}{tail}"
            "（其增量将累计到明天的日报）")


def _chronic_line(d: Digest) -> str:
    """连续多次抓取失败的仓库。与上面那行分开，因为这条要人去处理。

    "累计到明天"对这些仓库不再成立：连挂三次说明多半不是网络抖动，
    而是改名、删除或权限变更 —— 那种情况永远不会自愈，没人去看就一直挂着。
    """
    if not d.chronic_failures:
        return ""
    items = sorted(d.chronic_failures.items(),
                   key=lambda kv: (-kv[1], kv[0]))[:MAX_FAILED]
    rest = len(d.chronic_failures) - len(items)
    tail = f"，还有 {rest} 个" if rest > 0 else ""
    body = "、".join(f"{_escape(n)}（{c} 次）" for n, c in items)
    return (f"⚠️ 连续抓取失败：{body}{tail}"
            "，可能是改名、删除或权限变更，请检查")


def _recent_line(d: Digest) -> str:
    """昨日无更新时贴的近期活动量。只给数字，不列明细。

    存在的理由：群里一天没消息，第一反应是"脚本挂了"。发一条明说无更新、
    再带上近期活动量，既排除故障怀疑，也说明系统在正常工作。

    注意口径不同源：日报按"本次运行新看到的"判定，而这个数字按事件真实
    时间查库。可能出现"昨日无更新"但本周至今有 20 次提交 —— 那 20 次是
    前几天看到的，两者不矛盾，故文案里明确写出区间名。
    """
    if not d.recent_counts:
        return "近期无活动记录"
    parts = []
    for key, word in (("commits", "次提交"), ("merged", "个 PR 合并"),
                      ("opened", "个 PR 新建"), ("issues", "个 issue")):
        if d.recent_counts.get(key):
            parts.append(f"{d.recent_counts[key]} {word}")
    if not parts:
        return "近期无活动记录"
    return f"{d.recent_label}：{' · '.join(parts)}"


def render_text(d: Digest, limit: int = MAX_ITEMS) -> str:
    """卡片正文的 markdown。"""
    no_increment = not (d.commit_count or d.merged_prs
                        or d.opened_prs or d.issues)
    if no_increment:
        # 无更新也要发：不发的话群里第一反应是脚本挂了
        lines = [_headline(d), "昨日无更新", "", _recent_line(d)]
        totals = _totals_line(d)
        if totals:
            lines.append("")
            lines.append(totals)
        failed = _failed_line(d)
        if failed:
            lines.append(failed)
        chronic = _chronic_line(d)
        if chronic:
            lines.append(chronic)
        return "\n".join(lines)

    lines = [_headline(d), _summary_line(d)]

    for title, items in (("合并的 PR", d.merged_prs),
                         ("新建的 PR", d.opened_prs),
                         ("新增 issue", d.issues)):
        sec = _section(title, items, limit)
        if sec:
            lines.append("")
            lines.extend(sec)

    # 兜底：当天没有任何 PR/issue 时，卡片就只剩一个提交数字，什么信息
    # 都没有（2026-08-12 实际推出过这样一条）。此时把 commit 列出来。
    # 平时不列是因为一天几十条会刷屏，那个取舍在有 PR/issue 时仍然成立。
    if d.commits and not (d.merged_prs or d.opened_prs or d.issues):
        sec = _section("提交", d.commits, limit)
        if sec:
            lines.append("")
            lines.extend(sec)

    totals = _totals_line(d)
    if totals:
        lines.append("")
        lines.append(totals)

    failed = _failed_line(d)
    if failed:
        lines.append(failed)

    chronic = _chronic_line(d)
    if chronic:
        lines.append(chronic)

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
