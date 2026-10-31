# -*- coding: utf-8 -*-
"""栈顶 × RP 的四种组合，用来定「掉到地址 0」到底是哪个参数造成的。

配置：栈顶取错（0x4616C）+ RP 沿用引导残值（不设），跑满 200 万步上限看会不会停。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create

ENTRY, STOP, WRONG_SP = 0x0008133A, 0x00080506, 0x0004616C


def one(sp, rp, tag, limit=2000000):
    m = create("d300-a", autospin=True)
    m.load()
    m.boot_task(ENTRY, sp, 0, stop_pc=STOP, rp=rp)
    base = m.step_count
    before = m.step_count
    m.run(max_steps=limit)
    n = m.step_count - before
    hit_limit = (m.step_count - before) >= limit
    print("  %-34s 入口后 %9s 步 / 累计 %9s / 停 0x%06X / %s"
          % (tag, format(n, ","), format(m.step_count, ","), m.cpu.pc,
             "跑满上限" if hit_limit else "自己停住"))


print("栈顶 × RP 四种组合，同一快照、上限 200 万步：")
one(WRONG_SP, None, "栈顶 0x4616C / RP 沿用残值")
one(WRONG_SP, 0x00081338, "栈顶 0x4616C / RP=0x81338")
one(0x000462EC, None, "栈顶 0x462EC / RP 沿用残值")
one(0x000462EC, 0x00081338, "栈顶 0x462EC / RP=0x81338")
