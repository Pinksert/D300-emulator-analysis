# -*- coding: utf-8 -*-
"""exp_task_boot.py —— 逐个把 dormant 任务「复位激活」，看固件自己建不建（V16）

V15 留下的问题是「还有 7 个任务入口从没执行过，谁 sta_tsk 它们没定位」。
V16 换了走法：不动运行时，直接改**任务表记录里的 state 最高位**（0x80000000 =
「复位时就激活」），让 REALOS 初始化自己去建这些任务。

一次全开会互相抢 CPU（任务 11 优先级 1 会占住），所以这里是**单任务对照**：
每次只激活一个，看它的入口是否被执行、跑完退出还是卡在某个 flag。

    python probe/exp_task_boot.py            # 默认 20 万步 × 8 组
    python probe/exp_task_boot.py --steps 400000

⚠️ 这是**配置层面的实验**：真实固件里这些位是 0（不该开机启动）。报告里必须
写明「改了激活位」，不能写成「相机开机就会跑这些任务」。
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create          # noqa: E402
from frbox.flow import Flow               # noqa: E402
from frbox import realos                  # noqa: E402

DORMANT = [2, 3, 6, 7, 10, 11, 12]
STEPS, BATCH = 200000, 5000
ACTIVE_BIT = 0x80000000


def activate(m, tids):
    """把指定任务的 state 最高位置 1（任务表在 ROM 区，只能改 bus.rom 数组）。"""
    base = m.bus.rom_base
    done = []
    for tid in tids:
        rec = [r for r in realos.records(m) if r["id"] == tid]
        if not rec:
            continue
        off = rec[0]["addr"] + 0x0C - base
        m.bus.rom[off:off + 4] = ACTIVE_BIT.to_bytes(4, "big")
        done.append(tid)
    return done


def run(tids, steps=STEPS, allow90=True):
    m = create("d300-a")
    m.load()
    activated = activate(m, tids)
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    fl = Flow(m)
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        if allow90:
            m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        if m.cpu.halted or m.fault:
            break
    st = {t["id"]: t for t in realos.states(m)}
    rows = {}
    for tid in DORMANT:
        t = st.get(tid)
        if not t:
            continue
        rows[tid] = {
            "entry": t["entry"],
            "ran": cov.visited(t["entry"]),
            "kind": t["wait_kind"],
            "flag": t["wait_flag"],
            "pat": t["wait_pat"],
            "pc": t["pc"],
            "svc": len([c for c in fl.calls if len(c) > 8 and c[8] == t["tcb"]]),
            "exit": len([c for c in fl.calls
                         if len(c) > 8 and c[8] == t["tcb"] and c[2] == -21]),
        }
    return {"activated": activated, "steps": m.step_count, "words": cov.words(),
            "svc": len(fl.calls), "rows": rows,
            "sta": [(c[3], c[1]) for c in fl.calls if c[2] == -23],
            "halted": m.cpu.halted, "fault": m.fault}


def fmt(r, tid):
    row = r["rows"].get(tid)
    if not row:
        return "（无记录）"
    verdict = "跑过" if row["ran"] else "**没跑过**"
    if row["exit"]:
        verdict = "跑过→ext_tsk 退出"
    if row["kind"] in ("flag", "msg", "sleep"):
        verdict += "，卡在 %s%s%s" % (
            row["kind"],
            ("%d" % row["flag"]) if row["flag"] is not None else "",
            ("/0x%X" % row["pat"]) if row["pat"] else "")
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=STEPS)
    ap.add_argument("--no90", action="store_true", help="不给 0x90 放行（任务 8 会忙等）")
    args = ap.parse_args()

    print("=" * 92)
    print("单任务复位激活：每组只把 1 个任务的 state 最高位置 1，%d 步" % args.steps)
    print("=" * 92)

    base = run([], args.steps, not args.no90)
    print("\n%-6s %-9s %-8s %-7s %-30s %s"
          % ("激活", "执行地址", "服务", "该任务服务", "结论", "sta_tsk"))
    print("%-6s %-9s %-8d %-7s %-30s %s"
          % ("无", base["words"], base["svc"], "-",
             "基线（2/3/6/7/10/11/12 全 dormant）",
             " ".join("%d@0x%06X" % (t, a) for t, a in base["sta"])))

    out = []
    for tid in DORMANT:
        r = run([tid], args.steps, not args.no90)
        row = r["rows"].get(tid, {})
        print("%-6s %-9s %-8d %-7s %-30s %s"
              % ("任务%d" % tid, r["words"], r["svc"], row.get("svc", 0),
                 fmt(r, tid), " ".join("%d@0x%06X" % (t, a) for t, a in r["sta"])))
        out.append((tid, r))

    allr = run(DORMANT, args.steps, not args.no90)
    print("%-6s %-9s %-8d %-7s %-30s %s"
          % ("全开", allr["words"], allr["svc"], "-",
             "7 个一起激活（会互相抢 CPU）",
             " ".join("%d@0x%06X" % (t, a) for t, a in allr["sta"])))

    print("\n全开时 7 个任务各自的去向：")
    for tid in DORMANT:
        print("   任务%-3s %s" % (tid, fmt(allr, tid)))


if __name__ == "__main__":
    main()
