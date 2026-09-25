# kconfig_tree

Documents a Linux kernel configuration as an annotated tree that follows
the `make menuconfig` hierarchy, and keeps it current across kernel
updates.

Your notes on each option live in `kconfig_doc.txt`, beside the option and
in the tree, and the tool keeps them attached as the kernel changes. After
an update it tells you which options are new, which have gone, and which
changed value. It can also compare up to three `.config` files side by
side.

```
⚙ Linux Kernel Configuration
├▶ General setup
│├[*] System V IPC (SYSVIPC) # systemd and D-Bus need it
│└[ ] Kernel .config support (IKCONFIG) # config is kept with the image
└[*] Networking support (NET) -> # every service is networked
```

## Requirements

Python 3.9 or later, standard library only.

## Use

Run it from the root of a kernel source tree, where `Kconfig` and `.config`
live.

```sh
python3 kconfig_tree.py --add-new-enabled   # first run: document every active option
python3 kconfig_tree.py                     # after a kernel update: refresh, report changes
python3 kconfig_tree.py --show              # view the coloured tree
python3 kconfig_tree.py --emit-kconfig > fragment.config
```

To hide an option for good, move its line from `kconfig_doc.txt` into
`kconfig_doc_suppressed.txt`. To bring it back, delete it from there. Both
files are rewritten in Kconfig order on every run, and your comments move
with the options they belong to.

If `.config2` or `.config3` exist beside `.config`, each option shows one
column per file.

`python3 kconfig_tree.py --help` lists every option. `project.md` covers the
file format, the merge rules and the design.

## Copyright

Copyright (C) 2026 Nabeel Sowan <nabeel@vibes.se>
