# -*- coding: utf-8 -*-
"""exp_bgate2.py —— V30：第二道门（0x9F58D450 的字节 bit6）。

A 引用分类：状态字 A / B 各有几处读、几处写
B A 的唯一写点（0x21B33A）在哪、跑不跑得到
C 第二道门是哪一位（字节 0x01..0x80 扫描）
D 放行之后多走到哪（新增地址 + 0x12E6F8 解剖）
E 三个状态的口径对照（门关 / 只开门 / 门+第二道门）
F 结论与边界

跑法：cd emu && python probe/exp_bgate2.py > ../results/frbox/bgate2.txt
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
from frbox import brealos, bgate, bflag

A = bgate.STATUS          # 0x9F58D450
B = bgate.STATUS2         # 0x9F58D454
BOOT = 191520


def head(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def assembled(steps=200000):
    m = create("d300-b")
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    n = 0
    while not m.cpu.halted and n < BOOT:
        m.step(1)
        n += 1
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=steps, spin_guard=False)
    return m


# ------------------------------------------------------------------ A
head("A 引用分类：状态字 A / B 各有几处读、几处写")

m = create("d300-b")
m.load()
for addr, name in ((A, "A = 0x9F58D450"), (B, "B = 0x9F58D454")):
    rows = bgate.access(m, addr)
    c = Counter(k for _, k, _ in rows)
    print("%s：%d 处引用 → %s" % (name, len(rows), dict(c)))
    for pc, kind, f in rows:
        if kind != "test":
            print("    %08X %-6s %s" % (pc, kind, f))
    print()

# ------------------------------------------------------------------ B
head("B A 的唯一写点：0x21B33A 的条件清位")

print("0x21B32A - 0x21B350：")
for ln in m.disasm_at(0x21B32A, 12).splitlines():
    print("   " + ln)
print()
print("读法：先测 [A] 的某一位（btstl #8），置位时**清掉**另一位（bandl #7）。")
print("      这个函数在 0x21Bxxx 页 —— 我们的场景从来没执行到。")

# ------------------------------------------------------------------ C
head("C 第二道门是哪一位：字节扫描")

m0 = assembled()
prev0 = bytes(m0.cov.buf)
c0 = m0.cov.words()
bgate.open_gate(m0)
m0.run(max_steps=150000, spin_guard=False)
base_cov = m0.cov.words() - c0
cur0 = bytes(m0.cov.buf)
base_new = {m0.cov.base + (i << 1) for i in range(m0.cov.n) if cur0[i] and not prev0[i]}
print("门开着、状态字不动：覆盖 +%d" % base_cov)
print()
print("  字节 A+0 值   覆盖+   新增地址")
for val in (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80):
    mm = assembled()
    c_start = mm.cov.words()
    before = bytes(mm.cov.buf)
    bgate.open_gate(mm)
    mm.bus.wr(A, 1, val)
    mm.run(max_steps=150000, spin_guard=False)
    after = bytes(mm.cov.buf)
    new = {mm.cov.base + (i << 1) for i in range(mm.cov.n) if after[i] and not before[i]}
    extra = sorted(new - base_new)
    print("   0x%02X       %4d    %s" % (val, mm.cov.words() - c_start,
                                        " ".join("%X" % a for a in extra[:8]) or "—"))
print()
print("→ 只有 **0x40**（字节 bit6，字 0x40000000）会放行。")

# ------------------------------------------------------------------ D
head("D 放行之后多走到哪")

mm = assembled()
c_start = mm.cov.words()
before = bytes(mm.cov.buf)
bgate.open_gate(mm)
mm.bus.wr(A, 1, bgate.STATUS_WORK_READY)
mm.run(max_steps=150000, spin_guard=False)
after = bytes(mm.cov.buf)
new = sorted({mm.cov.base + (i << 1) for i in range(mm.cov.n) if after[i] and not before[i]})
print("相对开门前：覆盖 +%d、新增 %d 个地址（其中相对「只开门」多 7 个）"
      % (mm.cov.words() - c_start, len(new)))
print("   " + " ".join("%X" % a for a in new))
print("   结束 PC=%08X，位模式 flag3=%08X" % (mm.cpu.pc, bflag.pattern(mm, 3)))
print()
print("主循环这一段的代码（0x120184 - 0x1201AA）：")
for ln in m.disasm_at(0x120184, 10).splitlines():
    print("   " + ln)
print()
print("0x12E6F8 - 0x12E745（被调进去的函数）：")
for ln in m.disasm_at(0x12E6F8, 27).splitlines():
    print("   " + ln)
print()
print("读法：按参数 r4（0x0B）算出槽位 0x9F58D508 + r4*0x0C → wai_sem(-53) →")
print("      测槽位 +9 的某位 → 调 0x124F5A（小拷贝）→ borh 置位 → set_flg(-48)。")

# ------------------------------------------------------------------ E
head("E 三个状态的口径对照")

rows = []
for label, gate, a_byte in (("门关（基线）", False, None),
                            ("只开门", True, None),
                            ("门 + 第二道门", True, bgate.STATUS_WORK_READY)):
    mm = assembled()
    c_start = mm.cov.words()
    n_start = len(mm.flow.calls)
    if gate:
        bgate.open_gate(mm)
    if a_byte is not None:
        mm.bus.wr(A, 1, a_byte)
    mm.run(max_steps=300000, spin_guard=False)
    rows.append((label, mm.cov.words() - c_start, len(mm.flow.calls) - n_start,
                 mm.cpu.pc, len(mm.expeed.writes_to(0x76))))
print("  %-16s 覆盖+   调用+   结束PC     写76次数" % "状态")
for label, dc, dn, pc, w in rows:
    print("  %-16s %5d  %6d  %08X   %d" % (label, dc, dn, pc, w))
print()
print("读法：门只放行「看一眼状态字」，第二道门放行才真的调进 0x12E6F8 的实质部分。")
print("      三档都没有第二次写 0x76 —— 更深的路（0x1204xx）还没打开。")

# ------------------------------------------------------------------ F
head("F 结论与边界")

print("能报：")
print("  * 状态字 A（0x9F58D450）30 处引用里 26 处是位测试、1 处是条件清位")
print("    （0x21B33A，在从未执行的 0x21Bxxx 页），另有 3 处非直接访问")
print("  * 状态字 B（0x9F58D454）44 处引用里 23 处是位操作（清/置）——")
print("    它是固件自己维护的位图，不是纯硬件状态")
print("  * 第二道门 = **字节 A+0 的 bit6**（0x40 / 字 0x40000000）：")
print("    置上它，主循环才从 0x12018C 的 beq 落到 0x12018E，清 B 的 bit11 并 call 0x12E6F8")
print("  * 放行后多执行 7 个地址（0x12018E/194/196/19C/19E + 0x12E73E/740），")
print("    说明 0x12E6F8 里那段「测槽位 → 调 0x124F5A → 置位 → set_flg」真的跑了")
print("  * 0x12E6F8 的槽位算法：0x9F58D508 + r4*0x0C（r4 = 0x0B）")
print("不能报：")
print("  * A 的 bit6 在真实机器上由谁置位（固件只有一处条件清位，没有置位点）")
print("  * 0x12E6F8 的槽位里装的是什么、0x124F5A 那次拷贝拷了什么")
print("  * 更深的 0x1204xx 那段（写 0x76 的重复路径）仍然没打开")
print("  * 「主循环通了」—— 只是多走 7 个地址")
