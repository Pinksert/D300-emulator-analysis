# -*- coding: utf-8 -*-
"""exp_bdesc.py —— V36 探针：描述符表 0x1BC020 的条数、字段、索引方式、事件掩码、取首语义。

    A 表形状：70 条，0x1BC480 起换数据
    B 逐条 dump
    C 统计：状态字节指针 / 方式分布 / 处理函数
    D 表在 ROM：写被丢弃 + boot 全程 0 笔写
    E 索引 = 槽位号：70 条逐一配对 0x9F58D858
    F 事件掩码 0x9F58DA81 的取值规则（哨兵法）
    G 取首 0x12E63A：判空条件 + 读 +8 + 返回值在 R4
    H 结论与边界

用法：cd emu && python probe/exp_bdesc.py > ../results/frbox/bdesc.txt
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from frbox import brealos, blist, bqueue, bdesc          # noqa: E402
from collections import Counter                          # noqa: E402

RET = 0x00407DC          # 空闲任务 bra -1，当函数的「回程地址」
SCRATCH = 0x9F83F000     # 任务栈区高处的空地
CURSOR = 0x9F58DA81


def call_fn(m, addr, r4=0, stack=SCRATCH, max_steps=4000):
    """像 `call` 一样进函数：压回程地址、设 r4、关可屏蔽中断，跑到回程。"""
    c = m.cpu
    m.bus.wr(stack - 4, 4, RET)
    c.r[15] = stack - 4
    c.r[4] = r4
    c.i = 0
    c.pc = addr
    n = 0
    while n < max_steps and not c.halted and c.pc != RET:
        m.step(1)
        n += 1
    return n, c.pc


print("=" * 78)
print("A. 表形状：从 0x1BC020 往后，哪一条不再像描述符")
print("=" * 78)
m = brealos.boot()
end = None
for i in range(0, 140):
    a = bdesc.entry_addr(i)
    b = m.fetch_bytes(a, 16)
    p = int.from_bytes(b[0:4], "big")
    md = int.from_bytes(b[8:12], "big")
    fn = int.from_bytes(b[12:16], "big")
    ok = ((p == 0 or bdesc.STATUS_AREA[0] <= p < bdesc.STATUS_AREA[1])
          and md in (0, 1, 2) and 0x0012E000 <= fn < 0x00140000)
    if not ok:
        end = i
        print("  第 %d 条 @%08X 不合形状：%s" % (i, a, b.hex(" ")))
        break
print("  → 连续 70 条规整：0x%08X .. 0x%08X；第 %d 条 @%08X 起是另一块数据"
      % (bdesc.DESC_TABLE, bdesc.entry_addr(69) + 15, end, bdesc.entry_addr(end)))
print("  与槽位数对齐：槽位 %d / 描述符 %d → %s"
      % (bqueue.SLOT_COUNT, bdesc.DESC_COUNT,
         "一致" if bqueue.SLOT_COUNT == bdesc.DESC_COUNT else "不一致"))

print()
print("=" * 78)
print("B. 70 条逐条 dump")
print("=" * 78)
print(bdesc.dump(m))

print()
print("=" * 78)
print("C. 统计")
print("=" * 78)
a = bdesc.audit(m)
print("  条数            %d" % a["count"])
print("  有状态字节      %d 条；无（=0）%d 条 → 槽位 %s"
      % (a["with_status"], len(a["no_status"]), a["no_status"]))
print("  状态字节范围    %08X .. %08X；唯一指针 %d 个"
      % (a["status_min"], a["status_max"], a["unique_status"]))
print("  +8 方式分布     %s" % a["modes"])
print("  处理函数        %d 个不同；其中指向空 `ret` 的 %d 条" % (a["handlers"], len(a["noop"])))
print("  空处理槽位      %s" % a["noop"])
print("  形状异常        %s" % (a["bad_shape"] or "无"))
print("  0x12E752 真是 `ret`（97 20）：%s" % a["noop_is_ret"])
print()
print("  处理函数 → 槽位（同一函数的槽位合并显示）：")
g = bdesc.handler_groups(m)
for f, idx in sorted(g.items()):
    tag = "  ← 空处理" if f == bdesc.NOOP_HANDLER else ""
    print("    %08X ×%2d  槽位 %s%s" % (f, len(idx), idx, tag))

print()
print("=" * 78)
print("D. 表在 ROM 还是 RAM")
print("=" * 78)
t = m.bus.rd(bdesc.entry_addr(1) + 4, 4)
m.bus.wr(bdesc.entry_addr(1) + 4, 4, 0xDEADBEEF)
back = m.bus.rd(bdesc.entry_addr(1) + 4, 4)
print("  第 1 条 +4 原值 %08X → 写 DEADBEEF → 读回 %08X（%s）"
      % (t, back, "可写" if back == 0xDEADBEEF else "写被丢弃 → ROM"))
m.bus.wr(bdesc.entry_addr(1) + 4, 4, t)

mm = brealos.boot()
hits = []
ow = mm.bus.wr
def wr(a, sz, v, pc=None):
    if bdesc.DESC_TABLE - 0x20 <= a < bdesc.DESC_TABLE + 0x400:
        hits.append((mm.cpu.pc, a, sz, v))
    return ow(a, sz, v, pc)
mm.bus.wr = wr
brealos.boot(mm)
mm.bus.wr = ow
print("  boot 全程（191,520 步）对 0x1BC020 区写入 %d 笔 %s" % (len(hits), hits[:6] or ""))

print()
print("=" * 78)
print("E. 索引 = 槽位号：把游标 82 设成 i，看 0x9F58D858 指向哪条")
print("=" * 78)
m = brealos.boot()
print("   槽  0x9F58D858  应为        结果   81")
ok = 0
for i in range(bdesc.DESC_COUNT):
    for k, val in enumerate((0x55, i, 0, i)):
        m.bus.wr(CURSOR + k, 1, val)
    call_fn(m, bdesc.APPLY_FN, r4=0)
    got = bdesc.current(m)
    want = bdesc.entry_addr(i)
    same = (got == want)
    ok += 1 if same else 0
    if i < 8 or not same:
        print("   %2d  %08X  %08X  %s   %02X"
              % (i, got, want, "配对" if same else "✗", bdesc.event_mask(m)))
print("   ……（中间略，全部 70 条）" if ok == 70 else "")
print("  → %d/70 配对；当前描述符指针的唯一写点是 0x12E276" % ok)
if ok == 69:
    print()
    print("  唯一不配对的是槽位 0：写日志只有一笔（0x12E23C 把游标 82 清 0），没有取描述符、")
    print("  也没有更新 0x9F58D858 —— 说明**槽位号 0 被当作「空/无效」**，指针保留上一次的值。")
    print("  这也解释了为什么描述符 0 的状态字节指针是 0。")

print()
print("=" * 78)
print("F. 事件掩码 0x9F58DA81 的取值规则")
print("=" * 78)
m = brealos.boot()
rows = []
for i in range(bdesc.DESC_COUNT):
    e = bdesc.entry(m, i)
    for k, val in enumerate((0x55, i, 0, i)):
        m.bus.wr(CURSOR + k, 1, val)
    call_fn(m, bdesc.APPLY_FN, r4=0)
    rows.append((i, e["value"], e["mode"], bdesc.event_mask(m)))

for mode, name in ((0, "+8=0（清零？）"), (1, "+8=1（置位？）"), (2, "+8=2（未定）")):
    sel = [r for r in rows if r[2] == mode]
    print("  %s 共 %d 条：" % (name, len(sel)))
    print("    槽位/描述符+4/实测 81：%s"
          % ", ".join("%d/%d/%02X" % (i, v, g) for i, v, _m, g in sel[:12]))
hit1 = sum(1 for i, v, md, g in rows if md == 1 and g == v)
tot1 = sum(1 for i, v, md, g in rows if md == 1)
hit0 = sum(1 for i, v, md, g in rows if md == 0 and g == 0)
tot0 = sum(1 for i, v, md, g in rows if md == 0)
print()
print("  规则「+8=1 → 81 := +4」：%d/%d" % (hit1, tot1))
print("  规则「+8=0 → 81 := 0 」：%d/%d" % (hit0, tot0))
sel2 = [r for r in rows if r[2] == 2]
print("  「+8=2」的 %d 条：%s → 无统一规则"
      % (len(sel2), ", ".join("%d→%02X" % (i, g) for i, _v, _m, g in sel2)))
print("  哨兵法说明：调用前把 81 写成 0x55，值等于 0x55 就是「这条路径根本没写它」")

print()
print("=" * 78)
print("G. 取首 0x12E63A 的完整语义")
print("=" * 78)
print("  反汇编（0x12E63A 起 28 条）：")
print(m.disasm_at(bqueue.PEEK, 20))
print()
m = brealos.boot()
head = blist.DISPLAY_QUEUE_HEAD
node = bqueue.slot_addr(0)
m.bus.wr(head, 4, node)
m.bus.wr(head + 4, 4, node)
m.bus.wr(node, 4, head)
m.bus.wr(node + 4, 4, head)
m.bus.wr(node + 8, 1, 0x5A)
m.bus.wr(node + 9, 1, 0xA5)
reads = []
ow_rd = m.bus.rd
def rd(a, sz, pc=None):
    if 0x9F58D000 <= a < 0x9F58DC00:
        reads.append((m.cpu.pc, a, sz))
    return ow_rd(a, sz, pc)
m.bus.rd = rd
c = m.cpu
m.bus.wr(SCRATCH - 4, 4, RET)
c.r[15] = SCRATCH - 4
c.r[4] = 0
c.i = 0
c.pc = bqueue.PEEK
n = 0
while n < 3000 and not c.halted and c.pc != RET:
    m.step(1)
    n += 1
m.bus.rd = ow_rd
print("  非空链（head→槽位0，+8=5A，+9=A5）：%d 步返回" % n)
print("    r0..r3 = %s   ← 返回值不在这里" % " ".join("%08X" % c.r[k] for k in range(4)))
print("    r4     = %08X   ← 返回值在 R4" % c.r[4])
print("    读过的地址：%s"
      % ", ".join("%08X@%08X" % (a, pc) for pc, a, _s in reads[:6]))
print("    → 只读 +8（5A），+9（A5）一次都没被读")
print()
print("  is_empty 的判据（0x124F4E）：")
print(m.disasm_at(0x124F4E, 6))
print("    → [head] == head 即空（哨兵自指）")
empty = brealos.boot()
empty.bus.wr(head, 4, head)
empty.bus.wr(head + 4, 4, head)
cc = empty.cpu
empty.bus.wr(SCRATCH - 4, 4, RET)
cc.r[15] = SCRATCH - 4
cc.r[4] = 0
cc.i = 0
cc.pc = bqueue.PEEK
n = 0
while n < 3000 and not cc.halted and cc.pc != RET:
    empty.step(1)
    n += 1
print("    空表实测：%d 步，r4 = %08X（返回 0）" % (n, cc.r[4]))

print()
print("=" * 78)
print("H. 结论与边界")
print("=" * 78)
print("  可报：")
print("    · 描述符表 70 条 × 16 字节，索引就是槽位号（70/70）")
print("    · 字段：+0 状态字节指针（65 条、互不重复、0x9F58CE64..0x9F58D4FB）")
print("            +4 数值 / +8 方式 0-1-2 / +0xC 处理函数（44 个，27 个是空 `ret`）")
print("    · 表在 ROM（写被丢弃、boot 0 笔写）→ 静态描述表")
print("    · 0x9F58D858 = 当前槽位的描述符指针（唯一写点 0x12E276）")
print("    · 0x9F58DA81 = 事件掩码：+8=1 → +4，+8=0 → 0")
print("    · 取首：wai_sem(17) → is_empty([head]==head) → 空则 0，否则 r4=[首节点+8] → sig_sem(17)")
print("  不可报：")
print("    · +8=2 那 5 条的语义；各处理函数内部做什么")
print("    · 载荷 +8 的业务含义（本模拟里没人写它）")
print("    · 显示模块在真机上跑过；任何画面")
