#!/usr/bin/env python3
"""
kconfig_tree.py — Kernel configuration documentation tool.

Maintains a human-editable documentation tree of kernel config options,
tracking which are active and merging updates across kernel versions.

THREE-FILE MODEL
────────────────
  .config                     Kernel truth (what is active)
  kconfig_doc.txt             User allowlist (what to show and document)
  kconfig_doc_suppressed.txt  User denylist (what to permanently hide)

MERGE RULES (per symbol, every run)
────────────────────────────────────
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
  Full doc format:   ├─ [*] Some prompt (SYMBOL_NAME) # optional comment
  Bare symbol:       CONFIG_SYMBOL_NAME  # optional comment
  .config format:    CONFIG_SYMBOL=y  or  # CONFIG_SYMBOL is not set
  Pure comment:      # free text

All formats are normalised to full doc format on next write.

COMMENT ANCHORING
─────────────────
  option A
  # comment 1    ← anchored below: attached to option A
  # comment 2    ← anchored to comment 1 (same group)
                 ← blank line breaks chain
  # comment 3    ← blank above + option directly below → pre-anchored to B
  # comment 4    ← same group as comment 3
  option B
                 ← blank line
  # comment 5    ← blank above, no option below → freestanding (attached above)
  # comment 6    ← same group as comment 5

ORPHAN SPACING
──────────────
  Whenever two consecutive entries would imply a parent-child relationship
  that doesn't exist in the Kconfig tree, a blank line is inserted between
  them so the tree structure cannot be visually misread.

USAGE
─────
  python3 kconfig_tree.py --add-new-enabled   # first run, active options only
  python3 kconfig_tree.py --full              # first run, everything
  python3 kconfig_tree.py                     # normal update
  python3 kconfig_tree.py --show              # view coloured tree
  python3 kconfig_tree.py --add-new-enabled   # after kernel upgrade
  python3 kconfig_tree.py --emit-kconfig > my.config

  Suppress an option:   move its line from doc → suppressed file
  Un-suppress:          delete its line from suppressed file

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
TEE   = "├─ "
LAST  = "└─ "
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
    value: Optional[str] = None   # filled from .config

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
        """Return list of ancestors from root down to (not including) self."""
        chain: list[KNode] = []
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
                KNode(kind="comment", name="",
                      prompt=f"[missing: {path}]"))
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

            _ATTR_RE.match(line)  # consume silently


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
    if node.symbol():
        idx[node.symbol()] = node
    for c in node.children:
        build_knode_index(c, idx)
    return idx


# ── Doc / suppressed file parser ───────────────────────────────────────────────
#
# Accepts four input formats per line:
#   1. Full doc:     ├─ [*] Some prompt (SYMBOL) # comment
#   2. Bare symbol:  CONFIG_SYMBOL  # comment
#   3. .config set:  CONFIG_SYMBOL=value
#   4. .config unset: # CONFIG_SYMBOL is not set
#   5. Pure comment: # free text
#
# Comment anchoring (resolved after all lines are parsed):
#   - A comment block directly below an option/menu → anchored below
#     (attached to the option above it, emitted after it)
#   - A comment block with a blank line above AND an option directly below
#     → anchored above (pre-comment of the option below, emitted before it)
#   - A comment block with blank above and nothing/blank below → freestanding,
#     attached to option above as trailing group

_DOC_SYMBOL_RE   = re.compile(r"\((\w+)\)\s*(?:#.*)?$")
_TRAILING_CMT_RE = re.compile(r" (#.*)$")
_BARE_SYMBOL_RE  = re.compile(r"^(CONFIG_\w+)\s*(?:#.*)?$")
_DOTCFG_SET_RE   = re.compile(r"^(CONFIG_\w+)=(.*)$")
_DOTCFG_UNSET_RE = re.compile(r"^#\s+(CONFIG_\w+)\s+is not set\s*$")
_STRUCT_LEADER   = re.compile(r"^(▶ |◆ |\[if )")
_KCONFIG_CMT_RE  = re.compile(r"^--- (.+) ---$")


@dataclass
class RawEntry:
    """One logical item parsed from a doc/suppressed file, before anchoring."""
    symbol: str           # CONFIG_FOO or ""
    trailing_comment: str # "# …" on same line as option, or ""
    file_value: str       # value from =y/.config format, or ""
    is_pure_comment: bool = False
    comment_text: str = ""  # content of pure comment line(s)
    blank_before: bool = False  # was there a blank line before this entry?
    # Filled after anchoring:
    pre_comments:  list[str] = field(default_factory=list)  # comment lines before
    post_comments: list[str] = field(default_factory=list)  # comment lines after


def _extract_symbol_from_doc_line(content_clean: str) -> str:
    """Try to extract CONFIG_SYMBOL from full doc format (SYMBOL) tag."""
    m = _DOC_SYMBOL_RE.search(content_clean)
    if m:
        return f"CONFIG_{m.group(1)}"
    return ""


def _extract_trailing(content: str) -> tuple[str, str]:
    """Return (content_without_trailing, trailing_comment)."""
    m = _TRAILING_CMT_RE.search(content)
    if m:
        return content[:m.start()], m.group(1)
    return content, ""


def _struct_prompt_key(content_clean: str) -> str:
    """Normalise structural line content to a lookup key (strip leader/closer)."""
    s = _STRUCT_LEADER.sub("", content_clean, count=1)
    s = _KCONFIG_CMT_RE.sub(r"\1", s)
    return s.strip()


class DocFileParser:
    """
    Parses a doc or suppressed file into RawEntry objects, resolves comment
    anchoring, and returns:
      symbol_index:  CONFIG_FOO → RawEntry
      struct_index:  normalised_prompt → RawEntry  (for structural lines)
      ordered:       list[RawEntry] in file order (non-comment entries)
    """

    def __init__(self, path: Path, knode_index: dict[str, KNode]):
        self.path = path
        self.knode_index = knode_index
        self.symbol_index: dict[str, RawEntry] = {}
        self.struct_index: dict[str, RawEntry] = {}
        self.ordered: list[RawEntry] = []
        self.warnings: list[str] = []
        self._parse()

    def _parse(self):
        if not self.path.exists():
            return

        raw_lines = self.path.read_text(errors="replace").splitlines()

        # ── Pass 1: classify every line ───────────────────────────────────────
        # Items: ("option", RawEntry) | ("comment", str) | ("blank",)

        items: list[tuple] = []
        for raw in raw_lines:
            line = strip_ansi(raw).rstrip()

            if not line:
                items.append(("blank",))
                continue

            # Check for .config unset format first (starts with #)
            m = _DOTCFG_UNSET_RE.match(line)
            if m:
                sym = m.group(1)
                entry = RawEntry(symbol=sym, trailing_comment="",
                                 file_value="n")
                items.append(("option", entry))
                continue

            _prefix, content = _strip_tree_prefix(line)

            # Pure comment (not .config unset)
            if content.startswith("#"):
                items.append(("comment", content))
                continue

            content_clean, trailing = _extract_trailing(content)
            content_clean = content_clean.strip()

            # .config set format: CONFIG_FOO=value
            m = _DOTCFG_SET_RE.match(content_clean)
            if m:
                sym, val = m.group(1), m.group(2)
                entry = RawEntry(symbol=sym, trailing_comment=trailing,
                                 file_value=val)
                items.append(("option", entry))
                continue

            # Bare symbol: CONFIG_FOO (with no = sign, no (NAME) tag)
            m = _BARE_SYMBOL_RE.match(content_clean)
            if m and not _DOC_SYMBOL_RE.search(content_clean):
                sym = m.group(1)
                entry = RawEntry(symbol=sym, trailing_comment=trailing,
                                 file_value="")
                items.append(("option", entry))
                continue

            # Full doc format with (SYMBOL) tag
            sym = _extract_symbol_from_doc_line(content_clean)
            if sym:
                entry = RawEntry(symbol=sym, trailing_comment=trailing,
                                 file_value="")
                items.append(("option", entry))
                continue

            # Structural line (menu/choice/if/kconfig-comment) — no symbol
            prompt_key = _struct_prompt_key(content_clean)
            if prompt_key:
                entry = RawEntry(symbol="", trailing_comment=trailing,
                                 file_value="", comment_text=prompt_key)
                items.append(("option", entry))
                continue

            # Unrecognised non-blank non-comment line — treat as freestanding comment
            items.append(("comment", content))

        # ── Pass 2: resolve comment anchoring ────────────────────────────────
        # Walk items, collecting comment runs and deciding where to attach them.

        n = len(items)
        i = 0
        option_entries: list[RawEntry] = []  # in file order

        while i < n:
            kind = items[i][0]

            if kind == "blank":
                i += 1
                continue

            if kind == "option":
                entry = items[i][1]
                entry.blank_before = (i > 0 and items[i-1][0] == "blank")
                option_entries.append(entry)
                i += 1
                continue

            if kind == "comment":
                # Collect contiguous comment lines
                cmt_lines: list[str] = []
                while i < n and items[i][0] == "comment":
                    cmt_lines.append(items[i][1])
                    i += 1

                blank_above = (len(option_entries) == 0 or
                               (i - len(cmt_lines) > 0 and
                                items[i - len(cmt_lines) - 1][0] == "blank"))

                # Is there an option immediately after (no blank)?
                next_is_option = (i < n and items[i][0] == "option")

                if blank_above and next_is_option:
                    # Pre-anchor: attach to the option below
                    # We defer: mark with a sentinel and resolve after
                    # collecting the next option
                    # Simplest: peek ahead and attach now
                    next_entry = items[i][1]
                    next_entry.pre_comments = cmt_lines
                elif option_entries:
                    # Post-anchor or freestanding: attach to option above
                    option_entries[-1].post_comments.extend(cmt_lines)
                # else: comments before any option — will be prepended to first
                continue

        # ── Pass 3: index entries ─────────────────────────────────────────────

        for entry in option_entries:
            if entry.symbol:
                if entry.symbol not in self.symbol_index:
                    self.symbol_index[entry.symbol] = entry
                    self.ordered.append(entry)
                else:
                    self.warnings.append(
                        f"Duplicate symbol {entry.symbol} in "
                        f"{self.path.name} — keeping first occurrence")
            elif entry.comment_text:
                # Structural entry — key by prompt
                key = entry.comment_text
                if key not in self.struct_index:
                    self.struct_index[key] = entry
                    self.ordered.append(entry)
            # Pure structural with no key: discard (was unrecognised)

        # ── Pass 4: validate symbols against Kconfig tree ─────────────────────
        bad = []
        for sym in list(self.symbol_index.keys()):
            if sym not in self.knode_index:
                self.warnings.append(
                    f"Symbol {sym} in {self.path.name} not found in "
                    f"Kconfig tree — line removed")
                del self.symbol_index[sym]
                self.ordered = [e for e in self.ordered if e.symbol != sym]

        # ── Pass 5: value mismatch warnings ───────────────────────────────────
        for sym, entry in self.symbol_index.items():
            if not entry.file_value:
                continue
            node = self.knode_index.get(sym)
            if node is None:
                continue
            live = (node.value or "").strip()
            fv   = entry.file_value.strip().strip('"')
            live_s = live.strip('"')
            if fv != live_s and fv not in ("", "n") and live_s not in ("", "n"):
                self.warnings.append(
                    f"Value mismatch for {sym}: "
                    f"file has ={fv}, .config has ={live_s} — using .config value")


# ── Ancestry helpers ──────────────────────────────────────────────────────────

def _shared_ancestor_depth(a: KNode, b: KNode) -> int:
    """How many ancestors do a and b share (from root)?"""
    aa = [id(n) for n in a.ancestry()] + [id(a)]
    bb = set([id(n) for n in b.ancestry()] + [id(b)])
    count = 0
    for aid in aa:
        if aid in bb:
            count += 1
    return count


def _needs_blank(prev_node: Optional[KNode], curr_node: KNode) -> bool:
    """
    Return True if a blank line should be inserted before curr_node because
    its parent-child relationship with prev_node would be ambiguous.
    Two nodes need a blank when they share no structural parent closer than
    the root, i.e. they come from completely different subtrees.
    """
    if prev_node is None:
        return False
    prev_anc = set(id(n) for n in prev_node.ancestry())
    curr_anc = set(id(n) for n in curr_node.ancestry())
    # If they share an ancestor other than root (depth > 1 shared), no blank.
    shared = prev_anc & curr_anc
    # Remove root (always shared); if nothing else shared → blank
    # The root has no parent, so we identify it by parent == None
    non_root_shared = [n for n in prev_node.ancestry() + curr_node.ancestry()
                       if id(n) in shared and n.parent is not None]
    return len(non_root_shared) == 0


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
    prompt  = node.prompt or node.name
    tag     = f" ({node.name})" if node.name else ""
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
    prompt  = node.prompt or node.name
    pstr    = bold(prompt) if node.is_active() else gray(prompt)
    tag     = gray(f" ({node.name})") if node.name else ""
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


def _blank() -> OutputLine:
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

        self._emitted:  set[str] = set()   # emitted CONFIG_ symbols
        self._struct_emitted: set[int] = set()  # id(KNode) for structural

        # Effective doc set: for --full, merge suppressed into doc first
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

        self._prev_knode: Optional[KNode] = None  # for orphan blank detection

    # ── helpers ────────────────────────────────────────────────────────────────

    def _push(self, plain: str, coloured: str, symbol: str = "",
              is_notice: bool = False, is_warning: bool = False):
        self.output.append(OutputLine(plain=plain, coloured=coloured,
                                      symbol=symbol, is_notice=is_notice,
                                      is_warning=is_warning))

    def _push_blank(self):
        # Avoid double blanks
        if self.output and not self.output[-1].is_blank:
            self.output.append(_blank())

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

    def _get_trailing(self, sym: str) -> str:
        e = self._eff_doc.get(sym) or self.sup.symbol_index.get(sym)
        return e.trailing_comment if e else ""

    def _get_pre_comments(self, sym: str) -> list[str]:
        e = self._eff_doc.get(sym) or self.sup.symbol_index.get(sym)
        return e.pre_comments if e else []

    def _get_post_comments(self, sym: str) -> list[str]:
        e = self._eff_doc.get(sym) or self.sup.symbol_index.get(sym)
        return e.post_comments if e else []

    def _get_struct_trailing(self, key: str) -> str:
        e = self._eff_struct.get(key) or self.sup.struct_index.get(key)
        return e.trailing_comment if e else ""

    def _get_struct_pre(self, key: str) -> list[str]:
        e = self._eff_struct.get(key) or self.sup.struct_index.get(key)
        return e.pre_comments if e else []

    def _get_struct_post(self, key: str) -> list[str]:
        e = self._eff_struct.get(key) or self.sup.struct_index.get(key)
        return e.post_comments if e else []

    def _emit_comments(self, lines: list[str], prefix: str):
        for cmt in lines:
            self._push(f"{prefix}{cmt}", f"{prefix}{gray(cmt)}")

    def _maybe_blank(self, curr_node: KNode):
        """Insert blank if curr_node and prev_node have unrelated ancestry."""
        if _needs_blank(self._prev_knode, curr_node):
            self._push_blank()

    # ── tree walk ──────────────────────────────────────────────────────────────

    def run(self) -> list[OutputLine]:
        hdr = f"⚙ {self.root.prompt or 'Linux Kernel Configuration'}"
        self._push(hdr, bold(cyan(hdr)))
        self._check_conflicts()
        self._recurse(self.root, prefix="", depth=0)
        return self.output

    def _check_conflicts(self):
        for sym in self.doc.symbol_index:
            if sym in self.sup.symbol_index:
                msg = (f"Conflict: {sym} in both doc and suppressed "
                       f"— doc wins, removing from suppressed")
                self.warnings.append(msg)

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
                    # Notice for active options in neither file
                    sym = child.symbol()
                    if (child.is_active()
                            and not self._in_eff_doc(sym)
                            and not self._in_sup(sym)
                            and not self.add_new
                            and not self.add_new_en
                            and not self.full):
                        # Only notice leaf-active, not menuconfig containers
                        # whose children are already tracked
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

    def _recurse(self, parent: KNode, prefix: str, depth: int):
        children = self._visible_children(parent)
        for idx, child in enumerate(children):
            is_last   = idx == len(children) - 1
            connector = LAST if is_last else TEE
            child_pfx = prefix + (BLANK if is_last else PIPE)
            self._emit_child(child, prefix, connector, child_pfx)

    def _emit_child(self, node: KNode, prefix: str, connector: str,
                    child_pfx: str):

        # ── structural ─────────────────────────────────────────────────────────
        if node.kind in ("menu", "choice", "if", "comment"):
            nid = id(node)
            if nid in self._struct_emitted:
                self._recurse(node, child_pfx, 0)
                return
            self._struct_emitted.add(nid)

            self._maybe_blank(node)
            key      = node.prompt.strip() if node.prompt else ""
            trailing = self._get_struct_trailing(key)

            for cmt in self._get_struct_pre(key):
                self._push(f"{prefix}{cmt}", f"{prefix}{gray(cmt)}")

            p = _plain_line(node, prefix, connector, trailing)
            c = _colour_line(node, prefix, connector, trailing)
            self._push(p, c)

            for cmt in self._get_struct_post(key):
                self._push(f"{child_pfx}{cmt}", f"{child_pfx}{gray(cmt)}")

            self._prev_knode = node
            self._recurse(node, child_pfx, 0)
            return

        # ── config / menuconfig ────────────────────────────────────────────────
        sym = node.symbol()
        if sym in self._emitted:
            return
        self._emitted.add(sym)

        self._maybe_blank(node)

        in_eff = self._in_eff_doc(sym)
        in_sup = self._in_sup(sym)

        # Notices for newly added symbols
        if not in_eff and not in_sup and (
                self.add_new or self.add_new_en or self.full):
            notice_p = f"# + New option added to doc: {sym}"
            self._push(notice_p, green(notice_p), is_notice=True)
            self.notices.append(f"New option added to doc: {sym}")
        elif not in_eff and in_sup and self.full:
            notice_p = f"# + Restored from suppressed: {sym}"
            self._push(notice_p, green(notice_p), is_notice=True)

        trailing = self._get_trailing(sym)

        for cmt in self._get_pre_comments(sym):
            self._push(f"{prefix}{cmt}", f"{prefix}{gray(cmt)}")

        p = _plain_line(node, prefix, connector, trailing)
        c = _colour_line(node, prefix, connector, trailing)
        self._push(p, c, symbol=sym)

        for cmt in self._get_post_comments(sym):
            self._push(f"{child_pfx}{cmt}", f"{child_pfx}{gray(cmt)}")

        self._prev_knode = node

        if node.kind == "menuconfig" and node.children:
            self._recurse(node, child_pfx, 0)


# ── Updated suppressed file computation ───────────────────────────────────────

def compute_new_suppressed(sup: DocFileParser,
                           knode_index: dict[str, KNode],
                           full: bool) -> tuple[list[RawEntry], list[str]]:
    notices: list[str] = []
    if full:
        return [], notices
    new_entries: list[RawEntry] = []
    for entry in sup.ordered:
        sym = entry.symbol
        if sym and sym not in knode_index:
            notices.append(
                f"Dropped vanished symbol from suppressed: {sym}")
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
    """Write plain doc file, skip notice/warning lines, collapse double blanks."""
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
    """
    Write suppressed file in doc format, with orphan blank lines inserted
    between entries from unrelated subtrees.
    """
    if not entries:
        path.write_text("")
        return

    with path.open("w") as f:
        prev_node: Optional[KNode] = None
        for entry in entries:
            sym = entry.symbol
            node = knode_index.get(sym) if sym else None

            if node and _needs_blank(prev_node, node):
                f.write("\n")

            for cmt in entry.pre_comments:
                f.write(cmt + "\n")

            if sym and node:
                # Write in full doc format (glyph comes from live .config)
                tc = f" {entry.trailing_comment}" if entry.trailing_comment else ""
                f.write(f"{node.raw_glyph()} {node.prompt or node.name}"
                        f" ({node.name}){tc}\n")
            elif entry.comment_text:
                f.write(entry.comment_text + "\n")

            for cmt in entry.post_comments:
                f.write(cmt + "\n")

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

        # Remove from suppressed any symbols now in doc (conflict resolution)
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

    all_warnings = merger.warnings
    if all_warnings:
        print(file=sys.stderr)
        print(f"{magenta('WARNINGS')} ({len(all_warnings)}):", file=sys.stderr)
        for w in all_warnings:
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
