# kconfig_tree

`kconfig_tree.py` documents a Linux kernel configuration. Its documentation
files are human-editable, follow the `make menuconfig` hierarchy, and keep
their annotations from one kernel version to the next. It is one
self-contained Python 3 program, standard library only, run from the root
of a kernel source tree.

## Why it exists

A bare `.config` is about 3000-5000 opaque lines that record values and
nothing about why. After a kernel update a diff of two of them shows what
moved and none of the reasoning behind the old values. kconfig_tree keeps
that reasoning as a `# rationale` comment next to each option, in a tree you
can read. On the next update you can see which decisions still hold, which
symbols are new, and which have gone.

**In the setting it was written for, the documentation files are the source
of truth and `.config` is derived from them.** Edit the doc file, then
regenerate. Don't edit `.config` or a defconfig by hand. That setting also
taught why: a hand-maintained defconfig carried alongside went about seven
weeks stale. It differed on `CONFIG_MODULES` and `CC_OPTIMIZE_FOR_SIZE`, and
it built a modular `-O2` kernel where the documented configuration was
monolithic and `-Os`. So if you move a configuration to another repository,
carry `kconfig_doc.txt`, `kconfig_doc_suppressed.txt`, `kconfig_tree.py` and
`.config` together. If you need a defconfig, generate it with
`savedefconfig` from the `.config` the doc files produce.

## The three-file model

| File | Role | Default |
|---|---|---|
| `.config` | Kernel truth: the actual values | `.config` |
| `kconfig_doc.txt` | Allowlist: what to show and annotate | `--doc` |
| `kconfig_doc_suppressed.txt` | Denylist: what to hide permanently | `--suppressed` |

Merge rules at tree-walk time:

| Condition | Action |
|---|---|
| Symbol in doc | Emit with updated glyph; warn if the value changed |
| Symbol in suppressed | Never emit (unless `--full`) |
| In neither, inactive | Silent |
| In neither, active | Notice on stderr, suggesting `--add-new-enabled` |
| In both | Doc wins; warn; removed from suppressed on the next write |
| `--add-new` | Add every untracked symbol to doc |
| `--add-new-enabled [N]` | Add untracked symbols active in config column N (1-3) |
| `--full` | Merge suppressed into doc, then add everything remaining |

A symbol that no longer exists in the Kconfig tree is handled differently
in each file:

- **In doc**, it is kept as a `# CONFIG_SYMBOL` stub at its old position if
  it had a comment attached, so the rationale is not lost.
- **In suppressed**, it is dropped, with a notice.

## Command line

    --kconfig    PATH    top-level Kconfig             (Kconfig)
    --dotconfig  PATH    primary .config               (.config)
    --dotconfig2 PATH    second config for comparison  (.config2)
    --dotconfig3 PATH    third config for comparison   (.config3)
    --doc        PATH    doc file                      (kconfig_doc.txt)
    --suppressed PATH    suppressed file     (kconfig_doc_suppressed.txt)
    --arch       ARCH    $(ARCH) for Kconfig           (arm64)
    --add-new            add all untracked symbols to doc
    --add-new-enabled [N]  add active untracked symbols, column N
    --full               restore suppressed, add all remaining
    --emit-kconfig       print .config format to stdout, then exit
    --show               coloured tree on stdout
    --depth      N       max tree depth (with --show)
    --filter     WORD    subtrees containing WORD (with --show)
    --no-color           no ANSI colours
    --ascii              ASCII-only output, no UTF-8 box or glyph characters
    --no-doc             write no files this run
    --version            print the version and copyright, then exit

The version is in `VERSION` and again in the script as
`KCONFIG_TREE_VERSION`, because the script is vendored into kernel trees on
its own and has no `VERSION` file to read there. `test_kconfig_tree.py`
fails if the two disagree. `--version` prints `kconfig_tree <version>` on
the first line, for scripts to read, and the copyright on the next.

The number of config columns is decided at startup by which of `.config`,
`.config2` and `.config3` exist. The glyphs widen to match: `[*]` for one
config, `[* ]` for two, and `[*m ]` for three (y, m, unset). A value-typed
symbol shows `[=4096]` when every config agrees and `[=4096/8192]` when they
differ. The file format is the same however many columns there are.

## Architecture

- **`KconfigParser`** walks the Kconfig hierarchy depth-first from the
  top-level file. It resolves `source` and `rsource` and substitutes
  `$(ARCH)`. It builds a `KNode` tree, tracking
  `menu`/`if`/`choice` blocks on a stack.
- **`KNode`** is one Kconfig entry: `kind` (menu, config, menuconfig,
  choice, comment, if), `name`, `prompt`, `type_`, `value`, `alt_values`
  for columns 2 and 3, and `occurrence_key` for `if` nodes.
- **`annotate_tree(root, cfg, col)`** runs once per config: column 0 sets
  `value` and later columns append to `alt_values`.
- **`DocFileParser`** reads the doc and suppressed files. It accepts full
  tree lines (`│├[*] Prompt (SYMBOL) # comment`), bare `CONFIG_SYMBOL`
  lines, `.config` set and unset lines, and pure comments. It ignores the
  tree connectors, so only the content matters.
- **`Merger`** walks the Kconfig tree against the effective doc and
  suppressed indexes and produces a list of `OutputLine` objects.
- **`write_doc` / `write_suppressed`** write both files back in Kconfig
  tree order. In the suppressed file each entry sits at its true depth, and
  parents that are not suppressed are left out and not written.

### Keys that survive a kernel update

- **Structural nodes are keyed on two levels**, `"parent::own"`, so two
  identically named choices in different menus stay distinct.
- **`if` nodes get an occurrence key.** The first `if EXPR` in depth-first
  order is `[if EXPR]`, the second `[if EXPR (2)]`, and so on, so every key
  is globally unique. If a file refers to a higher occurrence number than
  the tree now has, that is a **warning**: the kernel update may have
  shifted the order. If the tree has more occurrences than the files
  reference, and numbered ones are already in use, that is a **notice**.
- **An `[if SYM]` block that follows a `menuconfig SYM` is folded** under
  that menuconfig in the doc file. The suppressed file always uses the flat
  Kconfig structure.

### Blank lines

The tree connectors carry the hierarchy, so the tool inserts no blank line
between options, even where consecutive options come from unrelated
subtrees. Blank lines in the output come only from comments of types 3
and 4, which keep the blanks around them. Settled 2026-09-26 by keeping
what the code does. `test_kconfig_tree.py` pins it with two menus whose
options must meet with no blank line between them.

The record had this wrong twice, which is why the measurement is here.
`_needs_blank()` inserted a blank between options that share no non-root
ancestor. It lost its only caller in revision 14 but stayed in the file,
and the module docstring went on describing it as the rule. It has been
deleted. The Merger also has a check that inserts a blank before an option
whose parent header was not printed. Since revision 36, headers cannot be
suppressed, so that check no longer fires: across 1065 options on a real
kernel doc it held for none, while the same probe with its condition
removed fired for all of them. It is kept in case headers become hideable
again.

### Comments

Four kinds, and each one travels with its anchor node wherever the tool
moves it:

    1 trailing        │├[*] Prompt (SYMBOL) # on the same line
    2 anchored-above  directly below a node, no blank between
    3 anchored-below  blank above, node directly below
    4 freestanding    blank above and below, written at column 0

### Invariants

1. Every `if` node's `occurrence_key` is globally unique.
2. Suppression is per node, never hierarchical. Suppressing a menu header
   hides that one line, and its children stay visible and tracked.
3. Comments always travel with their anchor.
4. Multi-config is additive: a single-config run behaves exactly as it did
   before columns existed.
5. The suppressed file is never filled in with structural context. Only
   what the user put there is written. Parent menus added automatically
   would be read back as suppressed.

## Maintaining a configuration with it

This guidance comes from using the tool on one board's kernel.

**Which options go in doc and which in suppressed:**

1. **Doc** holds the options that explain the configuration: active choices
   with their rationale, inactive siblings that show what was decided
   against, and deliberate "no" decisions that matter.
2. **Suppressed** holds auto-selected infrastructure, drivers for other
   vendors or platforms, and inactive options with no rationale worth
   keeping.
3. **Move groups, not single options.** The exception is a sparse group,
   with one or two active among twenty or more inactive. Keep the active
   ones and their nearest siblings in doc, and suppress the rest.
4. Don't leave a menu in doc with no children under it.
5. When in doubt, keep it in doc.

**Write rationale as an inline trailing comment, not as a section header
line.** Decorative standalone lines in doc, such as rules drawn with `──`
or `════`, are orphaned when the tool reorders the file.

**Errata:** keep every `ARM64_ERRATUM_*` active, as Linux does, since each
costs little when the core is unaffected. Record in the comment which ones
apply to the target's core and which are a safe no-op there.

**Make bulk changes in one pass from the original files.** One
reorganisation was attempted as a series of passes, each run on the
previous pass's output, and the state it produced was corrupt. What worked
was a single script. It read the three original files and decided each
symbol's destination exactly once: doc, suppressed, or deactivated, never
two of them. Then it checked the result: no symbol lost, no duplicates, and
every active doc symbol `=y` in the new `.config`. That script's decision
table was specific to one board and is not kept here. The approach is the
part that carries over.

## History

Written between 2026-04-29 and 2026-06-11. The git history was rebuilt
afterwards from the forty versions downloaded along the way: one commit per
version, dated by the download's modification time. Revision 6 was
identical to revision 5 and has no commit. Command-line options arrived as
follows:

- `--add-new`, `--doc`, `--emit-kconfig`, `--full` and `--no-doc` in
  revision 2, replacing `--active-only` and `--out`;
- `--add-new-enabled`, `--show` and `--suppressed` in revision 3;
- `--dotconfig2` and `--dotconfig3` in revision 33;
- `--ascii` in revision 35.

## Checks

`make check` runs the shared style gate over the tree, this tool's tests
and the gate's own suite; `make hooks` installs the commit-msg hook. `tool/` holds verbatim
copies from `claude-guidelines`, kept in step by its `sync.py`: fix them
there, not here.

The tool is vendored by copy into the kernel trees that use it. Those
copies are theirs; this repository is where the tool itself changes.

## Open

- **Few tests.** `test_kconfig_tree.py` checks that `--help` lists every
  option, that `--version` agrees with `VERSION`, and the blank-line rule.
  Every other behaviour above was established by running the tool on a
  real kernel tree, and no fixture checks any of it.
