#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_slot_profile.py —— 每个「已注册回调槽」到底在等哪个状态位

V13 的结论是：事件光打中断不够，ISR 里那段回调会先检查一个状态位，位没置就
什么都不做。要给系统补第二、第三个事件源，就必须先知道**每个槽对应哪个位**。

做法（全程只读，不改执行）：
  1. 跑 100 万步（开 0x23 事件源，与 V13 一致），收集被写过的回调槽；
  2. 每批跑完清空 `mem.all_writes`，只留「写槽」的记录 → 顺手拿到写入者 PC；
  3. 用 ROM 里的 ISR 扫描反查「槽 → 向量」，而不是套公式（0x3F074 就是例外）；
  4. 对每个槽取回调地址，反汇编入口后 N 条，**跟寄存器**抽状态位：
       `ldi:20 #0x60, r0` 之后的 `btstl #3, r0` 记作 0x60.bit3；
  5. 按 (位移, 位号) 汇总成画像表。

跑法：cd emu && python probe/exp_slot_profile.py [--steps N]
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
TBR = 0x000FFC00
SLOT_LO, SLOT_HI = 0x0003F000, 0x0003F100
BATCH = 50000

# 位操作（反汇编格式：`btstl   op1=8, op2=8, #0x8, r0`）—— 位号在前、寄存器在最后
RE_BITOP = re.compile(r"^(btst|bset|bclr|bor|band|bxor)\S*\s+.*#(0x[0-9A-Fa-f]+|-?\d+),\s*(r\d+)\s*$")
# 装载立即数（`ldi:20   op1=9, #0x60, op2=11, r0`）—— 中间的 op2 是噪声
RE_LDI = re.compile(r"^(ldi|mov|dmov)\S*\s+.*#(0x[0-9A-Fa-f]+|-?\d+),.*?(r\d+)\s*$")
# 直接页字面量写法（少数指令会直接给 `@0xNN`，或 dmovb 末尾的 `#130`）
RE_DP = re.compile(r"@0x([0-9A-F]{2})\b")
RE_DMOV = re.compile(r"^(dmov[hb])\S*\s+.*,\s*#(0x[0-9A-Fa-f]+|\d+)\s*$")


def isr_slots(m, entry, n=48):
    """沿 ISR 入口扫 n 条，返回第一个 0x3F0xx 立即数（它的回调槽）。"""
    rom, base = m.bus.rom, m.bus.rom_base
    off = entry - base
    for _ in range(n):
        if off < 0 or off >= len(rom) - 1:
            break
        ins = T.decode(rom, off)
        if ins is None:
            break
        mn, _o, vals, size, _w = ins
        for v in vals.values():
            if isinstance(v, int) and SLOT_LO <= v < SLOT_HI:
                return v
        if mn in ("ret", "reti"):
            break
        off += max(2, size)
    return None


def slot_to_vecs(m):
    """槽 → 使用它的向量列表（实测反查，不套公式）。"""
    out = {}
    for vec in range(0x10, 0x40):
        entry = m.bus.rd(TBR + (255 - vec) * 4, 4)
        if entry in (0x000FE250, 0x000FE776, 0x000FE3C0, 0x000FE41A):
            continue
        s = isr_slots(m, entry)
        if s is not None:
            out.setdefault(s, []).append(vec)
    return out


def run(steps):
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    m.enable_events(period=4096)
    writes = {}
    fills = {}
    for _ in range(steps // BATCH + 1):
        m.bus.all_writes.clear()
        m.run(max_steps=BATCH, spin_guard=False)
        for a, n, v, pc in m.bus.all_writes:
            if SLOT_LO <= a < SLOT_HI and v:
                writes.setdefault(a, []).append((pc, v))
                fills.setdefault(a, v)
        if m.cpu.halted or m.fault:
            break
    return m, fills, writes


def _num(s):
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def profile(m, entry, n=64):
    """反汇编回调并跟踪寄存器里的直接页地址 → [(addr, bit, 指令)]"""
    rom, base = m.bus.rom, m.bus.rom_base
    lines = fr_dis.disasm_addr(rom, entry, entry + n * 4, base)[:n]
    regs, hits, dps, dmov_offs = {}, [], set(), []
    for t in lines:
        # 行格式：`00088AA4  9b000060       ldi:20   op1=9, #0x60, op2=11, r0`
        # 按两个空格切：第二段是 hex 字节，第三段是「助记符 + 操作数」（前面还有对齐空格）
        body = t.split("  ", 2)[-1].strip()
        mn = body.split()[0] if body.split() else ""
        mo = RE_LDI.match(body)
        if mo:
            regs[mo.group(3)] = _num(mo.group(2))
        mb = RE_BITOP.match(body)
        if mb:
            addr = regs.get(mb.group(3))
            if addr is not None:
                hits.append((addr, _num(mb.group(2)), body.strip()))
                if addr < 0x100:
                    dps.add(addr)
        md = RE_DMOV.match(body)
        if md:
            # dmov 的 `#N` 是 **R13 基址 + 偏移**，不是绝对地址，单独列出供人工看
            off_v = _num(md.group(2))
            if off_v < 0x100:
                dmov_offs.append(off_v)
        for off in RE_DP.findall(body):
            dps.add(int(off, 16))
        if mn in ("ret", "reti"):
            break
    return lines, hits, dps, dmov_offs


def main():
    steps = 1000000
    if "--steps" in sys.argv:
        steps = int(sys.argv[sys.argv.index("--steps") + 1])

    print("=" * 78)
    print("回调槽画像：谁填的、回调里先检查哪个状态位")
    print("=" * 78)
    m, fills, writes = run(steps)
    s2v = slot_to_vecs(m)
    st = realos.states(m)
    print("跑完 %d 步；当前 PC=0x%06X" % (m.step_count, m.cpu.pc))
    print("在等：%s" % ("；".join(
        "任务%d:%s%s%s" % (t["id"], t["wait_kind"],
                           ("/flag%d" % t["wait_flag"]) if t["wait_flag"] else "",
                           ("/0x%X" % t["wait_pat"]) if t["wait_pat"] not in (0, 0xFFFFFFFF) else "")
        for t in st if t["wait_kind"] not in ("ready", "dormant")) or "无"))
    print()

    if not fills:
        print("一个槽都没填上 —— 与 V13 结论不符，先查 enable_events。")
        return

    print("被写的 0x3F0xx：%d 个" % len(fills))
    print("-" * 78)
    for a in sorted(fills):
        cb = fills[a]
        recs = writes.get(a, [])
        vecs = s2v.get(a, [])
        tag = "槽" if vecs else "非槽"
        print("0x%05X %s  vec %-10s ← 回调 0x%06X  写者 0x%06X ×%d"
              % (a, tag,
                 ",".join("0x%02X" % v for v in vecs) or "-",
                 cb, recs[0][0] if recs else 0, len(recs)))
    print()

    for a in sorted(fills):
        if a not in s2v:
            continue
        cb = fills[a]
        lines, hits, dps, dmov_offs = profile(m, cb)
        print("=" * 78)
        print("槽 0x%05X（vec %s）  回调 0x%06X"
              % (a, ",".join("0x%02X" % v for v in s2v[a]), cb))
        print("-" * 78)
        for t in lines[:16]:
            print("  " + t)
        if len(lines) > 16:
            print("  ...（共 %d 条）" % len(lines))
        if hits:
            print("  ★ 位判定：%s" % "；".join("0x%02X.bit%d" % (o, b) for o, b, _ in hits))
        else:
            print("  ★ 位判定：无（直接叫醒，不看状态位）")
        print("  触及的直接页：%s" % (" ".join("0x%02X" % x for x in sorted(dps)) or "无"))
        if dmov_offs:
            print("  dmov 偏移(R13+)：%s" % " ".join("0x%02X" % x for x in dmov_offs))
        print()


if __name__ == "__main__":
    main()
