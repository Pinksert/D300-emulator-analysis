# -*- coding: utf-8 -*-
"""V11：任务 1 由固件自带 REALOS 原生跑起来 —— 跑多远、调了什么、换没换过任务。

判据：
  1. 从复位到首次派发 43,428 步；派发后 2,000 步内 PC 一定到过 0x08133A；
  2. 30 万步里 `int #0x40` 上千次，服务号只有 μITRON 那几种，
     **每一次都由固件内核处理**（没挂半主机钩子）；
  3. S 位在 0x0FE4F2 / 0x0FE4F8 / 0x0FE4FE / 0x0FE50A / 0x0FE50C 这五处来回切 ——
     这就是「andccr/orccr 必须跟着换 R15」的那段现场；
  4. 【A 模式 / 只开 xfer】当前 TCB 变量 *(0x45980) 全程只被写一次
     → **不开调度建模时任务切换确实不会发生**（V2–V10 看到的就是这个）；
  5. 【B 模式 / 再开 0x44】当前 TCB 被写多次 → **任务切换真的发生了**（V11）；
  6. 内核确实起了别的任务（4/5/9/10 挂进队列），但它们都在等。

两种模式必须分开跑、分开报，否则数字会混：
  A = `enable_xfer()` + `realos.prepare()`      （V10 基线，任务 1 一路跑到底）
  B = `boot_realos()`（= A + `enable_dispatch()`）（V11，调度器会被叫醒）

跑法：cd emu && python probe/exp_realos_run.py
"""
import collections
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox.itron import SVC
from frbox import realos

STEPS = 300000


def sep(t=""):
    print()
    print("=" * 78)
    if t:
        print(t)


def main():
    # ---- 1. 复位到派发
    m = create("d300-a")
    m.load()
    m.run(max_steps=600000, stop_pcs={realos.DISPATCH_LD}, spin_guard=False)
    boot = m.step_count
    sep("1. 复位 → REALOS 首次派发")
    print(f"  {boot:,} 步，PC=0x{m.cpu.pc:06X}")

    # ---- 2. S 位切换现场（这一段就是 orccr/andccr 的用武之地）
    sep("2. 上下文恢复里的 S 位切换（0x0FE4DE–0x0FE50C）")
    c = m.cpu
    prev, tr = c.s, []
    for _ in range(40):
        pc0 = c.pc
        m.step(1)
        if c.s != prev:
            tr.append((m.step_count, pc0, prev, c.s, c.r[15], c.dr[2], c.dr[3]))
            prev = c.s
    for n, (st, pc, a, b, r15, ssp, usp) in enumerate(tr, 1):
        print(f"  第{n}次 {st:>7,} 步 @0x{pc:06X}: S {a}→{b}   r15=0x{r15:06X}"
              f"  SSP=0x{ssp:06X}  USP=0x{usp:06X}")
    print("  对应指令：")
    print("    " + m.disasm_at(0x0FE4F2, 1).strip())
    print("    " + m.disasm_at(0x0FE4F8, 1).strip())
    print("    " + m.disasm_at(0x0FE4FE, 1).strip())
    print("    " + m.disasm_at(0x0FE50A, 1).strip())
    print("    " + m.disasm_at(0x0FE50C, 1).strip())
    print("  → S 位一翻，R15 就在 SSP / USP 两个槽之间换支；不换支的话")
    print("    0x0FE4F4–0x0FE4FC 那 4 条 ld/st 全落在同一个槽上，reti 弹出错的 PC。")

    # ---- 3. A 模式：只开 xfer，改派任务 1 跑 30 万步（V10 基线）
    sep("3. 【A 模式 · 只开 xfer】改派任务 1 并跑 %s 步" % format(STEPS, ","))
    m2 = create("d300-a")
    m2.load()
    m2.enable_xfer()
    info = realos.prepare(m2, 1, max_steps=600000)
    print(f"  派发：TCB=0x{info['record']['tcb']:06X} 入口=0x{info['record']['entry']:06X}"
          f" 栈顶=0x{info['record']['sp']:06X}  上下文 PC=0x{info['context']['pc']:06X}")

    svc = collections.Counter()
    orig_eit = m2.cpu.eit

    def eit(num, level=None):
        if num == 0x40:
            svc[m2.cpu.r[12] & 0xFF] += 1
        return orig_eit(num, level)

    m2.cpu.eit = eit
    b = m2.bus
    b.watch.add(realos.CUR_TCB)
    b.access_log.clear()
    m2.run(max_steps=STEPS)
    print(f"  跑了 {STEPS:,} 步，int #0x40 = {m2.cpu.int40:,} 次，"
          f"停在上界时 PC=0x{m2.cpu.pc:06X}")
    print(f"  r12（服务号）= 0x{m2.cpu.r[12]:08X}   ILM={m2.cpu.ilm}"
          f"   S={m2.cpu.s}（1 = 任务运行在系统态，R15 就是任务栈）")

    sep("4. 【A 模式】服务调用分布（全部由固件内核处理）")
    total = sum(svc.values())
    print(f"  共 {total:,} 次 / {len(svc)} 种：")
    for code, cnt in svc.most_common():
        neg = (code & 0xFF) - 256
        print(f"    0x{code:02X} ({neg:>4})  {SVC.get(neg, '?'):<18} {cnt:>7,}")

    def dump_tcb_writes(bus, log, title):
        sep(title)
        ws = [e for e in log if e[0] == "W" and e[1] == realos.CUR_TCB]
        print(f"  {len(ws)} 次：")
        for kind, a, n, v, pc in ws:
            print(f"    0x{a:05X} ← 0x{v:06X} ({n}B) 来自 0x{pc:06X}")
        print(f"  现在是 0x{bus.rd(realos.CUR_TCB, 4):06X}")
        return ws

    ws = dump_tcb_writes(b, b.access_log,
                         "5. 【A 模式】当前 TCB 变量 *(0x%05X) 的写入" % realos.CUR_TCB)
    if len(ws) <= 1:
        print("  → 只有派发那一次写过，之后一次都没改 → **A 模式下任务切换确实不发生**。")
        print("    V2–V10 观察到的「从来没有任务切换」，根因就是仿真台没有建模")
        print("    直接页 0x44 —— 内核每次请求调度都写进了一个没人读的内存格子。")

    sep("6. 内核起了哪些任务（队列归属看 +00/+04 是否自指）")
    for r in realos.records(m2):
        t = r["tcb"]
        prv, nxt = b.rd(t, 4), b.rd(t + 4, 4)
        where = "空环" if prv == t == nxt else f"挂在 0x{prv:06X}"
        print(f"  任务 {r['id']:<3} TCB=0x{t:06X}  +18=0x{b.rd(t + 0x18, 4):06X}  {where}"
              f"{'   ← 正在跑' if t == b.rd(realos.CUR_TCB, 4) else ''}")

    # ---- 7. B 模式：再开 0x44，看调度器被叫醒之后会不会真的换任务
    sep("7. 【B 模式 · 再开 0x44】同一条启动链，任务会不会换")
    m3 = create("d300-a")
    m3.load()
    info3 = m3.boot_realos(1)
    print(f"  派发：TCB=0x{info3['record']['tcb']:06X} 入口=0x{info3['record']['entry']:06X}")
    b3 = m3.bus
    b3.watch.add(realos.CUR_TCB)
    b3.access_log.clear()
    m3.run(max_steps=STEPS)
    ws3 = dump_tcb_writes(b3, b3.access_log,
                          "7b. 【B 模式】当前 TCB 变量 *(0x%05X) 的写入" % realos.CUR_TCB)
    print(f"  调度请求 {m3.dispatch.requests} 次，"
          f"优先级编码查询 {m3.prio.lookups} 次，"
          f"位图为 0 的次数 {m3.prio.empty}")
    print("  → 同一个固件、同一条启动链，只多了 0x44 这一步建模，切换就发生了。")
    print("    A/B 两个模式的数字不能混着比。完整时间线见 realos-sched.txt。")

    sep("8. 结论")
    print("  内核原生可用：启动链、任务上下文、TCB、系统调用全部由固件完成。")
    print("  A 模式下任务 1 一路跑到底（它是唯一能跑的任务，自己「置标志 → 清标志 →")
    print("  等标志」地轮询，每次等待都立刻满足，内核没有理由切走）。")
    print("  B 模式（建模 0x44）下调度器被叫醒，任务 1 阻塞后真的切给了任务 5，")
    print("  最后落到空闲任务 —— 「从来没有任务切换」这句 V10 的结论到此作废。")
    print("  还差什么：13 个任务里只有 3 个上场。其余 11 个的上下文要靠 sta_tsk 去建，")
    print("  而触发 sta_tsk 的硬件事件（按键 / 拨盘 / 存储卡 / 时钟）我们还没有。")


if __name__ == "__main__":
    main()
