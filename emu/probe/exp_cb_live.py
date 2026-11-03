# -*- coding: utf-8 -*-
"""exp_cb_live.py —— 消息里的回调指针，到底有没有人调用（V18 第 1 步）

V17 的静态结论：消息 `+0x10` 是一个代码指针（固件自己那条是 0x0008225E），
任务 5 把它整条抄到 0x3FE48 落地缓冲。也留了一条边界：「那个回调由谁在何时调用」
没查。

本脚本只问三个能动态回答的问题：

  1. 0x0008225E 这段代码，**基线跑完有没有被执行过**（覆盖率直接查）；
  2. 0x3FE48+0x10 这个槽，跑起来后有没有被**读**过、被谁读（总线 watchpoint）；
  3. 投一条消息之后，这两件事的答案会不会变。

不看语义、不猜格式 —— 只回答「被访问了没有」。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402

CB_SAMPLE = 0x0008225E          # 固件自己那条消息里的回调指针
BUF = 0x0003FE48                # 任务 5 落地缓冲
SLOT = BUF + 0x10               # 回调槽
WATCH = [BUF + i for i in range(0, 0x20, 2)] + [SLOT + i for i in range(4)]
BATCH = 4096


def warm(m, rounds=40, batch=5000):
    """跑到任务 5 停在 rcv_msg 上（与 V17 同一套喂法）。"""
    for _ in range(rounds):
        m.run(max_steps=batch, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)


def covered(m, addr):
    if getattr(m, "cov", None) is None:
        return None
    return m.cov.visited(addr)


def dump_slot(m, title):
    w = [m.bus.rd(BUF + i * 4, 4) for i in range(8)]
    print("  %s: %s" % (title, " ".join("%08X" % x for x in w)))


def main():
    m = create("d300-a")
    m.load()
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    mb = m.enable_msgs()

    print("=" * 78)
    print("阶段 1：基线（不投递）跑 20 万步")
    # 先看「还没跑」时回调那段的状态
    print("  开机时 0x%05X 覆盖：%s" % (CB_SAMPLE, covered(m, CB_SAMPLE)))
    for a in WATCH:
        m.bus.watch.add(a)
    m.bus.access_log.clear()

    warm(m, rounds=40, batch=5000)
    print("  预热后步数 %d，服务 %d" % (m.step_count, len(m.flow.calls)))
    print("  回调 0x%05X 覆盖：%s" % (CB_SAMPLE, covered(m, CB_SAMPLE)))
    seg = [CB_SAMPLE + i for i in range(0, 0x40, 2)]
    hit = [a for a in seg if covered(m, a)]
    print("  回调段 0x%05X–0x%05X 共 32 个地址，命中 %d 个"
          % (seg[0], seg[-1], len(hit)))
    if hit:
        print("    命中：%s" % " ".join("0x%05X" % a for a in hit))

    print("\n  落地缓冲 0x%05X 的访问记录：" % BUF)
    if not m.bus.access_log:
        print("    （无）")
    for kind, a, n, v, pc in m.bus.access_log[:24]:
        print("    %s 0x%05X size=%d val=0x%08X pc=0x%08X"
              % (kind, a, n, v, pc))
    if len(m.bus.access_log) > 24:
        print("    … 共 %d 条" % len(m.bus.access_log))
    dump_slot(m, "落地缓冲内容")

    # 投一条
    print("\n" + "=" * 78)
    print("阶段 2：投一条消息，看访问记录与覆盖")
    t5 = mb.task5_state()
    print("  投前任务 5：%s pc=0x%08X" % (t5["kind"], t5["pc"]))
    m.bus.access_log.clear()
    cov_before = m.cov.words()
    r = mb.send()
    print("  send -> err=%s steps=%s" % (r.get("err"), r.get("steps")))
    warm(m, rounds=8, batch=5000)
    print("  投后任务 5：%s" % (mb.task5_state(),))
    print("  执行地址 %d → %d（+%d）" % (cov_before, m.cov.words(),
                                        m.cov.words() - cov_before))
    print("  回调 0x%05X 覆盖：%s" % (CB_SAMPLE, covered(m, CB_SAMPLE)))

    print("\n  落地缓冲 0x%05X 的访问记录（投递后）：" % BUF)
    for kind, a, n, v, pc in m.bus.access_log[:40]:
        print("    %s 0x%05X size=%d val=0x%08X pc=0x%08X"
              % (kind, a, n, v, pc))
    if len(m.bus.access_log) > 40:
        print("    … 共 %d 条" % len(m.bus.access_log))
    dump_slot(m, "落地缓冲内容")

    # 全段扫：有没有「读 +0x10 再 call」的形状
    print("\n" + "=" * 78)
    print("阶段 3：0x3FE48 的四个静态引用点在做什么")
    for a in (0x0A6304, 0x0A6440, 0x0A65AE, 0x0A6688):
        print("  0x%05X 覆盖：%s" % (a, covered(m, a)))


if __name__ == "__main__":
    main()
