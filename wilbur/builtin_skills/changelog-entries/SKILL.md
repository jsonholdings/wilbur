---
name: changelog-entries
description: This repo's CHANGELOG.md style for a new entry
---
`CHANGELOG.md` starts at `2.4.0`; earlier history lives in `git log` only.

- Add a new `## X.Y.Z` heading at the TOP of the file, above the previous version, once you've
  bumped the version per SemVer (a behavior change or fix bumps at least the patch number).
- Under the heading, one bullet per notable change, written as what is now true, not a diary of
  how it was built: no "fixed a bug where...", prefer "X now does Y" / "Added Z". State
  behavior, not the debugging story.
- Bullets are plain prose sentences ending in a period, not a nested sub-list, and reference the
  concrete mechanism (a flag, a file, a function) rather than a vague "improved robustness".
- Keep each bullet to roughly 1-4 sentences; a change big enough to need more belongs split into
  several bullets.
- Do not remove or rewrite older version sections -- history stays, this file is a record.
