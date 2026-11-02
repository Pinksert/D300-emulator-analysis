#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_evt_bitmap.py —— 0x41A56 是「事件位图」，谁置它、谁读它

exp_slot_profile 的结论：几个回调不约而同地先检查 **0x41A56** 的某一位
（vec 0x13 → bit4、vec 0x19 → bit8、vec 0x24 → bit9），而 vec 0x1B 检查
直接页 0x60 的 bit8/bit1/bit2。

也就是说「有没有数据」这件事并不在每个外设自己的寄存器里，而是集中记在
一个全局位图上：**外设把位图对应位置 1，中断处理才往下走**。这解释了为什么
V13 只补一个状态位（0x80/bit3）只能推进一条线。

本探针做三件事（纯静态 + 一次短运行）：
  1. 全 A 段扫描：找 `ldi … #0x41A56` 之后用同一寄存器做 bset/bor/bclr 的位置
     → 「谁置位、置第几位」；
  2. 反汇编这些位置的上下文（±6 条），看它们是哪个函数（前后有无特征串/写口）；
  3. 短跑 100 万步之后读一次位图的实际值，看哪些位被固件自己置起来了。

跑法：cd emu && python probe/exp_evt_bitmap.py
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

import fr_dis
import fr_emu
from frbox.machine import create
from frbox import realos

T = fr_emu.TABLE
BITMAP = 0x00041A56
RE_LDI = re.compile(r"^(ldi|mov)\S*\s+.*#(0x[0-9A-Fa-f]+|-?\d+),.*?(r\d+)\s*$")
RE_BITOP = re.compile(r"^(btst|bset|bclr|bor|band|bxor)\S*\s+.*#(0x[0-9A-Fa-f]+|-?\d+),\s*(r\d+)\s*$")
RE_ST = re.compile(r"^(st|sth|stb)\s+.*?(r\d+)\s*$")


def _num(s):
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def scan(m, limit=None):
    """全段扫描：返回 [(pc, kind, bit, text)]，kind 为 'set'/'clr'/'test'。"""
    rom, base, end = m.bus.rom, m.bus.rom_base, m.bus.rom_end
    off = 0
    regs = {}
    out = []
    last_ldi = {}          # reg -> (pc, value)
    while off < len(rom) - 1:
        ins = T.decode(rom, off)
        if ins is None:
            regs.clear()
            off += 2
            continue
        mn, _o, vals, size, _w = ins
        pc = base + off
        txt, _ = fr_dis.line(rom, off, base)
        body = txt.split("  ", 2)[-1].strip()
        mo = RE_LDI.match(body)
        if mo:
            regs[mo.group(3)] = _num(mo.group(2))
            last_ldi[mo.group(3)] = (pc, _num(mo.group(2)))
        mb = RE_BITOP.match(body)
        if mb:
            r = mb.group(3)
            if regs.get(r) == BITMAP:
                kind = "set" if mb.group(1).startswith(("bset", "bor")) else \
                       ("clr" if mb.group(1).startswith("bclr") else "test")
                out.append((pc, kind, _num(mb.group(2)), body))
            regs.pop(r, None)          # 用过就清，避免误配
        ms = RE_ST.match(body)
        if ms and regs.get(ms.group(2)) == BITMAP:
            out.append((pc, "store", -1, body))
            regs.pop(ms.group(2), None)
        off += max(2, size)
    return out


def context(m, pc, n=8):
    rom, base = m.bus.rom, m.bus.rom_base
    lo = max(base, pc - n * 4)
    hi = pc + n * 4
    return fr_dis.disasm_addr(rom, lo, hi, base)


def main():
    m = create("d300-a")
    m.load()
    hits = scan(m)
    print("=" * 78)
    print("全 A 段对 0x%05X 位图的操作：%d 处" % (BITMAP, len(hits)))
    print("=" * 78)
    for pc, kind, bit, txt in hits:
        print("0x%06X  %-5s bit%-2d  %s" % (pc, kind, bit, txt))
    print()

    print("=" * 78)
    print("逐处上下文（看它在哪个函数里）")
    print("=" * 78)
    for pc, kind, bit, txt in hits:
        print("-" * 78)
        print("0x%06X %s bit%d" % (pc, kind, bit))
        for t in context(m, pc):
            mark = " <<<" if t.startswith("%08X" % pc) else ""
            print("  " + t + mark)
    print()

    # 一次短跑，看固件自己把哪些位置起来了
    m2 = create("d300-a")
    m2.load()
    m2.boot_realos(1)
    m2.enable_events(period=4096)
    for _ in range(20):
        m2.run(max_steps=50000, spin_guard=False)
        if m2.cpu.halted or m2.fault:
            break
    v = m2.bus.rd(BITMAP, 2)
    print("=" * 78)
    print("跑 %d 步之后位图 0x%05X = 0x%04X（置起的位：%s）"
          % (m2.step_count, BITMAP, v,
             " ".join("bit%d" % i for i in range(16) if v >> i & 1) or "无"))
    st = realos.states(m2)
    print("在等：%s" % ("；".join(
        "任务%d:%s%s" % (t["id"], t["wait_kind"],
                         ("/flag%d/0x%X" % (t["wait_flag"], t["wait_pat"]))
                         if t["wait_flag"] else "")
        for t in st if t["wait_kind"] not in ("ready", "dormant")) or "无"))
    print()
    print("位图附近 32 字节：")
    for i in range(0, 32, 16):
        d = m2.bus.rd(BITMAP - 16 + i, 16)
        print("  0x%05X  %s" % (BITMAP - 16 + i, " ".join("%02X" % ((d >> (8 * (15 - j))) & 0xFF)
                                                          for j in range(16))))


if __name__ == "__main__":
    main()
