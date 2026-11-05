# -*- coding: utf-8 -*-
"""exp_bdispchain.py —— V32：显示队列的完整链路。

A 队列容量：70 个槽位（0x46），步长 0x0C（修正 V31 的「49 号」）
B 生产端 0x12E6F8：wai_sem(17) → 头插 → sig_sem(17)
C 消费端 0x12E602：摘 head → 清 70 个槽位 → set_flg(3,0x80)
D sem 17 = 显示队列互斥锁：8 处调用点成四对
E 0x12E630 回归：它就是 V25 找的 0x80 置位者
F 直接调内核查 sem 17 行不通（-69/-73 是调用上下文问题）
G 结论与边界

跑法：cd emu && python probe/exp_bdispchain.py > ../results/frbox/bdispchain.txt
"""

import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from frbox.machine import create
from frbox import brealos
import fr_emu

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROM_PATH = os.path.join(ROOT, "data", "B3000111.bin")
BASE = 0x40000


def head(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


m = create("d300-b")
m.load()

# ------------------------------------------------------------------ A
head("A 队列容量：70 个槽位，步长 0x0C")

print("0x12E612 - 0x12E62A（清槽位循环）：")
for ln in m.disasm_at(0x12E612, 13).splitlines():
    print("   " + ln)
print()
print("读法：")
print("  * 0x12E620 `ldi:8 r0, #0x46` → 循环 70 次")
print("  * 0x12E624 `blt:d` 的**延迟槽**是 0x12E626 `add r5, #0x0C`")
print("    → 每轮槽位地址 +0x0C，所以表 = 0x9F58D508 + n*0x0C，n = 0..69")
print("  * 循环体 `stb r4(=0), @(r5)` 清的是节点的 +0（next）")
print()
print("⚠️ 修正 V31：当时说「实测到 49 号」，那只是**观测到**的最大编号；")
print("   代码按 70 个清，说明表容量是 70。")

# ------------------------------------------------------------------ B
head("B 生产端 0x12E6F8：wai_sem(17) → 头插 → sig_sem(17)")

print("0x12E6F8 - 0x12E750：")
for ln in m.disasm_at(0x12E6F8, 30).splitlines():
    print("   " + ln)

# ------------------------------------------------------------------ C
head("C 消费端 0x12E602：摘 head → 清 70 个槽位 → set_flg(3,0x80)")

print("0x12E602 - 0x12E636：")
for ln in m.disasm_at(0x12E602, 20).splitlines():
    print("   " + ln)
print()
print("读法：")
print("  * 0x12E604/0x12E610：把 head 地址传给 0x124F46（摘节点）")
print("  * 0x12E612-0x12E626：清 70 个槽位的 +0")
print("  * 0x12E62A/0x12E62C/0x12E630：set_flg(3, 0x80) ★")

# ------------------------------------------------------------------ D
head("D sem 17 = 显示队列互斥锁：8 处调用点成四对")

rom = open(ROM_PATH, "rb").read()
pcs = []
for off in range(0, len(rom) - 2, 2):
    ins = fr_emu.TABLE.decode(rom, off)
    if ins and ins[0] == "int" and ins[2].get("u8") == 0x40:
        pcs.append(BASE + off)
print("扫到 int #0x40 共 %d 处" % len(pcs))

PAT = re.compile(r"^0x([0-9A-Fa-f]+):\s+\S+\s+(ldi:8)\s+.*?i8=(0x[0-9a-f]+).*?ri=(0x[0-9a-f]+)")


def back(pc, n=40):
    r4 = r12 = None
    for ln in reversed(m.disasm_at(pc - n, 26).splitlines()):
        s = ln.strip()
        mm = PAT.match(s)
        if not mm:
            continue
        if int(mm.group(1), 16) >= pc:
            continue
        val = int(mm.group(3), 16)
        if val & 0x80:
            val -= 0x100
        reg = int(mm.group(4), 16)
        if reg == 4 and r4 is None:
            r4 = val
        elif reg == 12 and r12 is None:
            r12 = val
        if r4 is not None and r12 is not None:
            break
    return r4, r12


sem = []
for pc in pcs:
    r4, r12 = back(pc)
    if r12 in (-53, -55):
        sem.append((pc, r4, r12))
print("wai_sem/sig_sem 共 %d 处" % len(sem))
print("  r4 号分布（前 8）：", Counter(r4 for _, r4, _ in sem).most_common(8))
print()
print("sem 17 的调用点：")
for pc, r4, r12 in sem:
    if r4 == 17:
        print("   %08X  %s" % (pc, "sig_sem" if r12 == -55 else "wai_sem"))
print()
print("  四对：0x12E24A/266、0x12E646/66A、0x12E694/6DE、0x12E712/748")
print("  全部落在 0x12E2xx–0x12E7xx —— 就是显示请求这一族函数。")

# ------------------------------------------------------------------ E
head("E 0x12E630 回归：它就是 V25 找的 0x80 置位者")

print("V25 当时的描述是「位 0x80 ← 0x12E630（显示绘制路径，0x12E138/0x12E154 调 0x12E636）」。")
print("现在能说清它在整条链里的位置：")
print()
print("    消费端 0x12E602 ── 摘 head ── 清 70 个槽位 ── set_flg(3,0x80) @0x12E630")
print()
print("  也就是说：**显示侧处理完一轮（或确认队列空）时，置 0x80 通知主循环**。")
print("  主循环等的正是这一位（V24：0x1203BE wai_flg(3, 0x200000) 是让出点，")
print("  真正的门是 0x120178 的字节；0x80 是另一条等待位）。")

# ------------------------------------------------------------------ F
head("F 直接调内核查 sem 17：行不通")

mm = brealos.boot()
brealos.sta_tsk(mm, 2)
mm.raise_irq(0x3F)
mm.run(max_steps=200000, spin_guard=False)
print("装配完成 PC=%08X 覆盖 %d" % (mm.cpu.pc, mm.cov.words()))
print()
for sid in (1, 16, 17, 20, 21):
    r = brealos.kernel_call(mm, -53, r4=sid, max_steps=30000)
    print("  wai_sem(%2d) err=%-5s steps=%d" % (sid, r["err"], r["steps"]))
print()
print("全部返回 -69（查表失败）—— 这是**调用上下文**的问题（我们不在任务里、")
print("也没走真实的对象表路径），不能据此说 sem 17 不存在。")
print("静态侧只能说到这里：sem 17 在固件里被四个地方成对使用。")

# ------------------------------------------------------------------ G
head("G 结论与边界")

print("能报：")
print("  * 显示队列容量 **70 个槽位**（0x12E620 的 0x46），步长 0x0C —— 修正 V31 的「49 号」；")
print("  * 生产端 0x12E6F8：wai_sem(17) → 头插 0x124F5A → sig_sem(17)；")
print("  * 消费端 0x12E602：摘 head（0x124F46）→ 清 70 个槽位的 +0 → set_flg(3,0x80)；")
print("  * sem 17 是这一族的互斥锁：8 处调用点、四对 wai/sig，全在 0x12E2xx–0x12E7xx；")
print("  * 0x12E630 在整条链里的位置确定了（清槽之后的「通知」）。")
print("不能报：")
print("  * sem 17 的初始计数、由谁创建；")
print("  * 队列被摘下来的节点之后怎么处理（0x124F46 只有 3 条指令，语义未定）；")
print("  * 70 个槽位里哪些真的被用过；")
print("  * 「显示链路通了」—— 仍然没有一个像素。")
