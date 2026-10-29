#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_spin.py —— 自动卡点探测：找出应用区所有「等硬件就绪」的轮询点

方法（不预设设备语义）：
  1. 启动链跑到 0x80506 → 跳任务 1（0x8133A）
  2. 每步记录 PC。若最近 WINDOW 步里 PC 只在很小的范围里打转，判定「自旋」
  3. 自旋时，从最近一次「直接页读」里取出寄存器号，把它的 bit7 置 1（假设
     bit7 = 就绪，这是 0x482 / 0x0AF 两处实测到的同一模式）
  4. 记录 (自旋 PC, 寄存器)，继续跑；下一个卡点再记一条

产出一张「卡点清单」：地址 / 轮询的寄存器 / 遇到时已跑多少步。
这张表就是后续设备建模的优先级列表。
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
WINDOW = 6000            # 观察最近这么多步
SPAN = 0x100             # PC 跨度小于这个才算自旋
MAXPCS = 14              # 不同 PC 数少于这个才算自旋
LIMIT = 6_000_000


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


def find_regs(m, base, span=0x40):
    """在 base..base+span 里找「ldi:xx 直接页地址, rN」并给出出现次数"""
    rom = m.bus.rom
    rb = m.bus.rom_base
    cnt = {}
    off = base - rb
    end = off + span
    while off < end and off < len(rom) - 1:
        ins = T.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mn, _o, vals, size, w = ins
        for k, v in vals.items():
            if isinstance(v, int) and 0 < v < 0x1000:
                cnt[v] = cnt.get(v, 0) + 1
                break
        off += max(2, size)
    return sorted(cnt.items(), key=lambda x: -x[1])[:3]


def main():
    m = build()
    c = m.cpu
    steps0 = m.step_count
    recent = []
    hotspots = {}          # pc 区间 -> 首次出现步数
    poked = []
    io_seen = 0

    for i in range(LIMIT):
        p = c.pc
        recent.append(p)
        if len(recent) > WINDOW:
            recent.pop(0)

        if i and i % 512 == 0 and len(recent) == WINDOW:
            lo, hi = min(recent), max(recent)
            if hi - lo < SPAN and len(set(recent)) < MAXPCS:
                key = hi & ~0xF
                if key not in hotspots:
                    # 从自旋窗口的代码里认寄存器：扫 ldi:xx <直接页地址>，
                    # 配对上紧随的 btsth/btstl/bandh/bandl/borl/st 就是目标
                    cand = find_regs(m, key)
                    if not cand:
                        cand = [(-1, 0)]
                    hotspots[key] = (i, cand, len(set(recent)))
                    # 给最热的那个寄存器置 bit7
                    for a, _n in cand[:1]:
                        old = m.dp.shadow.get(a, 0)
                        m.dp.shadow[a] = old | 0x80
                        poked.append((a, old))
                else:
                    # 同一处又卡住了 → 再置一次
                    i0, cand, _ = hotspots[key]
                    for a, _n in cand[:1]:
                        m.dp.shadow[a] = m.dp.shadow.get(a, 0) | 0x80

        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)

    print("=" * 78)
    print("实跑 %s 步，停 PC=0x%06X" % (format(m.step_count - steps0, ","), c.pc))
    total_pcs = len(set(recent))
    print("最近 %d 步内不同 PC %d 个（越小越像卡住）" % (WINDOW, total_pcs))
    print()
    print("发现卡点 %d 处：" % len(hotspots))
    for key in sorted(hotspots):
        i0, cand, npc = hotspots[key]
        regs = " ".join("0x%03X×%d" % (a, n) for a, n in cand)
        print("  PC≈0x%06X  首遇第 %s 步  循环内 %d 个 PC  轮询寄存器: %s"
              % (key, format(i0, ","), npc, regs))
    print()
    print("置位操作 %d 次，涉及寄存器: %s"
          % (len(poked), " ".join("0x%03X" % a for a, _ in poked[:20])))
    # 覆盖率
    hit = sorted({a for a, *_ in m.bus.all_writes if 0x3F000 <= a <= 0x3F0FF})
    print("回调槽被写 %d 个: %s" % (len(hit), " ".join("0x%03X" % a for a in hit)))
    print()
    print("--- 最终停点反汇编 ---")
    print(m.disasm_at((c.pc - 16) & ~1, 14))


main()
