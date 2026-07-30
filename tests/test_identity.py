import pytest
from contributors.identity import (
    parse_noreply_login, is_ai_assistant, is_bot, is_marked_bot, normalize_email,
    IdentityResolver,
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


def test_email_local_part_not_checked_against_blocklist():
    # Fix 1：邮箱本地部分不应该被检查黑名单
    # 真人"Zhang San"因邮箱 renovate@theirdomain.com 不应被误判为 bot
    assert is_bot(None, "Zhang San", "renovate@theirdomain.com") is False
    # 加号地址 devops+renovate@company.com 也不应该被检查
    assert is_bot(None, "Someone", "devops+renovate@company.com") is False


def test_blocklist_still_applies_to_login():
    # 但 login 为黑名单项时仍应被判定为 bot
    assert is_bot("renovate", "", "user@example.com") is True


def test_name_bracket_bot_still_detected():
    # name 中的 [bot] 后缀仍应被检测
    assert is_bot(None, "dependabot[bot]", "user@example.com") is True


# --- AI 代码助手识别（Q8：保留在主表，单独标注）---

@pytest.mark.parametrize("login", [
    "Copilot",
    "copilot",
    "copilot-swe-agent",
    "copilot-pull-request-reviewer",
])
def test_known_ai_assistant_logins(login):
    # 实测抽查中这三个账号都有实际贡献量：Copilot 295 次提交、
    # copilot-pull-request-reviewer 362 次评审、copilot-swe-agent 16 次 PR
    assert is_ai_assistant(login, "") is True


def test_ai_assistant_detected_from_name():
    assert is_ai_assistant(None, "Copilot") is True


def test_ai_assistant_matches_bracket_bot_variant():
    # GraphQL 侧同一助手可能带 [bot] 后缀返回
    assert is_ai_assistant("copilot-swe-agent[bot]", "") is True


@pytest.mark.parametrize("login", ["copilotkid", "mycopilot", "Copilotov"])
def test_real_users_containing_copilot_are_not_ai(login):
    # 与 is_bot 同一条铁律：禁止子串匹配，避免误伤真人
    assert is_ai_assistant(login, login) is False


def test_normal_user_is_not_ai_assistant():
    assert is_ai_assistant("njzjz", "Jinzhe Zeng") is False


def test_ci_bot_is_not_ai_assistant():
    # njzjz-bot / dependabot 是 CI 自动化，不是 AI 代码助手。
    # 两类分列在 is_bot 与 is_ai_assistant，便于人工分别筛选
    assert is_ai_assistant("njzjz-bot", "A bot of @njzjz") is False
    assert is_ai_assistant("dependabot[bot]", "") is False


def test_empty_input_is_not_ai_assistant():
    assert is_ai_assistant(None, "") is False


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


def test_no_email_no_login_with_name_creates_name_key():
    # Fix 2：无邮箱无 login 但有姓名时，应该生成基于姓名的键
    r = IdentityResolver()
    key = r.merge_key("", None, "Zhang San")
    assert key == "name:zhang san"


def test_no_email_with_different_names_creates_different_keys():
    # 两个无邮箱但姓名不同的贡献者应有不同的键（不被误合并）
    r = IdentityResolver()
    key1 = r.merge_key("", None, "Alice")
    key2 = r.merge_key("", None, "Bob")
    assert key1 != key2
    assert key1 == "name:alice"
    assert key2 == "name:bob"


def test_no_email_with_name_recorded_as_unmatched():
    # 无邮箱贡献者应被记入 unmatched，包含其姓名的可读标记
    r = IdentityResolver()
    r.merge_key("", None, "Zhang San")
    unmatched = r.unmatched_emails()
    assert len(unmatched) == 1
    # unmatched 中应该包含一个指示"无邮箱"且包含姓名的标记
    marker = list(unmatched)[0]
    assert "Zhang San" in marker or "zhang san" in marker


def test_no_email_no_name_recorded_as_unmatched():
    # 无邮箱无姓名也应被记入 unmatched（不能静默丢弃）
    r = IdentityResolver()
    key = r.merge_key("", None, "")
    assert key.startswith("unknown:")
    unmatched = r.unmatched_emails()
    assert len(unmatched) == 1


def test_same_name_multiple_times_share_key():
    # 邮箱为空但姓名相同的两人应有相同的键（因为无从区分）
    r = IdentityResolver()
    key1 = r.merge_key("", None, "John Doe")
    key2 = r.merge_key("", None, "John Doe")
    assert key1 == key2


def test_merge_key_with_email_login_and_name_email_takes_priority():
    # 有邮箱时，邮箱和 login 优先于 name
    r = IdentityResolver()
    r.add_mapping("work@corp.com", "alice")
    key = r.merge_key("work@corp.com", None, "Zhang San")
    assert key == "login:alice"


def test_merge_key_with_explicit_login_and_name_login_takes_priority():
    # 显式 login 优先级最高，即使有 name 也不用
    r = IdentityResolver()
    key = r.merge_key("", "bob", "Zhang San")
    assert key == "login:bob"


# --- is_marked_bot：只认 id 里显式的 [bot] 后缀，这类直接不进主表 ---

def test_marked_bot_requires_explicit_suffix():
    assert is_marked_bot("dependabot[bot]", "dependabot[bot]") is True
    assert is_marked_bot("github-actions[bot]", "") is True
    # name 侧带后缀也算（commit 与 API 两侧后缀不一致的情况实测存在）
    assert is_marked_bot(None, "coderabbitai[bot]") is True


def test_blocklisted_bot_without_suffix_is_not_marked():
    # njzjz-bot / codecov 是 bot，但 id 里没有 [bot]，靠黑名单推断。
    # 这类有误判风险，保留在主表只作标注，不能自动剔除。
    assert is_marked_bot("njzjz-bot", "njzjz-bot") is False
    assert is_marked_bot("codecov", "") is False
    assert is_marked_bot("renovate", "") is False


# --- 人工确认的映射（用户确认：abacus_fixer 应并入 mohanchen）---

def test_manual_mapping_loaded_on_init():
    # 无需任何 add_mapping 调用即生效
    r = IdentityResolver()
    assert r.resolve("mohanchen@pku.eud.cn") == "mohanchen"


def test_manual_mapping_merges_typo_email_with_correct_one():
    # pku.eud.cn 是 pku.edu.cn 的笔误，两者应归并为同一人
    r = IdentityResolver()
    r.add_mapping("mohanchen@pku.edu.cn", "mohanchen")
    assert r.merge_key("mohanchen@pku.eud.cn", None) == \
        r.merge_key("mohanchen@pku.edu.cn", None)


def test_manual_mapped_email_not_in_unmatched():
    r = IdentityResolver()
    r.merge_key("mohanchen@pku.eud.cn", None, "abacus_fixer")
    assert r.unmatched_emails() == set()


def test_api_mapping_overrides_manual():
    # GraphQL 拿到的归属比人工记录权威，同一邮箱应被覆盖
    r = IdentityResolver()
    r.add_mapping("mohanchen@pku.eud.cn", "someone-else")
    assert r.resolve("mohanchen@pku.eud.cn") == "someone-else"


def test_manual_mapping_does_not_affect_other_emails():
    r = IdentityResolver()
    assert r.merge_key("unrelated@pku.edu.cn", None) == \
        "email:unrelated@pku.edu.cn"


def test_marked_bot_implies_is_bot():
    # 凡 marked 必然也是 bot，反之不然
    for login in ("dependabot[bot]", "github-actions[bot]", "Copilot[bot]"):
        assert is_bot(login, "") is True


def test_real_user_with_bracket_text_not_marked():
    assert is_marked_bot("botelho", "Botelho") is False
    assert is_marked_bot(None, "Someone (bot fan)") is False
