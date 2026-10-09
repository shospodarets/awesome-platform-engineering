# Contributing

Your contributions are always welcome!

## What gets accepted

This is a curated list, so a submission needs proof that platform engineers already use and value it:

* **Tools:** at least **50 real GitHub stars**, or at least **5 real public user/client feedbacks**
  (case studies, independent reviews, write-ups or talks by users). Link that evidence in the PR description.
* **Time in public use:** a project that was published a few days or weeks ago is too early. Come back after
  it has been used in public for a while (a month at the very least, three months is better).
* **Articles, videos and other resources:** original, substantial content from a credible source. Marketing
  pages, SEO comparison posts and AI-generated content are declined.
* **Disclose your affiliation** if you work on the project. Self-promotion of brand-new products, outreach
  batches and AI-generated submissions are declined.

## How submissions are reviewed

Pull requests are reviewed automatically by an AI reviewer that applies the rules above, then merged,
sent back with requested changes, or declined with the reason. If you think a decision is wrong, or the
project now meets the bar, comment on the PR with the evidence (links): it is reviewed again, and the
maintainer looks at anything the reviewer cannot decide.

## Guidelines

Run a linter before a commit:

```
$ npm install
$ npm test
```

## Examples

* Adding a link:
```
- [Descriptive Name](URL) - Optional short description.
```

* Adding a link with sub-links:
```
- [Link Descriptive Name](URL)
	- [Sub-link1 Descriptive Name](URL)
	- [Sub-link2 Descriptive Name](URL) - Optional short description.
```

* Adding a section:
```
## Contents
...
- [%Section Name%](#section-name)

## Section Name

- [Descriptive Name](URL) - Optional short description.
 ```
