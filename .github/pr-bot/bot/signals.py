"""Deterministic evidence about a submission, gathered from the GitHub API.

Everything here is computed "as of" a timestamp, so the same code serves live reviews
(as_of = now) and backtests of historical PRs (as_of = the PR's creation time).
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .gh import GitHub, GitHubError
from .readme import (EntryDiff, diff_entries, extract_urls, find_duplicates, github_repo, parse_readme,
                     registrable_domain, sections)

def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


@dataclass
class RepoEvidence:
    repo: str
    exists: bool = True
    stars_now: int = 0
    stars_as_of: int = 0
    forks: int = 0
    watchers: int = 0
    open_issues: int = 0
    created_at: str | None = None
    pushed_at: str | None = None
    age_days_as_of: int = 0
    stars_per_day: float = 0.0
    archived: bool = False
    fork: bool = False
    license: str | None = None
    owner_type: str | None = None
    description: str | None = None
    homepage: str | None = None
    contributors: int = 0
    releases: int = 0
    max_14d_star_share: float | None = None  # share of stars (as of) that arrived in the busiest 14 days
    max_7d_star_share: float | None = None
    biggest_star_day: int | None = None
    stars_last_30d_as_of: int | None = None
    forkers_sampled: int = 0
    ghost_forker_share: float | None = None  # newest forks owned by brand-new, empty accounts
    issues_and_prs_seen: int = 0            # in the latest 100 issues/PRs up to as_of
    outside_issue_authors: int = 0          # distinct authors who are neither the owner nor a contributor
    submitter_affiliated: bool = False
    affiliation_evidence: str = ""
    fake_star_flags: list[str] = field(default_factory=list)
    star_warnings: list[str] = field(default_factory=list)
    star_history_available: bool = False
    linked_to_entry: bool = False   # the entry links this repo, or its homepage is the entry's domain
    source: str = "entry"           # entry | pr_body | identified

    def summary(self) -> dict:
        return asdict(self)


@dataclass
class Evidence:
    pr: dict
    as_of: str
    author: dict
    files: list[str]
    readme_only: bool
    entries: dict
    lint: dict | None
    repos: list[RepoEvidence]
    other_urls: list[str]
    author_prior_prs: list[dict]
    related_prior_prs: list[dict]
    hard_findings: list[str]
    link_check: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["repos"] = [r.summary() for r in self.repos]
        return d


# -- GitHub repo evidence ------------------------------------------------------------------

def star_days(gh: GitHub, repo: str, max_pages: int = 20) -> list[tuple[datetime, int]]:
    """Daily star counts from GitHub's privacy-safe weekly history endpoint, oldest first."""
    days: list[tuple[datetime, int]] = []
    for page in range(1, max_pages + 1):
        weeks = gh._request("GET", f"/repos/{repo}/stargazers/history?per_page=30&page={page}",
                            api_version="2026-03-10")[0] or []
        for w in weeks:
            start = datetime.fromtimestamp(int(w["week"]), tz=timezone.utc)
            for i, c in enumerate(w.get("days") or []):
                days.append((start + timedelta(days=i), int(c)))
        if len(weeks) < 30:
            break
    days.sort(key=lambda x: x[0])
    return days


def star_metrics(days: list[tuple[datetime, int]], as_of: datetime, stars_now: int | None = None) -> dict:
    series = [c for d, c in days if d <= as_of]
    total = sum(series)
    if stars_now is not None:  # count back from today's total: robust to history older than the pages fetched
        total = max(0, stars_now - sum(c for d, c in days if d > as_of))
    if not total:
        return {"stars_as_of": 0, "max_14d_star_share": None, "max_7d_star_share": None,
                "biggest_star_day": 0, "stars_last_30d_as_of": 0}

    def best(window: int) -> int:
        run = top = 0
        for i, c in enumerate(series):
            run += c - (series[i - window] if i >= window else 0)
            top = max(top, run)
        return top

    return {"stars_as_of": total, "max_14d_star_share": round(best(14) / total, 3),
            "max_7d_star_share": round(best(7) / total, 3), "biggest_star_day": max(series),
            "stars_last_30d_as_of": sum(series[-30:])}


def ghost_forkers(gh: GitHub, repo: str, sample: int = 30) -> tuple[int, float | None]:
    """Dagster-style low-activity profile on the newest forks' owners (stargazer lists are private now)."""
    forks = gh.get(f"/repos/{repo}/forks", sort="newest", per_page=sample) or []
    logins = [(f.get("owner") or {}).get("login") for f in forks if (f.get("owner") or {}).get("type") == "User"]
    logins = [l for l in logins if l][:sample]
    if len(logins) < 10:
        return len(logins), None
    fields = "createdAt followers{totalCount} following{totalCount} repositories(privacy:PUBLIC){totalCount} bio"
    query = "query {" + " ".join(f'u{i}: user(login: {json.dumps(l)}) {{ {fields} }}' for i, l in enumerate(logins)) + "}"
    data = gh.graphql(query)
    ghosts = checked = 0
    year_ago = datetime.now(timezone.utc) - timedelta(days=365)
    for u in data.values():
        if not u:
            continue
        checked += 1
        new = parse_ts(u["createdAt"]) > year_ago
        empty = (u["followers"]["totalCount"] <= 1 and u["following"]["totalCount"] <= 1
                 and u["repositories"]["totalCount"] <= 4 and not u.get("bio"))
        ghosts += int(new and empty)
    return checked, (round(ghosts / checked, 3) if checked else None)


def repo_evidence(gh: GitHub, repo: str, as_of: datetime, pr_author: str, deep: bool = True) -> RepoEvidence:
    ev = RepoEvidence(repo=repo)
    try:
        meta = gh.get(f"/repos/{repo}")
    except GitHubError as e:
        if e.status not in (404, 410, 451):
            raise  # rate limits and outages are retried later, not reported as a missing repository
        ev.exists = False
        ev.affiliation_evidence = f"repository not found ({e.status})"
        return ev
    if meta.get("full_name") and meta["full_name"].lower() != repo.lower():
        ev.repo = meta["full_name"]  # follow renames/transfers
        repo = meta["full_name"]
    created = parse_ts(meta.get("created_at"))
    ev.stars_now = meta.get("stargazers_count", 0)
    ev.forks = meta.get("forks_count", 0)
    ev.watchers = meta.get("subscribers_count", 0)
    ev.open_issues = meta.get("open_issues_count", 0)
    ev.created_at = meta.get("created_at")
    ev.pushed_at = meta.get("pushed_at")
    ev.age_days_as_of = max(0, (as_of - created).days) if created else 0
    ev.stars_per_day = round(ev.stars_now / max(1, (datetime.now(timezone.utc) - created).days), 2) if created else 0.0
    ev.archived = bool(meta.get("archived"))
    ev.fork = bool(meta.get("fork"))
    ev.license = (meta.get("license") or {}).get("spdx_id")
    ev.owner_type = (meta.get("owner") or {}).get("type")
    ev.description = meta.get("description")
    ev.homepage = meta.get("homepage")
    contributor_logins: set[str] = set()
    try:
        ev.contributors = gh.count(f"/repos/{repo}/contributors", anon="1")
        ev.releases = gh.count(f"/repos/{repo}/releases")
        contributor_logins = {c.get("login", "").lower()
                              for c in gh.get(f"/repos/{repo}/contributors", per_page=100) or []}
    except GitHubError:
        pass
    owner = repo.split("/")[0]
    ev.stars_as_of = ev.stars_now
    if deep:
        try:
            days = star_days(gh, repo)
            if days:
                m = star_metrics(days, as_of, ev.stars_now)
                ev.star_history_available = True
                for k, v in m.items():
                    setattr(ev, k, v)
        except GitHubError:
            pass
        try:
            ev.forkers_sampled, ev.ghost_forker_share = ghost_forkers(gh, repo)
        except (GitHubError, KeyError, TypeError):
            pass
        # real users file issues and PRs; purchased stars do not
        try:
            items = gh.get(f"/repos/{repo}/issues", state="all", per_page=100, sort="created", direction="desc")
            items = [i for i in items or [] if parse_ts(i.get("created_at")) and parse_ts(i["created_at"]) <= as_of]
            ev.issues_and_prs_seen = len(items)
            outside = {(i.get("user") or {}).get("login", "").lower() for i in items
                       if (i.get("user") or {}).get("type") == "User"}
            outside -= contributor_logins | {owner.lower(), pr_author.lower(), ""}
            ev.outside_issue_authors = len(outside)
        except GitHubError:
            pass
    # affiliation of the PR author with the project
    if owner.lower() == pr_author.lower():
        ev.submitter_affiliated, ev.affiliation_evidence = True, "PR author owns the repository"
    elif pr_author.lower() in contributor_logins:
        ev.submitter_affiliated, ev.affiliation_evidence = True, "PR author is a contributor of the repository"
    elif ev.owner_type == "Organization":
        try:
            gh._request("GET", f"/orgs/{owner}/members/{pr_author}")
            ev.submitter_affiliated, ev.affiliation_evidence = True, "PR author is a public member of the owning org"
        except GitHubError:
            pass
    ev.fake_star_flags = fake_star_flags(ev)
    ev.star_warnings = star_warnings(ev)
    return ev


def fake_star_flags(ev: RepoEvidence) -> list[str]:
    """Strong signs that stars were bought or farmed (these block an automatic merge).

    Adapted from StarScout / Dagster / RealStars to what GitHub still exposes (daily star history,
    forks, repository counters). Launch spikes (Hacker News, a release) are normal, so bursts are only
    soft warnings: see star_warnings()."""
    flags = []
    n = ev.stars_as_of or ev.stars_now
    if n >= 50 and ev.forks <= 1 and ev.watchers <= 1 and ev.contributors <= 1:
        flags.append("stars are not matched by any forks, watchers or other contributors")
    spike = n >= 50 and ev.biggest_star_day and ev.biggest_star_day / n >= 0.5
    corroborated = (ev.forks / max(n, 1) < 0.05 or (ev.ghost_forker_share or 0) > 0.2
                    or (ev.issues_and_prs_seen and ev.outside_issue_authors == 0 and ev.forks <= 2))
    if spike and corroborated:
        # organic launches in the merge history peak at <=19% of all stars on one day; a farmed batch lands at
        # once and leaves no forks or outside users behind (a genuine launch day alone is only a warning)
        flags.append(f"{int(100 * ev.biggest_star_day / n)}% of all stars arrived on a single day")
    if n >= 200 and ev.forks / max(n, 1) < 0.03:
        flags.append(f"fork-to-star ratio {ev.forks / n:.3f} is far below organic projects (~0.1-0.25)")
    if ev.ghost_forker_share is not None and ev.forkers_sampled >= 10 and ev.ghost_forker_share > 0.3:
        flags.append(f"{int(ev.ghost_forker_share * 100)}% of the newest forks belong to brand-new empty accounts")
    return flags


def star_warnings(ev: RepoEvidence) -> list[str]:
    """Weaker signals for the model to weigh: bursts can be a genuine launch."""
    warns = []
    n = ev.stars_as_of or ev.stars_now
    if n >= 50 and ev.max_14d_star_share is not None and ev.max_14d_star_share >= 0.5 and ev.age_days_as_of >= 45:
        warns.append(f"{int(ev.max_14d_star_share * 100)}% of the stars arrived within one 14-day window")
    if 50 <= n <= 80 and ev.max_7d_star_share is not None and ev.max_7d_star_share > 0.8:
        warns.append("the stars just above the 50-star bar nearly all arrived within one week")
    if n >= 50 and ev.age_days_as_of < 30:
        warns.append(f"the repository is only {ev.age_days_as_of} days old")
    if n >= 50 and ev.biggest_star_day and ev.biggest_star_day / n >= 0.5:
        warns.append(f"{int(100 * ev.biggest_star_day / n)}% of all stars arrived on a single day")
    return warns


# -- PR-level evidence ---------------------------------------------------------------------

ZERO_WIDTH = re.compile("[\u200b\u200c\u2060\ufeff]")  # not U+200D, which joins emoji sequences
INJECTION_WORDS = re.compile(r"\b(ignore|disregard|previous instructions|system prompt|you are an?|as an ai|"
                             r"ai reviewer|language model|llm|claude|gpt|approve (this|the)|merge (this|the) pr|"
                             r"auto-?merge)\b", re.I)


def hidden_instructions(text: str) -> list[str]:
    """Text a human reviewer would not see but a model would: HTML comments or zero-width characters
    that carry reviewer-directed instructions."""
    found = []
    for m in re.finditer(r"<!--(.*?)-->", text or "", re.S):
        if INJECTION_WORDS.search(m.group(1)):
            found.append("HTML comment: " + m.group(1).strip()[:120])
    if ZERO_WIDTH.search(text or "") and INJECTION_WORDS.search(ZERO_WIDTH.sub("", text or "")):
        found.append("zero-width characters around reviewer-directed text")
    return found


def link_status(url: str) -> dict:
    """Does the entry's link resolve? 404/410/NXDOMAIN mean broken; 401/403/429/5xx are inconclusive."""
    import socket
    import urllib.error
    import urllib.request
    headers = {"User-Agent": "Mozilla/5.0 (awesome-platform-engineering link check)"}
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                return {"url": url, "status": r.status, "final_url": r.geturl()}
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 429, 500, 501):
                continue
            return {"url": url, "status": e.code}
        except (urllib.error.URLError, socket.timeout, ValueError) as e:
            reason = str(getattr(e, "reason", e))
            if method == "HEAD":
                continue
            return {"url": url, "status": None, "error": reason[:120],
                    "broken": "Name or service not known" in reason or "nodename nor servname" in reason}
    return {"url": url, "status": None}


def file_at(gh: GitHub, repo: str, path: str, ref: str) -> str:
    data = gh.get(f"/repos/{repo}/contents/{path}", ref=ref)
    return base64.b64decode(data["content"]).decode("utf-8", "replace")


def gather(gh: GitHub, pr: dict, as_of: datetime | None = None, lint: dict | None = None,
           base_readme: str | None = None, head_readme: str | None = None, deep: bool = True,
           all_prs: list[dict] | None = None) -> Evidence:
    """Collect the deterministic evidence for one pull request."""
    as_of = as_of or datetime.now(timezone.utc)
    number = pr["number"]
    author = pr["user"]["login"]
    head_repo = (pr.get("head") or {}).get("repo") or {}
    head_full = head_repo.get("full_name") or gh.repo
    head_sha = pr["head"]["sha"]
    try:  # the files of exactly the commit under review (a later push cannot slip in unseen)
        cmp_files = gh.get(f"/repos/{gh.repo}/compare/{pr['base']['sha']}...{head_sha}").get("files") or []
        files = [f["filename"] for f in cmp_files]
    except GitHubError:
        files = [f["filename"] for f in gh.paginate(gh.r(f"/pulls/{number}/files"), max_pages=3)]
    readme_only = files == ["README.md"]
    if head_readme is None:
        try:
            head_readme = file_at(gh, head_full, "README.md", head_sha)
        except GitHubError:
            head_readme = file_at(gh, gh.repo, "README.md", head_sha)
    base_ref = pr["base"]["sha"] if as_of < datetime.now(timezone.utc) - timedelta(hours=1) else pr["base"]["ref"]
    if base_readme is None:
        base_readme = file_at(gh, gh.repo, "README.md", base_ref)
    # Diff against the merge base, like GitHub does: entries added to main after the PR branched off
    # must not look like removals. Duplicates are checked against the current list.
    merge_base_readme = base_readme
    try:
        cmp = gh.get(f"/repos/{gh.repo}/compare/{base_ref}...{head_sha}")
        mb = (cmp.get("merge_base_commit") or {}).get("sha")
        if mb:
            merge_base_readme = file_at(gh, gh.repo, "README.md", mb)
    except GitHubError:
        pass
    d: EntryDiff = diff_entries(merge_base_readme, head_readme)
    d.duplicates = find_duplicates(d.added, parse_readme(base_readme))

    try:
        u = gh.get(f"/users/{author}")
        acct_created = parse_ts(u.get("created_at"))
        author_info = {"login": author, "type": u.get("type"), "created_at": u.get("created_at"),
                       "account_age_days_as_of": (as_of - acct_created).days if acct_created else None,
                       "followers": u.get("followers"), "public_repos": u.get("public_repos"),
                       "company": u.get("company"), "blog": u.get("blog"), "bio": u.get("bio"),
                       "association": pr.get("author_association")}
    except GitHubError:
        author_info = {"login": author, "association": pr.get("author_association")}

    # GitHub repositories the added/edited entries link first, then ones named in the PR body.
    # Repositories already listed in the README are context, never evidence for the new entry.
    listed = {e.github_repo.lower() for e in parse_readme(base_readme) if e.github_repo}
    entry_domains = {registrable_domain(e.url) for e in d.added + [new for _, new in d.modified] if not e.github_repo}
    candidates: list[tuple[str, str]] = []
    for e in d.added + [new for _, new in d.modified]:
        if e.github_repo:
            candidates.append((e.github_repo, "entry"))
    body_urls = extract_urls(pr.get("body") or "")
    for url in body_urls:
        r = github_repo(url)
        if r and r.lower() not in listed:
            candidates.append((r, "pr_body"))
    seen, repos = set(), []
    for r, source in candidates:
        if r.lower() in seen or r.lower() == gh.repo.lower():
            continue
        seen.add(r.lower())
        rep = repo_evidence(gh, r, as_of, author, deep=deep and len(repos) < 2)
        rep.source = source
        rep.linked_to_entry = source == "entry" or bool(
            rep.homepage and registrable_domain(rep.homepage) in entry_domains)
        repos.append(rep)
        if len(repos) >= 3:
            break
    other_urls = [u for u in body_urls if not github_repo(u)][:15]

    prior, related = [], []
    names = {e.name.split("-")[0].strip().lower() for e in d.added if e.name}
    slugs = {r.repo.lower() for r in repos}
    domains = {registrable_domain(e.url) for e in d.added if not e.github_repo}
    for p in all_prs or []:
        if p["number"] == number or parse_ts(p["created_at"]) >= as_of:
            continue
        rec = {"number": p["number"], "title": p["title"], "author": p["user"]["login"],
               "state": "merged" if p.get("merged_at") else p["state"], "created_at": p["created_at"]}
        if p["user"]["login"].lower() == author.lower():
            prior.append(rec)
        text = f"{p['title']} {p.get('body') or ''}".lower()
        if any(s in text for s in slugs) or any(n and len(n) > 3 and n in p["title"].lower() for n in names) \
                or any(dm and dm in text for dm in domains):
            related.append(rec)

    hard = []
    injection = hidden_instructions(pr.get("body") or "") + hidden_instructions(
        "\n".join([e.raw for e in d.added + [new for _, new in d.modified]] + d.other_added_lines))
    if injection:
        hard.append("hidden instructions aimed at automated reviewers: " + "; ".join(injection)[:300])
    if not readme_only:
        hard.append("changes files other than README.md: " + ", ".join(files))
    if d.removed:
        hard.append(f"removes {len(d.removed)} existing entr{'y' if len(d.removed) == 1 else 'ies'}")
    if d.duplicates:
        hard.append("duplicates: " + "; ".join(f"'{a.name}' matches existing '{b.name}' in {b.section}"
                                                for a, b in d.duplicates))
    if not d.added and not d.modified:
        hard.append("adds no list entry")
    if lint and not lint.get("ok", True):
        hard.append("awesome-lint fails on the PR's README")
    links = [link_status(e.url) for e in (d.added + [new for _, new in d.modified])[:3]]
    for lk in links:
        if lk.get("status") in (404, 410) or lk.get("broken"):
            hard.append(f"broken link: {lk['url']} ({lk.get('status') or lk.get('error')})")

    def ent(e):
        return {"name": e.name, "url": e.url, "description": e.description, "section": e.section,
                "github_repo": e.github_repo, "raw": e.raw}

    return Evidence(
        pr={"number": number, "title": pr["title"], "body": (pr.get("body") or "")[:6000],
            "created_at": pr["created_at"], "state": pr["state"], "draft": pr.get("draft", False),
            "head_sha": head_sha, "author_association": pr.get("author_association"),
            "maintainer_can_modify": pr.get("maintainer_can_modify")},
        as_of=iso(as_of), author=author_info, files=files, readme_only=readme_only,
        entries={"added": [ent(e) for e in d.added],
                 "removed": [ent(e) for e in d.removed],
                 "modified": [{"old": ent(a), "new": ent(b)} for a, b in d.modified],
                 "other_added_lines": d.other_added_lines[:20],
                 "other_removed_lines": d.other_removed_lines[:20],
                 "duplicates": [{"added": ent(a), "existing": ent(b)} for a, b in d.duplicates],
                 "list_sections": sections(base_readme)},
        lint=lint, repos=repos, other_urls=other_urls,
        author_prior_prs=prior[:10], related_prior_prs=related[:10], hard_findings=hard, link_check=links)
