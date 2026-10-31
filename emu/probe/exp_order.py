# -*- coding: utf-8 -*-
"""1.10 / 1.11 真调用对照：从函数真实入口 0x000DB8EA 跑，记录按时间顺序的写入，
回答「1.11 到底改了什么顺序」。
"""
import sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")


sys.stdout.reconfigure(encoding="utf-8")
from fr_run import Runner

FW = r"C:\Users\admin\WorkBuddy\D300固件\D300Update"
BASE = 0x00080000
ENTRY = 0x000DB8EA
NAMES = {0x0003FAE9: "状态A高半字节", 0x0003FAF1: "状态B bit1", 0x0003F81C: "开关字节",
         0x0003F810: "字节", 0x0003F81F: "标志", 0x0004276C: "表[0]", 0x0004274E: "表[1]",
         0x00045758: "输出"}


def run_one(ver, flag):
    rom = open(f"{FW}\\A30001{ver}.bin", "rb").read()
    r = Runner(rom, BASE, depth_limit=5)
    r.m.wr(0x0004276C, 2, 0x0100)
    r.m.wr(0x0004274E, 2, 0x0080)
    r.m.wr(0x0003F81C, 1, flag)
    r.m.all_writes.clear()
    r.run(ENTRY, max_steps=500000)
    ws = [(pc, a, n, v) for (a, n, v, pc) in r.m.all_writes]
    calls = [c.target for c in r.root.children]
    return r, ws, calls


print(f"{'版本':<6}{'0x3F81C':<9}{'步数':<7}{'退出':<12}{'顶层调用顺序'}")
rows = {}
for ver in ("10", "11"):
    for flag in (0x00, 0x40):
        r, ws, calls = run_one(ver, flag)
        rows[(ver, flag)] = (r, ws, calls)
        seq = " → ".join(f"{c:06X}" for c in calls)
        print(f"1.{ver[1]:<5}0x{flag:02X}     {r.cpu.steps:<7}{r.ret_reason:<12}{seq}")

print("\n写入序列（按发生顺序，只列与镜头状态有关的地址）")
for (ver, flag), (r, ws, calls) in rows.items():
    print(f"\n--- 1.{ver[1]}  0x3F81C=0x{flag:02X}  共 {len(ws)} 次写 ---")
    for pc, a, n, v in ws:
        if a in NAMES or 0x3F800 <= a <= 0x3FAFF:
            print(f"  0x{pc:08X} → [0x{a:08X}] = 0x{v:0{n*2}X}   {NAMES.get(a, '')}")
