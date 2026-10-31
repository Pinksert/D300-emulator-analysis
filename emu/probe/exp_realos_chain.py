# -*- coding: utf-8 -*-
"""V10：A 段复位 → REALOS 首次派发 的完整链条，以及任务表 13 条记录。

判据：
  1. 从复位到派发点 0x0FE4E6 的步数每次一致（可复现）；
  2. 表头 *(0xFE000) = 8 是**用户任务数**，记录实际 13 条（1..12 + 空闲 0）；
  3. 内核为任务 1 / 5 / 0 写好的上下文首字 == 表里的 entry，第二字 == 表里的 sp；
  4. `*(0x3FBB0)` 在派发前被置 2（进入调度循环的标记）；
  5. 任务 0（空闲）入口 0x0FE510 是一条 `bra -1`。

跑法：cd emu && python probe/exp_realos_chain.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import realos

STAGE = 0x3FBB0


def sep(t=""):
    print()
    print("=" * 78)
    if t:
        print(t)


def main():
    m = create("d300-a")
    m.load()

    sep("1. 复位现场")
    c = m.cpu
    print(f"  复位 PC     = 0x{c.pc:06X}")
    print(f"  TBR(dr0)    = 0x{c.dr[0]:06X}   vec 0x40 槽 → 0x{c.vector_addr(0x40):06X}")
    print(f"  SSP(dr2)    = 0x{c.dr[2]:06X}   USP(dr3) = 0x{c.dr[3]:06X}")
    print(f"  r15         = 0x{c.r[15]:06X}（复位时是 SSP；第一条指令 ldi:32 会换成 0x4616C）")
    print(f"  *(0x{STAGE:05X}) = {m.bus.rd(STAGE, 4)}（还没进调度）")

    sep("2. 跑到 REALOS 首次派发点 0x%06X" % realos.DISPATCH_LD)
    m.run(max_steps=600000, stop_pcs={realos.DISPATCH_LD}, spin_guard=False)
    print(f"  步数        = {m.step_count:,}")
    print(f"  PC          = 0x{c.pc:06X}   （ld @(r14,0x18),r0 —— 取任务的保存 SP）")
    print(f"  r14         = 0x{c.r[14]:06X}（内核默认挑的是空闲任务）")
    print(f"  *(0x{STAGE:05X}) = {m.bus.rd(STAGE, 4)}（2 = 已进调度循环）")
    print(f"  当前 TCB    = 0x{m.bus.rd(realos.CUR_TCB, 4):06X}"
          f"（还没写回：写回在 0x0FE4EC，比派发点晚一条指令）")

    sep("3. 任务表 13 条记录")
    n_user = m.bus.rd(realos.TASK_TABLE, 4)
    rows = realos.records(m)
    print(f"  头部 *(0x{realos.TASK_TABLE:06X}) = {n_user}  ← 用户任务数，不是记录总数")
    print(f"  记录 {len(rows)} 条 × 0x{realos.TASK_REC:X} 字节，从 0x{realos.TASK_REC0:06X} 起")
    print()
    print("  id  属性  用户  TCB      入口       栈顶       状态     上下文首字")
    for r in rows:
        ctx = realos.context(m, r["sp"]) if r["sp"] else {"pc": 0}
        print("  %-3d 0x%02X  %-4s 0x%06X 0x%06X  0x%06X  %-8s 0x%06X%s"
              % (r["id"], r["attr"], "是" if r["user"] else "-", r["tcb"],
                 r["entry"], r["sp"], "已激活" if r["active"] else "未启动",
                 ctx["pc"], "  ←=entry" if ctx["pc"] == r["entry"] else ""))

    sep("4. 内核自己建好的上下文（不是我们摆的）")
    for tid in (1, 5, 0):
        r = realos.find(m, tid)
        d = m.fetch_bytes(r["sp"], 12)
        words = [int.from_bytes(d[i:i + 4], "big") for i in (0, 4, 8)]
        sp = m.bus.rd(r["tcb"] + realos.TCB_SP, 4)
        print(f"  任务 {tid}: 栈顶 0x{r['sp']:06X} = [0x{words[0]:06X}, 0x{words[1]:08X}, 0x{words[2]:06X}]")
        print(f"          首字 == 表里的 entry ？ {words[0] == r['entry']}"
              f"    TCB+0x18 保存的 SP == 表里的 sp ？ {sp == r['sp']}")

    sep("5. 空闲任务的入口")
    idle = realos.find(m, 0)
    print(f"  空闲任务 id 0：TCB=0x{idle['tcb']:06X} 入口=0x{idle['entry']:06X} 栈顶=0x{idle['sp']:06X}")
    print(f"  入口处 2 字节 = {m.fetch_bytes(idle['entry'], 2).hex(' ').upper()}"
          f"  ← 0x{int.from_bytes(m.fetch_bytes(idle['entry'], 2), 'big'):04X} 是 `bra -1`（原地自旋）")
    print("  → 空闲任务 = `bra -1`，所以「切到空闲」在 PC 上看不出来，要看 TCB 变量")

    sep("6. 派发之后：任务 1 进入应用区")
    m.cpu.r[14] = realos.find(m, 1)["tcb"]
    m.run(max_steps=2000, stop_pcs={0x0008133A}, spin_guard=False)
    print(f"  PC = 0x{m.cpu.pc:06X}  （== 任务 1 入口 0x0008133A ？ {m.cpu.pc == 0x0008133A}）")
    print(f"  PS = 0x{m.cpu.ps:08X}  S 位 = {(m.cpu.ps >> 5) & 1}（0 = 用 USP，即任务栈）")
    print(f"  r15 = 0x{m.cpu.r[15]:06X}（任务 1 栈顶 0x000462EC 往下长）")
    print(f"  当前 TCB = 0x{m.bus.rd(realos.CUR_TCB, 4):06X}")


if __name__ == "__main__":
    main()
