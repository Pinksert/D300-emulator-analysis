#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_cbmap.py —— 挖出「回调表 0x3F000-0x3F0FF 本该填什么」

思路：不做真符号执行，只维护「每个寄存器当前已知的立即数」。
遇到 ldi:8/ldi:12/ldi:20/ldi:32 就记下；遇到 st rX,@rY 且 rY 已知落在
0x3F000-0x3F0FF 就输出一条 (槽地址, 被写入值, PC)。

再过滤：值必须看起来像代码指针（0x00080000-0x00100000 或 0x000AB000+）。
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


def scan(machine_name):
    m = create(machine_name)
    m.load()
    rom = bytes(m.bus.rom)
    base = m.bus.rom_base

    reg = {}            # 寄存器号 -> 已知立即数
    hits = []           # (槽, 值, pc)
    off = 0
    while off < len(rom) - 1:
        ins = T.decode(rom, off)
        if ins is None:
            off += 2
            reg.clear()
            continue
        mn, _ops, vals, size, w = ins
        pc = base + off

        # --- 记录立即数 ---
        if mn.startswith("ldi"):
            dst = vals.get("ri")
            imm = vals.get("i8", vals.get("i20", vals.get("i32")))
            if dst is not None and imm is not None:
                reg[dst] = imm
        elif mn in ("mov", "movn"):
            rs, rd = vals.get("rs1"), vals.get("ri")
            if rs is not None and rd is not None:
                if rs in reg:
                    reg[rd] = reg[rs]
                else:
                    reg.pop(rd, None)
        elif mn.startswith("ld"):
            # ld @rX, rY —— 只有 ld @r0 这种才算，其它一律作废目标
            rd = vals.get("ri")
            rs = vals.get("rs")
            if rd is not None:
                reg.pop(rd, None)
        else:
            # 其它指令视作破坏：保守地把所有写过的东西清掉一半
            for k in list(reg):
                if k in (0, 1):
                    continue
            # 不破坏（太保守会漏），只在明确写寄存器时清

        # --- 检测 st rX,@rY （rj/ri 组合） ---
        if mn in ("st", "stb", "sth") and "ri" in vals and "rj" in vals:
            addr = reg.get(vals["rj"])
            src = vals.get("ri")
            if addr is not None and 0x3F000 <= addr <= 0x3F0FF and src in reg:
                hits.append((addr, reg[src], pc, mn))

        off += max(2, size)

    return hits


for name in ("d300-a", "d300-b"):
    print("=" * 74)
    print("机器", name)
    hits = scan(name)
    # 只保留像代码指针的
    code = [h for h in hits if 0x00080000 <= h[1] < 0x00100000]
    other = [h for h in hits if not (0x00080000 <= h[1] < 0x00100000)]
    print("  总命中 %d 条，其中值像代码指针 %d 条" % (len(hits), len(code)))
    print()
    slots = {}
    for a, v, pc, mn in code:
        slots.setdefault(a, []).append((v, pc, mn))
    print("  --- 回调槽 → 候选函数 ---")
    for a in sorted(slots):
        lst = slots[a]
        uniq = sorted({v for v, _, _ in lst})
        print("    [0x%05X] %d 处写入, 候选 %s" % (
            a, len(lst), " ".join("0x%06X" % v for v in uniq)))
        for v, pc, mn in lst[:4]:
            print("        0x%08X: %s = 0x%06X" % (pc, mn, v))
    print()
    print("  --- 值不像代码指针的写（多半是清 0 / 写标志）---")
    cnt = {}
    for a, v, pc, mn in other:
        cnt.setdefault((a, v), []).append(pc)
    for (a, v), pcs in sorted(cnt.items())[:20]:
        print("    [0x%05X] = 0x%X  ×%d  (首处 0x%08X)" % (a, v, len(pcs), pcs[0]))
    print()
