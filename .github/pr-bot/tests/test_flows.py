"""Flow tests: the bot's actions against a fake GitHub, with evidence gathering and Claude mocked."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot.__main__ as M  # noqa: E402
from bot import comments as C  # noqa: E402
from bot.gh import GitHub  # noqa: E402


class FakeGitHub(GitHub):
    def __init__(self, pr, comments=None):
        super().__init__(token="x", repo="o/r", dry_run=False)
        self.pr = pr
        self.comments = comments or []

    def get(self, path, **params):
        if path == f"/repos/o/r/pulls/{self.pr['number']}":
            return self.pr
        if path.startswith("/repos/o/r/issues") and path.endswith("/issues"):
            return []
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path, max_pages=10, accept="", **params):
        if path.endswith("/comments"):
            return list(self.comments)
        if path.endswith("/labels") or path.endswith("/pulls"):
            return []
        raise AssertionError(f"unexpected paginate {path}")

    def write(self, method, path, body=None):
        self.writes.append({"method": method, "path": path, "body": body})
        if method == "POST" and path.endswith(f"/issues/{self.pr['number']}/comments"):
            self.comments.append({"id": 1000 + len(self.comments), "user": {"login": "github-actions[bot]"},
                                  "body": body["body"]})
        if method == "PATCH" and path.endswith(f"/pulls/{self.pr['number']}"):
            self.pr["state"] = body["state"]
        if method == "PUT" and path.endswith("/merge"):
            self.pr["merged_at"] = "2026-10-09T00:00:00Z"
        return {}


def make_pr(**over):
    pr = {"number": 7, "title": "Add X", "state": "open", "merged_at": None, "labels": [], "user": {"login": "dev"},
          "head": {"sha": "abc", "label": "dev:add-x"}, "mergeable": True, "updated_at": "2026-10-09T00:00:00Z"}
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
