# followbackinig

Find the people you follow on Instagram who **don't follow you back**, fully
offline, so you can clean up your account. No login, no network, no API — it
reads Instagram's data export and produces a clickable action list for fast
manual unfollowing.

> Automated unfollowing is intentionally **not** included: it violates
> Instagram's Terms of Service and risks action against your account.

## Get your data

Instagram → **Accounts Center → Your information and permissions → Export your
information** → request an export with:

- **Scope:** *Followers and following*
- **Format:** *JSON*

Instagram emails you a `.zip` when it's ready. **Download it and you're done —
there is nothing to unzip and no folder to go looking for.**

## Usage

```bash
python3 ig_followback.py instagram-yourname-2026-09-08.zip
```

That's the whole workflow. The script opens the archive, finds the
`connections/followers_and_following/` data inside it wherever it happens to
sit, and reads it in place.

If you'd rather unzip it anyway, it takes that too — point it at the export
root or at the data folder itself, and all three behave identically:

```bash
python3 ig_followback.py /path/to/unzipped-export/
python3 ig_followback.py /path/to/connections/followers_and_following
python3 ig_followback.py            # or run it from inside that folder
```

`--help` lists the options.

## What it writes

A summary on stdout, plus these files **in the directory you run it from** —
never inside your export:

| File | What it is |
|------|-----------|
| `cleanup.html` | The main action list — profile links, checkboxes, sortable by follow date (oldest first, so stale follows surface) |
| `not_following_back.csv` | `username, profile_url, followed_on` for records/scripting |
| `not_following_back.txt` | Bare usernames |
| `pending_requests.*` | Follow requests you've **sent** that haven't been accepted yet, sortable by request date |
| `you_dont_follow_back.*` | Accounts that follow **you** but you don't follow back |

Each list comes as `.html`, `.csv` and `.txt`. `pending_requests.*` is only
written when your export contains a `pending_follow_requests.json` — that is,
when you actually have outstanding requests to private accounts.

## Working through the list

Open `cleanup.html`, click a profile link to unfollow in the Instagram app,
and tick the box to mark it done. Every row also has:

- **find** — searches Instagram for the username, which is how you recover an
  account that was renamed. A direct link never can.
- **copy** — copies the username to the clipboard.
- **dead** — marks the row as gone.

Your marks are saved in the browser, so closing the tab part-way through a long
list doesn't lose your progress. A counter shows how many you've handled, and a
filter box jumps to a specific username. Each list keeps its own marks.

## Broken profile links

Some links land on *"Sorry, this page isn't available."* Almost always the
account was already gone **before** Instagram generated the export —
deactivated, deleted, or banned. Instagram keeps such accounts in your
following list, so the export lists them faithfully and their URLs don't
resolve. Renames are a much rarer cause, since the export records each username
as of the moment the file was generated.

The export carries no account ID, so there is no offline way to tell a deleted
account from a renamed one.

**These usually can't be unfollowed from their profile** — there's no profile
left to open. Use **copy**, then in the app open your profile → **Following**,
paste the username into the search box in that list, and tap
**Following → Unfollow**. If it doesn't appear there, Instagram has already
removed it, there is nothing left to unfollow, and it will drop out of your
next export by itself.

Instagram has no bulk unfollow, so this is one account at a time. Unless you're
near the 7,500 following cap, marking them **dead** and moving on is usually the
better use of your time.

> **Why not check the links automatically?** Instagram's official Graph API
> returns a follower *count*, never the list, and only for Business accounts.
> Checking each profile would mean either scraping Instagram while logged out
> (which mostly returns a login wall) or using the private mobile API with your
> password — which violates Instagram's Terms of Service and risks your account.
> Neither belongs in a tool whose whole point is that it runs offline.

## Hiding accounts you've dealt with: `ignore.txt`

Dead accounts reappear in every export, so without this you'd re-triage the same
broken links forever. Mark them **dead**, press **Save ignore.txt**, and put the
downloaded file next to the script:

```bash
python3 ig_followback.py export.zip                    # honours ./ignore.txt
python3 ig_followback.py export.zip --ignore mine.txt  # use a different file
python3 ig_followback.py export.zip --no-ignore        # show everything again
```

One username per line; blank lines and `#` comments are skipped, a leading `@`
is fine, and matching is case-insensitive. The run summary always tells you how
many accounts were hidden, so nothing disappears silently.

## Output categories

- **Not following you back** — you follow them, they don't follow you (cleanup candidates)
- **You don't follow back** — they follow you, you don't follow them
- **Mutuals** — count of accounts following each other
- **Pending sent requests** — follow requests you've sent to private accounts that haven't been accepted yet

If your export contains entries the script can't read a username from, it says
so at the end of the run rather than dropping them silently.

## Privacy

Everything runs locally and nothing is ever sent anywhere. Your export, the
generated lists and your `ignore.txt` are all git-ignored, so your personal data
stays on your machine.

## Development

```bash
python3 -m unittest test_ig_followback -v
```

Pure stdlib — no dependencies to install.
