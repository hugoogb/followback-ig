#!/usr/bin/env python3
"""Tests for ig_followback (offline Instagram follow-back cleanup util)."""

import contextlib
import io
import json
import os
import sys
import tempfile
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
        self.assertEqual(list(accounts), ["Marta Murcia"])
        self.assertEqual(stats.suspicious, ["Marta Murcia"])

    def test_plain_username_entries_are_not_flagged(self):
        _accounts, stats = self._one(_block("alice", timestamp=1))
        self.assertEqual(stats.total, 0)


if __name__ == "__main__":
    unittest.main()
