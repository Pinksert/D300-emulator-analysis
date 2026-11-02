#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_flag_writer.py —— 每个事件标志的「最后一位写入者」是谁

V13 的遗留问题：任务 1 停在 flag9/0x400、任务 9 停在 flag10/0x4000，
静态扫 `set_flg` 找不到置位者。exp_evt_matrix 把位图 bit4+bit8 一起喂进去之后，
**flag9/0x400 被解开了**，任务 1 前进到 flag1/0x8000 —— 说明置位者确实存在于
那条链上，只是不走我们认得的 `set_flg` 形式。

本探针用「写监视」直接抓现行：分批跑、每批清空 `all_writes`，只保留落在
10 个 flag 控制块（0x45C38 起，步长 0x10）里的写，记录写入者 PC 与新值。

跑法：cd emu && python probe/exp_flag_writer.py [--mask 0x0110] [--steps N]
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

BITMAP = 0x00041A56
FLAG_CB = 0x00045C38
FLAG_STEP = 0x10
FLAG_COUNT = 10
FLAG_HI = FLAG_CB + FLAG_COUNT * FLAG_STEP


def run(mask, vecs, steps, watch_bitmap=True):
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    m.enable_events(period=4096)
    fl = Flow(m)
    writes = {}          # (addr) -> [(pc, val, step)]
    bm_writes = []
    BATCH = 5000
    for _ in range(steps // BATCH):
        m.bus.all_writes.clear()
        m.run(max_steps=BATCH, spin_guard=False)
        if mask:
            m.bus.wr(BITMAP, 2, m.bus.rd(BITMAP, 2) | mask)
            for v in vecs:
                m.raise_irq(v)
        for a, n, v, pc in m.bus.all_writes:
            if FLAG_CB <= a < FLAG_HI:
                writes.setdefault(a, []).append((pc, v, m.step_count))
            elif watch_bitmap and a in (BITMAP, BITMAP + 1):
                bm_writes.append((pc, a, v, m.step_count))
        if m.cpu.halted or m.fault:
            break
    return m, fl, writes, bm_writes


def main():
    steps, mask, vecs = 150000, 0x0110, [0x13, 0x19]
    if "--steps" in sys.argv:
        steps = int(sys.argv[sys.argv.index("--steps") + 1])
    if "--mask" in sys.argv:
        mask = int(sys.argv[sys.argv.index("--mask") + 1], 0)

    m, fl, writes, bm = run(mask, vecs, steps)
    print("=" * 78)
    print("掩码 0x%04X + 向量 %s，跑 %d 步" % (mask, ",".join("0x%02X" % v for v in vecs),
                                              m.step_count))
    print("=" * 78)

    print("\n一、flag 控制块被写过的字段（+0=队列头 +4=队列尾 +8=位模式 +0xC=回指）")
    print("-" * 78)
    for i in range(FLAG_COUNT):
        cb = FLAG_CB + i * FLAG_STEP
        rows = []
        for off, tag in ((0, "头"), (4, "尾"), (8, "位模式"), (0xC, "回指")):
            recs = writes.get(cb + off, [])
            if recs:
                pcs = sorted({pc for pc, v, s in recs})
                last = recs[-1]
                rows.append("+0x%X %s: 0x%X ← %s ×%d"
                            % (off, tag, last[1],
                               " ".join("0x%06X" % p for p in pcs[:4]), len(recs)))
        pat = int.from_bytes(m.fetch_bytes(cb + 8, 4), "big")
        head = int.from_bytes(m.fetch_bytes(cb, 4), "big")
        print("flag%-2d @0x%05X 现位模式=0x%-8X 队列%s"
              % (i + 1, cb, pat, "空" if head == cb else "非空"))
        for r in rows:
            print("        " + r)

    print("\n二、位模式字段的写入者（只看 +0x08，按 flag 汇总）")
    print("-" * 78)
    any_ = False
    for i in range(FLAG_COUNT):
        recs = writes.get(FLAG_CB + i * FLAG_STEP + 8, [])
        if not recs:
            continue
        any_ = True
        by_pc = {}
        for pc, v, s in recs:
            by_pc.setdefault(pc, []).append((v, s))
        print("flag%d：" % (i + 1))
        for pc, vs in sorted(by_pc.items()):
            print("    0x%06X ×%-3d 值 %s" % (pc, len(vs),
                                             " ".join("0x%X" % v for v, _ in vs[:6])))
    if not any_:
        print("（本轮没有任何一次写 +0x08 —— 位模式不是写进来的）")

    print("\n三、位图 0x41A56 的写入者（前 12 条）")
    print("-" * 78)
    for pc, a, v, s in bm[:12]:
        print("  step %-8d 0x%05X ← 0x%02X  (写者 0x%06X)" % (s, a, v, pc))
    if not bm:
        print("  （无）")

    st = realos.states(m)
    print("\n四、跑完的在等状态")
    print("-" * 78)
    for t in st:
        if t["wait_kind"] in ("ready", "dormant"):
            continue
        print("  任务%-2d %s%s  PC=0x%06X"
              % (t["id"], t["wait_kind"],
                 ("/flag%d/0x%X" % (t["wait_flag"], t["wait_pat"])) if t["wait_flag"] else "",
                 t["pc"]))
    sta = [(c[3], c[1]) for c in fl.calls if c[2] == -23]
    print("  sta_tsk：%s" % (" ".join("任务%d@0x%X" % (t, pc) for t, pc in sta) or "无"))


if __name__ == "__main__":
    main()
