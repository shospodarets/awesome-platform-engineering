# PR review bot

Reviews every pull request to this list with Claude and resolves it without the maintainer:

| Outcome | What the bot does |
|---|---|
| Merge | merges, thanks the contributor, redeploys the GitHub Pages site |
| Decline | explains the reason, invites a comment with evidence, closes the PR |
| Request changes | lists the fixes (e.g. awesome-lint errors); re-checks on every push; closes after 14 quiet days |
| Escalate | assigns @shospodarets, labels `needs-maintainer`, comments with `pr-bot-escalation` |

Every comment ends with `_* sent by AI_`.

## How a decision is made

1. **Deterministic evidence** (`bot/signals.py`): the README diff (entries added, removed, edited,
   duplicates, section), awesome-lint on the PR's README, and for each GitHub repository: stars at
   submission time from GitHub's daily star history, forks, contributors, releases, outside issue
   authors, a sample of the newest forks' owners, and whether the submitter is affiliated.
2. **Hard gates** (`bot/decide.py`, `pre_llm`): drafts and maintainer PRs are skipped; PRs touching other
   files or removing entries are escalated; exact duplicates and hidden instructions to AI reviewers are
   declined.
3. **Claude's review** (`bot/llm.py`, Opus 5.5 with web search) against [`policy.md`](policy.md), the
   maintainer's criteria distilled from 70+ past PR decisions.
4. **Safety net** (`post_llm`): a merge needs 50+ stars without fake-star flags, or 5+ independent public
   usage sources across 3+ domains; low-confidence decisions and declines of established projects are
   escalated instead.

Replies on a PR are triaged: new evidence triggers a re-review (and a reopen + merge if it now passes),
questions get an answer, complaints and repeated back-and-forth are escalated.

## Maintainer controls

- Comment `/bot review` on a PR to force a fresh review.
- Escalated PRs (`needs-maintainer`) are left alone; decide them yourself.
- Change the judgment by editing [`policy.md`](policy.md); change hard thresholds in `bot/decide.py`.
- Actions → *PR review bot* → *Run workflow*: list PR numbers (empty = all open PRs), `dry_run` to only
  print the plan, or `selftest` to send a test escalation email.

## Notifications

Gmail (shospodarets@gmail.com) marks every notification from this repository read, except mail with
`Cc: assign@noreply.github.com` or the phrase `pr-bot-escalation`, which stays unread and starred.
If the bot itself fails (e.g. API credit exhausted), it opens one "PR review bot needs attention" issue
assigned to the maintainer.

## Cost and keys

`ANTHROPIC_API_KEY` is a repository secret: key `awesome-platform-engineering-pr-bot` in the personal
Claude Console organisation, workspace `awesome-platform-engineering` with a $30/month spend cap and an
email alert at $20. A review costs roughly $0.05–0.15.

## Development

```
cd .github/pr-bot
pip install -r requirements.txt pytest
python -m pytest -q tests
GITHUB_TOKEN=... ANTHROPIC_API_KEY=... python -m bot review 77          # dry run, prints the decision
GITHUB_TOKEN=... ANTHROPIC_API_KEY=... python -m bot backtest 44 45     # replay past PRs as of their creation
```
