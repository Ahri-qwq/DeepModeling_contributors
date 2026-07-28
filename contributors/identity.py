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

    注意：意刻意不检查 email 参数。邮箱本地部分会有大量合法用户名，
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


class IdentityResolver:
    """维护 email → login 映射，并产出归并键。

    归并键格式：
      login:<lower>  —— 能确定 GitHub 账号时（可靠，同一人多邮箱自动合并）
      email:<lower>  —— 无法确定时的退化路径，同时记入 unmatched
    """

    def __init__(self) -> None:
        self._email_to_login: dict = {}
        self._unmatched: set = set()

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
