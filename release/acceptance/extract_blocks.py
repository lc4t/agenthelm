"""Turn the bash blocks of an acceptance prompt (markdown) into one runnable script.

usage: python3 -I extract_blocks.py <prompt.md> <output.sh>

Every fenced ``bash`` block of the prompt is copied verbatim into the output
script, in order, each preceded by an ``echo "######## BLOCK <n>"`` marker, so
the commands the real-machine session will run can be executed word for word in
a Linux sandbox first (see release/ACCEPTANCE.md, "云端先行"). Check the syntax
with ``bash -n <output.sh>``, then run it with ``HOME=<a temporary directory>
bash <output.sh>``. Non-bash blocks and prose are ignored. The number of blocks
is printed. Exit codes: 0 success, 2 bad usage.
"""
import re
import sys

FENCE = "`" * 3
USAGE = "usage: python3 -I extract_blocks.py <prompt.md> <output.sh>"


def extract(text):
    """The bodies of all ``bash`` fenced blocks in ``text``, in order."""
    return re.findall(FENCE + r"bash\n(.*?)" + FENCE, text, flags=re.S)


def render(blocks):
    parts = ["#!/bin/bash\n"]
    for index, block in enumerate(blocks):
        parts.append(f'echo "######## BLOCK {index}"\n{block}\n')
    return "".join(parts)


def main(argv):
    if len(argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        blocks = extract(handle.read())
    with open(argv[2], "w", encoding="utf-8") as handle:
        handle.write(render(blocks))
    print(len(blocks), "blocks")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
