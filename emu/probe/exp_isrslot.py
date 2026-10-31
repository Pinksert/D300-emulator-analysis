#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_isrslot.py —— 48 个 ISR 各自读哪个 0x3F0xx 回调槽

做法：沿每个 ISR 的入口往后扫最多 40 条指令，收集出现过的
「0x3F000–0x3F0FF」立即数（ldi* 装载），第一个通常是它的回调槽。

输出一张 向量 → 入口 → 回调槽 的对照表，
再和任务 1 跑起来后被写过的 6 个槽比对。
"""
import sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")



sys.stdout.reconfigure(encoding="utf-8")

import fr_emu
from frbox.machine import create

T = fr_emu.TABLE

FILLED = {0x3F028, 0x3F040, 0x3F048, 0x3F078, 0x3F0D4, 0x3F0EB}


def vec_table(m):
    TBR = 0x000FFC00
    out = {}
    for vec in range(0x10, 0x40):
        out[vec] = m.bus.rd(TBR + (255 - vec) * 4, 4)
    return out


def isr_slots(m, entry, n=42):
    """扫入口后 n 条指令，返回出现过的 0x3F0xx 立即数序列"""
    rom = m.bus.rom
    base = m.bus.rom_base
    slots = []
    off = entry - base
    for _ in range(n):
        if off < 0 or off >= len(rom) - 1:
            break
        ins = T.decode(rom, off)
        if ins is None:
            break
        mn, _o, vals, size, w = ins
        for k, v in vals.items():
            if isinstance(v, int) and 0x3F000 <= v <= 0x3F0FF:
                slots.append(v)
                break
        # ret / reti 就停
        if mn in ("ret", "reti") and not mn.endswith(":d"):
            break
        off += max(2, size)
    return slots


m = create("d300-a")
m.load()
vt = vec_table(m)

print("=" * 78)
print("向量  入口        回调槽序列（前 4 个）                      状态")
print("-" * 78)
rows = []
for vec in sorted(vt):
    entry = vt[vec]
    if entry in (0x000FE250, 0x000FE776, 0x000FE3C0, 0x000FE41A):
        continue
    s = isr_slots(m, entry)
    if not s:
        continue
    first = s[0]
    mark = ""
    if first in FILLED:
        mark = "★ 已被填 0x%03X" % first
    rows.append((vec, entry, s, mark))
    print("0x%02X  0x%06X   %-40s %s" % (
        vec, entry,
        " ".join("0x%03X" % x for x in s[:4]), mark))

print()
print("=" * 78)
print("任务1 跑起来后被填的 6 个槽：", " ".join("0x%03X" % a for a in sorted(FILLED)))
covered = [r for r in rows if r[3]]
print("这些槽对应的 ISR：%d 个" % len(covered))
for vec, entry, s, mark in covered:
    print("  vec 0x%02X 入口 0x%06X 槽 0x%03X" % (vec, entry, s[0]))
print()
missing = [r for r in rows if not r[3]]
print("其余 %d 个 ISR 的槽仍为空（首次读到的槽）：" % len(missing))
for vec, entry, s, mark in missing[:12]:
    print("  vec 0x%02X 入口 0x%06X 槽 0x%03X" % (vec, entry, s[0]))
