# -*- coding: utf-8 -*-
"""exp_mbx_live.py —— 任务 5 到底在等哪个信箱、等在什么地址上（V17 第 2 步）

静态已经给出两条硬结论（见 exp_msg_static.py 的输出）：

  * A 段全段 `rcv_msg` 只有 **1 处**：0x000A6324，就在任务 5 的 PC 0x0A6326 上一条；
  * `snd_msg` **15 处，R4 全是 1** —— 全固件只往一个信箱投消息。

本脚本跑真内核，抓任务 5 进 `int #0x40` 那一刻的实参，再看它等的控制块内存
长什么样。目标是把「信箱控制块地址」定下来 —— 有了地址就能绕开内核、
直接往队列里塞一条消息，让固件自己把任务 5 唤醒（V17 的正确做法：
不改内核代码，只注入数据）。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402

STEPS, BATCH = 200000, 5000


def base():
    m = create("d300-a")
    m.load()
    m.reset()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m


def spin(m, steps=STEPS):
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        if m.cpu.halted or m.fault:
            break


def dump(m, addr, n=8, title=""):
    if title:
        print("  %s @0x%05X" % (title, addr))
    for i in range(n):
        a = addr + i * 16
        row = [m.bus.rd(a + j, 1) for j in range(16)]
        print("    0x%05X  %s  |%s|"
              % (a, " ".join("%02X" % b for b in row),
                 "".join(chr(b) if 32 <= b < 127 else "." for b in row)))


def main():
    m = base()
    spin(m)
    print("=" * 78)
    print("跑 %d 步后（场景 A 口径）" % m.step_count)

    states = {t["id"]: t for t in realos.states(m)}
    print("\n任务状态")
    for tid in sorted(states):
        t = states[tid]
        print("  任务 %-2d st=0x%08X %-6s wai=0x%08X pat=0x%08X pc=0x%08X tcb=0x%05X"
              % (tid, t["wait_state"], t["wait_kind"], t["wait_flag"] or 0,
                 t["wait_pat"], t["pc"], t["tcb"]))

    # ---- 抓所有 rcv_msg / snd_msg
    print("\n" + "=" * 78)
    print("服务流水里的消息相关调用")
    msgs = [c for c in m.flow.calls if c[2] in (-61, -63, -60)]
    if not msgs:
        print("  无")
    for step, pc, svc, r4, r5, r6, r7, r13, tcb in msgs[:40]:
        print("  步 %-7d 0x%08X %-8s r4=0x%08X r5=0x%08X r6=0x%08X r7=0x%08X "
              "r13=0x%08X tcb=0x%05X"
              % (step, pc, {-61: "rcv_msg", -63: "snd_msg"}.get(svc, svc),
                 r4, r5, r6, r7, r13, tcb))
    print("  共 %d 条" % len(msgs))

    # ---- 任务 5 的等待现场
    t5 = states[5]
    print("\n" + "=" * 78)
    print("任务 5 的等待现场")
    print("  TCB 0x%05X  状态字 0x%08X  waiptn 0x%08X  SP 0x%05X"
          % (t5["tcb"], t5["state"], t5["wait_pat"], t5["sp"]))
    dump(m, t5["tcb"], 4, "TCB")
    dump(m, 0x03F040, 6, "回调槽 / 0x3F054 附近")

    # ---- r13 是栈帧，找 int 指令压栈的返回地址附近
    print("\n" + "=" * 78)
    print("任务 5 的栈（SP 附近，找 r4/r5 实参残留）")
    sp = t5["sp"]
    dump(m, (sp - 0x20) & 0xFFFFFFE0, 6, "SP-0x20")

    # ---- 扫描整个 RAM 找「消息指针 + 链接」形状的控制块
    print("\n" + "=" * 78)
    print("信箱控制块候选（0x3F000–0x46000 之间，含自指环的 8 字节对）")
    hits = 0
    for a in range(0x3F000, 0x46000, 4):
        x = m.bus.rd(a, 4)
        y = m.bus.rd(a + 4, 4)
        if x == a and y == a:
            print("    0x%05X  自指环（空队列）" % a)
            hits += 1
            if hits > 40:
                print("    …")
                break
    print("  共 %d 处" % hits)


if __name__ == "__main__":
    main()
