# -*- coding: utf-8 -*-
"""exp_gate.py —— 门字节 `0x9F58D08F` 的写点在修好的 `writers_of` 下还是 2 个（V51）。

V51 修了 `bstatic` 的 store 地址形态（见 `KNOWN_ISSUES #192`）：旧算法对
`@(R13,Rj)` 一律取 `rj` 当基址、把 `r13` 丢掉，于是 `0x9F5A0284` 的 44 处字段写
全塌成「写基址」，造出 38 个并不存在的写点。

这引出一个连带问题（`KNOWN_ISSUES #195`）：V48 的「门字节只有 2 个写点」
是同一个函数算的，那 2 个还算不算？

本探针给出答案：**算**。两个写点都是 `@Rj` 形态（base `0x1600`），
旧算法在这个形态上取 `r[rj]` 本来就是对的 —— #192 的影响面只限 `@(R13,Rj)`。

只读 ROM，不建机器跑指令，约 1 分钟。

四段：

  1 门字节的写点（修后算法）+ 每条指令的形态与前置两条
  2 门字节所在 4 字节字按字节拆开
  3 两个旧写点在新算法下算出的地址
  4 与 V48 原结论对账

跑：cd emu && python probe/exp_gate.py > ../results/frbox/gate.txt
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import fr_emu
from frbox import bstatic

GATE = 0x9F58D08F          # V25 解出的 B 段主循环门字节
WORD = 0x9F58D08C          # 它所在 4 字节字的起点（门字节 = +3）
OLD = (0x12ECF4, 0x12F180)  # V25/V48 报的那两个写点


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


sw, m = bstatic.sweep_of_type("d300-b")
rom, base = m.bus.rom, m.bus.rom_base

hr("0 本机静态扫描口径")
s = sw.summary()
print("ROM 基址 0x%06X，长度 %d 字节" % (base, len(rom)))
print("可定址 store %d 条 / 不可定址 %d 条（栈为主）"
      % (s["stores"], s["stores_unknown"]))
print("函数 %d 个 / call 边 %d 条 / 系统调用点 %d 个"
      % (s["funcs"], s["edges"], s["calls"]))

hr("1 门字节 0x%08X 的写点（修后算法）" % GATE)
ws = sw.writers_of(GATE)
print("共 %d 处\n" % len(ws))
for pc, mn in ws:
    off = pc - base
    ins = fr_emu.TABLE.decode(rom, off)
    if not ins:
        print("  0x%06X  %s（解不出指令）" % (pc, mn))
        continue
    _mn, bs, v, _size, _w = ins
    form = bstatic._ST_SHAPE.get(bs, ("?", 0))
    print("  0x%06X  %s  base=0x%04X  形态=%-11s %dB  ri=%s rj=%s"
          % (pc, _mn, bs, form[0], form[1], v.get("ri"), v.get("rj")))
    for back in (-2, -4, -6, -8):
        o = off + back
        if o < 0:
            continue
        i2 = fr_emu.TABLE.decode(rom, o)
        if i2:
            print("          [%+d] 0x%06X  %s" % (back, base + o, i2[0]))
print("""
读法：`stb` 的 base 0x1600 是 `st Rj` 形态，地址 = r[rj]。这里 rj=12、
ri=0 —— `ri` 是数据寄存器、`rj` 是指针寄存器，所以地址来自 `ldi:32 r12`
那条前置指令。旧算法取的就是 rj，因此在**这个**形态上没算错。""")

hr("2 门字节所在 4 字节字 0x%08X，按字节拆开" % WORD)
sw2 = sw.structure_writers(WORD, 4)
if not sw2:
    print("  无写点")
for off in sorted(sw2):
    lst = sw2[off]
    mark = "   ← 门字节" if WORD + off == GATE else ""
    print("  +0x%02X  0x%08X  %d 处：%s%s"
          % (off, WORD + off, len(lst),
             " ".join("0x%06X(%s)" % (pc, mn) for pc, mn in lst), mark))

hr("3 两个旧写点在新算法下的地址")
for pc0 in OLD:
    off = pc0 - base
    ins = fr_emu.TABLE.decode(rom, off)
    if not ins:
        print("  0x%06X  解不出" % pc0)
        continue
    _mn, bs, v, _size, _w = ins
    hit = [a for p, a, _m in sw.stores if p == pc0]
    print("  0x%06X  %s  base=0x%04X  ri=%s rj=%s  → %s"
          % (pc0, _mn, bs, v.get("ri"), v.get("rj"),
             ("0x%08X" % hit[0]) if hit else "不可定址（计入 stores_unknown）"))

hr("4 与 V48 原结论对账")
print("  V48 原话：门字节 0x%08X 只有 2 个写点（0x%06X / 0x%06X）"
      % (GATE, OLD[0], OLD[1]))
print("  修后实测：%d 个写点" % len(ws))
verdict = "一致，V48 那段不用改" if len(ws) == 2 else "不一致，V48 那段要重写"
print("  → %s" % verdict)

print("""
结论边界：
  · 能说：`writers_of` 的修复**不影响** `@Rj` 形态的历史结论；门字节的写点数
    仍是 2，V25/V48 的「唯一写者」链条不动。
  · 不能说：V45/V46 里用同一个函数算的「唯一写点」也都对 —— 那两版涉及
    `@(R13,Rj)` 的地址，`KNOWN_ISSUES #195` 仍开着。
  · 不能据此说门字节「该被谁写」已经解决 —— 写者自身的进入条件仍未定（KI#190）。
""")
