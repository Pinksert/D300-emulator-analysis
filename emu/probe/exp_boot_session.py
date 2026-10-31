# -*- coding: utf-8 -*-
"""生成 boot-session.txt：用 Monitor 跑一段脚本化的会话，输出就是原始实录。

比手抄可靠：只要这份脚本不变，任何人重跑都能得到同一段文字。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox.monitor import Monitor


def _kernel(auto):
    def factory():
        m = create("d300-a", autospin=True)
        m.attach_kernel(auto_event=auto)
        return m

    return factory


def session(title, factory, cmds):
    print()
    print("--- %s ---" % title)
    m = factory()
    m.load()
    mon = Monitor(m, out=lambda s="": print(s))
    for c in cmds:
        mon.execute(c)


print()
print("=== frbox 0.9：REALOS 任务表 + μITRON 半主机（probe/exp_boot_session.py 生成） ===")

session("1) 任务表",
        lambda: create("d300-a"),
        ["tasks"])

session("2) 真内核，不送事件：停在第 1 个握手",
        _kernel(False),
        ["boottask 0x8133A::0:0x80506 table rp=0x81338",
         "cont 200000",
         "info registers"])

session("3) 内核 + 自动送事件：跑完整条启动链",
        _kernel(True),
        ["boottask 0x8133A::0:0x80506 table rp=0x81338",
         "cont 200000",
         "info",
         "svc"])

session("4) 手动送事件 + resume",
        _kernel(False),
        ["boottask 0x8133A::0:0x80506 table rp=0x81338",
         "cont 200000",
         "flag set 3=0x2000",
         "resume 2000000",
         "info"])

print()
print("注：第 4 段 resume 之后仍会停在下一个握手上 —— 这正是「没有调度器、")
print("    只能一条条补事件」的直接体现（KNOWN_ISSUES 第 35 条）。")
