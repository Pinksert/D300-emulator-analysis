# -*- coding: utf-8 -*-
"""exp_msg_full.py —— 两条注入通道合起来：消息 + flag（V17 收尾）

V16 的最好成绩是「全开 dormant + 逐任务解锁 flag」= 11/12（剩任务 12）。
V17 找到了第二条通道：**借固件内核投递消息**。两者是不同原语，理应能叠加。

本脚本做四个场景的横向对比：

    P  V16-C 复现     全开 dormant + 逐任务 unlock（不应有变化，当作对照）
    Q  P + 消息投递   每个任务解锁前后各投一条消息
    R  Q + 二次扫描   解开一轮之后再扫一遍还没上场的任务
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create                       # noqa: E402
from frbox import realos, taskboot, unlock, mailbox     # noqa: E402

WARM, BATCH = 200000, 5000
ALL = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]


def base():
    m = create("d300-a")
    m.load()
    taskboot.activate(m, "dormant")
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    mb = m.enable_msgs()
    return m, cov, mb


def spin(m, steps):
    left = steps
    while left > 0:
        n = min(BATCH, left)
        m.run(max_steps=n, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        left -= n
        if m.cpu.halted or m.fault:
            break


def snap(m, cov):
    st = {t["id"]: t for t in realos.states(m)}
    return {
        "steps": m.step_count, "words": cov.words(), "svc": len(m.flow.calls),
        "ran": [t for t in ALL if cov.visited(st[t]["entry"])],
        "rows": [(t, cov.visited(st[t]["entry"]), st[t]["wait_kind"],
                  st[t]["wait_pat"], st[t]["pc"]) for t in ALL],
    }


def run(label, use_msg=False, second_pass=False):
    """V16 的 C 场景原样复现（12 条全扫 + 任务 12 单独补一段），再叠消息投递。"""
    m, cov, mb = base()
    spin(m, WARM)
    for tid in ALL:
        if use_msg and mb.task5_state()["kind"] == "msg":
            mb.send()
        unlock.trace(m, tid, rounds=12, batch=4000, cov=cov, idle_max=6)
    unlock.trace(m, 12, rounds=16, batch=6000, cov=cov, idle_max=8)
    if use_msg:
        for _ in range(4):
            if mb.task5_state()["kind"] == "msg":
                mb.send()
            spin(m, 10000)
    if second_pass:
        for tid in ALL:
            if cov.visited({t["id"]: t for t in realos.states(m)}[tid]["entry"]):
                continue
            unlock.trace(m, tid, rounds=32, batch=6000, cov=cov, idle_max=8)
            if use_msg and mb.task5_state()["kind"] == "msg":
                mb.send()
        spin(m, 40000)
    s = snap(m, cov)
    s["label"] = label
    s["sent"] = mb.sent
    return s


def table(s):
    print("=" * 84)
    print("%s   步数 %d   执行地址 %d   服务 %d   投递 %d   跑过 %d/12"
          % (s["label"], s["steps"], s["words"], s["svc"], s["sent"], len(s["ran"])))
    for tid, ran, kind, pat, pc in s["rows"]:
        if not ran:
            print("  任务 %-2d **没跑过**   %s" % (tid, kind))
    miss = [r[0] for r in s["rows"] if not r[1]]
    if miss:
        print("  未上场：%s" % miss)


def main():
    out = [run("P V16-C 复现：全开 + 逐任务解锁")]
    table(out[-1])
    out.append(run("Q P + 消息投递", use_msg=True))
    table(out[-1])
    out.append(run("R Q + 二次扫描", use_msg=True, second_pass=True))
    table(out[-1])

    print("=" * 84)
    print("%-30s %-10s %-10s %-8s %-8s %s"
          % ("场景", "步数", "执行地址", "服务", "投递", "跑过任务"))
    for s in out:
        print("%-30s %-10d %-10d %-8d %-8d %d/12"
              % (s["label"], s["steps"], s["words"], s["svc"], s["sent"],
                 len(s["ran"])))

    print("\n各场景末态")
    for s in out:
        print("  " + s["label"])
        for tid in ALL:
            row = [r for r in s["rows"] if r[0] == tid][0]
            print("    任务 %-2d %-8s %-8s pat=0x%-8X pc=0x%08X"
                  % (tid, "跑过" if row[1] else "**没**", row[2], row[3], row[4]))


if __name__ == "__main__":
    main()
