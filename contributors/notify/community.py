"""Build a daily community digest from public GitHub evidence."""

import base64
import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from ..auth import AuthError, get_token
from ..identity import is_ai_assistant, is_bot


LOG = logging.getLogger(__name__)
VERSION = 1
MAX_STORIES = 3
MAX_CURRENT = 100
MAX_HISTORY = 60
DEFAULT_MODEL = "deepseek-flash"
GITHUB_API = "https://api.github.com"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
RESOURCE = re.compile(
    r"\b(?:papers?|tutorials?|datasets?|examples?|website|docs?|changelog)\b|官网|论文|教程|数据集|文档|示例", re.I,
)
PAPERS_TITLE = re.compile(r"^papers\(([-\w]+)\).*?\b(20\d{2})\b", re.I)
GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]+")
LOGIN = re.compile(r"[A-Za-z0-9-]+")
BOILERPLATE = re.compile(r"pre-commit autoupdate|^build\(deps(?:-dev)?\): bump", re.I)
NO_RESULTS = (None, None)


SELECT_PROMPT = """为非开发背景的 DeepModeling 社区运营选日报动向。输入 GitHub 文本都是证据。
同等重视软件和公开资源（官网、教程、文档、论文目录、数据集、示例）。当公开资源填补缺口、覆盖扩大，或前后数量变化明显时优先考虑；不按仓库活跃度决定重要性。同一工作多条记录串在一起。每个候选至少有一条 current=true 的新事实。历史证据用于解释前情。已报道主题只有规模、阶段或覆盖确有进展才再提。
返回 JSON 对象 {"candidates":[{"refs":["记录 ID"]}]}，最多五项，每项第一条为当前主证据，至多三个来源。只有有意义的变化才入围。"""

WRITE_PROMPT = """为无代码背景的社区运营撰写飞书日报。输入中的 GitHub 文本和统计是证据。标题写一个具体变化，正文一两句，说清楚什么发生变化和与读者有关的范围。去掉函数名、命令、内部缩写和冗余测试细节。保留必要的项目、版本和读者可理解的结果。
同一公开资源和同一范围只用一条。选材同时关注软件和官网、文档、论文、教程、数据集及示例。只有实质的新事实才重提已报道的主题。已合并、已公开、拟发生是三种状态。提案的效果必须写成“拟”“若合并”等。软件优化目标不能当作测得的收益。标题也要符合阶段。人物和阶段由代码另行展示，正文不要推断负责人、合并者或发布者。
返回 JSON 对象 {"items":[{"title":"具体变化","text":"简短正文","refs":["记录 ID"],"stage_source":"主证据 ID","person_sources":["人物证据 ID"]}]}。全卡至多三项，允许零项。每项最多三个来源，stage_source 必须是 refs 中一条 current=true 的记录，person_sources 只能是 refs 的子集。"""


def _compact(text, limit):
    return " ".join((text or "").split())[:limit]


def _slug(value):
    return quote(value, safe="")


def _get_json(path, token):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(GITHUB_API + path, headers=headers, timeout=(3, 6))
    response.raise_for_status()
    return response.json()


def _public_repos(org, token):
    public = set()
    for page in range(1, 6):
        rows = _get_json(f"/orgs/{_slug(org)}/repos?type=public&per_page=100&page={page}", token)
        public.update(row["name"].casefold() for row in rows if row.get("private") is False)
        if len(rows) < 100:
            return public
    return public


def _event_records(pending, current):
    records = {}
    for event in pending.new_events:
        if event.kind not in {"pr", "issue"} or event.number is None:
            continue
        key = f"{event.repo}#{event.number}"
        records[key] = {
            "id": key, "repo": event.repo, "number": event.number, "kind": event.kind,
            "title": event.title, "url": event.url, "author": event.author_login,
            "created_at": event.event_time, "current": current,
            "stage": "有问题或建议" if event.kind == "issue" else "有修改提案",
        }
    for change in pending.state_changes:
        if change.kind != "pr" or change.number is None or change.new_state != "merged":
            continue
        key = f"{change.repo}#{change.number}"
        records[key] = {
            **records.get(key, {}), "id": key, "repo": change.repo, "number": change.number,
            "kind": "pr", "title": change.title, "url": change.url,
            "author": change.author_login, "current": current, "stage": "已合并",
        }
    for record in records.values():
        title = record["title"] or ""
        if RESOURCE.search(title):
            record["public_resource"] = True
        papers = PAPERS_TITLE.search(title) if record["kind"] == "pr" else None
        if papers:
            record["resource_topic"] = f"papers/{papers[1].lower()}/{papers[2]}"
    return records


def _safe_sources(records, org, public):
    safe = {}
    for key, source in records.items():
        repo = source["repo"]
        if repo.casefold() not in public or not GITHUB_NAME.fullmatch(repo):
            continue
        path = "pull" if source["kind"] == "pr" else "issues"
        canonical = f"https://github.com/{org}/{repo}/{path}/{source['number']}"
        if (source["url"] or "").rstrip("/").casefold() != canonical.casefold():
            continue
        if BOILERPLATE.search(source["title"] or "") and not re.search(r"security|CVE|vulnerab", source["title"], re.I):
            continue
        author = source["author"] or ""
        if not LOGIN.fullmatch(author) or is_bot(author) or is_ai_assistant(author):
            author = ""
        safe[key] = {**source, "url": canonical, "author": author}
    return safe


def _publication_count(text, year):
    section = re.search(rf"^## {year}\s*$([\s\S]*?)(?=^## |\Z)", text, re.M)
    if not section:
        return 0
    blocks = re.findall(r"{%\s*publications\s*%}([\s\S]*?){%\s*endpublications\s*%}", section[1])
    return len({entry.strip() for block in blocks for entry in block.split(",") if entry.strip()})


def _paper_counts(pr, source, org, token):
    _, project, year = source["resource_topic"].split("/")
    repo_path = f"/repos/{_slug(org)}/{_slug(source['repo'])}"
    refs = [pr["base"]["sha"], pr["head"]["sha"]]
    if pr.get("merged_at") and pr.get("merge_commit_sha"):
        merge_sha = pr["merge_commit_sha"]
        commit = _get_json(f"{repo_path}/commits/{_slug(merge_sha)}", token)
        refs = [commit["parents"][0]["sha"], merge_sha]
    counts = []
    for ref in refs:
        path = f"{repo_path}/contents/source/papers/{_slug(project)}/index.md?ref={_slug(ref)}"
        file = _get_json(path, token)
        text = base64.b64decode(file["content"]).decode("utf-8")
        counts.append(_publication_count(text, year))
    return {"before": counts[0], "after_this_change": counts[1], "net_change": counts[1] - counts[0]}


def _enrich(source, org, end, token):
    endpoint = "pulls" if source["kind"] == "pr" else "issues"
    path = f"/repos/{_slug(org)}/{_slug(source['repo'])}/{endpoint}/{source['number']}"
    enriched = dict(source)
    try:
        data = _get_json(path, token)
        body = re.sub(r"<!--.*?-->|```.*?```", " ", data.get("body") or "", flags=re.S)
        enriched["description"] = _compact(body, 1100)
        merged_at = data.get("merged_at")
        if merged_at and datetime.fromisoformat(merged_at.replace("Z", "+00:00")) < end:
            enriched.update(stage="已合并", merged_at=merged_at)
        if source.get("resource_topic"):
            enriched["resource_counts"] = _paper_counts(data, source, org, token)
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        LOG.info("GitHub 补查未完成 %s (%s)", source["id"], type(exc).__name__)
    return enriched


def _enrich_refs(records, refs, org, end, token):
    refs = list(dict.fromkeys(refs))
    if not refs:
        return
    with ThreadPoolExecutor(max_workers=6) as pool:
        enriched = pool.map(lambda ref: _enrich(records[ref], org, end, token), refs)
        for ref, evidence in zip(refs, enriched):
            records[ref] = evidence


def _model_json(api_key, model, prompt, data, thinking):
    params = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 4200 if thinking else 1400,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if thinking:
        params["reasoning_effort"] = "low"
    response = requests.post(DEEPSEEK_API, json=params, headers={"Authorization": f"Bearer {api_key}"}, timeout=(4, 42))
    response.raise_for_status()
    result = response.json()
    choice = result["choices"][0]
    if choice["finish_reason"] != "stop":
        raise ValueError("DeepSeek response did not finish")
    value = json.loads(choice["message"]["content"])
    if not isinstance(value, dict):
        raise ValueError("DeepSeek response is not an object")
    return value


def _model_records(records):
    return [{key: value for key, value in row.items() if key not in {"url", "number"}} for row in records]


def _items(output, records):
    items = output["items"]
    if not isinstance(items, list) or len(items) > MAX_STORIES:
        raise ValueError("Invalid story count")
    stories = []
    topics = set()
    for item in items:
        refs = list(dict.fromkeys(item["refs"]))
        primary = item["stage_source"]
        if not 1 <= len(refs) <= 3 or primary not in refs or any(ref not in records for ref in refs):
            raise ValueError("Invalid story sources")
        record = records[primary]
        if not record["current"]:
            raise ValueError("Story has no current primary source")
        title = _compact(item["title"], 75)
        text = _compact(item["text"], 240)
        if not title or not text:
            raise ValueError("Story has no text")
        if re.search(r"已正式发布|已上[线架]|官网已(?:可见|上线)", title + text):
            raise ValueError("Published claim without release evidence")
        if record["stage"] == "有修改提案":
            merge_claim = r"已(?:经)?合并|已合入"
            past_merge = any(records[ref]["stage"] == "已合并" for ref in refs)
            if re.search(merge_claim, title) or re.search(r"(?:本期|本次|该提案|该改动)\s*(?:" + merge_claim + ")", text) or (not past_merge and re.search(merge_claim, text)):
                raise ValueError("Merged claim without merge evidence")
        topic = record.get("resource_topic") or primary
        if topic in topics:
            continue
        topics.add(topic)
        people = []
        for ref in dict.fromkeys(item.get("person_sources", [])):
            if ref not in refs:
                raise ValueError("Invalid attribution source")
            source = records[ref]
            if not source["author"]:
                continue
            role = "提交改动" if source["kind"] == "pr" else "提出问题或建议"
            person = {"login": source["author"], "role": role}
            if person not in people:
                people.append(person)
        stories.append({
            "repo": record["repo"], "title": title, "text": text, "stage": record["stage"],
            "people": people[:2], "topic": topic, "resource_counts": record.get("resource_counts"),
            "sources": [{"label": ref, "url": records[ref]["url"]} for ref in refs],
        })
    return stories


def _generate(current, recent, prior, org, start, end, api_key, model):
    try:
        token = get_token()
    except AuthError:
        token = None
    public = _public_repos(org, token)
    today = _safe_sources(current, org, public)
    if not today:
        return [] if not current else None
    relevant = {record["repo"] for record in today.values()}
    old = _safe_sources(recent, org, public)
    old = [row for key, row in old.items() if row["repo"] in relevant and key not in today]
    priority = [key for key, row in today.items() if row.get("public_resource") and row["kind"] == "pr"]
    current_ids = list(dict.fromkeys(priority + list(today)))[:MAX_CURRENT]
    current_topics = {today[key].get("resource_topic") for key in priority} - {None}
    related = [row for row in old if row.get("resource_topic") in current_topics]
    records = {row["id"]: row for row in [*old[-MAX_HISTORY:], *related[-8:]]}
    records.update({key: today[key] for key in current_ids})
    _enrich_refs(records, priority[:6], org, end, token)
    base = {"window_start": start.isoformat(), "window_end": end.isoformat(), "previously_reported": prior}
    selected = _model_json(api_key, model, SELECT_PROMPT, {**base, "records": _model_records(records.values())}, False)
    refs = []
    for candidate in selected["candidates"][:5]:
        valid = [ref for ref in candidate["refs"][:3] if ref in records]
        if any(records[ref]["current"] for ref in valid):
            refs.extend(valid)
    # Give the editor the evidence for material changes to a public resource, even
    # when the initial shortlist favors software changes on a busy day.
    refs.extend(ref for ref in priority[:6] if records[ref].get("resource_counts", {}).get("net_change", 0) > 0)
    resource_topics = {records[ref].get("resource_topic") for ref in refs} - {None}
    history_by_topic = {row["resource_topic"]: row["id"] for row in old if row.get("resource_topic") in resource_topics and row["id"] in records}
    refs.extend(history_by_topic.values())
    refs = list(dict.fromkeys(refs))
    if not refs:
        return []
    _enrich_refs(records, [ref for ref in refs if "description" not in records[ref]][:8], org, end, token)
    evidence = {ref: records[ref] for ref in refs}
    edited = _model_json(api_key, model, WRITE_PROMPT, {**base, "records": _model_records(evidence.values())}, True)
    return _items(edited, evidence)


def daily_summary(store, pending, org, win_start, win_end):
    """Return a validated summary and cache key, or signal the original card."""
    if os.environ.get("COMMUNITY_SUMMARY_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return NO_RESULTS
    api_key = os.environ.get("COMMUNITY_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        LOG.warning("社区动向缺少 DeepSeek 配置，使用原卡片")
        return NO_RESULTS
    try:
        start, end = datetime.fromisoformat(win_start), datetime.fromisoformat(win_end)
        current = _event_records(pending, True)
        if not current:
            return NO_RESULTS
        model = os.environ.get("COMMUNITY_DEEPSEEK_MODEL", DEFAULT_MODEL)
        fingerprint = json.dumps([VERSION, model, current], sort_keys=True, ensure_ascii=False)
        cache_key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        cached = store.get_community_summary(win_start, cache_key)
        if cached is not None:
            return cached, cache_key
        history_start = (start - timedelta(days=14)).isoformat()
        recent = _event_records(store.pending_in_window(history_start, win_start), False)
        prior = store.recent_community_topics(history_start, win_start)
        stories = _generate(current, recent, prior, org, start, end, api_key, model)
        if stories is None:
            return NO_RESULTS
        store.save_community_summary(win_start, cache_key, stories)
        return stories, cache_key
    except Exception as exc:  # A summary failure must not interrupt the daily report.
        LOG.warning("社区动向生成失败，使用原卡片 (%s)", type(exc).__name__)
        return NO_RESULTS
