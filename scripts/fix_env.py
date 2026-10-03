"""Move inline comments in .env onto their own lines (Docker can read `KEY=   # note` as the value "# note").

python3 scripts/fix_env.py [path]     # default: .env ; keeps a backup at .env.bak
"""

import pathlib
import re
import shutil
import sys

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
lines, out, fixed = path.read_text().splitlines(), [], 0
for line in lines:
    m = re.match(r"^([A-Za-z0-9_]+=)(.*?)\s+#\s?(.*)$", line)
    if m and not m.group(2).startswith(("'", '"')):
        out += [f"# {m.group(3)}", f"{m.group(1)}{m.group(2)}"]
        fixed += 1
    else:
        out.append(line)
if fixed:
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.write_text("\n".join(out) + "\n")
print(f"{path}: fixed {fixed} line(s) with inline comments")
