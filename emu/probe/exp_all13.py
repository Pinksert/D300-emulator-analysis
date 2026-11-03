# -*- coding: utf-8 -*-
"""exp_all13.py —— 13 条任务的总账：三种场景下「谁真的跑起来了」（V16）

场景：

    A 原始        什么都不改，20 万步（V15 的 D 场景口径）
    B 全开        7 个 dormant 任务的「复位就激活」全置 1，跑 20 万步
    C 全开+解锁   B 之后再对每条任务做一次 unlock.trace（一站一站喂它要的 flag）

判据一律是**覆盖率**：`cov.visited(entry)` 为真才算跑过。状态字 ready 不算，
这是 V15 用任务 4 换来的口径。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create          # noqa: E402
from frbox import realos, taskboot, unlock  # noqa: E402

STEPS, BATCH = 200000, 5000
ALL_TASKS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]


def base(boot_all=False):
    m = create("d300-a")
    m.load()
    if boot_all:
        taskboot.activate(m, "dormant")
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m, cov


def spin(m, steps=STEPS):
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        if m.cpu.halted or m.fault:
            break


def snapshot(m, cov):
    st = {t["id"]: t for t in realos.states(m)}
    rows = []
    for tid in ALL_TASKS:
        t = st[tid]
        rows.append((tid, cov.visited(t["entry"]), t["wait_kind"],
                     t["wait_flag"], t["wait_pat"], t["pc"]))
    return {"words": cov.words(), "svc": len(m.flow.calls),
            "ran": [r[0] for r in rows if r[1]],
            "rows": rows, "steps": m.step_count}


def show(name, s):
    print("\n" + "=" * 88)
    print("%s   步数 %d   执行地址 %d   服务 %d   跑过的任务 %d/12"
          % (name, s["steps"], s["words"], s["svc"], len(s["ran"])))
    print("=" * 88)
    print("%-5s %-6s %-10s %-8s %s" % ("任务", "跑过", "在等", "要的位", "PC"))
    for tid, ran, kind, flag, pat, pc in s["rows"]:
        what = kind if flag is None else "flag%d" % flag
        print("%-5s %-6s %-10s %-8s 0x%06X"
              % (tid, "是" if ran else "**没**", what,
                 ("0x%X" % pat) if pat else "", pc))


def main():
    m, cov = base(False)
    spin(m)
    a = snapshot(m, cov)
    show("A 原始（什么都不改）", a)

    m, cov = base(True)
    spin(m)
    b = snapshot(m, cov)
    show("B 全开（7 个 dormant 的激活位全置 1）", b)

    for tid in ALL_TASKS:
        unlock.trace(m, tid, rounds=12, batch=4000, cov=cov, idle_max=6)
    # 任务 12 排在最后，预算已经用掉大半；单独再给它一段（它的入口在 0x0E3094，
    # 中间隔着一大片还没跑过的代码），确认「排到最后没轮到」不等于「起不来」。
    unlock.trace(m, 12, rounds=16, batch=6000, cov=cov, idle_max=8)
    c = snapshot(m, cov)
    show("C 全开 + 逐任务解锁", c)

    print("\n" + "=" * 88)
    print("%-16s %-10s %-10s %-10s" % ("场景", "执行地址", "服务", "跑过任务"))
    for nm, s in (("A 原始", a), ("B 全开", b), ("C 全开+解锁", c)):
        print("%-16s %-10d %-10d %d/12" % (nm, s["words"], s["svc"], len(s["ran"])))

    missing = [t for t in ALL_TASKS if t not in c["ran"]]
    print("\nC 场景仍没跑过的任务：%s" % (missing or "无"))


if __name__ == "__main__":
    main()
