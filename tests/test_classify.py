"""PR 意图分类器的单元测试。"""
import pytest

from contributors.notify.classify import (
    CATEGORY_ORDER,
    OTHER,
    classify_title,
    group_by_category,
)


class TestClassifyTitle:
    @pytest.mark.parametrize("title,expected", [
        ("feat: add MatterSim property descriptor", "新功能"),
        ("fix: deduplicate distributed training logs", "修复"),
        ("refactor: replace RapidJSON with nlohmann-json", "重构"),
        ("perf: speed up neighbor list build", "性能"),
        ("test: add regression case", "测试"),
        ("tests: take source_base off define private", "测试"),
        ("docs: generate input reference in CI", "文档"),
        ("doc: fix typo", "文档"),
        ("ci: cache generated C++ model fixtures", "工程"),
        ("build: bump cmake minimum", "工程"),
        ("chore: update deps", "工程"),
        ("style: reformat", "工程"),
        ("revert: undo the last change", "回滚"),
    ])
    def test_conventional_prefixes(self, title, expected):
        assert classify_title(title) == expected

    def test_bugfix_alias_maps_to_fix(self):
        assert classify_title("bugfix: memory leak") == "修复"

    @pytest.mark.parametrize("title", [
        "Fix(deltaspin): memory leak error for nspin=4",
        "FIX: uppercase prefix",
        "Refactor: Replace RapidJSON",
        "Docs: Generate input reference",
    ])
    def test_prefix_is_case_insensitive(self, title):
        assert classify_title(title) != OTHER

    def test_scope_in_parentheses_is_supported(self):
        # conventional commits 的 scope 写法：feat(pt-expt): ...
        assert classify_title("feat(pt-expt): add DPA4C-LR model") == "新功能"

    def test_breaking_change_bang_is_supported(self):
        # feat!: 表示 breaking change，仍然是新功能
        assert classify_title("feat!: drop python 3.8") == "新功能"

    def test_scope_with_bang(self):
        assert classify_title("fix(api)!: change return type") == "修复"

    @pytest.mark.parametrize("title,expected", [
        # 空格分隔：社区里很常见，尤其 abacus-develop
        ("Fix bug: Reset BFGS history after cell changes", "修复"),
        ("Fix module_hs sparse output controls", "修复"),
        ("Refactor module_neighlist", "重构"),
        ("Refactor Hcontainer-related codes: remove current_R", "重构"),
        # 斜杠分隔：分支名直接当标题时会出现
        ("perf/Optimize neighbor-list construction with OpenMP", "性能"),
        ("Feat/sim pair followups", "新功能"),
        # 有 scope 但没冒号
        ("refactor(dpmodel/dpa4)", "重构"),
    ])
    def test_separator_variants(self, title, expected):
        """冒号不是唯一分隔符，空格和斜杠同样常见。"""
        assert classify_title(title) == expected

    def test_fix_typo_is_still_a_fix(self):
        assert classify_title("Fix typo in periodic boundary description") == "修复"

    @pytest.mark.parametrize("title", [
        "OpenMP-Based Parallel Optimization for Molecular Dynamics",
        "add move constructor for several classes",
        "Support NequIP border_op communication",
        "merge from fanyu",
    ])
    def test_unrecognized_titles_fall_back_to_other(self, title):
        assert classify_title(title) == OTHER

    def test_empty_and_none_are_other(self):
        assert classify_title("") == OTHER
        assert classify_title(None) == OTHER

    def test_prefix_must_be_a_whole_word(self):
        # "fixture" 不该被当成 fix
        assert classify_title("fixture cleanup for tests") == OTHER

    def test_leading_whitespace_tolerated(self):
        assert classify_title("   fix: leading spaces") == "修复"


class TestCategoryOrder:
    def test_new_feature_comes_first(self):
        """新功能排最前：运营写推文的素材主要来自这里。"""
        assert CATEGORY_ORDER[0] == "新功能"

    def test_other_comes_last(self):
        """未识别的价值最低，排最后。"""
        assert CATEGORY_ORDER[-1] == OTHER

    def test_fix_is_second(self):
        assert CATEGORY_ORDER[1] == "修复"

    def test_order_has_no_duplicates(self):
        assert len(CATEGORY_ORDER) == len(set(CATEGORY_ORDER))


class TestGroupByCategory:
    def test_groups_follow_category_order(self):
        titles = ["ci: x", "feat: y", "fix: z"]
        groups = group_by_category(titles, key=lambda t: t)
        assert [g[0] for g in groups] == ["新功能", "修复", "工程"]

    def test_empty_categories_are_omitted(self):
        groups = group_by_category(["feat: only"], key=lambda t: t)
        assert [g[0] for g in groups] == ["新功能"]

    def test_items_keep_original_order_within_group(self):
        titles = ["fix: first", "fix: second", "fix: third"]
        groups = group_by_category(titles, key=lambda t: t)
        assert groups[0][1] == titles

    def test_empty_input_gives_no_groups(self):
        assert group_by_category([], key=lambda t: t) == []

    def test_works_with_objects_via_key(self):
        class Row:
            def __init__(self, title):
                self.title = title
        rows = [Row("feat: a"), Row("fix: b")]
        groups = group_by_category(rows, key=lambda r: r.title)
        assert [g[0] for g in groups] == ["新功能", "修复"]
        assert groups[0][1][0] is rows[0]
