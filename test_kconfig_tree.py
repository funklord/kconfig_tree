#!/usr/bin/env python3
"""Tests for kconfig_tree.py."""

import re
import subprocess
import sys
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


if __name__ == "__main__":
	unittest.main()
