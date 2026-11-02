# -*- coding: utf-8 -*-
"""exp_bitmask.py —— 位操作立即数到底是掩码还是位号？（V15）

V14 记过一条怪事：`0x0AD652` 写的是 `btstl #2`，逐位实测却只有 **bit1** 放行。
本探针用 `frbox.bits`（掩码语义）把三件事重算一遍：

1. 五个回调槽的判定，翻译成「测的是哪几位」；
2. 全 A 段 13 处碰 `0x41A56` 的地方，翻译成「置/清/测的是第几位」；
3. 用寄存器跟踪把「`ldi #0x41A56` → 后续位操作」连成链，看转发关系。

输出对照表，问一句：V14 说的「bit4 + bit8」在掩码语义下还成不成立。
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

import fr_emu
from frbox import bits as B
from frbox.machine import create

T = fr_emu.TABLE
BITOPS = ("bandl", "borl", "beorl", "bandh", "borh", "beorh", "btstl", "btsth")
BITMAP = 0x41A56

SLOTS = [
    (0x3F028, 0x13, 0x0ACBDA),
    (0x3F040, 0x19, 0x0ACAFE),
    (0x3F048, 0x1B, 0x088A96),
    (0x3F074, 0x23, 0x0A6938),
    (0x3F078, 0x24, 0x0AD28E),
]


def dec(rom, base, pc):
    ins = T.decode(rom, pc - base)
    if ins is None:
        return None, 2
    return ins, max(2, ins[3])


def walk(rom, base, start, n=40):
    """线性走 n 条，返回 [(pc, mnem, vals, text)]，遇 ret/ret:d 停。"""
    out, pc = [], start
    for _ in range(n):
        ins, sz = dec(rom, base, pc)
        if ins is None:
            break
        mn, _, vals, size, w = ins
        out.append((pc, mn, vals, "%s  %s" % (mn, " ".join(
            "%s=%s" % (k, ("0x%X" % v) if isinstance(v, int) and v > 9 else v)
            for k, v in vals.items() if k not in ("op1", "op2", "op3", "op4", "op5")))))
        pc += sz
        if mn in ("ret", "ret:d", "reti"):
            break
    return out


def annotate(rom, base, start, n=40, watch=()):
    """跟着寄存器走，把位操作翻译成「地址 + 哪几位」。"""
    regs, out = {}, []
    for pc, mn, vals, text in walk(rom, base, start, n):
        note = ""
        if mn == "ldi:20" and "i20" in vals and "ri" in vals:
            regs[vals["ri"]] = vals["i20"]
        if mn in BITOPS and "u4" in vals:
            a = regs.get(vals.get("ri"))
            msk = B.mask(mn, vals["u4"])
            where = ("0x%05X" % a) if a is not None else "r%s?" % vals.get("ri")
            note = "[%s 掩码 0x%02X → %s]" % (
                where, msk, ",".join("bit%d" % x for x in B.bits(mn, vals["u4"])) or "无")
            if a is not None and a in watch:
                note += " ★"
        out.append((pc, text, note))
    return out


def find_bitmap_refs(rom, base):
    """扫全段：`ldi:20 #0x41A56, rN` 出现在哪。"""
    refs = []
    for off in range(0, len(rom) - 4, 2):
        ins = T.decode(rom, off)
        if not ins:
            continue
        mn, _, vals, size, _ = ins
        if mn == "ldi:20" and vals.get("i20") == BITMAP and "ri" in vals:
            refs.append((base + off, vals["ri"]))
    return refs


def main():
    m = create("d300-a")
    m.load()
    rom, base = m.bus.rom, m.bus.rom_base

    print("=" * 96)
    print("一、五个回调槽：判定翻译成「哪几位」")
    print("=" * 96)
    for slot, vec, cb in SLOTS:
        print("\n槽 0x%05X  vec 0x%02X  回调 0x%06X" % (slot, vec, cb))
        for pc, text, note in annotate(rom, base, cb, 24, watch=(BITMAP, BITMAP + 1,
                                                                0x80, 0x90, 0x60)):
            print("  0x%06X  %-52s %s" % (pc, text, note))

    print("\n" + "=" * 96)
    print("二、全段 13 处碰 0x41A56（`ldi #0x41A56` 的地方）")
    print("=" * 96)
    refs = find_bitmap_refs(rom, base)
    print("共 %d 处引用 → %s" % (len(refs), " ".join("0x%06X(r%d)" % r for r in refs)))
    for site, reg in refs:
        print("\n--- 0x%06X  r%d ---" % (site, reg))
        for pc, text, note in annotate(rom, base, site, 22, watch=(BITMAP, BITMAP + 1)):
            print("  0x%06X  %-52s %s" % (pc, text, note))

    print("\n" + "=" * 96)
    print("三、结论对照")
    print("=" * 96)
    print("V14 报的是「bit4 与 bit8 必须同时置」。按掩码语义重解，")
    print("`btsth #1` = 掩码 0x10 = bit4，`btstl #1`(于 0x41A57) = 掩码 0x01 = 字内 bit8。")
    print("也就是说：位号没变，**只是过去把掩码当成了位号**；`btstl #2` 之所以对应 bit1，")
    print("因为它本来就是掩码 0x02。")


if __name__ == "__main__":
    main()
