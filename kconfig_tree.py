#!/usr/bin/env python3
"""
kconfig_tree.py — Kernel configuration documentation tool.

Maintains a human-editable documentation tree of kernel config options,
tracking which are active and merging updates across kernel versions.

THREE-FILE MODEL
────────────────
  .config                   Kernel truth  (what is active)
  kconfig_doc.txt           User's allowlist  (what to show and document)
  kconfig_doc_suppressed.txt  User's denylist  (what to permanently hide)

MERGE RULES (per symbol, every run)
────────────────────────────────────
  In doc                         → emit, update glyph
  In suppressed                  → never emit (unless --full)
  In neither, inactive           → silent, notify user to use --add-new
  In neither, active             → notify user to use --add-new-enabled
  In both (conflict)             → doc wins, warn, remove from suppressed
  --add-new                      → adds all symbols in neither file to doc
  --add-new-enabled              → adds only [*]/[M] symbols in neither to doc
  --full                         → merges suppressed→doc, adds all remaining

USAGE
─────
  # First run — populate doc with all active options
  python3 kconfig_tree.py --add-new-enabled

  # First run — populate doc with everything
  python3 kconfig_tree.py --full

  # Normal update run (update values, respect suppressions)
  python3 kconfig_tree.py

  # View the tree with colours
  python3 kconfig_tree.py --show

  # After kernel upgrade — add newly appeared active options
  python3 kconfig_tree.py --add-new-enabled

  # Suppress an active option permanently
  #   Move its line from kconfig_doc.txt to kconfig_doc_suppressed.txt

  # Un-suppress an option
  #   Delete its line from kconfig_doc_suppressed.txt

  # Re-add everything, clearing suppression
  python3 kconfig_tree.py --full

  # Emit a .config-format file
  python3 kconfig_tree.py --emit-kconfig > my.config

OPTIONS
───────
  --kconfig    PATH   Top-level Kconfig file        (default: Kconfig)
  --dotconfig  PATH   Kernel .config file           (default: .config)
  --doc        PATH   Doc file                      (default: kconfig_doc.txt)
  --suppressed PATH   Suppressed file               (default: kconfig_doc_suppressed.txt)
  --arch       ARCH   Architecture                  (default: arm64)
  --add-new           Add all symbols absent from both files to doc
  --add-new-enabled   Add only active symbols absent from both files to doc
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
def red(t):     return _c("31", t)

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)

# ── Tree characters ────────────────────────────────────────────────────────────
# Single-line box, single space, no trailing space.

PIPE  = "│ "
TEE   = "├─ "
LAST  = "└─ "
BLANK = "  "

_TREE_CHARS = set("│├└─ ")

def _strip_tree_prefix(line: str) -> tuple[str, str]:
    """Split off leading tree-drawing characters. Returns (prefix, content)."""
    i = 0
    while i < len(line) and line[i] in _TREE_CHARS:
        i += 1
    return line[:i], line[i:]

def _depth_of(line: str) -> int:
    """Approximate depth from prefix width. PIPE/BLANK unit = 2 chars."""
    prefix, _ = _strip_tree_prefix(line)
    return len(prefix) // 2


# ── KNode ─────────────────────────────────────────────────────────────────────

@dataclass
class KNode:
    kind: str         # menu | config | menuconfig | choice | comment | if
    name: str         # symbol name without CONFIG_ prefix, or ""
    prompt: str = ""
    type_: str = ""
    help_: str = ""
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

    def any_descendant_active(self) -> bool:
        if self.is_active():
            return True
        return any(c.any_descendant_active() for c in self.children)

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
                    if cur_node:
                        cur_node.help_ += line + "\n"
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


# ── Doc-file parser ────────────────────────────────────────────────────────────
#
# Symbols are tagged as  (SYMBOL_NAME)  in config/menuconfig lines.
# Structural lines (menu/choice/if) are identified by their content prefix:
#   ▶  for menu, ◆ for choice, --- for Kconfig comment, [ for if-block.
# Trailing comments (#…) are preserved on all line types.
# Pure comment lines (content starts with #) are anchored to the entry above.

# Matches the (SYMBOL_NAME) tag in a config line
_DOC_SYMBOL_RE = re.compile(r"\((\w+)\)\s*(?:#.*)?$")
# Matches trailing comment, preceded by single space
_TRAILING_CMT  = re.compile(r" (#.*)$")
# Matches structural header content prefixes
_STRUCT_PREFIX = re.compile(r"^(▶|◆|---|---|\[if )")


@dataclass
class DocEntry:
    raw_line: str           # as stored in file (plain, no ANSI)
    symbol: str             # CONFIG_FOO, or "" for structural/comment lines
    anchor_key: str         # symbol for config lines; menu prompt for structural
    trailing_comment: str   # "# …" preserved from this line, or ""
    attached_comments: list[str] = field(default_factory=list)
    is_pure_comment: bool = False


def _parse_doc_line(line: str) -> DocEntry:
    """Parse one non-empty, non-blank line from a doc file."""
    _prefix, content = _strip_tree_prefix(line)

    # Pure comment?
    if content.startswith("#"):
        return DocEntry(raw_line=line, symbol="", anchor_key="",
                        trailing_comment="", is_pure_comment=True)

    # Extract trailing comment
    trailing = ""
    m = _TRAILING_CMT.search(content)
    if m:
        trailing = m.group(1)
        content_clean = content[: m.start()]
    else:
        content_clean = content

    # Config/menuconfig line: has (SYMBOL_NAME) tag
    symbol = ""
    anchor_key = ""
    ms = _DOC_SYMBOL_RE.search(content_clean)
    if ms:
        symbol = f"CONFIG_{ms.group(1)}"
        anchor_key = symbol
    else:
        # Structural line: use trimmed content as anchor
        anchor_key = content_clean.strip()

    return DocEntry(raw_line=line, symbol=symbol, anchor_key=anchor_key,
                    trailing_comment=trailing)


def parse_doc(path: Path) -> tuple[list[DocEntry], dict[str, int]]:
    """
    Returns (entries, index) where index maps anchor_key → entry index.
    Pure comment lines are attached to the nearest preceding non-comment entry.
    """
    entries: list[DocEntry] = []
    index: dict[str, int] = {}

    if not path.exists():
        return entries, index

    for raw in path.read_text(errors="replace").splitlines():
        line = strip_ansi(raw).rstrip()
        if not line:
            continue

        entry = _parse_doc_line(line)

        if entry.is_pure_comment:
            # Attach to last non-comment entry
            for e in reversed(entries):
                if not e.is_pure_comment:
                    e.attached_comments.append(line)
                    break
            else:
                entries.append(entry)
            continue

        entries.append(entry)
        if entry.anchor_key and entry.anchor_key not in index:
            index[entry.anchor_key] = len(entries) - 1

    return entries, index


# ── Line renderers ─────────────────────────────────────────────────────────────

def _body(node: KNode) -> tuple[str, str]:
    """Return (plain_body, coloured_body) without prefix/connector/comment."""
    glyph_p = node.raw_glyph()
    glyph_c = node.coloured_glyph()

    if node.kind == "menu":
        p = f"▶ {node.prompt}" if node.prompt else "▶ (menu)"
        c = bold(cyan(p))
    elif node.kind == "choice":
        p = f"◆ {node.prompt or '(choice)'}"
        c = bold(p)
    elif node.kind == "comment":
        p = f"--- {node.prompt} ---"
        c = gray(p)
    elif node.kind == "if":
        p = f"[{node.prompt}]"
        c = gray(p)
    else:
        prompt = node.prompt or node.name
        tag_p  = f" ({node.name})" if node.name else ""
        tag_c  = gray(f" ({node.name})") if node.name else ""
        if glyph_p:
            p = f"{glyph_p} {prompt}{tag_p}"
            c = f"{glyph_c} {bold(prompt) if node.is_active() else gray(prompt)}{tag_c}"
        else:
            p = f"{prompt}{tag_p}"
            c = f"{bold(prompt) if node.is_active() else gray(prompt)}{tag_c}"

    return p, c


def _plain_line(node: KNode, prefix: str, connector: str,
                trailing: str = "") -> str:
    body_p, _ = _body(node)
    tc = f" {trailing}" if trailing else ""
    return f"{prefix}{connector}{body_p}{tc}"


def _colour_line(node: KNode, prefix: str, connector: str,
                 trailing: str = "") -> str:
    _, body_c = _body(node)
    tc = f" {gray(trailing)}" if trailing else ""
    return f"{prefix}{connector}{body_c}{tc}"


# ── OutputLine ─────────────────────────────────────────────────────────────────

@dataclass
class OutputLine:
    plain:      str
    coloured:   str
    symbol:     str  = ""   # CONFIG_FOO or "" — for --emit-kconfig lookup
    is_notice:  bool = False
    is_warning: bool = False


# ── Merger ─────────────────────────────────────────────────────────────────────

class Merger:
    """
    Walks the Kconfig tree and produces OutputLines using the three-file model.

    Per symbol:
      in doc                         → emit, update glyph
      in suppressed                  → skip (unless --full)
      in neither, inactive           → skip, notify (suggest --add-new)
      in neither, active             → skip, notify (suggest --add-new-enabled)
      in both (conflict)             → doc wins, warn
      --add-new                      → add inactive+active symbols in neither
      --add-new-enabled              → add only active symbols in neither
      --full                         → merge suppressed→doc, add all remaining
    """

    def __init__(
        self,
        root:         KNode,
        doc_entries:  list[DocEntry],
        doc_index:    dict[str, int],
        sup_entries:  list[DocEntry],
        sup_index:    dict[str, int],
        knode_index:  dict[str, KNode],
        add_new:      bool = False,
        add_new_en:   bool = False,
        full:         bool = False,
    ):
        self.root        = root
        self.doc_entries = doc_entries
        self.doc_index   = doc_index
        self.sup_entries = sup_entries
        self.sup_index   = sup_index
        self.knode_index = knode_index
        self.add_new     = add_new
        self.add_new_en  = add_new_en
        self.full        = full

        self.output:   list[OutputLine] = []
        self.notices:  list[str] = []    # informational messages
        self.warnings: list[str] = []    # conflict warnings

        # What goes into the new suppressed file after this run
        # (used by --full to clear it, otherwise preserved as-is)
        self.new_sup_entries: list[DocEntry] = []

        self._emitted: set[str] = set()  # CONFIG_FOO or __struct_<id>

    # ── helpers ────────────────────────────────────────────────────────────────

    def _push(self, plain: str, coloured: str, symbol: str = "",
              is_notice: bool = False, is_warning: bool = False):
        self.output.append(OutputLine(plain=plain, coloured=coloured,
                                      symbol=symbol, is_notice=is_notice,
                                      is_warning=is_warning))

    def _in_doc(self, key: str) -> bool:
        return bool(key) and key in self.doc_index

    def _in_sup(self, key: str) -> bool:
        return bool(key) and key in self.sup_index

    def _doc_trailing(self, key: str) -> str:
        if not self._in_doc(key):
            return ""
        return self.doc_entries[self.doc_index[key]].trailing_comment

    def _doc_comments(self, key: str) -> list[str]:
        if not self._in_doc(key):
            return []
        return self.doc_entries[self.doc_index[key]].attached_comments

    def _sup_trailing(self, key: str) -> str:
        if not self._in_sup(key):
            return ""
        return self.sup_entries[self.sup_index[key]].trailing_comment

    def _sup_comments(self, key: str) -> list[str]:
        if not self._in_sup(key):
            return []
        return self.sup_entries[self.sup_index[key]].attached_comments

    def _struct_key(self, node: KNode) -> str:
        return f"__struct_{id(node)}"

    # ── decision for a config/menuconfig node ─────────────────────────────────

    def _should_emit(self, node: KNode) -> bool:
        """Should this config/menuconfig node appear in the output?"""
        sym = node.symbol()
        if self.full:
            return True
        if self._in_doc(sym):
            return True
        if self._in_sup(sym):
            return False
        # In neither file
        if self.add_new:
            return True
        if self.add_new_en and node.is_active():
            return True
        return False

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
            elif child.kind in ("menu", "choice", "if"):
                if self._has_visible_children(child):
                    out.append(child)
            elif child.kind == "comment":
                out.append(child)
        return out

    # ── run ────────────────────────────────────────────────────────────────────

    def run(self) -> list[OutputLine]:
        hdr = f"⚙ {self.root.prompt or 'Linux Kernel Configuration'}"
        self._push(hdr, bold(cyan(hdr)))

        # If --full, seed suppressed entries back into the doc index so they
        # are treated as "in doc"
        if self.full:
            for entry in self.sup_entries:
                if entry.is_pure_comment or not entry.anchor_key:
                    continue
                if entry.anchor_key not in self.doc_index:
                    self.doc_entries.append(entry)
                    self.doc_index[entry.anchor_key] = len(self.doc_entries) - 1
            # Suppressed file will be written empty
            self.new_sup_entries = []
        else:
            # Suppressed file unchanged
            self.new_sup_entries = list(self.sup_entries)

        # Collect notices for symbols in neither file
        self._collect_untracked_notices()

        # Check for conflicts (in both doc and suppressed)
        self._check_conflicts()

        # Walk the tree
        self._recurse(self.root, prefix="", depth=0)

        return self.output

    def _collect_untracked_notices(self):
        """
        For every symbol in the Kconfig tree that is in neither doc nor
        suppressed, emit a notice suggesting how to add it.
        """
        self._walk_untracked(self.root)

    def _walk_untracked(self, node: KNode):
        sym = node.symbol()
        if sym and not self._in_doc(sym) and not self._in_sup(sym):
            if not self.full and not self.add_new:
                if node.is_active():
                    if not self.add_new_en:
                        self.notices.append(
                            f"Active option not in doc: {sym} "
                            f"(use --add-new-enabled or --add-new to include)")
                else:
                    if not self.add_new and not self.add_new_en:
                        pass  # silent for inactive — too noisy
        for c in node.children:
            self._walk_untracked(c)

    def _check_conflicts(self):
        """Warn about and resolve symbols present in both doc and suppressed."""
        for sym, didx in self.doc_index.items():
            if sym in self.sup_index:
                msg = (f"Conflict: {sym} is in both doc and suppressed files "
                       f"— doc wins; removing from suppressed")
                self.warnings.append(msg)
                # Remove from new_sup_entries
                self.new_sup_entries = [
                    e for e in self.new_sup_entries
                    if e.anchor_key != sym
                ]

    # ── recursive tree walk ────────────────────────────────────────────────────

    def _recurse(self, parent: KNode, prefix: str, depth: int):
        children = self._visible_children(parent)
        for idx, child in enumerate(children):
            is_last   = (idx == len(children) - 1)
            connector = LAST if is_last else TEE
            child_pfx = prefix + (BLANK if is_last else PIPE)
            self._emit_child(child, prefix, connector, child_pfx, depth)

    def _emit_child(self, node: KNode, prefix: str, connector: str,
                    child_pfx: str, depth: int):
        sym = node.symbol()

        # ── structural nodes ───────────────────────────────────────────────────
        if node.kind in ("menu", "choice", "if", "comment"):
            sk = self._struct_key(node)
            if sk not in self._emitted:
                self._emitted.add(sk)
                # Use menu prompt as anchor key for trailing comment lookup
                anchor = node.prompt.strip() if node.prompt else ""
                trailing = ""
                attached = []
                if anchor and self._in_doc(anchor):
                    trailing = self._doc_trailing(anchor)
                    attached = self._doc_comments(anchor)
                p = _plain_line(node, prefix, connector, trailing)
                c = _colour_line(node, prefix, connector, trailing)
                self._push(p, c)
                for cmt in attached:
                    _, content = _strip_tree_prefix(cmt)
                    self._push(f"{child_pfx}{content}",
                               f"{child_pfx}{gray(content)}")
            self._recurse(node, child_pfx, depth + 1)
            return

        # ── config / menuconfig ────────────────────────────────────────────────
        if sym in self._emitted:
            return
        self._emitted.add(sym)

        in_doc = self._in_doc(sym)
        in_sup = self._in_sup(sym)

        # Determine which trailing comment / attached comments to use.
        # If being newly added via --add-new* or --full (not in doc yet),
        # check suppressed for preserved comments.
        if in_doc:
            trailing = self._doc_trailing(sym)
            attached = self._doc_comments(sym)
        elif in_sup and self.full:
            trailing = self._sup_trailing(sym)
            attached = self._sup_comments(sym)
        else:
            trailing = ""
            attached = []

        # Notice if this is a newly added symbol (in neither originally)
        if not in_doc and not in_sup and (self.add_new or self.add_new_en or self.full):
            notice_p = f"# + New option added to doc: {sym}"
            notice_c = green(notice_p)
            self._push(notice_p, notice_c, is_notice=True)
            self.notices.append(f"New option added to doc: {sym}")
        elif not in_doc and in_sup and self.full:
            notice_p = f"# + Restored from suppressed: {sym}"
            notice_c = green(notice_p)
            self._push(notice_p, notice_c, is_notice=True)
            self.notices.append(f"Restored from suppressed: {sym}")

        p = _plain_line(node, prefix, connector, trailing)
        c = _colour_line(node, prefix, connector, trailing)
        self._push(p, c, symbol=sym)

        for cmt in attached:
            _, content = _strip_tree_prefix(cmt)
            self._push(f"{child_pfx}{content}",
                       f"{child_pfx}{gray(content)}")

        if node.kind == "menuconfig" and node.children:
            self._recurse(node, child_pfx, depth + 1)


# ── Suppressed file updater ────────────────────────────────────────────────────

def compute_new_suppressed(
    sup_entries:  list[DocEntry],
    doc_index:    dict[str, int],
    knode_index:  dict[str, KNode],
    full:         bool,
) -> tuple[list[DocEntry], list[str]]:
    """
    Determine what goes into the updated suppressed file.

    Rules:
    - If --full: suppressed file is cleared (everything moved to doc).
    - Otherwise: keep existing suppressed entries that still exist in Kconfig.
      Entries whose symbol has vanished from Kconfig are silently dropped.

    Note: Moving deleted-from-doc entries into suppressed is NOT done
    automatically — the suppressed file is user-managed.  The script only
    prunes stale entries (vanished symbols) from it.

    Returns (new_entries, notices).
    """
    notices: list[str] = []
    if full:
        return [], notices

    new_entries: list[DocEntry] = []
    for entry in sup_entries:
        if entry.is_pure_comment:
            new_entries.append(entry)
            continue
        sym = entry.symbol
        if not sym:
            # Structural suppressed entry — keep as-is
            new_entries.append(entry)
            continue
        if sym not in knode_index:
            notices.append(f"Dropped vanished symbol from suppressed file: {sym}")
            continue
        new_entries.append(entry)

    return new_entries, notices


# ── Emit Linux .config format ─────────────────────────────────────────────────

# Reuse _DOC_SYMBOL_RE for extracting symbols from output lines
_DOC_SYMBOL_RE = re.compile(r"\((\w+)\)\s*(?:#.*)?$")


def emit_kconfig_format(output_lines: list[OutputLine],
                        knode_index: dict[str, KNode]):
    print("# Generated by kconfig_tree.py")
    print("#")
    seen: set[str] = set()
    for ol in output_lines:
        if ol.is_notice or ol.is_warning:
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


# ── Write doc / suppressed ────────────────────────────────────────────────────

def write_plain(path: Path, lines: list[OutputLine], skip_notices: bool = True):
    with path.open("w") as f:
        for ol in lines:
            if ol.is_warning:
                continue
            if skip_notices and ol.is_notice:
                continue
            f.write(ol.plain + "\n")


def write_suppressed(path: Path, entries: list[DocEntry]):
    if not entries:
        # Write empty file to signal the suppressed list is clear
        path.write_text("")
        return
    with path.open("w") as f:
        for entry in entries:
            f.write(entry.raw_line + "\n")
            for cmt in entry.attached_comments:
                f.write(cmt + "\n")


# ── Post-render depth / filter ─────────────────────────────────────────────────

def _filter_output(lines: list[OutputLine],
                   max_depth: Optional[int],
                   filter_word: Optional[str]) -> list[OutputLine]:
    fw = filter_word.lower() if filter_word else None
    result = []
    for ol in lines:
        if ol.is_notice or ol.is_warning:
            result.append(ol)
            continue
        d = _depth_of(ol.plain)
        if max_depth is not None and d > max_depth:
            continue
        if fw and fw not in ol.plain.lower():
            continue
        result.append(ol)
    return result


# ── Summary stats ──────────────────────────────────────────────────────────────

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
    ap.add_argument("--kconfig",      default="Kconfig",
                    help="Top-level Kconfig file (default: Kconfig)")
    ap.add_argument("--dotconfig",    default=".config",
                    help="Kernel .config file (default: .config)")
    ap.add_argument("--doc",          default=DEFAULT_DOC,
                    help=f"Doc file (default: {DEFAULT_DOC})")
    ap.add_argument("--suppressed",   default=DEFAULT_SUP,
                    help=f"Suppressed file (default: {DEFAULT_SUP})")
    ap.add_argument("--arch",         default="arm64",
                    help="Architecture (default: arm64)")
    ap.add_argument("--add-new",      action="store_true",
                    help="Add all symbols absent from both files to doc")
    ap.add_argument("--add-new-enabled", action="store_true",
                    help="Add only active ([*]/[M]) symbols absent from both files")
    ap.add_argument("--full",         action="store_true",
                    help="Restore suppressed + add all remaining symbols")
    ap.add_argument("--emit-kconfig", action="store_true",
                    help="Print Linux .config format to stdout, then exit")
    ap.add_argument("--show",         action="store_true",
                    help="Show coloured tree on stdout")
    ap.add_argument("--depth",        type=int, default=None,
                    help="Max tree depth (with --show)")
    ap.add_argument("--filter",       default=None,
                    help="Show subtrees containing WORD (with --show)")
    ap.add_argument("--no-color",     action="store_true",
                    help="Disable ANSI colours")
    ap.add_argument("--no-doc",       action="store_true",
                    help="Do not write any files this run")
    args = ap.parse_args()

    if args.no_color or args.emit_kconfig:
        USE_COLOR = False

    kernel_root    = Path(".").resolve()
    kconfig_path   = Path(args.kconfig)
    dotconfig_path = Path(args.dotconfig)
    doc_path       = Path(args.doc)
    sup_path       = Path(args.suppressed)

    if not kconfig_path.exists():
        sys.exit(
            f"ERROR: Kconfig file not found: {kconfig_path}\n"
            "Run this script from your kernel source root directory."
        )

    # 1. Parse Kconfig tree
    print("Parsing Kconfig hierarchy …", file=sys.stderr)
    parser = KconfigParser(arch=args.arch, kernel_root=kernel_root)
    root   = parser.parse(kconfig_path)

    # 2. Load .config and annotate
    print(f"Loading {dotconfig_path} …", file=sys.stderr)
    cfg = load_dotconfig(dotconfig_path)
    annotate_tree(root, cfg)
    knode_index = build_knode_index(root)

    # 3. Parse doc and suppressed files
    if doc_path.exists():
        print(f"Reading {doc_path} …", file=sys.stderr)
    doc_entries, doc_index = parse_doc(doc_path)

    if sup_path.exists():
        print(f"Reading {sup_path} …", file=sys.stderr)
    sup_entries, sup_index = parse_doc(sup_path)

    # 4. Merge
    merger = Merger(
        root        = root,
        doc_entries = doc_entries,
        doc_index   = doc_index,
        sup_entries = sup_entries,
        sup_index   = sup_index,
        knode_index = knode_index,
        add_new     = args.add_new,
        add_new_en  = args.add_new_enabled,
        full        = args.full,
    )
    output_lines = merger.run()

    # 5. Compute updated suppressed entries
    new_sup, sup_notices = compute_new_suppressed(
        sup_entries  = merger.new_sup_entries,
        doc_index    = doc_index,
        knode_index  = knode_index,
        full         = args.full,
    )

    # 6. Apply depth/filter (only meaningful with --show)
    show_lines = output_lines
    if args.show and (args.depth is not None or args.filter):
        show_lines = _filter_output(output_lines, args.depth, args.filter)

    # 7. --emit-kconfig
    if args.emit_kconfig:
        emit_kconfig_format(output_lines, knode_index)
        return

    # 8. Write files
    if not args.no_doc:
        write_plain(doc_path, output_lines, skip_notices=True)
        print(f"Doc written → {doc_path}", file=sys.stderr)

        write_suppressed(sup_path, new_sup)
        if args.full and not new_sup:
            print(f"Suppressed file cleared → {sup_path}", file=sys.stderr)
        else:
            print(f"Suppressed file updated → {sup_path}", file=sys.stderr)

    # 9. --show: coloured tree to stdout
    if args.show:
        for ol in show_lines:
            print(ol.coloured)

    # 10. Notices (always to stderr; also inline in --show above via output_lines)
    all_notices = merger.notices + sup_notices
    if all_notices:
        print(file=sys.stderr)
        print("NOTICES:", file=sys.stderr)
        for n in all_notices:
            print(f"  + {n}", file=sys.stderr)

    # 11. Warnings
    if merger.warnings:
        print(file=sys.stderr)
        print(f"{magenta('WARNINGS')} ({len(merger.warnings)}):", file=sys.stderr)
        for w in merger.warnings:
            print(f"  {magenta('!')} {w}", file=sys.stderr)

    # 12. Stats (always)
    stats: dict = {"total": 0, "yes": 0, "module": 0, "no": 0,
                   "unset": 0, "other": 0}
    collect_stats(root, stats)
    active_in_doc = sum(
        1 for ol in output_lines
        if ol.symbol and not ol.is_notice and not ol.is_warning
        and knode_index.get(ol.symbol, KNode("", "")).is_active()
    )
    print(file=sys.stderr)
    print(
        f"{bold('Kconfig total')}: "
        f"{green(str(stats['yes']))} built-in  "
        f"{yellow(str(stats['module']))} module  "
        f"{gray(str(stats['no']))} disabled  "
        f"{gray(str(stats['unset']))} unset  "
        f"/ {stats['total']} symbols",
        file=sys.stderr,
    )
    print(
        f"{bold('Doc file')}:      "
        f"{len([o for o in output_lines if o.symbol and not o.is_notice])} symbols tracked  "
        f"({active_in_doc} active)",
        file=sys.stderr,
    )
    print(
        f"{bold('Suppressed')}:    "
        f"{len([e for e in new_sup if e.symbol])} symbols hidden",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
