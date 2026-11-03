# -*- coding: utf-8 -*-
"""exp_msg_frame.py —— 抓一条「固件自己造的合法消息」（V17 第 5 步）

矩阵实验（exp_msg_matrix.py）给了一个否定结果：投递成功、`rcv_msg` 也确实被调用
4 次，但消息内容换成 0x0000 / 0x0008 / 0xFFFF **轨迹完全一样**（Δ地址 37、
新增调用点 0 个）。说明任务 5 并不按消息体首字分派 —— 至少不按我瞎编的那种格式。

那就不编了：**任务 1 在步 44433 自己发过一条消息**（`snd_msg(1, 0x417DC)`），
它是固件自己构造的、格式一定合法。本脚本停在那一刻，把这条消息的字节抄下来，
再把「抄下来的样本」投回去 —— 用固件自己的数据格式，而不是猜的。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402

BATCH = 1024
TARGET_SND = 0x0008179C          # 任务 1 那处 snd_msg 的 int 地址


def base():
    m = create("d300-a")
    m.load()
    m.reset()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m


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
    # 跑到任务 1 那次 snd_msg 为止
    hit = False
    for _ in range(600):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        if any(c[2] == -63 for c in m.flow.calls):
            hit = True
            break
    print("=" * 78)
    print("到达 snd_msg 时步数 %d（命中 %s）" % (m.step_count, hit))
    snd = [c for c in m.flow.calls if c[2] == -63]
    for step, pc, svc, r4, r5, r6, r7, r13, tcb in snd:
        print("  snd_msg pc=0x%08X r4=0x%X r5=0x%05X r6=0x%X r7=0x%X tcb=0x%05X"
              % (pc, r4, r5, r6, r7, tcb))
    if not snd:
        print("  没抓到 —— 本机路径与预期不同")
        return

    msg = snd[0][4]
    print("\n消息体 0x%05X（发出去那一刻的内容）：" % msg)
    dump(m, msg & 0xFFFFFFF0, 6)
    print("\n  按 16 位字读：")
    ws = [m.bus.rd(msg + i * 2, 2) for i in range(16)]
    print("   " + " ".join("%04X" % w for w in ws))
    print("\n  按 32 位字读：")
    dw = [m.bus.rd(msg + i * 4, 4) for i in range(8)]
    print("   " + " ".join("%08X" % w for w in dw))

    # 消息前 16 字节里有没有指针？（值落在 RAM 区间就标出来）
    print("\n  前 8 个 32 位字里像指针的：")
    for i, w in enumerate(dw):
        if 0x3F000 <= w < 0x47000 or 0x80000 <= w < 0x100000:
            print("    +0x%02X = 0x%08X  ← 指针" % (i * 4, w))
            dump(m, w & 0xFFFFFFF0, 1, "指过去")

    # 信箱控制块此时的样子
    print()
    dump(m, 0x000466E4, 4, "信箱 0x466E4（此时应有 1 条在队列里）")

    # 消息队列长度：沿 +0x00 链走
    print("\n  队列链表（mbx+0x00 起）")
    p = m.bus.rd(0x466E4, 4)
    for i in range(6):
        if not (0x3F000 <= p < 0x47000):
            print("    第 %d 个 0x%08X（不是 RAM 指针，链到此为止）" % (i, p))
            break
        print("    第 %d 个 0x%08X" % (i, p))
        p = m.bus.rd(p, 4)


if __name__ == "__main__":
    main()
