#!/usr/bin/env python3
"""
kconfig_tree.py — Kernel configuration documentation tool.

THREE-FILE MODEL
────────────────
  .config                     Kernel truth (what is active)
  kconfig_doc.txt             User allowlist (what to show and document)
  kconfig_doc_suppressed.txt  User denylist (what to permanently hide)

MERGE RULES
───────────
  In doc                    → emit, update glyph, warn on value mismatch
  In suppressed             → never emit (unless --full)
  In neither, inactive      → silent
  In neither, active        → notice on stderr (suggest --add-new-enabled)
  In both (conflict)        → doc wins, warn, remove from suppressed
  --add-new                 → add all symbols in neither file
  --add-new-enabled         → add only [*]/[M] symbols in neither file
  --full                    → merge suppressed→doc, add all remaining

INPUT FORMATS ACCEPTED IN DOC / SUPPRESSED FILES
─────────────────────────────────────────────────
  Full doc:      ├─[*] Some prompt (SYMBOL) # trailing comment
  Bare symbol:   CONFIG_SYMBOL  # optional trailing comment
  .config set:   CONFIG_SYMBOL=y
  .config unset: # CONFIG_SYMBOL is not set
  Pure comment:  # free text

FOUR COMMENT TYPES
──────────────────
  1. Trailing   — on the same line as a node after node info
  2. Anchored-above — directly below a node (no blank between)
                      indented to body column of that node
                      optional blank below is preserved
  3. Anchored-below — blank line above + node directly below (no blank between)
                      indented to body column of that node
                      blank above is preserved
  4. Freestanding   — blank above AND blank below
                      no tree indentation (written at column 0)
                      anchored to node above for stability
                      both surrounding blanks preserved
                      blanks between lines in the group collapsed to one

ORPHAN SPACING
──────────────
  A blank line is inserted between two config/menuconfig nodes from
  completely different subtrees so the tree cannot be visually misread.

USAGE
─────
  python3 kconfig_tree.py --add-new-enabled   # first run, active options only
  python3 kconfig_tree.py --full              # first run, everything
  python3 kconfig_tree.py                     # normal update
  python3 kconfig_tree.py --show              # view coloured tree
  python3 kconfig_tree.py --emit-kconfig > my.config

  Suppress: move line from doc → suppressed file
  Un-suppress: delete line from suppressed file

OPTIONS
───────
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

# ── ANSI colour helpers ────────────────────────────────────────────────────────

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

# ── Tree drawing ───────────────────────────────────────────────────────────────

PIPE  = "│ "
TEE   = "├─"
LAST  = "└─"
BLANK = "  "

_TREE_CHARS = set("│├└─ ")

def _strip_tree_prefix(line: str) -> tuple[str, str]:
    i = 0
    while i < len(line) and line[i] in _TREE_CHARS:
        i += 1
    return line[:i], line[i:]

def _depth_of(plain: str) -> int:
    prefix, _ = _strip_tree_prefix(plain)
    return len(prefix) // 2


# ── KNode ─────────────────────────────────────────────────────────────────────

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

    def symbol(self) -> str:
        return f"CONFIG_{self.name}" if self.name else ""

    def is_active(self) -> bool:
        if self.value is None:
            return False
        v = self.value.strip().strip('"')
        return v not in ("", "n", "0")

    def raw_glyph(self) -> str:
        if self.kind in ("menu", "choice", "comment", "if"):
            return ""
        if self.value is None:
            return "[ ]"
        v = self.value.strip().strip('"')
        if v == "y":        return "[*]"
        if v == "m":        return "[M]"
        if v in ("n", ""):  return "[ ]"
        return f"[={v}]"

    def coloured_glyph(self) -> str:
        if self.kind in ("menu", "choice", "comment", "if"):
            return ""
        if self.value is None:
            return gray("[ ]")
        v = self.value.strip().strip('"')
        if v == "y":        return green("[*]")
        if v == "m":        return yellow("[M]")
        if v in ("n", ""):  return gray("[ ]")
        return cyan(f"[={v}]")

    def ancestry(self) -> list["KNode"]:
        chain: list["KNode"] = []
        p = self.parent
        while p:
            chain.append(p)
            p = p.parent
        chain.reverse()
        return chain


# ── Kconfig parser ─────────────────────────────────────────────────────────────

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
                node = KNode(kind="if", name="",
                             prompt=f"if {m.group(1).strip()}",
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


# ── .config loader ─────────────────────────────────────────────────────────────

def load_dotconfig(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not path.exists():
        print(f"WARNING: {path} not found — values will show as unset.",
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


def annotate_tree(node: KNode, cfg: dict[str, str]):
    if node.name:
        node.value = cfg.get(node.symbol())
    for c in node.children:
        annotate_tree(c, cfg)


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


# ── Doc / suppressed file parser ───────────────────────────────────────────────
#
# Four comment types (stored in RawEntry):
#   1. Trailing   — trailing_comment field (same line, already handled)
#   2. Anchored-above (post, no blank above)  → post_groups, not freestanding
#   3. Anchored-below (pre,  blank above)     → pre_group
#   4. Freestanding (post, blank above+below) → post_groups, is_freestanding()

_DOC_SYMBOL_RE   = re.compile(r"\((\w+)\)\s*(?:#.*)?$")
_SYMBOL_TAG_RE   = re.compile(r"\((\w+)\)")  # tag only, no trailing
_TRAILING_CMT_RE = re.compile(r" (#.*)$")
_BARE_SYMBOL_RE  = re.compile(r"^(CONFIG_\w+)\s*(?:#.*)?$")
_DOTCFG_SET_RE   = re.compile(r"^(CONFIG_\w+)=(.*)$")
_DOTCFG_UNSET_RE = re.compile(r"^#\s+(CONFIG_\w+)\s+is not set\s*$")
_STRUCT_LEADER   = re.compile(r"^(▶ |◆ |\[if )")
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
    comment_text: str = "" # normalised prompt key for structural lines
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
    s = _STRUCT_LEADER.sub("", content_clean, count=1)
    s = _KCONFIG_CMT_RE.sub(r"\1", s)
    return s.strip()


class DocFileParser:
    """
    Parses a doc or suppressed file into RawEntry objects.

    Public:
      symbol_index : dict[str, RawEntry]
      struct_index : dict[str, RawEntry]
      ordered      : list[RawEntry]
      warnings     : list[str]
    """

    def __init__(self, path: Path, knode_index: dict[str, KNode]):
        self.path = path
        self.knode_index = knode_index
        self.symbol_index: dict[str, RawEntry] = {}
        self.struct_index: dict[str, RawEntry] = {}
        self.ordered: list[RawEntry] = []
        self.dead_entries: list[RawEntry] = []  # unknown symbols, kept as comments
        self.warnings: list[str] = []
        self._parse()

    def _parse(self):
        if not self.path.exists():
            return

        raw_lines = self.path.read_text(errors="replace").splitlines()

        # ── Pass 1: classify every line ───────────────────────────────────────

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
                                       comment_text=prompt_key)))
                continue

            items.append(("comment", content))

        # ── Pass 2: resolve comment anchoring ─────────────────────────────────
        #
        # For each contiguous comment run, classify by context:
        #
        #   blank_above=F, next_is_option=—  → type 2 (anchored-above / post)
        #   blank_above=T, next_is_option=T  → type 3 (anchored-below / pre)
        #   blank_above=T, blank_below=T     → type 4 (freestanding / post)
        #
        # Note: type 4 wins over type 3 — if there's a blank below too,
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
                # Type 4: freestanding — append to post_groups of option above
                if option_entries:
                    option_entries[-1].post_groups.append(group)
                # else: before any option, discard

            elif blank_above and next_is_option:
                # Type 3: anchored-below — attach as pre_group to option below
                next_entry = items[i][1]
                next_entry.pre_group = group

            else:
                # Type 2: anchored-above — append to post_groups of option above
                if option_entries:
                    option_entries[-1].post_groups.append(group)
                # else: discard

        # ── Pass 3: index ─────────────────────────────────────────────────────

        for entry in option_entries:
            if entry.symbol:
                if entry.symbol not in self.symbol_index:
                    self.symbol_index[entry.symbol] = entry
                    self.ordered.append(entry)
                else:
                    self.warnings.append(
                        f"Duplicate symbol {entry.symbol} in "
                        f"{self.path.name} — keeping first")
            elif entry.comment_text:
                key = entry.comment_text
                if key not in self.struct_index:
                    self.struct_index[key] = entry
                    self.ordered.append(entry)

        # ── Pass 4: validate against Kconfig tree ─────────────────────────────
        # Unknown symbols:
        #   - with attached comments → moved to dead_entries, emitted inline as
        #     type-2 anchored comments beside their preceding known option
        #   - without comments       → silently dropped (no warning, no stub)

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
                    f"Kconfig tree — converted to inline comment")
            # else: silently dropped
            del self.symbol_index[sym]
        self.ordered = [e for e in self.ordered if e.symbol not in unknown_syms]

        # ── Pass 5: value mismatch warnings ───────────────────────────────────

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
                    f"file has ={fv}, .config has ={live} — using .config value")


# ── Ancestry / orphan-blank ───────────────────────────────────────────────────

def _needs_blank(prev_node: Optional[KNode], curr_node: KNode) -> bool:
    """
    True when a blank should be inserted between prev and curr because
    they come from completely different subtrees.
    Only fires for config/menuconfig nodes — structural nodes never set
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


# ── Comment indentation helpers ───────────────────────────────────────────────

def _comment_indent(prefix: str, connector: str) -> str:
    """
    Return the indentation string for a type-2 or type-3 comment line.
    The comment's '#' should align with the body of the node it is anchored to.
    Body starts at: prefix + connector + body_text
    So comment indent = prefix + ' ' * len(connector)
    """
    return prefix + " " * len(connector)


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


# ── Line renderers ─────────────────────────────────────────────────────────────

def _plain_body(node: KNode) -> str:
    glyph = node.raw_glyph()
    if node.kind == "menu":
        return f"▶ {node.prompt}" if node.prompt else "▶ (menu)"
    if node.kind == "choice":
        return f"◆ {node.prompt or '(choice)'}"
    if node.kind == "comment":
        return f"--- {node.prompt} ---"
    if node.kind == "if":
        return f"[{node.prompt}]"
    prompt = node.prompt or node.name
    tag    = f" ({node.name})" if node.name else ""
    return f"{glyph} {prompt}{tag}" if glyph else f"{prompt}{tag}"


def _colour_body(node: KNode) -> str:
    glyph = node.coloured_glyph()
    if node.kind == "menu":
        p = f"▶ {node.prompt}" if node.prompt else "▶ (menu)"
        return bold(cyan(p))
    if node.kind == "choice":
        return bold(f"◆ {node.prompt or '(choice)'}")
    if node.kind == "comment":
        return gray(f"--- {node.prompt} ---")
    if node.kind == "if":
        return gray(f"[{node.prompt}]")
    prompt = node.prompt or node.name
    pstr   = bold(prompt) if node.is_active() else gray(prompt)
    tag    = gray(f" ({node.name})") if node.name else ""
    return f"{glyph} {pstr}{tag}" if glyph else f"{pstr}{tag}"


def _plain_line(node: KNode, prefix: str, connector: str,
                trailing: str = "") -> str:
    tc = f" {trailing}" if trailing else ""
    return f"{prefix}{connector}{_plain_body(node)}{tc}"


def _colour_line(node: KNode, prefix: str, connector: str,
                 trailing: str = "") -> str:
    tc = f" {gray(trailing)}" if trailing else ""
    return f"{prefix}{connector}{_colour_body(node)}{tc}"


# ── OutputLine ─────────────────────────────────────────────────────────────────

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


# ── Merger ─────────────────────────────────────────────────────────────────────

class Merger:
    def __init__(
        self,
        root:         KNode,
        doc:          DocFileParser,
        sup:          DocFileParser,
        knode_index:  dict[str, KNode],
        add_new:      bool = False,
        add_new_en:   bool = False,
        full:         bool = False,
    ):
        self.root        = root
        self.doc         = doc
        self.sup         = sup
        self.knode_index = knode_index
        self.add_new     = add_new
        self.add_new_en  = add_new_en
        self.full        = full

        self.output:   list[OutputLine] = []
        self.notices:  list[str] = []
        self.warnings: list[str] = list(doc.warnings) + list(sup.warnings)

        self._emitted:        set[str] = set()
        self._struct_emitted: set[int] = set()

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


    # ── internal push helpers ──────────────────────────────────────────────────

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

    # ── emit a CommentGroup ────────────────────────────────────────────────────

    def _emit_group(self, group: Optional[CommentGroup],
                    indent: str, is_pre: bool = False):
        """
        Emit a comment group.
          is_pre=True  → type 3 (anchored-below): blank above preserved
          is_pre=False → type 2 (anchored-above) or type 4 (freestanding)
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

    # ── decision helpers ───────────────────────────────────────────────────────

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
        if self.add_new_en and node.is_active():
            return True
        return False

    def _entry(self, sym: str) -> Optional[RawEntry]:
        return self._eff_doc.get(sym) or self.sup.symbol_index.get(sym)

    def _struct_entry(self, key: str) -> Optional[RawEntry]:
        return self._eff_struct.get(key) or self.sup.struct_index.get(key)


    # ── tree walk ──────────────────────────────────────────────────────────────

    def run(self) -> list[OutputLine]:
        hdr = f"⚙ {self.root.prompt or 'Linux Kernel Configuration'}"
        self._push(hdr, bold(cyan(hdr)))
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
                    f"— doc wins, removing from suppressed")

    def _has_visible_children(self, node: KNode) -> bool:
        for child in node.children:
            if child.kind in ("config", "menuconfig"):
                if self._should_emit(child):
                    return True
            elif child.kind in ("menu", "choice", "if"):
                if self._has_visible_children(child):
                    return True
            elif child.kind == "comment":
                return True
        return False

    def _visible_children(self, node: KNode) -> list[KNode]:
        out = []
        for child in node.children:
            if child.kind in ("config", "menuconfig"):
                if self._should_emit(child):
                    out.append(child)
                else:
                    sym = child.symbol()
                    if (child.is_active()
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
            elif child.kind in ("menu", "choice", "if"):
                if self._has_visible_children(child):
                    out.append(child)
            elif child.kind == "comment":
                out.append(child)
        return out

    def _recurse(self, parent: KNode, prefix: str):
        children = self._visible_children(parent)
        for idx, child in enumerate(children):
            is_last   = idx == len(children) - 1
            connector = LAST if is_last else TEE
            child_pfx = prefix + (BLANK if is_last else PIPE)
            self._emit_child(child, prefix, connector, child_pfx)

    def _emit_child(self, node: KNode, prefix: str, connector: str,
                    child_pfx: str):
        # The indentation column for comments anchored to this node:
        # prefix + spaces equal to len(connector), so '#' aligns with body text.
        cmt_indent = _comment_indent(prefix, connector)

        # ── structural nodes ───────────────────────────────────────────────────
        if node.kind in ("menu", "choice", "if", "comment"):
            nid = id(node)
            if nid in self._struct_emitted:
                self._recurse(node, child_pfx)
                return
            self._struct_emitted.add(nid)

            key   = node.prompt.strip() if node.prompt else ""
            entry = self._struct_entry(key)

            # Type-3 pre-comment (blank above + node below)
            if entry and entry.pre_group:
                self._emit_group(entry.pre_group, cmt_indent, is_pre=True)

            trailing = entry.trailing_comment if entry else ""
            self._push(_plain_line(node, prefix, connector, trailing),
                       _colour_line(node, prefix, connector, trailing))

            # Type-2 or type-4 post-comment
            for g in (entry.post_groups if entry else []):
                self._emit_group(g, cmt_indent, is_pre=False)

            self._recurse(node, child_pfx)
            return

        # ── config / menuconfig ────────────────────────────────────────────────
        sym = node.symbol()
        if sym in self._emitted:
            return
        self._emitted.add(sym)

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

        trailing = entry.trailing_comment if entry else ""
        self._push(_plain_line(node, prefix, connector, trailing),
                   _colour_line(node, prefix, connector, trailing),
                   symbol=sym)

        # Type-2 or type-4 post-comment
        for g in (entry.post_groups if entry else []):
            self._emit_group(g, cmt_indent, is_pre=False)

        # Dead entries anchored to this symbol (unknown symbols with comments)
        for dead_entry in self._anchor_map.get(sym, []):
            self._emit_dead_comment(dead_entry, prefix, connector)

        if node.kind == "menuconfig" and node.children:
            self._recurse(node, child_pfx)


# ── Suppressed file update ────────────────────────────────────────────────────

def compute_new_suppressed(
    sup:         DocFileParser,
    knode_index: dict[str, KNode],
    full:        bool,
) -> tuple[list[RawEntry], list[str]]:
    notices: list[str] = []
    if full:
        return [], notices
    new_entries: list[RawEntry] = []
    for entry in sup.ordered:
        if entry.symbol and entry.symbol not in knode_index:
            notices.append(f"Dropped vanished symbol from suppressed: {entry.symbol}")
            continue
        new_entries.append(entry)
    return new_entries, notices


# ── Emit Linux .config format ─────────────────────────────────────────────────

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


# ── Write helpers ──────────────────────────────────────────────────────────────

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


def write_suppressed(path: Path, entries: list[RawEntry],
                     knode_index: dict[str, KNode]):
    if not entries:
        path.write_text("")
        return
    with path.open("w") as f:
        prev_node: Optional[KNode] = None
        for entry in entries:
            node = knode_index.get(entry.symbol) if entry.symbol else None
            if node and _needs_blank(prev_node, node):
                f.write("\n")
            if entry.pre_group:
                for line in entry.pre_group.lines:
                    f.write(line + "\n")
            if entry.symbol and node:
                tc     = f" {entry.trailing_comment}" if entry.trailing_comment else ""
                glyph  = node.raw_glyph()
                prompt = node.prompt or node.name
                f.write(f"{glyph} {prompt} ({node.name}){tc}\n")
            elif entry.comment_text:
                f.write(entry.comment_text + "\n")
            for pg in entry.post_groups:
                if pg.blank_above:
                    f.write("\n")
                for line in pg.lines:
                    f.write(line + "\n")
                if pg.blank_below:
                    f.write("\n")
            if node:
                prev_node = node


# ── Post-render depth / filter ─────────────────────────────────────────────────

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


# ── Stats ──────────────────────────────────────────────────────────────────────

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


# ── CLI ────────────────────────────────────────────────────────────────────────

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
    ap.add_argument("--dotconfig",       default=".config")
    ap.add_argument("--doc",             default=DEFAULT_DOC)
    ap.add_argument("--suppressed",      default=DEFAULT_SUP)
    ap.add_argument("--arch",            default="arm64")
    ap.add_argument("--add-new",         action="store_true")
    ap.add_argument("--add-new-enabled", action="store_true")
    ap.add_argument("--full",            action="store_true")
    ap.add_argument("--emit-kconfig",    action="store_true")
    ap.add_argument("--show",            action="store_true")
    ap.add_argument("--depth",           type=int, default=None)
    ap.add_argument("--filter",          default=None)
    ap.add_argument("--no-color",        action="store_true")
    ap.add_argument("--no-doc",          action="store_true")
    args = ap.parse_args()

    if args.no_color or args.emit_kconfig:
        USE_COLOR = False

    kernel_root    = Path(".").resolve()
    kconfig_path   = Path(args.kconfig)
    dotconfig_path = Path(args.dotconfig)
    doc_path       = Path(args.doc)
    sup_path       = Path(args.suppressed)

    if not kconfig_path.exists():
        sys.exit(f"ERROR: Kconfig file not found: {kconfig_path}\n"
                 "Run from your kernel source root directory.")

    print("Parsing Kconfig hierarchy …", file=sys.stderr)
    parser = KconfigParser(arch=args.arch, kernel_root=kernel_root)
    root   = parser.parse(kconfig_path)

    print(f"Loading {dotconfig_path} …", file=sys.stderr)
    cfg = load_dotconfig(dotconfig_path)
    annotate_tree(root, cfg)
    knode_index = build_knode_index(root)

    if doc_path.exists():
        print(f"Reading {doc_path} …", file=sys.stderr)
    doc = DocFileParser(doc_path, knode_index)

    if sup_path.exists():
        print(f"Reading {sup_path} …", file=sys.stderr)
    sup = DocFileParser(sup_path, knode_index)

    merger = Merger(
        root        = root,
        doc         = doc,
        sup         = sup,
        knode_index = knode_index,
        add_new     = args.add_new,
        add_new_en  = args.add_new_enabled,
        full        = args.full,
    )
    output_lines = merger.run()

    new_sup, sup_notices = compute_new_suppressed(sup, knode_index, args.full)

    show_lines = output_lines
    if args.show and (args.depth is not None or args.filter):
        show_lines = _filter_output(output_lines, args.depth, args.filter)

    if args.emit_kconfig:
        emit_kconfig_format(output_lines, knode_index)
        return

    if not args.no_doc:
        write_doc(doc_path, output_lines)
        print(f"Doc written → {doc_path}", file=sys.stderr)

        doc_syms = set(merger._eff_doc.keys())
        new_sup = [e for e in new_sup
                   if not e.symbol or e.symbol not in doc_syms]
        write_suppressed(sup_path, new_sup, knode_index)
        if args.full and not new_sup:
            print(f"Suppressed cleared → {sup_path}", file=sys.stderr)
        else:
            print(f"Suppressed updated → {sup_path}", file=sys.stderr)

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
        if knode_index.get(ol.symbol, KNode("", "")).is_active()
    )
    print(file=sys.stderr)
    print(
        f"{bold('Kconfig')}: "
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
        f"{len([e for e in new_sup if e.symbol])} suppressed",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
