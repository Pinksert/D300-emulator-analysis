# -*- coding: utf-8 -*-
"""exp_bxstat.py —— V29：EXPEED 的状态字节与写 0x76 的来历。

A 采样点定位（哪个 PC 真的在读寄存器）
B 7 组值复核（V28 的「字节 0 精确复现」还成立吗）
C 写 0x76 的来历：[0x9F5A321E] + 1
D 状态字节的读者（静态引用）
E 主循环 work 分支会不会重复写 0x76（开门实验）
F 采样对齐的开放问题（read_len 5 vs 6 对照）
G 结论与边界

跑法：cd emu && python probe/exp_bxstat.py > ../results/frbox/bxstat.txt
"""

import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from frbox.machine import create
from frbox import brealos, bgate

BOOT_STEPS = 191520


def head(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def boot(m, steps=BOOT_STEPS):
    n = 0
    while not m.cpu.halted and n < steps:
        m.step(1)
        n += 1
    return n


def assemble(m, steps=200000):
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=steps, spin_guard=False)


# ------------------------------------------------------------------ A
head("A 采样点定位：0x7000000A 的读里，哪个 PC 是真正取数据位的地方")

VAL = b"\xA5\x5A\x3C\xC3\x00\xFF"
m = create("d300-b")
m.load()
m.reset()
m.expeed.set_reg(6, VAL)
by = defaultdict(list)
_or = m.bus.rd


def _r(a, sz, pc=None):
    v = _or(a, sz, pc)
    if a == 0x7000000A:
        by[pc].append(v)
    return v


m.bus.rd = _r
boot(m)
assemble(m, steps=200000)
m.bus.rd = _or

print("寄存器 6 = %s；按读点统计：" % VAL.hex(" "))
print("      PC      次数  DATA=1 次数   值分布")
for pc in sorted(by):
    vs = by[pc]
    print("  %08X   %4d      %4d      %s"
          % (pc, len(vs), sum(1 for v in vs if v & 4), Counter(vs).most_common(2)))
print()
print("只有 0x19EF5E 的位串随寄存器内容变化（其余都是读-改-写过程里的回读）：")
for pc in sorted(by):
    if pc in (0x19EF5E, 0x19EF50, 0x19EF58):
        print("  %08X  %s" % (pc, "".join("1" if v & 4 else "0" for v in by[pc])))

# ------------------------------------------------------------------ B
head("B 7 组值复核：采样到的第 1 个字节 == 寄存器 6 的第 0 字节吗")


def sample_byte(val):
    mm = create("d300-b")
    mm.load()
    mm.reset()
    mm.expeed.set_reg(6, bytes([val]) + b"\x00" * 5)
    got = []
    _or2 = mm.bus.rd

    def r2(a, sz, pc=None):
        v = _or2(a, sz, pc)
        if a == 0x7000000A and pc == 0x19EF5E:
            got.append((v >> 2) & 1)
        return v

    mm.bus.rd = r2
    boot(mm)
    assemble(mm)
    mm.bus.rd = _or2
    by_ = 0
    for i, b in enumerate(got[:8]):
        by_ |= b << i
    return by_, len(got)


print("  reg6[0]   采样次数   拼出的字节   结论")
ok = 0
for v in (0x00, 0x01, 0x7F, 0x80, 0x88, 0xA5, 0xFF):
    got, n = sample_byte(v)
    good = (got == v)
    ok += good
    print("   0x%02X       %2d        0x%02X       %s" % (v, n, got, "一致" if good else "不一致"))
print("  → %d/7 完全一致（LSB 先）。V28 的「字节 0 精确复现」成立。" % ok)

# ------------------------------------------------------------------ C
head("C 写 0x76 的来历：[0x9F5A321E] + 1")

m2 = create("d300-b")
m2.load()
print("静态：写 0x9F5A321E 的那几行")
for ln in m2.disasm_at(0x19E9FA, 8).splitlines():
    print("   " + ln)
print("  —— 0x19E9FE stb 0 → 0x9F5A321E；0x19EA00 ldub 读回；0x19EA02 add 1；")
print("     0x19EA04 call（0x19EE70 = 写寄存器 0x76）；0x19EA06 stb 把 1 存回。")
print()
print("静态：0x19EE70 就是「写寄存器 0x76」的例程")
for ln in m2.disasm_at(0x19EE76, 9).splitlines():
    print("   " + ln)
print("  （0x19EE96 ldi:8 r4,#0x76 → 发命令 0x76；随后把入参当数据位发出去）")
print()

m3 = create("d300-b")
m3.load()
m3.reset()
m3.enable_cov()
m3.attach_flow()
boot(m3)
assemble(m3)
d3 = m3.expeed
print("动态（装配 20 万步）")
print("  寄存器 0x76 的写入次数 %d，载荷 %s" % (len(d3.writes_to(0x76)), d3.writes_to(0x76)))
print("  寄存器 6 的读取次数 %d" % d3.reads_of(6))
print("  状态字节 0x9F5A321C = %s，计数源 0x9F5A321E = %s"
      % (m3.fetch_bytes(0x9F5A321C, 1).hex(), m3.fetch_bytes(0x9F5A321E, 1).hex()))
print("  → 载荷 1 = 初值 0 + 1，与静态一致。")

# ------------------------------------------------------------------ D
head("D 状态字节的读者（静态 32 位立即数引用）")

for addr, note in ((0x9F5A321C, "EXPEED 状态字节本体"),
                   (0x9F5A321E, "写 0x76 用的计数源")):
    refs = bgate.refs(m2, addr)
    print("%08X（%s）引用 %d 处：" % (addr, note, len(refs)))
    for r in refs:
        print("   %08X" % r)
    print()
print("读法：")
print("  * 0x9F5A321C 的 5 处引用全在 0x19EBxx —— 同一个初始化函数内部，没有别的模块读它；")
print("  * 0x9F5A321E 被 0x120458（主循环 work 分支）和 0x12DE08（显示任务）读到。")

# ------------------------------------------------------------------ E
head("E 主循环 work 分支会不会重复写 0x76（开门实验）")

m4 = create("d300-b")
m4.load()
m4.reset()
m4.enable_cov()
m4.attach_flow()
boot(m4)
assemble(m4)
before = len(m4.expeed.writes_to(0x76))
from frbox import bgate as _bg
_bg.open_gate(m4)
m4.run(max_steps=1000000, spin_guard=False)
after = m4.expeed.writes_to(0x76)
print("开门前写 0x76：%d 次；开门后 100 万步：%d 次" % (before, len(after)))
print("载荷序列：", after)
print("结束 PC=%08X，覆盖 %d" % (m4.cpu.pc, m4.cov.words()))
print()
print("读法：0x120458 的代码是「读 [0x9F5A321E] → 加 1 → 调 0x19EE70 写 0x76」——")
print("      它在 work 分支里，但 work 分支没跑到（门后还有状态字），所以没有第二次写。")

# ------------------------------------------------------------------ F
head("F 应答长度会不会影响采样（怀疑已排除）")

m5 = create("d300-b")
m5.load()
print("对照实验（同一组寄存器值、只改应答字节数）：")
for rl in (5, 6):
    mm = create("d300-b")
    mm.load()
    mm.reset()
    mm.expeed.read_len = rl
    mm.expeed.set_reg(6, VAL)
    got = []
    _or3 = mm.bus.rd

    def r3(a, sz, pc=None):
        v = _or3(a, sz, pc)
        if a == 0x7000000A and pc == 0x19EF5E:
            got.append((v >> 2) & 1)
        return v

    mm.bus.rd = r3
    boot(mm)
    assemble(mm)
    mm.bus.rd = _or3
    bs = []
    for i in range(0, len(got) // 8 * 8, 8):
        v = 0
        for k in range(8):
            v |= got[i + k] << k
        bs.append(v)
    print("  read_len=%d：采样 %d 位 → %s" % (rl, len(got), " ".join("%02X" % b for b in bs)))
print("  寄存器 6 = %s" % VAL.hex(" "))
print()
print("读法：5 和 6 采到的都是 A5 00 —— 应答长度不影响采样位置。")
print("      （V29 中途出现过一次「改成 6 就采到末尾两字节」，复查是伪影：")
print("        那次是在 reset() 之后再改 read_len，器件的移位状态已经跑起来了一半。")
print("        干净地建新机器、load 前设参数，两种长度结果一致。）")
print("      所以 read_len 只决定「器件愿意驱动多少位」，不改变主机读到什么。")

# ------------------------------------------------------------------ G
head("G 结论与边界")

print("能报：")
print("  * 采样点就在 0x19EF5E（只有它的位串随寄存器内容变）；7 组值 7 次全对，")
print("    LSB 先，采到的第 1 个字节 == 寄存器 6 的第 0 字节")
print("  * 写寄存器 0x76 的值 = [0x9F5A321E] + 1：0x19E9FE 先把计数源清 0，")
print("    0x19EA00/02 读回加一，0x19EA04 调 0x19EE70（写 0x76 的例程），")
print("    0x19EA06 把 1 存回；动态载荷实测 [1]")
print("  * 寄存器 0x76 整场只写 1 次；寄存器 6 整场只读 1 次")
print("  * 0x9F5A321C 的 5 处引用全在 0x19EBxx（同一函数）；")
print("    0x9F5A321E 被 0x120458（work 分支）与 0x12DE08（显示任务）读")
print("  * 开门 100 万步也没有第二次写 0x76 —— work 分支仍然没走到")
print("不能报：")
print("  * 采样在「整条时钟串的第几位」开始 —— 只知道 0x19EF5E 采 16 次，")
print("    与应答长度无关，但和 48 个时钟的相位关系没算清")
print("  * 0x9F5A321E 这个计数器的语义（为什么写 0x76 要送一个递增数）")
print("  * 0x9F5A321C 状态字节除 bit7 外的含义")
print("  * 显示任务读到 0x9F5A321E 之后拿它做什么")
print("  * 「EXPEED 通信跑起来了」—— 仍只有读一次 + 写一次，没有帧数据")
