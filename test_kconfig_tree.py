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


TREE_KCONFIG = """\
mainmenu "Test"
menu "Alpha"
config A1
	bool "Alpha one"
config A2
	tristate "Alpha two"
config A3
	int "Alpha three"
config A4
	bool "Alpha four"
endmenu
menuconfig NETX
	bool "Net thing"
if NETX
config NX1
	bool "Net one"
endif
menu "Beta"
if BAR
config B1
	bool "Beta one"
endif
if BAR
config B2
	bool "Beta two"
endif
endmenu
"""

TREE_CONFIG = """\
CONFIG_A1=y
CONFIG_A2=m
CONFIG_A3=4096
# CONFIG_A4 is not set
CONFIG_NETX=y
CONFIG_NX1=y
CONFIG_B1=y
"""

HEADER = "⚙ Linux Kernel Configuration"


class KernelTree:
	"""A throwaway kernel source root, and the tool run inside it."""

	def __init__(self, test: unittest.TestCase,
	             kconfig: str = TREE_KCONFIG, config: str = TREE_CONFIG):
		self.tmp = tempfile.TemporaryDirectory()
		test.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name)
		self.test = test
		self.write("Kconfig", kconfig)
		self.write(".config", config)

	def write(self, name: str, text: str) -> None:
		(self.root / name).write_text(text, encoding="utf-8")

	def read(self, name: str) -> str:
		return (self.root / name).read_text(encoding="utf-8")

	def drop_config(self, sym: str) -> None:
		"""Remove SYM from Kconfig, as a kernel update does."""
		text = self.read("Kconfig")
		block = re.search(rf"^config {sym}\n\t[^\n]*\n", text, re.M)
		self.test.assertIsNotNone(block, sym)
		self.write("Kconfig", text.replace(block.group(0), "", 1))

	def run(self, *args: str) -> subprocess.CompletedProcess:
		result = subprocess.run([sys.executable, str(TOOL), "--no-color",
		                         *args], cwd=self.root, capture_output=True,
		                        text=True, timeout=60)
		self.test.assertEqual(result.returncode, 0, result.stdout
		                      + result.stderr)
		return result


def output(result: subprocess.CompletedProcess) -> str:
	return result.stdout + result.stderr


class CommentTest(unittest.TestCase):
	# All four comment types, in the layout the tool writes. A doc that is
	# already in that layout must come back byte for byte: anything else
	# means a comment moved, merged or vanished.
	DOC = """\
⚙ Linux Kernel Configuration
├▶ Alpha
│├[*] Alpha one (A1) # trailing one
││# anchored above A1

│ # anchored below, before A2
│└[M] Alpha two (A2)
└[*] Net thing (NETX) ->

# freestanding note

 └[*] Net one (NX1)
"""

	def test_comments_survive_unchanged(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", self.DOC)
		tree.run()
		self.assertEqual(tree.read("kconfig_doc.txt"), self.DOC)

	def test_moved_entry_returns_to_tree_order_with_its_comment(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt",
		           "│└[M] Alpha two (A2) # two rationale\n"
		           "│├[*] Alpha one (A1)\n")
		tree.run()
		lines = tree.read("kconfig_doc.txt").splitlines()
		self.assertEqual(lines, [HEADER, "└▶ Alpha",
		                         " ├[*] Alpha one (A1)",
		                         " └[M] Alpha two (A2) # two rationale"])


class ValueTest(unittest.TestCase):
	def test_glyphs_follow_dotconfig(self) -> None:
		tree = KernelTree(self)
		tree.run("--add-new-enabled")
		doc = tree.read("kconfig_doc.txt")
		self.assertIn("[*] Alpha one (A1)", doc)
		self.assertIn("[M] Alpha two (A2)", doc)
		self.assertIn("[=4096] Alpha three (A3)", doc)
		tree.write(".config", TREE_CONFIG.replace("=4096", "=8192"))
		tree.run()
		self.assertIn("[=8192] Alpha three (A3)", tree.read("kconfig_doc.txt"))

	# The merge rule is "emit, update glyph, warn on value mismatch". The
	# check reads only values given in .config form (CONFIG_A3=4096); a
	# doc in the tool's own tree form never carries one, so the glyph is
	# updated in silence.
	@unittest.expectedFailure
	def test_value_change_in_tree_form_warns(self) -> None:
		tree = KernelTree(self)
		tree.run("--add-new-enabled")
		tree.write(".config", TREE_CONFIG.replace("=4096", "=8192"))
		self.assertIn("A3", output(tree.run()))

	def test_value_change_in_dotconfig_form_warns(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A3=4096\n")
		tree.write(".config", TREE_CONFIG.replace("=4096", "=8192"))
		self.assertIn("Value mismatch for CONFIG_A3", output(tree.run()))

	def test_emit_kconfig_lists_tracked_symbols_with_live_values(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A2\nCONFIG_A3\nCONFIG_NX1\n")
		result = tree.run("--emit-kconfig", "--no-doc")
		emitted = [line for line in result.stdout.splitlines()
		           if line.startswith("CONFIG_")]
		self.assertEqual(emitted,
		                 ["CONFIG_A2=m", "CONFIG_A3=4096", "CONFIG_NX1=y"])


class TrackingTest(unittest.TestCase):
	def test_untracked_active_option_gets_a_notice(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		self.assertIn("Active option not tracked: CONFIG_A2",
		              output(tree.run("--no-doc")))

	# The same rule for an option whose menu shows nothing yet -- the shape
	# a kernel update takes when it adds a menu. The walk never enters a
	# menu with no visible children, so the notice is never raised.
	@unittest.expectedFailure
	def test_untracked_option_in_an_undocumented_menu_gets_a_notice(
	        self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		self.assertIn("CONFIG_B1", output(tree.run("--no-doc")))

	def test_add_new_enabled_reads_the_named_column(self) -> None:
		tree = KernelTree(self)
		tree.write(".config2", "CONFIG_A4=y\nCONFIG_B2=y\n")
		tree.run("--add-new-enabled", "2")
		doc = tree.read("kconfig_doc.txt")
		self.assertIn("(A4)", doc)
		self.assertIn("(B2)", doc)
		self.assertNotIn("(A1)", doc)
		# Two configs, so two glyph columns: unset in .config, y in .config2.
		self.assertIn("[ *] Alpha four (A4)", doc)

	def test_ascii_output_is_ascii(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\nCONFIG_NX1\nCONFIG_B1\n")
		shown = tree.run("--show", "--ascii", "--no-doc").stdout
		self.assertIn("Alpha one (A1)", shown)
		self.assertTrue(shown.isascii(), shown)


class SuppressionTest(unittest.TestCase):
	def test_suppressed_option_stays_hidden_and_silent(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		tree.write("kconfig_doc_suppressed.txt", "CONFIG_A2\n")
		for _ in range(2):
			result = tree.run()
			self.assertNotIn("(A2)", tree.read("kconfig_doc.txt"))
			self.assertIn("(A2)", tree.read("kconfig_doc_suppressed.txt"))
			self.assertNotIn("CONFIG_A2", output(result))

	def test_conflict_doc_wins(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		tree.write("kconfig_doc_suppressed.txt", "CONFIG_A1\nCONFIG_A2\n")
		result = output(tree.run())
		self.assertIn("Conflict: CONFIG_A1", result)
		# Handled as a conflict. The descendant pass would also remove A1,
		# since it counts a symbol as its own descendant, but with a warning
		# that sends the reader looking for a child that does not exist.
		self.assertNotIn("descendant", result)
		self.assertIn("(A1)", tree.read("kconfig_doc.txt"))
		suppressed = tree.read("kconfig_doc_suppressed.txt")
		self.assertNotIn("(A1)", suppressed)
		self.assertIn("(A2)", suppressed)


class VanishedSymbolTest(unittest.TestCase):
	def test_commented_doc_symbol_becomes_a_stub(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt",
		           "CONFIG_A1\nCONFIG_A3 # three rationale\n")
		tree.drop_config("A3")
		tree.run()
		doc = tree.read("kconfig_doc.txt")
		self.assertRegex(doc, r"# CONFIG_A3 # three rationale")

	# By design (Pass 4 in DocFileParser): nothing was written about it,
	# so nothing is kept.
	def test_uncommented_symbol_is_dropped_silently(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\nCONFIG_A3\n")
		tree.drop_config("A3")
		result = tree.run()
		self.assertNotIn("A3", tree.read("kconfig_doc.txt"))
		self.assertNotIn("A3", output(result))

	# The warning says the entry was "converted to inline comment", but the
	# suppressed file is written without dead entries, so the rationale is
	# gone from both files.
	@unittest.expectedFailure
	def test_commented_suppressed_symbol_keeps_its_comment(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		tree.write("kconfig_doc_suppressed.txt",
		           "CONFIG_A4 # rejected: breaks the thing\n")
		tree.drop_config("A4")
		tree.run()
		kept = (tree.read("kconfig_doc.txt")
		        + tree.read("kconfig_doc_suppressed.txt"))
		self.assertIn("rejected: breaks the thing", kept)


class IfOccurrenceTest(unittest.TestCase):
	KCONFIG = """\
mainmenu "Test"
if BAR
config T1
	bool "Top one"
endif
menu "Menu"
if BAR
config M1
	bool "Menu one"
endif
endmenu
"""

	def tree(self) -> KernelTree:
		return KernelTree(self, self.KCONFIG, "CONFIG_T1=y\nCONFIG_M1=y\n")

	def test_numbered_occurrences_are_assigned_in_order(self) -> None:
		tree = self.tree()
		tree.run("--add-new-enabled")
		doc = tree.read("kconfig_doc.txt")
		self.assertIn("[if BAR]", doc)
		self.assertIn("[if BAR (2)]", doc)
		self.assertLess(doc.index("(T1)"), doc.index("(M1)"))

	def test_missing_occurrence_warns_at_top_level(self) -> None:
		tree = self.tree()
		tree.write("kconfig_doc.txt",
		           "├[if BAR (5)]\n│└[*] Top one (T1)\n")
		self.assertIn("[if BAR (5)] referenced but tree has only 2",
		              output(tree.run("--no-doc")))

	# Inside a menu, an occurrence the tree lacks gets a two-level key
	# ("Menu::if BAR (5)"), and occurrence tracking skips any key with
	# "::" in it -- so the warning cannot fire where kernel if-blocks
	# nearly always are.
	@unittest.expectedFailure
	def test_missing_occurrence_warns_inside_a_menu(self) -> None:
		tree = self.tree()
		tree.write("kconfig_doc.txt",
		           "├▶ Menu\n│└[if BAR (5)]\n│ └[*] Menu one (M1)\n")
		self.assertIn("[if BAR (5)] referenced but tree has only 2",
		              output(tree.run("--no-doc")))


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
