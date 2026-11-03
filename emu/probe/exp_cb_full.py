# -*- coding: utf-8 -*-
"""exp_cb_full.py —— 回调链主实验：接入 tick 之后能走多远（V18 第 4 步）

四个场景，同一套预热口径（跑 40×5000 步 + 每批喂一次事件位与 vec 0x13/0x19）：

    A 基线        不 tick，看两个槽登记了什么、派发器有没有执行
    B 接上 tick   一直扫到没有槽挂着指针，逐站记录（回调 / 步数 / 覆盖增量 / 下一站）
    C B 之后跑    让被 set_flg / sta_tsk 叫醒的任务真正跑起来
    D 全开+ tick  V16 的 taskboot 把 7 个 dormant 任务一起放开，再接 tick

⚠️ 口径：所有 tick 都是**外部注入**（替那个从不执行的主循环 0x80588 转圈），
不是固件自己在跑。报告里不能写成「相机开始工作了」。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos, taskboot          # noqa: E402

BATCH = 5000
WARM_ROUNDS = 40


def base(boot_all=False):
    # ⚠️ 顺序：load → 改任务表（ROM）→ reset。反过来的话改的是已经拷进 RAM 的副本。
    m = create("d300-a")
    m.load()
    if boot_all:
        taskboot.activate(m, "dormant")
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    cb = m.enable_callbacks()
    return m, cb


def warm(m, rounds=WARM_ROUNDS):
    for _ in range(rounds):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)


def tasks(m):
    return {t["id"]: (t["wait_kind"], t["pc"]) for t in realos.states(m)}


def report(label, m, cb, cov0=None):
    cov = m.cov.words()
    print("\n" + "=" * 78)
    print("%s" % label)
    print("  步数 %d  执行地址 %d%s  服务 %d"
          % (m.step_count, cov,
             "（+%d）" % (cov - cov0) if cov0 is not None else "",
             len(m.flow.calls)))
    print("  " + cb.summary())
    print("  主循环 0x80588 覆盖 = %s" % m.cov.visited(0x80588))
    print("  派发器 0x8223A 覆盖 = %s ；0x822CE 覆盖 = %s"
          % (m.cov.visited(0x8223A), m.cov.visited(0x822CE)))
    st = tasks(m)
    live = [(i, k, pc) for i, (k, pc) in sorted(st.items())
            if k not in ("dormant",) and i != 0]
    print("  活着的任务 %d 个：%s"
          % (len(live), " ".join("%d(%s)" % (i, k) for i, k, _ in live)))
    return cov


def main():
    print("=" * 78)
    print("A 基线：不 tick")
    m, cb = base()
    warm(m)
    c0 = report("A 基线（24 万步，不注入 tick）", m, cb)

    print("\n" + "=" * 78)
    print("B 接上 tick：一直扫到没有槽挂着指针")
    ev = []

    def on_event(t, name, r):
        ev.append((t, name, r))
        print("  第 %-4d 圈 %-3s 0x%05X → 0x%05X  %6d 步  覆盖 +%-4d%s"
              % (t, name, r["cb"], r["next"], r["steps"], r["delta"],
                 "  （交权给调度器）" if r["cut"] else ""))

    cb.chain(400, on_event=on_event)
    c1 = report("B tick 完之后", m, cb, c0)

    print("\n  逐站：")
    for t, name, r in ev:
        print("    第 %-4d 圈 %s：0x%05X → 0x%05X（%d 步 / +%d）"
              % (t, name, r["cb"], r["next"], r["steps"], r["delta"]))

    print("\n" + "=" * 78)
    print("C 再让系统自己跑 12 万步（不 tick）")
    warm(m, rounds=24)
    c2 = report("C 空转之后", m, cb, c1)

    print("\n" + "=" * 78)
    print("D 全开 dormant 任务 + tick")
    m2, cb2 = base(boot_all=True)
    warm(m2)
    d0 = m2.cov.words()
    print("  起点：%s" % cb2.summary())
    ev2 = []

    def on_event2(t, name, r):
        ev2.append((t, name, r))
        print("  第 %-4d 圈 %-3s 0x%05X → 0x%05X  %6d 步  覆盖 +%-4d%s"
              % (t, name, r["cb"], r["next"], r["steps"], r["delta"],
                 "  （交权给调度器）" if r["cut"] else ""))

    cb2.chain(400, on_event=on_event2)
    c3 = report("D 全开 + tick", m2, cb2, d0)

    print("\n" + "=" * 78)
    print("E 换一种预热口径：一次性 fast 6 万步（不注中断）再 tick")
    m3, cb3 = base()
    m3.run(max_steps=60000)
    e0 = m3.cov.words()
    print("  起点：覆盖 %d  %s" % (e0, cb3.summary()))
    print("  现场：%s" % " ".join(
        "%d(%s)" % (t["id"], t["wait_kind"])
        for t in sorted(realos.states(m3), key=lambda x: x["id"]) if t["id"]))
    ev3 = []

    def on_event3(t, name, r):
        ev3.append((t, name, r))
        print("  第 %-4d 圈 %-3s 0x%05X → 0x%05X  %6d 步  覆盖 +%-4d%s"
              % (t, name, r["cb"], r["next"], r["steps"], r["delta"],
                 "  （交权给调度器）" if r["cut"] else ""))

    cb3.chain(400, on_event=on_event3)
    c4 = report("E fast 口径 + tick", m3, cb3, e0)

    print("\n" + "=" * 78)
    print("总账")
    print("  A 基线                %6d" % c0)
    print("  B tick（+链）          %6d  (+%d)" % (c1, c1 - c0))
    print("  C 再空转              %6d  (+%d)" % (c2, c2 - c1))
    print("  D 全开+tick           %6d  (+%d)" % (c3, c3 - d0))
    print("  E fast 口径 + tick    %6d  (+%d)" % (c4, c4 - e0))
    print("  B 场景走了 %d 站，D 场景走了 %d 站，E 场景走了 %d 站"
          % (len(ev), len(ev2), len(ev3)))


if __name__ == "__main__":
    main()
