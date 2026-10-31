#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V11：固件自己的调度器第一次真的换起任务来。

V10 结束时还挂着一句话：「至今没有发生过一次真正的任务切换」。
这一轮把挡在路上的三样东西补齐，切换就发生了：

  1. 直接页 0x44 是「请求调度」寄存器（只写，固件从不读它）
        → 写 1 就挂一条 vec 0x3F 的中断，级别取自固件自己写进 ICR 的值
  2. vec 0x3F → 0x000FE776 就是调度器本身
  3. 0x3F4/0x3FC 是硬件优先级编码器
        → 写就绪位图进 0x3F4，从 0x3FC 读回「最高优先级号」（= 前导零个数）
  4. ILM 的优先级约定是「号越小越优先，受理条件 level < ILM」

本脚本把这条链从头到尾走一遍并落盘。跑法：

    cd emu && python probe/exp_realos_sched.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402
from frbox.itron import SVC                 # noqa: E402

STEPS = 1000000


def sep(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


def main():
    m = create("d300-a")
    m.load()
    recs = realos.records(m)
    name = {r["tcb"]: ("任务 %d" % r["id"] if r["id"] else "空闲任务")
            for r in recs}
    entry = {r["tcb"]: r["entry"] for r in recs}

    # ---------------------------------------------------------------- 1
    sep("1. 内核的三张表：就绪位图 / 就绪队列 / 派发请求")
    print("  0x00045980  当前 TCB（调度器 0x0FE798 处读它）")
    print("  0x00045984  就绪位图，置位规律是 bit(31 − 优先级)")
    print("  0x00045988  就绪队列数组，13 条 × 8 字节（next, prev），空队列自指")
    print("  0x000459F0  派发请求字节（调度器用 xchb 读走并清零）")
    print("  0x0003F4/0x3FC  硬件优先级编码器：写位图 → 读优先级号")
    print("  直接页 0x44  「请求调度」寄存器：写 1 → 挂 vec 0x3F")

    m.boot_realos(1)
    print("\n  跑到派发点之后 0x00045980 起 0x78 字节：")
    for i in range(0, 0x78, 16):
        a = 0x45980 + i
        print("    0x%05X: %s" % (a, m.fetch_bytes(a, 16).hex(' ')))

    print("\n  就绪队列数组逐条（下标 = 优先级）：")
    for p in range(13):
        a = 0x45988 + p * 8
        nxt = m.bus.rd(a, 4)
        who = name.get(nxt, "—")
        if nxt == a:
            who = "空（自环）"
        print("    优先级 %-2d  队首 0x%06X  %s" % (p, nxt, who))
    bm = m.bus.rd(0x45984, 4)
    print("  就绪位图 *(0x45984) = 0x%08X" % bm)
    print("  位图里置位的优先级：%s"
          % ", ".join(str(31 - b) for b in range(32) if bm >> b & 1))

    # ---------------------------------------------------------------- 2
    sep("2. 优先级编码器：写什么、读什么、读完怎么用")
    print("  调度器 0x0FE830 那几条：")
    print("    0x0FE836  ld @r12, r13        ; r13 = 就绪位图")
    print("    0x0FE838  ldi:20 #0x3F4, r14")
    print("    0x0FE83C  st r13, @(r14,0)    ; W 0x003F4 ← 位图")
    print("    0x0FE83E  ld @(r14,8), r13    ; R 0x003FC → 结果")
    print("    0x0FE842  lsl #3, r13         ; × 8 = 队列节点大小")
    print("    0x0FE844  ld @(r12,r13), r13  ; 取该优先级的队首 TCB")
    print("\n  实测（把位图直接写进去读回）：")
    for v in (0x50800000, 0x80000000, 0x00000001, 0x00008000, 0x00000000):
        m.bus.wr(0x3F4, 4, v)
        r = m.bus.rd(0x3FC, 4)
        print("    写 0x%08X → 读回 %-3d（前导零 = %d）"
              % (v, r, 32 - v.bit_length() if v else 32))

    # ---------------------------------------------------------------- 3
    sep("3. ILM 的优先级约定：号越小越优先，受理条件 level < ILM")
    print("")
    print("  这里刻意只开 xfer、不开 0x44：不开调度建模时任务 1 会一直待在")
    print("  应用区，应用/内核两侧的 ILM 才分得开。")
    from collections import Counter
    m3 = create("d300-a")
    m3.load()
    m3.enable_xfer()
    realos.prepare(m3, 1, max_steps=600000)
    j = Counter()
    for _ in range(200000):
        if m3.cpu.halted or m3.fault:
            break
        m3.step(1)
        j[(m3.cpu.ilm, 0x0FE000 <= m3.cpu.pc < 0x100000)] += 1
    for (ilm, ink), c in sorted(j.items(), key=lambda x: -x[1]):
        print("    ILM=0x%02X  %s  %8d 步" % (ilm, "内核" if ink else "应用", c))
    print("\n  vec 0x3F 的 ICR 级别 = 0x%02X（调度器）"
          % m.irq_level(0x3F))
    print("  串口 vec 0x1B 的 ICR 级别 = 0x%02X" % m.irq_level(0x1B))
    print("  → 调度器是启用向量里级别号最大的（优先级最低），好让外设 ISR 抢占它")
    print("  → 内核 stilm #0x1E 恰好只挡调度器这一级，派发因此推迟到 reti 之后")

    # ---------------------------------------------------------------- 4
    sep("4. 跑 %s 步：任务真的换起来" % format(STEPS, ","))
    m2 = create("d300-a")
    m2.load()
    m2.boot_realos(1)
    name2 = {r["tcb"]: ("任务 %d" % r["id"] if r["id"] else "空闲任务")
             for r in realos.records(m2)}
    ent2 = {r["tcb"]: r["entry"] for r in realos.records(m2)}

    switches = []
    cur = [None]
    orig_wr = m2.bus.wr

    def wr(a, n, v, pc=0):
        if a == 0x45980 and v != cur[0]:
            cur[0] = v
            switches.append((m2.step_count, v, pc))
        return orig_wr(a, n, v, pc)
    m2.bus.wr = wr

    hw = Counter()
    svc = Counter()
    steps_in = Counter()
    orig_eit = m2.cpu.eit

    def eit(num, lvl=0):
        if num == 0x40:
            svc[m2.cpu.r[12] & 0xFF] += 1
        else:
            hw[num] += 1
        return orig_eit(num, lvl)
    m2.cpu.eit = eit

    for _ in range(STEPS):
        if m2.cpu.halted or m2.fault:
            break
        m2.step(1)
        if m2.cpu.pc >= 0x080000:
            steps_in[cur[0]] += 1

    print("  共 %s 步，最终 PC=0x%06X（0x0FE510 = 空闲任务的 bra -1）"
          % (format(m2.step_count, ","), m2.cpu.pc))
    print("\n  任务切换时间线（*(0x45980) 每次变化）：")
    for st, tcb, pc in switches:
        print("    step %9s  → %-8s TCB=0x%06X  入口 0x%06X   由 0x%06X 写入"
              % (format(st, ","), name2.get(tcb, "?"), tcb,
                 ent2.get(tcb, 0), pc))
    print("\n  各任务在应用区执行的步数：")
    for tcb, n in steps_in.most_common():
        print("    %-8s %9s 步" % (name2.get(tcb, "?"), format(n, ",")))
    print("\n  硬件中断受理：%s" % {"vec 0x%02X" % k: v for k, v in hw.items()})
    print("  调度请求 %d 次，优先级编码查询 %d 次，位图为 0 的次数 %d"
          % (m2.dispatch.requests, m2.prio.lookups, m2.prio.empty))
    print("\n  服务调用：")
    for c, n in svc.most_common():
        print("    %-16s %s" % (SVC.get((c & 0xFF) - 256, hex(c)), format(n, ",")))
    print("\n  注意：任务 1 与任务 5 之外没别的任务跑，是因为其余 11 个任务的")
    print("  上下文要靠 sta_tsk 去建，而这条路上触发 sta_tsk 的硬件事件我们还没有。")


if __name__ == "__main__":
    main()
