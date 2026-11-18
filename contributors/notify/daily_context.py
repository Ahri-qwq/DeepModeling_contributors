"""Verified branch relationships and references to previously sent daily stories."""

from datetime import datetime
from urllib.parse import quote

import requests


PR_FIELDS = ("base_branch", "base_repo", "head_branch", "head_repo", "head_sha", "merge_sha", "merged_at")


def pr_facts(data, end):
    facts = {}
    for side in ("base", "head"):
        branch = data.get(side) or {}
        repo = (branch.get("repo") or {}).get("full_name")
        name = branch.get("ref")
        if isinstance(repo, str) and isinstance(name, str):
            facts.update({f"{side}_repo": repo, f"{side}_branch": name})
    if (data.get("head") or {}).get("sha"):
        facts["head_sha"] = data["head"]["sha"]
    merged_at = data.get("merged_at")
    if merged_at and datetime.fromisoformat(merged_at.replace("Z", "+00:00")) < end:
        facts["merged_at"] = merged_at
        if data.get("merge_commit_sha"):
            facts["merge_sha"] = data["merge_commit_sha"]
    return facts


def previous_reports(records, prior):
    """Only match existing, public evidence IDs to actually sent stories."""
    reported = {}
    for story in prior:
        refs = [story["topic"], *(source["label"] for source in story.get("sources", []))]
        for ref in refs:
            if ref in records:
                reported.setdefault(ref, story)
    return reported


def _report_day(report):
    day = report.get("report_date", "")
    return f"{int(day[5:7])} 月 {int(day[8:10])} 日" if day else "此前"


def _same_branch(base, head):
    return bool(base.get("base_branch") and head.get("head_branch") and base.get("base_repo") and head.get("head_repo")
                and base["base_branch"] == head["head_branch"]
                and base.get("base_repo", "").casefold() == head.get("head_repo", "").casefold())


def branch_context(evidence, reported, fetch):
    """Establish a few explicit PR relationships, including squash merges."""
    cache = {}
    checks = 0

    def get(path):
        if path not in cache:
            cache[path] = fetch(path)
        return cache[path]

    def includes(parent, child):
        nonlocal checks
        if not child.get("merge_sha") or not parent.get("merge_sha") or not parent.get("head_sha") or checks >= 4:
            return False
        checks += 1
        prefix = "/repos/" + quote(parent["base_repo"], safe="/")
        child_sha = quote(child["merge_sha"], safe="")
        merge_sha = quote(parent["merge_sha"], safe="")
        head_sha = quote(parent["head_sha"], safe="")
        try:
            comparison = get(f"{prefix}/compare/{child_sha}...{merge_sha}")
            if comparison.get("status") in {"ahead", "identical"}:
                return True
            # Squash/rebase can remove ancestry. Verify the PR head contains the
            # child, then require the final merged tree to equal that head tree.
            comparison = get(f"{prefix}/compare/{child_sha}...{head_sha}")
            if comparison.get("status") not in {"ahead", "identical"}:
                return False
            head = get(f"{prefix}/git/commits/{head_sha}")
            merge = get(f"{prefix}/git/commits/{merge_sha}")
            return bool(head.get("tree", {}).get("sha") and head["tree"]["sha"] == merge.get("tree", {}).get("sha"))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return False

    for ref, current in evidence.items():
        if not current["current"] or current["kind"] != "pr" or current.get("resource_topic"):
            continue
        context = []
        previous = reported.get(ref)
        previous_stage = (previous.get("source_facts", {}).get(ref, {}).get("stage") or
                          (previous["stage"] if previous["topic"] == ref else None)) if previous else None
        if previous_stage == "有修改提案" and current["stage"] == "已合并":
            context.append({"kind": "proposal_merged", "source": ref, "report": previous,
                            "note": f"{_report_day(previous)}报道的提案现已合并。"})
        for old_ref, report in reported.items():
            if old_ref == ref or old_ref not in evidence:
                continue
            old = evidence[old_ref]
            if old["kind"] != "pr" or old["repo"].casefold() != current["repo"].casefold():
                continue
            if _same_branch(current, old) and old_ref in current.get("follows_prs", []):
                context.append({"kind": "follow_up", "source": old_ref, "report": report,
                                "note": f"接续{_report_day(report)}报道的整合工作，本批{'已先合入' if current['stage'] == '已合并' else '拟先合入'}其整合分支。"})
            elif current["stage"] == "已合并" and _same_branch(old, current) and includes(current, old):
                context.append({"kind": "includes_reported", "source": old_ref, "report": report,
                                "note": f"本次整体合入，包含{_report_day(report)}已报道的补充改动。"})
        if context:
            current["report_context"] = context


def annotate(stories, evidence):
    """Archive evidence snapshots and render one concise continuity sentence."""
    for story in stories:
        primary = story.get("primary_source", story["topic"])
        row = evidence.get(primary, {})
        facts = {key: row[key] for key in PR_FIELDS if key in row}
        if facts:
            story["primary_pr"] = facts
        contexts = row.get("report_context", [])
        # The containing merge is more informative than repeating an earlier
        # proposal. Keep one sentence in the card and all verified links in data.
        if contexts:
            best = max(contexts, key=lambda item: {"proposal_merged": 0, "follow_up": 1, "includes_reported": 2}[item["kind"]])
            story["context_note"] = best["note"]
            story["report_context"] = contexts
            ref = best["source"]
            if ref not in {source["label"] for source in story["sources"]}:
                story["sources"].append({"label": ref, "url": evidence[ref]["url"]})
        story["source_facts"] = {source["label"]: {"stage": evidence[source["label"]]["stage"],
                                                   **{key: evidence[source["label"]][key] for key in PR_FIELDS
                                                      if key in evidence[source["label"]]}}
                                 for source in story["sources"] if source["label"] in evidence}
    return stories
