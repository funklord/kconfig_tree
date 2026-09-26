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
	# check once read only values in .config form (CONFIG_A3=4096), so a
	# doc in the tool's own tree form had its glyphs updated in silence.
	# The glyph now carries the value the check compares.
	def test_value_change_in_tree_form_warns(self) -> None:
		tree = KernelTree(self)
		tree.run("--add-new-enabled")
		tree.write(".config", TREE_CONFIG.replace("=4096", "=8192")
		                                 .replace("CONFIG_A2=m", "CONFIG_A2=y"))
		result = output(tree.run())
		self.assertIn("Value mismatch for CONFIG_A3: file has =4096, "
		              ".config has =8192", result)
		self.assertIn("Value mismatch for CONFIG_A2: file has =m", result)
		# Warned once: the rewritten glyph now agrees.
		self.assertNotIn("Value mismatch", output(tree.run()))

	# An option turned off or on is a change like any other, and the one
	# most worth a warning; the check used to skip anything unset.
	def test_option_turned_off_or_on_warns(self) -> None:
		tree = KernelTree(self)
		tree.run("--add-new")
		self.assertIn("[ ] Alpha four (A4)", tree.read("kconfig_doc.txt"))
		tree.write(".config", TREE_CONFIG
		           .replace("CONFIG_A1=y\n", "")
		           .replace("CONFIG_A3=4096\n", "")
		           .replace("# CONFIG_A4 is not set", "CONFIG_A4=y"))
		result = output(tree.run())
		self.assertIn("Value mismatch for CONFIG_A1: file has =y, "
		              ".config has not set", result)
		self.assertIn("Value mismatch for CONFIG_A3: file has =4096, "
		              ".config has not set", result)
		self.assertIn("Value mismatch for CONFIG_A4: file has not set, "
		              ".config has =y", result)
		self.assertNotIn("Value mismatch", output(tree.run()))

	# With .config unset, a value glyph's first value may be another
	# column's; a bool glyph's first character is always .config's.
	def test_two_column_unset_is_read_per_glyph_kind(self) -> None:
		tree = KernelTree(self, config="CONFIG_A1=y\n")
		tree.write(".config2", "CONFIG_A1=y\nCONFIG_A3=8192\n")
		tree.write("kconfig_doc.txt", "CONFIG_A1\nCONFIG_A3\n")
		tree.run()
		doc = tree.read("kconfig_doc.txt")
		self.assertIn("[**] Alpha one (A1)", doc)
		self.assertIn("[=8192] Alpha three (A3)", doc)
		self.assertNotIn("Value mismatch", output(tree.run()))
		tree.write(".config", "")
		result = output(tree.run())
		self.assertIn("Value mismatch for CONFIG_A1: file has =y", result)
		self.assertNotIn("CONFIG_A3", result)

	# A two-column value glyph lists distinct values in column order, so
	# .config's is the first: read as such it warns on a change there and
	# on nothing else.
	def test_two_column_value_glyph_reads_the_first_column(self) -> None:
		tree = KernelTree(self)
		tree.write(".config2", "CONFIG_A3=8192\n")
		tree.write("kconfig_doc.txt", "CONFIG_A3\n")
		tree.run()
		self.assertIn("[=4096/8192]", tree.read("kconfig_doc.txt"))
		self.assertNotIn("Value mismatch", output(tree.run()))
		tree.write(".config", TREE_CONFIG.replace("=4096", "=2048"))
		self.assertIn("file has =4096, .config has =2048", output(tree.run()))

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
	# a kernel update takes when it adds a menu. The notice was raised
	# during the walk, which never enters such a menu; it is now a pass of
	# its own over the whole tree.
	def test_untracked_option_in_an_undocumented_menu_gets_a_notice(
	        self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		result = output(tree.run("--no-doc"))
		self.assertIn("Active option not tracked: CONFIG_B1", result)
		self.assertEqual(result.count("not tracked: CONFIG_A2 "), 1)

	# With --add-new or --add-new-enabled the option is added instead of
	# noticed, in an undocumented menu as anywhere else.
	def test_add_flags_add_instead_of_noticing(self) -> None:
		for flag in ("--add-new", "--add-new-enabled"):
			with self.subTest(flag=flag):
				tree = KernelTree(self)
				tree.write("kconfig_doc.txt", "CONFIG_A1\n")
				result = output(tree.run(flag))
				self.assertIn("(B1)", tree.read("kconfig_doc.txt"))
				self.assertNotIn("not tracked", result)

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

	# The suppressed file used to be written without dead entries, so the
	# rationale vanished from both files under a warning that said it had
	# been converted. The stub must also survive the next read -- as the
	# file's only content, a plain comment attaches to nothing -- and turn
	# back into a suppressed option when the symbol returns.
	def test_commented_suppressed_symbol_keeps_its_comment(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		tree.write("kconfig_doc_suppressed.txt",
		           "CONFIG_A4 # rejected: breaks the thing\n")
		kconfig = tree.read("Kconfig")
		tree.drop_config("A4")
		first = output(tree.run())
		self.assertIn("CONFIG_A4", first)
		stub = tree.read("kconfig_doc_suppressed.txt")
		self.assertEqual(stub, "# CONFIG_A4 # rejected: breaks the thing\n")
		second = output(tree.run())
		self.assertEqual(tree.read("kconfig_doc_suppressed.txt"), stub)
		self.assertNotIn("CONFIG_A4", second)
		tree.write("Kconfig", kconfig)
		tree.run()
		self.assertIn("(A4) # rejected: breaks the thing",
		              tree.read("kconfig_doc_suppressed.txt"))

	def test_suppressed_stub_stays_under_its_anchor(self) -> None:
		tree = KernelTree(self)
		tree.write("kconfig_doc.txt", "CONFIG_A1\n")
		tree.write("kconfig_doc_suppressed.txt",
		           "CONFIG_A2 # two why\nCONFIG_A4 # rejected\n")
		tree.drop_config("A4")
		tree.run()
		lines = tree.read("kconfig_doc_suppressed.txt").splitlines()
		self.assertIn("(A2) # two why", lines[-2])
		self.assertEqual(lines[-1], "# CONFIG_A4 # rejected")
		tree.run()
		self.assertEqual(tree.read("kconfig_doc_suppressed.txt").splitlines(),
		                 lines)


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
	# ("Menu::if BAR (5)"), and occurrence tracking used to skip any key
	# with "::" in it -- so the warning could not fire where kernel
	# if-blocks nearly always are. It now reads the line, not the key.
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
