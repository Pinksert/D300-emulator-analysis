# -*- coding: utf-8 -*-
"""追问：栈顶取错到底是不是独立成因？

3.2 表里「栈顶 0x4616C + RP=0x81338」和「栈顶 0x462EC + RP=0x81338」结果一模一样，
说明 0x180 字节的栈顶偏差这一版里恰好没踩到东西。把偏差放大到跨区，看是否单独致命。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create

ENTRY, STOP, GOOD_RP = 0x0008133A, 0x00080506, 0x00081338


def one(sp, tag, limit=2000000):
    m = create("d300-a", autospin=True)
    m.load()
    m.boot_task(ENTRY, sp, 0, stop_pc=STOP, rp=GOOD_RP)
    before = m.step_count
    m.run(max_steps=limit)
    print("  %-28s 入口后 %9s 步 / 停 0x%06X"
          % (tag, format(m.step_count - before, ","), m.cpu.pc))


print("RP 一律给对（0x81338），只动栈顶：")
one(0x000462EC, "0x462EC（任务表里的真值）")
one(0x0004616C, "0x4616C（表头字段，差 0x180）")
one(0x00046000, "0x46000（差 0x2EC）")
one(0x00045000, "0x45000（往下 0x12EC）")
one(0x0007F000, "0x7F000（系统栈，明显错）")

print()
print("再补一条反向：栈顶给对，RP 不给。")
m = create("d300-a", autospin=True)
m.load()
m.boot_task(ENTRY, 0x000462EC, 0, stop_pc=STOP, rp=None)
before = m.step_count
m.run(max_steps=2000000)
print("  %-28s 入口后 %9s 步 / 停 0x%06X"
      % ("0x462EC / RP 不设", format(m.step_count - before, ","), m.cpu.pc))
