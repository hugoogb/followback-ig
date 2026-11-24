# Instagram Follow-Back Cleanup Util — Design

**Date:** 2026-06-26
**Status:** Implemented

## Purpose

Help the user clean up their Instagram account by identifying, fully offline,
the people they follow who do **not** follow them back, and producing a fast,
clickable action list for manual unfollowing.

No login, no network, no API. Everything runs locally against the files from
Instagram's "Download your information" export (scope: *Followers and
following*, format: JSON).

## Decisions

- **Scope:** Offline analysis **+** a clickable action list. No automation of
  unfollowing (that would violate Instagram's ToS and risk account action).
- **Whitelist:** None. Every non-mutual the user follows appears every run.
- **Action list contents:** profile links + checkboxes, follow date, and a
  plain-text/CSV export.

## Key change from the base script

The base script collapses everything into bare `set[str]` of usernames via set
algebra (`following - followers`, etc.). This design keeps the richer per-account
data the export already contains — **username, profile URL (`href`), and follow
date (`timestamp`)** — so the action list can show and sort by follow date.

## Architecture

Single file, `ig_followback.py`, four small and independently testable units:

1. **`load_accounts(folder) -> (dict, dict)`**
   Parses `following.json` and `followers_*.json` into mappings
   `username -> {"href": str, "timestamp": int | None}`.
   Preserves robustness of the base script:
   - multi-file followers (`followers_1.json`, `followers_2.json`, …)
   - older single-file exports (`followers.json`)
   - dict-vs-list top-level shapes (`relationships_following` /
     `relationships_followers` keys vs. bare lists)

2. **`compute_diff(following, followers) -> Diff`**
   Pure function over the dict keys. Returns:
   - `not_following_back` — you follow them, they don't (cleanup candidates)
   - `fans_you_dont_follow_back` — they follow you, you don't follow them
   - `mutuals` — count of `following & followers`
   Candidate records are sorted oldest-follow-first; unknown dates sort last.

3. **`write_outputs(candidates, folder)`**
   Emits three files into the export folder:
   - `cleanup.html` — action list: one row per candidate with a clickable
     `https://instagram.com/<user>` link, a checkbox, and the follow date.
     Sortable by follow date, oldest-first by default. Dependency-free:
     plain HTML + a few lines of vanilla JS.
   - `not_following_back.csv` — `username,profile_url,followed_on`.
   - `not_following_back.txt` — bare usernames (base-script compatibility).

4. **`main()`** — wires it together, prints summary counts, points the user at
   the generated files.

## Data details

- The export `timestamp` is a Unix epoch integer. Convert to `YYYY-MM-DD`
  (UTC) for display.
- Entries with no timestamp or `0` (IG's missing-value sentinel) show
  `unknown` and sort last (in the HTML the sort key is `~`, which orders after
  all digits).
- Sort candidates by follow date ascending; ties broken by username for
  determinism.

## Error handling

- Missing `following.json` → clear exit message.
- No `followers_*.json` and no `followers.json` → clear exit message.
- Malformed/missing fields per entry are skipped gracefully, never crash.

## Testing

`compute_diff` and the parsing are pure, so tests use small fixture JSON to
verify the three diff categories, timestamp formatting (including the `unknown`
and `0` cases), multi-file follower loading, and dict-vs-list shape handling.
Run: `python3 -m unittest test_ig_followback -v`.

## Out of scope

- Any login, scraping, or automated unfollowing.
- A whitelist (explicitly declined).
- Networked profile enrichment (follower counts, bios, etc.).

## Later additions (2026-11)

- **Keep mark** — supersedes "Whitelist: none". Accounts followed on purpose
  are marked *keep* in the action list and saved to `ignore.txt` alongside
  *dead* ones; saving merges with the ignore list already in effect.
- **Optional export files** — close friends (tagged), recently unfollowed (own
  list), blocked/restricted (counted).
- **`--since OLD_EXPORT`** — who stopped following you and who started.
- **Browser version** (`web/index.html`) — a port of the parser and the action
  list that reads the zip in the page, under a CSP that forbids network access.
  `web/test_web.cjs` checks it against the Python tool.
