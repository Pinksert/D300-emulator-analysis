# -*- coding: utf-8 -*-
"""生成 session.txt 与 autospin-session.txt：脚本化跑 Monitor，输出即原始实录。

这样这两份文件就不再是手抄的 —— 代码一改，重跑一遍就知道哪里对不上。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create, MACHINES
from frbox.monitor import Monitor


def play(title, factory, cmds, load=True):
    if title:
        print()
        print("=== %s ===" % title)
    m = factory()
    if load:
        m.load()
    mon = Monitor(m, out=lambda s="": print(s))
    for c in cmds:
        mon.execute(c)


def section(t):
    print()
    print("--- %s ---" % t)


def plain_session():
    print("=== 1) 两台机器 ===")
    print("可用机器：")
    for name in sorted(MACHINES):
        print("  %-8s %s" % (name, getattr(MACHINES[name], "desc", "")))

    section("2) A 段：跑到时钟锁定、启动链尽头 0xFE50C 的 reti")
    play("", lambda: create("d300-a"),
         ["bp 0x000FE50C", "cont 200000", "info irq", "info rom"], load=True)

    section("3) B 段：跑到复位链尽头 0x407D8")
    play("", lambda: create("d300-b"),
         ["bp 0x000407D8", "cont 400000", "info irq", "info rom"], load=True)

    section("4) A 段：手工拉起任务 1（跳过 REALOS 调度器）")
    play("", lambda: create("d300-a"),
         ["boottask 0x8133A::0:0x80506 table rp=0x81338",
          "info irq", "info rom"], load=True)

    section("5) 反汇编与内存")
    play("", lambda: create("d300-a"),
         ["x/8i 0x000804F0", "x/8x 0x000FE000"], load=True)


def autospin_session():
    print("=== frbox 0.9：自旋消除器（autospin） ===")

    section("1) 不开：卡在第一处忙等，PC 停在 0x0DCD2C 一带")
    m = create("d300-a")
    m.load()
    mon = Monitor(m, out=lambda s="": print(s))
    mon.execute("autospin off")
    mon.execute("boottask 0x8133A::0:0x80506 table rp=0x81338")
    mon.execute("cont 40000")
    for c in ("info irq", "info rom", "autospin status"):
        mon.execute(c)

    section("2) 开 autospin：三处全放行，任务正常返回停机占位")
    m = create("d300-a", autospin=True)
    m.load()
    mon = Monitor(m, out=lambda s="": print(s))
    mon.execute("autospin on")
    mon.execute("boottask 0x8133A::0:0x80506 table rp=0x81338")
    mon.execute("cont 200000")
    for c in ("info irq", "info rom", "autospin status"):
        mon.execute(c)

    section("3) 半路关掉：第 3 处忙等会重新把任务卡住")
    m = create("d300-a", autospin=True)
    m.load()
    mon = Monitor(m, out=lambda s="": print(s))
    mon.execute("autospin on")
    mon.execute("boottask 0x8133A::0:0x80506 table rp=0x81338")
    mon.execute("cont 20000")
    mon.execute("autospin status")
    mon.execute("autospin off")
    mon.execute("cont 100000")
    for c in ("info irq", "info rom", "autospin status"):
        mon.execute(c)


def irq_session():
    print("=== frbox 0.9：中断子系统 ===")
    m = create("d300-a")
    m.load()
    mon = Monitor(m, out=lambda s="": print(s))

    section("向量表（跑到第一个卡点后）")
    mon.execute("boottask 0x8133A::0:0x80506 table rp=0x81338")
    mon.execute("cont 20000")
    mon.execute("irq table")

    section("注入 vec 0x13 / 0x1B")
    mon.execute("irq fire 0x13")
    mon.execute("irq fire 0x1B")

    section("未用向量 0x1F 会走到停机占位")
    mon.execute("irq fire 0x1F")


which = sys.argv[1] if len(sys.argv) > 1 else "session"
if which == "session":
    plain_session()
elif which == "autospin":
    autospin_session()
else:
    irq_session()
