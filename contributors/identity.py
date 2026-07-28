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
    """只认 [bot] 后缀与显式黑名单，禁止子串匹配。"""
    candidates = [c for c in (login, name) if c]
    for c in candidates:
        low = c.strip().lower()
        if low.endswith("[bot]"):
            return True
        if low in BOT_BLOCKLIST:
            return True
    # 邮箱本地部分也查一次黑名单，覆盖 login 缺失的情况
    local = normalize_email(email).split("@", 1)[0]
    # 去掉 12345+ 前缀
    local = local.split("+", 1)[-1]
    if local and local in BOT_BLOCKLIST:
        return True
    if local.endswith("[bot]"):
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

    def merge_key(self, email: str, login: Optional[str] = None) -> str:
        """显式传入的 login 优先级最高（来自 API，比历史映射新）。"""
        chosen = login or self.resolve(email)
        if chosen:
            return f"login:{chosen.strip().lower()}"
        e = normalize_email(email)
        if e:
            self._unmatched.add(e)
        return f"email:{e}"

    def unmatched_emails(self) -> set:
        return set(self._unmatched)
