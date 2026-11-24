#!/usr/bin/env python3
"""
Instagram follow-back cleanup (personal, offline).

Reads the .zip from Instagram's "Export your information" (scope: Followers and
following, format: JSON) and helps you clean up who you follow. It reports:
  - people you follow who DON'T follow you back  (cleanup candidates)
  - people who follow you that you DON'T follow back
  - pending follow requests you've sent, mutuals, close friends
  - with --since OLD.zip: who stopped following you, and who started

and writes clickable action lists (HTML, plus CSV and TXT) into the folder you
run it from. Accounts marked dead or keep there can be saved to ignore.txt to
keep them out of future runs.

No login, no network, no API. Everything runs locally on the export files.
Automated unfollowing is intentionally NOT included: it violates Instagram's
Terms of Service and risks action against your account.

Usage:
    python3 ig_followback.py instagram-yourname-2026-09-08.zip
    python3 ig_followback.py new.zip --since old.zip
Also accepts the unzipped export, or its connections/followers_and_following
folder. --help lists the options.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


class ExportError(Exception):
    """The export could not be located or read. Carries a message for the user."""


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

    Returns a mapping casefolded_username -> {"href", "timestamp", "display"}.

    Keys are casefolded because following.json and followers_*.json are matched
    against each other by key: a casing difference between the two files would
    otherwise invent a "doesn't follow you back" and lose a mutual. "display"
    keeps the username as Instagram wrote it, for showing and for links.

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
        accounts[username.casefold()] = {
            "href": f"https://instagram.com/{username}",
            "timestamp": timestamp,
            "display": username,
        }
    return accounts


class _ExportSource:
    """The export's JSON files, read from a folder or straight out of a .zip.

    Instagram delivers a zip whose files sit three levels down, under
    connections/followers_and_following/. Rather than make that the user's
    problem, this resolves a zip, an unzipped export root, or the folder
    itself to the same interface.
    """

    def __init__(self, label: str, names: dict[str, object], read):
        self.label = label       # what to name in an error message
        self._names = names      # basename -> key the reader understands
        self._read = read        # key -> bytes

    def exists(self, name: str) -> bool:
        return name in self._names

    def glob(self, pattern: str) -> list[str]:
        return sorted(n for n in self._names if fnmatch(n, pattern))

    def read_json(self, name: str):
        try:
            return json.loads(self._read(self._names[name]).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            raise ExportError(f"{name} in {self.label} is not readable JSON: {err}")


_ANCHORS = ("following.json", "following.html")


def _zip_source(path: Path) -> _ExportSource:
    archive = zipfile.ZipFile(path)
    members = [m for m in archive.namelist() if not m.endswith("/")]
    # Anchor on following.json so the export folder is found wherever it sits;
    # following.html finds an HTML-format export, so it can be named as such.
    anchor_member = next(
        (m for anchor in _ANCHORS for m in members if m.rsplit("/", 1)[-1] == anchor), None)
    prefix = anchor_member.rsplit("/", 1)[0] + "/" if anchor_member and "/" in anchor_member else ""
    names = {m[len(prefix):]: m for m in members
             if m.startswith(prefix) and "/" not in m[len(prefix):]}
    return _ExportSource(path.name, names, archive.read)


def _dir_source(path: Path) -> _ExportSource:
    folder = path
    if not (folder / "following.json").exists():
        # Point at the unzipped export root and we'll find the folder inside.
        for anchor in _ANCHORS:
            found = sorted(path.rglob(anchor), key=lambda q: len(q.parts))
            if found:
                folder = found[0].parent
                break
    names = {q.name: q for q in folder.iterdir() if q.is_file()}
    return _ExportSource(str(folder), names, lambda q: q.read_bytes())


def open_export(path: Path) -> _ExportSource:
    """Resolve a .zip, an export root, or the export folder to one interface."""
    if not path.exists():
        raise ExportError(f"No such file or folder: {path}")
    if path.is_file():
        if not zipfile.is_zipfile(path):
            raise ExportError(f"{path} is not a folder or a .zip archive")
        return _zip_source(path)
    return _dir_source(path)


def _as_source(folder) -> _ExportSource:
    """Accept a path or an already-open source, so callers can pass either."""
    return folder if isinstance(folder, _ExportSource) else open_export(Path(folder))


def _load_following(source: _ExportSource, stats: ParseStats | None = None) -> dict[str, dict]:
    if not source.exists("following.json") and source.exists("following.html"):
        raise ExportError(
            f"{source.label} is an HTML-format export, which can't be read reliably. "
            f"Request a new export and choose Format: JSON.")
    if not source.exists("following.json"):
        raise ExportError(f"Could not find following.json in {source.label}")
    data = source.read_json("following.json")
    # following.json is a dict keyed by "relationships_following"
    blocks = data["relationships_following"] if isinstance(data, dict) else data
    return _extract_accounts(blocks, stats)


def _load_followers(source: _ExportSource, stats: ParseStats | None = None) -> dict[str, dict]:
    # Instagram splits large follower lists: followers_1.json, followers_2.json, ...
    files = source.glob("followers_*.json")
    if not files and source.exists("followers.json"):
        files = ["followers.json"]  # older single-file exports
    if not files:
        raise ExportError(f"Could not find followers_*.json in {source.label}")
    accounts: dict[str, dict] = {}
    for name in files:
        data = source.read_json(name)
        # followers files are usually a top-level list
        blocks = data if isinstance(data, list) else data.get("relationships_followers", [])
        accounts.update(_extract_accounts(blocks, stats))
    return accounts


def load_accounts(folder,
                  stats: ParseStats | None = None) -> tuple[dict[str, dict], dict[str, dict]]:
    """Load (following, followers) from an export folder, root, or .zip."""
    source = _as_source(folder)
    return _load_following(source, stats), _load_followers(source, stats)


def load_pending_requests(folder, stats: ParseStats | None = None) -> dict[str, dict]:
    """Load follow requests you've SENT that are still pending.

    Reads pending_follow_requests.json (private accounts you asked to follow
    that haven't accepted yet). This file is optional — accounts with no
    pending requests won't have it — so a missing file returns {} rather than
    erroring.
    """
    source = _as_source(folder)
    if not source.exists("pending_follow_requests.json"):
        return {}
    data = source.read_json("pending_follow_requests.json")
    # pending_follow_requests.json is a dict keyed by
    # "relationships_follow_requests_sent"; tolerate a bare list too.
    blocks = data if isinstance(data, list) else data.get("relationships_follow_requests_sent", [])
    return _extract_accounts(blocks, stats)


# Optional lists that ship in the same folder of a "Followers and following"
# export. Each is a dict holding one "relationships_*" list, or a bare list.
CLOSE_FRIENDS_FILE = "close_friends.json"
RECENTLY_UNFOLLOWED_FILE = "recently_unfollowed_profiles.json"
BLOCKED_FILE = "blocked_profiles.json"
RESTRICTED_FILE = "restricted_profiles.json"


def _blocks_from(data) -> list:
    """The relationship blocks in an export file, whatever its top-level key."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def load_optional_list(folder, filename: str) -> dict[str, dict]:
    """Load one of the optional relationship lists, or {} if it isn't there.

    These files only exist when they have something in them (no close friends,
    no close_friends.json), so absence is normal and never an error. Their
    anomalies aren't counted in ParseStats: the rows they produce are
    flags and counts, not links the user is asked to open.
    """
    source = _as_source(folder)
    if not source.exists(filename):
        return {}
    return _extract_accounts(_blocks_from(source.read_json(filename)))


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
    # Keys are casefolded; show the username the way Instagram spells it.
    username = info.get("display") or username
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


@dataclass
class Changes:
    """What moved between an older export and a newer one."""
    lost_followers: list[dict]  # followed you before, don't now
    new_followers: list[dict]   # follow you now, didn't before
    you_unfollowed: list[dict]  # you followed them before, don't now
    you_followed: list[dict]    # you follow them now, didn't before


def compare_exports(old_following: dict[str, dict], old_followers: dict[str, dict],
                    new_following: dict[str, dict], new_followers: dict[str, dict]) -> Changes:
    """Diff two exports of the same account.

    A lost follower may also be an account that was deactivated or deleted in
    between: the export carries no account ID, so the two look identical.
    """
    return Changes(
        lost_followers=_sorted_records(set(old_followers) - set(new_followers), old_followers),
        new_followers=_sorted_records(set(new_followers) - set(old_followers), new_followers),
        you_unfollowed=_sorted_records(set(old_following) - set(new_following), old_following),
        you_followed=_sorted_records(set(new_following) - set(old_following), new_following),
    )


def latest_timestamp(*account_maps: dict[str, dict]) -> int:
    """The most recent follow timestamp across maps -- a proxy for export age."""
    return max((info.get("timestamp") or 0
                for accounts in account_maps for info in accounts.values()), default=0)


def flag_close_friends(records: list[dict], close_friends: dict[str, dict]) -> int:
    """Mark records that are on your Close Friends list; returns how many."""
    flagged = 0
    for record in records:
        record["close_friend"] = record["username"].casefold() in close_friends
        flagged += record["close_friend"]
    return flagged


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
  .dead, .copy, .keep { font-size: .8rem; padding: .15rem .45rem; margin-right: .25rem; }
  tr.gone .dead { background: #fde68a; border-color: #d97706; }
  tr.kept { opacity: .55; background: #f0f7ff; }
  tr.kept .keep { background: #bfdbfe; border-color: #2563eb; }
  .tag { font-size: .7rem; background: #dcfce7; color: #166534; border-radius: 4px; padding: .05rem .35rem; margin-left: .4rem; white-space: nowrap; }
  body.plain .dead, body.plain .keep, body.plain #save, body.plain .note { display: none; }
  .hint { color: #999; font-size: .85rem; }
  .note { background: #f7f7f7; border-left: 3px solid #ddd; padding: .6rem .8rem; margin: 1rem 0; font-size: .9rem; color: #555; }
</style>
</head>
<body class="__BODY_CLASS__">
<h1>__TITLE__</h1>
<p class="meta">__INTRO__</p>
<div class="note">Link says <em>“Sorry, this page isn’t available”</em>? The account was
deleted, banned, or renamed. Hit <strong>find</strong> to search for it, or mark it
<strong>dead</strong>. Following someone on purpose who will never follow back (a brand, an
artist, a news account)? Mark it <strong>keep</strong>. Then press <strong>Save ignore.txt</strong>
and put that file in the folder you run the script from: those accounts stay out of
future runs. Rows tagged <span class="tag">close friend</span> are on your Close Friends list.</div>
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
// Usernames already in the ignore file this list was generated with. They are
// hidden from the table, so Save has to carry them over or they'd come back.
const PRIOR = __PRIOR__;
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
  tr.classList.toggle('kept', s === 'keep');
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

// A dead account has no profile page to open, so the way to unfollow it is to
// paste its username into the search box of your own Following list.
async function copyUsername(btn) {
  const name = btn.closest('tr').dataset.username;
  let ok = false;
  try {
    await navigator.clipboard.writeText(name);
    ok = true;
  } catch (err) {
    // The async clipboard API needs a secure context, which a file:// page is
    // not in every browser; fall back to the old selection-based copy.
    try {
      const ta = document.createElement('textarea');
      ta.value = name;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (err2) {
      ok = false;
    }
  }
  btn.textContent = ok ? 'copied' : 'failed';
  setTimeout(() => { btn.textContent = 'copy'; }, 1200);
}

tbody.addEventListener('click', e => {
  if (e.target.classList.contains('copy')) { copyUsername(e.target); return; }
  const mark = e.target.classList.contains('dead') ? 'gone'
             : e.target.classList.contains('keep') ? 'keep' : null;
  if (!mark) return;
  const tr = e.target.closest('tr');
  if (state[tr.dataset.username] === mark) delete state[tr.dataset.username];
  else state[tr.dataset.username] = mark;
  paint(tr); save(); count();
});

document.querySelector('#q').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  rows().forEach(tr => { tr.hidden = q && !tr.dataset.username.toLowerCase().includes(q); });
});

document.querySelector('#save').addEventListener('click', () => {
  const marked = Object.keys(state);
  if (!marked.length && !PRIOR.length) { alert('Nothing marked yet.'); return; }
  const seen = new Set();
  const section = (heading, names) => {
    const fresh = names.filter(n => !seen.has(n.toLowerCase())).sort();
    fresh.forEach(n => seen.add(n.toLowerCase()));
    return fresh.length ? ['', '# ' + heading, ...fresh] : [];
  };
  const by = mark => marked.filter(n => state[n] === mark);
  const body = ['# Usernames hidden from future runs of ig_followback.py',
                '# Generated ' + new Date().toISOString().slice(0, 10),
                ...section('Already in your ignore list', PRIOR),
                ...section('Dead: deleted, banned or renamed', by('gone')),
                ...section('Keep: following on purpose', by('keep')),
                ...section('Handled', by('done'))].join('\n') + '\n';
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
                        date_label: str = "Followed on", prior: list[str] | None = None,
                        triage: bool = True) -> str:
    """Render a sortable, checkbox action list for any set of account records.

    prior: usernames already in the ignore file, carried into its next save.
    triage: False for read-only reports (changes between exports), which hides
            the dead/keep/ignore-file tools that only make sense for cleanup.
    """
    rows = []
    for acc in records:
        user = html.escape(acc["username"])
        url = html.escape(acc["profile_url"], quote=True)
        find = html.escape(_search_url(acc["username"]), quote=True)
        # 'unknown' sorts after real dates as a data attribute too (~ > digits).
        sort_date = acc["followed_on"] if acc["followed_on"] != "unknown" else "~"
        tag = ('<span class="tag" title="On your Close Friends list">close friend</span>'
               if acc.get("close_friend") else "")
        rows.append(
            f'<tr data-username="{user}" data-date="{html.escape(sort_date)}" data-done="0">'
            f'<td><input type="checkbox"></td>'
            f'<td><a class="u" href="{url}" target="_blank" rel="noopener">{user}</a>'
            f'<a class="find" href="{find}" target="_blank" rel="noopener">find</a>{tag}</td>'
            f'<td>{html.escape(acc["followed_on"])}</td>'
            f'<td><button class="copy" type="button" title="Copy username, to paste into your Following list search">copy</button>'
            f'<button class="dead" type="button" title="Deleted, banned or renamed">dead</button>'
            f'<button class="keep" type="button" title="Following on purpose: hide from future runs">keep</button></td>'
            f'</tr>'
        )
    # A per-list key so the two action lists don't share triage marks.
    state_key = "ig_followback:" + re.sub(r"[^a-z0-9]+", "_", title.casefold()).strip("_")
    replacements = {
        "__TITLE__": html.escape(title),
        "__INTRO__": html.escape(intro),
        "__DATE_LABEL__": html.escape(date_label),
        "__STATE_KEY__": html.escape(state_key, quote=True),
        "__BODY_CLASS__": "" if triage else "plain",
        # JSON inside <script>: escape "<" so no value can close the tag.
        "__PRIOR__": json.dumps(sorted(prior or [])).replace("<", "\\u003c"),
        "__ROWS__": "\n".join(rows),
    }
    # One pass over the template, so a substituted value -- a username can
    # legally be "__ROWS__" -- is never itself treated as a placeholder.
    pattern = re.compile("|".join(map(re.escape, replacements)))
    return pattern.sub(lambda m: replacements[m.group(0)], _HTML_TEMPLATE)


def _write_list(records: list[dict], html_path: Path, csv_path: Path, txt_path: Path,
                title: str, intro: str, date_label: str, prior: list[str] | None = None,
                triage: bool = True) -> list[Path]:
    """Write an HTML action list plus CSV and TXT for a set of account records."""
    date_field = date_label.lower().replace(" ", "_")  # "Followed on" -> "followed_on"

    html_path.write_text(
        _render_action_list(title, intro, records, date_label, prior, triage),
        encoding="utf-8"
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


def write_outputs(candidates: list[dict], pending: list[dict], out_dir: Path,
                  fans: list[dict] | None = None, prior: list[str] | None = None,
                  recently_unfollowed: list[dict] | None = None,
                  changes: Changes | None = None) -> list[Path]:
    """Write the unfollow action list, plus every other list that has content.

    Files are written into out_dir (the directory the script is run from),
    not the export folder, so running the tool doesn't litter your export.
    prior is the ignore list in effect, carried into the next save of it.
    Returns the paths written.
    """
    def out(stem):
        return out_dir / f"{stem}.html", out_dir / f"{stem}.csv", out_dir / f"{stem}.txt"

    html_path, csv_path, txt_path = out_dir / "cleanup.html", *out("not_following_back")[1:]
    flagged = sum(1 for r in candidates if r.get("close_friend"))
    written = _write_list(
        candidates, html_path, csv_path, txt_path,
        title="Not following you back",
        intro=f"{len(candidates)} accounts you follow who don't follow you back. "
              "Tick the box once you've unfollowed someone in the app."
              + (f" {flagged} of them are on your Close Friends list." if flagged else ""),
        date_label="Followed on", prior=prior,
    )
    if pending:
        written += _write_list(
            pending, *out("pending_requests"),
            title="Pending sent follow requests",
            intro=f"{len(pending)} follow requests you've sent that haven't been "
                  "accepted yet. Open a profile to cancel the request if you want; "
                  "tick the box to mark it handled.",
            date_label="Requested on", prior=prior,
        )
    if fans:
        written += _write_list(
            fans, *out("you_dont_follow_back"),
            title="You don't follow them back",
            intro=f"{len(fans)} accounts that follow you but you don't follow "
                  "back. Tick the box once you've decided about someone.",
            date_label="Followed you on", prior=prior,
        )
    if recently_unfollowed:
        written += _write_list(
            recently_unfollowed, *out("recently_unfollowed"),
            title="Recently unfollowed",
            intro=f"{len(recently_unfollowed)} accounts Instagram lists as recently "
                  "unfollowed by you -- the cleanup you've already done.",
            date_label="Unfollowed on", triage=False,
        )
    if changes is not None:
        # Always written when comparing, even empty, so a stale file from an
        # earlier comparison never passes for this one.
        written += _write_list(
            changes.lost_followers, *out("unfollowed_you"),
            title="Stopped following you",
            intro=f"{len(changes.lost_followers)} accounts followed you in the older "
                  "export and don't in this one. Some may have deactivated or deleted "
                  "their account instead -- an export can't tell the two apart.",
            date_label="Followed you on", triage=False,
        )
        written += _write_list(
            changes.new_followers, *out("new_followers"),
            title="New followers",
            intro=f"{len(changes.new_followers)} accounts follow you now that didn't "
                  "in the older export.",
            date_label="Followed you on", triage=False,
        )
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

_EXPORT_HELP = (
    "Request one from Instagram: Accounts Center > Your information and\n"
    "permissions > Export your information, with scope 'Followers and\n"
    "following' and format JSON. Then point this script at the .zip it\n"
    "gives you, or at any folder inside it."
)

# Rows printed per section before pointing at the HTML list instead.
PRINT_LIMIT = 20


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find who you follow on Instagram that doesn't follow you back, "
                    "offline, from your data export.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Get your data: Instagram > Accounts Center > Your information and "
               "permissions > Export your information (scope: Followers and "
               "following, format: JSON). Point this script at the .zip it gives "
               "you -- no need to unzip it first.",
    )
    parser.add_argument(
        "folder", nargs="?", default=".", type=Path, metavar="EXPORT",
        help="the export .zip, the folder it unzips to, or the "
             "connections/followers_and_following folder itself "
             "(default: current directory)",
    )
    parser.add_argument(
        "--since", metavar="OLD_EXPORT", type=Path, default=None,
        help="an older export of the same account: also report who stopped "
             "following you and who started, between the two",
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
    parser.add_argument(
        "--print-all", action="store_true",
        help=f"print every account in the terminal, not just the first {PRINT_LIMIT} "
             f"of each list",
    )
    return parser.parse_args(argv)


def _print_section(heading: str, records: list[dict], verb: str, limit: int | None,
                   more_in: str) -> None:
    print(f"\n=== {heading} ===")
    if not records:
        print("  (none)")
        return
    for acc in records[:limit]:
        tag = "  [close friend]" if acc.get("close_friend") else ""
        print(f"  {acc['username']:<30} {verb} {acc['followed_on']}  {acc['profile_url']}{tag}")
    if limit is not None and len(records) > limit:
        print(f"  ... and {len(records) - limit} more -- see {more_in}, "
              f"or pass --print-all")


def _load_export(path: Path, stats: ParseStats | None):
    source = open_export(path)  # opened once and shared, so a zip isn't reopened
    following, followers = load_accounts(source, stats)
    return source, following, followers


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    out_dir = Path.cwd()
    limit = None if args.print_all else PRINT_LIMIT

    stats = ParseStats()
    try:
        source, following, followers = _load_export(args.folder, stats)
        pending_accounts = load_pending_requests(source, stats)
        close_friends = load_optional_list(source, CLOSE_FRIENDS_FILE)
        recent = load_optional_list(source, RECENTLY_UNFOLLOWED_FILE)
        blocked = load_optional_list(source, BLOCKED_FILE)
        restricted = load_optional_list(source, RESTRICTED_FILE)
        old = _load_export(args.since, None) if args.since else None
    except ExportError as err:
        sys.exit(f"{err}\n\n{_EXPORT_HELP}")
    diff = compute_diff(following, followers)
    candidates = diff.not_following_back
    pending = _sorted_records(set(pending_accounts), pending_accounts)
    recently_unfollowed = _sorted_records(set(recent), recent)

    ignore_path = args.ignore or (out_dir / IGNORE_FILENAME)
    ignored = set() if args.no_ignore else load_ignore_list(ignore_path)
    candidates, hidden = apply_ignore_list(candidates, ignored)
    pending, pending_hidden = apply_ignore_list(pending, ignored)
    fans, fans_hidden = apply_ignore_list(diff.fans_you_dont_follow_back, ignored)
    close_not_back = flag_close_friends(candidates, close_friends)
    flag_close_friends(fans, close_friends)

    changes = None
    if old is not None:
        _, old_following, old_followers = old
        changes = compare_exports(old_following, old_followers, following, followers)
        flag_close_friends(changes.lost_followers, close_friends)

    print(f"Following:             {len(following)}")
    print(f"Followers:             {len(followers)}")
    print(f"Mutuals:               {diff.mutuals}")
    print(f"Don't follow you back: {len(candidates)}"
          + (f"  ({close_not_back} on your Close Friends list)" if close_not_back else ""))
    print(f"You don't follow back: {len(fans)}")
    print(f"Pending sent requests: {len(pending)}")
    if close_friends:
        print(f"Close friends:         {len(close_friends)}")
    if recent:
        print(f"Recently unfollowed:   {len(recent)}")
    if blocked or restricted:
        print(f"Blocked / restricted:  {len(blocked)} / {len(restricted)}")
    if hidden or pending_hidden or fans_hidden:
        print(f"Hidden by {ignore_path.name}:   {hidden + pending_hidden + fans_hidden}")

    _print_section("Not following you back (candidates to unfollow)", candidates,
                   "followed", limit, "cleanup.html")
    if pending:
        _print_section("Pending follow requests you've sent (awaiting acceptance)",
                       pending, "requested", limit, "pending_requests.html")

    if changes is not None:
        print(f"\n=== Changes since {args.since.name} ===")
        print(f"Stopped following you: {len(changes.lost_followers)}")
        print(f"New followers:         {len(changes.new_followers)}")
        print(f"You unfollowed:        {len(changes.you_unfollowed)}")
        print(f"You started following: {len(changes.you_followed)}")
        if latest_timestamp(*old[1:]) > latest_timestamp(following, followers):
            print(f"  Note: {args.since.name} has more recent follows than "
                  f"{args.folder.name}. Did you swap them? The older export goes "
                  f"after --since.")
        _print_section("Stopped following you", changes.lost_followers,
                       "followed you", limit, "unfollowed_you.html")

    written = write_outputs(candidates, pending, out_dir, fans,
                            prior=sorted(ignored), recently_unfollowed=recently_unfollowed,
                            changes=changes)
    print(f"\nSaved {len(candidates)} unfollow candidates"
          + (f" and {len(pending)} pending requests" if pending else "")
          + ". Open the action list(s):")
    for path in written:
        if path.suffix == ".html":
            print(f"  {path}")
    print("  (each also has a .csv and a .txt next to it)")
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
          f"\n(or 'keep' for accounts you follow on purpose) and save {IGNORE_FILENAME}"
          f"\nin this folder to keep them out of future runs.")


if __name__ == "__main__":
    main()
