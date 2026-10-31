# -*- coding: utf-8 -*-
"""四种挂法的对照，int #0x40 的次数一律按「执行到的 int #0x40 指令条数」数。

为什么不用 svc_hook 计数：不挂钩子时 int #0x40 会真的走 EIT（进向量表），
挂上钩子就被截住当服务调用返回 —— 两者根本不是同一条路径。用 opcode 计数
才能把「int #0x40 执行了多少次」和「有多少次被当服务处理」分开。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create

ENTRY, RP, STOP = 0x0008133A, 0x00081338, 0x00080506
INT40 = 0x1F40


def run(tag, hook, limit=2000000):
    m = create("d300-a", autospin=True)
    m.load()
    if hook is not None:
        hook(m)
    m.boot_task(ENTRY, from_table=True, arg=0, stop_pc=STOP, rp=RP)
    base = m.step_count
    cpu = m.cpu
    orig = cpu.step
    n = [0]

    def stepped():
        if m.bus.rd(cpu.pc, 2) == INT40:
            n[0] += 1
        orig()

    cpu.step = stepped
    m.run(max_steps=limit)
    cpu.step = orig
    print("  %-26s 入口后 %9s 步 / 累计 %9s / int #0x40 %5s / 停 0x%06X"
          % (tag, format(m.step_count - base, ","), format(m.step_count, ","),
             format(n[0], ","), cpu.pc))


print("同一快照、同一 200 万步上限，只换 μITRON 那一层：")
run("A 不挂钩子（走真向量表）", None)
run("B --itron（一律立即返回）", lambda m: m.attach_itron())
run("C --itron-block（真阻塞）", lambda m: m.attach_kernel(auto_event=False))
run("D --itron-block --autoevent", lambda m: m.attach_kernel(auto_event=True))
print()
print("读法：")
print("  A 的 int #0x40 全部进了向量表，没有一次被当服务处理 —— 等于固件自己的 REALOS 不可用")
print("  B 每次都立即成功返回，任务因此一路往下跑，从来不等")
print("  C 在第一个 wai_flg 上停住，步数最少，但那次等待是真的")
print("  D 补上 13 个事件后走完，最后自己调 sys_ext_tsk 退出")
