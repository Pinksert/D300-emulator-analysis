# -*- coding: utf-8 -*-
"""exp_bsem.py —— V33：信号量表、链表原语、槽位标志

A 信号量表 0x54F2C：40 项、16 字节/项、与 flag 表首尾相接
B 计数：谁写、boot 后的分布、boot 之后三个为 0
C 链表原语：单元调用 0x124F46 / 0x124F5A 实测写入
D 更正 V31 / V32：间接调用（call @r12）与语义
E 槽位 +9 的写入者
F 结论：能报 / 不能报

跑法：
    cd emu && python probe/exp_bsem.py > ../results/frbox/bsem.txt
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))        # emu/probe
EMU_DIR = os.path.dirname(HERE)                          # emu
REPO = os.path.dirname(EMU_DIR)                          # 仓库根

from frbox.machine import create
from frbox import brealos, bgate, bsem, blist


def head(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


# ---------------------------------------------------------------- A 信号量表
head("A 信号量表 0x54F2C：40 项，16 字节/项")

m = brealos.boot()
print(bsem.describe())
print()
print("  id  表项地址    +0        对象指针   id<<16    计数  对象地址核对")
for r in bsem.entries(m):
    if r["id"] > 6 and r["id"] not in (14, 15, 16, 17, 20, 21, 28, 29, 30, 39, 40):
        continue
    ok = "OK" if r["obj"] == bsem.object_addr_calc(r["id"]) else "✗"
    print("  %2d  %08X  %08X  %08X  %08X  %5d  %s"
          % (r["id"], r["addr"], r["words"][0], r["obj"], r["idword"],
             bsem.count(m, r["id"]), ok))
print("  （省略 id 7–13 / 18–19 / 22–27 / 31–38，形状相同）")
s = bsem.summary(m)
print()
print("  条数 %d；对象地址与步长不符的：%s" % (s["count"], s["mismatched"] or "无"))
print("  表尾 %08X —— flag 表从 0x551B0 起（V24 解出），中间 4 字节填充" % s["table_end"])
print("  +0 不是 0x00010001 的项：%s（语义未证）"
      % ", ".join("id %d = 0x%08X" % (i, w) for i, w in s["odd_first_word"]))

# ---------------------------------------------------------------- B 计数
head("B 计数在 object+8（半字）：谁写、boot 后是多少")

mm = create("d300-b")
log = []
_ow = mm.bus.wr


def _watch_count(a, sz, v, pc=None):
    if sz == 2 and bsem.SEM_OBJ_BASE <= a < bsem.SEM_OBJ_BASE + 40 * bsem.SEM_OBJ_STRIDE:
        if (a - bsem.SEM_OBJ_BASE) % bsem.SEM_OBJ_STRIDE == bsem.F_COUNT:
            log.append((mm.step_count, pc or 0, a, v))
    return _ow(a, sz, v, pc)


mm.bus.wr = _watch_count
mm.load()
mm.reset()
mm.enable_cov()
mm.attach_flow()
n = 0
while not mm.cpu.halted and n < 191520:
    mm.step(1)
    n += 1
brealos.sta_tsk(mm, 2)
mm.raise_irq(0x3F)
mm.run(max_steps=200000, spin_guard=False)
mm.bus.wr = _ow

c = Counter(p for _, p, _, _ in log)
print("  计数写入 %d 次，按 PC：" % len(log))
for pc, k in c.most_common():
    who = {0x00040910: "内核初始化（写 1）",
           0x0004193A: "wai_sem 的 sth（拿锁）",
           0x000418F0: "sig_sem 的 sth（放锁）"}.get(pc, "?")
    print("    0x%08X  %2d 次  %s" % (pc, k, who))
print("  → boot 的 sem 16/20/21 三对握手：wai 6 次、sig 6 次，逐次抵消")
print()
print("  boot 后计数分布：")
dist = Counter(bsem.count(m, i) for i in range(1, 41))
print("    %s" % dict(sorted(dist.items())))
print("    计数为 0 的：%s" % sorted(s["counts_not_one"]))
print("    sem 17 → 对象 %08X，计数 %d（V32 说的「队列互斥锁」由此量化）"
      % (bsem.object_addr(m, 17), bsem.count(m, 17)))
print("    对象 +4（等待队列哨兵）= %08X = 自指 → 空" % bsem.waitq(m, 17))
print("    对象 +0xC（背指）= %08X = 表项 0x55030" % bsem.backptr(m, 17))

# ---------------------------------------------------------------- C 链表原语
head("C 链表原语：单元调用 0x124F46（init）与 0x124F5A（insert）")

HEADW = 0x9F58D000
NODEW = 0x9F58D100


def unit(pc, r4, r5, setup=None, steps=12):
    mm = create("d300-b")
    mm.load()
    mm.reset()
    for i in range(32):
        mm.bus.wr(HEADW + i, 1, 0xA0 + i)
        mm.bus.wr(NODEW + i, 1, 0xC0 + i)
    if setup:
        setup(mm)
    cc = mm.cpu
    cc.r[4], cc.r[5] = r4, r5
    cc.pc = pc
    writes = []
    ow = mm.bus.wr

    def _w(a, sz, v, pcx=None):
        writes.append((pcx or 0, a, sz, v))
        return ow(a, sz, v, pcx)

    mm.bus.wr = _w
    for _ in range(steps):
        mm.step(1)
        if cc.halted:
            break
    mm.bus.wr = ow
    return mm, writes


print("  C1  pc=0x124F46  r4=0x9F58D000（init）")
mm, ws = unit(blist.LIST_INIT, HEADW, 0)
for p, a, sz, v in ws:
    print("      pc=%08X [%08X] = %08X" % (p, a, v))
print("      head 区：%s  → 空表（哨兵自指）" % mm.fetch_bytes(HEADW, 8).hex(" "))
print("      is_empty = %s" % blist.is_empty(mm, HEADW))

print()
print("  C2  pc=0x124F5A  r4=0x9F58D000  r5=0x9F58D100（insert，表头未初始化）")
mm, ws = unit(blist.LIST_INSERT, HEADW, NODEW)
for p, a, sz, v in ws:
    print("      pc=%08X [%08X] = %08X" % (p, a, v))
print("      head 区：%s  node 区：%s" % (mm.fetch_bytes(HEADW, 8).hex(" "),
                                         mm.fetch_bytes(NODEW, 8).hex(" ")))
print("      ⚠️ 表头没 init 时第 4 笔写会落到「旧首节点+4」，旧首是垃圾值 → 写到野地址")

print()
print("  C3  先 init 再 insert（正确用法）")
mm, ws = unit(blist.LIST_INSERT, HEADW, NODEW, setup=lambda x: blist.init_model(x, HEADW))
for p, a, sz, v in ws:
    print("      pc=%08X [%08X] = %08X" % (p, a, v))
print("      head 区：%s  node 区：%s" % (mm.fetch_bytes(HEADW, 8).hex(" "),
                                         mm.fetch_bytes(NODEW, 8).hex(" ")))
print("      → head = node、node = (head, head)：单元素循环表")

print()
print("  语义（两条原语都在 ROM 里，都是 call @r12 间接调用）：")
print("      0x124F46  init(head):  [head] = head; [head+4] = head")
print("      0x124F5A  insert(head, node):")
print("                  node->prev = head")
print("                  node->next = head->next")
print("                  head->next = node")
print("                  旧首节点->prev = node")

# ---------------------------------------------------------------- D 更正
head("D 更正 V31 / V32")

print("  V31 说：「node->next = old_head; head = node」+「+4 也存了一个节点地址」")
print("      → 实际是**循环双链表**：+0 = next、+4 = prev（空表时 prev 就是哨兵）")
print()
print("  V32 说：「0x124F46 是摘 head（只有三条指令，像空桩）」")
print("      → 实际是**初始化空表**（哨兵自指）。摘节点的代码还没找到，仍是 OPEN")
print()
print("  方法论：两个原语都用 `ldi:32 r12,#addr` + `call @r12` 调用。")
print("      只扫 rel12 相对调用会得到「0 个调用点」——V31 的 exp_bqueue 就是这么写的：")
import fr_emu  # noqa: E402

rom = open(os.path.join(REPO, "data", "B3000111.bin"), "rb").read()
BASE = 0x40000

for target in (blist.LIST_INIT, blist.LIST_INSERT):
    rel12, imm = 0, 0
    for off in range(0, len(rom) - 4, 2):
        ins = fr_emu.TABLE.decode(rom, off)
        if ins is None:
            continue
        mn, _b, v, _s, _w = ins
        if mn in ("call", "call:d") and "rel12" in v:
            pc = BASE + off
            if (pc + 2 + fr_emu.sx(v["rel12"], 11) * 2) & 0xFFFFFFFF == target:
                rel12 += 1
    imm = rom.count(target.to_bytes(4, "big"))
    print("      0x%08X：rel12 相对调用 %d 处 / 32 位立即数 %d 处" % (target, rel12, imm))
print("      → 正确判法是扫立即数（每个 `ldi:32` 之后就是 `call @r12`）。")
print("      0x120458（V29 那段）用同样方法扫：立即数 0 处 → 「静态无入口」的结论仍成立。")

# ---------------------------------------------------------------- E 槽位 +9
head("E 槽位 +9 的写入者：只有生产端 0x12E740")

mm = create("d300-b")
hits = []
_ow = mm.bus.wr


def _watch_flag(a, sz, v, pc=None):
    d = a - 0x9F58D508
    if 0 <= d < 70 * 0x0C and d % 0x0C == 9:
        hits.append((mm.step_count, pc or 0, a, sz, v))
    return _ow(a, sz, v, pc)


mm.bus.wr = _watch_flag
mm.load()
mm.reset()
mm.enable_cov()
mm.attach_flow()
n = 0
while not mm.cpu.halted and n < 191520:
    mm.step(1)
    n += 1
brealos.sta_tsk(mm, 2)
mm.raise_irq(0x3F)
mm.run(max_steps=200000, spin_guard=False)
bgate.open_gate(mm)
mm.bus.wr(bgate.STATUS, 1, bgate.STATUS_WORK_READY)
mm.run(max_steps=200000, spin_guard=False)
mm.bus.wr = _ow

print("  写入 %d 次；PC 分布：%s" % (len(hits), dict(Counter(p for _, p, _, _, _ in hits))))
print("  涉及的槽位：%s" % dict(Counter((a - 0x9F58D508) // 0x0C for _, _, a, _, _ in hits)))
print("  前 6 笔：")
for st, pc, a, sz, v in hits[:6]:
    print("     step=%-7d pc=%08X 槽位 %2d +9 = 0x%02X"
          % (st, pc, (a - 0x9F58D508) // 0x0C, v))
nz = [(k, mm.bus.rd(0x9F58D508 + k * 0x0C + 9, 1)) for k in range(70)
      if mm.bus.rd(0x9F58D508 + k * 0x0C + 9, 1)]
print("  70 个槽位里 +9 非零的：%s" % [(k, hex(v)) for k, v in nz])
print("  → 置位者是生产端（0x12E6F8 里），消费端 0x12E602 在本场景一次都没执行")
print("  显示队列现状：%s" % blist.describe(mm, blist.DISPLAY_QUEUE_HEAD))
print("      （[head] = %08X 是生产端插进去的节点，但哨兵从未 init）"
      % mm.bus.rd(blist.DISPLAY_QUEUE_HEAD, 4))

# ---------------------------------------------------------------- F 结论
head("F 结论")

print("能报：")
print("  * 信号量表 @0x54F2C 共 40 项（16 字节/项），表尾 0x551AC，紧接 flag 表 0x551B0")
print("  * 表项：+0 两半字 / +4 对象指针（0x68000638+(id-1)*0x10）/ +8 id<<16 / +C 0")
print("  * 对象：+0 自指 / +4 自指（空等待队列）/ +8 计数（半字）/ +C 背指表项+4")
print("  * 计数由 0x40910 初始化成 1；wai_sem 0x4193A、sig_sem 0x418F0 改它")
print("  * boot 后 37 个 = 1、3 个 = 0（sem 14 / 27 / 29）；sem 17 = 1 → 二值互斥量")
print("  * 0x124F46 = 初始化空表（哨兵自指）；0x124F5A = 插到哨兵之后（循环双链表）")
print("  * 节点 +0 = next、+4 = prev（更正 V31 的「另一个节点地址」）")
print("  * 槽位 +9 的唯一写入者是生产端 0x12E740（置 0x80，11 号槽位置 0xC0）")
print("不能报：")
print("  * 表项 +0 的语义（多数 0x00010001，但 id 1/15/28/30 是别的值）")
print("  * 为什么 sem 14 / 27 / 29 的计数是 0（被谁占用？还是没初始化？）")
print("  * 摘节点（消费端）的代码在哪 —— 0x12E602 在本场景没执行过")
print("  * 「显示队列在工作」——哨兵从来没被 init，链表只是勉强成形")
print("  * 有没有像素（仍然一条帧数据都没有）")
