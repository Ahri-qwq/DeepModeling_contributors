import csv
import json
from datetime import datetime, timezone

from contributors.config import Config
from contributors.output import (
    Row, BASE_COLUMNS, LINE_COLUMNS, summarize,
    write_csv, write_markdown, write_json,
)


def mk_cfg(tmp_path, **kw):
    base = dict(org="deepmodeling",
                since=datetime(2025, 7, 28, tzinfo=timezone.utc),
                until=datetime(2026, 7, 29, tzinfo=timezone.utc),
                include_forks="all", max_repo_size=2048,
                out_dir=str(tmp_path))
    base.update(kw)
    return Config(**base)


def mk_row(repo="dpdata", login="alice", commits=5, **kw):
    d = dict(repo=repo, login=login, name="Alice", email="a@x.com",
             github_url=f"https://github.com/{login}", commits=commits,
             commits_not_in_upstream=commits, pr_created=2, pr_merged=1,
             pr_reviewed=3, issue_created=1, issue_commented=4,
             is_fork=False, upstream="", upstream_family="", is_bot=False)
    d.update(kw)
    return Row(**d)


def test_base_columns_match_spec_order():
    assert BASE_COLUMNS[:6] == [
        "repo", "login", "name", "email", "github_url", "commits",
    ]
    assert "commits_not_in_upstream" in BASE_COLUMNS
    for c in ["pr_created", "pr_merged", "pr_reviewed",
              "issue_created", "issue_commented",
              "is_fork", "upstream", "upstream_family", "is_bot"]:
        assert c in BASE_COLUMNS


def test_line_columns_absent_from_base():
    for c in LINE_COLUMNS:
        assert c not in BASE_COLUMNS


def test_summarize_accumulates_across_repos():
    rows = [mk_row(repo="a", commits=3), mk_row(repo="b", commits=4)]
    out = summarize(rows)
    assert len(out) == 1
    assert out[0].commits == 7
    assert out[0].pr_created == 4
    assert out[0].repo == ""


def test_summarize_sorts_by_commits_desc():
    rows = [mk_row(login="low", commits=1), mk_row(login="high", commits=9)]
    out = summarize(rows)
    assert [r.login for r in out] == ["high", "low"]


def test_summarize_merges_emails_uniquely():
    rows = [mk_row(repo="a", email="a@x.com"), mk_row(repo="b", email="b@y.com")]
    out = summarize(rows)
    assert set(out[0].email.split(";")) == {"a@x.com", "b@y.com"}


def test_csv_omits_line_columns_when_disabled(tmp_path):
    p = tmp_path / "o.csv"
    write_csv([mk_row()], p, mk_cfg(tmp_path, count_lines=False))
    header = p.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "additions" not in header
    assert "commits" in header


def test_csv_includes_line_columns_when_enabled(tmp_path):
    p = tmp_path / "o.csv"
    r = mk_row(additions=10, deletions=2, files_changed=3,
               additions_raw=99, deletions_raw=5)
    write_csv([r], p, mk_cfg(tmp_path, count_lines=True))
    header = p.read_text(encoding="utf-8-sig").splitlines()[0]
    for c in LINE_COLUMNS:
        assert c in header


def test_csv_uses_utf8_bom_for_excel(tmp_path):
    p = tmp_path / "o.csv"
    write_csv([mk_row(name="张三")], p, mk_cfg(tmp_path))
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")


def test_csv_roundtrip_preserves_values(tmp_path):
    p = tmp_path / "o.csv"
    write_csv([mk_row()], p, mk_cfg(tmp_path))
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["login"] == "alice"
    assert rows[0]["commits"] == "5"


# --- 用户全局约定：表格单元格内禁止 ** 加粗 ---

def test_markdown_table_cells_have_no_bold(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row()], p, mk_cfg(tmp_path))
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            assert "**" not in line, f"表格单元格禁止加粗: {line}"


def test_markdown_has_header_and_separator(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row()], p, mk_cfg(tmp_path))
    lines = [line for line in p.read_text(encoding="utf-8").splitlines()
             if line.startswith("|")]
    assert "repo" in lines[0]
    assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-", ":"}


def test_markdown_states_the_window(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row()], p, mk_cfg(tmp_path))
    text = p.read_text(encoding="utf-8")
    # 报表口径必须写明，闭区间显示为 2025-07-28 ~ 2026-07-28
    assert "2025-07-28" in text and "2026-07-28" in text


def test_markdown_escapes_pipe_in_values(tmp_path):
    p = tmp_path / "o.md"
    write_markdown([mk_row(name="a|b")], p, mk_cfg(tmp_path))
    body = [line for line in p.read_text(encoding="utf-8").splitlines()
            if line.startswith("|") and "alice" in line][0]
    assert r"a\|b" in body


def test_json_is_valid_and_includes_meta(tmp_path):
    p = tmp_path / "o.json"
    write_json([mk_row()], p, mk_cfg(tmp_path))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["meta"]["since"] == "2025-07-28"
    assert data["meta"]["until"] == "2026-07-28"
    assert data["contributors"][0]["login"] == "alice"


def test_json_notes_line_metric_caveat_when_enabled(tmp_path):
    p = tmp_path / "o.json"
    write_json([mk_row()], p, mk_cfg(tmp_path, count_lines=True))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "caveat" in json.dumps(data["meta"], ensure_ascii=False)
