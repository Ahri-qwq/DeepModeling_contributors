"""身份归并与 bot 识别。

这是本脚本最易出错的部分。两条铁律：
1. bot 识别只用 [bot] 后缀 + 显式黑名单。实测 botelho / Botspot / bot50
   都是真实用户，子串匹配会把他们从表彰名单里误删。
2. 无法归并的邮箱必须进 unmatched，绝不静默丢弃 —— 漏掉一个真实贡献者
   比多算一个严重。
"""
import re
from typing import Optional

NOREPLY_DOMAIN = "users.noreply.github.com"

# 无 [bot] 后缀但确实是机器人的账号。实测 dpdata 仓库大量提交来自 njzjz-bot。
BOT_BLOCKLIST = {
    "njzjz-bot",
    "dependabot",
    "dependabot-preview",
    "renovate",
    "renovate-bot",
    "codecov",
    "codecov-io",
    "pre-commit-ci",
    "github-actions",
    "web-flow",          # GitHub 网页端合并使用的虚拟账号
    "deepmodeling-bot",
}

# AI 代码助手账号。与 BOT_BLOCKLIST 刻意分开：前者是 AI 写的代码，
# 后者是 CI 自动化（升依赖、跑格式化），人工核对时判断依据完全不同。
# 两者都不排除、只标注，由人决定去留（用户 Q8/Q9 裁决）。
AI_ASSISTANT_LOGINS = {
    "copilot",
    "copilot-swe-agent",
    "copilot-pull-request-reviewer",
    "claude",
    "devin-ai-integration",
    "cursoragent",
    "codex",
}

# 人工确认的 email → login 映射。仅用于自动归并无法覆盖的情形，
# 每条都必须有可核验的依据，写在注释里 —— 这是唯一绕过自动判定的入口，
# 误加一条就会把两个人合成一个。
MANUAL_EMAIL_LOGIN = {
    # 拼写笔误：pku.eud.cn 应为 pku.edu.cn，与 mohanchen@pku.edu.cn
    # 属同一人。该地址下 750 次提交署名 abacus_fixer，本无关联账号。
    "mohanchen@pku.eud.cn": "mohanchen",
}

_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9._-]+(?:\[bot\])?)@" + re.escape(NOREPLY_DOMAIN) + r"$",
    re.IGNORECASE,
)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def parse_noreply_login(email: str) -> Optional[str]:
    """从 xxx@users.noreply.github.com 解析 login，无需 API 请求。

    两种格式都要处理：
      12345+name@users.noreply.github.com
      name@users.noreply.github.com
    """
    m = _NOREPLY_RE.match(normalize_email(email))
    return m.group(1) if m else None


def is_bot(login: Optional[str], name: str = "", email: str = "") -> bool:
    """只认 [bot] 后缀与显式黑名单，禁止子串匹配。

    注意：刻意不检查 email 参数。邮箱本地部分会有大量合法用户名，
    如 renovate@company.com（renovate 是某人的用户名，不是 bot）、
    devops+renovate@company.com，在邮箱中出现黑名单词不代表是 bot。
    bot 身份必须明确来自 login（GitHub 账号）或 name 字段。
    """
    candidates = [c for c in (login, name) if c]
    for c in candidates:
        low = c.strip().lower()
        if low.endswith("[bot]"):
            return True
        if low in BOT_BLOCKLIST:
            return True
    return False


def is_marked_bot(login: Optional[str], name: str = "") -> bool:
    """账号 id 里是否显式带 [bot] 后缀 —— GitHub 平台自己打的标记。

    与 is_bot 的区别：is_bot 还包含 BOT_BLOCKLIST 的推断结果（njzjz-bot、
    codecov 这类确是机器人但 id 里没有后缀）。分两列输出，是为了让人能
    区分「平台确认」与「我们推断」——后者才有误判风险，需要人工复核。
    """
    for c in (login, name):
        if c and c.strip().lower().endswith("[bot]"):
            return True
    return False


def is_ai_assistant(login: Optional[str], name: str = "") -> bool:
    """识别 AI 代码助手账号，用于标注而非排除。

    与 is_bot 同样禁止子串匹配：copilotkid、mycopilot 都可能是真人。
    只做精确全等，并容忍 GraphQL 侧返回的 [bot] 后缀变体
    （实测同一助手在 commit 与 API 两侧的账号名后缀不一致）。
    """
    for c in (login, name):
        if not c:
            continue
        low = c.strip().lower()
        if low.endswith("[bot]"):
            low = low[:-len("[bot]")]
        if low in AI_ASSISTANT_LOGINS:
            return True
    return False


class IdentityResolver:
    """维护 email → login 映射，并产出归并键。

    归并键格式：
      login:<lower>  —— 能确定 GitHub 账号时（可靠，同一人多邮箱自动合并）
      email:<lower>  —— 无法确定时的退化路径，同时记入 unmatched
    """

    def __init__(self) -> None:
        self._email_to_login: dict = {}
        self._unmatched: set = set()
        # 人工映射先装载。API 侧后来的 add_mapping 会覆盖同一邮箱 ——
        # 这是有意的：GraphQL 拿到的账号归属比人工记录更权威。
        for email, login in MANUAL_EMAIL_LOGIN.items():
            self.add_mapping(email, login)

    def add_mapping(self, email: str, login: Optional[str]) -> None:
        e = normalize_email(email)
        if e and login:
            self._email_to_login[e] = login

    def resolve(self, email: str) -> Optional[str]:
        e = normalize_email(email)
        if not e:
            return None
        if e in self._email_to_login:
            return self._email_to_login[e]
        return parse_noreply_login(e)

    def merge_key(self, email: str, login: Optional[str] = None, name: str = "") -> str:
        """产生唯一的归并键，用于在贡献者去重时识别同一个人。

        优先级顺序：
        1. 显式 login（来自 API，比历史映射新）
        2. 从 email 解析的 login（noreply 或历史映射）
        3. email 本身（无法解析 login 时，记入 unmatched）
        4. name（邮箱为空时的备选方案，记入 unmatched）
        5. unknown（邮箱、login、name 都无法使用时，记入 unmatched）
        """
        # 优先级 1-2：显式 login 或从 email 解析的 login
        chosen = login or self.resolve(email)
        if chosen:
            return f"login:{chosen.strip().lower()}"

        # 优先级 3：邮箱有效
        e = normalize_email(email)
        if e:
            self._unmatched.add(e)
            return f"email:{e}"

        # 优先级 4：邮箱为空，尝试用 name
        if name:
            name_lower = name.strip().lower()
            # 记入可读标记，标识这是无邮箱贡献者
            self._unmatched.add(f"<无邮箱> {name.strip()}")
            return f"name:{name_lower}"

        # 优先级 5：邮箱、name 都为空
        self._unmatched.add("<无邮箱无姓名>")
        return "unknown:"

    def unmatched_emails(self) -> set:
        return set(self._unmatched)
