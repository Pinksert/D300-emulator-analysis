#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_xver.py —— 四个版本 A 段固件交叉对照

盯两处「空桩」：
  1) 复位链第三跳 call 的目标（1.11 里是 0x000804FA = 纯 ret）
  2) main 的第二条 call 目标（1.11 里是 0x00080520 = 纯 ret）

做法：先从每个版本自己的复位入口解析出这两条 call 的目标，
再反汇编目标处的前几条指令。地址不同也能对上，因为是从代码里读出来的。
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

T = fr_emu.TABLE
BASE = 0x00080000          # A 段加载基址
ROM_BASE = 0x00080000

FW = r"C:\Users\admin\WorkBuddy\D300固件\D300Update"


def load(fn):
    with open(fn, "rb") as f:
        return f.read()


class Rom:
    """极简 ROM 视图：地址 → 字节"""

    def __init__(self, data, base=ROM_BASE):
        self.data = data
        self.base = base

    def rd(self, addr, n):
        off = addr - self.base
        return self.data[off:off + n]

    def dis(self, addr, count):
        off = addr - self.base
        out = []
        for _ in range(count):
            if off < 0 or off >= len(self.data) - 1:
                break
            ins = T.decode(self.data, off)
            if ins is None:
                out.append("  %08X: %04X  <undef>" % (self.base + off,
                                                      int.from_bytes(self.data[off:off + 2], "big")))
                off += 2
                continue
            mn, _ops, vals, size, w = ins
            arg = ", ".join("%s=%s" % (k, hex(v) if isinstance(v, int) else v)
                            for k, v in vals.items())
            out.append("  %08X: %-10s %s" % (self.base + off, mn, arg))
            off += max(2, size)
        return "\n".join(out)


def analyze(fn):
    rom = Rom(load(fn))
    # 复位入口固定 0xFC000，但地址是绝对 vaddr
    ent = 0x000FC000
    print("=" * 76)
    print("%s   复位入口 0x%06X" % (fn.split("/")[-1], ent))
    print(rom.dis(ent, 16))

    # 扫复位段：找所有 "ldi:32 X, rD" 后紧跟 "call @rD" 的配对
    calls = []
    off = ent
    reg = {}
    for _ in range(40):
        ins = T.decode(rom.data, off - rom.base)
        if ins is None:
            break
        mn, _o, vals, size, w = ins
        if mn == "ldi:32" and vals.get("ri") is not None:
            reg[vals["ri"]] = vals["i32"]
        if mn == "call" and vals.get("ri") is not None and vals["ri"] in reg:
            calls.append((off, reg[vals["ri"]]))
        if mn == "jmp":
            break
        off += max(2, size)

    print("\n  复位段 call 链：")
    for pc, tgt in calls:
        print("    0x%08X -> 0x%06X" % (pc, tgt))

    # 逐个看目标开头
    for pc, tgt in calls:
        print("\n  --- 目标 0x%06X 前 8 条 ---" % tgt)
        print(rom.dis(tgt, 8))

    # 第一个 call 目标里（硬件初始化）不看；第三个 call 目标是重点
    if len(calls) >= 3:
        t3 = calls[2][1]
        print("\n  ★ 复位链第三跳目标 0x%06X 全部内容（32 字节）" % t3)
        print("     %s" % " ".join("%02X" % x for x in rom.rd(t3, 32)))
    return rom, calls


roms = {}
for v in ("0102", "0103", "0110", "0111"):
    fn = FW + "/A300" + v + ".bin"
    roms[v] = analyze(fn)
    print()
