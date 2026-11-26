# PR review bot

Reviews every pull request to this list with Claude and resolves it without the maintainer:

| Outcome | What the bot does |
|---|---|
| Merge | merges, thanks the contributor, redeploys the GitHub Pages site |
| Merge refused by GitHub | asks the contributor to update the branch; retries in maintainer runs; escalates after 14 quiet days ([below](#when-github-refuses-the-merge)) |
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

Replies on a PR are triaged: only the PR author's replies drive the bot. New evidence triggers a re-review
(and a reopen + merge if it now passes, at most twice), questions get an answer, complaints and long
back-and-forth are escalated. A takedown, abuse, legal or security report from anyone is escalated without a
model call. If the maintainer reopens a PR the bot closed, the bot leaves that PR to him.

## When GitHub refuses the merge

GitHub can refuse the Actions token's merge of a **fork** branch based on an older `main` that lacks its newer
workflow changes: `403 Resource not accessible by integration`
([#90](https://github.com/shospodarets/awesome-platform-engineering/pull/90), 25 November 2026). `GITHUB_TOKEN`
can never hold the `workflows` permission, and GitHub does not apply the refusal consistently (a
maintainer-dispatched run merged [#76](https://github.com/shospodarets/awesome-platform-engineering/pull/76),
which had the same gap). When the merge is refused and `main` changed
`.github/workflows/` since the branch point, the bot:

- asks the contributor, once per commit, to update the branch (label `pr-bot:approved-needs-rebase`); the push
  gets a normal review, which merges it if it still passes;
- retries the approved merge in every run the maintainer starts: the daily sweep (a schedule's actor is whoever
  last edited the cron line) and *Run workflow*. No model reviews that commit again, so the retry only merges a
  quiet PR: nobody but the bot has commented or reviewed since the approving review read the PR, and the title
  and description are unchanged. Otherwise it waits for the author's push or the maintainer's `/bot review` (a
  fresh review that reads the PR's comments, though not its reviews, and restarts the count). The hard gates,
  possible duplicates and the entry's link and repositories are checked again against the current `main`; a
  failed check declines (an exact duplicate) or escalates;
- after 14 days without activity escalates to the maintainer instead of closing an approved entry.

A refusal without such a workflow gap, or with a token that lost `contents: write`, is still reported as a bot
failure.

## Maintainer controls

- Comment `/bot review` on a PR to force a fresh review.
- Escalated PRs (`needs-maintainer`) are left alone; decide them yourself.
- Change the judgment by editing [`policy.md`](policy.md); change hard thresholds in `bot/decide.py`.
- Actions → *PR review bot* → *Run workflow*: list PR numbers (empty = all open PRs), `dry_run` to only
  print the plan, or `selftest` to send a test escalation email. With `dry_run` off it also retries at once
  a merge GitHub refused.

## Notifications

Gmail (shospodarets@gmail.com) marks PR notifications from this repository read, except mail with
`Cc: assign@noreply.github.com`, `Cc: ci_activity@noreply.github.com` or the phrase `pr-bot-escalation`,
which stays unread, starred and important. GitHub keeps the assign reason for the whole thread, so once a
PR is escalated every later reply on it reaches the maintainer unread too.
Issues (not PRs) and failed Actions runs also stay unread. If the bot itself fails (e.g. API credit
exhausted, or a run crashes), it opens one "PR review bot needs attention" issue assigned to the maintainer.
Close it once fixed: while it is open, later failures open no new issue.

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
