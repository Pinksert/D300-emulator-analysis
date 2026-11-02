# -*- coding: utf-8 -*-
"""exp_ready_queue.py —— 「ready 却一次都没跑」的任务，到底在不在就绪队列里（V15）

覆盖率发现任务 4 的状态字是 `0x00`（就绪）、PC 停在入口，可入口那条指令**一次都没执行过**。
`sta_tsk(4)` 也确实发生了（调用点 0x08C7F4）。于是有三种可能：

  A. 根本没进就绪队列（`sta_tsk` 成功了但入队失败）；
  B. 进了队列，但优先级位图没置（调度器看不见它）；
  C. 进了队列、位图也置了，只是永远轮不到（更高优先级的任务从不让出）。

本探针把三处内部结构全部倒出来对照：
  就绪位图 `0x45984` / 就绪队列 `0x45988 + 优先级×8`（head,tail）/ 每个 TCB 的 `+0x00,+0x04`
"""

import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create       # noqa: E402
from frbox.flow import Flow            # noqa: E402
from frbox import realos               # noqa: E402

READY_BITMAP = 0x45984
READY_QUEUE = 0x45988
STEPS, BATCH = 150000, 5000


def u32(m, a):
    d = m.fetch_bytes(a, 4)
    return int.from_bytes(d, "big") if len(d) == 4 else 0


def main():
    m = create("d300-a")
    m.load()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    fl = Flow(m)
    for _ in range(STEPS // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        for v in (0x13, 0x19):
            m.raise_irq(v)

    st = {t["id"]: t for t in realos.states(m)}
    tcb2task = {t["tcb"]: t["id"] for t in st.values() if t["tcb"]}

    print("步数 %d，服务调用 %d，当前任务 %d"
          % (m.step_count, len(fl.calls),
             tcb2task.get(u32(m, realos.CUR_TCB), -1)))

    bitmap = u32(m, READY_BITMAP)
    print("\n就绪位图 0x%05X = 0x%08X → 置位的优先级 %s"
          % (READY_BITMAP, bitmap,
             [31 - i for i in range(32) if bitmap >> i & 1] or "无"))

    print("\n就绪队列 0x%05X 起，13 条 × 8B（V11 结论：13×8B = 104B，正好到派发请求 0x459F0）："
          % READY_QUEUE)
    queues = {}
    for i in range(13):
        head = u32(m, READY_QUEUE + i * 8)
        tail = u32(m, READY_QUEUE + i * 8 + 4)
        if head or tail:
            queues[i] = (head, tail)
            print("  槽 %-2d head=0x%06X(%s) tail=0x%06X(%s)"
                  % (i, head, tcb2task.get(head, "?"), tail, tcb2task.get(tail, "?")))

    print("\n任务  TCB      状态字      PC        入口       入口跑过  在哪个队列槽")
    for tid in sorted(st):
        t = st[tid]
        if not t["tcb"]:
            continue
        where = "-"
        for p, (head, tail) in queues.items():
            if head == t["tcb"] or tail == t["tcb"]:
                where = "槽%d" % p
        print("%-5s 0x%06X 0x%08X 0x%06X 0x%06X %-8s %s"
              % (tid, t["tcb"], t["wait_state"], t["pc"], t["entry"],
                 "是" if cov.visited(t["entry"]) else "**没**", where))

    print("\n判读：")
    t4 = st.get(4)
    if t4:
        in_q = [p for p, q in queues.items() if t4["tcb"] in q]
        print("  任务 4：状态字 0x%08X（就绪）、PC 0x%06X、入口跑过=%s、在队列槽 %s"
              % (t4["wait_state"], t4["pc"],
                 "是" if cov.visited(t4["entry"]) else "否",
                 in_q or "**没有**"))
    print("  当前任务仍在跑，且步骤数在涨 → 调度器没有被饿死，是「这一个任务」没被排上")


if __name__ == "__main__":
    main()
