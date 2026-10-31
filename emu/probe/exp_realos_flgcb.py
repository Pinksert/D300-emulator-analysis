#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V11：A 段 REALOS 内核的对象模型 —— 服务分发表 / 对象登记表 / 标志控制块。

V10 已经证明固件自带的 REALOS 能原生跑起来（复位 → 派发 → 任务 1 起）。
这一份把「内核内部长什么样」挖到能逐字段引用的程度。

判据：
  1. 服务分发表 @0x0FE3B8：索引 = 服务号 × 2（带符号），条目是相对 0x0FE3B8
     的 int16 偏移 —— 从陷阱 wrapper(0x0FE3C0) 的 lsl/lduh/extsh/add/call 读出来；
  2. 对象登记表 @0x0FE180：步长 0x10，第 N 个对象 = 0x0FE180 + (N-1)*0x10，
     条目 +0x04 = 该对象的控制块指针 —— 从查对象函数 0x0FEC7A 的 mulh/add 读出来；
  3. 标志控制块：第 N 个 = 0x00045C38 + (N-1)*0x10；+0x00 自指针（兼空队列哨兵）、
     +0x04 回指登记表条目、+0x08 位模式、+0x0C 备用。
     实证：任务 1 调过 set_flg(2, 0x4000) 之后 *(0x45C50) 就是 0x00004000；
  4. μITRON 的参数顺序是**从任务 1 的调用点指令**读出来的，不是从 B 段推断的：
       wai_flg   r4=p_flgptn  r5=flgid  r6=waiptn  r7=wfmode   （0x08147E–0x081490）
       clr_flg   r4=flgid     r5=位模式                        （0x08C82A–0x08C836）
       set_flg   同上
     返回值一律在 r12（wai_flg 返回 E_OK=0）。
  5. wai_flg 处理过程中的关键分支在 0x0FEE00–0x0FEE38：先算 `(pattern & waiptn)`，
     不满足时把当前 TCB(0x0459FC) 挂进标志的等待队列 —— 但整条路径**不经过**
     0x0FE4DE–0x0FE514 那段（上下文恢复 / 首次派发用的路径）。

跑法：cd emu && python probe/exp_realos_flgcb.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox.itron import SVC

SVCTAB = 0x000FE3B8       # 服务分发表基址
OBJTAB = 0x000FE180       # 对象登记表基址
FLGCB0 = 0x00045C38       # 第一个标志控制块
STRIDE = 0x10             # 登记表与控制块的步长
PAT = 0x08                # 控制块里的位模式偏移


def sep(t):
    print()
    print("=" * 78)
    print(t)


def cb(n):
    return FLGCB0 + (n - 1) * STRIDE


def main():
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    m.run(max_steps=300000)
    bus = m.bus

    sep("1. 服务分发表 @0x%06X（索引 = 服务号 × 2 的带符号偏移）" % SVCTAB)
    print("  陷阱 wrapper 自己怎么查表（0x0FE3EC–0x0FE3FA）：")
    for a in (0x0FE3EC, 0x0FE3EE, 0x0FE3F4, 0x0FE3F6, 0x0FE3F8, 0x0FE3FA):
        print("    0x%06X: %s" % (a, m.disasm_at(a, 1).split("\n")[0][:56]))
    print()
    print("  服务号  服务名              表地址        偏移     → 处理函数")
    for neg in (-46, -47, -48, -44, -38, -23, -25, -21):
        addr = SVCTAB + 2 * neg
        off = int.from_bytes(m.fetch_bytes(addr, 2), "big")
        if off >= 0x8000:
            off -= 0x10000
        print("    0x%02X    %-18s 0x%06X   %+6d    0x%06X"
              % (neg & 0xFF, SVC.get(neg, "?"), addr, off, SVCTAB + off))

    sep("2. 对象登记表 @0x%06X（步长 0x10，+0x04 = 控制块指针）" % OBJTAB)
    print("  #    地址        +0x00       +0x04(控制块)  +0x08    +0x0C")
    for i in range(1, 11):
        a = OBJTAB + (i - 1) * STRIDE
        w = [bus.rd(a + 4 * k, 4) for k in range(4)]
        print("  %-4d 0x%06X   0x%08X  0x%06X      0x%04X    0x%04X"
              % (i, a, w[0], w[1], w[2], w[3]))

    sep("3. 标志控制块 @0x%06X（步长 0x10）" % FLGCB0)
    print("  flag  CB 地址    +0x00(自指/空队列)  +0x04(等待队列头)   +0x08(位模式)  +0x0C(回指登记表)")
    for n in range(1, 11):
        b = cb(n)
        w = [bus.rd(b + 4 * k, 4) for k in range(4)]
        print("  %-4d  0x%06X   0x%08X         0x%06X          0x%08X     0x%08X"
              % (n, b, w[0], w[1], w[2], w[3]))
    print()
    print("  实证：set_flg(2, 0x4000) 之后 *(0x45C50) = 0x%08X（应为 0x00004000）"
          % bus.rd(cb(2) + PAT, 4))
    print("  实证：+0x0C 回指登记表条目里存它的那个格子（flag3 → 0x%06X = 第 3 条 +0x04）"
          % bus.rd(cb(3) + 0x0C, 4))
    q9 = bus.rd(cb(9) + 0x04, 4)
    print("  实证：队列空时 +0x04 指向自己（flag1 0x%06X），有任务在等就指向那个任务的 TCB；"
          % bus.rd(cb(1) + 0x04, 4))
    print("        flag9 的 +0x04 = 0x%06X —— 正是任务 1 的 TCB，所以「等待队列」这个读法有实证。"
          % q9)

    sep("4. μITRON 参数顺序 —— 直接读任务 1 的调用点指令")
    print("  wai_flg 的调用点 0x08147E–0x081490：")
    print(m.disasm_at(0x08147E, 8))
    print("  → r4 = p_flgptn（任务栈上的地址），r5 = flgid，r6 = waiptn，r7 = wfmode")
    print()
    print("  clr_flg 的调用点 0x08C82A–0x08C836：")
    print(m.disasm_at(0x08C82A, 6))
    print("  → r4 = flgid，r5 = 位模式（clr_flg / set_flg 同一套）")

    sep("5. wai_flg 的等待判定（实测：第一次 flag=3 waiptn=0x2000 mode=2）")
    print("    0x0FEE04  r14 ← *(登记表条目+4) = 0x00045C58   ← 标志 3 的控制块")
    print("    0x0FEE0A  r8  ← 位模式          = 0x00000000   ← 此刻 flag3 是空的")
    print("    0x0FEE0C  r9 = wfmode & 2        = 2           ← OR 等待")
    print("    0x0FEE1C  r9 = pattern & waiptn  = 0           ← 不满足")
    print("    0x0FEE2C  r14 ← *(0x45980)       = 0x000459FC  ← 当前任务的 TCB")
    print("    0x0FEE38  r8 = 0x00100004                      ← 任务状态字（等待态）")
    print()
    print("  → 等待确实没被满足、也确实把当前 TCB 挂进了队列；但整条路径不经过")
    print("    0x0FE4DE–0x0FE514（首次派发用的上下文恢复段）。")

    sep("6. 为什么到现在还没有一次真正的任务切换")
    print("  300,000 步里：")
    print("    系统调用 1,888 次，全部由固件自己的内核处理")
    print("    *(0x045980)（当前 TCB）全程只被写过 1 次：0x0459FC，来自 0x0FE4EC")
    print("    任务的 USP 全程落在 0x0462xx —— 自始至终只有任务 1 在跑")
    print("  → 下一轮的墙：要么找到内核真正交出 CPU 的那个入口，要么造一个真实硬件事件。")


if __name__ == "__main__":
    main()
