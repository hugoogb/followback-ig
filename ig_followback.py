#!/usr/bin/env python3
"""
Instagram follow-back cleanup (personal, offline).

Reads the JSON files from your Instagram "Export your information" export
(scope: Followers and following, format: JSON) and helps you clean up who you
follow. It reports:
  - people you follow who DON'T follow you back  (cleanup candidates)
  - people who follow you that you DON'T follow back
  - mutuals count

and generates an action list to make manual unfollowing fast:
  - cleanup.html             clickable profile links + checkboxes, sortable by follow date
  - not_following_back.csv   username, profile_url, followed_on
  - not_following_back.txt   bare usernames

No login, no network, no API. Everything runs locally on the export files.
Automated unfollowing is intentionally NOT included: it violates Instagram's
Terms of Service and risks action against your account.

Usage:
    python ig_followback.py /path/to/connections/followers_and_following
or drop this script into that folder and just run:
    python ig_followback.py
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

# Instagram usernames are letters, digits, periods and underscores, max 30.
# Anything else that lands in a username field is a display name or a stray
# label, and would build a profile URL that is guaranteed to 404.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Matches the plain web profile URL and the `/_u/<user>` app deeplink the
# export actually contains.
_HREF_RE = re.compile(r"instagram\.com/(?:_u/)?([A-Za-z0-9._]{1,30})/?$")

# "Username" as Instagram writes it in a few common export languages. Only a
# hint -- parsing falls back to the shape of the value, so an export in an
# unlisted language still works.
_USERNAME_LABELS = {
    "username", "nombre de usuario", "nom d'utilisateur", "nome utente",
    "benutzername", "nome de usuário", "gebruikersnaam", "användarnamn",
    "kullanıcı adı", "nazwa użytkownika", "имя пользователя", "ユーザーネーム",
    "사용자 이름", "用户名",
}


@dataclass
class ParseStats:
    """Anomalies noticed while reading an export, surfaced to the user.

    Broken profile links are the symptom these explain: a block whose username
    could not be read produces no row at all, and one that yielded something
    that isn't a username produces a row whose link cannot resolve.
    """
    skipped: int = 0
    suspicious: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.skipped + len(self.suspicious)


def _username_from_href(href) -> str:
    """Pull the username out of a profile href, or "" if there isn't one."""
    match = _HREF_RE.search((href or "").strip())
    return match.group(1) if match else ""


def _label_value_candidates(pairs) -> list[str]:
    """Username candidates from a `label_values` list, best last.

    The labels are localised, so a label match is only a hint. The reliable
    signal is the shape of the value: a username has no spaces and fits the
    Instagram character set, while a display name usually doesn't.
    """
    labelled, shaped = [], []
    for pair in pairs:
        label = (pair.get("label") or "").strip().casefold()
        value = (pair.get("value") or "").strip()
        if not value:
            continue
        if label in _USERNAME_LABELS:
            labelled.append(value)
        elif _USERNAME_RE.match(value):
            shaped.append(value)
    # Instagram orders Name before Username, so a later shaped value is the
    # better guess; an explicitly labelled one beats both.
    return list(reversed(shaped)) + labelled


def _username_candidates(block) -> list[str]:
    """Every place a username might live in one block, best first.

    The href comes first because it is the only source identical in every
    language and never holds a display name -- it is what makes a link work.
    """
    hrefs, values = [], []
    for item in block.get("string_list_data") or []:
        from_href = _username_from_href(item.get("href"))
        if from_href:
            hrefs.append(from_href)
        value = (item.get("value") or "").strip()
        if value:
            values.append(value)

    candidates = hrefs + values
    candidates += reversed(_label_value_candidates(block.get("label_values") or []))
    title = (block.get("title") or "").strip()
    if title:
        candidates.append(title)
    return candidates


def _username_and_timestamp(block) -> tuple[str, int | None]:
    """Pull (username, timestamp) from one IG relationship block.

    Instagram emits the same data in several shapes across export files:
      - followers:  string_list_data[].value holds the username
      - following:  string_list_data[] has only href + timestamp;
                    the username is the block-level "title"
      - pending:    no string_list_data at all; label_values holds a
                    {"label": "Username", "value": ...} pair, and the
                    timestamp is on the block itself
    Every location is tried, and a candidate that actually looks like a
    username wins over one that doesn't -- otherwise a display name sitting in
    a "title" would become a profile URL that cannot resolve.
    """
    timestamp = block.get("timestamp")  # block-level (pending requests)
    for item in block.get("string_list_data") or []:
        if item.get("timestamp"):
            timestamp = item["timestamp"]

    candidates = _username_candidates(block)
    valid = next((c for c in candidates if _USERNAME_RE.match(c)), "")
    # Fall back to an unusable candidate rather than dropping the account: the
    # row is still worth showing, and its "find" link can search for it.
    return (valid or (candidates[0] if candidates else "")), timestamp


def _extract_accounts(blocks, stats: ParseStats | None = None) -> dict[str, dict]:
    """Pull accounts out of a list of IG relationship blocks.

    Returns a mapping username -> {"href": str, "timestamp": int | None}.
    The profile URL is always built as a clean web link from the username
    rather than reusing the export's href, which is sometimes a
    `.../_u/<user>` app-deeplink that doesn't open cleanly in a browser.
    """
    accounts: dict[str, dict] = {}
    for block in blocks:
        username, timestamp = _username_and_timestamp(block)
        if not username:
            if stats is not None:
                stats.skipped += 1
            continue
        if stats is not None and not _USERNAME_RE.match(username):
            stats.suspicious.append(username)
        accounts[username] = {
            "href": f"https://instagram.com/{username}",
            "timestamp": timestamp,
        }
    return accounts


def _load_following(folder: Path, stats: ParseStats | None = None) -> dict[str, dict]:
    path = folder / "following.json"
    if not path.exists():
        sys.exit(f"Could not find {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    # following.json is a dict keyed by "relationships_following"
    blocks = data["relationships_following"] if isinstance(data, dict) else data
    return _extract_accounts(blocks, stats)


def _load_followers(folder: Path, stats: ParseStats | None = None) -> dict[str, dict]:
    # Instagram splits large follower lists: followers_1.json, followers_2.json, ...
    files = sorted(folder.glob("followers_*.json"))
    if not files and (folder / "followers.json").exists():
        files = [folder / "followers.json"]  # older single-file exports
    if not files:
        sys.exit(f"Could not find followers_*.json in {folder}")
    accounts: dict[str, dict] = {}
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        # followers files are usually a top-level list
        blocks = data if isinstance(data, list) else data.get("relationships_followers", [])
        accounts.update(_extract_accounts(blocks, stats))
    return accounts


def load_accounts(folder: Path,
                  stats: ParseStats | None = None) -> tuple[dict[str, dict], dict[str, dict]]:
    """Load (following, followers) from an export folder."""
    return _load_following(folder, stats), _load_followers(folder, stats)


def load_pending_requests(folder: Path, stats: ParseStats | None = None) -> dict[str, dict]:
    """Load follow requests you've SENT that are still pending.

    Reads pending_follow_requests.json (private accounts you asked to follow
    that haven't accepted yet). This file is optional — accounts with no
    pending requests won't have it — so a missing file returns {} rather than
    erroring.
    """
    path = folder / "pending_follow_requests.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    # pending_follow_requests.json is a dict keyed by
    # "relationships_follow_requests_sent"; tolerate a bare list too.
    blocks = data if isinstance(data, list) else data.get("relationships_follow_requests_sent", [])
    return _extract_accounts(blocks, stats)


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #

@dataclass
class Diff:
    not_following_back: list[dict]        # you follow them, they don't follow you
    fans_you_dont_follow_back: list[dict]  # they follow you, you don't follow them
    mutuals: int


def format_date(timestamp) -> str:
    """Convert an IG epoch timestamp to YYYY-MM-DD, or 'unknown' if absent."""
    if not timestamp:  # None or 0 (IG uses 0 as a missing sentinel)
        return "unknown"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _to_record(username: str, info: dict) -> dict:
    timestamp = info.get("timestamp")
    return {
        "username": username,
        "profile_url": info.get("href") or f"https://instagram.com/{username}",
        "timestamp": timestamp,
        "followed_on": format_date(timestamp),
    }


def _sorted_records(usernames, source: dict[str, dict]) -> list[dict]:
    records = [_to_record(name, source[name]) for name in usernames]
    # Oldest follow first; unknown (no/zero timestamp) sorts last; ties by name.
    records.sort(key=lambda r: (not r["timestamp"], r["timestamp"] or 0, r["username"]))
    return records


def compute_diff(following: dict[str, dict], followers: dict[str, dict]) -> Diff:
    following_names = set(following)
    follower_names = set(followers)
    return Diff(
        not_following_back=_sorted_records(following_names - follower_names, following),
        fans_you_dont_follow_back=_sorted_records(follower_names - following_names, followers),
        mutuals=len(following_names & follower_names),
    )


# --------------------------------------------------------------------------- #
# Ignore list
#
# Dead links are the main source of friction in the action list: an account
# that was deleted, banned, or renamed still appears every single run, so the
# same handful of broken profiles gets re-triaged on every export. Recording
# them once in an ignore file keeps them out of future runs.
# --------------------------------------------------------------------------- #

IGNORE_FILENAME = "ignore.txt"


def load_ignore_list(path: Path) -> set[str]:
    """Read usernames to hide from the action list.

    One username per line; blank lines and `#` comments are skipped, a leading
    `@` is tolerated, and matching is case-insensitive. A missing file is not
    an error -- it just means nothing is ignored yet.
    """
    if not path.exists():
        return set()
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lstrip("@")
        if line:
            names.add(line.casefold())
    return names


def apply_ignore_list(records: list[dict], ignored: set[str]) -> tuple[list[dict], int]:
    """Drop ignored usernames from records, returning (kept, hidden_count)."""
    if not ignored:
        return records, 0
    kept = [r for r in records if r["username"].casefold() not in ignored]
    return kept, len(records) - len(kept)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

# Placeholder templating (rather than str.format) keeps the CSS and JS braces
# below readable -- no doubling every `{` in the stylesheet.
# The template is a raw string: the JS below contains "\n" escapes that must
# reach the browser intact rather than being turned into real newlines.
_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  body { font: 16px/1.5 -apple-system, system-ui, sans-serif; margin: 2rem auto; max-width: 720px; color: #222; padding: 0 1rem; }
  h1 { font-size: 1.4rem; margin-bottom: .25rem; }
  .meta { color: #666; margin-bottom: 1rem; }
  .bar { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem; }
  .bar input[type=search] { flex: 1 1 12rem; padding: .4rem .6rem; font-size: 1rem; border: 1px solid #ccc; border-radius: 6px; }
  button { font: inherit; padding: .35rem .7rem; border: 1px solid #ccc; background: #fff; border-radius: 6px; cursor: pointer; }
  button:hover { background: #f2f2f2; }
  #count { color: #666; font-variant-numeric: tabular-nums; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #eee; vertical-align: middle; }
  th { cursor: pointer; user-select: none; background: #fafafa; position: sticky; top: 0; }
  th:hover { background: #f0f0f0; }
  tr.done { opacity: .4; text-decoration: line-through; }
  tr.gone { opacity: .55; background: #fff8f0; }
  tr.gone .u { text-decoration: line-through wavy #d97706; }
  a { color: #0a66c2; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .find { font-size: .8rem; color: #888; margin-left: .5rem; }
  .dead { font-size: .8rem; padding: .15rem .45rem; }
  tr.gone .dead { background: #fde68a; border-color: #d97706; }
  .hint { color: #999; font-size: .85rem; }
  .note { background: #f7f7f7; border-left: 3px solid #ddd; padding: .6rem .8rem; margin: 1rem 0; font-size: .9rem; color: #555; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<p class="meta">__INTRO__</p>
<div class="note">Link says <em>“Sorry, this page isn’t available”</em>? The account was
deleted, banned, or renamed. Hit <strong>find</strong> to search for it, or mark it
<strong>dead</strong> and press <strong>Save ignore.txt</strong> — put that file next to the
script and those accounts stay out of future runs.</div>
<div class="bar">
  <input type="search" id="q" placeholder="Filter usernames…" autocomplete="off">
  <span id="count"></span>
  <button id="save">Save ignore.txt</button>
  <button id="reset">Clear marks</button>
</div>
<table id="t">
<thead>
<tr><th data-k="done">✓</th><th data-k="username">Username</th><th data-k="date">__DATE_LABEL__</th><th></th></tr>
</thead>
<tbody>
__ROWS__
</tbody>
</table>
<p class="hint">Click a column header to sort. Marks are saved in this browser.</p>
<script>
const KEY = '__STATE_KEY__';
const tbody = document.querySelector('#t tbody');
const rows = () => [...tbody.querySelectorAll('tr')];

// Triage marks survive a reload: a long list is never finished in one sitting.
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { state = {}; }
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };

function paint(tr) {
  const s = state[tr.dataset.username];
  tr.classList.toggle('done', s === 'done');
  tr.classList.toggle('gone', s === 'gone');
  tr.querySelector('input[type=checkbox]').checked = s === 'done';
  tr.dataset.done = s === 'done' ? '1' : '0';
}

function count() {
  const all = rows();
  const marked = all.filter(tr => state[tr.dataset.username]).length;
  document.querySelector('#count').textContent = marked + ' of ' + all.length + ' handled';
}

tbody.addEventListener('change', e => {
  if (e.target.type !== 'checkbox') return;
  const tr = e.target.closest('tr');
  if (e.target.checked) state[tr.dataset.username] = 'done';
  else delete state[tr.dataset.username];
  paint(tr); save(); count();
});

tbody.addEventListener('click', e => {
  if (!e.target.classList.contains('dead')) return;
  const tr = e.target.closest('tr');
  if (state[tr.dataset.username] === 'gone') delete state[tr.dataset.username];
  else state[tr.dataset.username] = 'gone';
  paint(tr); save(); count();
});

document.querySelector('#q').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  rows().forEach(tr => { tr.hidden = q && !tr.dataset.username.includes(q); });
});

document.querySelector('#save').addEventListener('click', () => {
  const names = Object.keys(state).sort();
  if (!names.length) { alert('Nothing marked yet.'); return; }
  const body = ['# Usernames hidden from future runs of ig_followback.py',
                '# Generated ' + new Date().toISOString().slice(0, 10),
                ...names].join('\n') + '\n';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([body], {type: 'text/plain'}));
  a.download = 'ignore.txt';
  a.click();
  URL.revokeObjectURL(a.href);
});

document.querySelector('#reset').addEventListener('click', () => {
  if (!confirm('Clear all marks on this list?')) return;
  state = {}; save(); rows().forEach(paint); count();
});

let asc = true, lastKey = 'date';
document.querySelectorAll('#t th').forEach(th => th.addEventListener('click', () => {
  const k = th.dataset.k;
  if (!k) return;
  asc = k === lastKey ? !asc : true;
  lastKey = k;
  rows().sort((a, b) => {
    const va = a.dataset[k] || '', vb = b.dataset[k] || '';
    return (va > vb ? 1 : va < vb ? -1 : 0) * (asc ? 1 : -1);
  }).forEach(r => tbody.appendChild(r));
}));

rows().forEach(paint); count();
</script>
</body>
</html>
"""


def _search_url(username: str) -> str:
    """Instagram search for a username.

    The fallback when a direct profile link 404s: a renamed account still
    exists, and searching is the fastest way to find where it went.
    """
    return ("https://www.instagram.com/explore/search/keyword/?q="
            + quote(username, safe=""))


def _render_action_list(title: str, intro: str, records: list[dict],
                        date_label: str = "Followed on") -> str:
    """Render a sortable, checkbox action list for any set of account records."""
    rows = []
    for acc in records:
        user = html.escape(acc["username"])
        url = html.escape(acc["profile_url"], quote=True)
        find = html.escape(_search_url(acc["username"]), quote=True)
        # 'unknown' sorts after real dates as a data attribute too (~ > digits).
        sort_date = acc["followed_on"] if acc["followed_on"] != "unknown" else "~"
        rows.append(
            f'<tr data-username="{user}" data-date="{html.escape(sort_date)}" data-done="0">'
            f'<td><input type="checkbox"></td>'
            f'<td><a class="u" href="{url}" target="_blank" rel="noopener">{user}</a>'
            f'<a class="find" href="{find}" target="_blank" rel="noopener">find</a></td>'
            f'<td>{html.escape(acc["followed_on"])}</td>'
            f'<td><button class="dead" type="button" title="Deleted, banned or renamed">dead</button></td>'
            f'</tr>'
        )
    # A per-list key so the two action lists don't share triage marks.
    state_key = "ig_followback:" + re.sub(r"[^a-z0-9]+", "_", title.casefold()).strip("_")
    replacements = {
        "__TITLE__": html.escape(title),
        "__INTRO__": html.escape(intro),
        "__DATE_LABEL__": html.escape(date_label),
        "__STATE_KEY__": html.escape(state_key, quote=True),
        "__ROWS__": "\n".join(rows),
    }
    out = _HTML_TEMPLATE
    for placeholder, value in replacements.items():
        out = out.replace(placeholder, value)
    return out


def _write_list(records: list[dict], html_path: Path, csv_path: Path, txt_path: Path,
                title: str, intro: str, date_label: str) -> list[Path]:
    """Write an HTML action list plus CSV and TXT for a set of account records."""
    date_field = date_label.lower().replace(" ", "_")  # "Followed on" -> "followed_on"

    html_path.write_text(
        _render_action_list(title, intro, records, date_label), encoding="utf-8"
    )
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["username", "profile_url", date_field])
        for acc in records:
            writer.writerow([acc["username"], acc["profile_url"], acc["followed_on"]])
    txt_path.write_text(
        "\n".join(acc["username"] for acc in records), encoding="utf-8"
    )
    return [html_path, csv_path, txt_path]


def write_outputs(candidates: list[dict], pending: list[dict], out_dir: Path) -> list[Path]:
    """Write the unfollow action list and, if any, the pending-requests list.

    Files are written into out_dir (the directory the script is run from),
    not the export folder, so running the tool doesn't litter your export.
    Returns the paths written.
    """
    written = _write_list(
        candidates,
        out_dir / "cleanup.html",
        out_dir / "not_following_back.csv",
        out_dir / "not_following_back.txt",
        title="Not following you back",
        intro=f"{len(candidates)} accounts you follow who don't follow you back. "
              "Tick the box once you've unfollowed someone in the app.",
        date_label="Followed on",
    )
    if pending:
        written += _write_list(
            pending,
            out_dir / "pending_requests.html",
            out_dir / "pending_requests.csv",
            out_dir / "pending_requests.txt",
            title="Pending sent follow requests",
            intro=f"{len(pending)} follow requests you've sent that haven't been "
                  "accepted yet. Open a profile to cancel the request if you want; "
                  "tick the box to mark it handled.",
            date_label="Requested on",
        )
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ig_followback.py",
        description="Find who you follow on Instagram that doesn't follow you back, "
                    "offline, from your data export.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Get your data: Instagram > Accounts Center > Your information and "
               "permissions > Export your information (scope: Followers and "
               "following, format: JSON). Unzip it and point this script at "
               "connections/followers_and_following/.",
    )
    parser.add_argument(
        "folder", nargs="?", default=".", type=Path,
        help="the connections/followers_and_following folder (default: current directory)",
    )
    parser.add_argument(
        "--ignore", metavar="FILE", type=Path, default=None,
        help=f"usernames to hide from the action list, one per line "
             f"(default: {IGNORE_FILENAME} in the current directory, if present)",
    )
    parser.add_argument(
        "--no-ignore", action="store_true",
        help="show every account, even ones listed in the ignore file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    folder = args.folder
    out_dir = Path.cwd()

    stats = ParseStats()
    following, followers = load_accounts(folder, stats)
    diff = compute_diff(following, followers)
    candidates = diff.not_following_back
    pending_accounts = load_pending_requests(folder, stats)
    pending = _sorted_records(set(pending_accounts), pending_accounts)

    ignore_path = args.ignore or (out_dir / IGNORE_FILENAME)
    ignored = set() if args.no_ignore else load_ignore_list(ignore_path)
    candidates, hidden = apply_ignore_list(candidates, ignored)
    pending, pending_hidden = apply_ignore_list(pending, ignored)

    print(f"Following:             {len(following)}")
    print(f"Followers:             {len(followers)}")
    print(f"Mutuals:               {diff.mutuals}")
    print(f"Don't follow you back: {len(candidates)}")
    print(f"You don't follow back: {len(diff.fans_you_dont_follow_back)}")
    print(f"Pending sent requests: {len(pending)}")
    if hidden or pending_hidden:
        print(f"Hidden by {ignore_path.name}:   {hidden + pending_hidden}")
    print()

    print("=== Not following you back (candidates to unfollow) ===")
    for acc in candidates:
        print(f"  {acc['username']:<30} followed {acc['followed_on']}  {acc['profile_url']}")

    if pending:
        print("\n=== Pending follow requests you've sent (awaiting acceptance) ===")
        for acc in pending:
            print(f"  {acc['username']:<30} requested {acc['followed_on']}  {acc['profile_url']}")

    written = write_outputs(candidates, pending, out_dir)
    print(f"\nSaved {len(candidates)} unfollow candidates"
          + (f" and {len(pending)} pending requests" if pending else "")
          + ". Open the action list(s):")
    for path in written:
        print(f"  {path}")
    if stats.total:
        print(f"\nParsing notes for this export:")
        if stats.skipped:
            one = stats.skipped == 1
            print(f"  {stats.skipped} entr{'y' if one else 'ies'} had no readable "
                  f"username and {'was' if one else 'were'} left out.")
        if stats.suspicious:
            count, one = len(stats.suspicious), len(stats.suspicious) == 1
            shown = ", ".join(stats.suspicious[:5])
            more = f" (+{count - 5} more)" if count > 5 else ""
            print(f"  {count} entr{'y' if one else 'ies'} did not look like a username, "
                  f"so {'its link' if one else 'their links'} will not resolve: "
                  f"{shown}{more}")

    print(f"\nBroken profile link? The account was most likely deleted, deactivated or"
          f"\nbanned before this export was generated -- Instagram keeps those in your"
          f"\nfollowing list. Use the 'find' link to search for it, or mark it 'dead'"
          f"\nand save {IGNORE_FILENAME} next to this script to keep it out of future runs.")


if __name__ == "__main__":
    main()
