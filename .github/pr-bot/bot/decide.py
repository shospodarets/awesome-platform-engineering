"""Combine deterministic gates with Claude's review into one action.

Claude proposes; this module disposes. A merge always needs hard evidence the code can check,
so a persuasive PR body or a prompt-injected web page cannot talk the bot into merging.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from .readme import normalize_url, registrable_domain

MERGE_MIN_CONFIDENCE = 0.65
MERGE_MIN_CONFIDENCE_NO_REPO = 0.8
DECLINE_MIN_CONFIDENCE = 0.7
STAR_BAR = 50
FEEDBACK_BAR = 5
ESTABLISHED_STARS = 1000
ESTABLISHED_AGE_DAYS = 180


@dataclass
class Action:
    kind: str                      # merge | decline | request_changes | escalate | skip
    public_reason: str = ""
    note: str = ""
    changes: list[str] = field(default_factory=list)
    duplicate_of: dict | None = None
    gate: str = ""                 # which rule produced the action


def pre_llm(ev: dict, owner: str) -> Action | None:
    """Gates that decide without a model call. Returns None when the model should review."""
    pr = ev["pr"]
    if pr.get("draft"):
        return Action("skip", gate="draft")
    if pr.get("author_association") in ("OWNER", "MEMBER", "COLLABORATOR") or ev["author"]["login"].lower() == owner:
        return Action("skip", gate="maintainer PR")
    entries = ev["entries"]
    injected = [h for h in ev.get("hard_findings", []) if h.startswith("hidden instructions")]
    if injected and "HTML comment" in injected[0]:
        return Action("decline", "The PR contains hidden text addressed to automated reviewers, so it cannot be "
                      "accepted.", injected[0], gate="prompt injection")
    if injected:
        return Action("escalate", "", injected[0], gate="possible prompt injection")
    if not ev["readme_only"]:
        return Action("escalate", "it changes files other than the README",
                      "Changes non-README files: " + ", ".join(ev["files"]), gate="non-readme files")
    if entries["removed"]:
        names = ", ".join(e["name"] for e in entries["removed"])
        return Action("escalate", "it removes existing entries", f"Removes entries: {names}", gate="removes entries")
    if [l for l in entries.get("other_added_lines", []) if l.strip()]:
        return Action("escalate", "it changes the README beyond list entries",
                      "Non-entry lines added: " + " | ".join(entries["other_added_lines"][:5])[:400],
                      gate="non-entry changes")
    if not entries["added"] and not entries["modified"]:
        return Action("escalate", "it does not add a list entry in the usual format",
                      "No added or edited list entry detected", gate="no entry")
    exact = [d for d in entries["duplicates"]
             if d["added"]["url"].rstrip("/").lower() == d["existing"]["url"].rstrip("/").lower()
             or (d["added"]["github_repo"] and d["added"]["github_repo"].lower() == (d["existing"]["github_repo"] or "").lower())]
    if exact and len(exact) == len(entries["added"]) and not entries["modified"]:
        return Action("decline", duplicate_of=exact[0]["existing"], gate="exact duplicate",
                      note=f"Already listed: {exact[0]['existing']['name']}")
    return None


def same_host_link_fix(ev: dict) -> bool:
    """An edit that only fixes an existing entry's link on the same site (or its redirect target)."""
    mods, added = ev["entries"]["modified"], ev["entries"]["added"]
    if not mods or added:
        return False
    targets = {registrable_domain(lk["final_url"]) for lk in ev.get("link_check", []) if lk.get("final_url")}
    return all(registrable_domain(m["new"]["url"]) in ({registrable_domain(m["old"]["url"])} | targets) for m in mods)


def _vendor_domains(ev: dict) -> set[str]:
    doms = set()
    for e in ev["entries"]["added"] + [m["new"] for m in ev["entries"]["modified"]]:
        if not e.get("github_repo"):
            doms.add(registrable_domain(e["url"]))
    for r in ev["repos"]:
        if r.get("homepage"):
            doms.add(registrable_domain(r["homepage"]))
    return {d for d in doms if d}


def popularity_proven(ev: dict, review: dict) -> tuple[bool, str]:
    for r in ev["repos"]:
        if not r.get("linked_to_entry", True):
            continue  # a repo merely mentioned in the PR body proves nothing about the entry
        if r.get("exists") and (r.get("stars_as_of") or r.get("stars_now", 0)) >= STAR_BAR and not r.get("fake_star_flags") \
                and not r.get("archived"):
            return True, f"{r['repo']} has {r.get('stars_as_of') or r['stars_now']} stars without fake-star signals"
    vendor = _vendor_domains(ev) | {"github.com"}
    owners = {r["repo"].split("/")[0].lower() for r in ev["repos"]}
    seen = {normalize_url(u) for u in review.get("_seen_urls") or []}
    indep = []
    for item in review.get("independent_evidence") or []:
        url = item.get("url", "")
        if normalize_url(url) not in seen:
            continue  # only pages the model actually retrieved through search/fetch count

        dom = registrable_domain(url)
        if not dom or dom in vendor:
            continue
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        first = (parts.path.strip("/").split("/") or [""])[0].lower()
        if (host in ("github.com", "gitlab.com") and first in owners) or any(host == f"{o}.github.io" for o in owners):
            continue
        indep.append(dom)
    if len(indep) >= FEEDBACK_BAR and len(set(indep)) >= 3:
        return True, f"{len(indep)} independent evidence links across {len(set(indep))} domains"
    return False, "no repository with 50+ organic stars and fewer than 5 independent usage sources"


def _established(ev: dict) -> bool:
    return any(r.get("linked_to_entry", True) and r.get("stars_now", 0) >= ESTABLISHED_STARS and r.get("age_days_as_of", 0) >= ESTABLISHED_AGE_DAYS
               and not r.get("fake_star_flags") for r in ev["repos"])


def post_llm(ev: dict, review: dict) -> Action:
    decision = review["decision"]
    conf = float(review.get("confidence") or 0)
    reason = review.get("reason_for_submitter", "")
    note = review.get("maintainer_note", "")
    lint = ev.get("lint") or {}
    category = review.get("category")

    if decision == "merge":
        if ev["entries"]["duplicates"]:
            return Action("escalate", "it may duplicate an existing entry", note, gate="possible duplicate")
        entries = ev["entries"]["added"] + [m["new"] for m in ev["entries"]["modified"]]
        links_github = any(e.get("github_repo") for e in entries)
        in_tooling = any((e.get("section") or "").lower().startswith("tooling") for e in entries)
        if not same_host_link_fix(ev) and (category in ("tool", "list") or links_github or in_tooling
                                           or any(r.get("linked_to_entry") for r in ev["repos"])):
            ok, why = popularity_proven(ev, review)
            if not ok:
                return Action("escalate", "", f"Model proposed merge but the hard bar is not proven ({why}). {note}",
                              gate="merge without proof")
            if conf < MERGE_MIN_CONFIDENCE:
                return Action("escalate", "", f"Low-confidence merge ({conf:.2f}). {note}", gate="low confidence merge")
        elif conf < MERGE_MIN_CONFIDENCE_NO_REPO:
            return Action("escalate", "", f"Low-confidence merge of a {category} ({conf:.2f}). {note}",
                          gate="low confidence merge")
        if not ev.get("lint"):
            return Action("defer", "", "No awesome-lint result for this commit yet", gate="lint pending")
        if lint.get("ok") is False:
            return Action("request_changes", "The entry looks good, but the README linter (awesome-lint) fails on "
                          "this change:", note,
                          changes=[f"Fix the awesome-lint error: {lint.get('summary') or 'see the failed check'}"],
                          gate="merge blocked by lint")
        return Action("merge", reason, note, gate="merge")

    if decision == "decline":
        if conf < DECLINE_MIN_CONFIDENCE:
            return Action("escalate", "", f"Low-confidence decline ({conf:.2f}): {reason} {note}",
                          gate="low confidence decline")
        if _established(ev) and not ev["entries"]["duplicates"]:
            return Action("escalate", "", f"Model declined an established project: {reason} {note}",
                          gate="established project declined")
        return Action("decline", reason, note, gate="decline")

    if decision == "request_changes":
        changes = [c for c in review.get("requested_changes") or [] if c.strip()]
        if not changes:
            return Action("escalate", "", f"Changes requested without a list. {note}", gate="empty changes")
        return Action("request_changes", reason, note, changes=changes, gate="request changes")

    return Action("escalate", reason, note, gate="model escalated")
