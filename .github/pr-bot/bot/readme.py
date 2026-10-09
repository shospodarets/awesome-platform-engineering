"""Parse the awesome-list README and compute which entries a PR adds, removes or edits."""
from __future__ import annotations

import difflib
import re
import urllib.parse
from dataclasses import dataclass, field

ENTRY_RE = re.compile(r"^(?P<indent>\s*)[-*]\s+\[(?P<name>[^\]]+)\]\((?P<url>[^)\s]+)\)(?P<rest>.*)$")
URL_RE = re.compile(r"https?://[^\s)<>\"'\]]+")
GITHUB_REPO_RE = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
NOT_REPO_OWNERS = {"orgs", "sponsors", "features", "marketplace", "topics", "collections", "about", "settings",
                   "apps", "enterprise", "pricing", "login", "site", "trending", "user-attachments"}


@dataclass
class Entry:
    name: str
    url: str
    description: str
    section: str
    line_no: int
    raw: str
    indent: int = 0

    @property
    def github_repo(self) -> str | None:
        return github_repo(self.url)


@dataclass
class EntryDiff:
    added: list[Entry] = field(default_factory=list)
    removed: list[Entry] = field(default_factory=list)
    modified: list[tuple[Entry, Entry]] = field(default_factory=list)   # (old, new)
    other_added_lines: list[str] = field(default_factory=list)          # non-entry lines added
    other_removed_lines: list[str] = field(default_factory=list)
    duplicates: list[tuple[Entry, Entry]] = field(default_factory=list)  # (added, existing)


def github_repo(url: str) -> str | None:
    m = GITHUB_REPO_RE.match(url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in NOT_REPO_OWNERS:
        return None
    repo = re.sub(r"\.git$", "", repo)
    repo = repo.split("#")[0].split("?")[0]
    if not repo or repo in (".", ".."):
        return None
    return f"{owner}/{repo}"


def normalize_url(url: str) -> str:
    u = urllib.parse.urlsplit(url.strip())
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = u.path.rstrip("/")
    query = "&".join(p for p in u.query.split("&") if p and not p.lower().startswith(("utm_", "ref=", "source=")))
    norm = f"{host}{path}"
    if query:
        norm += f"?{query}"
    return norm.lower() if host == "github.com" else norm


def registrable_domain(url: str) -> str:
    host = (urllib.parse.urlsplit(url.strip()).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def extract_urls(text: str) -> list[str]:
    seen, out = set(), []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_readme(text: str) -> list[Entry]:
    entries: list[Entry] = []
    section = ""
    for i, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if line.startswith("# "):
            section = ""
            continue
        m = ENTRY_RE.match(line)
        if m and section and section.lower() != "contents":
            rest = m.group("rest").strip()
            desc = re.sub(r"^[-–—:]\s*", "", rest).strip()
            entries.append(Entry(name=m.group("name").strip(), url=m.group("url").strip(), description=desc,
                                 section=section, line_no=i, raw=line, indent=len(m.group("indent"))))
    return entries


def sections(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines()
            if line.startswith("## ") and line[3:].strip().lower() != "contents"]


def _entry_key_name(e: Entry) -> str:
    # "Peon- Open source ..." style names carry the description inside the brackets
    return re.split(r"\s*[-–—:]\s+|\s*[-–—]\s*(?=[A-Z])", e.name, maxsplit=1)[0].strip().lower()


def diff_entries(base_text: str, head_text: str) -> EntryDiff:
    base_lines = base_text.splitlines()
    head_lines = head_text.splitlines()
    head_entries = {e.line_no: e for e in parse_readme(head_text)}
    base_entries = {e.line_no: e for e in parse_readme(base_text)}
    out = EntryDiff()
    sm = difflib.SequenceMatcher(a=base_lines, b=head_lines, autojunk=False)
    removed: list[Entry] = []
    added: list[Entry] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for i in range(i1, i2):
            if (i + 1) in base_entries:
                removed.append(base_entries[i + 1])
            elif base_lines[i].strip():
                out.other_removed_lines.append(base_lines[i])
        for j in range(j1, j2):
            if (j + 1) in head_entries:
                added.append(head_entries[j + 1])
            elif head_lines[j].strip():
                out.other_added_lines.append(head_lines[j])
    # pair removed/added entries that are edits of the same item (same url or same name)
    for old in list(removed):
        match = next((new for new in added
                      if normalize_url(new.url) == normalize_url(old.url)
                      or _entry_key_name(new) == _entry_key_name(old)), None)
        if match:
            out.modified.append((old, match))
            removed.remove(old)
            added.remove(match)
    out.removed = removed
    out.added = added
    out.duplicates = find_duplicates(out.added, list(base_entries.values()))
    return out


def find_duplicates(added: list[Entry], existing: list[Entry]) -> list[tuple[Entry, Entry]]:
    dups = []
    for new in added:
        for old in existing:
            same_url = normalize_url(new.url) == normalize_url(old.url)
            same_repo = new.github_repo and old.github_repo and new.github_repo.lower() == old.github_repo.lower()
            same_name = _entry_key_name(new) == _entry_key_name(old) and len(_entry_key_name(new)) > 2
            if same_url or same_repo or same_name:
                dups.append((new, old))
                break
    return dups
