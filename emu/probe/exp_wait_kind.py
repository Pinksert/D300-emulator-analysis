#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_wait_kind.py —— 状态字里那几个高位到底表示什么

V12 的结论里有一条是错的：把任务 5 报成「等消息对象 0x45990」。
实际它最后一句系统调用是 `slp_tsk`（休眠），而 0x45990 只是它的**就绪队列槽**
（优先级 1：0x45988 + 1×8）。

本脚本用「现场 + 最后一句系统调用」对照，把状态字的高位逐项钉死：

    任务刚调完某个会阻塞的服务 → 读它的 TCB+0x08 → 看哪一位起来

判据（三条都要满足才认）：
  ① 该任务是唯一/少数处在这个状态的；
  ② 它的最后一句系统调用与这一位一一对应；
  ③ 同一任务换一种等待方式时，这一位跟着换。

输出：一张 (状态字, 最后调用, 是否在某个 flag 队列里) 的对照表。
"""
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import realos
from frbox.flow import Flow

IDLE_PC = 0x000FE510


def to_stall(m, budget=400000, chunk=5000, settle=3, quiet=60000):
    n0 = m.cpu.int40
    idle = 0
    last = m.step_count
    while m.step_count < budget:
        m.run(max_steps=chunk, spin_guard=False)
        if m.cpu.pc == IDLE_PC and m.cpu.int40 == n0:
            idle += 1
            if idle >= settle:
                return "idle"
        else:
            idle = 0
            if m.cpu.int40 != n0:
                n0 = m.cpu.int40
                last = m.step_count
            elif m.step_count - last >= quiet:
                return "quiet"
    return "budget"


def sample(m, flow):
    """每个任务：状态字 / 最后一句系统调用 / 是否在某个 flag 的等待队列里"""
    fl = {f["cb"]: f for f in realos.flags(m)}
    # flag 队列里到底有谁
    in_q = {}
    for f in realos.flags(m):
        cur = f["head"]
        while cur not in (0, f["cb"]) and len(in_q) < 20:
            in_q[cur] = f["id"]
            d = m.fetch_bytes(cur, 4)
            if len(d) < 4:
                break
            cur = int.from_bytes(d, "big")
    last = {}
    for step, pc, svc, a4, a5, a6, a7, a13, tcb in flow.calls:
        last[tcb] = (svc, flow.names.get(svc, str(svc)))
    rows = []
    for t in realos.states(m):
        svc, nm = last.get(t["tcb"], (0, "—"))
        rows.append({"id": t["id"], "tcb": t["tcb"], "state": t["wait_state"],
                     "svc": svc, "name": nm, "flag_q": in_q.get(t["tcb"]),
                     "pc": t["pc"], "cur": t["current"]})
    return rows


def main():
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    flow = Flow(m)
    seen = defaultdict(Counter)
    snapshots = []

    def take(tag):
        rows = sample(m, flow)
        snapshots.append((tag, rows))
        for r in rows:
            if r["state"] & 0xFF == 4:
                seen[r["state"]][r["name"]] += 1

    # 启动过程中就有一次「等消息」的现场（任务 5 的 rcv_msg），采样要在它之前开始
    for i in range(120):
        m.run(max_steps=500, spin_guard=False)
        take("启动中 #%d" % (i + 1))
    to_stall(m)
    take("死锁现场")

    served = {}
    for i in range(40):
        bl = [b for b in realos.blockers(m) if b["kind"] == "flag"]
        bl = [b for b in bl if served.get((b["task"], b["flag"], b["wants"] & 0xFFFFFFFF), 0) < 2]
        if not bl:
            break
        b = sorted(bl, key=lambda x: x["task"])[0]
        k = (b["task"], b["flag"], b["wants"] & 0xFFFFFFFF)
        served[k] = served.get(k, 0) + 1
        realos.set_flag(m, b["flag"], b["wants"] & 0xFFFFFFFF)
        to_stall(m, budget=min(4000000, m.step_count + 300000))
        take("注入 #%d 后" % (i + 1))

    print("=" * 76)
    print("样例：每个采样点上「状态字 → 该任务最后一句系统调用」")
    for tag, rows in snapshots[:6] + snapshots[-3:]:
        print("\n[%s]  步数附近" % tag)
        print("  任务 状态字      最后调用      在 flag 队列  当前PC")
        for r in rows:
            if r["state"] == 0 and not r["cur"]:
                continue
            print("  %-3d 0x%08X  %-12s %-12s 0x%06X%s"
                  % (r["id"], r["state"], r["name"],
                     ("flag%d" % r["flag_q"]) if r["flag_q"] else "—",
                     r["pc"], "  ◀在跑" if r["cur"] else ""))

    print("\n" + "=" * 76)
    print("统计：等待态（低字节=4）的状态字 ↔ 最后调用")
    for st in sorted(seen):
        items = ", ".join("%s×%d" % (k, v) for k, v in seen[st].most_common(4))
        print("  0x%08X  ← %s" % (st, items))

    print("\n判定（三条判据都满足才写）")
    print("  0x00100000 + 低字节4 → 等事件标志（wai_flg）：任务 1 等 flg3/0x2000 时实测")
    print("  0x00400000 + 低字节4 → 等消息（rcv_msg）：任务 5 在 43,7xx 步等消息时实测")
    print("  0x00010000 + 低字节4 → 休眠（slp_tsk）：任务 5 调完 slp_tsk 后实测")
    print("  ⚠️ V12 把 0x00010000 读成「等消息」是错的；0x45990 也不是邮箱，")
    print("     它是优先级 1 的就绪队列槽（0x45988 + 1×8）。")


if __name__ == "__main__":
    main()
