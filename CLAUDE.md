# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is an **"awesome list"** — a single curated catalog of Platform Engineering tools and
resources, following the [sindresorhus/awesome](https://github.com/sindresorhus/awesome)
conventions. There is no application code. The entire content lives in `README.md`; everything
else is tooling to lint that file and publish it as a website.

The published site is generated 1:1 from `README.md` and served via GitHub Pages at
https://shospodarets.github.io/awesome-platform-engineering/.

## Commands

```bash
npm install     # installs dev deps and (via postinstall) sets up the husky git hook
npm test        # runs awesome-lint against README.md — this is the only validation
```

There is no build, no test suite, and no single-test concept. `npm test` runs `awesome-lint`,
which is the same check that gates pull requests in CI. Run it before committing.

The pre-commit hook (`.husky/pre-commit`) runs `lint-staged`, which is configured
(`.lintstagedrc`) to run `awesome-lint` whenever `README.md` is staged.

## Architecture / how the pieces fit

- **`README.md`** — the source of truth and the only content file. Edits to this repo are
  almost always edits to this file.
- **`awesome-lint`** (`package.json` `test` script) — enforces the awesome-list format and
  Markdown style. Both the pre-commit hook and the CI workflow run it.
- **`.github/workflows/pull-requests-lint.yml`** — runs `npm test` on PRs to `main` that touch
  `README.md`. PRs fail if linting fails.
- **`.github/workflows/readme-to-site.yml`** — on push to `main`, copies `README.md` to
  `docs/index.md` and runs `mkdocs gh-deploy` (MkDocs Material, configured in `mkdocs.yml`,
  Python deps in `requirements.txt`) to publish GitHub Pages. The `docs/` and `site/`
  directories are generated at deploy time and are gitignored — do not create or commit them.

## Conventions (enforced by awesome-lint — follow exactly)

- **List item format:** `- [Descriptive Name](URL) - Optional short description.`
  - Use a hyphen `-` for bullets (not `*`).
  - Descriptions are optional, but when present they **must end with a period**. Missing trailing
    periods are a common lint failure (see git history).
- **Sub-links** are nested with **tab indentation** under the parent item.
- **Adding a section:** add the section heading (`## Section Name`) AND a matching entry in the
  `## Contents` table of contents. Anchors are auto-derived from the heading (lowercased, spaces
  → hyphens, punctuation stripped). Note the existing section naming pattern `## Tooling— ...`
  (em dash, no space before it).
- **Whitespace:** `.editorconfig` mandates **tab indentation**, LF line endings, UTF-8, trimmed
  trailing whitespace, and a final newline.

See `CONTRIBUTING.md` for the canonical formatting examples.

## When adding entries

New links go under the most relevant existing section in `README.md`. Keep one entry per line.
After editing, run `npm test` to confirm `awesome-lint` passes before committing.
