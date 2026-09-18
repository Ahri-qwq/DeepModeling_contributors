"""把 PR 标题归到「在干什么」的意图类别。

只对 PR 生效。commit 和 issue 不走这里：实测近 90 天的规范前缀覆盖率
PR 75%、commit 44%、issue 9% —— 后两者分类出来大半是噪音。而且 commit
是过程不是意图（一个 PR 里混着大量 "fix typo"、"address review"），
按它分类会让改错别字和重写求解器占同样的权重。

识别不了的一律归 OTHER，不猜。目前 OTHER 占比不低（abacus-develop 这类
不守 conventional commits 的仓库贡献了大部分），补齐要靠 LLM 做语义判断，
见 docs/运维日志.md 的 todo。
"""
import re

OTHER = "其他"

# 类别在卡片里的出场顺序。新功能排第一是产品决策：运营写推文、发社区
# 通告的素材主要来自这里，不能被数量占优的修复挤到后面去。
CATEGORY_ORDER = [
    "新功能",
    "修复",
    "重构",
    "性能",
    "测试",
    "文档",
    "工程",
    "回滚",
    OTHER,
]

# conventional commits 的 type -> 中文类别。同义词合并到同一类：
# 读日报的是运营不是开发，"ci" 和 "build" 的区别对他们没有意义。
_TYPE_TO_CATEGORY = {
    "feat": "新功能",
    "fix": "修复",
    "bugfix": "修复",
    "refactor": "重构",
    "perf": "性能",
    "test": "测试",
    "tests": "测试",
    "docs": "文档",
    "doc": "文档",
    "ci": "工程",
    "build": "工程",
    "chore": "工程",
    "style": "工程",
    "revert": "回滚",
}

# 形如 type(scope)!<sep> subject。scope 和 ! 可选，分隔符三选一：
#   fix: xxx          规范写法
#   Fix xxx           空格分隔，abacus-develop 等仓库的主流写法
#   perf/Optimize xxx 斜杠分隔，分支名直接当标题时出现
#   refactor(dpmodel) 只有 scope，后面什么都没有
# 空格也算分隔符是权衡后的选择：不认的话近 90 天有 100 个明确可判的 PR
# 会掉进「其他」（Fix module_hs、Refactor module_neighlist 这类）。代价是
# 理论上可能误判 Fix 开头的自然句，实测样本里没有这种情况。
# \b 收尾防止 fixture cleanup 里的 fix 被当成类型词。
_PREFIX = re.compile(
    r"^\s*(" + "|".join(sorted(_TYPE_TO_CATEGORY, key=len, reverse=True)) + r")\b"
    r"(?:\([^)]*\))?\s*!?\s*(?::|/|\s|$)",
    re.IGNORECASE,
)


def classify_title(title):
    """从 PR 标题判断意图类别，认不出返回 OTHER。"""
    if not title:
        return OTHER
    m = _PREFIX.match(title)
    if not m:
        return OTHER
    return _TYPE_TO_CATEGORY[m.group(1).lower()]


def group_by_category(items, key):
    """按类别分组，返回 [(类别, [条目...])]，空类别不出现。

    顺序固定为 CATEGORY_ORDER，组内保持传入顺序 —— 调用方通常已经
    按时间排好，重排会让同一个 PR 在不同天的卡片里跳来跳去。
    """
    buckets = {}
    for item in items:
        buckets.setdefault(classify_title(key(item)), []).append(item)
    return [(cat, buckets[cat]) for cat in CATEGORY_ORDER if cat in buckets]
