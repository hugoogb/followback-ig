# followbackinig

Find the people you follow on Instagram who **don't follow you back**, fully
offline, so you can clean up your account. No login, no network, no API — it
reads the JSON files from Instagram's data export and produces a clickable
action list for fast manual unfollowing.

> Automated unfollowing is intentionally **not** included: it violates
> Instagram's Terms of Service and risks action against your account.

## Get your data

Instagram → **Accounts Center → Your information and permissions → Export your
information** → request an export with:

- **Scope:** *Followers and following*
- **Format:** *JSON*

Unzip it and locate the `connections/followers_and_following/` folder. It
contains `following.json` and one or more `followers_*.json` files.

## Usage

```bash
python3 ig_followback.py /path/to/connections/followers_and_following
```

Or drop `ig_followback.py` into that folder and run it with no arguments.
`--help` lists the options.

It prints a summary and writes these files into the export folder:

| File | What it is |
|------|-----------|
| `cleanup.html` | Action list — clickable profile links + checkboxes, sortable by follow date (oldest first, so stale follows surface) |
| `not_following_back.csv` | `username, profile_url, followed_on` for records/scripting |
| `not_following_back.txt` | Bare usernames |
| `pending_requests.html` | Action list of follow requests you've **sent** that haven't been accepted yet — clickable links + checkboxes, sortable by request date |
| `pending_requests.csv` | `username, profile_url, requested_on` |
| `pending_requests.txt` | Bare usernames |

The `pending_requests.*` files are only written when your export contains a
`pending_follow_requests.json` (i.e. you have outstanding requests to private
accounts).

Open `cleanup.html` in your browser, click each profile link to unfollow in the
Instagram app, and tick the box to mark it done. Use `pending_requests.html` the
same way to review and cancel stale requests.

Your ticks are saved in the browser, so closing the tab part-way through a long
list doesn't lose your progress, and a counter shows how many you've handled.
There's also a filter box for jumping to a specific username.

## Broken profile links

Some links will land on *"Sorry, this page isn't available."* Almost always the
account was already gone **before** Instagram generated the export — deactivated,
deleted, or banned. Instagram keeps such accounts in your following list (they
still count towards your following total), so the export lists them faithfully
and their profile URLs simply don't resolve.

Renames are a much rarer cause: the export records each username as of the
moment the file was generated, so unless you leave the export sitting around for
weeks before running the script, a name has to change in that short window to
break. The export carries no account ID, so there is no offline way to tell a
deleted account from a renamed one.

Accounts that were deactivated or banned generally **can't be unfollowed from
their profile** — there's no profile left to open. Unfollow them from your own
following list in the app, or just mark them dead and move on.

Each row therefore has two extra affordances:

- **find** — searches Instagram for the username. This is the one that recovers
  a renamed account, which a direct link can never do.
- **dead** — marks the row as gone. It stays marked across reloads.

Once you've marked the dead ones, press **Save ignore.txt** and put the
downloaded file next to the script. Those accounts are then hidden from every
future run, so you never re-triage the same broken links after your next export:

```bash
python3 ig_followback.py <export folder>              # honours ./ignore.txt
python3 ig_followback.py <export folder> --ignore mine.txt
python3 ig_followback.py <export folder> --no-ignore  # show everything again
```

`ignore.txt` is one username per line; blank lines and `#` comments are
skipped, a leading `@` is fine, and matching is case-insensitive.

> **Why not just check the links automatically?** Instagram's official Graph API
> returns a follower *count*, never the list, and only for Business accounts.
> Checking each profile would mean either scraping Instagram while logged out
> (which mostly returns a login wall) or using the private mobile API with your
> password — which violates Instagram's Terms of Service and risks your account.
> Neither belongs in a tool whose whole point is that it runs offline.

## Output categories

- **Not following you back** — you follow them, they don't follow you (cleanup candidates)
- **You don't follow back** — they follow you, you don't follow them
- **Mutuals** — count of accounts following each other
- **Pending sent requests** — follow requests you've sent to private accounts that haven't been accepted yet

## Privacy

Everything runs locally. Your export files and the generated lists are
git-ignored so your personal data never leaves your machine.

## Development

```bash
python3 -m unittest test_ig_followback -v
```

Pure stdlib — no dependencies to install.
