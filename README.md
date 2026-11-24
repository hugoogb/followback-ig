# followback-ig

Find the people you follow on Instagram who **don't follow you back**, fully
offline, so you can clean up your account. No login, no network, no API — it
reads Instagram's data export and produces a clickable action list for fast
manual unfollowing. Compare two exports and it also tells you **who unfollowed
you**.

There are two ways to use it, and they read the same exports and the same
`ignore.txt`:

- **In your browser** — no install. Open the page, drop in the `.zip`, done.
  Works on a phone, which is where you'll be unfollowing anyway.
- **On the command line** — `python3 ig_followback.py export.zip`, for people
  who'd rather have files and scripts.

> Automated unfollowing is intentionally **not** included: it violates
> Instagram's Terms of Service and risks action against your account.

## Get your data

Instagram → **Accounts Center → Your information and permissions → Export your
information** → request an export with:

- **Scope:** *Followers and following*
- **Format:** *JSON*

Instagram emails you a `.zip` when it's ready. **Download it and you're done —
there is nothing to unzip and no folder to go looking for.** If you picked
*HTML* by mistake, both versions say so; request a new export with *JSON*.

## In the browser

Open **[hugoogb.github.io/followback-ig](https://hugoogb.github.io/followback-ig/)**
(or download [`web/index.html`](web/index.html) and open the file — it works the
same offline) and drop your `.zip` onto it. If Safari already unzipped it for
you, choose the folder instead.

Everything happens inside that browser tab. The page carries a
Content-Security-Policy that forbids it from making *any* network request, so
your export physically cannot be uploaded — the browser would block it. It is a
single file with no libraries, so you can read all of it.

It shows the same lists as the command line, as tabs, with the same **find**,
**copy**, **dead** and **keep** buttons. Dead and kept accounts are hidden
automatically and stay hidden for future exports opened in the same browser.
**Save ignore.txt** / **Import ignore.txt** move those marks between browsers or
to the command-line version.

## On the command line

Python 3.9+, no dependencies. Either run the script directly, or install it as
a `followback` command:

```bash
pipx install git+https://github.com/hugoogb/followback-ig
```

```bash
followback instagram-yourname-2026-09-08.zip
```

The rest of this section uses `python3 ig_followback.py`; `followback` is the
same thing.

### Usage

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

`--help` lists the options. The terminal shows the first 20 accounts of each
list; `--print-all` shows everything. The files always contain everything.

### What it writes

A summary on stdout, plus these files **in the directory you run it from** —
never inside your export:

| File | What it is |
|------|-----------|
| `cleanup.html` | The main action list — profile links, checkboxes, sortable by follow date (oldest first, so stale follows surface) |
| `not_following_back.csv` | `username, profile_url, followed_on` for records/scripting |
| `not_following_back.txt` | Bare usernames |
| `pending_requests.*` | Follow requests you've **sent** that haven't been accepted yet, sortable by request date |
| `you_dont_follow_back.*` | Accounts that follow **you** but you don't follow back |
| `recently_unfollowed.*` | Accounts Instagram lists as recently unfollowed by you |
| `unfollowed_you.*`, `new_followers.*` | Only with `--since`: see [Who unfollowed you](#who-unfollowed-you) |

Each list comes as `.html`, `.csv` and `.txt`. `pending_requests.*` and
`recently_unfollowed.*` are only written when your export contains those
files — Instagram leaves them out when they'd be empty.

Accounts on your **Close Friends** list are tagged *close friend* in every list,
and the summary says how many of them don't follow you back, so you don't
unfollow one by accident.

## Who unfollowed you

Keep your old export, and next time compare against it:

```bash
python3 ig_followback.py new-export.zip --since old-export.zip
```

On top of the usual output this reports who **stopped following you**, your
**new followers**, and how many accounts you unfollowed or started following in
between, and writes `unfollowed_you.*` and `new_followers.*`. The browser
version does the same with **Compare with an older export…**.

An account that deactivated or deleted itself in between looks exactly like one
that unfollowed you — the export has no account IDs — so treat
*stopped following you* as "gone from your followers". If the two exports look
swapped (the "old" one has newer follows), you're told.

## Working through the list

Open `cleanup.html` (or use the browser version), click a profile link to
unfollow in the Instagram app, and tick the box to mark it done. Every row also
has:

- **find** — searches Instagram for the username, which is how you recover an
  account that was renamed. A direct link never can.
- **copy** — copies the username to the clipboard.
- **dead** — marks the row as gone.
- **keep** — for accounts you follow on purpose and don't expect a follow-back
  from: brands, artists, news. Without it they'd show up in every run.

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

Dead accounts and accounts you follow on purpose reappear in every export, so
without this you'd re-triage the same rows forever. Mark them **dead** or
**keep**, press **Save ignore.txt**, and put the downloaded file in the folder
you run the script from:

```bash
python3 ig_followback.py export.zip                    # honours ./ignore.txt
python3 ig_followback.py export.zip --ignore mine.txt  # use a different file
python3 ig_followback.py export.zip --no-ignore        # show everything again
```

One username per line; blank lines and `#` comments are skipped, a leading `@`
is fine, and matching is case-insensitive. The run summary always tells you how
many accounts were hidden, so nothing disappears silently.

Saving again from a later run keeps what was already in the file — the accounts
it hid aren't on the page, but they're carried over into the new download,
grouped under *Dead*, *Keep* and *Already in your ignore list*.

## Output categories

- **Not following you back** — you follow them, they don't follow you (cleanup candidates)
- **You don't follow back** — they follow you, you don't follow them
- **Mutuals** — count of accounts following each other
- **Pending sent requests** — follow requests you've sent to private accounts that haven't been accepted yet
- **Close friends** — tagged wherever they appear
- **Recently unfollowed**, **blocked** and **restricted** — listed or counted when your export has them
- **Stopped following you** / **New followers** — when comparing two exports

If your export contains entries the script can't read a username from, it says
so at the end of the run rather than dropping them silently.

## Privacy

Everything runs locally and nothing is ever sent anywhere — and in the browser
version, the page is not *able* to send anything. Your export, the generated
lists and your `ignore.txt` are all git-ignored, so your personal data stays on
your machine.

## Development

```bash
python3 -m unittest test_ig_followback -v
```

Pure stdlib — no dependencies to install.

The browser version has end-to-end tests that build fixture exports, drive the
page in Chromium, and check its results against the Python tool on the same
export, so the two parsers can't drift apart. They need Node 18+ and
Playwright:

```bash
npm install --no-save playwright && npx playwright install chromium
node web/test_web.cjs
```

The parsing logic exists twice — `ig_followback.py` and the `<script>` in
`web/index.html` — so a change to one needs the same change in the other. Both
test suites run in CI on every push. Pushes to `master` that touch the page
publish it to GitHub Pages (enable once under *Settings → Pages → Source:
GitHub Actions*).
