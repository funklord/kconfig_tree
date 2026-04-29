#!/usr/bin/env python3
"""
kconfig_tree.py — Render a text-tree of the kernel Kconfig hierarchy
showing which options are active, mirroring the menuconfig structure.

Usage
-----
  # From your kernel source root (where .config lives):
  python3 kconfig_tree.py [OPTIONS]

Options
-------
  --kconfig   PATH   Top-level Kconfig file  (default: Kconfig)
  --dotconfig PATH   Compiled config file    (default: .config)
  --arch      ARCH   Architecture            (default: arm64)
  --depth     N      Max menu depth to show  (default: unlimited)
  --filter    WORD   Only show subtrees containing WORD (case-insensitive)
  --active-only      Hide options that are disabled / not set
  --no-color         Disable ANSI colours
  --out       FILE   Write output to FILE instead of stdout

Example
-------
  python3 kconfig_tree.py --arch arm64 --filter USB --depth 5
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── ANSI colour helpers ────────────────────────────────────────────────────────

USE_COLOR = True

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def cyan(t):   return _c("36", t)
def gray(t):   return _c("90", t)
def bold(t):   return _c("1",  t)
def red(t):    return _c("31", t)


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class KNode:
    """One node in the Kconfig tree (menu, config, choice, comment, if)."""
    kind: str            # 'menu' | 'config' | 'menuconfig' | 'choice' | 'comment' | 'if' | 'source'
    name: str            # symbol name or menu title
    prompt: str = ""     # human-readable prompt string
    type_: str = ""      # bool | tristate | int | hex | string
    help_: str = ""
    depends: str = ""
    children: list = field(default_factory=list)
    parent: Optional["KNode"] = field(default=None, repr=False)
    file: str = ""
    lineno: int = 0

    # filled in after .config is loaded
    value: Optional[str] = None   # raw value from .config, or None

    def symbol(self) -> str:
        """CONFIG_ name."""
        return f"CONFIG_{self.name}" if self.name else ""

    def is_active(self) -> bool:
        if self.value is None:
            return False
        v = self.value.strip().strip('"')
        return v not in ("", "n", "0")

    def status_glyph(self) -> str:
        """Return a short status tag."""
        if self.kind in ("menu", "choice", "comment", "if"):
            return ""
        if self.value is None:
            return gray("[ ]")
        v = self.value.strip().strip('"')
        if v == "y":
            return green("[*]")
        if v == "m":
            return yellow("[M]")
        if v in ("n", ""):
            return gray("[ ]")
        # numeric / string
        return cyan(f"[={v}]")


# ── Kconfig parser ─────────────────────────────────────────────────────────────

_COMMENT_RE  = re.compile(r"^\s*#")
_BLANK_RE    = re.compile(r"^\s*$")
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
_DEFAULT_RE  = re.compile(r"^\s+default\s+")
_HELP_RE     = re.compile(r"^\s+help\b|^\s+---help---")
_RANGE_RE    = re.compile(r"^\s+range\b")
_SELECT_RE   = re.compile(r"^\s+select\b")
_IMPLY_RE    = re.compile(r"^\s+imply\b")
_VISIBLE_RE  = re.compile(r"^\s+visible\s+if\b")
_OPTION_RE   = re.compile(r"^\s+option\b")


class KconfigParser:
    def __init__(self, arch: str, kernel_root: Path):
        self.arch = arch
        self.root_path = kernel_root
        self.env = {
            "ARCH":   arch,
            "SRCARCH": arch,
            # common substitutions used in Kconfig source lines
        }
        self._parsed: set[str] = set()

    def _resolve_path(self, path_str: str, current_dir: Path) -> Optional[Path]:
        """Expand $(VAR) in source paths and resolve relative to current_dir or kernel root."""
        def sub(m):
            return self.env.get(m.group(1), m.group(0))
        path_str = re.sub(r"\$\((\w+)\)", sub, path_str)
        p = current_dir / path_str
        if p.exists():
            return p
        p2 = self.root_path / path_str
        if p2.exists():
            return p2
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
        except OSError as e:
            # Non-fatal: just annotate missing sources
            parent.children.append(
                KNode(kind="comment", name="", prompt=f"[missing: {path}]",
                      file=str(path), lineno=0)
            )
            return

        self._parse_lines(lines, path, parent)

    def _parse_lines(self, lines: list[str], path: Path, parent: KNode):
        i = 0
        menu_stack: list[KNode] = [parent]

        def cur() -> KNode:
            return menu_stack[-1]

        current_node: Optional[KNode] = None
        in_help = False
        help_indent = 0

        while i < len(lines):
            raw = lines[i]
            line = raw.rstrip()
            i += 1

            # ── help block ───────────────────────────────────────────────────
            if in_help:
                stripped = line.lstrip()
                indent = len(line) - len(stripped)
                if stripped == "" or indent > help_indent:
                    if current_node:
                        current_node.help_ += line + "\n"
                    continue
                else:
                    in_help = False
                    # fall through to normal parsing

            if _COMMENT_RE.match(line) or _BLANK_RE.match(line):
                current_node = None
                continue

            # ── menu ─────────────────────────────────────────────────────────
            m = _MENU_RE.match(line)
            if m:
                node = KNode(kind="menu", name="", prompt=m.group(1),
                             file=str(path), lineno=i)
                node.parent = cur()
                cur().children.append(node)
                menu_stack.append(node)
                current_node = None
                continue

            if _ENDMENU_RE.match(line):
                if len(menu_stack) > 1:
                    menu_stack.pop()
                current_node = None
                continue

            # ── config / menuconfig ───────────────────────────────────────────
            m = _CONFIG_RE.match(line)
            if m:
                node = KNode(kind=m.group(1), name=m.group(2),
                             file=str(path), lineno=i)
                node.parent = cur()
                cur().children.append(node)
                current_node = node
                continue

            # ── choice / endchoice ────────────────────────────────────────────
            if _CHOICE_RE.match(line):
                node = KNode(kind="choice", name="", prompt="(choice)",
                             file=str(path), lineno=i)
                node.parent = cur()
                cur().children.append(node)
                menu_stack.append(node)
                current_node = node
                continue

            if _ENDCHOICE.match(line):
                if len(menu_stack) > 1:
                    menu_stack.pop()
                current_node = None
                continue

            # ── comment (Kconfig keyword, not #) ─────────────────────────────
            m = _COMMENT2_RE.match(line)
            if m:
                node = KNode(kind="comment", name="", prompt=m.group(1),
                             file=str(path), lineno=i)
                node.parent = cur()
                cur().children.append(node)
                current_node = None
                continue

            # ── if / endif ───────────────────────────────────────────────────
            m = _IF_RE.match(line)
            if m and not line.strip().startswith("default"):
                node = KNode(kind="if", name="", prompt=f"if {m.group(1).strip()}",
                             file=str(path), lineno=i)
                node.parent = cur()
                cur().children.append(node)
                menu_stack.append(node)
                current_node = None
                continue

            if _ENDIF_RE.match(line):
                if len(menu_stack) > 1:
                    menu_stack.pop()
                current_node = None
                continue

            # ── source ───────────────────────────────────────────────────────
            m = _SOURCE_RE.match(line)
            if m:
                src_path = self._resolve_path(m.group(1), path.parent)
                if src_path:
                    self._parse_file(src_path, cur())
                current_node = None
                continue

            # ── attributes on current_node ───────────────────────────────────
            if current_node is None:
                continue

            m = _TYPE_RE.match(line)
            if m:
                current_node.type_ = m.group(1)
                if m.group(2):
                    current_node.prompt = m.group(2)
                continue

            m = _PROMPT_RE.match(line)
            if m:
                current_node.prompt = m.group(1)
                continue

            m = _DEPENDS_RE.match(line)
            if m:
                current_node.depends = m.group(1).strip()
                continue

            if _HELP_RE.match(line):
                in_help = True
                help_indent = len(line) - len(line.lstrip()) + 1
                continue

            # silently consume other known attributes
            for pat in (_DEFAULT_RE, _SELECT_RE, _IMPLY_RE, _RANGE_RE,
                        _VISIBLE_RE, _OPTION_RE):
                if pat.match(line):
                    break


# ── .config loader ─────────────────────────────────────────────────────────────

def load_dotconfig(path: Path) -> dict[str, str]:
    """Return {CONFIG_FOO: value} for all set symbols; 'n' entries too."""
    cfg: dict[str, str] = {}
    if not path.exists():
        print(f"WARNING: {path} not found — no active-status information.", file=sys.stderr)
        return cfg
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#"):
            # detect  # CONFIG_FOO is not set
            m = re.match(r"#\s+(CONFIG_\w+)\s+is not set", line)
            if m:
                cfg[m.group(1)] = "n"
            continue
        m = re.match(r"(CONFIG_\w+)=(.*)", line)
        if m:
            cfg[m.group(1)] = m.group(2)
    return cfg


# ── annotate tree ──────────────────────────────────────────────────────────────

def annotate(node: KNode, cfg: dict[str, str]):
    sym = node.symbol()
    if sym:
        node.value = cfg.get(sym)   # None if not mentioned at all
    for child in node.children:
        annotate(child, cfg)


# ── render ─────────────────────────────────────────────────────────────────────

PIPE      = "│   "
TEE       = "├── "
LAST      = "└── "
BLANK     = "    "

def _node_label(node: KNode) -> str:
    glyph = node.status_glyph()
    if node.kind in ("menu",):
        label = bold(cyan(f"▶ {node.prompt}")) if node.prompt else bold(cyan("▶ (menu)"))
    elif node.kind == "choice":
        label = bold(f"◆ {node.prompt}")
    elif node.kind == "comment":
        label = gray(f"--- {node.prompt} ---")
    elif node.kind == "if":
        label = gray(f"[{node.prompt}]")
    else:
        # config / menuconfig
        name = yellow(f"CONFIG_{node.name}") if node.name else ""
        prompt = node.prompt or node.name
        if node.is_active():
            prompt_str = bold(prompt)
        else:
            prompt_str = gray(prompt)
        parts = [glyph, prompt_str]
        if node.name:
            parts.append(gray(f"  ({node.name})"))
        label = " ".join(p for p in parts if p)
    return label


def _subtree_has_active(node: KNode) -> bool:
    if node.is_active():
        return True
    return any(_subtree_has_active(c) for c in node.children)


def render_tree(
    node: KNode,
    prefix: str = "",
    is_last: bool = True,
    depth: int = 0,
    max_depth: Optional[int] = None,
    filter_word: Optional[str] = None,
    active_only: bool = False,
    out=sys.stdout,
    _root: bool = True,
):
    # apply active_only filter
    if active_only and node.kind not in ("menu", "choice", "if", "comment"):
        if not node.is_active():
            return

    # apply word filter (show node only if subtree matches)
    if filter_word:
        fw = filter_word.lower()
        def matches(n: KNode) -> bool:
            haystack = (n.name + n.prompt + n.depends).lower()
            if fw in haystack:
                return True
            return any(matches(c) for c in n.children)
        if not matches(node):
            return

    connector = LAST if is_last else TEE
    child_prefix = prefix + (BLANK if is_last else PIPE)

    if _root:
        out.write(bold(cyan(f"⚙  {node.prompt or 'Kernel Configuration'}\n")))
        child_prefix = ""
    else:
        label = _node_label(node)
        out.write(f"{prefix}{connector}{label}\n")

    # depth guard
    if max_depth is not None and depth >= max_depth:
        if node.children:
            out.write(f"{child_prefix}{LAST}{gray('...')}\n")
        return

    children = node.children
    if active_only:
        children = [c for c in children
                    if c.kind in ("menu", "choice", "if", "comment")
                    or c.is_active()]
    if filter_word:
        fw = filter_word.lower()
        def matches_any(n: KNode) -> bool:
            haystack = (n.name + n.prompt + n.depends).lower()
            if fw in haystack:
                return True
            return any(matches_any(c) for c in n.children)
        children = [c for c in children if matches_any(c)]

    for idx, child in enumerate(children):
        last = (idx == len(children) - 1)
        render_tree(
            child,
            prefix=child_prefix,
            is_last=last,
            depth=depth + 1,
            max_depth=max_depth,
            filter_word=filter_word,
            active_only=active_only,
            out=out,
            _root=False,
        )


# ── summary stats ──────────────────────────────────────────────────────────────

def collect_stats(node: KNode, stats: dict):
    if node.kind in ("config", "menuconfig"):
        stats["total"] += 1
        v = node.value
        if v is None:
            stats["unset"] += 1
        elif v == "y":
            stats["yes"] += 1
        elif v == "m":
            stats["module"] += 1
        elif v == "n":
            stats["no"] += 1
        else:
            stats["other"] += 1
    for c in node.children:
        collect_stats(c, stats)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    global USE_COLOR

    ap = argparse.ArgumentParser(
        description="Render a menuconfig-style text tree from Kconfig + .config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--kconfig",     default="Kconfig",  help="Top-level Kconfig file")
    ap.add_argument("--dotconfig",   default=".config",  help=".config file")
    ap.add_argument("--arch",        default="arm64",    help="Architecture (ARCH=)")
    ap.add_argument("--depth",       type=int, default=None, help="Max depth to display")
    ap.add_argument("--filter",      default=None,       help="Only show subtrees matching WORD")
    ap.add_argument("--active-only", action="store_true", help="Hide disabled options")
    ap.add_argument("--no-color",    action="store_true", help="Disable ANSI colours")
    ap.add_argument("--out",         default=None,       help="Output file (default: stdout)")
    args = ap.parse_args()

    if args.no_color:
        USE_COLOR = False

    kernel_root = Path(".").resolve()
    kconfig_path = Path(args.kconfig)
    dotconfig_path = Path(args.dotconfig)

    if not kconfig_path.exists():
        sys.exit(f"ERROR: Kconfig file not found: {kconfig_path}\n"
                 "Run this script from your kernel source root directory.")

    print(f"Parsing Kconfig hierarchy from {kconfig_path} …", file=sys.stderr)
    parser = KconfigParser(arch=args.arch, kernel_root=kernel_root)
    root = parser.parse(kconfig_path)

    print(f"Loading {dotconfig_path} …", file=sys.stderr)
    cfg = load_dotconfig(dotconfig_path)
    annotate(root, cfg)

    stats: dict = {"total": 0, "yes": 0, "module": 0, "no": 0, "unset": 0, "other": 0}
    collect_stats(root, stats)

    out = open(args.out, "w") if args.out else sys.stdout

    render_tree(
        root,
        max_depth=args.depth,
        filter_word=args.filter,
        active_only=args.active_only,
        out=out,
    )

    # Summary footer
    summary = (
        f"\n{bold('Summary')}:  "
        f"{green(str(stats['yes']))} built-in  "
        f"{yellow(str(stats['module']))} module  "
        f"{gray(str(stats['no']))} disabled  "
        f"{gray(str(stats['unset']))} unset  "
        f"/ {stats['total']} total symbols\n"
    )
    out.write(summary)

    if args.out:
        out.close()
        print(f"Written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
