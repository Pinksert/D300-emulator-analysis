#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_dp_bit.py —— 任务 8 等的是直接页 0x90 的哪一位

V14 用「位图 bit4+bit8」把任务 8 拉起来之后，它一直停在

    0x0AD64E  ldi   r0, #0x90
    0x0AD652  btstl #2, r0
    0x0AD654  beq   *-8            ; 位没起来就回去重测

也就是说任务 8 在**轮询**直接页 0x90 的某一位。`btstl #2` 的 2 到底是位号、
还是某种编码（V13 的 `btstl #8,@0x80` 就不是 bit8 —— dev_evt 置的是 0x80 的 bit3
却让那段代码通过了），只能逐位试。

做法：固定「位图 bit4+bit8 → vec 0x13/0x19」把任务 8 拉起，然后对 0x90 逐位置 1，
看任务 8 是否离开 0x0AD64E–0x0AD654 这段自旋，以及服务调用数怎么变。

跑法：cd emu && python probe/exp_dp_bit.py [--steps N] [--addr 0x90]
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

SPIN_LO, SPIN_HI = 0x0AD64E, 0x0AD656       # 任务 8 的轮询环


def run(addr, bit, steps, mask=0x0110, vecs=(0x13, 0x19)):
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    m.enable_events(period=4096)
    fl = Flow(m)
    BATCH = 5000
    spin = 0
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x41A56, 2, m.bus.rd(0x41A56, 2) | mask)
        if bit is not None:
            m.bus.wr(addr, 1, m.bus.rd(addr, 1) | (1 << bit))
        for v in vecs:
            m.raise_irq(v)
        cur = int.from_bytes(m.fetch_bytes(0x45980, 4), "big")
        if cur == 0x045B30 and SPIN_LO <= m.cpu.pc < SPIN_HI:
            spin += 1
        if m.cpu.halted or m.fault:
            break
    st = realos.states(m)
    t8 = [t for t in st if t["id"] == 8]
    return {"bit": bit, "steps": m.step_count, "svc": len(fl.calls),
            "spin": spin, "t8pc": t8[0]["pc"] if t8 else 0,
            "t8state": t8[0]["wait_state"] if t8 else 0,
            "ctx": sorted(t["id"] for t in st if t["pc"]),
            "waits": [(t["id"], t["wait_kind"], t["wait_flag"], t["wait_pat"])
                      for t in st if t["wait_kind"] not in ("ready", "dormant")],
            "sta": [(c[3], c[1]) for c in fl.calls if c[2] == -23]}


def main():
    steps, addr = 200000, 0x90
    if "--steps" in sys.argv:
        steps = int(sys.argv[sys.argv.index("--steps") + 1])
    if "--addr" in sys.argv:
        addr = int(sys.argv[sys.argv.index("--addr") + 1], 0)

    print("=" * 78)
    print("直接页 0x%02X 逐位扫描：任务 8 的轮询环是 0x%06X–0x%06X"
          % (addr, SPIN_LO, SPIN_HI - 1))
    print("%-8s %-8s %-6s %-6s %-10s %s" % ("置位", "步数", "服务", "自旋占比", "任务8 PC", "在等"))
    rows = []
    for bit in [None] + list(range(8)):
        r = run(addr, bit, steps)
        rows.append(r)
        tag = "不置" if bit is None else "bit%d" % bit
        print("%-8s %-8d %-6d %-6s 0x%06X   %s"
              % (tag, r["steps"], r["svc"], "%d/%d" % (r["spin"], steps // 5000),
                 r["t8pc"],
                 "；".join("任务%d:%s%s" % (i, k, ("/f%d" % f) if f else "")
                          for i, k, f, p in r["waits"]) or "无"))

    moved = [r for r in rows if r["bit"] is not None and r["spin"] < (steps // 5000) // 4]
    print()
    if moved:
        print("★ 能让任务 8 离开轮询环的位：%s"
              % " ".join("0x%02X.bit%d" % (addr, r["bit"]) for r in moved))
        for r in moved:
            print("    bit%d：服务 %d 次，sta_tsk %s"
                  % (r["bit"], r["svc"],
                     " ".join("任务%d@0x%X" % (t, pc) for t, pc in r["sta"]) or "无"))
    else:
        print("没有任何一位能让它离开 —— 说明 0x90 只被读、不靠写，或者条件不止这一位。")


if __name__ == "__main__":
    main()
