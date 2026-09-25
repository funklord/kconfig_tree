#!/usr/bin/env python3
"""
kconfig_tree.py - Kernel configuration documentation tool.

THREE-FILE MODEL
----------------
  .config                     Kernel truth (what is active)
  kconfig_doc.txt             User allowlist (what to show and document)
  kconfig_doc_suppressed.txt  User denylist (what to permanently hide)

MERGE RULES
-----------
  In doc                    -> emit, update glyph, warn on value mismatch
  In suppressed             -> never emit (unless --full)
  In neither, inactive      -> silent
  In neither, active        -> notice on stderr (suggest --add-new-enabled)
  In both (conflict)        -> doc wins, warn, remove from suppressed
  --add-new                 -> add all symbols in neither file
  --add-new-enabled         -> add only [*]/[M] symbols in neither file
  --full                    -> merge suppressed->doc, add all remaining

INPUT FORMATS ACCEPTED IN DOC / SUPPRESSED FILES
-------------------------------------------------
  Full doc:      +-[*] Some prompt (SYMBOL) # trailing comment
  Bare symbol:   CONFIG_SYMBOL  # optional trailing comment
  .config set:   CONFIG_SYMBOL=y
  .config unset: # CONFIG_SYMBOL is not set
  Pure comment:  # free text

FOUR COMMENT TYPES
------------------
  1. Trailing   - on the same line as a node after node info
  2. Anchored-above - directly below a node (no blank between)
                      indented to body column of that node
                      optional blank below is preserved
  3. Anchored-below - blank line above + node directly below (no blank between)
                      indented to body column of that node
                      blank above is preserved
  4. Freestanding   - blank above AND blank below
                      no tree indentation (written at column 0)
                      anchored to node above for stability
                      both surrounding blanks preserved
                      blanks between lines in the group collapsed to one

ORPHAN SPACING
--------------
  A blank line is inserted between two config/menuconfig nodes from
  completely different subtrees so the tree cannot be visually misread.

USAGE
-----
  python3 kconfig_tree.py --add-new-enabled   # first run, active options only
  python3 kconfig_tree.py --full              # first run, everything
  python3 kconfig_tree.py                     # normal update
  python3 kconfig_tree.py --show              # view coloured tree
  python3 kconfig_tree.py --emit-kconfig > my.config

  Suppress: move line from doc -> suppressed file
  Un-suppress: delete line from suppressed file

OPTIONS
-------
  --kconfig    PATH   Top-level Kconfig file        (default: Kconfig)
  --dotconfig  PATH   Kernel .config file           (default: .config)
  --doc        PATH   Doc file                      (default: kconfig_doc.txt)
  --suppressed PATH   Suppressed file               (default: kconfig_doc_suppressed.txt)
  --arch       ARCH   Architecture                  (default: arm64)
  --add-new           Add all symbols in neither file to doc
  --add-new-enabled   Add only active symbols in neither file to doc
  --full              Restore suppressed + add all remaining symbols
  --emit-kconfig      Print Linux .config format to stdout, then exit
  --show              Show coloured tree on stdout
  --depth      N      Max tree depth (with --show)
  --filter     WORD   Show subtrees containing WORD (with --show)
  --no-color          Disable ANSI colours
  --no-doc            Do not write any files this run
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# -- ANSI colour helpers --------------------------------------------------------

USE_COLOR = True

def _c(code: str, text: str) -> str:
	return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def green(t):   return _c("32", t)
def yellow(t):  return _c("33", t)
def cyan(t):    return _c("36", t)
def gray(t):    return _c("90", t)
def bold(t):    return _c("1",  t)
def magenta(t): return _c("35", t)

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
def strip_ansi(s: str) -> str:
	return ANSI_RE.sub("", s)

# -- Tree drawing ---------------------------------------------------------------

class GlyphSet:
	"""All user-visible non-content glyphs in one place.

    PIPE / BLANK : per-depth prefix units - must be equal width.
    TEE  / LAST  : per-node connectors    - must be equal width,
                   may differ in width from PIPE/BLANK.
    Swap the module-level G for ASCII mode via --ascii.
    """
	def __init__(self,
                 pipe="│",  tee="├", last="└", blank=" ",
                 menu="▶",  choice="◆", header="⚙", expand="->",
                 comment_bar="---"):
		self.pipe        = pipe
		self.tee         = tee
		self.last        = last
		self.blank       = blank
		self.menu        = menu
		self.choice      = choice
		self.header      = header
		self.expand      = expand
		self.comment_bar = comment_bar
		self._tree_chars: frozenset = frozenset(
            c for s in (pipe, tee, last, blank) for c in s)

	@property
	def connector_width(self) -> int:
		return len(self.tee)   # tee and last must be same width

	@property
	def pipe_width(self) -> int:
		return len(self.pipe)  # pipe and blank must be same width

	def strip_tree_prefix(self, line: str) -> tuple[str, str]:
		i = 0
		while i < len(line) and line[i] in self._tree_chars:
			i += 1
		return line[:i], line[i:]

	def depth_of(self, plain: str) -> int:
		prefix, _ = self.strip_tree_prefix(plain)
		total = len(prefix)
		cw    = self.connector_width
		pw    = self.pipe_width
		if total < cw:
			return 0
		return (total - cw) // pw

	def comment_indent(self, prefix: str, is_last: bool) -> str:
		"""Return indentation for a comment anchored to a node.
        '#' aligns with body text at column len(prefix)+connector_width.
        is_last=True  -> spaces     (no more siblings below)
        is_last=False -> pipe chars (more siblings follow)
        """
		cw        = self.connector_width
		pipe_char = self.pipe[0]
		cont      = (" " * cw) if is_last \
                    else (pipe_char + " " * (cw - 1))
		return prefix + cont


# Per-level tree indent width in characters.
# 1 = compact (default): each depth level is exactly 1 char wide.
# 2 = classic: pipe="| " blank="  " (one trailing space per level).
# Increase for wider, more readable trees on wide terminals.
TREE_INDENT_WIDTH = 1


def _make_glyphset(pipe_char: str, **kwargs) -> GlyphSet:
	"""Build a GlyphSet with pipe/blank scaled to TREE_INDENT_WIDTH."""
	n = max(1, TREE_INDENT_WIDTH)
	return GlyphSet(
        pipe=pipe_char + " " * (n - 1),
        blank=" " * n,
        **kwargs,
    )


# UTF-8 default - TEE/LAST are 1 char (no trailing -)
G = _make_glyphset("│", tee="├", last="└",
                   menu="▶",  choice="◆", header="⚙", expand="->",
                   comment_bar="---")

# ASCII alternative - activated with --ascii
_ASCII_GLYPHS = _make_glyphset("|", tee="+", last="+",
                               menu=">", choice="+", header="*", expand="->",
                               comment_bar="---")


def _strip_tree_prefix(line: str) -> tuple[str, str]:
	return G.strip_tree_prefix(line)

def _depth_of(plain: str) -> int:
	return G.depth_of(plain)

def _comment_indent(prefix: str, connector: str) -> str:
	return G.comment_indent(prefix, connector == G.last)


# -- KNode ---------------------------------------------------------------------

@dataclass
class KNode:
	kind: str         # menu | config | menuconfig | choice | comment | if
	name: str         # symbol name without CONFIG_ prefix, or ""
	prompt: str = ""
	type_: str = ""
	depends: str = ""
	children: list = field(default_factory=list)
	parent: Optional["KNode"] = field(default=None, repr=False)
	file: str = ""
	lineno: int = 0
	value: Optional[str] = None
	alt_values: list = field(default_factory=list)
	occurrence_key:  str = ""
	first_child_sym: str = ""

	def symbol(self) -> str:
		return f"CONFIG_{self.name}" if self.name else ""

	def is_active(self, col: int = 0) -> bool:
		v = self._col_value(col)
		if v is None: return False
		return v.strip().strip('"') not in ("", "n", "0")

	def _col_value(self, col: int) -> Optional[str]:
		if col == 0: return self.value
		idx = col - 1
		return self.alt_values[idx] if idx < len(self.alt_values) else None

	def any_col_active(self) -> bool:
		return any(self.is_active(c)
                   for c in range(1 + len(self.alt_values)))

	def _val_char(self, col: int) -> str:
		v = self._col_value(col)
		if v is None: return " "
		v = v.strip().strip('"')
		if v == "y": return "*"
		if v == "m": return "m"
		return " "

	def raw_glyph(self, num_cols: int = 1) -> str:
		if self.kind in ("menu", "choice", "comment", "if"):
			return ""
		if num_cols == 1:
			if self.value is None: return "[ ]"
			v = self.value.strip().strip('"')
			if v == "y":       return "[*]"
			if v == "m":       return "[M]"
			if v in ("n",""): return "[ ]"
			return f"[={v}]"
		# Multi-col: check for value-type (int/hex/string)
		vstr = [((self._col_value(c) or "").strip().strip('"'))
                for c in range(num_cols)]
		non_bool = any(s not in ('y','m','n','') for s in vstr)
		if non_bool:
			unique = list(dict.fromkeys(s for s in vstr if s and s not in ('n','')))
			return f"[={'/' .join(unique)}]" if unique else "[ ]"
		return "[" + "".join(self._val_char(c) for c in range(num_cols)) + "]"

	def coloured_glyph(self, num_cols: int = 1) -> str:
		if self.kind in ("menu", "choice", "comment", "if"):
			return ""
		if num_cols == 1:
			if self.value is None: return gray("[ ]")
			v = self.value.strip().strip('"')
			if v == "y":       return green("[*]")
			if v == "m":       return yellow("[M]")
			if v in ("n",""): return gray("[ ]")
			return cyan(f"[={v}]")
		vstr = [((self._col_value(c) or "").strip().strip('"'))
                for c in range(num_cols)]
		non_bool = any(s not in ('y','m','n','') for s in vstr)
		if non_bool:
			unique = list(dict.fromkeys(s for s in vstr if s and s not in ('n','')))
			return cyan(f"[={'/' .join(unique)}]") if unique else gray("[ ]")
		def cc(c):
			ch = self._val_char(c)
			return green("*") if ch=="*" else yellow("m") if ch=="m" else gray(" ")
		return "[" + "".join(cc(c) for c in range(num_cols)) + "]"

	def ancestry(self) -> list["KNode"]:
		chain: list["KNode"] = []
		p = self.parent
		while p:
			chain.append(p)
			p = p.parent
		chain.reverse()
		return chain


# -- Kconfig parser -------------------------------------------------------------

_BLANK_RE    = re.compile(r"^\s*$")
_HASH_RE     = re.compile(r"^\s*#")
_MENU_RE     = re.compile(r'^menu\s+"(.+)"')
_ENDMENU_RE  = re.compile(r"^endmenu\b")
_CONFIG_RE   = re.compile(r"^(config|menuconfig)\s+(\w+)")
_CHOICE_RE   = re.compile(r"^choice\b")
_ENDCHOICE   = re.compile(r"^endchoice\b")
_COMMENT2_RE = re.compile(r'^comment\s+"(.+)"')
_SOURCE_RE   = re.compile(r'^(?:source|rsource|osource|orsource)\s+"?([^\s"]+)"?')
_IF_RE       = re.compile(r"^if\s+(.+)")
_ENDIF_RE    = re.compile(r"^endif\b")
_TYPE_RE     = re.compile(r"^\s+(bool|tristate|int|hex|string)(?:\s+\"([^\"]*)\")?\s*$")
_PROMPT_RE   = re.compile(r'^\s+prompt\s+"([^"]*)"')
_DEPENDS_RE  = re.compile(r"^\s+depends\s+on\s+(.+)")
_HELP_RE     = re.compile(r"^\s+help\b|^\s+---help---")
_ATTR_RE     = re.compile(r"^\s+(default|select|imply|range|visible|option)\b")


class KconfigParser:
	def __init__(self, arch: str, kernel_root: Path):
		self.arch = arch
		self.root_path = kernel_root
		self.env = {"ARCH": arch, "SRCARCH": arch}
		self._parsed: set[str] = set()

	def _resolve(self, path_str: str, cur_dir: Path) -> Optional[Path]:
		path_str = re.sub(
            r"\$\((\w+)\)", lambda m: self.env.get(m.group(1), m.group(0)),
            path_str)
		for base in (cur_dir, self.root_path):
			p = base / path_str
			if p.exists():
				return p
		return None

	def parse(self, kconfig_file: Path) -> KNode:
		root = KNode(kind="menu", name="", prompt="Linux Kernel Configuration")
		self._parse_file(kconfig_file, root)
		return root

	def _parse_file(self, path: Path, parent: KNode):
		real = str(path.resolve())
		if real in self._parsed:
			return
		self._parsed.add(real)
		try:
			lines = path.read_text(errors="replace").splitlines()
		except OSError:
			parent.children.append(
                KNode(kind="comment", name="", prompt=f"[missing: {path}]"))
			return
		self._parse_lines(lines, path, parent)

	def _parse_lines(self, lines: list[str], path: Path, parent: KNode):
		i = 0
		stack: list[KNode] = [parent]
		cur_node: Optional[KNode] = None
		in_help = False
		help_indent = 0

		def cur() -> KNode:
			return stack[-1]

		while i < len(lines):
			line = lines[i].rstrip()
			i += 1

			if in_help:
				stripped = line.lstrip()
				indent = len(line) - len(stripped)
				if stripped == "" or indent > help_indent:
					continue
				in_help = False

			if _HASH_RE.match(line) or _BLANK_RE.match(line):
				cur_node = None
				continue

			m = _MENU_RE.match(line)
			if m:
				node = KNode(kind="menu", name="", prompt=m.group(1),
                             file=str(path), lineno=i)
				node.parent = cur()
				cur().children.append(node)
				stack.append(node)
				cur_node = None
				continue

			if _ENDMENU_RE.match(line):
				if len(stack) > 1:
					stack.pop()
				cur_node = None
				continue

			m = _CONFIG_RE.match(line)
			if m:
				node = KNode(kind=m.group(1), name=m.group(2),
                             file=str(path), lineno=i)
				node.parent = cur()
				cur().children.append(node)
				cur_node = node
				continue

			if _CHOICE_RE.match(line):
				node = KNode(kind="choice", name="", prompt="(choice)",
                             file=str(path), lineno=i)
				node.parent = cur()
				cur().children.append(node)
				stack.append(node)
				cur_node = node
				continue

			if _ENDCHOICE.match(line):
				if len(stack) > 1:
					stack.pop()
				cur_node = None
				continue

			m = _COMMENT2_RE.match(line)
			if m:
				node = KNode(kind="comment", name="", prompt=m.group(1),
                             file=str(path), lineno=i)
				node.parent = cur()
				cur().children.append(node)
				cur_node = None
				continue

			m = _SOURCE_RE.match(line)
			if m:
				src = self._resolve(m.group(1), path.parent)
				if src:
					self._parse_file(src, cur())
				cur_node = None
				continue

			m = _IF_RE.match(line)
			if m and not line.strip().startswith("default"):
				# Strip inline Kconfig comment from expression
				# e.g. "if !KMSAN # avoid false positives" -> "if !KMSAN"
				import re as _re
				_expr = _re.sub(r'\s*#.*$', '', m.group(1)).strip()
				node = KNode(kind="if", name="",
                             prompt=f"if {_expr}",
                             file=str(path), lineno=i)
				node.parent = cur()
				cur().children.append(node)
				stack.append(node)
				cur_node = None
				continue

			if _ENDIF_RE.match(line):
				if len(stack) > 1:
					stack.pop()
				cur_node = None
				continue

			if cur_node is None:
				continue

			m = _TYPE_RE.match(line)
			if m:
				cur_node.type_ = m.group(1)
				if m.group(2):
					cur_node.prompt = m.group(2)
				continue

			m = _PROMPT_RE.match(line)
			if m:
				cur_node.prompt = m.group(1)
				continue

			m = _DEPENDS_RE.match(line)
			if m:
				cur_node.depends = m.group(1).strip()
				continue

			if _HELP_RE.match(line):
				in_help = True
				help_indent = len(line) - len(line.lstrip()) + 1
				continue

			_ATTR_RE.match(line)


# -- .config loader -------------------------------------------------------------

def load_dotconfig(path: Path) -> dict[str, str]:
	cfg: dict[str, str] = {}
	if not path.exists():
		print(f"WARNING: {path} not found - values will show as unset.",
              file=sys.stderr)
		return cfg
	for line in path.read_text(errors="replace").splitlines():
		line = line.strip()
		if line.startswith("#"):
			m = re.match(r"#\s+(CONFIG_\w+)\s+is not set", line)
			if m:
				cfg[m.group(1)] = "n"
			continue
		m = re.match(r"(CONFIG_\w+)=(.*)", line)
		if m:
			cfg[m.group(1)] = m.group(2)
	return cfg


def annotate_tree(node: KNode, cfg: dict[str, str], col: int = 0):
	if node.name:
		v = cfg.get(node.symbol())
		if col == 0:
			node.value = v
		else:
			while len(node.alt_values) < col:
				node.alt_values.append(None)
			node.alt_values[col - 1] = v
	for c in node.children:
		annotate_tree(c, cfg, col)


def build_knode_index(node: KNode,
                      idx: Optional[dict] = None) -> dict[str, KNode]:
	if idx is None:
		idx = {}
	sym = node.symbol()
	if sym and sym not in idx:   # keep first definition; Kconfig allows
		idx[sym] = node          # config + menuconfig with the same name
	for c in node.children:
		build_knode_index(c, idx)
	return idx


def _first_config_sym(node: KNode) -> str:
	for child in node.children:
		if child.kind in ("config", "menuconfig") and child.name:
			return child.symbol()
		sym = _first_config_sym(child)
		if sym: return sym
	return ""


def assign_if_keys(root: KNode) -> dict[str, int]:
	"""Assign occurrence_key to every 'if' node. Returns {expr: count}.
    First occurrence: 'if EXPR'; Nth (N>=2): 'if EXPR (N)'.
    """
	counts: dict[str, int] = {}
	def _walk(node: KNode):
		if node.kind == "if" and node.prompt:
			expr = node.prompt.strip()
			n = counts.get(expr, 0) + 1
			counts[expr] = n
			node.occurrence_key  = f"__if__{expr}__#{n}"
			node.first_child_sym = _first_config_sym(node)
		for child in node.children: _walk(child)
	_walk(root)
	def _finalise(node: KNode):
		if node.kind == "if" and node.occurrence_key.startswith("__if__"):
			parts = node.occurrence_key[len("__if__"):].rsplit("__#", 1)
			expr, n = parts[0], int(parts[1])
			node.occurrence_key = expr if n == 1 else f"{expr} ({n})"
		for child in node.children: _finalise(child)
	_finalise(root)
	return counts


_IF_OCC_RE = re.compile(r"^(if .+) \((\d+)\)$")


def _parse_if_occurrence(key: str) -> tuple[str, int]:
	m = _IF_OCC_RE.match(key)
	return (m.group(1), int(m.group(2))) if m else (key, 1)


def build_struct_knode_index(
        node: KNode,
        idx:  Optional[dict] = None) -> dict[str, list[KNode]]:
	"""Build own_prompt -> [KNode, ...] index for structural nodes.
    Used by DocFileParser to resolve single-level prompt strings to the
    correct two-level key via _struct_node_key, even when the parent node
    is absent from the doc/suppressed file.
    Skips the synthetic root (parent == None).
    """
	if idx is None:
		idx = {}
	if (node.kind in ("menu", "choice", "comment")
            and node.prompt and node.parent is not None):
		idx.setdefault(node.prompt.strip(), []).append(node)
	elif node.kind == "if" and node.occurrence_key and node.parent is not None:
		idx.setdefault(node.occurrence_key, []).append(node)
	for c in node.children:
		build_struct_knode_index(c, idx)
	return idx


# -- Doc / suppressed file parser -----------------------------------------------
#
# Four comment types (stored in RawEntry):
#   1. Trailing   - trailing_comment field (same line, already handled)
#   2. Anchored-above (post, no blank above)  -> post_groups, not freestanding
#   3. Anchored-below (pre,  blank above)     -> pre_group
#   4. Freestanding (post, blank above+below) -> post_groups, is_freestanding()

_DOC_SYMBOL_RE   = re.compile(r"\((\w+)\)\s*(?:->|→)?\s*(?:#.*)?$")
_SYMBOL_TAG_RE   = re.compile(r"\((\w+)\)")  # tag only, no trailing
_TRAILING_CMT_RE = re.compile(r" (#.*)$")
_BARE_SYMBOL_RE  = re.compile(r"^(CONFIG_\w+)\s*(?:#.*)?$")
_BARE_SYM_RE     = re.compile(r"^\w+$")  # single bare identifier
_DOTCFG_SET_RE   = re.compile(r"^(CONFIG_\w+)=(.*)$")
_DOTCFG_UNSET_RE = re.compile(r"^#\s+(CONFIG_\w+)\s+is not set\s*$")
def _build_struct_leader() -> re.Pattern:
	m  = re.escape(G.menu)
	c  = re.escape(G.choice)
	cb = re.escape(G.comment_bar)
	return re.compile(rf"^({m} |{c} |{cb} |\[if )")

_STRUCT_LEADER = _build_struct_leader()
_KCONFIG_CMT_RE  = re.compile(r"^--- (.+) ---$")


@dataclass
class CommentGroup:
	"""A contiguous run of comment lines with surrounding blank info."""
	lines:       list[str]
	blank_above: bool
	blank_below: bool

	def is_freestanding(self) -> bool:
		"""Type 4: blank above AND blank below."""
		return self.blank_above and self.blank_below


@dataclass
class RawEntry:
	"""One logical item parsed from a doc/suppressed file."""
	symbol: str            # CONFIG_FOO or ""
	trailing_comment: str  # type-1, or ""
	file_value: str        # from =y / is-not-set, or ""
	comment_text: str = "" # own-prompt (later upgraded to two-level key)
	depth: int = 0           # tree depth of this line in the doc file
	blank_before: bool = False
	pre_group:  Optional[CommentGroup] = None  # type-3
	post_groups: list[CommentGroup] = field(default_factory=list)  # type-2 and/or type-4
	anchor_above_sym: str = ""  # for dead entries: last known symbol above in doc


def _extract_trailing(content: str) -> tuple[str, str]:
	"""Return (content_without_trailing_comment, trailing_comment).

    When a (SYMBOL) tag is present we only search for a trailing comment
    AFTER the tag, so that '#' characters inside the prompt text
    (e.g. 'Kernel support for scripts starting with #!') are not mistaken
    for a trailing comment and the (SYMBOL) tag is not swallowed.

    Uses _SYMBOL_TAG_RE (no trailing group) to find the tag position,
    then _TRAILING_CMT_RE on the text after the tag.
    """
	tag_match = _SYMBOL_TAG_RE.search(content)
	if tag_match:
		after = content[tag_match.end():]
		m = _TRAILING_CMT_RE.search(after)
		if m:
			return content[:tag_match.end() + m.start()], m.group(1)
		return content, ""
	m = _TRAILING_CMT_RE.search(content)
	if m:
		return content[:m.start()], m.group(1)
	return content, ""


def _struct_prompt_key(content_clean: str) -> str:
	"""Normalise a structural doc line's content to the key used by the
    Merger (= KNode.prompt.strip()) so that struct_index lookups match.

    Display format -> KNode.prompt:
      > General setup          -> General setup
      * Kernel compression     -> (choice)
      --- Some comment ---     -> Some comment
      [if CEPH_FS]             -> if CEPH_FS   (must keep 'if ' prefix)
    """
	# [if EXPR] blocks: strip enclosing brackets, keep 'if EXPR' intact
	# so the key matches KNode.prompt which is stored as 'if EXPR'.
	if content_clean.startswith("[if "):
		inner = content_clean[1:]  # strip leading [
		if inner.endswith("]"):
			inner = inner[:-1]     # strip trailing ]
		return inner.strip()
	s  = _STRUCT_LEADER.sub("", content_clean, count=1)
	cb = G.comment_bar
	import re as _re
	m  = _re.match(rf"^{_re.escape(cb)} (.+) {_re.escape(cb)}$",
                   content_clean)
	if m:
		return m.group(1).strip()
	if s.endswith(" " + cb):
		s = s[:-(len(cb) + 1)]
	return s.strip()


def _struct_node_key(node: KNode) -> str:
	if node.kind == "if":
		return node.occurrence_key
	own = node.prompt.strip() if node.prompt else ""
	p = node.parent
	while p:
		if p.parent is None:  # stop before synthetic root
			break
		if p.kind in ("menu", "choice", "if", "comment") and p.prompt:
			return f"{p.prompt.strip()}::{own}"
		p = p.parent
	return own


class DocFileParser:
	"""
    Parses a doc or suppressed file into RawEntry objects.

    Public:
      symbol_index : dict[str, RawEntry]
      struct_index : dict[str, RawEntry]
      ordered      : list[RawEntry]
      warnings     : list[str]
    """

	def __init__(self, path: Path, knode_index: dict[str, KNode],
                 struct_knode_index: Optional[dict[str, list[KNode]]] = None,
                 is_suppressed: bool = False):
		self.path = path
		self.knode_index = knode_index
		self.is_suppressed = is_suppressed
		self.struct_knode_index: dict[str, list[KNode]] = struct_knode_index or {}
		self.symbol_index: dict[str, RawEntry] = {}
		self.struct_index: dict[str, RawEntry] = {}
		self.ordered: list[RawEntry] = []
		self.dead_entries: list[RawEntry] = []  # unknown symbols, kept as comments
		self.max_if_occurrences: dict[str, int] = {}
		self.warnings: list[str] = []
		self._parse()

	def _parse(self):
		if not self.path.exists():
			return

		raw_lines = self.path.read_text(errors="replace").splitlines()

		# -- Pass 1: classify every line ---------------------------------------

		items: list[tuple] = []   # ("option", RawEntry) | ("comment", str) | ("blank",)

		for raw in raw_lines:
			line = strip_ansi(raw).rstrip()

			if not line:
				items.append(("blank",))
				continue

			m = _DOTCFG_UNSET_RE.match(line)
			if m:
				items.append(("option",
                              RawEntry(symbol=m.group(1),
                                       trailing_comment="",
                                       file_value="n")))
				continue

			_prefix, content = _strip_tree_prefix(line)

			if content.startswith("#"):
				items.append(("comment", content))
				continue

			content_clean, trailing = _extract_trailing(content)
			content_clean = content_clean.strip()

			m = _DOTCFG_SET_RE.match(content_clean)
			if m:
				items.append(("option",
                              RawEntry(symbol=m.group(1),
                                       trailing_comment=trailing,
                                       file_value=m.group(2))))
				continue

			m = _BARE_SYMBOL_RE.match(content_clean)
			if m and not _DOC_SYMBOL_RE.search(content_clean):
				items.append(("option",
                              RawEntry(symbol=m.group(1),
                                       trailing_comment=trailing,
                                       file_value="")))
				continue

			# Check structural leader BEFORE _DOC_SYMBOL_RE so that
			# e.g. "* Link Time Optimization (LTO)" is never mis-parsed
			# as CONFIG_LTO.
			if _STRUCT_LEADER.match(content_clean):
				prompt_key = _struct_prompt_key(content_clean)
				if prompt_key:
					items.append(("option",
                                  RawEntry(symbol="",
                                           trailing_comment=trailing,
                                           file_value="",
                                           comment_text=prompt_key,
                                           depth=_depth_of(line))))
				continue

			ms = _DOC_SYMBOL_RE.search(content_clean)
			if ms:
				items.append(("option",
                              RawEntry(symbol=f"CONFIG_{ms.group(1)}",
                                       trailing_comment=trailing,
                                       file_value="")))
				continue

			prompt_key = _struct_prompt_key(content_clean)
			if prompt_key:
				items.append(("option",
                              RawEntry(symbol="",
                                       trailing_comment=trailing,
                                       file_value="",
                                       comment_text=prompt_key,
                                       depth=_depth_of(line))))
				continue

			items.append(("comment", content))

		# -- Pass 2: resolve comment anchoring ---------------------------------
		#
		# For each contiguous comment run, classify by context:
		#
		#   blank_above=F, next_is_option=-  -> type 2 (anchored-above / post)
		#   blank_above=T, next_is_option=T  -> type 3 (anchored-below / pre)
		#   blank_above=T, blank_below=T     -> type 4 (freestanding / post)
		#
		# Note: type 4 wins over type 3 - if there's a blank below too,
		# it is freestanding regardless of what follows after the blank.

		n = len(items)
		i = 0
		option_entries: list[RawEntry] = []

		while i < n:
			kind = items[i][0]

			if kind == "blank":
				i += 1
				continue

			if kind == "option":
				entry = items[i][1]
				entry.blank_before = (i > 0 and items[i - 1][0] == "blank")
				option_entries.append(entry)
				i += 1
				continue

			# comment run
			cmt_start = i
			cmt_lines: list[str] = []
			while i < n and items[i][0] == "comment":
				cmt_lines.append(items[i][1])
				i += 1

			blank_above    = cmt_start > 0 and items[cmt_start - 1][0] == "blank"
			next_is_option = i < n and items[i][0] == "option"
			blank_below    = i < n and items[i][0] == "blank"

			group = CommentGroup(lines=cmt_lines,
                                 blank_above=blank_above,
                                 blank_below=blank_below)

			if blank_above and blank_below:
				# Type 4: freestanding - append to post_groups of option above
				if option_entries:
					option_entries[-1].post_groups.append(group)
				# else: before any option, discard

			elif blank_above and next_is_option:
				# Type 3: anchored-below - attach as pre_group to option below
				next_entry = items[i][1]
				next_entry.pre_group = group

			else:
				# Type 2: anchored-above - append to post_groups of option above
				if option_entries:
					option_entries[-1].post_groups.append(group)
				# else: discard

		# -- Pass 3a: index all CONFIG_ symbols first -------------------------
		# We do symbols before structural nodes so that during structural
		# disambiguation (Pass 3b) we can check which candidate's subtree
		# contains the symbols that appear in this file.  This is much more
		# reliable than using only the nearest preceding symbol, because a
		# structural node's children in the file are the definitive proof of
		# which Kconfig subtree it belongs to.

		for entry in option_entries:
			if not entry.symbol:
				continue
			if entry.symbol not in self.symbol_index:
				self.symbol_index[entry.symbol] = entry
				self.ordered.append(entry)
			else:
				self.warnings.append(
                    f"Duplicate symbol {entry.symbol} in "
                    f"{self.path.name} - keeping first")

		# -- Pass 3b: index structural entries ---------------------------------
		# Now that symbol_index is complete, disambiguate duplicate structural
		# prompts by finding which candidate's subtree contains the most
		# (or any) symbols already present in symbol_index.

		def _desc_in_index(node: KNode) -> set[str]:
			"""Return symbols in self.symbol_index that are descendants of node."""
			result: set[str] = set()
			for child in node.children:
				sym = child.symbol()
				if sym and sym in self.symbol_index:
					result.add(sym)
				result.update(_desc_in_index(child))
			return result

		def _is_in_subtree(ancestor: KNode, node: KNode) -> bool:
			"""True if ancestor is node or any ancestor of node."""
			p = node
			while p:
				if p is ancestor:
					return True
				p = p.parent
			return False

		depth_stack:    list[tuple[int, str]] = []
		last_known_sym: str = ""  # nearest preceding CONFIG_ in file order

		for entry in option_entries:
			if entry.symbol:
				# Track last seen symbol for sibling-disambiguation fallback.
				last_known_sym = entry.symbol
				continue
			if not entry.comment_text:
				continue

			own = entry.comment_text
			d   = entry.depth
			# Pop entries at same or deeper depth
			while depth_stack and depth_stack[-1][0] >= d:
				depth_stack.pop()

			# Skip if-entries in suppressed file entirely:
			# they are written for display context only and carry
			# no suppression effect.  Comments on them are dropped.
			if self.is_suppressed and own.startswith("if ") and "::" not in own:
				# Warn if the user had a comment on this if-entry
				_has_cmt = (entry.trailing_comment
                            or entry.pre_group
                            or any(not g.is_freestanding()
                                   for g in entry.post_groups))
				if _has_cmt:
					self.warnings.append(
                        f"Comment on [{own}] in suppressed file was dropped "
                        f"({self.path.name}:{entry.depth}) -- "
                        f"if-entries are display-only context; "
                        f"move the comment to the controlling config option")
				# Still track max occurrence for change detection so
				# auto-derived if-context in suppressed file does not
				# trigger false NOTICE messages on every run.
				if own.startswith("if ") and "::" not in own:
					_base, _n = _parse_if_occurrence(own)
					self.max_if_occurrences[_base] = max(
                        self.max_if_occurrences.get(_base, 0), _n)
				depth_stack.append((d, own))
				continue

			matching = self.struct_knode_index.get(own, [])
			if len(matching) == 1:
				# Unambiguous: use Kconfig tree directly.
				key = _struct_node_key(matching[0])
			elif len(matching) > 1:
				# Multiple candidates with the same prompt.
				# Strategy 1: descendant count - the candidate whose subtree
				# contains the most indexed symbols wins.  Works when the
				# structural node's children are present in the file.
				scored = [(len(_desc_in_index(m)), m) for m in matching]
				scored.sort(key=lambda x: x[0], reverse=True)
				resolved = None
				if scored[0][0] > 0 and (
                        len(scored) < 2 or scored[0][0] > scored[1][0]):
					resolved = scored[0][1]

				# Strategy 2: sibling/ancestor check using the nearest
				# preceding CONFIG_ symbol.  Works when children are absent
				# from the file (e.g. a childless [if GREYBUS]) but a sibling
				# config (e.g. GREYBUS itself) precedes it.
				if resolved is None and last_known_sym:
					ref = self.knode_index.get(last_known_sym)
					if ref:
						sib = [
                            m for m in matching
                            if m.parent is not None
                            and _is_in_subtree(m.parent, ref)
                        ]
						if len(sib) == 1:
							resolved = sib[0]

				# Strategy 3: depth-stack parent prompt
				if resolved is None:
					parent_prompt = depth_stack[-1][1] if depth_stack else ""
					cand_key = f"{parent_prompt}::{own}" if parent_prompt else own
					cand_matches = [m for m in matching
                                    if _struct_node_key(m) == cand_key]
					if len(cand_matches) == 1:
						resolved = cand_matches[0]

				key = (_struct_node_key(resolved) if resolved
                       else (f"{depth_stack[-1][1]}::{own}"
                             if depth_stack else own))
			else:
				# Unknown prompt: depth-stack fallback
				parent_prompt = depth_stack[-1][1] if depth_stack else ""
				key = f"{parent_prompt}::{own}" if parent_prompt else own

			entry.comment_text = key  # upgrade to two-level key in-place
			if key not in self.struct_index:
				self.struct_index[key] = entry
				self.ordered.append(entry)
			# Track highest occurrence number seen for change detection.
			# Only for genuine 'if' node keys - exclude two-level keys such as
			# "if EXPR::Child prompt" where the parent happens to be an if-block.
			if key.startswith("if ") and "::" not in key:
				base_expr, n = _parse_if_occurrence(key)
				self.max_if_occurrences[base_expr] = max(
                    self.max_if_occurrences.get(base_expr, 0), n)
			depth_stack.append((d, own))

		# -- Pass 4: validate against Kconfig tree -----------------------------
		# Unknown symbols:
		#   - with attached comments -> moved to dead_entries, emitted inline as
		#     type-2 anchored comments beside their preceding known option
		#   - without comments       -> silently dropped (no warning, no stub)

		unknown_syms = {sym for sym in self.symbol_index
                        if sym not in self.knode_index}

		# Record anchor: last known symbol before each unknown entry in doc order
		last_known = ""
		for entry in self.ordered:
			if entry.symbol and entry.symbol not in unknown_syms:
				last_known = entry.symbol
			elif entry.symbol in unknown_syms:
				entry.anchor_above_sym = last_known

		for sym in unknown_syms:
			entry = self.symbol_index[sym]
			has_comments = bool(entry.pre_group or entry.post_groups
                                or entry.trailing_comment)
			if has_comments:
				self.dead_entries.append(entry)
				self.warnings.append(
                    f"Symbol {sym} in {self.path.name} not found in "
                    f"Kconfig tree - converted to inline comment")
			# else: silently dropped
			del self.symbol_index[sym]
		self.ordered = [e for e in self.ordered if e.symbol not in unknown_syms]

		# -- Pass 5: value mismatch warnings -----------------------------------

		for sym, entry in self.symbol_index.items():
			if not entry.file_value:
				continue
			node = self.knode_index.get(sym)
			if node is None:
				continue
			live = (node.value or "").strip().strip('"')
			fv   = entry.file_value.strip().strip('"')
			if fv != live and fv not in ("", "n") and live not in ("", "n"):
				self.warnings.append(
                    f"Value mismatch for {sym}: "
                    f"file has ={fv}, .config has ={live} - using .config value")


# -- Ancestry / orphan-blank ---------------------------------------------------

def _needs_blank(prev_node: Optional[KNode], curr_node: KNode) -> bool:
	"""
    True when a blank should be inserted between prev and curr because
    they come from completely different subtrees.
    Only fires for config/menuconfig nodes - structural nodes never set
    prev_node, so they can't trigger orphan blanks.
    """
	if prev_node is None:
		return False
	if prev_node.kind not in ("config", "menuconfig"):
		return False
	if curr_node.kind not in ("config", "menuconfig"):
		return False
	prev_anc = set(id(n) for n in prev_node.ancestry())
	curr_anc = set(id(n) for n in curr_node.ancestry())
	shared = prev_anc & curr_anc
	non_root_shared = [n for n in prev_node.ancestry() + curr_node.ancestry()
                       if id(n) in shared and n.parent is not None]
	return len(non_root_shared) == 0


# -- Comment indentation helpers -----------------------------------------------



def _emit_comment_group(push_fn, push_blank_fn,
                        group: CommentGroup,
                        indent: str,
                        coloured: bool = True):
	"""
    Emit a comment group using push_fn(plain, coloured).
    Type 4 (freestanding): no indent, blanks above and below preserved,
                           single blank between lines if originally present.
    Types 2/3 (anchored):  use indent, no surrounding blanks added beyond
                           what post_blank / pre_blank says.
    """
	free = group.is_freestanding()

	if free:
		if group.blank_above:
			push_blank_fn()
		# Emit lines preserving inter-line blanks (collapsed to one)
		last_was_blank = False
		for line in group.lines:
			# Freestanding: no indent prefix
			push_fn(line, gray(line) if coloured else line)
			last_was_blank = False
		if group.blank_below:
			push_blank_fn()
	else:
		# Anchored (type 2 or 3): emit with indent, no surrounding blanks
		for line in group.lines:
			plain    = f"{indent}{line}"
			coloured_line = f"{indent}{gray(line)}" if coloured else plain
			push_fn(plain, coloured_line)


# -- Line renderers -------------------------------------------------------------

def _plain_body(node: KNode, num_cols: int = 1) -> str:
	glyph = node.raw_glyph(num_cols)
	if node.kind == "menu":
		return f"{G.menu} {node.prompt}" if node.prompt else f"{G.menu} (menu)"
	if node.kind == "choice":
		return f"{G.choice} {node.prompt or '(choice)'}"
	if node.kind == "comment":
		return f"--- {node.prompt} ---"
	if node.kind == "if":
		return f"[{node.occurrence_key or node.prompt}]"
	prompt = node.prompt or node.name
	tag    = f" ({node.name})" if node.name else ""
	return f"{glyph} {prompt}{tag}" if glyph else f"{prompt}{tag}"


def _colour_body(node: KNode, num_cols: int = 1) -> str:
	glyph = node.coloured_glyph(num_cols)
	if node.kind == "menu":
		p = f"{G.menu} {node.prompt}" if node.prompt else f"{G.menu} (menu)"
		return bold(cyan(p))
	if node.kind == "choice":
		return bold(f"{G.choice} {node.prompt or '(choice)'}")
	if node.kind == "comment":
		return gray(f"--- {node.prompt} ---")
	if node.kind == "if":
		return gray(f"[{node.occurrence_key or node.prompt}]")
	prompt = node.prompt or node.name
	pstr   = bold(prompt) if node.is_active(0) else gray(prompt)
	tag    = gray(f" ({node.name})") if node.name else ""
	return f"{glyph} {pstr}{tag}" if glyph else f"{pstr}{tag}"


def _plain_line(node: KNode, prefix: str, connector: str,
                trailing: str = "", num_cols: int = 1) -> str:
	tc = f" {trailing}" if trailing else ""
	return f"{prefix}{connector}{_plain_body(node, num_cols)}{tc}"


def _colour_line(node: KNode, prefix: str, connector: str,
                 trailing: str = "", num_cols: int = 1) -> str:
	tc = f" {gray(trailing)}" if trailing else ""
	return f"{prefix}{connector}{_colour_body(node, num_cols)}{tc}"


# -- menuconfig+if folding helpers --------------------------------------------

def _folded_ifs(node: KNode) -> list[KNode]:
	"""Return ALL [if SYM] siblings to fold visually under this menuconfig.

    A sibling if-block is foldable when its expression is a single bare
    symbol matching node.name.  There may be more than one such block
    (e.g. [if USB] and [if USB (2)] under the same parent).

    Doc file only: suppressed file always uses flat Kconfig structure.
    """
	if node.kind != "menuconfig" or not node.name:
		return []
	parent = node.parent
	if parent is None:
		return []
	return [
        c for c in parent.children
        if c is not node
        and c.kind == "if"
        and c.prompt.startswith("if ")
        and _BARE_SYM_RE.match(c.prompt[3:].strip())
        and c.prompt[3:].strip() == node.name
    ]


def _is_folded_if(child: KNode, parent_node: KNode) -> bool:
	"""True if child is an [if SYM] that should be folded under a menuconfig
    sibling with the same symbol name.
    """
	if child.kind != "if" or not child.prompt.startswith("if "):
		return False
	sym = child.prompt[3:].strip()
	if not _BARE_SYM_RE.match(sym):
		return False
	return any(c.kind == "menuconfig" and c.name == sym
               for c in parent_node.children if c is not child)


def _first_doc_descendant(node: KNode, eff_doc: dict) -> str:
	"""Return the first doc-tracked symbol in node's subtree (incl. folded
    if-block siblings for menuconfig nodes), or '' if none found.
    Used by conflict resolution to enforce the invariant that a suppressed
    symbol must not have doc-tracked descendants.
    """
	sym = node.symbol()
	if sym and sym in eff_doc:
		return sym
	for child in node.children:
		found = _first_doc_descendant(child, eff_doc)
		if found:
			return found
	if node.kind == "menuconfig":
		for f in _folded_ifs(node):
			found = _first_doc_descendant(f, eff_doc)
			if found:
				return found
	return ""


def _subtree_has_suppressed(node: KNode, symbol_index: dict) -> bool:
	"""True if any config/menuconfig in node's subtree is in symbol_index."""
	sym = node.symbol()
	if sym and sym in symbol_index:
		return True
	return any(_subtree_has_suppressed(c, symbol_index)
               for c in node.children)


# -- OutputLine -----------------------------------------------------------------

@dataclass
class OutputLine:
	plain:      str
	coloured:   str
	symbol:     str  = ""
	is_blank:   bool = False
	is_notice:  bool = False
	is_warning: bool = False


def _blank_line() -> OutputLine:
	return OutputLine(plain="", coloured="", is_blank=True)


# -- Merger ---------------------------------------------------------------------

class Merger:
	def __init__(
        self,
        root:         KNode,
        doc:          DocFileParser,
        sup:          DocFileParser,
        knode_index:  dict[str, KNode],
        add_new:        bool = False,
        add_new_en:     bool = False,
        add_new_en_col: int  = 0,
        full:           bool = False,
        num_cols:       int  = 1,
    ):
		self.root          = root
		self.doc           = doc
		self.sup           = sup
		self.knode_index   = knode_index
		self.add_new       = add_new
		self.add_new_en    = add_new_en
		self.add_new_en_col = add_new_en_col
		self.full          = full
		self.num_cols      = num_cols

		self.output:   list[OutputLine] = []
		self.notices:  list[str] = []
		self.warnings: list[str] = list(doc.warnings) + list(sup.warnings)

		self._emitted:              set[str] = set()
		self._struct_emitted:       set[int] = set()
		# IDs of structural nodes whose header line was actually written.
		# Used to detect when a parent was suppressed (header skipped)
		# so we know when to insert a blank before a child.
		self._struct_header_emitted: set[int] = set()
		# Parent of the last emitted config/menuconfig node.
		# Used so we only blank on the FIRST child of each new suppressed-
		# parent group, not between every sibling.
		self._prev_emitted_parent: Optional[KNode] = None

		self._eff_doc:    dict[str, RawEntry] = dict(doc.symbol_index)
		self._eff_struct: dict[str, RawEntry] = dict(doc.struct_index)
		if full:
			for sym, entry in sup.symbol_index.items():
				if sym not in self._eff_doc:
					self._eff_doc[sym] = entry
					self.notices.append(f"Restored from suppressed: {sym}")
			for key, entry in sup.struct_index.items():
				if key not in self._eff_struct:
					self._eff_struct[key] = entry

		# Build anchor map for dead entries (unknown symbols with comments)
		all_dead = list(doc.dead_entries)
		if full:
			all_dead += sup.dead_entries
		self._anchor_map:   dict[str, list[RawEntry]] = {}
		self._rootless_dead: list[RawEntry] = []
		for dead_entry in all_dead:
			anchor = dead_entry.anchor_above_sym
			if anchor:
				self._anchor_map.setdefault(anchor, []).append(dead_entry)
			else:
				self._rootless_dead.append(dead_entry)


	# -- internal push helpers --------------------------------------------------

	def _push(self, plain: str, coloured: str, symbol: str = "",
              is_notice: bool = False, is_warning: bool = False):
		self.output.append(OutputLine(plain=plain, coloured=coloured,
                                      symbol=symbol, is_notice=is_notice,
                                      is_warning=is_warning))

	def _push_blank(self):
		if self.output and not self.output[-1].is_blank:
			self.output.append(_blank_line())

	def _push_comment(self, plain: str, coloured_text: str):
		self._push(plain, coloured_text)

	# -- emit a CommentGroup ----------------------------------------------------

	def _emit_group(self, group: Optional[CommentGroup],
                    indent: str, is_pre: bool = False):
		"""
        Emit a comment group.
          is_pre=True  -> type 3 (anchored-below): blank above preserved
          is_pre=False -> type 2 (anchored-above) or type 4 (freestanding)
        """
		if group is None:
			return

		if group.is_freestanding():
			# Type 4: no indent, blank above and below
			if group.blank_above:
				self._push_blank()
			for line in group.lines:
				self._push(line, gray(line))
			if group.blank_below:
				self._push_blank()

		elif is_pre:
			# Type 3: blank above preserved, then indented lines
			if group.blank_above:
				self._push_blank()
			for line in group.lines:
				self._push(f"{indent}{line}", f"{indent}{gray(line)}")

		else:
			# Type 2: indented lines, optional blank below
			for line in group.lines:
				self._push(f"{indent}{line}", f"{indent}{gray(line)}")
			if group.blank_below:
				self._push_blank()

	# -- decision helpers -------------------------------------------------------

	def _in_eff_doc(self, sym: str) -> bool:
		return sym in self._eff_doc

	def _in_sup(self, sym: str) -> bool:
		return sym in self.sup.symbol_index

	def _should_emit(self, node: KNode) -> bool:
		sym = node.symbol()
		if self.full:
			return True
		if self._in_eff_doc(sym):
			return True
		if self._in_sup(sym):
			return False
		if self.add_new:
			return True
		if self.add_new_en and node.is_active(self.add_new_en_col):
			return True
		return False

	def _entry(self, sym: str) -> Optional[RawEntry]:
		return self._eff_doc.get(sym) or self.sup.symbol_index.get(sym)

	def _struct_entry(self, node: KNode) -> Optional[RawEntry]:
		key      = _struct_node_key(node)
		doc_e    = self._eff_struct.get(key)
		sup_e    = self.sup.struct_index.get(key)
		# Warn when both files have a comment and they differ
		if (doc_e and sup_e
                and doc_e.trailing_comment
                and sup_e.trailing_comment
                and doc_e.trailing_comment != sup_e.trailing_comment):
			self.warnings.append(
                f"Comment conflict on structural node '{key}': "
                f"doc has {doc_e.trailing_comment!r}, "
                f"suppressed has {sup_e.trailing_comment!r} -- doc wins")
		return doc_e or sup_e

	def _struct_has_desc_comment(self, node: KNode) -> bool:
		"""True if this structural node has a descriptive comment
        (types 1, 2, 3 — trailing, anchored-above, anchored-below).
        Freestanding type-4 comments do NOT count: they belong to no
        specific node and are positional best-effort.
        Used for the comment exception: keep a childless structural
        node visible when the user has annotated it.
        """
		entry = self._struct_entry(node)
		if not entry:
			return False
		if entry.trailing_comment:
			return True
		if entry.pre_group:
			return True
		return any(not g.is_freestanding() for g in entry.post_groups)

	# _is_struct_suppressed removed: structural nodes are never hidden
	# from the doc by the suppressed file.  Visibility is controlled
	# purely by whether there are visible children or a descriptive comment.

	# -- tree walk --------------------------------------------------------------

	def run(self) -> list[OutputLine]:
		hdr = f"{G.header} {self.root.prompt or 'Linux Kernel Configuration'}"
		self._push(hdr, bold(cyan(hdr)))
		# Mark the root as having its header emitted so root-level
		# config nodes don't trigger the suppressed-parent blank.
		self._struct_header_emitted.add(id(self.root))
		self._check_conflicts()
		# Emit rootless dead entries (no known anchor above them) as plain
		# top-level comments before the tree, so they are visible and preserved.
		for dead_entry in self._rootless_dead:
			self._emit_dead_comment(dead_entry, prefix="", connector="")
		self._recurse(self.root, prefix="")
		return self.output

	def _emit_dead_comment(self, entry: RawEntry,
                           prefix: str, connector: str):
		"""Emit an unknown symbol as a type-2 anchored comment at the
        indentation of its anchor option (prefix + connector-width spaces)."""
		indent = _comment_indent(prefix, connector)
		sym    = entry.symbol
		tc     = f" {entry.trailing_comment}" if entry.trailing_comment else ""
		# Pre-group (if any): type-3 style, same indent
		if entry.pre_group:
			for line in entry.pre_group.lines:
				self._push(f"{indent}{line}", f"{indent}{gray(line)}")
		# The symbol itself as a commented-out line
		stub_p = f"{indent}# {sym}{tc}"
		stub_c = f"{indent}{magenta(f'# {sym}{tc}')}"
		self._push(stub_p, stub_c)
		# Post-groups: type-2 style, same indent, no surrounding blanks
		for g in entry.post_groups:
			for line in g.lines:
				self._push(f"{indent}{line}", f"{indent}{gray(line)}")

	def _check_conflicts(self):
		for sym in self.doc.symbol_index:
			if sym in self.sup.symbol_index:
				self.warnings.append(
                    f"Conflict: {sym} in both doc and suppressed "
                    f"- doc wins, removing from suppressed")

	# _is_struct_in_doc removed: the comment exception is now handled
	# by _struct_has_desc_comment, which checks the actual comment content.

	def _folded_if_owner(self, child: KNode, siblings: list) -> Optional[KNode]:
		"""Return the menuconfig that owns this folded-if, only if that
        menuconfig is actually being emitted (in doc or via add_new etc.).
        Returns None if the menuconfig is not being emitted, meaning the
        if-block should remain visible rather than being folded away.
        """
		if not _is_folded_if(child, child.parent):
			return None
		sym = child.prompt[3:].strip()
		mc  = next((c for c in siblings
                    if c.kind == "menuconfig" and c.name == sym), None)
		if mc is None:
			return None
		return mc if self._should_emit(mc) else None

	def _has_visible_children(self, node: KNode) -> bool:
		children = node.children
		for i, child in enumerate(children):
			# Only fold the if-block when its owning menuconfig is
			# actually being emitted.  If not, treat it as a normal
			# structural node so its children remain reachable.
			if (_is_folded_if(child, node)
                    and self._folded_if_owner(child, children) is not None):
				continue
			if child.kind in ("config", "menuconfig"):
				if self._should_emit(child):
					return True
			elif child.kind in ("menu", "choice", "if", "comment"):
				if (self._has_visible_children(child)
                        or self._struct_has_desc_comment(child)):
					return True
		return False

	def _visible_children(self, node: KNode) -> list[KNode]:
		out = []
		children = node.children
		for i, child in enumerate(children):
			# Only fold when owning menuconfig is being emitted
			if (_is_folded_if(child, node)
                    and self._folded_if_owner(child, children) is not None):
				# Warn if the user put a descriptive comment on this
				# if-entry in the doc file - it will never be shown.
				key = _struct_node_key(child)
				_e  = self._eff_struct.get(key)
				if _e and (_e.trailing_comment or _e.pre_group
                           or any(not g.is_freestanding()
                                  for g in _e.post_groups)):
					# Find the owning menuconfig for the warning message
					_mc = next((c for c in children
                                if c.kind == "menuconfig"
                                and c.name == child.prompt[3:].strip()),
                               None)
					_mc_name = _mc.name if _mc else "?"
					self.warnings.append(
                        f"Comment on [{child.occurrence_key}] in doc is "
                        f"unreachable -- the if-block is folded under "
                        f"menuconfig {_mc_name}; "
                        f"move the comment to that menuconfig line")
				continue
			if child.kind in ("config", "menuconfig"):
				if self._should_emit(child):
					out.append(child)
				else:
					sym = child.symbol()
					if (child.any_col_active()
                            and not self._in_eff_doc(sym)
                            and not self._in_sup(sym)
                            and not self.add_new
                            and not self.add_new_en
                            and not self.full):
						if child.kind != "menuconfig" or not any(
                                self._in_eff_doc(c.symbol())
                                for c in child.children if c.symbol()):
							self.notices.append(
                                f"Active option not tracked: {sym} "
                                f"(use --add-new-enabled or --add-new)")
			elif child.kind in ("menu", "choice", "if", "comment"):
				# Visible when: has visible children OR has a descriptive
				# comment (the comment exception keeps annotated nodes
				# visible even when all children are suppressed).
				if (self._has_visible_children(child)
                        or self._struct_has_desc_comment(child)):
					out.append(child)
		return out

	def _recurse(self, parent: KNode, prefix: str):
		children = self._visible_children(parent)
		for idx, child in enumerate(children):
			is_last   = idx == len(children) - 1
			connector = G.last if is_last else G.tee
			child_pfx = prefix + (G.blank if is_last else G.pipe)
			self._emit_child(child, prefix, connector, child_pfx)

	def _emit_child(self, node: KNode, prefix: str, connector: str,
                    child_pfx: str):
		# The indentation column for comments anchored to this node:
		# prefix + spaces equal to len(connector), so '#' aligns with body text.
		cmt_indent = _comment_indent(prefix, connector)

		# -- structural nodes ---------------------------------------------------
		if node.kind in ("menu", "choice", "if", "comment"):
			nid = id(node)
			if nid in self._struct_emitted:
				self._recurse(node, child_pfx)
				return
			self._struct_emitted.add(nid)

			# Structural nodes are always shown when they have visible
			# children OR a descriptive comment.  They cannot be suppressed
			# by the suppressed file - only symbol entries control visibility.
			has_children = self._has_visible_children(node)
			has_comment  = self._struct_has_desc_comment(node)
			if not has_children and not has_comment:
				# Nothing to show - not reachable via _visible_children,
				# but guard here for safety.
				return

			entry = self._struct_entry(node)

			# Type-3 pre-comment (blank above + node below)
			if entry and entry.pre_group:
				self._emit_group(entry.pre_group, cmt_indent, is_pre=True)

			trailing = entry.trailing_comment if entry else ""
			self._push(_plain_line(node, prefix, connector, trailing, self.num_cols),
                       _colour_line(node, prefix, connector, trailing, self.num_cols))

			# Type-2 or type-4 post-comment
			for g in (entry.post_groups if entry else []):
				self._emit_group(g, cmt_indent, is_pre=False)

			# Record that this structural node's header was actually emitted.
			self._struct_header_emitted.add(id(node))
			self._recurse(node, child_pfx)
			return

		# -- config / menuconfig ------------------------------------------------
		sym = node.symbol()
		if sym in self._emitted:
			return
		self._emitted.add(sym)

		# Blank before this node only when:
		#  1. its direct parent's header was suppressed (not emitted), AND
		#  2. we are seeing a NEW parent group (not a sibling of the previous node)
		# In a normal tree render connectors make hierarchy clear - blanks are
		# only needed when suppression has broken the visual parent-child chain.
		parent = node.parent
		parent_suppressed = (
            parent is not None
            and parent.kind in ("menu", "choice", "if", "comment")
            and id(parent) not in self._struct_header_emitted
        )
		new_parent_group = (parent is not self._prev_emitted_parent)
		# len > 1 because sym was already added above - we want
		# "something was emitted before this node", not "this node exists".
		if parent_suppressed and new_parent_group and len(self._emitted) > 1:
			self._push_blank()

		entry  = self._entry(sym)
		in_eff = self._in_eff_doc(sym)
		in_sup = self._in_sup(sym)

		if not in_eff and not in_sup and (self.add_new or self.add_new_en or self.full):
			msg = f"# + New option added to doc: {sym}"
			self._push(msg, green(msg), is_notice=True)
			self.notices.append(f"New option added to doc: {sym}")
		elif not in_eff and in_sup and self.full:
			msg = f"# + Restored from suppressed: {sym}"
			self._push(msg, green(msg), is_notice=True)

		# Type-3 pre-comment
		if entry and entry.pre_group:
			self._emit_group(entry.pre_group, cmt_indent, is_pre=True)

		# menuconfig folding: collect all [if SYM] siblings to fold
		folded_list = _folded_ifs(node) if node.kind == "menuconfig" else []
		has_folded_children = any(self._has_visible_children(f)
                                  for f in folded_list)

		trailing = entry.trailing_comment if entry else ""
		if has_folded_children:
			# Prepend expand marker before any trailing comment
			trailing = (G.expand + " " + trailing) if trailing else G.expand
		self._push(_plain_line(node, prefix, connector, trailing, self.num_cols),
                   _colour_line(node, prefix, connector, trailing, self.num_cols),
                   symbol=sym)

		# Type-2 or type-4 post-comment
		for g in (entry.post_groups if entry else []):
			self._emit_group(g, cmt_indent, is_pre=False)

		# Dead entries anchored to this symbol (unknown symbols with comments)
		for dead_entry in self._anchor_map.get(sym, []):
			self._emit_dead_comment(dead_entry, prefix, connector)

		self._prev_emitted_parent = node.parent

		if node.kind == "menuconfig":
			for folded in folded_list:
				# Mark each folded-if as header-emitted so its children
				# do not trigger the suppressed-parent blank logic.
				self._struct_header_emitted.add(id(folded))
				# Recurse into folded-if children at menuconfig child depth
				self._recurse(folded, child_pfx)
			# Also recurse into any direct children of the menuconfig itself
			if node.children:
				self._recurse(node, child_pfx)


# -- Suppressed file update ----------------------------------------------------

def prune_suppressed(
    sup:         DocFileParser,
    knode_index: dict[str, KNode],
    full:        bool,
) -> tuple[list[str], list[str]]:
	"""Prune vanished symbols from sup in-place. Clears everything if full."""
	notices: list[str] = []
	if full:
		sup.symbol_index.clear()
		sup.struct_index.clear()
		sup.ordered.clear()
		return notices, []
	to_remove = [sym for sym in sup.symbol_index
                 if sym not in knode_index]
	for sym in to_remove:
		notices.append(f"Dropped vanished symbol from suppressed: {sym}")
		del sup.symbol_index[sym]
	sup.ordered = [e for e in sup.ordered
                   if not e.symbol or e.symbol in knode_index]
	return notices, []


# -- Emit Linux .config format -------------------------------------------------

def emit_kconfig_format(output_lines: list[OutputLine],
                        knode_index: dict[str, KNode]):
	print("# Generated by kconfig_tree.py")
	print("#")
	seen: set[str] = set()
	for ol in output_lines:
		if ol.is_blank or ol.is_notice or ol.is_warning:
			continue
		sym = ol.symbol
		if not sym or sym in seen:
			continue
		seen.add(sym)
		node = knode_index.get(sym)
		if node is None:
			continue
		v = (node.value or "").strip()
		if v in ("", "n"):
			print(f"# {sym} is not set")
		else:
			print(f"{sym}={node.value}")


# -- Write helpers --------------------------------------------------------------

def write_doc(path: Path, lines: list[OutputLine]):
	with path.open("w") as f:
		last_blank = False
		for ol in lines:
			if ol.is_warning or ol.is_notice:
				continue
			if ol.is_blank:
				if not last_blank:
					f.write("\n")
				last_blank = True
			else:
				f.write(ol.plain + "\n")
				last_blank = False


def _walk_suppressed(
    node:       KNode,
    sup:        "DocFileParser",
    prefix:     str,
    parent_children: list,
    out:        list,
    _emitted_syms: Optional[set] = None,
):
	"""Walk Kconfig tree depth-first.
    For each node in sup.symbol_index or sup.struct_index, record
    (node, entry, prefix, connector) so write_suppressed can render it
    at the correct tree depth.  Parent nodes that are NOT in the suppressed
    index are silently skipped but their depth is still accumulated.
    _emitted_syms prevents duplicates when the same CONFIG_ symbol is
    defined more than once in the Kconfig tree.
    """
	if _emitted_syms is None:
		_emitted_syms = set()
	sym = node.symbol()

	# Determine connector for this node among its siblings
	if parent_children:
		is_last = (node is parent_children[-1])
	else:
		is_last = True
	connector = G.last if is_last else G.tee
	child_pfx = prefix + (G.blank if is_last else G.pipe)

	in_sup = False
	entry  = None
	if (node.kind in ("config", "menuconfig") and sym in sup.symbol_index
            and sym not in _emitted_syms):
		entry  = sup.symbol_index[sym]
		in_sup = True
		_emitted_syms.add(sym)
	elif node.kind in ("menu", "choice", "if", "comment"):
		# Structural nodes are auto-derived: emit whenever the subtree
		# contains suppressed symbols.  Entry from struct_index is used
		# for comment recovery (menu/choice/comment only; if-entries
		# are not indexed from suppressed files so entry will be None).
		if _subtree_has_suppressed(node, sup.symbol_index):
			nkey  = _struct_node_key(node)
			entry = sup.struct_index.get(nkey)  # None is fine
			in_sup = True

	if in_sup:
		out.append((node, entry, prefix, connector))

	for child in node.children:
		_walk_suppressed(child, sup, child_pfx, node.children, out,
                         _emitted_syms)


def write_suppressed(path: Path, sup: "DocFileParser", root: KNode,
                     knode_index: dict[str, KNode], full: bool):
	"""Write the suppressed file.

    Each suppressed entry is rendered at its CORRECT tree depth and with
    the correct prefix/connector (as it would appear in the doc file).
    Non-suppressed parent nodes are not written - their depth is accumulated
    silently so that children appear at the right indentation level.

    A blank line is inserted between consecutive entries whose direct Kconfig
    parent was NOT itself emitted in the suppressed file (i.e. the parent is
    absent, so the hierarchy break needs visual marking).
    """
	if not sup.symbol_index and not sup.struct_index:
		path.write_text("")
		return

	# Collect (node, entry, prefix, connector) in Kconfig tree order
	ordered: list[tuple] = []
	_emitted_syms: set = set()  # prevents duplicate symbols from dual-defined Kconfig entries
	for child in root.children:
		_walk_suppressed(child, sup, "", root.children, ordered, _emitted_syms)

	# Which node ids are explicitly in the suppressed file?
	sup_node_ids: set[int] = {id(node) for node, *_ in ordered}

	def _nearest_absent_ancestor(n: KNode) -> Optional[KNode]:
		"""Walk up until we find the first ancestor that is NOT in the
        suppressed file (not in sup_node_ids) and is not the root.
        Two nodes with the same nearest absent ancestor are in the same
        logical group and should not be separated by a blank."""
		p = n.parent
		while p and p is not root:
			if id(p) not in sup_node_ids:
				return p
			p = p.parent
		return None  # parent chain is all-present or at root

	with path.open("w") as f:
		first                    = True
		prev_absent_ancestor: Optional[KNode] = None
		for node, entry, prefix, connector in ordered:
			is_struct = node.kind in ("menu", "choice", "if", "comment")

			# Blank when consecutive entries have DIFFERENT nearest absent
			# ancestors - meaning they belong to different logical groups.
			# Entries that share the same absent ancestor (e.g. siblings
			# of a suppressed choice, or children of a choice that is
			# itself inside an absent menu) stay adjacent.
			absent_anc = _nearest_absent_ancestor(node)
			if not first and absent_anc is not None and absent_anc is not prev_absent_ancestor:
				f.write("\n")
			first = False
			prev_absent_ancestor = absent_anc

			# Pre-group comments (entry may be None for synthetic if-nodes)
			if entry and entry.pre_group:
				if entry.pre_group.blank_above:
					f.write("\n")
				for line in entry.pre_group.lines:
					f.write(line + "\n")

			# Node line - same format as doc, at correct depth
			tc = f" {entry.trailing_comment}" if (entry and entry.trailing_comment) else ""
			if node.symbol():
				glyph  = node.raw_glyph()
				prompt = node.prompt or node.name
				f.write(f"{prefix}{connector}{glyph} {prompt} ({node.name}){tc}\n")
			elif is_struct:
				body = _plain_body(node)
				f.write(f"{prefix}{connector}{body}{tc}\n")

			# Post-group comments
			for pg in (entry.post_groups if entry else []):
				if pg.blank_above:
					f.write("\n")
				for line in pg.lines:
					f.write(line + "\n")
				if pg.blank_below:
					f.write("\n")


# -- Post-render depth / filter -------------------------------------------------

def _filter_output(lines: list[OutputLine],
                   max_depth: Optional[int],
                   filter_word: Optional[str]) -> list[OutputLine]:
	fw = filter_word.lower() if filter_word else None
	result = []
	for ol in lines:
		if ol.is_blank or ol.is_notice or ol.is_warning:
			result.append(ol)
			continue
		d = _depth_of(ol.plain)
		if max_depth is not None and d > max_depth:
			continue
		if fw and fw not in ol.plain.lower():
			continue
		result.append(ol)
	return result


# -- Stats ----------------------------------------------------------------------

def collect_stats(node: KNode, stats: dict):
	if node.kind in ("config", "menuconfig"):
		stats["total"] += 1
		v = node.value
		if v is None:       stats["unset"]  += 1
		elif v == "y":      stats["yes"]    += 1
		elif v == "m":      stats["module"] += 1
		elif v == "n":      stats["no"]     += 1
		else:               stats["other"]  += 1
	for c in node.children:
		collect_stats(c, stats)


# -- CLI ------------------------------------------------------------------------

DEFAULT_DOC = "kconfig_doc.txt"
DEFAULT_SUP = "kconfig_doc_suppressed.txt"


def main():
	global USE_COLOR

	ap = argparse.ArgumentParser(
        description="Kernel configuration documentation tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
	ap.add_argument("--kconfig",         default="Kconfig")
	ap.add_argument("--dotconfig",        default=".config")
	ap.add_argument("--dotconfig2",       default=".config2")
	ap.add_argument("--dotconfig3",       default=".config3")
	ap.add_argument("--doc",             default=DEFAULT_DOC)
	ap.add_argument("--suppressed",      default=DEFAULT_SUP)
	ap.add_argument("--arch",            default="arm64")
	ap.add_argument("--add-new",         action="store_true")
	ap.add_argument("--add-new-enabled", nargs="?", const="1", default=None,
                    metavar="1-3",
                    help="Add active symbols from column N (default 1)")
	ap.add_argument("--full",            action="store_true")
	ap.add_argument("--emit-kconfig",    action="store_true")
	ap.add_argument("--show",            action="store_true")
	ap.add_argument("--depth",           type=int, default=None)
	ap.add_argument("--filter",          default=None)
	ap.add_argument("--no-color",        action="store_true")
	ap.add_argument("--ascii",           action="store_true",
                    help="ASCII-only output: no UTF-8 box/glyph chars")
	ap.add_argument("--no-doc",          action="store_true")
	args = ap.parse_args()

	if args.no_color or args.emit_kconfig:
		USE_COLOR = False
	if args.ascii:
		global G, _STRUCT_LEADER
		G = _ASCII_GLYPHS
		_STRUCT_LEADER = _build_struct_leader()

	kernel_root    = Path(".").resolve()
	kconfig_path   = Path(args.kconfig)
	dotconfig_path  = Path(args.dotconfig)
	dotconfig2_path = Path(args.dotconfig2)
	dotconfig3_path = Path(args.dotconfig3)
	doc_path        = Path(args.doc)
	sup_path        = Path(args.suppressed)

	add_new_en     = args.add_new_enabled is not None
	add_new_en_col = 0
	if add_new_en:
		try:
			add_new_en_col = max(0, int(args.add_new_enabled or "1") - 1)
		except ValueError:
			add_new_en_col = 0

	if not kconfig_path.exists():
		sys.exit(f"ERROR: Kconfig file not found: {kconfig_path}\n"
                 "Run from your kernel source root directory.")

	print("Parsing Kconfig hierarchy ...", file=sys.stderr)
	parser = KconfigParser(arch=args.arch, kernel_root=kernel_root)
	root   = parser.parse(kconfig_path)

	print(f"Loading {dotconfig_path} ...", file=sys.stderr)
	cfg = load_dotconfig(dotconfig_path)
	annotate_tree(root, cfg, col=0)

	num_cols = 1
	if dotconfig2_path.exists():
		print(f"Loading {dotconfig2_path} ...", file=sys.stderr)
		annotate_tree(root, load_dotconfig(dotconfig2_path), col=1)
		num_cols = 2
	if dotconfig3_path.exists():
		print(f"Loading {dotconfig3_path} ...", file=sys.stderr)
		annotate_tree(root, load_dotconfig(dotconfig3_path), col=2)
		num_cols = 3

	knode_index = build_knode_index(root)

	if_occurrence_counts = assign_if_keys(root)

	struct_knode_index = build_struct_knode_index(root)

	if doc_path.exists():
		print(f"Reading {doc_path} ...", file=sys.stderr)
	doc = DocFileParser(doc_path, knode_index, struct_knode_index)

	if sup_path.exists():
		print(f"Reading {sup_path} ...", file=sys.stderr)
	sup = DocFileParser(sup_path, knode_index, struct_knode_index,
                        is_suppressed=True)

	combined_max: dict[str, int] = {}
	for expr, n in {**doc.max_if_occurrences, **sup.max_if_occurrences}.items():
		combined_max[expr] = max(combined_max.get(expr,0),
                                doc.max_if_occurrences.get(expr,0),
                                sup.max_if_occurrences.get(expr,0))
	for expr, seen_max in combined_max.items():
		tree_n = if_occurrence_counts.get(expr, 0)
		if seen_max > tree_n:
			print(f"WARNING: [{expr} ({seen_max})] referenced but tree has "
                  f"only {tree_n} occurrence(s) - may have shifted after kernel update",
                  file=sys.stderr)
		elif tree_n > seen_max and seen_max > 1:
			# Only notify when the user is already referencing numbered occurrences
			# (seen_max > 1) so they know there are more.  When seen_max == 1 the
			# user only references the unnumbered first occurrence - that's normal
			# and not worth reporting for every common 'if' expression.
			print(f"NOTICE: [{expr}] has {tree_n} occurrences in tree; "
                  f"doc+suppressed reference up to [{expr} ({seen_max})] - "
                  f"[{expr} ({tree_n})] also exists",
                  file=sys.stderr)

	merger = Merger(
        root           = root,
        doc            = doc,
        sup            = sup,
        knode_index    = knode_index,
        add_new        = args.add_new,
        add_new_en     = add_new_en,
        add_new_en_col = add_new_en_col,
        full           = args.full,
        num_cols       = num_cols,
    )
	output_lines = merger.run()

	sup_notices, _ = prune_suppressed(sup, knode_index, args.full)

	# Conflict resolution - two passes enforcing both invariants:
	#
	# Pass A: direct conflict - symbol in both doc and suppressed, doc wins.
	#
	# Pass B: descendant conflict - a suppressed symbol must not have any
	#   doc-tracked descendants (direct children or folded if-block subtrees,
	#   recursively).  Auto-remove from suppressed and warn: the user must
	#   suppress all descendants before suppressing the parent.
	#   This enforces: "a symbol's parent is not suppressable while any of
	#   its children remain in doc."
	doc_syms = set(merger._eff_doc.keys())

	# Pass A
	for sym in list(sup.symbol_index.keys()):
		if sym in doc_syms:
			del sup.symbol_index[sym]
	sup.ordered = [e for e in sup.ordered
                   if not e.symbol or e.symbol not in doc_syms]

	# Pass B
	for sym in list(sup.symbol_index.keys()):
		node = knode_index.get(sym)
		if node is None:
			continue
		doc_child = _first_doc_descendant(node, merger._eff_doc)
		if doc_child:
			del sup.symbol_index[sym]
			sup.ordered = [e for e in sup.ordered
                           if not e.symbol or e.symbol != sym]
			merger.warnings.append(
                f"Removed {sym} from suppressed: descendant {doc_child} "
                f"is doc-tracked. Suppress all descendants first, "
                f"then suppress {sym}")

	# Post-run invariant check: any doc-tracked symbol not in output is a bug
	emitted_syms = {ol.symbol for ol in output_lines if ol.symbol}
	for sym in doc_syms:
		if sym in emitted_syms or sym not in knode_index:
			continue
		if sup.symbol_index.get(sym):
			continue  # legitimately suppressed after resolution
		merger.warnings.append(
            f"{sym} is doc-tracked but was not emitted "
            f"(possible tree traversal bug -- please report)")

	show_lines = output_lines
	if args.show and (args.depth is not None or args.filter):
		show_lines = _filter_output(output_lines, args.depth, args.filter)

	if args.emit_kconfig:
		emit_kconfig_format(output_lines, knode_index)
		return

	if not args.no_doc:
		write_doc(doc_path, output_lines)
		print(f"Doc written -> {doc_path}", file=sys.stderr)

		write_suppressed(sup_path, sup, root, knode_index, args.full)
		cleared = args.full and not sup.symbol_index and not sup.struct_index
		print(f"Suppressed {'cleared' if cleared else 'updated'} -> {sup_path}",
              file=sys.stderr)

	if args.show:
		for ol in show_lines:
			print(ol.coloured)

	all_notices = merger.notices + sup_notices
	if all_notices:
		print(file=sys.stderr)
		print("NOTICES:", file=sys.stderr)
		for n in all_notices:
			print(f"  + {n}", file=sys.stderr)

	if merger.warnings:
		print(file=sys.stderr)
		print(f"{magenta('WARNINGS')} ({len(merger.warnings)}):", file=sys.stderr)
		for w in merger.warnings:
			print(f"  {magenta('!')} {w}", file=sys.stderr)

	stats: dict = {"total": 0, "yes": 0, "module": 0, "no": 0,
                   "unset": 0, "other": 0}
	collect_stats(root, stats)
	tracked = [ol for ol in output_lines
               if ol.symbol and not ol.is_notice and not ol.is_warning]
	active_tracked = sum(
        1 for ol in tracked
        if knode_index.get(ol.symbol, KNode("", "")).any_col_active()
    )
	print(file=sys.stderr)
	col_label = f" ({num_cols} configs)" if num_cols > 1 else ""
	print(
        f"{bold('Kconfig')}{col_label}: "
        f"{green(str(stats['yes']))} built-in  "
        f"{yellow(str(stats['module']))} module  "
        f"{gray(str(stats['no']))} disabled  "
        f"{gray(str(stats['unset']))} unset  "
        f"/ {stats['total']} total",
        file=sys.stderr,
    )
	print(
        f"{bold('Doc')}:     "
        f"{len(tracked)} tracked  "
        f"({active_tracked} active)  "
        f"{len(sup.symbol_index)} suppressed",
        file=sys.stderr,
    )


if __name__ == "__main__":
	main()
