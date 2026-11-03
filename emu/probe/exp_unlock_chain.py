# -*- coding: utf-8 -*-
"""exp_unlock_chain.py —— 每个任务「在等什么、解开后往哪走」的完整轨迹（V16）

用法：

    python probe/exp_unlock_chain.py                    # 7 个 dormant 任务
    python probe/exp_unlock_chain.py --tasks 1,5,8,9    # 指定
    python probe/exp_unlock_chain.py --all              # 全部 13 条

两种模式：

    boot    改任务表激活位，让 REALOS 初始化自己把任务建起来（dormant 的 7 个）
    live    不动配置，只在已经跑起来的现场上接着推（1/5/8/9 这种本来就激活的）

判据：每一站记 PC / 等哪个 flag 的哪几位 / 该任务已发起的服务数；解开后
PC·服务·覆盖三者都不动就判死。任务自己 `ext_tsk` 的会标「已退出」。
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create          # noqa: E402
from frbox import realos, unlock          # noqa: E402

DORMANT = [2, 3, 6, 7, 11, 12]
LIVE = [1, 5, 8, 9]
ALL = [1, 2, 3, 5, 6, 7, 8, 9, 11, 12]
ACTIVE_BIT = 0x80000000


def boot(tid):
    """把任务 tid 的 state 最高位置 1，让 REALOS 初始化自己创建它。"""
    m = create("d300-a")
    m.load()
    base = m.bus.rom_base
    rec = [r for r in realos.records(m) if r["id"] == tid]
    if rec:
        a = rec[0]["addr"] + 0x0C
        m.bus.rom[a - base:a - base + 4] = ACTIVE_BIT.to_bytes(4, "big")
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m, cov


def trace_one(tid, rounds, batch, mode="boot", idle=6):
    m, cov = boot(tid) if mode == "boot" else _live()
    st = unlock.trace(m, tid, rounds=rounds, batch=batch, cov=cov, idle_max=idle)
    return st, cov


def _live():
    m = create("d300-a")
    m.load()
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m, cov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rounds", type=int, default=16)
    ap.add_argument("--batch", type=int, default=4000)
    ap.add_argument("--idle", type=int, default=6,
                    help="「还没被派发」最多容忍几轮（固件里 sta_tsk 有到 5 万步才发生的）")
    args = ap.parse_args()

    if args.all:
        tasks = ALL
    elif args.tasks:
        tasks = [int(x) for x in args.tasks.split(",") if x.strip()]
    else:
        tasks = DORMANT

    print("=" * 96)
    print("任务轨迹：每站「跑 %d 步 → 看它等哪个 flag 的哪几位 → 用固件内核置位」× %d 轮"
          % (args.batch, args.rounds))
    print("=" * 96)

    summary = []
    for tid in tasks:
        mode = "live" if tid in LIVE else "boot"
        st, cov = trace_one(tid, args.rounds, args.batch, mode, args.idle)
        print("\n" + "-" * 96)
        print("任务 %d（%s 模式）" % (tid, "运行时" if mode == "live" else "复位激活"))
        print("-" * 96)
        unlock.print_trace(st)
        print("覆盖地址 %d" % (cov.words() if cov is not None else -1))
        summary.append((tid, len(st["stations"]), st["end"]))

    print("\n" + "=" * 96)
    print("%-6s %-6s %s" % ("任务", "站数", "收尾"))
    for tid, n, end in summary:
        print("%-6d %-6d %s" % (tid, n, end))


if __name__ == "__main__":
    main()
