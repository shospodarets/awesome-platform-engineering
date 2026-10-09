"""PR review bot for shospodarets/awesome-platform-engineering.

  python -m bot plan                       # (Actions) which PRs this event concerns -> $GITHUB_OUTPUT
  python -m bot run --prs '[77]'           # (Actions) review / triage those PRs and act
  python -m bot review 77 --dry-run        # local: show what the bot would do
  python -m bot backtest 31 40 41 ...      # local: replay historical PRs as of their creation time
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import comments as C
from .decide import Action, post_llm, pre_llm
from .gh import GitHub, GitHubError
from .signals import gather, parse_ts

OWNER = C.OWNER
BOT_LOGINS = {"github-actions[bot]", os.environ.get("PR_BOT_LOGIN", "github-actions[bot]")}
PAGES_WORKFLOW = "readme-to-site.yml"
STALE_DAYS = 14
MAX_TRIAGE_REPLIES = 3
MAX_REREVIEWS = 2
MAX_TRIAGE_CALLS = 6
MERGED_ALERT = re.compile(r"\b(remove|removal|take ?down|delete|wrong|incorrect|outdated|broken|legal|copyright|"
                          r"trademark|lawyer|unfair|complain\w*|misleading)\b", re.I)
LABELS = {
    "pr-bot:merged": ("0e8a16", "Merged by the PR review bot"),
    "pr-bot:declined": ("b60205", "Declined by the PR review bot"),
    "pr-bot:changes-requested": ("fbca04", "PR review bot asked for changes"),
    "pr-bot:approved-needs-rebase": ("1d76db", "Approved; waiting for the branch to be updated"),
    "needs-maintainer": ("d93f0b", "The PR review bot escalated this to the maintainer"),
    "pr-bot:error": ("000000", "The PR review bot failed on this PR"),
    "pr-bot:health": ("000000", "The PR review bot itself needs attention"),
}
BOT_LABELS = set(LABELS) - {"pr-bot:health"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


# -- state ---------------------------------------------------------------------------------

def issue_comments(gh: GitHub, number: int) -> list[dict]:
    return gh.paginate(gh.r(f"/issues/{number}/comments"), max_pages=5)


def bot_state(comments: list[dict]) -> dict:
    for c in reversed(comments):
        if (c.get("user") or {}).get("login") in BOT_LOGINS:
            st = C.read_state(c.get("body", ""))
            if st:
                return st
    return {}


def ensure_labels(gh: GitHub) -> None:
    if gh.dry_run:
        return
    existing = {l["name"] for l in gh.paginate(gh.r("/labels"), max_pages=3)}
    for name, (color, desc) in LABELS.items():
        if name not in existing:
            try:
                gh.write("POST", gh.r("/labels"), {"name": name, "color": color, "description": desc})
            except GitHubError:
                pass


def set_bot_label(gh: GitHub, pr: dict, label: str | None) -> None:
    current = {l["name"] for l in pr.get("labels", [])}
    for old in (current & BOT_LABELS) - {label}:
        try:
            gh.write("DELETE", gh.r(f"/issues/{pr['number']}/labels/{old}"))
        except GitHubError:
            pass
    if label and label not in current:
        gh.write("POST", gh.r(f"/issues/{pr['number']}/labels"), {"labels": [label]})


def comment(gh: GitHub, number: int, body: str) -> None:
    gh.write("POST", gh.r(f"/issues/{number}/comments"), {"body": body})


def save_state(gh: GitHub, comments: list[dict], state: dict) -> None:
    """Record progress (e.g. comments already triaged) without posting: rewrite the marker in the bot's
    latest state-bearing comment. Edits send no notifications."""
    for c in reversed(comments):
        if (c.get("user") or {}).get("login") in BOT_LOGINS and C.read_state(c.get("body", "")) is not None:
            body = C.STATE_RE.sub("", c["body"]).rstrip() + C.marker(state)
            gh.write("PATCH", gh.r(f"/issues/comments/{c['id']}"), {"body": body})
            return


# -- lint ----------------------------------------------------------------------------------

def load_lint(gh: GitHub, pr: dict, lint_dir: str | None) -> dict | None:
    sha = pr["head"]["sha"]
    if lint_dir:
        p = Path(lint_dir) / f"lint-{pr['number']}.json"
        if p.exists():
            data = json.loads(p.read_text())
            if data.get("sha") == sha:
                return data
    # fall back to the repository's own "Pull Requests Linter" check on the head commit
    try:
        runs = gh.get(gh.r(f"/commits/{sha}/check-runs"), per_page=50).get("check_runs", [])
    except GitHubError:
        return None
    for run in runs:
        if run.get("name") == "lint" and run.get("status") == "completed":
            return {"sha": sha, "ok": run.get("conclusion") == "success", "summary": "Pull Requests Linter check",
                    "source": "check-run"}
    return None


# -- acting --------------------------------------------------------------------------------

def dispatch_pages(gh: GitHub) -> None:
    try:
        gh.write("POST", gh.r(f"/actions/workflows/{PAGES_WORKFLOW}/dispatches"), {"ref": "main"})
    except GitHubError as e:
        log(f"pages dispatch failed: {e}")


def wait_mergeable(gh: GitHub, number: int) -> dict:
    pr = gh.get(gh.r(f"/pulls/{number}"))
    for _ in range(6):
        if pr.get("mergeable") is not None:
            break
        time.sleep(5)
        pr = gh.get(gh.r(f"/pulls/{number}"))
    return pr


def apply(gh: GitHub, pr: dict, action: Action, state: dict) -> str:
    n = pr["number"]
    author = pr["user"]["login"]
    state = {**state, "decision": action.kind, "sha": pr["head"]["sha"], "at": now().strftime("%Y-%m-%dT%H:%M:%SZ")}
    if action.kind in ("skip", "defer"):
        return action.kind
    if action.kind == "merge":
        if pr["state"] == "closed":
            try:
                gh.write("PATCH", gh.r(f"/pulls/{n}"), {"state": "open"})
            except GitHubError:  # e.g. the head branch was deleted
                comment(gh, n, C.reply(f"Thank you @{author}, the entry now meets the bar, but this PR cannot be "
                                       "reopened (its branch is gone). Please open a new PR with the same change "
                                       "and it will be merged.", {**state, "decision": "reopen-failed"}))
                return "reopen failed"
            if not gh.dry_run:
                reopened = gh.get(gh.r(f"/pulls/{n}"))
                if reopened["head"]["sha"] != pr["head"]["sha"]:
                    return "reopened (newer commits; the next run reviews them)"
        fresh = wait_mergeable(gh, n) if not gh.dry_run else pr
        if fresh.get("mergeable") is False:
            if state.get("prev_decision") != "approved-needs-rebase" or state.get("prev_sha") != pr["head"]["sha"]:
                comment(gh, n, C.approved_needs_rebase({**state, "decision": "approved-needs-rebase"}, author))
            set_bot_label(gh, pr, "pr-bot:approved-needs-rebase")
            return "approved-needs-rebase"
        gh.write("PUT", gh.r(f"/pulls/{n}/merge"), {
            "merge_method": "merge", "sha": pr["head"]["sha"],
            "commit_title": f"Merge pull request #{n} from {pr['head'].get('label', '')}",
            "commit_message": pr["title"],
        })
        comment(gh, n, C.merged(state, author))
        set_bot_label(gh, pr, "pr-bot:merged")
        dispatch_pages(gh)
        return "merged"
    if action.kind == "decline":
        if action.duplicate_of:
            body = C.declined_duplicate(action.duplicate_of["name"], action.duplicate_of["section"], state, author)
        else:
            body = C.declined(action.public_reason, state, author)
        comment(gh, n, body)
        set_bot_label(gh, pr, "pr-bot:declined")
        if pr["state"] == "open":
            gh.write("PATCH", gh.r(f"/pulls/{n}"), {"state": "closed"})
        return "declined"
    if action.kind == "request_changes":
        comment(gh, n, C.changes_requested(action.public_reason, action.changes, state, author))
        set_bot_label(gh, pr, "pr-bot:changes-requested")
        return "changes-requested"
    # escalate
    escalate(gh, pr, action, state)
    return "escalated"


def escalate(gh: GitHub, pr: dict, action: Action, state: dict) -> None:
    n = pr["number"]
    state = {**state, "decision": "escalate", "note": (action.note or "")[:300]}
    # Gmail keeps mail with Cc assign@noreply.github.com or the escalation marker unread and starred
    gh.write("POST", gh.r(f"/issues/{n}/assignees"), {"assignees": [OWNER]})
    set_bot_label(gh, pr, "needs-maintainer")
    comment(gh, n, C.escalated(action.public_reason, state, pr["user"]["login"]))


# -- review / triage -----------------------------------------------------------------------

def review_pr(gh: GitHub, pr: dict, lint_dir: str | None, extra_context: str = "", as_of: datetime | None = None,
              all_prs: list[dict] | None = None, use_llm: bool = True,
              maintainer_context: str = "") -> tuple[Action, dict, dict, dict]:
    lint = load_lint(gh, pr, lint_dir)
    ev = gather(gh, pr, as_of=as_of, lint=lint, all_prs=all_prs).to_dict()
    gate = pre_llm(ev, OWNER)
    if gate:
        return gate, ev, {}, {}
    if not use_llm:
        return Action("skip", gate="llm disabled"), ev, {}, {}
    from .llm import ModelNoDecision, review_submission
    try:
        review, usage = review_submission(ev, extra_context, maintainer_context)
    except ModelNoDecision as e:
        return Action("escalate", "", f"The model gave no usable decision: {e}", gate="no model decision"), ev, {}, {}
    action = post_llm(ev, review)
    if action.gate == "merge without proof" and add_identified_repo(gh, ev, review, as_of):
        action = post_llm(ev, review)
    return action, ev, review, usage


def add_identified_repo(gh: GitHub, ev: dict, review: dict, as_of: datetime | None) -> bool:
    """The entry links a website and Claude named its GitHub repository: verify that repository ourselves
    and accept it only if its homepage is the entry's own domain (so a famous unrelated repo cannot be used)."""
    from .readme import github_repo, registrable_domain
    from .signals import repo_evidence
    slug = github_repo(f"https://github.com/{(review.get('github_repository') or '').strip().strip('/')}")
    if not slug or any(r["repo"].lower() == slug.lower() for r in ev["repos"]):
        return False
    entry_domains = {registrable_domain(e["url"]) for e in ev["entries"]["added"] + [m["new"] for m in ev["entries"]["modified"]]
                     if not e.get("github_repo")}
    entry_domains |= {registrable_domain(lk["final_url"]) for lk in ev.get("link_check", []) if lk.get("final_url")}
    rep = repo_evidence(gh, slug, as_of or now(), ev["author"]["login"])
    if not rep.exists or not rep.homepage or registrable_domain(rep.homepage) not in entry_domains:
        return False
    rep.source, rep.linked_to_entry = "identified", True
    ev["repos"].append(rep.summary())
    return True


def conversation_text(comments: list[dict]) -> list[dict]:
    return [{"author": (c.get("user") or {}).get("login"), "at": c.get("created_at"),
             "body": C.strip_marker(c.get("body", ""))[:3000]} for c in comments]


def handle_review(gh: GitHub, pr: dict, lint_dir: str | None, reason: str, force: bool = False,
                  seen_up_to: int | None = None) -> dict:
    n = pr["number"]
    comments = issue_comments(gh, n)
    state = bot_state(comments)
    labels = {l["name"] for l in pr.get("labels", [])}
    if pr.get("merged_at"):
        return {"pr": n, "result": "already merged"}
    if "needs-maintainer" in labels and not force:
        return {"pr": n, "result": "waiting for maintainer"}
    if state.get("sha") == pr["head"]["sha"] and state.get("decision") not in (None, "error") and not force:
        return {"pr": n, "result": f"already reviewed ({state.get('decision')})"}
    convo = conversation_text(comments)
    extra = json.dumps([c for c in convo if c["author"] != OWNER][-10:]) if convo else ""
    owner_notes = "\n\n".join(c["body"] for c in convo if c["author"] == OWNER and c["body"])[-4000:]
    action, ev, review, usage = review_pr(gh, pr, lint_dir, extra_context=extra, all_prs=list_prs(gh),
                                          maintainer_context=owner_notes)
    state_out = {"v": 1, "prev_decision": state.get("decision"), "prev_sha": state.get("sha"),
                 "rereviews": state.get("rereviews", 0) + (1 if force else 0),
                 "triage_calls": state.get("triage_calls", 0),
                 # comments not yet triaged stay pending for their own issue_comment run
                 "last_comment_id": seen_up_to if seen_up_to is not None else (
                     state.get("last_comment_id", 0) if state else max([c["id"] for c in comments], default=0))}
    result = apply(gh, pr, action, state_out)
    return {"pr": n, "trigger": reason, "result": result, "gate": action.gate, "reason": action.public_reason,
            "note": action.note, "confidence": review.get("confidence"), "model_decision": review.get("decision"),
            "usage": usage}


def handle_comments(gh: GitHub, pr: dict, lint_dir: str | None) -> dict:
    n = pr["number"]
    comments = issue_comments(gh, n)
    state = bot_state(comments)
    labels = {l["name"] for l in pr.get("labels", [])}
    since = state.get("last_comment_id", 0)
    new = [c for c in comments if c["id"] > since and (c.get("user") or {}).get("login") not in BOT_LOGINS]
    top = max([c["id"] for c in comments], default=0)
    owner_cmds = [c for c in new if (c.get("user") or {}).get("login") == OWNER and "/bot review" in c.get("body", "")]
    if owner_cmds:
        return handle_review(gh, pr, lint_dir, "maintainer asked for a re-review", force=True, seen_up_to=top)
    if not state:
        # a PR the bot never reviewed (e.g. declined by hand before the bot existed): only its author's
        # reply, typically an appeal with new evidence, starts a review
        if any((c.get("user") or {}).get("login") == pr["user"]["login"] for c in new):
            return handle_review(gh, pr, lint_dir, "author replied before any bot review", seen_up_to=top)
        return {"pr": n, "result": "no bot history"}
    # only the PR author's replies drive the bot: third parties cannot withdraw, re-open or burn model calls
    new = [c for c in new if (c.get("user") or {}).get("login") == pr["user"]["login"]]
    if not new:
        return {"pr": n, "result": "no new comments from the author"}
    if state.get("triage_calls", 0) >= MAX_TRIAGE_CALLS:
        escalate(gh, pr, Action("escalate", "the conversation needs the maintainer",
                                "Triage cap reached on this PR"), {**state, "v": 1,
                                "last_comment_id": max(c["id"] for c in comments)})
        return {"pr": n, "result": "escalated (triage cap)"}
    if "needs-maintainer" in labels:
        return {"pr": n, "result": "waiting for maintainer"}
    if pr.get("merged_at"):  # thanks and chatter on merged PRs need no model call, but a complaint does
        if any(MERGED_ALERT.search(c.get("body", "")) for c in new):
            escalate(gh, pr, Action("escalate", "the contributor raised a concern about the merged entry",
                                    "Comment on a merged PR mentions a removal, correction or legal concern"),
                     {**state, "v": 1, "last_comment_id": top})
            return {"pr": n, "result": "escalated (merged PR concern)"}
        save_state(gh, comments, {**state, "last_comment_id": top})
        return {"pr": n, "result": "already merged"}
    bot_replies = sum(1 for c in comments if (c.get("user") or {}).get("login") in BOT_LOGINS
                      and (C.read_state(c.get("body", "")) or {}).get("decision") == "reply")
    from .llm import triage_comment
    summary = {"title": pr["title"], "state": pr["state"], "author": pr["user"]["login"],
               "merged": bool(pr.get("merged_at"))}
    latest = new[-1]
    joined = {"author": latest["user"]["login"], "body": "\n\n---\n\n".join(c.get("body", "") for c in new)[:6000]}
    new_ids = {c["id"] for c in new}
    prior = [c for c in comments if c["id"] not in new_ids]
    tri, usage = triage_comment(summary, conversation_text(prior)[-12:], joined, state)
    base_state = {**state, "v": 1, "last_comment_id": max(c["id"] for c in comments),
                  "triage_calls": state.get("triage_calls", 0) + 1}
    out = {"pr": n, "trigger": "comment", "intent": tri["intent"], "usage": usage}

    if tri["intent"] == "complaint" or tri["needs_maintainer"] or bot_replies >= MAX_TRIAGE_REPLIES:
        escalate(gh, pr, Action("escalate", "the contributor asked for a maintainer's decision",
                                tri.get("maintainer_note", "")), base_state)
        return {**out, "result": "escalated"}
    if tri["intent"] == "withdrawal":
        if pr["state"] == "open":
            comment(gh, n, C.withdrawn({**base_state, "decision": "withdrawn"}, pr["user"]["login"]))
            gh.write("PATCH", gh.r(f"/pulls/{n}"), {"state": "closed"})
        return {**out, "result": "closed (withdrawn)"}
    if tri["rereview"] or tri["intent"] == "new_evidence":
        if pr.get("merged_at"):
            save_state(gh, comments, base_state)
            return {**out, "result": "already merged"}
        if state.get("rereviews", 0) >= MAX_REREVIEWS:  # each re-review is a full web-research call
            escalate(gh, pr, Action("escalate", "the contributor provided more evidence after several re-reviews",
                                    tri.get("maintainer_note", "")), base_state)
            return {**out, "result": "escalated"}
        res = handle_review(gh, pr, lint_dir, "new evidence in comments", force=True, seen_up_to=top)
        return {**out, **res}
    if tri["intent"] in ("acknowledgement", "fix_pushed", "spam"):
        if tri["intent"] == "acknowledgement":
            try:
                gh.write("POST", gh.r(f"/issues/comments/{latest['id']}/reactions"), {"content": "+1"})
            except GitHubError:
                pass
        save_state(gh, comments, base_state)  # mark these comments as handled
        return {**out, "result": f"no reply ({tri['intent']})"}
    if tri.get("reply"):
        comment(gh, n, C.reply(tri["reply"], {**base_state, "decision": "reply"}))
        return {**out, "result": "replied"}
    escalate(gh, pr, Action("escalate", "", tri.get("maintainer_note", "")), base_state)
    return {**out, "result": "escalated"}


def handle_stale(gh: GitHub, pr: dict) -> dict | None:
    labels = {l["name"] for l in pr.get("labels", [])}
    if not labels & {"pr-bot:changes-requested", "pr-bot:approved-needs-rebase"}:
        return None
    updated = parse_ts(pr["updated_at"])
    if now() - updated < timedelta(days=STALE_DAYS):
        return None
    comments = issue_comments(gh, pr["number"])
    st = {**bot_state(comments), "decision": "stale-closed"}
    comment(gh, pr["number"], C.stale_closed(st))
    set_bot_label(gh, pr, "pr-bot:declined")
    gh.write("PATCH", gh.r(f"/pulls/{pr['number']}"), {"state": "closed"})
    return {"pr": pr["number"], "result": "closed (stale)"}


_PR_CACHE: list[dict] | None = None


def list_prs(gh: GitHub) -> list[dict]:
    global _PR_CACHE
    if _PR_CACHE is None:
        _PR_CACHE = gh.paginate(gh.r("/pulls"), max_pages=5, state="all")
    return _PR_CACHE


# -- event plumbing ------------------------------------------------------------------------

def plan(event_name: str, event: dict, dispatch_prs: str = "") -> list[int]:
    if event_name == "pull_request_target":
        pr = event["pull_request"]
        action = event.get("action")
        if action == "edited" and pr["state"] != "open":
            return []
        return [pr["number"]]
    if event_name == "issue_comment":
        issue = event.get("issue") or {}
        user = (event.get("comment") or {}).get("user") or {}
        if "pull_request" not in issue or user.get("login") in BOT_LOGINS or user.get("type") == "Bot":
            return []
        return [issue["number"]]
    if event_name in ("workflow_dispatch", "schedule"):
        if dispatch_prs.strip() == "selftest":
            return []
        if dispatch_prs and dispatch_prs not in ("open", "all-open"):
            return [int(x) for x in dispatch_prs.replace(" ", "").split(",") if x]
        return []  # resolved at run time (all open PRs) to keep the plan output small
    return []


def selftest_escalation(gh: GitHub) -> list[dict]:
    """Prove the escalation path end to end: an assignment by the bot must land unread in Gmail."""
    issue = gh.write("POST", gh.r("/issues"), {
        "title": "PR review bot: escalation self-test",
        "body": "Self-test of the escalation path: the bot assigns the maintainer, which must arrive as an "
                "unread email. This issue closes itself." + C.FOOTER})
    if not issue:
        return [{"pr": "selftest", "result": "dry-run"}]
    n = issue["number"]
    comment(gh, n, f"@{OWNER} escalation self-test: this email should be unread and starred "
                   f"({C.ESCALATION_MARKER})." + C.FOOTER)
    gh.write("POST", gh.r(f"/issues/{n}/assignees"), {"assignees": [OWNER]})
    time.sleep(20)
    gh.write("PATCH", gh.r(f"/issues/{n}"), {"state": "closed", "state_reason": "completed"})
    return [{"pr": f"issue #{n}", "result": "self-test sent"}]


def run(gh: GitHub, event_name: str, event: dict, prs: list[int], lint_dir: str | None) -> list[dict]:
    if event_name == "workflow_dispatch" and os.environ.get("PR_BOT_DISPATCH_PRS", "").strip() == "selftest":
        return selftest_escalation(gh)
    ensure_labels(gh)
    results = []
    if event_name in ("workflow_dispatch", "schedule") and not prs:
        open_prs = gh.paginate(gh.r("/pulls"), max_pages=5, state="open", sort="created", direction="asc")
        for pr in open_prs:
            try:
                stale = handle_stale(gh, pr)
                res = stale or handle_review(gh, pr, lint_dir, event_name)
                if not stale and str(res.get("result", "")).startswith(("already reviewed", "waiting")):
                    res = handle_comments(gh, pr, lint_dir)  # replies a missed or failed run left pending
                results.append(res)
            except Exception as e:  # keep going with the other PRs
                results.append(on_error(gh, pr, e))
        return results
    for n in prs:
        pr = gh.get(gh.r(f"/pulls/{n}"))
        try:
            if event_name == "issue_comment":
                results.append(handle_comments(gh, pr, lint_dir))
            elif event_name == "pull_request_target" and event.get("action") == "reopened":
                sender = (event.get("sender") or {}).get("login", "")
                if sender == OWNER:  # the maintainer overrode the bot: leave the PR to him
                    set_bot_label(gh, pr, "needs-maintainer")
                    results.append({"pr": n, "result": "reopened by the maintainer; left to him"})
                else:
                    results.append(handle_review(gh, pr, lint_dir, "reopened", force=True))
            else:
                results.append(handle_review(gh, pr, lint_dir, event_name))
        except Exception as e:
            results.append(on_error(gh, pr, e))
    return results


def on_error(gh: GitHub, pr: dict, err: Exception) -> dict:
    traceback.print_exc()
    try:  # add, don't replace: needs-maintainer and friends must survive a transient failure
        gh.write("POST", gh.r(f"/issues/{pr['number']}/labels"), {"labels": ["pr-bot:error"]})
    except Exception:
        pass
    return {"pr": pr["number"], "result": "error", "error": f"{type(err).__name__}: {err}"[:500]}


def report_failures(gh: GitHub, results: list[dict]) -> None:
    """Open (once) a maintainer issue when the bot keeps failing, e.g. API credit exhausted."""
    errors = [r for r in results if r.get("result") == "error"]
    if not errors or gh.dry_run:
        return
    open_issues = [i for i in gh.get(gh.r("/issues"), state="open", labels="pr-bot:health", per_page=10) or []
                   if "pull_request" not in i]
    if open_issues:
        return
    body = "The PR review bot failed:\n\n" + "\n".join(f"- #{r['pr']}: `{r['error']}`" for r in errors)
    body += f"\n\nRun: {os.environ.get('GITHUB_SERVER_URL', '')}/{gh.repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    body += f"\n\nIt retries daily; close this issue once fixed ({C.ESCALATION_MARKER})." + C.FOOTER
    issue = gh.write("POST", gh.r("/issues"), {"title": "PR review bot needs attention", "body": body,
                                               "labels": ["pr-bot:health"], "assignees": [OWNER]})
    log(f"opened failure issue: {(issue or {}).get('html_url')}")


def summarize(results: list[dict]) -> None:
    lines = ["| PR | result | gate / intent | reason |", "|---|---|---|---|"]
    for r in results:
        why = (r.get("reason") or r.get("note") or r.get("error") or "").replace("|", "/").replace("\n", " ")[:200]
        lines.append(f"| #{r.get('pr')} | {r.get('result')} | {r.get('gate') or r.get('intent') or ''} | {why} |")
    text = "\n".join(lines)
    log(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("## PR review bot\n\n" + text + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    p_run = sub.add_parser("run")
    p_run.add_argument("--prs", default="[]")
    p_run.add_argument("--lint-dir", default=os.environ.get("PR_BOT_LINT_DIR"))
    p_rev = sub.add_parser("review")
    p_rev.add_argument("numbers", nargs="+", type=int)
    p_rev.add_argument("--lint-dir")
    p_rev.add_argument("--no-llm", action="store_true")
    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("numbers", nargs="+", type=int)
    p_bt.add_argument("--out", default="backtest.jsonl")
    p_bt.add_argument("--no-llm", action="store_true")
    args = ap.parse_args(argv)

    dry = os.environ.get("PR_BOT_DRY_RUN", "").lower() in ("1", "true", "yes")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event = {}
    if os.environ.get("GITHUB_EVENT_PATH") and Path(os.environ["GITHUB_EVENT_PATH"]).exists():
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())

    if args.cmd == "plan":
        prs = plan(event_name, event, os.environ.get("PR_BOT_DISPATCH_PRS", ""))
        batch = event_name in ("workflow_dispatch", "schedule")
        out = {"prs": json.dumps(prs), "run": "true" if (prs or batch) else "false"}
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                for k, v in out.items():
                    f.write(f"{k}={v}\n")
        log(json.dumps(out))
        return 0

    if args.cmd == "run":
        gh = GitHub(dry_run=dry)
        results = run(gh, event_name, event, json.loads(args.prs), args.lint_dir)
        summarize(results)
        if dry:
            log(json.dumps(gh.writes, indent=1)[:20000])
        report_failures(gh, results)
        return 0

    if args.cmd == "review":
        gh = GitHub(dry_run=True)
        for n in args.numbers:
            pr = gh.get(gh.r(f"/pulls/{n}"))
            action, ev, review, usage = review_pr(gh, pr, args.lint_dir, use_llm=not args.no_llm,
                                                  all_prs=list_prs(gh))
            log(json.dumps({"pr": n, "action": action.__dict__, "review": review, "usage": usage,
                            "repos": ev["repos"], "hard": ev["hard_findings"]}, indent=1, default=str))
        return 0

    if args.cmd == "backtest":
        gh = GitHub(dry_run=True)
        with open(args.out, "a") as f:
            for n in args.numbers:
                pr = gh.get(gh.r(f"/pulls/{n}"))
                actual = "merged" if pr.get("merged_at") else pr["state"]
                try:
                    action, ev, review, usage = review_pr(gh, pr, None, as_of=parse_ts(pr["created_at"]),
                                                          all_prs=list_prs(gh), use_llm=not args.no_llm)
                    rec = {"pr": n, "actual": actual, "bot": action.kind, "gate": action.gate,
                           "reason": action.public_reason, "note": action.note, "review": review, "usage": usage,
                           "repos": [{k: r[k] for k in ("repo", "stars_as_of", "stars_now", "forks", "outside_issue_authors", "age_days_as_of",
                                                        "fake_star_flags", "submitter_affiliated")} for r in ev["repos"]]}
                except Exception as e:
                    traceback.print_exc()
                    rec = {"pr": n, "actual": actual, "bot": "error", "error": str(e)[:300]}
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                log(f"#{n}: actual={actual} bot={rec['bot']} ({rec.get('gate')})")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
