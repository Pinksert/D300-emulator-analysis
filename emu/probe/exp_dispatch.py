# -*- coding: utf-8 -*-
"""exp_dispatch.py —— 找到「谁调用回调」：单槽 + 派发器（V18 第 2 步）

V17 留的边界是「那个回调由谁在何时调用」。顺着 `0x417EC`（消息样本 +0x10）
反查只有两处引用：

    0x81752  写：`0x81744` 的 r4==1 分支，把 r6 存进去（只登记，不发送）
    0x82252  读：`0x8223A` 这个函数把值取出来，下一条就是 `call r0`

也就是说 A 段有一个**全局单槽回调登记表**（槽宽 1）+ 一个派发器。
本脚本回答四件事：

  1. 派发器 0x8223A、`call r0` 那一句，基线跑完有没有被执行；
  2. 槽里上电是什么值（0 = 没有登记）；
  3. 谁 call 了派发器（call 钩子全量记录）；
  4. 手动 call 一次回调 0x8225E，看它会不会把下一个回调登记进槽里。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402

DISPATCH = 0x0008223A       # 派发器入口
CALL_SITE = 0x00082258      # `call r0`
SLOT = 0x000417EC           # 回调指针槽
ARG = 0x000417F0            # 参数槽
FLAG = 0x0003F404           # 挂起标志
CB1 = 0x0008225E            # 固件自己那条消息里带的回调


def warm(m, rounds=40, batch=5000):
    for _ in range(rounds):
        m.run(max_steps=batch, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)


def main():
    m = create("d300-a")
    m.load()
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()

    print("=" * 78)
    print("阶段 1：上电时三个全局量的值")
    for name, a, n in (("回调槽 0x417EC", SLOT, 4), ("参数槽 0x417F0", ARG, 4),
                       ("挂起标志 0x3F404", FLAG, 1)):
        print("  %-16s = 0x%0*X" % (name, n * 2, m.bus.rd(a, n)))

    # call 钩子：记录所有子程序调用的目标
    calls = []

    def hook(cpu, tgt, pc):
        if tgt in (DISPATCH, CB1, CALL_SITE):
            calls.append((pc, tgt, list(cpu.r[4:8])))
        return False

    m.cpu.call_hook = hook

    print("\n" + "=" * 78)
    print("阶段 2：基线跑 20 万步，看派发器有没有被进过")
    warm(m)
    print("  步数 %d，服务 %d" % (m.step_count, len(m.flow.calls)))
    for name, a in (("派发器入口 0x8223A", DISPATCH), ("call r0 那一条 0x82258", CALL_SITE),
                    ("回调 0x8225E", CB1), ("登记 0x81744 被调用的落点", 0x00081744)):
        print("  %-28s 覆盖=%s" % (name, m.cov.visited(a) if m.cov else "?"))
    print("  钩到的调用：%s" % (calls or "（无）"))

    print("\n  两个全局量现在的值：")
    print("    回调槽 0x417EC = 0x%08X" % m.bus.rd(SLOT, 4))
    print("    参数槽 0x417F0 = 0x%08X" % m.bus.rd(ARG, 4))
    print("    挂起标志 0x3F404 = 0x%02X" % m.bus.rd(FLAG, 1))

    print("\n" + "=" * 78)
    print("阶段 3：手动走一次派发器（槽里现在是 0，应该什么也不做）")
    before = m.cov.words()
    if m.bus.rd(SLOT, 4) == 0:
        print("  槽 = 0 → 跳过：派发器会 `call r0`，硬调会跳到 0 地址跑飞")
    else:
        r = m.call_function(DISPATCH, max_steps=20000)
        print("  call 0x8223A -> %s，执行地址 %d → %d" % (r, before, m.cov.words()))

    print("\n" + "=" * 78)
    print("阶段 4：手动调一次回调 0x8225E，看它登记了什么")
    before = m.cov.words()
    r = m.call_function(CB1, r4=1, r5=0x0A, max_steps=60000)
    print("  call 0x8225E -> %s，执行地址 %d → %d（+%d）"
          % (r, before, m.cov.words(), m.cov.words() - before))
    print("  回调槽 0x417EC = 0x%08X" % m.bus.rd(SLOT, 4))
    print("  参数槽 0x417F0 = 0x%08X" % m.bus.rd(ARG, 4))
    print("  挂起标志 0x3F404 = 0x%02X" % m.bus.rd(FLAG, 1))
    print("  钩到的调用：%s" % ([(("0x%08X" % pc), ("0x%08X" % t)) for pc, t, _ in calls] or "（无）"))

    print("\n" + "=" * 78)
    print("阶段 5：槽里有值了，再走一次派发器")
    before = m.cov.words()
    r = m.call_function(DISPATCH, r4=0, r5=0, max_steps=60000)
    print("  call 0x8223A -> %s，执行地址 %d → %d（+%d）"
          % (r, before, m.cov.words(), m.cov.words() - before))
    print("  回调槽 0x417EC = 0x%08X" % m.bus.rd(SLOT, 4))
    print("  参数槽 0x417F0 = 0x%08X" % m.bus.rd(ARG, 4))
    print("  挂起标志 0x3F404 = 0x%02X" % m.bus.rd(FLAG, 1))
    print("  钩到：")
    for pc, tgt, regs in calls:
        print("    pc=0x%08X -> 0x%08X r4..r7=%s"
              % (pc, tgt, [("0x%X" % x) for x in regs]))


if __name__ == "__main__":
    main()
