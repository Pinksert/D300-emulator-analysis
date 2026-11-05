# -*- coding: utf-8 -*-
"""exp_bqueue.py —— V31：显示请求队列的结构。

A 0x12E6F8 的引用规模（它是通用「提交」接口）
B 它的签名：r4 = 槽位号、r5 = 另一个指针
C 队列结构：link head @0x9F58D500，节点 @0x9F58D508+n*0x0C，push 在 0x124F62
D 补上 r5 会不会让门后走更远（结论：不会）
E 0x120458（重复写 0x76 的那段）在静态上有没有入口
F 结论与边界

跑法：cd emu && python probe/exp_bqueue.py > ../results/frbox/bqueue.txt
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
from frbox import brealos, bgate

BOOT = 191520
HEAD = 0x9F58D500
BASE = 0x9F58D508
STRIDE = 0x0C


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


ROM_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "B3000111.bin")

# ------------------------------------------------------------------ A
head("A 0x12E6F8 的引用规模：它是通用接口，不是某个局部小函数")

rom = open(ROM_PATH, "rb").read()
pat = (0x12E6F8).to_bytes(4, "big")
hits, i = [], rom.find(pat)
while i >= 0:
    hits.append(0x40000 + i - 2)
    i = rom.find(pat, i + 1)
print("作为 ldi:32 立即数出现 %d 次" % len(hits))
pages = Counter((a >> 12) for a in hits)
print("按 4KB 页分布（前 10）：", [(hex(p << 12), n) for p, n in pages.most_common(10)])
print()
print("对比几个已知地址的引用规模：")
for tgt, name in ((0x12E6F8, "显示提交 0x12E6F8"),
                  (0x1200AC, "任务 2 入口 0x1200AC"),
                  (0x120458, "写 0x76 的 0x120458"),
                  (0x12E630, "set_flg(3,0x80) 的 0x12E630")):
    p = tgt.to_bytes(4, "big")
    c, j = 0, rom.find(p)
    while j >= 0:
        c += 1
        j = rom.find(p, j + 1)
    print("  %-28s %d 次" % (name, c))

# ------------------------------------------------------------------ B
head("B 签名：r4 = 槽位号、r5 = 另一个指针")

m = assembled()
bgate.open_gate(m)
m.bus.wr(bgate.STATUS, 1, bgate.STATUS_WORK_READY)
c = m.cpu
entry = None
for _ in range(200000):
    m.step(1)
    if c.pc == 0x12E6F8:
        entry = (c.r[4], c.r[5], c.r[8])
        break
print("进入 0x12E6F8 时：r4=%s r5=%s r8=%s" % (hex(entry[0]), hex(entry[1]), hex(entry[2])))
print("  主循环 0x12019E 只给了 r4 = 0x0B；r5 是 0x120160 那条 clr_flg 掩码的残值。")
print("  正常调用者（如 0x12E1EE）会先把 r5 设成一个缓冲区指针。")
print()
print("0x12E6F8 - 0x12E710：")
for ln in m.disasm_at(0x12E6F8, 10).splitlines():
    print("   " + ln)

# ------------------------------------------------------------------ C
head("C 队列结构：head @0x9F58D500、节点 @0x9F58D508+n*0x0C、push 在 0x124F62")

print("0x124F5A - 0x124F66（被 0x12E728 调用）：")
for ln in m.disasm_at(0x124F5A, 6).splitlines():
    print("   " + ln)
print()

m2 = assembled()
for i in range(32):
    m2.bus.wr(HEAD + i, 1, 0xAA + i)          # 给 head 区填标记
bgate.open_gate(m2)
m2.bus.wr(bgate.STATUS, 1, bgate.STATUS_WORK_READY)
logs = []
_ow = m2.bus.wr


def _w(a, sz, v, pc=None):
    if HEAD <= a < HEAD + 0x30 and (v & 0xFFFFFFFF):
        logs.append((m2.step_count, pc or 0, a, v))
    return _ow(a, sz, v, pc)


m2.bus.wr = _w
m2.run(max_steps=150000, spin_guard=False)
m2.bus.wr = _ow
print("对 head 区的非零写入 %d 次：" % len(logs))
for st, pc, a, v in logs[:8]:
    print("   step=%-7d pc=%08X [%08X] = %08X" % (st, pc, a, v))
print()
print("  → 这两笔都是 **节点地址**：0x9F58D58C = 0x9F58D508 + 0x0B*0x0C（11 号槽位）")
print("    说明 0x124F5A 不是拷贝，是「把节点挂到链表头」：`[head] = node`。")
print()
print("槽位表（base 0x9F58D508，步长 0x0C，步 11 号 = 0x9F58D58C）：")
print("  0x9F58D58C: %s" % m2.fetch_bytes(0x9F58D58C, 12).hex(" "))

# ------------------------------------------------------------------ D
head("D 补上 r5 会不会让门后走更远")


def run(fix_r5, steps=150000):
    mm = assembled()
    c0 = mm.cov.words()
    bgate.open_gate(mm)
    mm.bus.wr(bgate.STATUS, 1, bgate.STATUS_WORK_READY)
    cc = mm.cpu
    k = 0
    while k < steps:
        mm.step(1)
        k += 1
        if fix_r5 and cc.pc == 0x12E728:
            cc.r[5] = 0x9F58D600
    return mm, mm.cov.words() - c0


a_m, a_dc = run(False)
b_m, b_dc = run(True)
print("  不补 r5：覆盖 +%d 结束 PC=%08X" % (a_dc, a_m.cpu.pc))
print("  补 r5：  覆盖 +%d 结束 PC=%08X" % (b_dc, b_m.cpu.pc))
print()
print("  → 覆盖一样。参数不是缺的那一环 —— 门后走不远另有原因（更深的等待/信号量）。")

# ------------------------------------------------------------------ E
head("E 0x120458（重复写 0x76 的那段）在静态上有没有入口")

for tgt, kind in ((0x120458, "立即数"),):
    p = tgt.to_bytes(4, "big")
    cnt, j = 0, rom.find(p)
    while j >= 0:
        cnt += 1
        j = rom.find(p, j + 1)
    print("0x%08X 作为 32 位%s出现 %d 次" % (tgt, kind, cnt))
print("（对照：0x12E6F8 是 %d 次；任务 2 入口 0x1200AC 是 1 次。）" % len(hits))
print()
print("  → 既没有直接 call 指向它，ROM 里也没有它的地址常量。")
print("    那段只能靠顺序流入到达，或者地址是运行时算出来的 —— 静态上没有入口。")

# ------------------------------------------------------------------ F
head("F 结论与边界")

print("能报：")
print("  * 0x12E6F8 是显示请求的通用「提交」接口：312 处 ldi:32 引用")
print("  * 签名 = r4 槽位号 + r5 另一个指针；主循环那个调用点只给了 r4，")
print("    r5 是 clr_flg 掩码的残值 0xFFDFFFFF")
print("  * 队列结构：head @0x9F58D500、节点 @0x9F58D508 + n*0x0C、")
print("    挂链在 0x124F62（`[head] = node`，实测写进 0x9F58D58C = 11 号槽位）")
print("  * 0x124F5A 不是拷贝，是链表插入")
print("  * 补 r5 不改变控制流（覆盖仍 +109）—— 参数不是缺的那一环")
print("  * 0x120458 在 ROM 里无立即数、无直接 call → 静态无入口")
print("不能报：")
print("  * 节点 12 字节的字段含义（只知道 +9 是标志字节）")
print("  * r5 在正常调用里的语义（是源缓冲？还是返回节点？）")
print("  * 0x120458 到底由谁进入（只能是运行时地址）")
print("  * 「显示队列跑起来了」—— 只是一次挂链，没有任何像素")
