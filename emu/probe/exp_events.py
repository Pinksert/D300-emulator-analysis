#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_events.py —— 给一个「完成位 + 中断」的外设模型，系统能自己走多远

V12 的推进靠「用内核注入 flag」。V13 换成一个更像硬件的东西：一个周期性置
状态位（0x80/bit3）并打中断（vec 0x23）的事件源（`frbox/dev_evt.py`）。

判据：
  - 服务调用次数（V11 基线：14）
  - 有现场的任务数 / 真跑过的任务（见过 ≥2 个 PC）
  - 任务 1 走到第几个等待点、有没有 sta_tsk 拉起新任务
  - 事件周期从 1,024 扫到 16,384，看结论是否依赖这个参数

跑法：cd emu && python probe/exp_events.py
"""
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import realos
from frbox.flow import Flow

STEPS = 1000000


def run_one(period, steps=STEPS):
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    fl = Flow(m)
    ev = m.enable_events(period=period) if period else None
    pcs = {}
    for _ in range(steps // 50000):
        m.run(max_steps=50000, spin_guard=False)
        for t in realos.states(m):
            if t["pc"]:
                pcs.setdefault(t["id"], set()).add(t["pc"])
        if m.cpu.halted or m.fault:
            break
    st = realos.states(m)
    sta = [(c[3], c[1]) for c in fl.calls if c[2] == -23]
    return {"period": period, "steps": m.step_count, "svc": len(fl.calls),
            "events": ev.fired if ev else 0,
            "ctx": sorted(t["id"] for t in st if t["pc"]),
            "ran": sorted(t for t, s in pcs.items() if len(s) >= 2),
            "sta": sta,
            "waits": [(t["id"], t["wait_kind"], t["wait_flag"], t["wait_pat"])
                      for t in st if t["wait_kind"] not in ("ready", "dormant")],
            "halted": m.cpu.halted, "fault": m.fault, "pc": m.cpu.pc}


def main():
    print("=" * 78)
    print("周期性「完成位 0x80/bit3 + vec 0x23」事件源：不同周期下系统走多远")
    print("  （%d 步，无任何 flag 注入）" % STEPS)
    print("%-8s %-8s %-6s %-6s %-22s %s"
          % ("周期", "步数", "事件", "服务", "有现场的任务", "真跑过"))
    rows = []
    for p in (0, 1024, 2048, 4096, 8192, 16384):
        r = run_one(p)
        rows.append(r)
        print("%-8s %-8d %-6d %-6d %-22s %s"
              % (p if p else "不挂", r["steps"], r["events"], r["svc"],
                 ",".join(str(x) for x in r["ctx"]),
                 ",".join(str(x) for x in r["ran"])))

    print("\n各自的 sta_tsk 与停机等待点")
    for r in rows:
        sta = " ".join("任务%d@0x%X" % (t, pc) for t, pc in r["sta"]) or "无"
        waits = "；".join("任务%d:%s/%s/0x%X" % (i, k, f, p)
                         for i, k, f, p in r["waits"]) or "无"
        print("  周期 %-6s sta_tsk: %-28s 在等: %s" % (r["period"] or "不挂", sta, waits))
        if r["halted"] or r["fault"]:
            print("      ⚠️ halted=%s fault=%s" % (r["halted"], r["fault"]))

    print("\n对照：V11 基线（不挂事件源、不做注入）")
    base = rows[0]
    print("  服务 %d 次 / 有现场 %s / 真跑过 %s"
          % (base["svc"], base["ctx"], base["ran"]))


if __name__ == "__main__":
    main()
