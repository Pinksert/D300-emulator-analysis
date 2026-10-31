# -*- coding: utf-8 -*-
"""按助记符过滤，打印 条目(助记符, 语法, ifmt, base列表) —— 用于写解释器时确定寻址模式"""
import re, sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

sys.stdout.reconfigure(encoding='utf-8')
HERE = _EMU
cpu = open(HERE + r"\fr30.cpu", "r", encoding="utf-8", errors="replace").read()
opc = open(HERE + r"\fr30-opc.c", "r", encoding="utf-8", errors="replace").read()

fields = {}
for m in re.finditer(r'^\((?:dnf|df|dnop)\s+(\S+)\s+"[^"]*"\s*\([^)]*\)\s+(\d+)\s+(\d+)', cpu, re.M):
    fields[m.group(1)] = (int(m.group(2)), int(m.group(3)))

ifmt = {}
for m in re.finditer(r'static const CGEN_IFMT (ifmt_\w+) ATTRIBUTE_UNUSED = \{\s*'
                     r'(\d+),\s*(\d+),\s*(0x[0-9a-fA-F]+),\s*\{(.*?)\}\s*\};', opc, re.S):
    ifmt[m.group(1)] = (int(m.group(3)), int(m.group(4), 16), re.findall(r'F \((\w+)\)', m.group(5)))

FILTER = sys.argv[1:] or ["ld", "lduh", "ldub", "st", "sth", "stb", "dmov", "dmovh", "dmovb", "mov", "ldi"]
for m in re.finditer(r'/\*\s*([a-z0-9:_.\-]+)(?:\s+([^*]*?))?\s*\*/\s*\{\s*\{[^}]*\},\s*\{(.*?)\},\s*&\s*(ifmt_\w+),\s*\{([^}]*)\}\s*\}',
                     opc, re.S):
    mnem, syn, fm, base = m.group(1), m.group(3), m.group(4), m.group(5)
    if mnem.split(":")[0] not in FILTER:
        continue
    bitsize, mask, fs = ifmt.get(fm, (0, 0, []))
    bases = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', base)]
    fs_str = " ".join(f"{f}@({fields.get(f,('?','?'))[0]},{fields.get(f,('?','?'))[1]})" for f in fs)
    print(f"{mnem:10s} ifmt={fm:16s} mask=0x{mask:04X} base=" +
          ",".join(f"0x{b:X}" for b in bases) + f"  fields=[{fs_str}]  syn={syn.strip()[:60]}")
