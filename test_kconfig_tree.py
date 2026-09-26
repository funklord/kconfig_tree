#!/usr/bin/env python3
"""Tests for kconfig_tree.py."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "kconfig_tree.py"


class HelpTest(unittest.TestCase):
	# The module docstring is the --help epilog and its OPTIONS list is
	# written by hand, so it fell behind the parser once: --dotconfig2,
	# --dotconfig3 and --ascii were accepted and never listed. The flags
	# are taken from argparse's own usage line, so a new option fails
	# here until the docstring names it.
	def test_options_list_names_every_flag(self) -> None:
		result = subprocess.run([sys.executable, str(TOOL), "--help"],
		                        capture_output=True, text=True, timeout=60)
		self.assertEqual(result.returncode, 0, result.stderr)
		text = result.stdout
		usage = text[:text.index("\n\n")]
		flags = sorted(set(re.findall(r"--[a-z0-9-]+", usage)) - {"--help"})
		self.assertGreater(len(flags), 10, "usage line not parsed")
		options = text[text.index("\nOPTIONS\n"):]
		missing = [flag for flag in flags
		           if not re.search(r"^\s*" + re.escape(flag) + r"\b",
		                            options, re.M)]
		self.assertEqual(missing, [])


FIXTURE_KCONFIG = """\
mainmenu "Test"
menu "Alpha"
config A1
	bool "Alpha one"
config A2
	bool "Alpha two"
endmenu
menu "Beta"
config B1
	bool "Beta one"
endmenu
"""


class BlankLineTest(unittest.TestCase):
	# Settled 2026-09-26: no blank between options, even across unrelated
	# subtrees -- the connectors carry the hierarchy. A rule inserting one
	# between options with no shared non-root ancestor existed as dead code
	# after revision 14 and was described as live, so this pins the
	# behaviour rather than any description of it.
	def test_no_blank_between_unrelated_subtrees(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			(root / "Kconfig").write_text(FIXTURE_KCONFIG, encoding="utf-8")
			(root / ".config").write_text(
			        "CONFIG_A1=y\nCONFIG_A2=y\nCONFIG_B1=y\n",
			        encoding="utf-8")
			result = subprocess.run(
			        [sys.executable, str(TOOL), "--add-new-enabled",
			         "--no-color"],
			        cwd=root, capture_output=True, text=True, timeout=60)
			self.assertEqual(result.returncode, 0, result.stderr)
			lines = (root / "kconfig_doc.txt").read_text(
			        encoding="utf-8").splitlines()
		self.assertEqual(len(lines), 6, lines)
		self.assertIn("(A2)", lines[3])
		self.assertIn("Beta", lines[4])
		self.assertNotIn("", lines)


class VersionTest(unittest.TestCase):
	# The number lives in the VERSION file and, because the script travels
	# alone into the trees that vendor it, again in the script. Asked of
	# the running program rather than grepped from the source, so a copy
	# that parses but prints the wrong thing fails too.
	def test_version_matches_the_version_file(self) -> None:
		result = subprocess.run([sys.executable, str(TOOL), "--version"],
		                        capture_output=True, text=True, timeout=60)
		self.assertEqual(result.returncode, 0, result.stderr)
		want = (TOOL.parent / "VERSION").read_text(encoding="utf-8").strip()
		first, _, rest = result.stdout.partition("\n")
		self.assertEqual(first, f"kconfig_tree {want}")
		self.assertIn("Copyright (C) 2026 Nabeel Sowan", rest)


if __name__ == "__main__":
	unittest.main()
