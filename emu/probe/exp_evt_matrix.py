#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_evt_matrix.py —— 「置位图位 + 打对应中断」能否解开剩下的等待

exp_evt_bitmap 找到 13 处对 0x41A56 的操作，其中最完整的一处是 0x0AD662：

    0x0AD666  ldi  r0, #0x90
    0x0AD66A  btstl #8, r0        ; 外设状态寄存器 0x90 的 bit8 起来了？
    0x0AD66E  borl  #8, r4        ; → 在 0x41A56 置 bit8
    0x0AD672  borh  #8, r4        ; → 连 0x41A57 的高位一起置

也就是说固件自己就是这么转发的：**外设状态位 → 软件事件位图 → 回调里的位判定**。
本探针按这条链反向喂事件：先置位图位，再打中断，看任务 1/5/9 的等待点有没有变化。

组合来自 exp_slot_profile 的画像：

    槽 0x3F028 vec 0x13 ← 检查 0x41A56.bit4
    槽 0x3F040 vec 0x19 ← 检查 0x41A56.bit8
    槽 0x3F048 vec 0x1B ← 检查直接页 0x60 的 bit8/bit1/bit2
    槽 0x3F074 vec 0x23 ← 不检查（0x80/bit3 已由 dev_evt 覆盖）
    槽 0x3F078 vec 0x24 ← 检查 0x41A56.bit9

跑法：cd emu && python probe/exp_evt_matrix.py [--steps N]
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import realos
from frbox.flow import Flow

STEPS = 400000
BITMAP = 0x00041A56
DP60 = 0x60

# (名字, 位图掩码, (直接页, 位掩码) 或 None, 向量列表)
COMBOS = [
    ("基线", 0x0000, None, []),
    ("b4", 0x0010, None, [0x13]),
    ("b8", 0x0100, None, [0x19]),
    ("b9", 0x0200, None, [0x24]),
    ("b4+b8", 0x0110, None, [0x13, 0x19]),
    ("b4+b9", 0x0210, None, [0x13, 0x24]),
    ("b8+b9", 0x0300, None, [0x19, 0x24]),
    ("b4+b8+b9", 0x0310, None, [0x13, 0x19, 0x24]),
    ("+b10", 0x0710, None, [0x13, 0x19, 0x24]),
    ("+b12", 0x1310, None, [0x13, 0x19, 0x24]),
    ("b4..b12", 0x1FF0, None, [0x13, 0x19, 0x24]),
    ("位图全置", 0xFFFF, None, [0x13, 0x19, 0x1B, 0x24, 0x3C]),
    ("0x60 三位", 0x0000, (DP60, 0x106), [0x1B]),
]


def run(name, mask, dp, vecs, steps=STEPS):
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    m.enable_events(period=4096)
    fl = Flow(m)
    fired = 0
    BATCH = 5000
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        if mask or dp:
            if mask:
                m.bus.wr(BITMAP, 2, m.bus.rd(BITMAP, 2) | mask)
            if dp:
                off, bits = dp
                m.bus.wr(off, 1, m.bus.rd(off, 1) | bits)
            for v in vecs:
                m.raise_irq(v)
            fired += 1
        if m.cpu.halted or m.fault:
            break
    st = realos.states(m)
    waits = [(t["id"], t["wait_kind"], t["wait_flag"], t["wait_pat"])
             for t in st if t["wait_kind"] not in ("ready", "dormant")]
    sta = [(c[3], c[1]) for c in fl.calls if c[2] == -23]
    return {"name": name, "steps": m.step_count, "fired": fired,
            "svc": len(fl.calls), "ctx": sorted(t["id"] for t in st if t["pc"]),
            "waits": waits, "sta": sta,
            "halted": m.cpu.halted, "fault": m.fault, "pc": m.cpu.pc}


def main():
    steps = STEPS
    if "--steps" in sys.argv:
        steps = int(sys.argv[sys.argv.index("--steps") + 1])

    print("=" * 78)
    print("事件位图注入：%d 步、每 5000 步一次" % steps)
    print("%-12s %-8s %-6s %-6s %-14s %s"
          % ("组合", "步数", "事件", "服务", "有现场", "在等"))
    rows = []
    for name, mask, dp, vecs in COMBOS:
        r = run(name, mask, dp, vecs, steps)
        rows.append(r)
        waits = "；".join("任务%d:%s%s" % (i, k, ("/f%d/0x%X" % (f, p)) if f else "")
                         for i, k, f, p in r["waits"]) or "无"
        print("%-16s %-8d %-6d %-6d %-14s %s"
              % (name, r["steps"], r["fired"], r["svc"],
                 ",".join(str(x) for x in r["ctx"]), waits))
        if r["halted"] or r["fault"]:
            print("    ⚠️ halted=%s fault=%s" % (r["halted"], r["fault"]))

    print("\nsta_tsk 明细")
    for r in rows:
        s = " ".join("任务%d@0x%X" % (t, pc) for t, pc in r["sta"]) or "无"
        print("  %-16s %s" % (r["name"], s))


if __name__ == "__main__":
    main()
