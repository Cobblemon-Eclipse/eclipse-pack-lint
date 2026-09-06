"""`fix` must never touch the input pack, and must be honest about misses."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packlint import fixer  # noqa: E402
from tests import fixtures as fx  # noqa: E402


class FixerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pack = fx.write_zip(
            os.path.join(self.tmp.name, "pack.zip"),
            {"a.json": "{}", "b.json": "{}", "keep.png": "PNG"},
        )

    def test_apply_writes_a_new_zip_and_leaves_the_original_alone(self) -> None:
        out = os.path.join(self.tmp.name, "out.zip")
        with open(self.pack, "rb") as fh:
            before = fh.read()
        outcome = fixer.apply_manifest(
            self.pack, {"delete": ["a.json"], "write": {"c.json": "{\"new\": 1}"}}, out
        )
        self.assertEqual(outcome["deleted"], ["a.json"])
        self.assertEqual(outcome["added"], ["c.json"])
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(sorted(zf.namelist()), ["b.json", "c.json", "keep.png"])
        with open(self.pack, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_replacing_an_existing_entry_keeps_it_in_place(self) -> None:
        out = os.path.join(self.tmp.name, "out.zip")
        outcome = fixer.apply_manifest(
            self.pack, {"delete": [], "write": {"b.json": "{\"v\": 2}"}}, out
        )
        self.assertEqual(outcome["replaced"], ["b.json"])
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.read("b.json"), b'{"v": 2}')
            self.assertEqual(len(zf.namelist()), 3)

    def test_it_reports_deletions_the_pack_did_not_contain(self) -> None:
        out = os.path.join(self.tmp.name, "out.zip")
        outcome = fixer.apply_manifest(self.pack, {"delete": ["nope.json"]}, out)
        self.assertEqual(outcome["delete_not_found"], ["nope.json"])

    def test_it_refuses_to_write_in_place_or_over_an_existing_file(self) -> None:
        with self.assertRaises(ValueError):
            fixer.apply_manifest(self.pack, {"delete": []}, self.pack)
        out = os.path.join(self.tmp.name, "taken.zip")
        open(out, "w").close()
        with self.assertRaises(ValueError):
            fixer.apply_manifest(self.pack, {"delete": []}, out)

    def test_a_failed_apply_leaves_no_partial_zip(self) -> None:
        out = os.path.join(self.tmp.name, "out.zip")
        with self.assertRaises(Exception):
            fixer.apply_manifest(self.pack, {"write": {"x.json": 5}}, out)
        self.assertFalse(os.path.exists(out + ".partial"))


if __name__ == "__main__":
    unittest.main()
