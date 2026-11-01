#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_realos_flow.py —— 原生 REALOS 跑起来之后，任务到底在等什么

V11 把任务切换跑通了（6 次），但 13 个任务只有 3 个上场。V12 的问题很具体：

    任务 1 为什么停下来？它在等哪个 flag 的哪几位？
    这些位该由谁置？是某个中断，还是另一个任务？

做法（不做任何打桩，全走固件自己的路径）：

  1. `boot_realos()` 把固件从复位跑到它自己的派发点，改派给任务 1；
  2. 挂**被动**的 `int #0x40` 观察钩子（`Machine.attach_int_hook`），每次服务调用
     记下：步数 / PC / 服务号 / R4–R7 / 当前 TCB（`*(0x45980)`）；
  3. 跑到空闲任务连续空转或步数用完为止；
  4. 打印：服务调用序列（按任务分组）、17 个 flag 对象的位模式与等待队列、
     13 条任务表记录的激活位与上下文。

判据：
  「任务在等 X」= 它最后一次 wai_flg 的 (flgid, waiptn)；
  「等到了」= 之后有同 flag 的 set_flg 调用，或该 flag 的位模式覆盖 waiptn。

用法：cd emu && python probe/exp_realos_flow.py [--steps N]
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import realos

CUR_TCB = realos.CUR_TCB
FLAG_CB = 0x00045C38
FLAG_STEP = 0x10
FLAG_N = 17
IDLE_PC = 0x000FE510

from frbox.flow import load_names          # 服务号→名称（社区表 + 常用补全）

NAMES = load_names()


def rd(m, a, n=4):
    d = m.fetch_bytes(a, n)
    return int.from_bytes(d, "big") if len(d) == n else None


def main():
    steps = 400000
    if "--steps" in sys.argv:
        steps = int(sys.argv[sys.argv.index("--steps") + 1])
    m = create("d300-a")
    m.load()
    info = m.boot_realos(task_id=1)
    print("启动：真实任务数 %d，派发点前 %d 步，改派给任务 %d（TCB 0x%X）"
          % (len(realos.records(m)), info["steps"], info["record"]["id"],
             info["record"]["tcb"]))

    log = []
    tcb_name = {}

    def hook(cpu, num, pc):
        # num 是 int 指令的向量号（恒 0x40）；服务号在 R12 的低 8 位（负数补码）
        svc = cpu.r[12] & 0xFF
        svc = svc - 0x100 if svc & 0x80 else svc
        tcb = rd(m, CUR_TCB) or 0
        log.append((m.step_count, pc, svc, cpu.r[4], cpu.r[5], cpu.r[6], cpu.r[7], tcb))

    m.attach_int_hook(hook)
    base = m.step_count
    idle_hits = 0
    prev = None
    # 跑到连续 40 万步都停在空闲任务（或步数用完）
    while m.step_count - base < steps:
        m.run(max_steps=20000, spin_guard=False)
        cur = rd(m, CUR_TCB) or 0
        if m.cpu.pc == IDLE_PC and cur == prev:
            idle_hits += 1
            if idle_hits >= 3:
                break
        else:
            idle_hits = 0
        prev = cur
        if m.cpu.halted or m.fault:
            break
    total = m.step_count - base
    print("跑了 %d 步（含启动），执行到 %d 次 int #0x40，停 PC=0x%08X"
          % (total, len(log), m.cpu.pc))
    if m.fault:
        print("  故障：", m.fault)

    # 任务切换时间线
    print("\n任务切换时间线（*0x45980 变化）")
    line, last = [], None
    for st, pc, num, a4, a5, a6, a7, tcb in log:
        if tcb != last:
            line.append((st, tcb))
            last = tcb
    print("  " + " → ".join("%d:0x%X" % (s, t) for s, t in line))

    print("\n服务调用序列（%d 条，按当前 TCB 分组）" % len(log))
    for st, pc, num, a4, a5, a6, a7, tcb in log:
        nm = NAMES.get(num, "?")
        args = ""
        if nm == "wai_flg":
            args = "flg%d ptn=0x%X mode=%s" % (a5 or 0, a6 or 0, a7)
        elif nm in ("set_flg", "clr_flg"):
            args = "flg%d bits=0x%X" % (a4 or 0, a5 or 0)
        elif nm in ("sta_tsk", "ter_tsk", "chg_pri"):
            args = "tsk=%s" % (a4 if nm != "chg_pri" else "id%d pri%d" % (a4 or 0, a5 or 0))
        print("  %8d  0x%08X  tcb=0x%05X  %-8s %s" % (st, pc, tcb, nm, args))

    print("\n10 个 flag 对象（0x45C38 起，步长 0x10；登记表 0xFE180 只到第 10 个）")
    for f in realos.flags(m):
        mark = "  ★有等待队列" if f["waiters"] else ""
        print("  flg%-2d @0x%05X  模式=0x%08X  队列头=0x%05X  等待者 %d%s"
              % (f["id"], f["cb"], f["pattern"], f["head"], f["waiters"], mark))

    print("\n13 条任务记录（表里的 state 高位是「复位时就激活」，不是「被 sta_tsk 过」）")
    for r in realos.records(m):
        t = [x for x in realos.states(m) if x["id"] == r["id"]][0]
        print("  任务 %-2d 入口 0x%08X 初始栈 0x%05X 表state 0x%08X 当前PC=0x%08X%s"
              % (r["id"], r["entry"], r["sp"], r["state"], t["pc"],
                 "  ◀ 在跑" if t["current"] else ""))


if __name__ == "__main__":
    main()
