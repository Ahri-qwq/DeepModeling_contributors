"""把渲染好的飞书卡片转成 HTML 预览，方便在浏览器里看实际观感。

只读缓存与已落盘的 CSV，不发任何网络请求、不碰事件库。
"""
import csv
import html
import json
import re
import sys

sys.path.insert(0, ".")

from contributors.events import (Event, StateChange, extract_issue_events,
                                 extract_pr_events)
from contributors.main import _year_totals
from contributors.notify import card
from contributors.notify.digest import build
from contributors.output import Row, summarize
from contributors.store import UpsertResult

CACHE = ".cache/api/deepmd-kit-2026-08-10.json"
WANT_PR = [5946, 5951, 5955, 5956, 5960, 5961, 5962, 5963]
WANT_ISSUE = [5947, 5948, 5949, 5950, 5952, 5953, 5954, 5957, 5959]


def load_rows():
    rows = []
    with open("output/all/by_repo.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows.append(Row(
                repo=r["repo"], login=r["login"], name=r["name"],
                email=r["email"], github_url=r["github_url"],
                commits=int(r["commits"] or 0),
                commits_loose=int(r["commits_loose"] or 0),
                commits_not_in_upstream=int(r["commits_not_in_upstream"] or 0),
                pr_created=int(r["pr_created"] or 0),
                pr_merged=int(r["pr_merged"] or 0),
                pr_reviewed=int(r["pr_reviewed"] or 0),
                issue_created=int(r["issue_created"] or 0),
                issue_commented=int(r["issue_commented"] or 0),
                is_fork=r["is_fork"] == "True", upstream=r["upstream"],
                upstream_family=r["upstream_family"],
                is_bot=r["is_bot"] == "True",
                is_ai_assistant=r["is_ai_assistant"] == "True"))
    return rows


def build_digest():
    with open(CACHE, encoding="utf-8") as fh:
        raw = json.load(fh)
    prs = extract_pr_events(raw.get("prs", []), "deepmd-kit", "deepmodeling")
    iss = extract_issue_events(raw.get("issues", []), "deepmd-kit",
                               "deepmodeling")

    new = [p for p in prs if p.number in WANT_PR]
    for p in new:
        p.state = "open"
    new += [i for i in iss if i.number in WANT_ISSUE]
    new += [Event("commit", "deepmd-kit", None, str(i), "x", "u", None,
                  "2026-08-10T00:00:00+00:00", None) for i in range(17)]

    chg = [StateChange(m.key, "pr", "deepmd-kit", m.number, m.title, m.url,
                       "open", "merged", m.author_login)
           for m in prs if m.number == 5958]

    rows = load_rows()
    t = _year_totals(rows)
    return build(UpsertResult(new, chg),
                 last_notify_at="2026-08-10T02:00:00+00:00",
                 total_contributors=len(summarize(rows)),
                 total_commits=t["commits"], total_prs_merged=t["pr_merged"],
                 total_prs_created=t["pr_created"],
                 total_issues=t["issue_created"],
                 repos_processed=34, repos_total=34)


def md_to_html(s: str) -> str:
    """把卡片的 lark_md 正文近似还原成 HTML，仅用于预览。"""
    s = html.escape(s)
    # card 里对 [ ] 做过转义，预览时还原回来
    s = s.replace("\\[", "[").replace("\\]", "]")
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(@[A-Za-z0-9_.-]+)", r'<span class="au">\1</span>', s)
    return s.replace("\n", "<br>")


def _elements_to_html(elements: list[dict]) -> str:
    blocks = []
    for element in elements:
        tag = element["tag"]
        if tag == "hr":
            blocks.append("<hr>")
        elif tag == "column_set":
            css = "footer" if element.get("background_style") else "story"
            columns = "".join(_elements_to_html(col["elements"]) for col in element["columns"])
            blocks.append(f'<section class="{css}">{columns}</section>')
        elif tag in ("div", "markdown"):
            data = element["text"] if tag == "div" else element
            content = data["content"]
            body = html.escape(content) if data.get("tag") == "plain_text" else md_to_html(content)
            css = "notation" if data.get("text_size") == "notation" else ""
            blocks.append(f'<div class="{css}">{body}</div>')
    return "".join(blocks)


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepModeling 社区日报预览</title>
<style>
:root{{
  color-scheme:light dark;
  --ground:#f2f3f5; --surface:#fff; --ink:#1f2329; --muted:#646a73;
  --line:#dee0e3; --link:#1456f0; --header:#eef3ff; --title:#244bb0;
  --footer:#f5f6f7;
}}
@media (prefers-color-scheme:dark){{
  :root{{
    --ground:#161719; --surface:#222326; --ink:#ebebeb; --muted:#a6a6a6;
    --line:#434343; --link:#75a4ff; --header:#152340; --title:#c2d4ff;
    --footer:#292929;
  }}
}}
*{{box-sizing:border-box}}
body{{
  margin:0; padding:24px 12px; background:var(--ground); color:var(--ink);
  font-family:system-ui,"Microsoft YaHei",sans-serif;
}}
.card{{
  max-width:600px; margin:0 auto; background:var(--surface);
  border:1px solid var(--line); border-radius:10px; overflow:hidden;
}}
.hd{{background:var(--header); color:var(--title); padding:12px 16px}}
h1{{font-size:18px; line-height:1.4; margin:0}}
.date{{font-size:13px; margin-top:3px}}
.bd{{padding:12px 16px; display:grid; gap:8px; font-size:14px; line-height:1.55; overflow-wrap:anywhere}}
.story,.footer{{display:grid; gap:4px}}
.footer{{background:var(--footer); padding:8px 12px}}
hr{{width:100%; margin:2px 0 0; border:0; border-top:1px solid var(--line)}}
.notation{{font-size:12px; color:var(--muted)}}
a{{color:var(--link); text-underline-offset:2px}}
.meta{{max-width:600px; margin:12px auto 0; color:var(--muted); font-size:12px}}
</style>
<article class="card">
  <header class="hd"><h1>{title}</h1><div class="date">{subtitle}</div></header>
  <div class="bd">{body}</div>
</article>
<p class="meta">{note}</p>
</html>
"""


def render_preview(payload: dict, note: str) -> str:
    report = payload["card"]
    header = report["header"]
    return TEMPLATE.format(
        title=html.escape(header["title"]["content"]),
        subtitle=html.escape(header.get("subtitle", {}).get("content", "")),
        body=_elements_to_html(report["body"]["elements"]),
        note=html.escape(note),
    )


def main():
    payload = card.render(build_digest())
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    out = render_preview(payload, f"卡片 {size} 字节，数据来自 08-10 缓存。实际样式以飞书客户端为准。")
    with open("preview_card.html", "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"已生成 preview_card.html（{size} 字节）")


if __name__ == "__main__":
    main()
