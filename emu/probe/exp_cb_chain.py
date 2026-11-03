# -*- coding: utf-8 -*-
"""exp_cb_chain.py —— 把派发器手动接上，看这条回调链能走几站（V18 第 3 步）

背景（V18 前两步）：

  * 全局单槽 `0x417EC` 里躺着回调指针，`0x417F0` 是高半字倒计时，
    `0x3F404` bit1 是「有挂起」；
  * 派发器 `0x8223A` 的语义是「计数减 1，减到 0 才 `call [0x417EC]`」，而它唯一的
    引用点 `0x80606` 在主循环 `0x80588` 里 —— 那个主循环在模拟器里**一次都没跑过**。

所以这条链是「登记了、没人派发」。本脚本按固件本来的节奏手动 tick 派发器：

    tick 一次 = 主循环转一圈（真实固件里由 0x80588 那一大段分派表做）

每 10–12 个 tick 会真正执行一个回调，回调执行完又登记下一站。跑够多 tick，
看它走到哪一站停住、覆盖涨到多少、末态卡在什么上。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402

SLOT, ARG, FLAG, DISP = 0x000417EC, 0x000417F0, 0x0003F404, 0x0008223A
SLOT1, ARG1, FLAG1, DISP1 = 0x000417F4, 0x000417F8, 0x0003F40D, 0x000822CE


def warm(m, rounds=40, batch=5000):
    for _ in range(rounds):
        m.run(max_steps=batch, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)


def state(m):
    return {"cb": m.bus.rd(SLOT, 4), "cnt": m.bus.rd(ARG, 2),
            "cb1": m.bus.rd(SLOT1, 4), "cnt1": m.bus.rd(ARG1, 2),
            "flag1": m.bus.rd(FLAG1, 1),
            "flag": m.bus.rd(FLAG, 1), "cov": m.cov.words(),
            "svc": len(m.flow.calls)}


def run(label, ticks=240, between=0):
    m = create("d300-a")
    m.load()
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    warm(m)
    s0 = state(m)
    print("\n" + "=" * 78)
    print("%s  起点：槽0=0x%08X/0x%04X 槽1=0x%08X/0x%04X 标志0=0x%02X 标志1=0x%02X 覆盖=%d"
          % (label, s0["cb"], s0["cnt"], s0["cb1"], s0["cnt1"],
             s0["flag"], s0["flag1"], s0["cov"]))

    hits = []
    fired = 0
    for i in range(ticks):
        if m.bus.rd(SLOT, 4) == 0:
            print("  tick %-3d 槽空，链到此为止" % i)
            break
        cb = m.bus.rd(SLOT, 4)
        c0 = m.cov.words()
        r = m.call_function(DISP, max_steps=20000)
        cut = r["steps"] >= 20000
        d = m.cov.words() - c0
        if d:
            fired += 1
            hits.append((i, cb, r["steps"], d, m.bus.rd(SLOT, 4),
                         m.bus.rd(ARG, 2), m.bus.rd(FLAG, 1)))
            print("  tick %-3d 派发 0x%05X → %d 步，覆盖 +%-4d，"
                  "下一站 0x%05X 计数 0x%04X 标志 0x%02X"
                  % (i, cb, r["steps"], d, m.bus.rd(SLOT, 4),
                     m.bus.rd(ARG, 2), m.bus.rd(FLAG, 1)))
        if cut:
            print("  tick %-3d 派发 0x%05X → 没回来（%d 步）：控制权交给调度器了"
                  % (i, cb, r["steps"]))
        if r["halted"]:
            print("  tick %-3d CPU halted" % i)
            break
        if between:
            m.run(max_steps=between, spin_guard=False)
            m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
            for v in (0x13, 0x19):
                m.raise_irq(v)
    s1 = state(m)
    print("  收尾：槽0=0x%08X/0x%04X 槽1=0x%08X/0x%04X 标志0=0x%02X 标志1=0x%02X"
          % (s1["cb"], s1["cnt"], s1["cb1"], s1["cnt1"], s1["flag"], s1["flag1"]))
    print("        覆盖 %d → %d（+%d）服务 %d"
          % (s0["cov"], s1["cov"], s1["cov"] - s0["cov"], s1["svc"] - s0["svc"]))
    print("  真正执行过的回调：%d 次，共 %d 站" % (fired, len(hits)))
    for i, cb, steps, d, nxt, cnt, fl in hits:
        print("    tick %-3d 0x%05X → 0x%05X（%d 步 / +%d）"
              % (i, cb, nxt, steps, d))

    print("\n  末态任务表：")
    for t in realos.states(m):
        print("    任务 %-2d %-7s pc=0x%08X" % (t["id"], t["wait_kind"], t["pc"]))
    return m, s0, s1, hits


def main():
    run("A 只 tick 派发器（240 次）", ticks=240)
    run("B 每次派发后空转 4000 步", ticks=240, between=4000)
    run("C 只 tick 40 次（看门控节奏）", ticks=40)
    run("D 每次派发后空转 6000 步（让被唤醒的任务跑）", ticks=120, between=6000)


if __name__ == "__main__":
    main()
