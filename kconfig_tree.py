#!/usr/bin/env python3
"""
kconfig_tree.py — Semi-automatic kernel configuration documentation tool.

Renders a menuconfig-style text tree showing which options are active.
On each run it merges the Kconfig hierarchy with an existing documentation
file, preserving human comments and suppression decisions, and always
writes a canonical plain-text doc file (kconfig_doc.txt by default).

────────────────────────────────────────────────────────────────────────
TYPICAL WORKFLOW
────────────────────────────────────────────────────────────────────────
  # First run — generate full doc from scratch
  python3 kconfig_tree.py --full

  # Subsequent runs — update values, keep suppressions & comments
  python3 kconfig_tree.py

  # After a kernel upgrade — add only new symbols to the doc
  python3 kconfig_tree.py --add-new

  # Re-expand everything (e.g. after big upgrade), keep all comments
  python3 kconfig_tree.py --full

  # Emit a .config-format file from the current doc state
  python3 kconfig_tree.py --emit-kconfig > my.config

────────────────────────────────────────────────────────────────────────
OPTIONS
────────────────────────────────────────────────────────────────────────
  --kconfig    PATH   Top-level Kconfig file        (default: Kconfig)
  --dotconfig  PATH   Kernel .config file           (default: .config)
  --doc        PATH   Doc file to read/write        (default: kconfig_doc.txt)
  --arch       ARCH   Architecture                  (default: arm64)
  --add-new           Add symbols missing from doc as inactive entries
  --full              Re-add ALL symbols; preserve existing comments
  --emit-kconfig      Print Linux config format to stdout, then exit
  --depth      N      Max tree depth to display
  --filter     WORD   Show only subtrees containing WORD
  --no-color          Disable ANSI colours
  --no-doc            Do not write the doc file this run
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
def red(t):     return _c("31", t)
def magenta(t): return _c("35", t)

ANSI_ESCAPE = re.compile(r"\033\[[0-9;]*m")

def strip_ansi(s: str) -> str:
    return ANSI_ESCAPE.sub("", s)

# ── Tree drawing characters ────────────────────────────────────────────────────

PIPE  = "│   "
TEE   = "├── "
LAST  = "└── "
BLANK = "    "
_TREE_CHARS = set("│├└─ ")

# ── KNode ─────────────────────────────────────────────────────────────────────

@dataclass
class KNode:
    """One node in the Kconfig tree."""
    kind: str         # menu | config | menuconfig | choice | comment | if
    name: str         # symbol name without CONFIG_ prefix, or ""
    prompt: str = ""
    type_: str = ""   # bool | tristate | int | hex | string
    help_: str = ""
    depends: str = ""
    children: list = field(default_factory=list)
    parent: Optional["KNode"] = field(default=None, repr=False)
    file: str = ""
    lineno: int = 0
    value: Optional[str] = None  # filled from .config

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
        """Plain-text status glyph, no colour codes."""
        if self.kind in ("menu", "choice", "comment", "if"):
            return ""
        if self.value is None:
            return "[ ]"
        v = self.value.strip().strip('"')
        if v == "y":       return "[*]"
        if v == "m":       return "[M]"
        if v in ("n", ""): return "[ ]"
        return f"[={v}]"

    def coloured_glyph(self) -> str:
        if self.kind in ("menu", "choice", "comment", "if"):
            return ""
        if self.value is None:
            return gray("[ ]")
        v = self.value.strip().strip('"')
        if v == "y":       return green("[*]")
        if v == "m":       return yellow("[M]")
        if v in ("n", ""): return gray("[ ]")
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


# ── Symbol index over the Kconfig tree ────────────────────────────────────────

def build_knode_index(node: KNode,
                      idx: Optional[dict[str, KNode]] = None
                      ) -> dict[str, KNode]:
    if idx is None:
        idx = {}
    if node.symbol():
        idx[node.symbol()] = node
    for c in node.children:
        build_knode_index(c, idx)
    return idx


# ── Doc-file parser ────────────────────────────────────────────────────────────
#
# The doc file is the plain-text (no ANSI) tree.  Each line is one of:
#   • A Kconfig node line  — ends with  (SYMBOL_NAME)  optionally followed by
#                            a trailing comment  # …
#   • A structural header  — menu / choice / if / Kconfig comment marker
#   • A pure comment line  — content (after stripping tree chars) starts with #
#
# Pure comment lines are attached to the entry immediately above them.

_DOC_SYMBOL_RE  = re.compile(r"\((\w+)\)\s*(?:#.*)?$")
_TRAILING_CMT   = re.compile(r"\s+(#.*)$")
_PURE_CMT_CHAR  = "#"


def _strip_tree_prefix(line: str) -> tuple[str, str]:
    """Split off leading tree-drawing characters. Returns (prefix, content)."""
    i = 0
    while i < len(line) and line[i] in _TREE_CHARS:
        i += 1
    return line[:i], line[i:]


@dataclass
class DocEntry:
    """One logical entry parsed from the existing doc file."""
    raw_line: str            # stripped of ANSI, kept as-is
    symbol: str              # CONFIG_FOO or "" for structural/comment lines
    trailing_comment: str    # "# …" text trailing on this line, or ""
    # Pure comment lines anchored to this entry (the lines below it)
    attached_comments: list[str] = field(default_factory=list)
    is_pure_comment: bool = False


def parse_doc(path: Path) -> tuple[list[DocEntry], dict[str, int]]:
    """
    Parse doc file → (entries, symbol_index).
    symbol_index: CONFIG_FOO → index in entries.
    Pure comment lines are attached to the previous non-comment entry.
    """
    entries: list[DocEntry] = []
    symbol_index: dict[str, int] = {}

    if not path.exists():
        return entries, symbol_index

    for raw in path.read_text(errors="replace").splitlines():
        line = strip_ansi(raw).rstrip()
        if not line:
            continue

        _prefix, content = _strip_tree_prefix(line)

        # Pure comment line?
        if content.startswith(_PURE_CMT_CHAR):
            if entries:
                # attach to the last non-comment entry (skip over comments)
                for e in reversed(entries):
                    if not e.is_pure_comment:
                        e.attached_comments.append(line)
                        break
                else:
                    # No non-comment entry yet — store as standalone entry
                    entries.append(DocEntry(raw_line=line, symbol="",
                                            trailing_comment="",
                                            is_pure_comment=True))
            else:
                entries.append(DocEntry(raw_line=line, symbol="",
                                        trailing_comment="",
                                        is_pure_comment=True))
            continue

        # Extract trailing comment (must come before symbol extraction)
        trailing = ""
        m = _TRAILING_CMT.search(content)
        if m:
            trailing = m.group(1)
            content_clean = content[: m.start()]
        else:
            content_clean = content

        # Extract CONFIG symbol from (NAME) tag
        symbol = ""
        ms = _DOC_SYMBOL_RE.search(content_clean)
        if ms:
            symbol = f"CONFIG_{ms.group(1)}"

        entry = DocEntry(raw_line=line, symbol=symbol,
                         trailing_comment=trailing)
        entries.append(entry)
        if symbol and symbol not in symbol_index:
            symbol_index[symbol] = len(entries) - 1

    return entries, symbol_index


# ── Line renderers ─────────────────────────────────────────────────────────────

def _plain_line(node: KNode, prefix: str, connector: str,
                trailing: str = "") -> str:
    glyph = node.raw_glyph()
    if node.kind == "menu":
        body = f"▶ {node.prompt}" if node.prompt else "▶ (menu)"
    elif node.kind == "choice":
        body = f"◆ {node.prompt or '(choice)'}"
    elif node.kind == "comment":
        body = f"--- {node.prompt} ---"
    elif node.kind == "if":
        body = f"[{node.prompt}]"
    else:
        prompt  = node.prompt or node.name
        sym_tag = f"  ({node.name})" if node.name else ""
        body    = f"{glyph} {prompt}{sym_tag}" if glyph else f"{prompt}{sym_tag}"

    tc = f"  {trailing}" if trailing else ""
    return f"{prefix}{connector}{body}{tc}"


def _colour_line(node: KNode, prefix: str, connector: str,
                 trailing: str = "") -> str:
    glyph = node.coloured_glyph()
    if node.kind == "menu":
        body = bold(cyan(f"▶ {node.prompt}")) if node.prompt else bold(cyan("▶ (menu)"))
    elif node.kind == "choice":
        body = bold(f"◆ {node.prompt or '(choice)'}")
    elif node.kind == "comment":
        body = gray(f"--- {node.prompt} ---")
    elif node.kind == "if":
        body = gray(f"[{node.prompt}]")
    else:
        prompt   = node.prompt or node.name
        sym_tag  = gray(f"  ({node.name})") if node.name else ""
        pstr     = bold(prompt) if node.is_active() else gray(prompt)
        body     = f"{glyph} {pstr}{sym_tag}" if glyph else f"{pstr}{sym_tag}"

    tc = f"  {gray(trailing)}" if trailing else ""
    return f"{prefix}{connector}{body}{tc}"


# ── OutputLine ─────────────────────────────────────────────────────────────────

@dataclass
class OutputLine:
    plain:      str
    coloured:   str
    is_warning: bool = False


# ── Merger ─────────────────────────────────────────────────────────────────────

class Merger:
    """
    Walks the Kconfig tree depth-first and produces OutputLines by merging
    with the existing doc file.

    Suppression contract
    --------------------
    - Symbol absent from doc AND no active descendant AND not --add-new/--full
      → suppressed (not emitted).
    - Symbol present in doc → always emitted with updated glyph/value.
    - Symbol absent from doc BUT has an active descendant → restored;
      all structural ancestors are also restored; a WARNING is issued for any
      CONFIG_ ancestor that was absent from the doc.
    - Pure comment lines are re-emitted anchored to their parent symbol.
    - Symbols that have vanished from Kconfig are silently dropped (they
      simply won't appear in the new tree walk).
    """

    def __init__(
        self,
        root:        KNode,
        doc_entries: list[DocEntry],
        doc_index:   dict[str, int],
        knode_index: dict[str, KNode],
        add_new:     bool = False,
        full:        bool = False,
    ):
        self.root        = root
        self.doc_entries = doc_entries
        self.doc_index   = doc_index
        self.knode_index = knode_index
        self.add_new     = add_new
        self.full        = full
        self.output:   list[OutputLine] = []
        self.warnings: list[str]        = []
        # Keys: CONFIG_FOO for symbol nodes, or "__struct_<id>" for structural
        self._emitted: set[str] = set()

    # ── public entry point ─────────────────────────────────────────────────────

    def run(self) -> list[OutputLine]:
        hdr_plain    = f"⚙  {self.root.prompt or 'Kernel Configuration'}"
        hdr_coloured = bold(cyan(hdr_plain))
        self._push(hdr_plain, hdr_coloured)
        self._recurse(self.root, prefix="", depth=0)
        return self.output

    # ── helpers ────────────────────────────────────────────────────────────────

    def _push(self, plain: str, coloured: str, is_warning: bool = False):
        self.output.append(OutputLine(plain=plain, coloured=coloured,
                                      is_warning=is_warning))

    def _in_doc(self, sym: str) -> bool:
        return bool(sym) and sym in self.doc_index

    def _trailing(self, sym: str) -> str:
        if not self._in_doc(sym):
            return ""
        return self.doc_entries[self.doc_index[sym]].trailing_comment

    def _comments(self, sym: str) -> list[str]:
        if not self._in_doc(sym):
            return []
        return self.doc_entries[self.doc_index[sym]].attached_comments

    def _struct_key(self, node: KNode) -> str:
        return f"__struct_{id(node)}"

    # ── decision: should a config/menuconfig node be emitted? ─────────────────

    def _should_emit(self, node: KNode) -> bool:
        sym = node.symbol()
        if self.full:
            return True
        if self._in_doc(sym):
            return True
        if self.add_new and sym:
            return True
        # Restoration: any active descendant brings the node back
        if node.any_descendant_active():
            return True
        return False

    # ── structural node: emit if it would have visible children ───────────────

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
        result = []
        for child in node.children:
            if child.kind in ("config", "menuconfig"):
                if self._should_emit(child):
                    result.append(child)
            elif child.kind in ("menu", "choice", "if"):
                if self._has_visible_children(child):
                    result.append(child)
            elif child.kind == "comment":
                result.append(child)
        return result

    # ── recursive walk ─────────────────────────────────────────────────────────

    def _recurse(self, parent: KNode, prefix: str, depth: int):
        children = self._visible_children(parent)
        for idx, child in enumerate(children):
            is_last   = (idx == len(children) - 1)
            connector = LAST if is_last else TEE
            child_pfx = prefix + (BLANK if is_last else PIPE)
            self._emit_node(child, prefix, connector, child_pfx, depth)

    def _emit_node(self, node: KNode, prefix: str, connector: str,
                   child_pfx: str, depth: int):
        sym = node.symbol()

        # ── structural nodes (menu / choice / if / comment) ────────────────────
        if node.kind in ("menu", "choice", "if", "comment"):
            key = self._struct_key(node)
            if key not in self._emitted:
                self._emitted.add(key)
                self._push(_plain_line(node, prefix, connector),
                           _colour_line(node, prefix, connector))
            self._recurse(node, child_pfx, depth + 1)
            return

        # ── config / menuconfig ────────────────────────────────────────────────
        if sym in self._emitted:
            return  # duplicate (can happen with menuconfig re-entry)
        self._emitted.add(sym)

        # Warn about restoration (node not in doc but forced in by active child)
        if not self._in_doc(sym) and not self.full and not self.add_new:
            if node.any_descendant_active():
                self._warn_restored(node)
            # Also check if any CONFIG_ ancestors are absent from doc
            self._check_config_ancestors(node)

        trailing = self._trailing(sym)
        self._push(_plain_line(node, prefix, connector, trailing),
                   _colour_line(node, prefix, connector, trailing))

        # Re-emit attached comment lines (anchored to this symbol)
        for cmt in self._comments(sym):
            _, content = _strip_tree_prefix(cmt)
            self._push(f"{child_pfx}{content}",
                       f"{child_pfx}{gray(content)}")

        # menuconfig nodes can have children
        if node.kind == "menuconfig" and node.children:
            self._recurse(node, child_pfx, depth + 1)

    def _warn_restored(self, node: KNode):
        sym = node.symbol()
        msg = f"# WARNING: {sym} restored (has active descendant)"
        self._push(msg, magenta(msg), is_warning=True)
        self.warnings.append(
            f"Restored suppressed symbol {sym} (active descendant found)")

    def _check_config_ancestors(self, node: KNode):
        """
        Walk up the parent chain.  Any CONFIG_ ancestor that is absent from
        the doc but has a child present is worth warning about, so the user
        can decide whether to add it back.
        """
        p = node.parent
        while p:
            if p.kind in ("config", "menuconfig") and p.symbol():
                psym = p.symbol()
                if not self._in_doc(psym) and psym not in self._emitted:
                    msg = (f"# WARNING: ancestor {psym} absent from doc "
                           f"but descendant {node.symbol()} is present")
                    self._push(msg, magenta(msg), is_warning=True)
                    self.warnings.append(
                        f"Ancestor {psym} missing from doc "
                        f"(descendant {node.symbol()} is present)")
            p = p.parent


# ── Emit Linux .config format ─────────────────────────────────────────────────

def emit_kconfig_format(output_lines: list[OutputLine],
                        knode_index: dict[str, KNode]):
    """Print all symbols from the merged output in Linux .config format."""
    print("# Generated by kconfig_tree.py")
    print("#")
    seen: set[str] = set()
    for ol in output_lines:
        if ol.is_warning:
            continue
        m = _DOC_SYMBOL_RE.search(ol.plain)
        if not m:
            continue
        sym = f"CONFIG_{m.group(1)}"
        if sym in seen:
            continue
        seen.add(sym)
        node = knode_index.get(sym)
        if node is None:
            continue
        if node.value is None or node.value.strip() in ("n", ""):
            print(f"# {sym} is not set")
        else:
            print(f"{sym}={node.value}")


# ── Post-render depth / filter ────────────────────────────────────────────────

def _filter_output(lines: list[OutputLine],
                   max_depth: Optional[int],
                   filter_word: Optional[str]) -> list[OutputLine]:
    fw = filter_word.lower() if filter_word else None
    result = []
    for ol in lines:
        if ol.is_warning:
            result.append(ol)
            continue
        prefix, _ = _strip_tree_prefix(ol.plain)
        depth = len(prefix) // 4
        if max_depth is not None and depth > max_depth:
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
                    help=f"Doc file to read/write (default: {DEFAULT_DOC})")
    ap.add_argument("--arch",         default="arm64",
                    help="Architecture (default: arm64)")
    ap.add_argument("--add-new",      action="store_true",
                    help="Add symbols missing from doc as inactive entries")
    ap.add_argument("--full",         action="store_true",
                    help="Re-add ALL symbols; preserve existing comments")
    ap.add_argument("--emit-kconfig", action="store_true",
                    help="Print Linux .config format to stdout, then exit")
    ap.add_argument("--depth",        type=int, default=None,
                    help="Max tree depth to display")
    ap.add_argument("--filter",       default=None,
                    help="Show only subtrees containing WORD")
    ap.add_argument("--no-color",     action="store_true",
                    help="Disable ANSI colours")
    ap.add_argument("--no-doc",       action="store_true",
                    help="Do not write the doc file this run")
    args = ap.parse_args()

    if args.no_color or args.emit_kconfig:
        USE_COLOR = False

    kernel_root    = Path(".").resolve()
    kconfig_path   = Path(args.kconfig)
    dotconfig_path = Path(args.dotconfig)
    doc_path       = Path(args.doc)

    if not kconfig_path.exists():
        sys.exit(
            f"ERROR: Kconfig file not found: {kconfig_path}\n"
            "Run this script from your kernel source root directory."
        )

    # 1. Parse Kconfig tree
    print("Parsing Kconfig hierarchy …", file=sys.stderr)
    parser = KconfigParser(arch=args.arch, kernel_root=kernel_root)
    root   = parser.parse(kconfig_path)

    # 2. Load .config values and annotate tree
    print(f"Loading {dotconfig_path} …", file=sys.stderr)
    cfg = load_dotconfig(dotconfig_path)
    annotate_tree(root, cfg)

    # 3. Build symbol → KNode index
    knode_index = build_knode_index(root)

    # 4. Parse existing doc file
    if doc_path.exists():
        print(f"Reading doc file {doc_path} …", file=sys.stderr)
    doc_entries, doc_index = parse_doc(doc_path)

    # 5. Merge
    merger = Merger(
        root        = root,
        doc_entries = doc_entries,
        doc_index   = doc_index,
        knode_index = knode_index,
        add_new     = args.add_new,
        full        = args.full,
    )
    output_lines = merger.run()

    # 6. Apply depth / filter
    if args.depth is not None or args.filter:
        output_lines = _filter_output(output_lines, args.depth, args.filter)

    # 7. --emit-kconfig mode
    if args.emit_kconfig:
        emit_kconfig_format(output_lines, knode_index)
        return

    # 8. Write plain doc file
    if not args.no_doc:
        with doc_path.open("w") as f:
            for ol in output_lines:
                if not ol.is_warning:   # don't persist warning lines
                    f.write(ol.plain + "\n")
        print(f"Doc written to {doc_path}", file=sys.stderr)

    # 9. Colourised stdout
    for ol in output_lines:
        print(ol.coloured)

    # 10. Warnings to stderr
    if merger.warnings:
        print(file=sys.stderr)
        print(f"{magenta('WARNINGS')} ({len(merger.warnings)}):", file=sys.stderr)
        for w in merger.warnings:
            print(f"  {magenta('!')} {w}", file=sys.stderr)

    # 11. Stats footer to stderr
    stats: dict = {"total": 0, "yes": 0, "module": 0, "no": 0,
                   "unset": 0, "other": 0}
    collect_stats(root, stats)
    print(
        f"\n{bold('Summary')}:  "
        f"{green(str(stats['yes']))} built-in  "
        f"{yellow(str(stats['module']))} module  "
        f"{gray(str(stats['no']))} disabled  "
        f"{gray(str(stats['unset']))} unset  "
        f"/ {stats['total']} total Kconfig symbols",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
