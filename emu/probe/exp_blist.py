# -*- coding: utf-8 -*-
"""exp_blist.py —— V34：链表四件套、消费端真身、表头语义

A 四件套：init / is_empty / insert / unlink（单元调用 ROM 里那四条）
B 消费端三条函数：清空（0x12E602）/ 取首（0x12E63A）/ 重挂（0x12E672）
C 表项 +0 的真语义：第一项是表头（两张表互证），对象参数在「槽位 +16」处
D 计数信号量：sem 14 / 27 / 29 的上限 7 / 255 / 255
E 更正 V33：0x124F46 的调用点是 44 处，不是 2 处
F 结论：能报 / 不能报

跑法：
    cd emu && python probe/exp_blist.py > ../results/frbox/blist.txt
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
EMU_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(EMU_DIR)

from frbox.machine import create
from frbox import brealos, bsem, blist

H = 0x9F58D000
NA, NB, NC = 0x9F58D100, 0x9F58D200, 0x9F58D300


def head(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def rom_call(pc, r4=None, r5=None, steps=16):
    """把 PC 指到某条 ROM 例程跑几条，返回 (machine, 写入日志, 结束时的 r4)。"""
    m = create("d300-b")
    m.load()
    m.reset()
    c = m.cpu
    c.r[4] = 0 if r4 is None else r4
    c.r[5] = 0 if r5 is None else r5
    c.pc = pc
    log = []
    ow = m.bus.wr

    def _w(a, sz, v, pcx=None):
        log.append((pcx or 0, a, sz, v))
        return ow(a, sz, v, pcx)

    m.bus.wr = _w
    n = 0
    while n < steps and not c.halted:
        m.step(1)
        n += 1
    m.bus.wr = ow
    return m, log, c


def build(nodes):
    m = create("d300-b")
    m.load()
    m.reset()
    blist.init_model(m, H)
    for x in nodes:
        blist.insert_model(m, H, x)
    return m


# ---------------------------------------------------------------- A 四件套
head("A 链表四件套（都在 ROM 里，都是 call @r12 间接调用）")

print("  0x124F46 init   / 0x124F4E is_empty / 0x124F5A insert / 0x124F7A unlink")
print()
m, log, c = rom_call(blist.LIST_INIT, H)
print("  A1 init(head=0x%08X)：写 %d 笔" % (H, len(log)))
for p, a, sz, v in log:
    print("       pc=%08X [%08X] = %08X" % (p, a, v))
print("       head 区 = %s → 空表" % m.fetch_bytes(H, 8).hex(" "))

print()
print("  A2 is_empty(0x124F4E)：")
for nodes, label in (([], "空表"), ([NA], "1 节点"), ([NA, NB, NC], "3 节点")):
    mm = build(nodes)
    before = mm.fetch_bytes(H, 8)
    r = blist.is_empty_rom(mm, H)
    after = mm.fetch_bytes(H, 8)
    print("       %-6s 返回 %d   内存有没有被改：%s" % (label, r, before != after))
print("       → 返回 1 = 空、0 = 非空；不改内存（纯查询）")

print()
print("  A3 insert：往 1 节点链（A）里插 B，逐笔写入（r4 = head、r5 = node）")
mm = build([NA])
m2 = build([NA])
c2i = m2.cpu
c2i.r[4], c2i.r[5] = H, NB
c2i.pc = blist.LIST_INSERT
log = []
_ow2 = m2.bus.wr


def _w2(a, sz, v, pcx=None):
    log.append((pcx or 0, a, sz, v))
    return _ow2(a, sz, v, pcx)


m2.bus.wr = _w2
for _ in range(16):
    m2.step(1)
m2.bus.wr = _ow2
for p, a, sz, v in log:
    print("       pc=%08X [%08X] = %08X" % (p, a, v))
print("       walk = %s（新节点在链首）" % [hex(x) for x in blist.walk(m2, H)])

print()
print("  A4 unlink(r4 = 节点)：从 3 节点链里摘中间那个")
mm = build([NA, NB, NC])
print("       摘之前：%s" % [hex(x) for x in blist.walk(mm, H)])
mm2 = build([NA, NB, NC])
c2 = mm2.cpu
c2.r[4] = NB
c2.pc = blist.LIST_UNLINK
for _ in range(16):
    mm2.step(1)
print("       摘之后：%s（返回 r4 = %08X）"
      % ([hex(x) for x in blist.walk(mm2, H)], c2.r[4]))
print("       反向走：%s" % [hex(x) for x in blist.back_walk(mm2, H)])
print("       ⚠️ 传哨兵进去会破坏链（固件不检查参数）")

# ---------------------------------------------------------------- B 消费端
head("B 消费端三条函数：清空 / 取首 / 重挂")

m0 = create("d300-b")
m0.load()
print("  B1 0x12E602 = 清空队列")
print(m0.disasm_at(0x12E602, 20))
print("      序列：init(head) → 清 70 个槽位的 +9 → set_flg(3, 0x80)")
print("      ⚠️ V32 把它读成「摘 head」，其实是**整队清空**")

print()
print("  B2 0x12E63A = 取队首（peek）")
print(m0.disasm_at(0x12E63A, 16))
print("      序列：wai_sem(17) → is_empty(head) → 取首节点 +8 的字节 → sig_sem(17) → 返回")
print("      注意它**没有摘链**：只是把首节点的数据读出来")

print()
print("  B3 0x12E672 = 重挂（把一个槽位节点重新挂到链上）")
print(m0.disasm_at(0x12E672, 8))
print("      …")
print(m0.disasm_at(0x12E6A2, 10))
print("      序列：按 0x9F58DA82 的索引取槽位 → wai_sem(17) → 测 [+9] bit3 →")
print("            unlink(node)（0x124F7A）→ … → insert(head, node)（0x124F5A）")
print("      这是**唯一会摘链的地方**，而且摘完马上又挂回去 —— 是「移到队首」而不是「丢弃」")

print()
print("  队列生命周期小结：")
print("      生产 0x12E6F8：wai_sem(17) → insert → sig_sem(17)")
print("      取首 0x12E63A：wai_sem(17) → is_empty → 读首节点 +8 → sig_sem(17)")
print("      重挂 0x12E672：wai_sem(17) → unlink → … → insert → sig_sem(17)")
print("      清空 0x12E602：init(head) → 清 70 槽 → set_flg(3, 0x80)")
print("      四条路径**都**上锁 sem 17（这就是它 8 处四对的来历）")

# ---------------------------------------------------------------- C 表头
head("C 表项 +0 的真语义：第一项是表头")

mm = brealos.boot()
print("  两张对象表的首项 +0：")
for name, base, cnt in (("信号量表", bsem.SEM_TABLE, 40), ("标志表", 0x551B0, 17)):
    w0 = mm.bus.rd(base, 4)
    print("    %-6s @%08X  +0 = 0x%08X → 高半字 %2d（实际条数 %2d） / 低半字 %2d（项宽 16）"
          % (name, base, w0, w0 >> 16, cnt, w0 & 0xFFFF))
print("  → 两个高半字分别等于两张表的**条数**，两个低半字都等于**项宽**")
print()
print("  每个对象的参数放在「自己槽位 + 16」处，两个半字 = [初始计数 | 上限]：")
print("    slot   +0           描述谁     计数  上限")
for sid in (1, 2, 15, 28, 30, 40):
    w0 = mm.bus.rd(bsem.table_addr(sid), 4)
    print("    %2d   0x%08X   sem %2d     %3d   %3d"
          % (sid, w0, sid - 1, w0 >> 16, w0 & 0xFFFF))
w0 = mm.bus.rd(bsem.TAIL_PAD, 4)
print("    表尾 0x551AC = 0x%08X   sem 40     %3d   %3d" % (w0, w0 >> 16, w0 & 0xFFFF))
print()
print("  核对：40 项的计数全都等于「下一项 +0 的高半字」——%s"
      % ("全部命中" if not bsem.summary(mm)["init_rule_fail"] else "有例外"))

# ---------------------------------------------------------------- D 计数信号量
head("D 那三个 0 计数是「计数信号量」，不是「被占用」")

print("  id  初始计数  上限   类型")
for sid in range(1, 41):
    own = mm.bus.rd(bsem.table_addr(sid + 1) if sid < 40 else bsem.TAIL_PAD, 4)
    cnt, mx = own >> 16, own & 0xFFFF
    if mx != 1 or cnt != 1:
        print("  %2d     %3d     %3d   %s" % (sid, cnt, mx,
                                             "计数信号量（从 0 起）" if cnt == 0 else "非二值"))
print("  其余 37 个：初始 1 / 上限 1 → 二值互斥量")
print("  → 7 与 255 是**上限**（max count），不是别的表的编号")
print("  sem 17（显示队列锁）= 1 / 1 → 二值")

# ---------------------------------------------------------------- E 更正
head("E 更正 V33：0x124F46 的调用点是 44 处，不是 2 处")

import fr_emu  # noqa: E402

rom = open(os.path.join(REPO, "data", "B3000111.bin"), "rb").read()
BASE = 0x40000
for target in (blist.LIST_INIT, blist.LIST_IS_EMPTY, blist.LIST_INSERT, blist.LIST_UNLINK):
    pat = target.to_bytes(4, "big")
    hits, i = [], rom.find(pat)
    while i >= 0:
        hits.append(BASE + i - 2)
        i = rom.find(pat, i + 1)
    rel12 = 0
    for off in range(0, len(rom) - 4, 2):
        ins = fr_emu.TABLE.decode(rom, off)
        if ins is None:
            continue
        mn, _b, v, _s, _w = ins
        if mn in ("call", "call:d") and "rel12" in v:
            pc = BASE + off
            if (pc + 2 + fr_emu.sx(v["rel12"], 11) * 2) & 0xFFFFFFFF == target:
                rel12 += 1
    print("  0x%08X  立即数 %3d 处 / rel12 相对调用 %d 处" % (target, len(hits), rel12))
print()
print("  V33 的探针把 0x124F46 印成「2 处」是算错了（应为 44 处）；0x124F5A 的 88 处是对的。")
print("  规则不变：判调用者扫 32 位立即数。")

# ---------------------------------------------------------------- F 结论
head("F 结论")

print("能报：")
print("  * 链表四件套：init 0x124F46 / is_empty 0x124F4E / insert 0x124F5A / unlink 0x124F7A")
print("  * is_empty 返回 1=空、0=非空，不改内存；unlink 的参数是**节点本身**")
print("  * 消费端不是「摘 head」：0x12E602 是**清空整队**（init + 清 70 槽 + 通知 0x80）")
print("  * 取首在 0x12E63A（只读首节点的 +8，不摘链）；唯一摘链在 0x12E672，摘完又插回")
print("  * 表项 +0 的真语义：**第一项是表头 [条数 | 项宽]**（40|16 与 17|16 两张表互证）")
print("  * 对象参数在「槽位 +16」处：高半字 = 初始计数、低半字 = 上限")
print("  * sem 14 / 27 / 29 = 计数信号量（上限 7 / 255 / 255，初始 0），其余 37 个是二值")
print("  * 0x124F46 的调用点 44 处（V33 的 2 处是算错）")
print("不能报：")
print("  * 队列里的 70 个槽位分别属于什么界面元素")
print("  * 为什么哨兵的 init 只放在「清空」函数里（本场景没执行 → 链不完整）")
print("  * 计数信号量的「上限」在运行期有没有被检查过（内核代码路径没走到）")
print("  * 有没有像素（仍然一条帧数据都没有）")
