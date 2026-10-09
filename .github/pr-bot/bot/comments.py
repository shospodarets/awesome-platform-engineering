"""Public comment texts (in the maintainer's voice) and the hidden state marker."""
from __future__ import annotations

import json
import re

FOOTER = "\n\n_* sent by AI_"
OWNER = "shospodarets"
SITE = "https://shospodarets.github.io/awesome-platform-engineering/"
STATE_RE = re.compile(r"<!-- pr-bot:state (\{.*?\}) -->", re.S)
RULE = ("the project needs at least 50 real GitHub stars, or at least 5 real public user/client feedbacks "
        "(case studies, independent reviews, write-ups by users), plus some time in public use")
# Gmail keeps notification mail containing this phrase unread (GitHub's Cc reason is sticky per thread,
# so the assignment alone is not a reliable signal). Only escalations and failure reports contain it.
ESCALATION_MARKER = "pr-bot-escalation"
REOPEN_HINT = ("If something was missed, or once the project meets the bar, please reply here with the evidence "
               "(links) and it will be reviewed once again.")


def marker(state: dict) -> str:
    return f"\n<!-- pr-bot:state {json.dumps(state, separators=(',', ':'), sort_keys=True)} -->"


def read_state(body: str) -> dict | None:
    found = STATE_RE.findall(body or "")
    if not found:
        return None
    try:
        return json.loads(found[-1])  # the bot appends its marker last
    except json.JSONDecodeError:
        return None


def strip_marker(body: str) -> str:
    return STATE_RE.sub("", body or "").strip()


def _clean(text: str) -> str:
    text = (text or "").strip()
    # the model must not ping people or close the footer itself
    text = re.sub(r"@(?=[A-Za-z0-9-])", "@\u200b", text)
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")  # no forged state markers or hidden text
    text = text.replace("_* sent by AI_", "").strip()
    text = re.sub(r"pr[\W_]*bot[\W_]*escalation", "[removed]", text, flags=re.I)
    return text


def _who(author: str) -> str:
    return f" @{author}" if author else ""


def merged(state: dict, author: str = "") -> str:
    return (f"Merged ✅.{_who(author)} thank you for the contribution! It will appear on [the site]({SITE}) shortly."
            + FOOTER + marker(state))


def approved_needs_rebase(state: dict, author: str = "") -> str:
    return (f"Thank you for the contribution{_who(author)}! The entry is approved, but the PR conflicts with the "
            "current `main`. Please update your branch (rebase or merge `main`) and push: it will be merged "
            "automatically." + FOOTER + marker(state))


def declined(reason: str, state: dict, author: str = "") -> str:
    reason = _clean(reason)
    return (f"Thank you for the contribution{_who(author)}. {reason}\n\n"
            f"The common rule for this list: {RULE}. {REOPEN_HINT}" + FOOTER + marker(state))


def declined_duplicate(existing_name: str, section: str, state: dict, author: str = "") -> str:
    return (f"Thank you{_who(author)}. This is already in the list as **{_clean(existing_name)}** under "
            f"*{_clean(section)}*, so I will close this one as a duplicate. If you meant to update the existing "
            f"entry, please open a PR that changes that line instead. {REOPEN_HINT}" + FOOTER + marker(state))


def changes_requested(reason: str, changes: list[str], state: dict, author: str = "") -> str:
    items = "\n".join(f"- {_clean(c)}" for c in changes if c.strip())
    lead = _clean(reason) or "A few changes are needed before it can be merged:"
    return (f"Thank you for the contribution{_who(author)}! {lead}\n\n{items}\n\n"
            "The PR is re-checked automatically after you push." + FOOTER + marker(state))


def escalated(public_reason: str, state: dict, author: str = "") -> str:
    reason = _clean(public_reason)
    body = f"Thank you for the contribution{_who(author)}! This one needs the maintainer's own review"
    body += f": {reason}" if reason else "."
    body += f"\n\n@{OWNER} please take a look ({ESCALATION_MARKER})."
    return body + FOOTER + marker(state)


def reply(text: str, state: dict) -> str:
    return _clean(text) + FOOTER + marker(state)


def stale_closed(state: dict) -> str:
    return ("Closing this PR because the requested changes were not made within two weeks. If something was "
            "missed, please push the changes and comment here, or open a new PR." + FOOTER + marker(state))


def withdrawn(state: dict, author: str = "") -> str:
    return f"Understood, closing the PR. Thank you{_who(author)}!" + FOOTER + marker(state)
