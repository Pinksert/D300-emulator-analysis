# -*- coding: utf-8 -*-
"""列出 fr30-opc.c 里全部指令的助记符与语法，供写解释器时对照"""
import re, sys, collections

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

sys.stdout.reconfigure(encoding='utf-8')
HERE = _EMU
opc = open(HERE + r"\fr30-opc.c", "r", encoding="utf-8", errors="replace").read()

rows = []
for m in re.finditer(r'/\*\s*([a-z0-9:_.\-]+)(?:\s+([^*]*?))?\s*\*/\s*\{\s*\{[^}]*\},\s*\{(.*?)\},\s*&\s*(ifmt_\w+),\s*\{([^}]*)\}\s*\}',
                     opc, re.S):
    mnem, syn, fm, base = m.group(1), m.group(3), m.group(4), m.group(5)
    rows.append((mnem, syn.strip() if syn else "", fm))

print("总条目:", len(rows))
seen = {}
for mnem, syn, fm in rows:
    seen.setdefault(mnem, []).append((syn, fm))
print("不同助记符:", len(seen))
for k in sorted(seen):
    syns = sorted(set(s for s, f in seen[k]))
    print(f"{k:12s} {len(seen[k]):2d}  " + " | ".join(syns[:4]))
