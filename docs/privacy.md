# Privacy

This repository is public. It ships a product, never its author's data. That is
a property worth enforcing with a gate rather than with care, so it is enforced
by `scripts/privacy-scan.sh`, run from `.github/workflows/privacy.yml` on every
push and pull request.

## Two deny-lists

| File | Committed? | Holds |
|---|---|---|
| `.github/privacy-denylist.txt` | yes | **patterns**, never secrets: identifier *shapes* (an email, a private address block, an internal-hostname shape) and the project's former name |
| `.github/privacy-denylist.local.txt` | **no**, gitignored | the author's own vocabulary: real names, hostnames, serials, the exports the parsers were written against |

The local file is gitignored because committing it would publish exactly the
strings it exists to keep out — but a gitignored file is invisible to CI, which
made the scan blind to the class it was written for. It therefore reaches CI
from the **`PRIVACY_DENYLIST_LOCAL` repository secret**, materialised into the
gitignored path for the length of the job. Fork pull requests receive no
secrets, so there the missing supplement is a warning; on `main` it is a hard
failure, because a partial scan that reports `clean` is worse than a red one —
it looks like a pass.

The patterns are extended regexes, one per line; blank lines and `#` comments
are ignored. Paths in `.privacyignore` are skipped.

## What the scan covers

- **The working tree**, on every run.
- **The full history**, on every run — public history is permanent, so a clean
  working tree is not enough: a leak three commits back is still a leak.
- A **self-test job** plants the former project name in a throwaway repository
  and fails if the scan accepts it. A gate that has never been seen failing is
  decoration, and every way a gate rots — a `paths:` filter, an emptied
  deny-list, a renamed file, a scanner that exits 0 on a parse error — looks
  exactly like a passing build.

`gitleaks` runs alongside it over the same history, for credentials rather than
identifiers.

## Before making a change here

`./scripts/privacy-scan.sh` locally, and `--history` before anything is pushed
for the first time. A match means the content must not exist in the repository:
deleting the file and committing again does not help, because the content stays
in history and in every clone.

A false positive is narrowed in the deny-list pattern, or by adding a specific
path to `.privacyignore` — every ignore entry is a hole in the control, so the
list is kept as short as possible. `LICENSE` is the one deliberate exception: a
copyright notice has to name its holder to mean anything.

## The other direction: data you import

The repository is not the only place data can leak from. Takeback reads
archives you provide, and two features exist for sharing them safely:

- **Anonymising an import** (`app/anonymize.py`, from an import's page) rebuilds
  the archive with every text value replaced by coherent fake data, on a
  default-deny model: anything the code does not explicitly recognise as
  technical is treated as sensitive. See the README.
- **Screen-sharing mode** (the crossed-out eye in the sidebar) blurs page text
  and strips chart labels, values and tooltips, in the browser only. It changes
  no data and is not a substitute for anonymising an export.

Neither is a substitute for not exposing the app: Takeback has **no
authentication**, and an imported archive is your entire history with a
platform. Run it locally or behind an auth proxy — see the README.
