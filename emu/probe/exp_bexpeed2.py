# -*- coding: utf-8 -*-
"""exp_bexpeed2.py —— V28：EXPEED 器件模型与寄存器 6。

A 器件侧命令日志 / B 帧结构修正（V27 的抓包 bug）/ C 引脚时序三条铁律
D 寄存器 6 的落点与分支 / E 逐位矩阵 / F 两条路径的覆盖差异 / G 结论与边界

跑法：cd emu && python probe/exp_bexpeed2.py > ../results/frbox/bexpeed2.txt
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from frbox.machine import create
from frbox import brealos, bgate, bexpeed

BOOT_STEPS = 191520


def head(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def boot_to(m, steps=BOOT_STEPS):
    """跑 boot 到复位后的空闲点（不 sta_tsk）。"""
    n = 0
    while not m.cpu.halted and n < steps:
        m.step(1)
        n += 1
    return n


def assemble(m, steps=400000):
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=steps, spin_guard=False)


# ------------------------------------------------------------------ A
head("A 器件侧命令日志：主机对 EXPEED 说了什么")

m = create("d300-b")
m.load()
m.reset()
m.enable_cov()
m.attach_flow()
boot_to(m)
assemble(m)
d = m.expeed.summary()
print("器件收到的命令 %d 条：" % len(d["cmds"]))
for c in d["cmds"]:
    print("    %-5s 寄存器 0x%02X" % (c["kind"], c["reg"]))
print("主机打进来的位 %d，器件驱动回去的位 %d" % (d["bits_in"], d["bits_out"]))
print("寄存器文件：", {k: v.hex(" ") for k, v in sorted(m.expeed.regs.items())} or "（空，全 0）")

# ------------------------------------------------------------------ B
head("B 帧结构修正：V27 报的「2 帧 / 0 条写命令」是抓包器 bug")

m2 = create("d300-b")
tap = bexpeed.ExpeedTap().attach(m2)
m2.load()
m2.reset()
boot_to(m2)
assemble(m2, steps=200000)
tap.detach()
s = tap.summary()
print("按 CS 上升沿分帧 + 只数 CLK 上升沿 + 收尾 flush：")
print("  窗口数 %d，各位数 %s" % (s["frames"], s["bits"]))
for c in s["cmds"]:
    print("    %-5s reg=%-5s payload=%s" % (
        c["kind"], hex(c["reg"]) if c["reg"] is not None else "-", c["payload"].hex(" ")))
print("  端口配置写（非位串行）%d 次" % s["cfg_writes"])
print()
print("V27 的三个 bug：")
print("  1) 在 CS **下降沿**收帧 → 数据相位那一窗和后面的写命令并成一帧")
print("  2) 按「写了 bit1 就算一位」计数 → 读-改-写会把同一位数两三次（19 vs 16）")
print("  3) 收尾那一帧没有 flush → 最后一条写命令 0x76 整个丢掉")
print("修正后：读寄存器 6 → 48 位数据相位 → **写寄存器 0x76 = 0x01**（真写命令）")

# ------------------------------------------------------------------ C
head("C 引脚时序三条铁律（都是踩坑踩出来的）")

LAB = r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp\gh_upload\D300-emulator-v2\data\B3000111.bin"
m3 = create("d300-b")
m3.load()
regs = []
_ow = m3.bus.wr
seq = []


def _w(a, sz, v, pc=None):
    if a == 0x7000000A:
        seq.append((pc or 0, v))
    return _ow(a, sz, v, pc)


m3.bus.wr = _w
m3.reset()
boot_to(m3)
assemble(m3, steps=200000)
m3.bus.wr = _ow

print("位引擎 0x19EF0C–0x19EF2A 每位的写序列（前 24 笔）：")
print("   " + "  ".join("%X:%02X" % (pc & 0xFFFFFF, v) for pc, v in seq[:24]))
print()
print("  铁律 1：DATA 是**粘滞**的 —— 主机先单独写 0x04 / 0x00，再用 0x02 打时钟脉冲；")
print("          那一笔不携带 DATA 信息，不能把 DATA 清掉（按值覆盖会解出全 0）。")
print("  铁律 2：只在 CLK **上升沿**采样 —— 主机的读-改-写会把 CLK=1 写两三次。")
print("  铁律 3：帧边界在 CS **上升沿**（一次选中 = 一位宽）；最后一个窗口要 flush。")

# ------------------------------------------------------------------ D
head("D 寄存器 6 的读回值：落点、编码、分支")

def run(reg6, label, steps=400000):
    mm = create("d300-b")
    if reg6 is not None:
        mm.expeed.set_reg(6, reg6)
    mm.load()
    mm.reset()
    mm.enable_cov()
    mm.attach_flow()
    boot_to(mm)
    assemble(mm, steps=steps)
    print("  [%-12s] 覆盖 %-5d 调用 %-5d 结束 PC=%08X  状态字节[0x9F5A321C]=%s"
          % (label, mm.cov.words(), len(mm.flow.calls), mm.cpu.pc,
             mm.fetch_bytes(0x9F5A321C, 2).hex(" ")))
    return mm


print("寄存器 6 的字节 0 是「状态字节」，会被原样存到 0x9F5A321C；")
print("bit7 是特例：置位时不存读回值，而是存常量 0x88。")
print()
m_none = run(None, "不设(全 0)")
run(b"\x01" + b"\x00" * 5, "01 00 00 00 00")
run(b"\x20" + b"\x00" * 5, "20 00 00 00 00")
run(b"\x7f" + b"\x00" * 5, "7F 00 00 00 00")
m_hi = run(b"\x80" + b"\x00" * 5, "80 00 00 00 00")
run(b"\x88" + b"\x00" * 5, "88 00 00 00 00")
run(b"\xff" * 6, "FF × 6")
run(b"\x00\xff" + b"\x00" * 4, "00 FF 00 00 00")
print()
print("静态对照：")
print("  0x19E9AC  ldi:8 r0, #0x88     ← bit7 分支把常量装进 r0")
print("  0x19E9B0  stb   r0, @(r11)    ← 写 0x9F5A321C（实测值 0x88）")
print("  0x19E9CC  stb   rX, @(r11)    ← 另一条分支存读回字节（实测 0x20/0x01/0x00）")

# ------------------------------------------------------------------ E
head("E 逐位矩阵：哪个位真的改变行为")

print("  reg6 字节0   覆盖   调用   结束 PC    状态字节")
for v in (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x7F, 0x80, 0x81, 0xFF):
    mm = create("d300-b")
    mm.expeed.set_reg(6, bytes([v]) + b"\x00" * 5)
    mm.load()
    mm.reset()
    mm.enable_cov()
    mm.attach_flow()
    boot_to(mm)
    assemble(mm)
    print("   0x%02X      %5d  %5d   %08X   %s"
          % (v, mm.cov.words(), len(mm.flow.calls), mm.cpu.pc,
             mm.fetch_bytes(0x9F5A321C, 1).hex()))
print()
print("读法：只有 **bit7** 会改变路径（覆盖 2632 → 2692）；其余位只改状态字节内容，")
print("      不改控制流。字节 1..5 对控制流没有影响。")

# ------------------------------------------------------------------ F
head("F 两条路径差在哪：覆盖差异地址")

def covset(mm):
    buf = bytes(mm.cov.buf)
    return {mm.cov.base + (i << 1) for i in range(mm.cov.n) if buf[i]}


c0, c1 = covset(m_none), covset(m_hi)
print("bit7=0 独有 %d 个地址；bit7=1 独有 %d 个地址" % (len(c0 - c1), len(c1 - c0)))
print("  bit7=1 新增（前 20）：", " ".join("%X" % a for a in sorted(c1 - c0)[:20]))
print("  bit7=0 新增（前 12）：", " ".join("%X" % a for a in sorted(c0 - c1)[:12]))
print("  按页统计 bit7=1 新增：", Counter(a >> 12 for a in (c1 - c0)).most_common(6))
print()
print("内存差异：")
WINS = [(0x9F580000, 0x00100000), (0x68000000, 0x00020000), (0x9F800000, 0x00040000)]
for base, size in WINS:
    a = bytes(m_none.fetch_bytes(base, size))
    b = bytes(m_hi.fetch_bytes(base, size))
    diffs = [i for i in range(len(a)) if a[i] != b[i]]
    print("  %08X 段（取样 %d 字节）差异 %d 处 %s"
          % (base, len(a), len(diffs),
             [(hex(base + i), "%02X→%02X" % (a[i], b[i])) for i in diffs[:6]]))

# ------------------------------------------------------------------ G
head("G 结论与边界")

print("能报：")
print("  * 主机对 EXPEED 一共说了三句：读寄存器 6 → 48 位数据相位 → 写寄存器 0x76 = 0x01")
print("    （V27 的「0 条写命令」是抓包 bug，本版修正）")
print("  * 位串行三条时序铁律：DATA 粘滞 / CLK 上升沿采样 / CS 上升沿分帧")
print("  * 寄存器 6 的字节 0 被固件存到 0x9F5A321C（0x19E9B0 / 0x19E9CC 两个存点）")
print("  * bit7 置位 → 存常量 0x88 并改走另一条初始化路径（覆盖 2632→2692、")
print("    终点从内核 0x406D6 变成主循环 0x1203B0）；其余位只改内容不改控制流")
print("  * 字节 1..5 对控制流无影响")
print("不能报：")
print("  * 寄存器 6 的其余 4 个字节是什么（值是死的，固件没比较过）")
print("  * 0x88 这个常量的含义（bit3 是什么状态未知）")
print("  * 写寄存器 0x76 = 1 的实际作用（没有回读、没有后续观察）")
print("  * 「EXPEED 已经能出画面」—— 显示链路仍只有这三句，没有帧数据")
print()
print("⚠️ 寄存器默认全 0 是「不知道」的占位，不是实测值；")
print("   0x88 常量与 bit7 分支是 1.11 的实测行为，换版本要重验。")
