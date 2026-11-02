# -*- coding: utf-8 -*-
"""exp_task4_unblock.py —— 任务 4 是不是被同优先级的任务 8 忙等饿死的（V15）

`exp_ready_queue.py` 显示：任务 4 与任务 8 同在优先级 3 的队列里（head=任务8，
tail=任务4），而任务 8 当时正卡在 `0x0AD652` 轮询直接页 `0x90` 的忙等环里。
V14 逐位实测过：只有 `0x90` 的 bit1 能放行。于是做三组对照：

    A 不喂            任务 8 忙等 → 任务 4 永远轮不到
    B 喂 0x90.bit1    任务 8 走掉（sleep/svc）→ 任务 4 应该被派发
    C 喂 0x90.bit2    反证：喂别的位没用

判据是覆盖率里任务 4 的入口 `0x08C486` 有没有被执行，而不是看状态字（状态字
ready 只说明「在队列里」，不说明「被派发过」）。
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create          # noqa: E402
from frbox.flow import Flow               # noqa: E402
from frbox import realos                  # noqa: E402

STEPS, BATCH = 200000, 5000
T4_ENTRY = 0x08C486
T8_POLL = 0x0AD652


def run(bit, steps=STEPS):
    m = create("d300-a")
    m.load()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    fl = Flow(m)
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        if bit is not None:
            m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | (1 << bit))
        for v in (0x13, 0x19):
            m.raise_irq(v)
    st = {t["id"]: t for t in realos.states(m)}
    return {
        "bit": bit, "m": m, "cov": cov, "svc": len(fl.calls),
        "t4_ran": cov.visited(T4_ENTRY),
        "t4": st.get(4), "t8": st.get(8),
        "words": cov.words(),
    }


def main():
    print("%-10s %-6s %-8s %-26s %-26s %s"
          % ("喂 0x90", "服务", "任务4跑过", "任务8 (状态/PC)", "任务4 (状态/PC)", "执行地址"))
    for bit in (None, 1, 2):
        r = run(bit)
        t8, t4 = r["t8"], r["t4"]
        print("%-10s %-6d %-8s 0x%08X 0x%06X %-8s 0x%08X 0x%06X %d"
              % ("不喂" if bit is None else "bit%d" % bit, r["svc"],
                 "是" if r["t4_ran"] else "**没**",
                 t8["wait_state"], t8["pc"], t8["wait_kind"],
                 t4["wait_state"], t4["pc"], r["words"]))


if __name__ == "__main__":
    main()
