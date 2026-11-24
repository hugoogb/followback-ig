#!/usr/bin/env python3
"""Tests for ig_followback (offline Instagram follow-back cleanup util)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import zipfile
import unittest
from pathlib import Path

import ig_followback as ig


def _block(username, href="", timestamp=None):
    """Build a single IG relationship block as it appears in the export."""
    item = {"value": username, "href": href or f"https://instagram.com/{username}"}
    if timestamp is not None:
        item["timestamp"] = timestamp
    return {"string_list_data": [item]}


class FormatDateTests(unittest.TestCase):
    def test_formats_epoch_as_iso_date(self):
        # 2021-01-01 00:00:00 UTC
        self.assertEqual(ig.format_date(1609459200), "2021-01-01")

    def test_missing_timestamp_is_unknown(self):
        self.assertEqual(ig.format_date(None), "unknown")

    def test_zero_timestamp_is_unknown(self):
        # IG sometimes emits 0 for an absent timestamp
        self.assertEqual(ig.format_date(0), "unknown")


class ComputeDiffTests(unittest.TestCase):
    def setUp(self):
        self.following = {
            "alice": {"href": "https://instagram.com/alice", "timestamp": 200},
            "bob": {"href": "https://instagram.com/bob", "timestamp": 100},
            "carol": {"href": "https://instagram.com/carol", "timestamp": None},
        }
        self.followers = {
            "alice": {"href": "https://instagram.com/alice", "timestamp": 999},
            "dave": {"href": "https://instagram.com/dave", "timestamp": 999},
        }

    def test_not_following_back_excludes_mutuals(self):
        diff = ig.compute_diff(self.following, self.followers)
        names = [acc["username"] for acc in diff.not_following_back]
        self.assertEqual(set(names), {"bob", "carol"})

    def test_not_following_back_sorted_oldest_first_unknown_last(self):
        diff = ig.compute_diff(self.following, self.followers)
        names = [acc["username"] for acc in diff.not_following_back]
        # bob (ts 100) before carol (unknown -> last)
        self.assertEqual(names, ["bob", "carol"])

    def test_candidate_carries_profile_url_and_followed_on(self):
        diff = ig.compute_diff(self.following, self.followers)
        bob = next(a for a in diff.not_following_back if a["username"] == "bob")
        self.assertEqual(bob["profile_url"], "https://instagram.com/bob")
        self.assertEqual(bob["followed_on"], ig.format_date(100))

    def test_fans_you_dont_follow_back(self):
        diff = ig.compute_diff(self.following, self.followers)
        names = [acc["username"] for acc in diff.fans_you_dont_follow_back]
        self.assertEqual(names, ["dave"])

    def test_mutuals_count(self):
        diff = ig.compute_diff(self.following, self.followers)
        self.assertEqual(diff.mutuals, 1)


class LoadAccountsTests(unittest.TestCase):
    def _write(self, folder, name, data):
        (folder / name).write_text(json.dumps(data), encoding="utf-8")

    def test_loads_following_dict_shape_and_followers_multifile(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self._write(folder, "following.json", {
                "relationships_following": [
                    _block("alice", timestamp=100),
                    _block("bob", timestamp=200),
                ]
            })
            # followers split across multiple files, top-level list shape
            self._write(folder, "followers_1.json", [_block("alice", timestamp=300)])
            self._write(folder, "followers_2.json", [_block("eve", timestamp=400)])

            following, followers = ig.load_accounts(folder)

            self.assertEqual(set(following), {"alice", "bob"})
            self.assertEqual(following["bob"]["timestamp"], 200)
            self.assertEqual(set(followers), {"alice", "eve"})

    def test_loads_older_single_file_followers(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self._write(folder, "following.json", {
                "relationships_following": [_block("alice")]
            })
            self._write(folder, "followers.json", [_block("zoe")])

            _following, followers = ig.load_accounts(folder)
            self.assertEqual(set(followers), {"zoe"})

    def test_skips_entries_without_value(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self._write(folder, "following.json", {
                "relationships_following": [
                    {"string_list_data": [{"value": "", "href": "x"}]},
                    _block("real"),
                ]
            })
            self._write(folder, "followers.json", [])
            following, _followers = ig.load_accounts(folder)
            self.assertEqual(set(following), {"real"})


class PendingRequestsTests(unittest.TestCase):
    def _write(self, folder, name, data):
        (folder / name).write_text(json.dumps(data), encoding="utf-8")

    def test_loads_pending_requests_dict_shape(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self._write(folder, "pending_follow_requests.json", {
                "relationships_follow_requests_sent": [
                    _block("private_one", timestamp=100),
                    _block("private_two", timestamp=200),
                ]
            })
            pending = ig.load_pending_requests(folder)
            self.assertEqual(set(pending), {"private_one", "private_two"})
            self.assertEqual(pending["private_two"]["timestamp"], 200)

    def test_loads_pending_requests_list_shape(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self._write(folder, "pending_follow_requests.json",
                        [_block("private_one", timestamp=100)])
            pending = ig.load_pending_requests(folder)
            self.assertEqual(set(pending), {"private_one"})

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            # No pending_follow_requests.json present — must not crash.
            self.assertEqual(ig.load_pending_requests(Path(d)), {})


class RealExportShapeTests(unittest.TestCase):
    """Shapes seen in actual Instagram exports (regression coverage)."""

    def _write(self, folder, name, data):
        (folder / name).write_text(json.dumps(data), encoding="utf-8")

    def test_following_username_from_block_title_when_value_absent(self):
        # following.json: username is the block "title"; string_list_data has
        # only href + timestamp (no "value").
        accounts = ig._extract_accounts([
            {"title": "jaywheelerpr",
             "string_list_data": [
                 {"href": "https://www.instagram.com/_u/jaywheelerpr",
                  "timestamp": 1781997016}]},
        ])
        self.assertIn("jaywheelerpr", accounts)
        self.assertEqual(accounts["jaywheelerpr"]["timestamp"], 1781997016)

    def test_builds_clean_web_profile_url_not_deeplink(self):
        accounts = ig._extract_accounts([
            {"title": "ghost",
             "string_list_data": [
                 {"href": "https://www.instagram.com/_u/ghost", "timestamp": 5}]},
        ])
        self.assertEqual(accounts["ghost"]["href"], "https://instagram.com/ghost")

    def test_pending_requests_label_values_shape(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self._write(folder, "pending_follow_requests.json", {
                "relationships_follow_requests_sent": [
                    {"timestamp": 1778601827,
                     "label_values": [
                         {"label": "URL", "value": ""},
                         {"label": "Name", "value": "Marta Murcia"},
                         {"label": "Username", "value": "marta.murcia"},
                     ]},
                ]
            })
            pending = ig.load_pending_requests(folder)
            self.assertIn("marta.murcia", pending)
            self.assertEqual(pending["marta.murcia"]["timestamp"], 1778601827)


class OutputLocationTests(unittest.TestCase):
    def test_outputs_written_to_cwd_not_input_folder(self):
        with tempfile.TemporaryDirectory() as src_d, tempfile.TemporaryDirectory() as run_d:
            src, run = Path(src_d), Path(run_d)
            (src / "following.json").write_text(
                json.dumps({"relationships_following": [_block("ghost", timestamp=100)]}),
                encoding="utf-8")
            (src / "followers.json").write_text(
                json.dumps([_block("fan", timestamp=100)]), encoding="utf-8")

            old_argv, old_cwd = sys.argv, os.getcwd()
            try:
                os.chdir(run)
                sys.argv = ["ig_followback.py", str(src)]
                with contextlib.redirect_stdout(io.StringIO()):
                    ig.main()
            finally:
                sys.argv = old_argv
                os.chdir(old_cwd)

            self.assertTrue((run / "cleanup.html").exists())
            self.assertTrue((run / "not_following_back.csv").exists())
            self.assertFalse((src / "cleanup.html").exists())


class RenderActionListTests(unittest.TestCase):
    def test_includes_title_and_usernames(self):
        records = [ig._to_record("ghosty",
                   {"href": "https://instagram.com/ghosty", "timestamp": 100})]
        out = ig._render_action_list("My Title", "some intro", records)
        self.assertIn("My Title", out)
        self.assertIn("some intro", out)
        self.assertIn("ghosty", out)


class IgnoreListTests(unittest.TestCase):
    def test_parses_comments_blanks_at_prefix_and_casing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ignore.txt"
            path.write_text(
                "# a comment\n"
                "@Deleted.Acct\n"
                "\n"
                "Renamed.User   # trailing comment\n",
                encoding="utf-8")
            self.assertEqual(ig.load_ignore_list(path),
                             {"deleted.acct", "renamed.user"})

    def test_missing_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ig.load_ignore_list(Path(d) / "nope.txt"), set())

    def test_apply_filters_case_insensitively_and_counts(self):
        records = [ig._to_record(u, {"timestamp": 1})
                   for u in ("keep", "DropMe")]
        kept, hidden = ig.apply_ignore_list(records, {"dropme"})
        self.assertEqual([r["username"] for r in kept], ["keep"])
        self.assertEqual(hidden, 1)

    def test_empty_ignore_list_is_a_passthrough(self):
        records = [ig._to_record("keep", {"timestamp": 1})]
        kept, hidden = ig.apply_ignore_list(records, set())
        self.assertEqual(kept, records)
        self.assertEqual(hidden, 0)

    def test_main_hides_ignored_accounts_unless_no_ignore(self):
        with tempfile.TemporaryDirectory() as src_d, tempfile.TemporaryDirectory() as run_d:
            src, run = Path(src_d), Path(run_d)
            (src / "following.json").write_text(json.dumps({
                "relationships_following": [_block("ghost", timestamp=100),
                                            _block("dead", timestamp=200)]}),
                encoding="utf-8")
            (src / "followers.json").write_text("[]", encoding="utf-8")
            (run / "ignore.txt").write_text("dead\n", encoding="utf-8")

            old_cwd = os.getcwd()
            try:
                os.chdir(run)
                with contextlib.redirect_stdout(io.StringIO()):
                    ig.main([str(src)])
                self.assertEqual((run / "not_following_back.txt").read_text(), "ghost")

                with contextlib.redirect_stdout(io.StringIO()):
                    ig.main([str(src), "--no-ignore"])
                self.assertIn("dead", (run / "not_following_back.txt").read_text())
            finally:
                os.chdir(old_cwd)


class DeadLinkAffordanceTests(unittest.TestCase):
    """The action list has to stay usable when a profile link 404s."""

    def _render(self):
        records = [ig._to_record("gone.user",
                                 {"href": "https://instagram.com/gone.user",
                                  "timestamp": 100})]
        return ig._render_action_list("Not following you back", "intro", records)

    def test_row_offers_a_search_fallback_for_renamed_accounts(self):
        out = self._render()
        self.assertIn(ig._search_url("gone.user"), out)

    def test_row_offers_a_dead_marker(self):
        self.assertIn('class="dead"', self._render())

    def test_row_offers_a_copy_button(self):
        # A dead account has no profile to open: unfollowing it means pasting
        # the username into the search box of your own Following list.
        self.assertIn('class="copy"', self._render())

    def test_no_unreplaced_placeholders(self):
        self.assertNotIn("__", self._render())

    def test_template_keeps_js_newline_escape_intact(self):
        # Regression: as a non-raw Python string the template's "\n" escapes
        # became real newlines, splitting a JS string literal across lines and
        # killing every script on the page.
        self.assertIn("\\n", ig._HTML_TEMPLATE)


class UsernameExtractionTests(unittest.TestCase):
    """A link that 404s because the script built it wrong is the worst kind."""

    def _one(self, block):
        stats = ig.ParseStats()
        accounts = ig._extract_accounts([block], stats)
        return accounts, stats

    def test_href_beats_a_display_name_in_title(self):
        # Regression: this built "https://instagram.com/Marta Murcia".
        accounts, _ = self._one({
            "title": "Marta Murcia",
            "string_list_data": [
                {"href": "https://www.instagram.com/_u/marta.murcia", "timestamp": 1}]})
        self.assertEqual(list(accounts), ["marta.murcia"])
        self.assertEqual(accounts["marta.murcia"]["href"],
                         "https://instagram.com/marta.murcia")

    def test_href_beats_a_display_name_in_value(self):
        accounts, _ = self._one({"string_list_data": [
            {"value": "Jay Wheeler PR",
             "href": "https://www.instagram.com/jaywheelerpr", "timestamp": 3}]})
        self.assertEqual(list(accounts), ["jaywheelerpr"])

    def test_recovers_account_when_only_the_href_has_the_username(self):
        # Regression: this entry was dropped silently.
        accounts, stats = self._one({
            "title": "",
            "string_list_data": [
                {"href": "https://www.instagram.com/_u/ghosty", "timestamp": 2}]})
        self.assertEqual(list(accounts), ["ghosty"])
        self.assertEqual(stats.skipped, 0)

    def test_localised_label_values_still_parse(self):
        # A Spanish export labels the field "Nombre de usuario"; the value's
        # shape identifies it even when the label is unknown.
        accounts, _ = self._one({"timestamp": 9, "label_values": [
            {"label": "URL", "value": ""},
            {"label": "Nombre", "value": "Marta Murcia"},
            {"label": "Nombre de usuario", "value": "marta.murcia"}]})
        self.assertEqual(list(accounts), ["marta.murcia"])

    def test_unknown_label_falls_back_to_value_shape(self):
        accounts, _ = self._one({"timestamp": 9, "label_values": [
            {"label": "Nimi", "value": "Marta Murcia"},
            {"label": "Kasutajanimi", "value": "marta.murcia"}]})
        self.assertEqual(list(accounts), ["marta.murcia"])

    def test_unreadable_entry_is_counted_not_silently_dropped(self):
        accounts, stats = self._one(
            {"title": "", "string_list_data": [{"href": "", "timestamp": 4}]})
        self.assertEqual(accounts, {})
        self.assertEqual(stats.skipped, 1)

    def test_unusable_username_is_kept_but_flagged(self):
        # Nothing resolvable anywhere: keep the row so the user can still
        # search for it, but say so rather than pretending the link works.
        accounts, stats = self._one({"title": "Marta Murcia"})
        self.assertEqual(list(accounts), ["marta murcia"])  # key is casefolded
        self.assertEqual(accounts["marta murcia"]["display"], "Marta Murcia")
        self.assertEqual(stats.suspicious, ["Marta Murcia"])

    def test_plain_username_entries_are_not_flagged(self):
        _accounts, stats = self._one(_block("alice", timestamp=1))
        self.assertEqual(stats.total, 0)


class CaseFoldingTests(unittest.TestCase):
    """following.json and followers_*.json are matched by key, so casing drift
    between the two files must not invent or destroy a relationship."""

    def _accounts(self, name, timestamp):
        return ig._extract_accounts([_block(name, timestamp=timestamp)])

    def test_casing_difference_still_counts_as_a_mutual(self):
        following = self._accounts("Hugo.GB", 100)
        followers = self._accounts("hugo.gb", 200)
        diff = ig.compute_diff(following, followers)
        self.assertEqual(diff.mutuals, 1)
        self.assertEqual(diff.not_following_back, [])
        self.assertEqual(diff.fans_you_dont_follow_back, [])

    def test_display_keeps_the_original_spelling(self):
        following = self._accounts("Hugo.GB", 100)
        followers = self._accounts("someone.else", 200)
        diff = ig.compute_diff(following, followers)
        record = diff.not_following_back[0]
        self.assertEqual(record["username"], "Hugo.GB")
        self.assertEqual(record["profile_url"], "https://instagram.com/Hugo.GB")


class ExportErrorTests(unittest.TestCase):
    """Loaders raise so the module is importable as a library; only the CLI exits."""

    def test_missing_following_raises_rather_than_exiting(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ig.ExportError):
                ig.load_accounts(Path(d))

    def test_missing_followers_raises_rather_than_exiting(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "following.json").write_text(
                json.dumps({"relationships_following": []}), encoding="utf-8")
            with self.assertRaises(ig.ExportError):
                ig.load_accounts(Path(d))

    def test_cli_turns_the_error_into_a_clean_exit(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stderr(io.StringIO()):
                    ig.main([d])
            self.assertNotEqual(caught.exception.code, 0)


class ExportSourceTests(unittest.TestCase):
    """A zip, the folder it unzips to, and the data folder must behave alike."""

    EXPORT_SUBDIR = "instagram-user-2026-09-08-abc/connections/followers_and_following"

    def _build(self, root: Path) -> Path:
        folder = root / self.EXPORT_SUBDIR
        folder.mkdir(parents=True)
        (folder / "following.json").write_text(json.dumps({
            "relationships_following": [_block("alice", timestamp=100),
                                        _block("ghosty", timestamp=200)]}),
            encoding="utf-8")
        (folder / "followers_1.json").write_text(
            json.dumps([_block("alice", timestamp=300)]), encoding="utf-8")
        # Noise elsewhere in the archive must not confuse the lookup.
        other = root / "instagram-user-2026-09-08-abc" / "messages"
        other.mkdir(parents=True)
        (other / "inbox.json").write_text("{}", encoding="utf-8")
        return folder

    def _expect(self, source):
        following, followers = ig.load_accounts(source)
        self.assertEqual(set(following), {"alice", "ghosty"})
        self.assertEqual(set(followers), {"alice"})

    def test_reads_the_data_folder_directly(self):
        with tempfile.TemporaryDirectory() as d:
            self._expect(self._build(Path(d)))

    def test_finds_the_data_folder_from_the_unzipped_export_root(self):
        with tempfile.TemporaryDirectory() as d:
            self._build(Path(d))
            self._expect(Path(d) / "instagram-user-2026-09-08-abc")

    def test_reads_straight_out_of_a_zip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "src"
            root.mkdir()
            self._build(root)
            archive = Path(d) / "export.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for item in root.rglob("*"):
                    if item.is_file():
                        zf.write(item, item.relative_to(root))
            self._expect(archive)

    def test_pending_requests_read_from_a_zip_too(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "src"
            folder = self._build(root)
            (folder / "pending_follow_requests.json").write_text(json.dumps({
                "relationships_follow_requests_sent": [
                    _block("private.acct", timestamp=5)]}), encoding="utf-8")
            archive = Path(d) / "export.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for item in root.rglob("*"):
                    if item.is_file():
                        zf.write(item, item.relative_to(root))
            self.assertEqual(set(ig.load_pending_requests(archive)), {"private.acct"})

    def test_a_non_archive_file_is_rejected_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            junk = Path(d) / "notes.txt"
            junk.write_text("hello", encoding="utf-8")
            with self.assertRaises(ig.ExportError):
                ig.open_export(junk)

    def test_html_format_export_is_named_as_such(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "connections" / "followers_and_following"
            folder.mkdir(parents=True)
            (folder / "following.html").write_text("<html></html>", encoding="utf-8")
            with self.assertRaisesRegex(ig.ExportError, "Format: JSON"):
                ig.load_accounts(Path(d))

    def test_missing_path_is_rejected_clearly(self):
        with self.assertRaises(ig.ExportError):
            ig.open_export(Path("/definitely/not/here"))


class FansOutputTests(unittest.TestCase):
    """The follow-you-back list was computed every run and thrown away."""

    def test_writes_a_list_for_accounts_you_dont_follow_back(self):
        with tempfile.TemporaryDirectory() as src_d, tempfile.TemporaryDirectory() as run_d:
            src, run = Path(src_d), Path(run_d)
            (src / "following.json").write_text(json.dumps({
                "relationships_following": [_block("alice", timestamp=100)]}),
                encoding="utf-8")
            (src / "followers.json").write_text(json.dumps([
                _block("alice", timestamp=1), _block("superfan", timestamp=2)]),
                encoding="utf-8")
            old_cwd = os.getcwd()
            try:
                os.chdir(run)
                with contextlib.redirect_stdout(io.StringIO()):
                    ig.main([str(src)])
            finally:
                os.chdir(old_cwd)
            self.assertEqual((run / "you_dont_follow_back.txt").read_text(), "superfan")
            self.assertIn("followed_you_on", (run / "you_dont_follow_back.csv").read_text())

    def test_each_list_keeps_its_own_triage_marks(self):
        # Shared localStorage would let a tick on one list mark the other.
        keys = {ig._render_action_list(t, "i", []).split("const KEY = ")[1].split(chr(10))[0]
                for t in ("Not following you back", "You don't follow them back",
                          "Pending sent follow requests")}
        self.assertEqual(len(keys), 3)


def _run_main(src, run, *extra):
    """Run the CLI from `run` against export `src`; returns stdout."""
    old_cwd, buf = os.getcwd(), io.StringIO()
    try:
        os.chdir(run)
        with contextlib.redirect_stdout(buf):
            ig.main([str(src), *extra])
    finally:
        os.chdir(old_cwd)
    return buf.getvalue()


def _export(folder, following, followers, **extra_files):
    """Write a minimal export folder: usernames -> (timestamp)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "following.json").write_text(json.dumps({
        "relationships_following": [_block(u, timestamp=t) for u, t in following.items()]}),
        encoding="utf-8")
    (folder / "followers_1.json").write_text(
        json.dumps([_block(u, timestamp=t) for u, t in followers.items()]), encoding="utf-8")
    for name, data in extra_files.items():
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


class KeepMarkTests(unittest.TestCase):
    """Accounts followed on purpose need a way out of the list besides 'dead'."""

    def test_row_offers_a_keep_marker(self):
        records = [ig._to_record("brand", {"timestamp": 1})]
        self.assertIn('class="keep"', ig._render_action_list("T", "i", records))

    def test_prior_ignore_entries_are_carried_into_the_next_save(self):
        # Hidden rows aren't in the table, so without this a second save would
        # silently drop everything ignored in earlier runs.
        out = ig._render_action_list("T", "i", [], prior=["old.dead", "brand"])
        self.assertIn('const PRIOR = ["brand", "old.dead"];', out)

    def test_prior_cannot_break_out_of_the_script_tag(self):
        out = ig._render_action_list("T", "i", [], prior=["</script><b>"])
        self.assertNotIn("</script><b>", out)

    def test_main_passes_the_ignore_list_to_the_page(self):
        with tempfile.TemporaryDirectory() as d:
            src = _export(Path(d) / "src", {"ghost": 1, "dead.one": 2}, {})
            run = Path(d) / "run"
            run.mkdir()
            (run / "ignore.txt").write_text("dead.one\n", encoding="utf-8")
            _run_main(src, run)
            self.assertIn('const PRIOR = ["dead.one"];',
                          (run / "cleanup.html").read_text(encoding="utf-8"))

    def test_a_username_that_looks_like_a_placeholder_is_left_alone(self):
        records = [ig._to_record("__ROWS__", {"timestamp": 1})]
        out = ig._render_action_list("T", "i", records, prior=["__TITLE__"])
        self.assertIn('data-username="__ROWS__"', out)
        self.assertIn('"__TITLE__"', out)


class OptionalListsTests(unittest.TestCase):
    CLOSE = {"relationships_close_friends": [_block("bestie", timestamp=1)]}
    RECENT = {"relationships_unfollowed_users": [_block("old.crush", timestamp=7)]}

    def test_missing_optional_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            src = _export(Path(d), {"a": 1}, {"a": 1})
            self.assertEqual(ig.load_optional_list(src, ig.CLOSE_FRIENDS_FILE), {})

    def test_reads_close_friends_whatever_the_top_level_key(self):
        with tempfile.TemporaryDirectory() as d:
            src = _export(Path(d), {"a": 1}, {}, close_friends=self.CLOSE)
            self.assertEqual(set(ig.load_optional_list(src, ig.CLOSE_FRIENDS_FILE)), {"bestie"})

    def test_close_friends_who_dont_follow_back_are_tagged(self):
        with tempfile.TemporaryDirectory() as d:
            src = _export(Path(d) / "src", {"Bestie": 1, "rando": 2}, {},
                          close_friends=self.CLOSE)
            run = Path(d) / "run"
            run.mkdir()
            out = _run_main(src, run)
            page = (run / "cleanup.html").read_text(encoding="utf-8")
            self.assertEqual(page.count('class="tag" title='), 1)
            self.assertIn("1 on your Close Friends list", out)

    def test_recently_unfollowed_gets_its_own_read_only_list(self):
        with tempfile.TemporaryDirectory() as d:
            src = _export(Path(d) / "src", {"a": 1}, {"a": 1},
                          recently_unfollowed_profiles=self.RECENT)
            run = Path(d) / "run"
            run.mkdir()
            _run_main(src, run)
            self.assertEqual((run / "recently_unfollowed.txt").read_text(), "old.crush")
            self.assertIn('<body class="plain">',
                          (run / "recently_unfollowed.html").read_text(encoding="utf-8"))


class CompareExportsTests(unittest.TestCase):
    def test_compare_exports_reports_all_four_directions(self):
        acc = lambda *names: {n: {"timestamp": 1} for n in names}
        changes = ig.compare_exports(
            old_following=acc("kept", "dropped"), old_followers=acc("loyal", "leaver"),
            new_following=acc("kept", "added"), new_followers=acc("loyal", "newbie"))
        names = lambda recs: [r["username"] for r in recs]
        self.assertEqual(names(changes.lost_followers), ["leaver"])
        self.assertEqual(names(changes.new_followers), ["newbie"])
        self.assertEqual(names(changes.you_unfollowed), ["dropped"])
        self.assertEqual(names(changes.you_followed), ["added"])

    def test_since_writes_change_lists(self):
        with tempfile.TemporaryDirectory() as d:
            old = _export(Path(d) / "old", {"a": 1}, {"a": 1, "Leaver": 2})
            new = _export(Path(d) / "new", {"a": 1}, {"a": 1, "newbie": 9})
            run = Path(d) / "run"
            run.mkdir()
            out = _run_main(new, run, "--since", str(old))
            self.assertEqual((run / "unfollowed_you.txt").read_text(), "Leaver")
            self.assertEqual((run / "new_followers.txt").read_text(), "newbie")
            self.assertIn("Stopped following you: 1", out)
            self.assertNotIn("Did you swap them?", out)

    def test_swapped_exports_are_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            old = _export(Path(d) / "old", {"a": 1}, {"a": 1})
            new = _export(Path(d) / "new", {"a": 1}, {"a": 1, "later": 99})
            run = Path(d) / "run"
            run.mkdir()
            self.assertIn("Did you swap them?", _run_main(old, run, "--since", str(new)))

    def test_change_lists_are_written_even_when_empty(self):
        with tempfile.TemporaryDirectory() as d:
            same = _export(Path(d) / "x", {"a": 1}, {"a": 1})
            run = Path(d) / "run"
            run.mkdir()
            _run_main(same, run, "--since", str(same))
            self.assertEqual((run / "unfollowed_you.txt").read_text(), "")


class TerminalOutputTests(unittest.TestCase):
    def test_long_lists_are_truncated_unless_print_all(self):
        with tempfile.TemporaryDirectory() as d:
            src = _export(Path(d) / "src", {f"user{i:02}": i + 1 for i in range(30)}, {})
            run = Path(d) / "run"
            run.mkdir()
            short = _run_main(src, run)
            self.assertIn("... and 10 more", short)
            self.assertNotIn("user29", short)
            self.assertIn("user29", _run_main(src, run, "--print-all"))
            # The files always hold everything.
            self.assertEqual(len((run / "not_following_back.txt").read_text().split()), 30)


if __name__ == "__main__":
    unittest.main()
