#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_autospin.py —— 自动消除「等硬件就绪」卡点

已实证的模式（三处同构）：
    ldi:20 <reg>, r0
    borl  #2, @r0          ; 置 bit2 = 启动
    ldi:20 <reg>, r0
    btsth #N, @r0          ; 等 bit (N<<4)
    beq   -4               ; 不满足就回去
    ldi:20 <reg>, r0
    bandl #D, @r0          ; 软件自己清启动位

本脚本：
  1. 停顿检测：连续 SPIN_N 步 PC 跨度 < 64
  2. 在停顿窗口里找 btsth，取它的 u4 立即数 N，得到 mask = N<<4
  3. 找窗口里 ldi 的直接页寄存器（<0x1000），把 shadow[reg] |= mask
  4. 记录 (PC, reg, bit)，继续；同一处连续注入 K 次仍过不去就放弃
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

import fr_emu
from frbox.machine import create

T = fr_emu.TABLE
TASK1 = 0x0008133A
LIMIT = 1_500_000
SPIN_N = 1200
MAX_PER_SITE = 8


def decode_run(m, lo, hi):
    """反汇编 [lo,hi)，返回 (ldi 的直接页寄存器列表, btsth 的 u4 列表)"""
    rom, rb = m.bus.rom, m.bus.rom_base
    off = lo - rb
    regs, masks = [], []
    while off < hi - rb and off < len(rom) - 1:
        ins = T.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mn, _o, vals, size, w = ins
        for key in ("i8", "i12", "i20", "i32"):
            v = vals.get(key)
            if isinstance(v, int):
                if mn.startswith("ldi") and 0 < v < 0x1000:
                    regs.append(v)
                break
        if mn == "btsth":
            u4 = vals.get("u4")
            if isinstance(u4, int) and u4:
                masks.append(u4 << 4)
        off += max(2, size)
    return regs, masks


def build():
    m = create("d300-a")
    m.load()
    c = m.cpu
    for _ in range(300000):
        if c.pc == 0x00080506:
            break
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    c.pc = TASK1
    c.r[15] = 0x0004616C
    c.r[4] = 0
    c.halted = False
    return m


def main():
    m = build()
    c = m.cpu
    steps0 = m.step_count

    recent = []
    sites = []            # (pc, reg, mask, 步数, 注入次数)
    done = {}
    give_up = set()

    i = 0
    while i < LIMIT:
        p = c.pc
        recent.append(p)
        if len(recent) > SPIN_N:
            recent.pop(0)

        if i and i % 256 == 0 and len(recent) >= SPIN_N // 2:
            lo, hi = min(recent), max(recent)
            uniq = set(recent)
            if hi - lo < 64 and 1 < len(uniq) <= 12 and len(recent) >= SPIN_N // 2:
                key = lo & ~1          # 只保证半字对齐，不能向下多取整
                if key in give_up:
                    pass
                else:
                    regs, masks = decode_run(m, lo, hi + 2)
                    if regs and masks:
                        reg = regs[0]      # 循环里第一个 ldi 就是它轮询的寄存器
                        mask = max(masks)
                        n = done.get(key, 0) + 1
                        done[key] = n
                        if n <= MAX_PER_SITE:
                            old = m.dp.shadow.get(reg, 0)
                            m.dp.shadow[reg] = old | mask
                            sites.append((key, reg, mask, i, n, old))
                        else:
                            give_up.add(key)
                            sites.append((key, reg, mask, i, -1, 0))
        c.step()
        m.step_count += 1
        i += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)

    print("=" * 80)
    print("实跑 %s 步，最终 PC=0x%06X" % (format(m.step_count - steps0, ","), c.pc))
    print()
    print("卡点清单（按出现顺序）：")
    print("  #  循环起点    寄存器  等待位   首遇步数   第几次注入  旧值")
    for k, (key, reg, mask, at, n, old) in enumerate(sites, 1):
        print("  %-2d 0x%06X  0x%03X   0x%02X    %-9s  %-3s        0x%02X"
              % (k, key, reg, mask, format(at, ","),
                 ("放弃" if n < 0 else str(n)), old))
    print()
    hit = sorted({a for a, *_ in m.bus.all_writes if 0x3F000 <= a <= 0x3F0FF})
    print("回调槽被写 %d 个: %s" % (len(hit), " ".join("0x%03X" % a for a in hit)))
    cnt = {}
    print()
    print("最终停点反汇编：")
    print(m.disasm_at((c.pc - 16) & ~1, 14))


main()
