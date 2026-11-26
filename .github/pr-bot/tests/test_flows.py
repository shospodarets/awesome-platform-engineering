"""Flow tests: the bot's actions against a fake GitHub, with evidence gathering and Claude mocked."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot.__main__ as M  # noqa: E402
from bot import comments as C  # noqa: E402
from bot.gh import GitHub, GitHubError  # noqa: E402

# GitHub's answer when the Actions token may not merge a fork branch that lacks main's workflow changes (#90)
DENIED = GitHubError(403, '{"message":"Resource not accessible by integration","status":"403"}',
                     "https://api.github.com/repos/o/r/pulls/7/merge")
GAP = [".github/workflows/pr-review-bot.yml", "README.md"]


class FakeGitHub(GitHub):
    def __init__(self, pr, comments=None, merge_errors=None, compare_files=None):
        super().__init__(token="x", repo="o/r", dry_run=False)
        self.pr = pr
        self.comments = comments or []
        self.merge_errors = list(merge_errors or [])  # raised by successive merge calls
        self.compare_files = compare_files or []      # files main changed since the PR branched off
        self.gets = []
        self.open_prs = []                            # what the sweep lists
        self.blob_error = None                        # set when the token lost contents: write
        self.reviews, self.review_comments, self.reviews_error = [], [], None

    def get(self, path, **params):
        self.gets.append(path)
        if path == f"/repos/o/r/pulls/{self.pr['number']}":
            return self.pr
        if path.startswith("/repos/o/r/issues") and path.endswith("/issues"):
            return []
        if path.startswith("/repos/o/r/compare/"):
            return {"files": [{"filename": f} for f in self.compare_files]}
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path, max_pages=10, accept="", **params):
        if path.endswith(f"/pulls/{self.pr['number']}/reviews"):
            if self.reviews_error:
                raise self.reviews_error
            return list(self.reviews)
        if path.endswith(f"/pulls/{self.pr['number']}/comments"):
            return list(self.review_comments)
        if path.endswith("/comments"):
            return list(self.comments)
        if path.endswith("/pulls"):
            return list(self.open_prs)
        if path.endswith("/labels"):
            return []
        raise AssertionError(f"unexpected paginate {path}")

    def write(self, method, path, body=None):
        self.writes.append({"method": method, "path": path, "body": body})
        if method == "POST" and path.endswith(f"/issues/{self.pr['number']}/comments"):
            self.comments.append({"id": 1000 + len(self.comments), "user": {"login": "github-actions[bot]"},
                                  "body": body["body"]})
        if method == "PATCH" and path.endswith(f"/pulls/{self.pr['number']}"):
            self.pr["state"] = body["state"]
        if method == "POST" and path.endswith("/git/blobs") and self.blob_error:
            raise self.blob_error
        if method == "PUT" and path.endswith("/merge"):
            if self.merge_errors:
                raise self.merge_errors.pop(0)
            self.pr["merged_at"] = "2026-10-09T00:00:00Z"
        return {}


def make_pr(**over):
    pr = {"number": 7, "title": "Add X", "state": "open", "merged_at": None, "labels": [], "user": {"login": "dev"},
          "head": {"sha": "abc", "label": "dev:add-x"}, "base": {"ref": "main", "sha": "b0"}, "mergeable": True,
          "updated_at": "2026-10-09T00:00:00Z"}
    pr.update(over)
    return pr


@pytest.fixture
def review(monkeypatch):
    """Make review_pr return a chosen action without network or model calls."""
    def set_action(action):
        monkeypatch.setattr(M, "review_pr", lambda *a, **k: (action, {}, {"decision": action.kind, "confidence": 0.9}, {}))
        monkeypatch.setattr(M, "list_prs", lambda gh: [])
        monkeypatch.setattr(M, "dispatch_pages", lambda gh: gh.writes.append({"method": "DISPATCH", "path": ""}))
    return set_action


def kinds(gh):
    return [(w["method"], w["path"].split("/repos/o/r")[-1]) for w in gh.writes]


def test_merge_flow_comments_after_merge_and_redeploys(review):
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr())
    res = M.handle_review(gh, gh.pr, None, "opened")
    assert res["result"] == "merged"
    k = kinds(gh)
    assert k[0] == ("PUT", "/pulls/7/merge")
    assert ("POST", "/issues/7/comments") in k and ("DISPATCH", "") in k
    assert C.read_state(gh.comments[-1]["body"])["decision"] == "merge"


def test_decline_flow_comments_before_closing(review):
    review(M.Action("decline", "Only 3 stars.", gate="decline"))
    gh = FakeGitHub(make_pr())
    M.handle_review(gh, gh.pr, None, "opened")
    k = kinds(gh)
    assert k.index(("POST", "/issues/7/comments")) < k.index(("PATCH", "/pulls/7"))
    assert gh.pr["state"] == "closed"
    assert "Only 3 stars." in gh.comments[-1]["body"] and gh.comments[-1]["body"].count("_* sent by AI_") == 1


def test_escalation_assigns_owner_and_marks_mail(review):
    review(M.Action("escalate", "mixed signals", gate="model escalated"))
    gh = FakeGitHub(make_pr())
    M.handle_review(gh, gh.pr, None, "opened")
    assert ("POST", "/issues/7/assignees") in kinds(gh)
    body = gh.comments[-1]["body"]
    assert "@shospodarets" in body and C.ESCALATION_MARKER in body


def test_same_head_is_not_reviewed_twice(review):
    review(M.Action("decline", "x", gate="decline"))
    gh = FakeGitHub(make_pr())
    M.handle_review(gh, gh.pr, None, "opened")
    n = len(gh.writes)
    gh.pr["state"] = "open"
    res = M.handle_review(gh, gh.pr, None, "edited")
    assert res["result"].startswith("already reviewed") and len(gh.writes) == n


def test_conflicting_approved_pr_waits_for_rebase(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr(mergeable=False))
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    res = M.handle_review(gh, gh.pr, None, "opened")
    assert res["result"] == "approved-needs-rebase"
    assert not any(w["path"].endswith("/merge") for w in gh.writes)


def test_new_evidence_on_closed_pr_reopens_and_merges(review, monkeypatch):
    st = {"v": 1, "decision": "decline", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "declined" + C.marker(st)},
                {"id": 2, "user": {"login": "dev"}, "body": "We now have 120 stars: https://github.com/x/x"}]
    gh = FakeGitHub(make_pr(state="closed"), comments)
    monkeypatch.setattr("bot.llm.triage_comment", lambda *a, **k: (
        {"intent": "new_evidence", "needs_maintainer": False, "rereview": True, "reply": "", "maintainer_note": ""}, {}))
    review(M.Action("merge", "ok", gate="merge"))
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    res = M.handle_comments(gh, gh.pr, None)
    assert res["result"] == "merged"
    k = kinds(gh)
    assert k.index(("PATCH", "/pulls/7")) < k.index(("PUT", "/pulls/7/merge"))


def test_complaint_escalates_and_is_not_triaged_again(monkeypatch):
    st = {"v": 1, "decision": "decline", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "declined" + C.marker(st)},
                {"id": 2, "user": {"login": "dev"}, "body": "This is unfair, I want a human."}]
    gh = FakeGitHub(make_pr(state="closed"), comments)
    calls = []
    monkeypatch.setattr("bot.llm.triage_comment", lambda *a, **k: calls.append(1) or (
        {"intent": "complaint", "needs_maintainer": True, "rereview": False, "reply": "", "maintainer_note": "upset"}, {}))
    assert M.handle_comments(gh, gh.pr, None)["result"] == "escalated"
    gh.pr["labels"] = [{"name": "needs-maintainer"}]
    gh.comments.append({"id": 3, "user": {"login": "dev"}, "body": "hello?"})
    assert M.handle_comments(gh, gh.pr, None)["result"] == "waiting for maintainer"
    assert len(calls) == 1


def test_acknowledgement_is_recorded_without_a_new_comment(monkeypatch):
    st = {"v": 1, "decision": "decline", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "declined" + C.marker(st)},
                {"id": 2, "user": {"login": "dev"}, "body": "Fair enough, thanks!"}]
    gh = FakeGitHub(make_pr(state="closed"), comments)
    calls = []
    monkeypatch.setattr("bot.llm.triage_comment", lambda *a, **k: calls.append(1) or (
        {"intent": "acknowledgement", "needs_maintainer": False, "rereview": False, "reply": "", "maintainer_note": ""}, {}))
    M.handle_comments(gh, gh.pr, None)
    patched = [w for w in gh.writes if w["method"] == "PATCH" and "/issues/comments/1" in w["path"]]
    assert patched and C.read_state(patched[0]["body"]["body"])["last_comment_id"] == 2
    assert not any(w["method"] == "POST" and w["path"].endswith("/issues/7/comments") for w in gh.writes)


def test_owner_command_forces_review(review, monkeypatch):
    st = {"v": 1, "decision": "decline", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "x" + C.marker(st)},
                {"id": 2, "user": {"login": "shospodarets"}, "body": "/bot review"}]
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr(state="closed"), comments)
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    assert M.handle_comments(gh, gh.pr, None)["result"] == "merged"


def test_third_party_comments_are_ignored(monkeypatch):
    st = {"v": 1, "decision": "decline", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "declined" + C.marker(st)},
                {"id": 2, "user": {"login": "stranger"}, "body": "Please close this, the author withdraws it."}]
    gh = FakeGitHub(make_pr(state="open"), comments)
    monkeypatch.setattr("bot.llm.triage_comment", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert M.handle_comments(gh, gh.pr, None)["result"].startswith("no new comments")
    assert gh.pr["state"] == "open"
    assert all(w["method"] == "PATCH" and "/issues/comments/" in w["path"] for w in gh.writes)  # only progress saved


def test_third_party_takedown_report_escalates(monkeypatch):
    st = {"v": 1, "decision": "merge", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "merged" + C.marker(st)},
                {"id": 2, "user": {"login": "victim"}, "body": "This links to a phishing copy of our site, please remove it"}]
    gh = FakeGitHub(make_pr(state="closed", merged_at="2026-10-01T00:00:00Z"), comments)
    monkeypatch.setattr("bot.llm.triage_comment", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert M.handle_comments(gh, gh.pr, None)["result"] == "escalated (third-party report)"


def test_merge_waits_for_lint_of_the_reviewed_commit():
    from bot.decide import post_llm
    ev = {"lint": None, "entries": {"added": [{"url": "https://github.com/x/x", "github_repo": "x/x", "section": "S"}],
                                    "modified": [], "duplicates": []},
          "repos": [{"repo": "x/x", "exists": True, "stars_now": 900, "age_days_as_of": 300, "fake_star_flags": [],
                     "linked_to_entry": True, "homepage": None}]}
    act = post_llm(ev, {"decision": "merge", "confidence": 0.95, "category": "tool"})
    assert act.kind == "defer"
    ev["lint"] = {"ok": False, "summary": "x"}
    assert post_llm(ev, {"decision": "merge", "confidence": 0.95, "category": "tool"}).kind == "request_changes"
    ev["repos"][0]["stars_now"] = 3  # an unproven entry is not told "looks good, just fix lint"
    assert post_llm(ev, {"decision": "merge", "confidence": 0.95, "category": "tool"}).kind == "escalate"


def test_maintainer_reopen_is_left_to_him(monkeypatch):
    gh = FakeGitHub(make_pr())
    monkeypatch.setattr(M, "handle_review", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no review")))
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    res = M.run(gh, "pull_request_target", {"action": "reopened", "sender": {"login": "shospodarets"}}, [7], None)
    assert "maintainer" in res[0]["result"]
    assert any(w["path"].endswith("/issues/7/labels") for w in gh.writes)


def test_complaint_on_merged_pr_escalates_without_model(monkeypatch):
    st = {"v": 1, "decision": "merge", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "merged" + C.marker(st)},
                {"id": 2, "user": {"login": "dev"}, "body": "Please remove our entry, the link is outdated"}]
    gh = FakeGitHub(make_pr(state="closed", merged_at="2026-10-01T00:00:00Z"), comments)
    monkeypatch.setattr("bot.llm.triage_comment", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert M.handle_comments(gh, gh.pr, None)["result"].startswith("escalated")


def test_maintainer_comment_on_untouched_closed_pr_does_nothing(monkeypatch):
    comments = [{"id": 5, "user": {"login": "shospodarets"}, "body": "Closing as discussed"}]
    gh = FakeGitHub(make_pr(state="closed"), comments)
    monkeypatch.setattr(M, "handle_review", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no review")))
    assert M.handle_comments(gh, gh.pr, None)["result"] == "no bot history"
    assert not gh.writes


# -- GitHub refuses the Actions token's merge (#90): fork branch behind main's workflow changes ------------------

PENDING = {"v": 1, "decision": "approved-needs-update", "sha": "abc", "refused_sha": "abc", "last_comment_id": 1,
           "rereviews": 0, "triage_calls": 0, "read_up_to": 0, "read_at": "2026-11-25T16:44:00Z"}


def pending_merge(*extra_comments, **over):
    """A PR whose approved merge GitHub refused earlier, as the bot left it."""
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "approved" + C.marker(PENDING),
                 "created_at": "2026-11-25T16:45:30Z"}, *extra_comments]
    return FakeGitHub(make_pr(labels=[{"name": "pr-bot:approved-needs-rebase"}]), comments, **over)


def evidence_for_retry(duplicates=(), hard_findings=(), repos=()):
    """The deterministic evidence retry_merge re-checks: enough for the real pre_llm and gates."""
    added = {"name": "Kumoss", "url": "https://www.kumoss.io", "github_repo": None, "section": "Tooling",
             "description": "", "raw": "- [Kumoss](https://www.kumoss.io) - IaC assistant."}
    return {"pr": {"draft": False, "author_association": "CONTRIBUTOR"}, "author": {"login": "dev"},
            "hard_findings": list(hard_findings), "readme_only": True, "files": ["README.md"], "lint": None,
            "repos": list(repos),
            "entries": {"added": [added], "removed": [], "modified": [], "other_added_lines": [],
                        "duplicates": [{"added": added, "existing": {**added, "url": e}} for e in duplicates]}}


@pytest.fixture
def no_review(monkeypatch):
    """A retried merge must not pay for a model review; the real deterministic gates still run."""
    import types
    evidence = {"duplicates": (), "hard_findings": (), "repos": ()}
    monkeypatch.setattr(M, "review_pr", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model review")))
    monkeypatch.setattr(M, "load_lint", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no re-lint")))
    monkeypatch.setattr(M, "gather", lambda gh, pr, **k: types.SimpleNamespace(
        to_dict=lambda: evidence_for_retry(**evidence)))
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    monkeypatch.setattr(M, "dispatch_pages", lambda gh: gh.writes.append({"method": "DISPATCH", "path": ""}))
    return evidence


def test_refused_fork_merge_asks_for_a_branch_update_not_a_health_issue(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr(labels=[{"name": "pr-bot:error"}]), merge_errors=[DENIED], compare_files=GAP)
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    res = M.run(gh, "pull_request_target", {"action": "opened"}, [7], None)
    assert res[0]["result"] == "approved-needs-update" and not gh.pr["merged_at"]
    assert "/repos/o/r/compare/abc...main" in gh.gets
    body = gh.comments[-1]["body"]
    assert "Update branch" in body and "@dev" in body and body.count("_* sent by AI_") == 1
    assert C.read_state(body)["decision"] == "approved-needs-update" and C.read_state(body)["sha"] == "abc"
    k = kinds(gh)
    assert ("DELETE", "/issues/7/labels/pr-bot:error") in k and ("POST", "/issues/7/labels") in k
    assert ("DISPATCH", "") not in k
    M.report_failures(gh, res)
    assert ("POST", "/issues") not in kinds(gh)


def test_unexplained_merge_refusal_is_still_reported(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr(), merge_errors=[DENIED], compare_files=["README.md"])
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    res = M.run(gh, "pull_request_target", {"action": "opened"}, [7], None)
    assert res[0]["result"] == "error" and "not accessible by integration" in res[0]["error"]
    M.report_failures(gh, res)
    assert ("POST", "/issues") in kinds(gh)


def test_other_merge_errors_are_not_mistaken_for_the_refusal(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    moved = GitHubError(409, '{"message":"Head branch was modified. Review and try the merge again."}', "u")
    gh = FakeGitHub(make_pr(), merge_errors=[moved], compare_files=GAP)
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    with pytest.raises(GitHubError):
        M.handle_review(gh, gh.pr, None, "opened")
    assert not any("/compare/" in g for g in gh.gets)


def test_maintainer_run_retries_the_refused_merge_without_a_new_review(no_review):
    gh = pending_merge()
    res = M.handle_review(gh, gh.pr, None, "schedule")
    assert res["result"] == "merged" and gh.pr["merged_at"]
    assert C.read_state(gh.comments[-1]["body"])["decision"] == "merge"
    assert ("DISPATCH", "") in kinds(gh)


def test_refused_retry_stays_quiet_and_contributor_events_just_wait(no_review):
    gh = pending_merge(merge_errors=[DENIED], compare_files=GAP)
    res = M.handle_review(gh, gh.pr, None, "workflow_dispatch")
    assert res["result"].startswith("waiting for branch update") and not gh.pr["merged_at"]
    assert not any(w["method"] == "POST" and w["path"].endswith("/comments") for w in gh.writes)  # asked once
    n = len(gh.writes)
    assert M.handle_review(gh, gh.pr, None, "pull_request_target")["result"] == "waiting for branch update"
    assert len(gh.writes) == n


def test_retry_declines_an_entry_that_landed_on_main_meanwhile(no_review):
    no_review["duplicates"] = ("https://www.kumoss.io/",)  # exact duplicate: pre_llm declines it
    gh = pending_merge()
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "declined"
    assert not any(w["path"].endswith("/merge") for w in gh.writes) and gh.pr["state"] == "closed"


def test_retry_escalates_a_possible_duplicate(no_review):
    no_review["duplicates"] = ("https://kumoss.io/",)  # same project, other URL: pre_llm lets it through
    gh = pending_merge()
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "escalated"
    assert not any(w["path"].endswith("/merge") for w in gh.writes)
    assert "needs-maintainer" in str([w["body"] for w in gh.writes if w["path"].endswith("/labels")])


@pytest.mark.parametrize("change", [
    {"hard_findings": ["broken link: https://www.kumoss.io (404)"]},
    {"repos": [{"repo": "x/kumoss", "exists": True, "archived": True, "linked_to_entry": True}]},
    {"repos": [{"repo": "x/kumoss", "exists": False, "archived": False, "linked_to_entry": True}]},
])
def test_retry_escalates_an_entry_that_broke_since_the_approval(no_review, change):
    no_review.update(change)
    gh = pending_merge()
    res = M.handle_review(gh, gh.pr, None, "schedule")
    assert res["result"] == "escalated" and res["gate"] == "changed since approval"
    assert not any(w["path"].endswith("/merge") for w in gh.writes)


def test_retry_ignores_a_repo_merely_named_in_the_pr_body(no_review):
    no_review["repos"] = [{"repo": "big/other", "exists": False, "archived": False, "linked_to_entry": False}]
    gh = pending_merge()
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "merged"


def test_unhandled_comments_are_read_before_a_retry_merges(no_review, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    gh = pending_merge({"id": 2, "user": {"login": "victim"},
                        "body": "Do not merge: this repository was compromised and ships malware"})
    gh.pr["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.open_prs = [gh.pr]
    res = M.run(gh, "schedule", {}, [], None)
    assert res[0]["result"] == "escalated (third-party report)"
    assert not any(w["path"].endswith("/merge") for w in gh.writes)


def test_a_maintainer_note_holds_the_retry_until_a_fresh_review(no_review):
    gh = pending_merge({"id": 2, "user": {"login": "shospodarets"}, "body": "Please hold, I want to check the licence"})
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "waiting for branch update (a comment since the approval)"
    assert not gh.writes
    # his notes from before the refusal do not hold it
    gh = pending_merge()
    gh.comments.insert(0, {"id": 0, "user": {"login": "shospodarets"}, "body": "Older note"})
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "merged"
    # nor do notes a fresh review has read: his `/bot review` after them was handled (progress saved past it)
    handled = {**PENDING, "last_comment_id": 3}
    gh = FakeGitHub(make_pr(labels=[{"name": "pr-bot:approved-needs-rebase"}]), [
        {"id": 1, "user": {"login": "github-actions[bot]"}, "body": "approved" + C.marker(handled)},
        {"id": 2, "user": {"login": "shospodarets"}, "body": "Please hold, I want to check the licence"},
        {"id": 3, "user": {"login": "shospodarets"}, "body": "Licence is fine. /bot review",
         "created_at": "2026-11-26T09:00:00Z"}])
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "merged"


def triage_replies(monkeypatch, text="Use the Update branch button."):
    monkeypatch.setattr("bot.llm.triage_comment", lambda *a, **k: (
        {"intent": "question", "needs_maintainer": False, "rereview": False, "reply": text, "maintainer_note": ""}, {}))


def test_a_reply_to_a_pending_merge_keeps_it_pending(no_review, monkeypatch):
    triage_replies(monkeypatch)
    gh = pending_merge({"id": 2, "user": {"login": "dev"}, "body": "How do I update the branch?"})
    assert M.handle_comments(gh, gh.pr, None)["result"] == "replied"
    st = C.read_state(gh.comments[-1]["body"])
    assert st["decision"] == "reply" and st["refused_sha"] == "abc" and M.merge_pending(st, gh.pr)


def test_a_conflict_ends_the_pending_merge_and_a_later_reply_does_not_revive_it(no_review, monkeypatch):
    gh = pending_merge()
    gh.pr["mergeable"] = False
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "approved-needs-rebase"
    conflict = C.read_state(gh.comments[-1]["body"])
    assert conflict["decision"] == "approved-needs-rebase" and "refused_sha" not in conflict
    triage_replies(monkeypatch, "Merge main into your branch to resolve it.")
    gh.comments.append({"id": 50, "user": {"login": "dev"}, "body": "What conflict?"})
    assert M.handle_comments(gh, gh.pr, None)["result"] == "replied"
    assert "refused_sha" not in C.read_state(gh.comments[-1]["body"])
    n = len(gh.writes)
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "already reviewed (reply)" and len(gh.writes) == n


def test_an_escalated_retry_is_not_revived_by_a_reply(no_review, monkeypatch):
    no_review["hard_findings"] = ["broken link: https://www.kumoss.io (404)"]
    gh = pending_merge()
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "escalated"
    assert "refused_sha" not in C.read_state(gh.comments[-1]["body"])


@pytest.mark.parametrize("source", ["identified", "pr_body"])
def test_a_repo_the_approval_rested_on_is_stored_and_rechecked(monkeypatch, no_review, source):
    import types
    ev = {"repos": [{"repo": "kumoss/kumoss", "source": source, "linked_to_entry": True},
                    {"repo": "kumoss/entry", "source": "entry", "linked_to_entry": True}]}
    monkeypatch.setattr(M, "review_pr", lambda *a, **k: (M.Action("merge", "ok", gate="merge"), ev, {}, {}))
    monkeypatch.setattr(M, "list_prs", lambda gh: [])
    gh = FakeGitHub(make_pr(), merge_errors=[DENIED], compare_files=GAP)
    assert M.handle_review(gh, gh.pr, None, "opened")["result"] == "approved-needs-update"
    st = C.read_state(gh.comments[-1]["body"])
    assert st["linked_repos"] == ["kumoss/kumoss"] and st["text"] == M.digest(gh.pr)
    gone = types.SimpleNamespace(linked_to_entry=False, summary=lambda: {
        "repo": "kumoss/kumoss", "exists": True, "archived": True, "linked_to_entry": True})
    monkeypatch.setattr(M, "repo_evidence", lambda *a, **k: gone)
    gh2 = FakeGitHub(make_pr(labels=[{"name": "pr-bot:approved-needs-rebase"}]),
                     [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "x" + C.marker(st),
                       "created_at": "2026-11-25T16:45:30Z"}])
    res = M.handle_review(gh2, gh2.pr, None, "schedule")
    assert res["result"] == "escalated" and res["gate"] == "changed since approval"


def test_run_workflow_with_pr_numbers_does_not_triage_replies(review, monkeypatch):
    st = {"v": 1, "decision": "decline", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "declined" + C.marker(st)},
                {"id": 2, "user": {"login": "dev"}, "body": "Here is more evidence"}]
    gh = FakeGitHub(make_pr(state="closed"), comments)
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    monkeypatch.setattr(M, "handle_comments", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no triage")))
    assert M.run(gh, "workflow_dispatch", {}, [7], None)[0]["result"] == "already reviewed (decline)"


def test_a_token_without_contents_write_is_reported_not_blamed_on_the_branch(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr(), merge_errors=[DENIED], compare_files=GAP)
    gh.blob_error = GitHubError(403, '{"message":"Resource not accessible by integration"}', "u")
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    res = M.run(gh, "pull_request_target", {"action": "opened"}, [7], None)
    assert res[0]["result"] == "error" and not gh.comments
    M.report_failures(gh, res)
    assert ("POST", "/issues") in kinds(gh)


def test_saved_progress_keeps_a_reply_counted_as_a_reply(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    reply = {**PENDING, "decision": "reply", "last_comment_id": 2}
    gh = pending_merge({"id": 2, "user": {"login": "dev"}, "body": "How do I update?"},
                       {"id": 3, "user": {"login": "github-actions[bot]"}, "body": "Like this." + C.marker(reply)},
                       {"id": 4, "user": {"login": "shospodarets"}, "body": "/bot review"},
                       merge_errors=[DENIED], compare_files=GAP)
    assert M.handle_comments(gh, gh.pr, None)["result"] == "approved-needs-update"
    saved = [w for w in gh.writes if w["method"] == "PATCH" and w["path"].endswith("/issues/comments/3")]
    st = C.read_state(saved[-1]["body"]["body"])
    assert st["decision"] == "reply" and st["last_comment_id"] == 4 and st["refused_sha"] == "abc"
    assert not any(w["method"] == "POST" and w["path"].endswith("/issues/7/comments") for w in gh.writes)


def test_a_bot_reply_keeps_the_refused_merge_pending(no_review):
    reply = {**PENDING, "decision": "reply", "last_comment_id": 2}
    gh = pending_merge({"id": 2, "user": {"login": "dev"}, "body": "Where is the Update branch button?"},
                       {"id": 3, "user": {"login": "github-actions[bot]"}, "body": "It is ..." + C.marker(reply)})
    gh.pr["updated_at"] = "2026-01-01T00:00:00Z"
    assert M.handle_stale(gh, gh.pr)["result"].startswith("escalated") and gh.pr["state"] == "open"
    gh = pending_merge({"id": 2, "user": {"login": "dev"}, "body": "Where is the Update branch button?"},
                       {"id": 3, "user": {"login": "github-actions[bot]"}, "body": "It is ..." + C.marker(reply)})
    res = M.handle_review(gh, gh.pr, None, "schedule")  # someone spoke since the refusal: wait for the push
    assert res["result"] == "waiting for branch update (a comment since the approval)" and not gh.writes


def test_forced_review_refused_again_saves_its_progress_once(review, monkeypatch):
    calls = []
    review(M.Action("merge", "ok", gate="merge"))
    reviewed = M.review_pr
    monkeypatch.setattr(M, "review_pr", lambda *a, **k: calls.append(1) or reviewed(*a, **k))
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    gh = pending_merge({"id": 2, "user": {"login": "shospodarets"}, "body": "/bot review"},
                       merge_errors=[DENIED], compare_files=GAP)
    assert M.handle_comments(gh, gh.pr, None)["result"] == "approved-needs-update"
    assert not any(w["method"] == "POST" and w["path"].endswith("/issues/7/comments") for w in gh.writes)
    saved = [w for w in gh.writes if w["method"] == "PATCH" and w["path"].endswith("/issues/comments/1")]
    assert saved and C.read_state(saved[-1]["body"]["body"])["last_comment_id"] == 2
    gh.comments[0]["body"] = saved[-1]["body"]["body"]  # GitHub now holds the saved marker
    assert M.handle_comments(gh, gh.pr, None)["result"].startswith("no new comments") and len(calls) == 1


def test_forced_review_of_a_conflicting_pr_saves_its_progress(review, monkeypatch):
    st = {"v": 1, "decision": "approved-needs-rebase", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "conflict" + C.marker(st)},
                {"id": 2, "user": {"login": "shospodarets"}, "body": "/bot review"}]
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr(mergeable=False), comments)
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    assert M.handle_comments(gh, gh.pr, None)["result"] == "approved-needs-rebase"
    saved = [w for w in gh.writes if w["method"] == "PATCH" and w["path"].endswith("/issues/comments/1")]
    assert saved and C.read_state(saved[-1]["body"]["body"])["last_comment_id"] == 2
    assert not any(w["method"] == "POST" and w["path"].endswith("/issues/7/comments") for w in gh.writes)


def test_quiet_retries_do_not_touch_the_pr(no_review):
    gh = pending_merge(merge_errors=[DENIED, DENIED], compare_files=GAP)
    M.handle_review(gh, gh.pr, None, "schedule")
    M.handle_review(gh, gh.pr, None, "schedule")
    # only the merge attempts and the unreferenced permission-check blob: no comment, label or marker edit
    assert kinds(gh) == [("PUT", "/pulls/7/merge"), ("POST", "/git/blobs")] * 2


def test_a_closed_pending_pr_is_never_reopened_by_a_retry(no_review):
    gh = pending_merge()
    gh.pr["state"] = "closed"
    assert M.handle_review(gh, gh.pr, None, "workflow_dispatch")["result"] == "closed" and not gh.writes


def test_the_sweep_reports_the_retry_outcome(no_review, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    gh = pending_merge(merge_errors=[DENIED], compare_files=GAP)
    gh.pr["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.open_prs = [gh.pr]
    res = M.run(gh, "schedule", {}, [], None)
    assert res[0]["result"] == "waiting for branch update (merge refused again)"


def test_pushed_update_gets_a_normal_review_and_merge(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    gh = pending_merge()
    gh.pr["head"]["sha"] = "def"  # the author updated the branch
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: g.pr)
    assert M.handle_review(gh, gh.pr, None, "pull_request_target")["result"] == "merged"


def test_two_quiet_weeks_escalate_an_approved_entry_instead_of_closing_it():
    gh = pending_merge()
    gh.pr["updated_at"] = "2026-01-01T00:00:00Z"
    res = M.handle_stale(gh, gh.pr)
    assert res["result"].startswith("escalated") and gh.pr["state"] == "open"
    k = kinds(gh)
    assert ("POST", "/issues/7/assignees") in k and ("DELETE", "/issues/7/labels/pr-bot:approved-needs-rebase") in k
    body = gh.comments[-1]["body"]
    assert C.ESCALATION_MARKER in body and "approved" in body and C.read_state(body)["decision"] == "escalate"


def test_stale_conflicting_pr_is_still_closed():
    st = {"v": 1, "decision": "approved-needs-rebase", "sha": "abc", "last_comment_id": 1}
    comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": "conflict" + C.marker(st)}]
    gh = FakeGitHub(make_pr(labels=[{"name": "pr-bot:approved-needs-rebase"}], updated_at="2026-01-01T00:00:00Z"),
                    comments)
    assert M.handle_stale(gh, gh.pr)["result"] == "closed (stale)" and gh.pr["state"] == "closed"


def test_workflow_gap_lists_only_workflow_files_and_fails_open():
    gh = FakeGitHub(make_pr(), compare_files=GAP)
    assert M.workflow_gap(gh, gh.pr) == [".github/workflows/pr-review-bot.yml"]
    gh.compare_files = ["README.md"]
    assert M.workflow_gap(gh, gh.pr) == []

    class Broken(FakeGitHub):
        def get(self, path, **params):
            raise GitHubError(404, '{"message":"Not Found"}', path)
    assert M.workflow_gap(Broken(make_pr()), make_pr()) == []
    assert M.integration_denied(DENIED)
    assert not M.integration_denied(GitHubError(403, '{"message":"API rate limit exceeded"}', "u"))


def test_a_bot_reply_does_not_release_a_hold(no_review, monkeypatch):
    triage_replies(monkeypatch)
    gh = pending_merge({"id": 2, "user": {"login": "shospodarets"}, "body": "Please hold, I want to check the licence"},
                       {"id": 3, "user": {"login": "dev"}, "body": "How do I update the branch?"})
    assert M.handle_comments(gh, gh.pr, None)["result"] == "replied"
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"].startswith("waiting for branch update")
    assert not gh.pr["merged_at"]


def test_the_authors_request_to_wait_holds_the_retry(no_review, monkeypatch):
    triage_replies(monkeypatch, "Sure, push the rename when it is ready.")
    gh = pending_merge({"id": 2, "user": {"login": "dev"}, "body": "Please don't merge yet, a rename is coming"})
    assert M.handle_comments(gh, gh.pr, None)["result"] == "replied"
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"].startswith("waiting for branch update")
    assert not gh.pr["merged_at"]


@pytest.mark.parametrize("where, item, held", [
    ("reviews", {"user": {"login": "shospodarets"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-26T10:00:00Z"}, True),
    ("reviews", {"user": {"login": "stranger"}, "state": "COMMENTED", "submitted_at": "2026-11-26T10:00:00Z"}, True),
    ("review_comments", {"user": {"login": "shospodarets"}, "created_at": "2026-11-26T10:00:00Z"}, True),
    ("reviews", {"user": {"login": "shospodarets"}, "state": "COMMENTED", "submitted_at": "2026-11-20T10:00:00Z"}, False),
    ("reviews", {"user": {"login": "github-actions[bot]"}, "state": "COMMENTED", "submitted_at": "2026-11-26T10:00:00Z"}, False),
])
def test_a_review_since_the_refusal_holds_the_retry(no_review, where, item, held):
    gh = pending_merge()
    getattr(gh, where).append(item)
    res = M.handle_review(gh, gh.pr, None, "schedule")
    assert (res["result"] == "waiting for branch update (a review since the approval)") is held
    assert bool(gh.pr["merged_at"]) is not held


def test_unreadable_reviews_hold_the_retry(no_review):
    gh = pending_merge()
    gh.reviews_error = GitHubError(502, "Bad Gateway", "u")
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"].endswith("(the PR's reviews could not be read)")


def test_an_edited_title_or_description_holds_the_retry(no_review):
    gh = pending_merge()
    st = {**PENDING, "text": M.digest(gh.pr)}
    gh.comments[0]["body"] = "approved" + C.marker(st)
    gh.pr["title"] = "[DO NOT MERGE] Add X"
    res = M.handle_review(gh, gh.pr, None, "schedule")
    assert res["result"].endswith("(the title or description changed since the approval)") and not gh.writes


def test_comments_posted_while_the_approving_review_ran_still_hold_the_retry(monkeypatch, no_review):
    gh = FakeGitHub(make_pr(), merge_errors=[DENIED], compare_files=GAP)

    def reviewing(*a, **k):  # the maintainer and a stranger post while the model call runs
        gh.comments += [{"id": 2, "user": {"login": "shospodarets"}, "body": "Hold: I want to check the licence"},
                        {"id": 3, "user": {"login": "fan"}, "body": "Great tool!"}]
        return M.Action("merge", "ok", gate="merge"), {}, {}, {}
    monkeypatch.setattr(M, "review_pr", reviewing)
    monkeypatch.setattr(M, "list_prs", lambda gh: [])
    assert M.handle_review(gh, gh.pr, None, "opened")["result"] == "approved-needs-update"
    assert M.handle_comments(gh, gh.pr, None)["result"].startswith("no new comments")  # marks the fan's comment read
    res = M.handle_review(gh, gh.pr, None, "schedule")
    assert res["result"] == "waiting for branch update (a comment since the approval)" and not gh.pr["merged_at"]


def test_a_maintainer_note_the_approval_read_does_not_hold_the_retry(monkeypatch, no_review):
    st = {"v": 1, "decision": "request_changes", "sha": "old", "last_comment_id": 9}
    comments = [{"id": 9, "user": {"login": "github-actions[bot]"}, "body": "fix lint" + C.marker(st)},
                {"id": 11, "user": {"login": "shospodarets"}, "body": "Agreed, and please shorten the description"}]
    gh = FakeGitHub(make_pr(), comments, merge_errors=[DENIED], compare_files=GAP)
    monkeypatch.setattr(M, "review_pr", lambda *a, **k: (M.Action("merge", "ok", gate="merge"), {}, {}, {}))
    monkeypatch.setattr(M, "list_prs", lambda gh: [])
    assert M.handle_review(gh, gh.pr, None, "synchronize")["result"] == "approved-needs-update"  # the push's review
    refusal = C.read_state(gh.comments[-1]["body"])
    assert refusal["last_comment_id"] == 9 and refusal["read_up_to"] == 11  # the note was read, not triaged
    monkeypatch.setattr(M, "review_pr", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model review")))
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "merged"


def test_a_bot_reply_after_the_read_point_holds_the_retry(no_review):
    # the push's review read the author's wait request (11) before its triage; the refusal (12), then the reply (13)
    st = {**PENDING, "last_comment_id": 9, "read_up_to": 11}
    reply = {**st, "decision": "reply", "last_comment_id": 12}
    gh = FakeGitHub(make_pr(labels=[{"name": "pr-bot:approved-needs-rebase"}]), [
        {"id": 11, "user": {"login": "dev"}, "body": "Please don't merge yet, a rename is coming"},
        {"id": 12, "user": {"login": "github-actions[bot]"}, "body": "approved" + C.marker(st)},
        {"id": 13, "user": {"login": "github-actions[bot]"}, "body": "Sure, push it when ready." + C.marker(reply)}])
    assert M.handle_review(gh, gh.pr, None, "schedule")["result"] == "waiting for branch update (a comment since the approval)"
    assert not gh.pr["merged_at"]


@pytest.mark.parametrize("edit, result", [
    ({"title": "[DO NOT MERGE] Add X"}, "waiting for branch update (the title or description changed since the approval)"),
    ({"head": {"sha": "def", "label": "dev:add-x"}}, "waiting (the PR changed during this run)"),
    ({"labels": [{"name": "needs-maintainer"}]}, "waiting (the PR changed during this run)"),
])
def test_the_retry_checks_the_pr_as_it_is_now_not_the_sweeps_list(no_review, monkeypatch, edit, result):
    import copy
    from datetime import datetime, timezone
    monkeypatch.setattr(M, "ensure_labels", lambda g: None)
    gh = pending_merge()
    gh.comments[0]["body"] = "approved" + C.marker({**PENDING, "text": M.digest(gh.pr)})
    gh.pr["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.open_prs = [copy.deepcopy(gh.pr)]  # listed at the start of the sweep
    gh.pr.update(edit)                    # then edited before the sweep reaches it
    res = M.run(gh, "schedule", {}, [], None)
    assert res[0]["result"] == result and not gh.pr["merged_at"]
    assert not any(w["path"].endswith("/merge") for w in gh.writes)


def test_a_push_between_review_and_merge_is_left_to_its_own_run(review, monkeypatch):
    review(M.Action("merge", "ok", gate="merge"))
    gh = FakeGitHub(make_pr())
    monkeypatch.setattr(M, "wait_mergeable", lambda g, n: {**g.pr, "head": {"sha": "def"}})
    res = M.handle_review(gh, gh.pr, None, "opened")
    assert res["result"] == "waiting (new commits since this run read the PR)"
    assert not gh.writes  # no 409, no error label, no health issue

