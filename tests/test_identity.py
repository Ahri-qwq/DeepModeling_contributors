import pytest
from contributors.identity import (
    parse_noreply_login, is_bot, normalize_email, IdentityResolver,
)


# --- noreply 邮箱解析（零成本路径，覆盖率最高）---

def test_parse_noreply_with_numeric_prefix():
    # 实测最常见格式
    assert parse_noreply_login(
        "49699333+dependabot[bot]@users.noreply.github.com"
    ) == "dependabot[bot]"


def test_parse_noreply_without_prefix():
    assert parse_noreply_login("njzjz@users.noreply.github.com") == "njzjz"


def test_parse_noreply_is_case_insensitive_on_domain():
    assert parse_noreply_login("njzjz@Users.NoReply.GitHub.com") == "njzjz"


def test_parse_non_noreply_returns_none():
    assert parse_noreply_login("someone@gmail.com") is None


def test_parse_empty_returns_none():
    assert parse_noreply_login("") is None


# --- bot 识别：只认后缀与黑名单，绝不子串匹配 ---

def test_bracket_bot_suffix_is_bot():
    assert is_bot("dependabot[bot]", "dependabot[bot]", "x@y.com") is True
    assert is_bot("pre-commit-ci[bot]", "", "x@y.com") is True
    assert is_bot("github-actions[bot]", "", "x@y.com") is True


def test_blocklisted_bot_without_suffix_is_bot():
    # 实测发现：njzjz-bot 是真实 bot 但无 [bot] 后缀
    assert is_bot("njzjz-bot", "njzjz-bot", "njzjz.bot@gmail.com") is True


@pytest.mark.parametrize("login", ["botelho", "Botspot", "bot50", "BotBitmap"])
def test_real_users_containing_bot_are_not_bots(login):
    # 实测的真实 GitHub 用户，子串匹配会误伤他们
    assert is_bot(login, login, f"{login}@example.com") is False


def test_normal_user_is_not_bot():
    assert is_bot("njzjz", "Jinzhe Zeng", "njzjz@example.com") is False


def test_none_login_is_not_bot_by_default():
    assert is_bot(None, "Someone", "a@b.com") is False


# --- 邮箱归一化 ---

def test_normalize_email_lowercases_and_strips():
    assert normalize_email("  Njzjz@Example.COM ") == "njzjz@example.com"


# --- 归并逻辑 ---

def test_resolver_maps_email_to_login():
    r = IdentityResolver()
    r.add_mapping("a@x.com", "alice")
    assert r.resolve("a@x.com") == "alice"


def test_resolver_resolves_noreply_without_mapping():
    r = IdentityResolver()
    assert r.resolve("12345+bob@users.noreply.github.com") == "bob"


def test_resolver_is_case_insensitive():
    r = IdentityResolver()
    r.add_mapping("A@X.com", "alice")
    assert r.resolve("a@x.com  ") == "alice"


def test_same_login_multiple_emails_share_merge_key():
    r = IdentityResolver()
    r.add_mapping("work@corp.com", "alice")
    r.add_mapping("home@gmail.com", "alice")
    assert r.merge_key("work@corp.com", None) == r.merge_key("home@gmail.com", None)


def test_unknown_email_falls_back_to_email_key():
    r = IdentityResolver()
    key = r.merge_key("ghost@nowhere.com", None)
    assert key == "email:ghost@nowhere.com"


def test_unknown_email_recorded_as_unmatched():
    r = IdentityResolver()
    r.merge_key("ghost@nowhere.com", None)
    assert "ghost@nowhere.com" in r.unmatched_emails()


def test_resolved_email_not_recorded_as_unmatched():
    r = IdentityResolver()
    r.add_mapping("a@x.com", "alice")
    r.merge_key("a@x.com", None)
    assert r.unmatched_emails() == set()


def test_explicit_login_takes_priority_over_email_lookup():
    r = IdentityResolver()
    r.add_mapping("a@x.com", "stale")
    assert r.merge_key("a@x.com", "fresh") == "login:fresh"


def test_login_merge_key_is_case_insensitive():
    r = IdentityResolver()
    assert r.merge_key("a@x.com", "Alice") == r.merge_key("b@y.com", "alice")
