# -*- coding: utf-8 -*-
"""V10：REALOS 的 TCB 布局与任务上下文（全部读固件自己写出来的内存）。

判据：
  1. 13 条任务表记录指向 13 个 TCB，相邻差 0x2C（= TCB 大小）；
  2. TCB+0x0C 的高 16 位 == 表里的属性值（0x04 → 0x00040000）；
  3. TCB+0x18 == 表里的栈顶；TCB+0x24 == 自己那条表记录的地址；
  4. TCB+0x00 / +0x04 是双向链表的前驱后继：自指 = 不在任何队列里；
  5. 已建上下文的三个任务（1 / 5 / 0）栈顶前两个字 = [entry, PS=0x001F0030]，
     第三个字统一是 0x0FE250（内核给任务准备的「返回落地处」，里面是 jmp 0x81338）。

跑法：cd emu && python probe/exp_realos_tcb.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import realos


def sep(t=""):
    print()
    print("=" * 78)
    if t:
        print(t)


def main():
    # 取「刚派发完」的现场：这时内核刚建好三份初始上下文，任务 1 还没跑起来，
    # 栈顶那几个字还是内核写进去的原值（跑起来之后栈顶就是活的现场了）。
    m0 = create("d300-a")
    m0.load()
    m0.boot_realos(1)
    rows = realos.records(m0)

    sep("0. 刚派发完的现场（任务 1 还没跑）")
    for tid in (1, 5, 0):
        r = realos.find(m0, tid)
        w = [int.from_bytes(m0.fetch_bytes(r["sp"] + 4 * i, 4), "big") for i in range(3)]
        print(f"  任务 {tid}: 栈顶 0x{r['sp']:06X} = [0x{w[0]:06X}, 0x{w[1]:08X}, 0x{w[2]:06X}]"
              f"   首字 == entry ？ {w[0] == r['entry']}")
    print("  第三字统一 0x0FE250 —— 内核给任务准备的「返回落地处」：")
    print("    " + m0.disasm_at(0x0FE250, 2).replace("\n", "\n    "))
    print("  → 任务返回就落到 0x081338（固件自带的 `bra -1` 停机占位）")

    m = create("d300-a")
    m.load()
    print("\n再开一台：任务 1 接下来后跑 30 万步（让内核把能起的任务都起一遍）")
    m.boot_realos(1)
    m.run(max_steps=300000)
    rows = realos.records(m)

    sep("1. 13 个 TCB，步长 0x2C")
    for r in rows:
        print(f"  任务 {r['id']:<3} TCB=0x{r['tcb']:06X}"
              f"（表记录 0x{r['addr']:06X}）")

    sep("2. TCB 字段（0x2C 字节）")
    print("  任务 TCB      +00 前驱   +04 后继   +0C 属性<<16|状态  +18 保存SP  +24 回指表记录")
    for r in rows:
        t = r["tcb"]
        prev, nxt = m.bus.rd(t, 4), m.bus.rd(t + 4, 4)
        attr_state = m.bus.rd(t + 0x0C, 4)
        sp = m.bus.rd(t + 0x18, 4)
        back = m.bus.rd(t + 0x24, 4)
        print(f"  {r['id']:<3}  0x{t:06X} 0x{prev:06X} 0x{nxt:06X}  "
              f"0x{attr_state:08X}      0x{sp:06X}   0x{back:06X}  "
              f"{'OK' if back == r['addr'] else '≠表记录'}")

    sep("3. 三条一致性判据")
    ok1 = all(m.bus.rd(r["tcb"] + 0x0C, 4) >> 16 == r["attr"] for r in rows)
    ok2 = all(m.bus.rd(r["tcb"] + 0x18, 4) == r["sp"] for r in rows)
    ok3 = all(m.bus.rd(r["tcb"] + 0x24, 4) == r["addr"] for r in rows)
    print(f"  +0x0C 高 16 位 == 表里的属性     {ok1}")
    print(f"  +0x24 == 自己的表记录地址        {ok3}")
    print(f"  +0x18 == 表里的栈顶              {ok2}"
          f"{'' if ok2 else '  ← 任务 1 正在跑，它的 +0x18 已经是活的现场，不再是初始栈顶'}")
    print("  → 任务表和 TCB 是互指的两套账，任一侧读错都能立刻发现")

    sep("4. 队列归属：+00/+04 自指 = 不在任何队列里")
    for r in rows:
        t = r["tcb"]
        prev, nxt = m.bus.rd(t, 4), m.bus.rd(t + 4, 4)
        if prev == t == nxt:
            where = "空环（不在队列里）"
        else:
            where = f"挂在 0x{prev:06X} 这个对象上"
        st = m.bus.rd(t + 0x0C, 4) & 0xFFFF
        print(f"  任务 {r['id']:<3} 状态字 0x{st:04X}  {where}")

    sep("5. 跑过 30 万步之后，阻塞着的任务还留着初始上下文")
    print("  任务 栈顶      首字 PC    第二字 PS    第三字")
    for tid in (1, 5, 0):
        r = realos.find(m, tid)
        w = [int.from_bytes(m.fetch_bytes(r["sp"] + 4 * i, 4), "big") for i in range(3)]
        print(f"  {tid:<3}  0x{r['sp']:06X}  0x{w[0]:06X}   0x{w[1]:08X}   0x{w[2]:06X}"
              f"   {'首字仍 == entry' if w[0] == r['entry'] else '（栈顶已是活的现场）'}")

    sep("6. 跑完 30 万步之后，当前 TCB 是谁")
    cur = m.bus.rd(realos.CUR_TCB, 4)
    r1 = realos.find(m, 1)
    who = "?"
    for _r in realos.records(m):
        if _r["tcb"] == cur:
            who = "任务 %d" % _r["id"]
    print(f"  *(0x{realos.CUR_TCB:05X}) = 0x{cur:06X}（{who}）"
          f"{'  == 任务 1 的 TCB' if cur == r1['tcb'] else '  ≠ 任务 1'}")
    print("  → 开着调度建模（直接页 0x44）跑，当前 TCB 早就不是任务 1 了：")
    print("    任务 1 阻塞后调度器切给了优先级更高的任务，最后落到空闲任务。")
    print("    这正是 V11 推翻「从来没有任务切换」的那条证据（见 realos-sched.txt）。")
    print(f"  任务 1 的 TCB+0x18 里存着当前 SP = 0x{m.bus.rd(r1['tcb'] + 0x18, 4):06X}")
    print(f"  （任务在跑的时候这个字段是它的现场；不在跑的时候是内核让出时的现场）")


if __name__ == "__main__":
    main()
