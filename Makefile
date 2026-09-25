# kconfig_tree -- the program is one Python file and needs no build. This
# Makefile runs the checks and installs the git hook.

PYTHON ?= python3

.PHONY: all check test style style-source style-docs hooks help

all:
	@:

help:
	@echo 'Targets:'
	@echo '  check  style, then the gate'"'"'s own suite'
	@echo '  style  the indentation and whitespace gate'
	@echo '  hooks  install the commit-msg hook from tool/hooks/'

check: style test

# The gate's suite travels with the gate. The tool itself has no tests yet;
# see project.md.
test:
	$(PYTHON) tool/test_style_gate.py

# The indentation and whitespace gate, shared verbatim with the sibling
# projects; `docs` additionally holds project.md to the tree it describes.
style: style-source style-docs

style-source:
	$(PYTHON) tool/style_gate.py check

style-docs:
	$(PYTHON) tool/style_gate.py docs

hooks:
	@if ! command -v git >/dev/null 2>&1; then \
		echo "hooks: git is not installed, so there is nowhere to install to." >&2; \
		exit 1; \
	fi; \
	dir=$$(git rev-parse --git-common-dir 2>/dev/null); \
	if [ -z "$$dir" ]; then \
		echo "hooks: not a git repository, so there is nowhere to install to." >&2; \
		exit 1; \
	fi; \
	mkdir -p "$$dir/hooks"; \
	install -m 0755 tool/hooks/commit-msg "$$dir/hooks/commit-msg"; \
	echo "hooks: commit-msg installed from tool/hooks/ into $$dir/hooks/"
