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


TEMPLATE = """<title>飞书卡片预览 · DeepModeling 社区日报</title>
<style>
:root{{
  --ground:#f2f3f5; --surface:#ffffff; --ink:#1f2329; --muted:#8f959e;
  --line:#dee0e3; --brand:#3370ff; --panel:#ffffff; --shadow:rgba(31,35,41,.12);
}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{
    --ground:#14161a; --surface:#1f2329; --ink:#e7e9eb; --muted:#8f959e;
    --line:#2f343a; --brand:#4e83fd; --panel:#1a1d21; --shadow:rgba(0,0,0,.5);
  }}
}}
:root[data-theme="dark"]{{
  --ground:#14161a; --surface:#1f2329; --ink:#e7e9eb; --muted:#8f959e;
  --line:#2f343a; --brand:#4e83fd; --panel:#1a1d21; --shadow:rgba(0,0,0,.5);
}}
*{{box-sizing:border-box}}
body{{
  margin:0; padding:32px 24px 56px; background:var(--ground); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  display:flex; flex-direction:column; align-items:center; gap:20px;
}}
.eyebrow{{
  width:100%; max-width:560px; font-size:12px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--muted);
}}
.card{{
  width:100%; max-width:560px; background:var(--surface); border-radius:8px;
  overflow:hidden; box-shadow:0 2px 14px var(--shadow);
}}
.hd{{
  background:var(--brand); color:#fff; padding:12px 16px;
  font-size:15px; font-weight:600;
}}
.bd{{padding:14px 16px; font-size:14px; line-height:1.75; overflow-wrap:anywhere}}
.bd a{{color:var(--brand); text-decoration:none}}
.bd a:hover{{text-decoration:underline}}
.bd a:focus-visible{{outline:2px solid var(--brand); outline-offset:2px}}
.au{{color:var(--muted)}}
.notes{{
  width:100%; max-width:560px; background:var(--panel); border:1px solid var(--line);
  border-radius:8px; padding:14px 16px; font-size:13px; line-height:1.7;
}}
.notes h2{{margin:0 0 8px; font-size:12px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--muted); font-weight:600}}
.notes ul{{margin:0; padding-left:18px}}
.notes li{{margin:3px 0}}
.notes code{{
  background:var(--ground); border:1px solid var(--line); border-radius:3px;
  padding:0 4px; font-size:12px;
}}
.meta{{
  width:100%; max-width:560px; font-size:12px; color:var(--muted);
  font-variant-numeric:tabular-nums;
}}
</style>
<p class="eyebrow">飞书卡片渲染预览</p>
<div class="card"><div class="hd">{title}</div><div class="bd">{body}</div></div>
<div class="notes">
  <h2>这次改动要看的三处</h2>
  <ul>
    <li>每条标题末尾的 <code>@提交者</code>，取原始 login，不做身份归并</li>
    <li>issue #5950 若作者取不到会整个省略后缀，此处 API 有值故照常显示</li>
    <li>底部四项累计，数字取自 <code>summary.csv</code> 已有列，不额外发 API 请求</li>
  </ul>
</div>
<p class="meta">卡片 {size} 字节 / 上限 18432 · 数据来自 08-10 缓存，非实时 · 实际样式以飞书客户端为准</p>"""


def main():
    payload = card.render(build_digest())
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    out = TEMPLATE.format(
        title=html.escape(payload["card"]["header"]["title"]["content"]),
        body=md_to_html(payload["card"]["elements"][0]["text"]["content"]),
        size=size)
    with open("preview_card.html", "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"已生成 preview_card.html（{size} 字节）")


if __name__ == "__main__":
    main()
