<!-- Copied from ~/.claude/guidelines/code-style.md -- the source. Keep in
     sync; fix drift the moment you notice it. -->

# code-style.md

Code style for private projects. This file is the **source**. Every private
project except the one holding this file carries a copy at its repo root,
and the copies must not diverge -- see *Keeping the copies in sync* at the
end, which says why the exception exists.

Vendored submodules, generated sources and attic/historical material are
exempt: they keep whatever their upstream or generator produces. Each
project's copy names its own exempt paths.

## The three rules

1. **`snake_case`, not `camelCase`,** for identifiers this project defines.
2. **Tabs for indentation, spaces for alignment.**
3. **Lowercase filenames,** unless a tool demands otherwise.

Everything below is these three rules in detail, plus the exceptions that
are already settled. An exception not listed here is not yet settled: raise
it rather than deciding in passing.

## 1. Naming

`snake_case` for functions, variables, type names and fields.

This holds **even inside a toolkit whose own API is `camelCase`**. Call the
foreign API exactly as it is spelled (`setParent`, `addWidget`) -- that is
not a violation, it is the API's name. But names *you* introduce stay
`snake_case`. Do not let the surrounding convention pull your own names
across.

- Prefer the plain descriptive name over the redundant one. Name the thing,
  not its category: `plan`, not `plan_struct` or `plan_result`.
- **No abbreviations that are not already vocabulary.** `observed`, not
  `obs`; `interface`, not `iff`. This matters most wherever an internal
  name escapes into something you cannot rename later -- a wire format, a
  config key, a CLI output, an on-disk path.
- **One word per concept, everywhere.** The same word in the type name, the
  file path, the subcommand and the documentation. A synonym introduced for
  variety reads as a second concept.

### Prefixes, and visibility

Prefixes exist to keep this project's symbols from colliding with a
library's. So they follow **visibility**, and the choice is a matter of
judgement rather than a mechanical rule:

- **Anything with more than small visibility carries the project prefix** --
  the public API, and anything a linker or importer outside its own module
  can reach.
- **Module-private symbols are left unprefixed**, precisely so that the
  absence of a prefix reads as "this does not leave the module."

The middle case decides itself on link safety, not on taste. A symbol that
is internal by intent but still reaches the linker -- cross-file within a
library, not `static`, not part of the API -- is *not* private for this
purpose. Prefix it. A deliberate parallel copy of a function in two
libraries needs a **distinct** name, not the same name in both on the
assumption that nothing will ever link both sides; that assumption fails
later, at a call site that changed nothing, and names files you did not
touch.

Where a language enforces its own scheme, accept it rather than fight it,
and say in the project's copy that the toolchain is doing it:

- **Rust** -- `non_snake_case` and `non_camel_case_types` are on by default,
  so types are `PascalCase` and constants `SCREAMING_SNAKE_CASE`. That is
  the toolchain's, not a choice. Package systems that demand kebab-case
  (Cargo crate names, Debian package names) likewise read back with their
  own spelling; do not invent a third by naming the directory differently
  from the package.
- **Python** -- a leading underscore (`_name`) is the language's private
  marker and stands in for "unprefixed" above.

## 2. Indentation and alignment

Indent structural nesting with **tab** characters, one tab per level. When
lining up tokens *within* a line -- continuation parameters under an open
paren, a block comment's `*` column, an aligned trailing comment -- use
**spaces**, after the indent tabs.

The point of the split: alignment is expressed relative to the shared
leading tabs, so it survives at any tab width. No tab width is prescribed
anywhere; the viewer decides.

```c
int thing_do(thing_t *thing, const char *name, size_t name_len,
              uint8_t *out, size_t out_cap) {
>---if (!thing) return ERR_MALFORMED;
>---return thing_write(thing, name, name_len, out, out_cap,
>---                    THING_DEFAULT_FLAGS);
}
```

(`>---` marks a tab; everything lining up under `(` is spaces.)

Never mix tabs and spaces *within* the indent itself. Tabs come first and
spaces come after; the reverse, or an alternation, is what breaks at a
different tab width -- and in Python it is a syntax error.

### Settled exceptions

Divergence needs a technical reason. These reasons are already accepted and
need no discussion:

- **Makefile recipe lines** -- `make` requires a literal tab. Compliant by
  construction.
- **YAML** -- the spec forbids tabs for indentation outright. Use spaces.
- **Markdown** -- list continuation and code fences are space-indented by
  specification. Exempt.
- **Debian packaging files** -- exempt, and the two halves are exempt for
  different reasons. `debian/changelog` has a fixed layout that a tab is
  not part of: `dpkg-parsechangelog` calls a tab-indented change line
  "unrecognized" and loses the trailer outright if a tab precedes `--`. A
  deb822 continuation in `control` or `copyright` is the opposite case --
  `deb822(5)` allows a leading SPACE *or* TAB and dpkg round-trips either,
  but that leading whitespace is field syntax rather than indentation, so
  the rule has nothing to say about it and everything past it is
  alignment. Both measured against dpkg rather than read off the manual.
- **Go** -- `gofmt` emits tabs natively. Compliant already.
- **Vendored, generated and attic sources** -- exempt, per the header.

Python deserves a note, because PEP 8 prefers spaces and the tension looks
worse than it is: the language's only hard rule is that indentation must not
be *ambiguous*, and tabs-then-spaces is unambiguous at every tab width.
Continuation lines inside brackets are not indentation-significant at all.
Never a space *before* a tab in leading whitespace -- that is the case that
raises `TabError`.

Anything else that seems to need spaces: signal it to the list in
`claude-guidelines`' `project.md`, follow the rule meanwhile, and it gets
settled and added here in a pass rather than in whichever project met it
first.

## 3. Filenames

**Lowercase, always**, for everything the project names itself. So
`main_window.cpp`, not `MainWindow.cpp`.

**The separator follows what the name binds to**, and the two cases are a
technical difference rather than a matter of taste:

- **`snake_case` where the filename becomes an identifier** -- a source
  file, a header, a module. `desired_state.rs` *is* the module
  `desired_state`, and `desired-state.rs` cannot be a module at all,
  because a hyphen is not legal in a Rust path; Python imports are the
  same. That is the language's requirement wearing a convention's
  clothes, and it is not negotiable where it applies.
- **`kebab-case` for prose** -- documentation, design notes, decision
  records. Nothing imports `code-style.md`, so no identifier is at stake,
  and kebab-case is what markdown and URLs settled on long ago.

This rule used to say `snake_case` for documentation too, and every
private project was quietly ignoring it -- including this one. Measured
across all fourteen trees before it was rewritten: of 197 tracked markdown
basenames, 174 are kebab-case, 19 are a single word with no separator to
argue about, and four carry an underscore. Three of those four are SHOUTY
and break the lowercase half regardless of separator, which leaves exactly
one genuine counter-example in the workspace. Every file in this
guidelines directory was already kebab-case, so the rule as written was
one its own document broke.

Settled exceptions:

- **Names a tool will not accept lowercased** -- `Makefile`,
  `CMakeLists.txt`, `AndroidManifest.xml`, `Dockerfile`, `Cargo.toml`.
- **Root files with an established convention** -- `README.md`, `LICENSE`,
  `CHANGELOG.md`, `AUTHORS`, `VERSION`. The last is this workspace's own
  rather than the wider world's, and is settled by use: a project that
  packages or ships a version tracks one, and a build reads it for the
  package version and for whatever the program prints, so the number
  lives in exactly one place. `claude-guidelines` is the exception,
  because it packages nothing.
- **Package-system spellings** -- kebab-case where Cargo or Debian require
  it. That is now the same spelling prose uses, so a crate directory and
  the design note beside it agree by construction rather than by
  coincidence.

### Singular, unless somebody else standardised the plural

**Prefer the singular for a directory this project names itself.** `helper/`
rather than `helpers/`, `doc/` rather than `docs/`, `fixture/` rather than
`fixtures/`. The name says what kind of thing lives there, not how many;
one of them and forty of them go in the same place, and the directory
should not have to be renamed when the count changes.

There are two exceptions, and they are not equal. This is the same shape
as the lowercase rule above, which yields first to `Makefile` because make
will not read anything else, and only then to `README.md` because the world
settled it.

**First: a name a tool requires is not a name we choose.** It outranks the
singular exactly as it outranks lowercase, it needs no measurement and no
argument, and the test is whether something breaks when the name changes.
This is a *technical* fact, so it is open-ended rather than a list -- a
tool met tomorrow that demands a name gets the same answer, whether the
name it demands is plural, singular, capitalised or none of those.

Present here: **Cargo** looks for `tests/`, `examples/` and `benches/` by
those exact names, and `cargo-fuzz` for `fuzz_targets/`. **GitHub**
requires `.github/workflows/`. **git** keeps `hooks/`, which is why
`tool/hooks/` is spelled that way. And a **foreign package's directory
names are the same exception one layer out**: the Android SDK ships
`platform-tools/` and `build-tools/`, and those spellings are the SDK's
however this tree spells its own. The rename pass learned that by
breaking them -- a word-boundary rewrite of `tools/` reaches into
`platform-tools/` because the hyphen is a boundary, so every adb target
in four projects pointed at a binary that was not there and the
signature check went quiet by a new route. A sweep must skip compound
names it does not own, and package-system spellings (Cargo crates,
Debian packages) under *Filenames* above are this same carve-out.

**Second: a plural an ecosystem has settled**, which is a convention rather
than a requirement -- nothing breaks, but a reader would be surprised by
the singular. Cargo workspaces conventionally keep members in `crates/`,
and that is this kind rather than the first. **These need measuring**, and
the project's copy names what it was measured against, so the next reader
does not reopen it.

Where the two are confused, the cost lands on whoever renames a directory
because it looked like a convention and finds the build no longer works.
So say which kind is being claimed.

**The settled inventory took this rule by the holder's instruction, not by
sweep.** When this section was first written, three canonical names in
`harmonization.md` were plural -- `tools/`, `docs/` and `docs/decisions/` --
and the paragraph here held them out of reach, because renaming an inventory
entry is a cross-project rewrite rather than a spelling change: the decision
records were cited by path hundreds of times, and `tools/` was named from
`sync.py`, every Makefile's hook target and the `~/.claude` symlink the
copies are spread from. The holder then said to rename all three, and they
are `tool/`, `doc/` and `doc/decision/` now, moved in a deliberate pass with
each project's gates run against the result. The history is kept because the
next inventory entry will raise the same question, and the answer stays the
same: an inventory name moves when its owner says so, at whatever cost was
measured, and not as a side effect of a style rule.

## ASCII in source

Source and comments are ASCII. Write `--` where prose would use an em dash,
and "section" for a section sign.

This governs **the text the repository writes about itself**, not the data
the software handles. Three exceptions, and they are the rule's shape
rather than holes in it:

- **Documentation.** Markdown may use typographic punctuation.
- **User-facing text in UI software.** A tick a program prints is output,
  not prose -- `GREEN('gpg ')` is correct as it stands.
- **Anything that genuinely requires Unicode**: a fixture for a UTF-8
  parser, a terminal emulator's character tables, a font tool.

Where a project needs the rule enforced, `ascii_only` in `.style-gate.toml`
turns it on. In Python and in C/C++ it enforces exactly the shape above --
ASCII outside string literals, Unicode allowed inside them. Python is read
with `tokenize`; C and C++ get a scanner written for the purpose, nothing in
the standard library lexing them. Every other language still gets a
whole-file byte check, having no lexer here, and so does a file in either of
those two that will not lex: a file nobody can parse is not a file that has
been cleared.

It was the whole file for everyone until a project that prints two status
ticks had to switch the check off to keep them, which switched it off for
its comments as well, and an em dash arrived in one. **An exception wider
than its reason is how a rule stops being enforced.**

The C/C++ half followed from the same shape, measured. A Qt tree kept the
check off for the glyphs on its toolbar and in its media dialog, which are
genuinely output; of the 1114 non-ASCII characters in the 233 C and C++
files its gate reads, 260 were those, and the other 854 were prose that had
collected in comments across 163 of the files -- 437 em dashes and 369
section signs, the two characters this rule names by example. One em dash is
what the first incident cost. The difference is only how long nobody looked.

## Formatters

A formatter is allowed **only if it can be configured to honour the three
rules completely**. Configuration gaps are disqualifying, not something to
work around: a formatter that gets indentation right and alignment wrong
will rewrite the tree on somebody's next save.

So the decision is per tool, per project, and it is a real evaluation:

- If it can be made to comply, use it, and commit the config with a comment
  saying which setting is load-bearing and what happens without it.
- If it cannot, do not run it -- **not even ad hoc on a single file**. The
  failure mode is a silent conversion of files that were already correct,
  discovered later as a reverted commit rather than an error.
- If no existing tool fits and the rule is worth mechanising, write our
  own. A checker that only gates indentation is worth more than a formatter
  that reflows everything.

**Record the decision and the finding that produced it** in the project's
copy of this file -- which tool, what specifically failed, what would change
the answer. A verdict without its evidence gets re-litigated, and a tool
that improves later never gets reconsidered because nobody remembers what
was actually wrong with it.

Naming and filename rules are review items, not automated ones.

## Precedence

Three layers, and they are not equals:

1. **The global guidelines** (`~/.claude/CLAUDE.md` and the files it
   imports) -- the source, and they win.
2. **The project's `project.md`** -- project-specific design and conventions.
3. **The project's `code-style.md`** -- this file, copied.

A project copy that disagrees with the source is **drift, not an
override**: fix it. A project that genuinely needs to diverge needs a
technical reason, and that is not a decision to make while working on
something else -- signal it to the list in `claude-guidelines`'
`project.md` and keep following the source meanwhile.

**When a conflict between layers actually comes up, stop and ask.** Do not
silently pick a winner, even the global one.

This precedence rule lives here and in the global guidelines only. It does
not belong in a `project.md`.

## Keeping the copies in sync

Each private project keeps a copy of this file at its repo root -- except
the one this file lives in. `claude-guidelines` holds the source at
`guidelines/code-style.md`, and a copy beside it would be the same document
twice in one repository with nothing to keep the two honest; its root
`code-style.md` says so and points here. Every other private project carries
a copy, opening with a header that names the source:

```markdown
<!-- Copied from ~/.claude/guidelines/code-style.md -- the source. Keep in
     sync; fix drift the moment you notice it. -->
```

Below the copied rules, a project adds only what is genuinely its own: its
exempt paths, its formatter verdicts, its language-specific notes, its
tooling commands.

**This source is deliberately plain ASCII** -- no em dashes, no section
signs, no arrows -- so that a copy can be byte-verbatim in every project,
including one whose own rules restrict the characters its files may
contain. Keep it that way when editing: a typographic character introduced
here becomes a transliteration problem in every repository that carries a
copy.

Where a copy must still be adapted, **"do not diverge" means semantically
identical, not byte-identical**: a project transliterating to satisfy its
own character-set rule, or renumbering a heading to fit its own structure,
is that project's rule working correctly, **not drift, and not something to
reconcile back**. What must match is every rule and every exception, in
substance.

**`sync.py --check` now reports the copies that have fallen behind**, which
until 2026-08-24 nothing did. The three files spread verbatim were checked on
every run and this one -- the document that says what the rules are -- was
checked by nobody, so drift was indistinguishable from the adaptation the
section above asks for. It found real losses: four copies had dropped
*Precedence*, the section saying this source outranks them; three had dropped
*Formatters*, whose rule was paid for by a formatter rewriting committed
files; one had dropped *ASCII in source*; and seven had dropped this very
section, which is the one that would have told a reader to look.

It asks the weaker question that can actually be answered -- does the copy
still carry a section for every section here -- and it never writes this
file, because overwriting a copy would delete the part the project owns. A
heading that *extends* one of these satisfies it, since that is what
recording a project's own formatter verdict looks like.

**If you notice a copy diverging from the source, reconcile it as soon as
you notice** -- do not leave it for later and do not work around it. If the
divergence looks deliberate rather than stale, that is the conflict case
above: ask.

**And keep a census out of this document, because a copied number goes
stale sixteen times at once.** The `VERSION` exception above used to
carry one -- "sixteen of seventeen, re-counted 2026-08-27" -- and on
2026-09-01 fifteen of the sixteen copies still held an older generation
of it or had rewritten the sentence locally to say something true about
their own tree instead. Not one of them was wrong about the RULE.

That drift was undetectable by construction. The audit compares
headings, and every heading was intact; a prose diff cannot help either,
because this section says in as many words that a copy may differ
textually and be correct. So a fact that changes, written into a
document that is copied and permitted to vary, cannot be kept true and
cannot be checked.

The fix is not a better comparison. **State the rule and the criterion
that decides it, and keep the count where it can be recounted once.**
The exception above now says which projects track a `VERSION` and why,
without saying how many.

Noticing requires looking. **Re-read this source before writing or
reconciling any project's copy**, rather than working from what was loaded
at the start of the session -- it may have changed since, and a copy
reconciled against a stale source is drift being written rather than
fixed.

The project's `project.md` may state the three rules in brief and point
here for the detail. It does not restate the precedence rule.

## This project

**Exempt paths:** none. The tree is one Python program and its documents.

**kconfig_tree.py was retabbed on 2026-09-25**, from four spaces to tabs,
by `style_gate.py fix`. Proved outside the fixer as well: `ast.dump()` is
identical before and after, and expanding each leading tab back to four
spaces reproduces the original file byte for byte. The lines still led by
spaces are continuation alignment and docstring text, which is the rule
working rather than an exception to it.

**ASCII in source holds already.** Measured with `tokenize` on 2026-09-25:
no non-ASCII character outside a string literal. The box-drawing and glyph
characters in string literals are program output, which the rule allows.

**Formatters:** none evaluated, none run.
