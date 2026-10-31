#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_task3.py —— 应用区长跑：能跑多远、覆盖率多少、最终卡在哪

前置结论：启动链跑到 0x80506（BSS 清零后）再跳 0x8133A，
应用区 300k 步内写出 802 个 PC、10 次 int#0x40、填 6 个回调槽。
本实验拉长到 3,000,000 步，并做覆盖率对照。
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
import fr_svc
from frbox.machine import create

NAMES = fr_svc.load_props()
TASK1 = 0x0008133A


def build():
    m = create("d300-a")
    m.load()
    c = m.cpu
    for _ in range(200000):
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


def long_run(limit):
    m = build()
    c = m.cpu
    cnt = {}
    orig = fr_emu.Fr.eit

    def patched(self, vec, level=None):
        cnt.setdefault(vec, []).append(self.pc)
        return orig(self, vec, level)

    fr_emu.Fr.eit = patched
    pcs = set()
    tail = []
    steps0 = m.step_count
    stop = None
    try:
        for i in range(limit):
            p = c.pc
            pcs.add(p)
            tail.append(p)
            if len(tail) > 12:
                tail.pop(0)
            if p < 0x1000:
                stop = ("low", p, i)
                break
            c.step()
            m.step_count += 1
            if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
                for d in m.devices:
                    d.tick(m.cycles_per_tick)
    finally:
        fr_emu.Fr.eit = orig

    print("=" * 74)
    print("上限 %s 步" % format(limit, ","))
    print("  实跑 %s 步 / 不同 PC %s / 停 0x%06X"
          % (format(m.step_count - steps0, ","), format(len(pcs), ","), c.pc))
    print("  μITRON 服务 ×%d" % sum(len(v) for v in cnt.values()))
    if cnt:
        per = {}
        for vec, sites in cnt.items():
            per[vec] = len(sites)
        print("    ", " ".join("vec0x%02X:%d" % (k, v) for k, v in sorted(per.items())))
    hit = sorted({a for a, *_ in m.bus.all_writes if 0x3F000 <= a <= 0x3F0FF})
    print("  回调槽 %d 个: %s" % (len(hit), " ".join("0x%03X" % a for a in hit)))
    if stop:
        print("  ⚠ 停在低地址", stop)
    # ROM 码段覆盖率（0x80000-0xC0002 里有多少字节的 PC 被走过）
    code_pcs = [p for p in pcs if 0x00080000 <= p < 0x000C0002]
    print("  代码区 PC %s 个（应用区 0x81300 以上 %s 个）"
          % (format(len(code_pcs), ","),
             format(len([p for p in code_pcs if p >= 0x81300]), ",")))
    return m, pcs, cnt


print("对照组：原始启动链（不跳任务）")
m0 = create("d300-a")
m0.load()
m0.run(44346)
p0 = {p for p, *_ in m0.bus.all_writes}
print("  启动链 PC 数见 V6：824，这里只作参照")
print()

long_run(3000000)
print()
long_run(10000000)
