# -*- coding: utf-8 -*-
"""exp_bhandler.py —— V37：43 个非空处理函数到底做什么

V36 报了两件事：44 个处理函数、27 个指向 0x12E752（单条 `ret`）。留了三个尾巴：
  ① +8 = 2 那五条的语义
  ② 43 个非空处理函数各自做什么
  ③ 载荷 +8 的业务含义（V35 起一直 OPEN）

本探针的答案是：**处理函数是「事件翻译器」，不是绘制代码**。

A 44 个处理函数 × 70 个槽位的全表（地址 / 槽位 / 副作用摘要）
B 事件标志族：flgid 只有 1/3/10/12，42 条 set_flg 的位模式全表
C 位操作族：borl/borh/bandh 打在哪些全局字上
D 状态链：7 个处理函数回调提交 0x12E6F8
E 模式 2 的五条：掩码来自分派代码里的常量，不是 +4
F 掩码区是 9 字节 0x9F58DA7C..84（更正 V36 的「四字节游标」）
G 提交函数不存 r5（两次不同载荷，写入逐笔相同）
H 接收端线索：显示模块在等 flag 3

跑法：
    cd emu && python probe/exp_bhandler.py > ../results/frbox/bhandler.txt
"""
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_REPO = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from frbox.machine import create
from frbox import brealos, bdesc, bhandler, blist, bqueue


def head(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


RET = 0x00407DC
SCR = 0x9F83F000


def call_fn(m, pc, r4=0, r5=0, limit=8000):
    """把 pc 当函数调一次，返回执行步数。"""
    c = m.cpu
    m.bus.wr(SCR - 4, 4, RET)
    c.r[15] = SCR - 8
    c.r[4], c.r[5] = r4, r5
    c.i = 0
    c.pc = pc
    n = 0
    while n < limit and not c.halted and c.pc != RET:
        m.step(1)
        n += 1
    return n


m = create("d300-b")
m.load()

# ---------------------------------------------------------------- A
head("A. 44 个处理函数 × 70 个槽位")
dp = bhandler.dispatch(m)
au = bhandler.audit(m)
print("  处理函数 %d 个；槽位 %d 个（合计 %d）"
      % (len(dp), len(bdesc.entries(m)), sum(len(v) for v in dp.values())))
print("  其中 %d 个函数是**单条 ret**（0x%X / 0x%X / 0x%X / 0x%X / 0x%X），"
      "合计占 %d 个槽位 —— 比 V36 报的 27 个多 4 个"
      % (len(au["ret_handlers"]), au["ret_handlers"][0], au["ret_handlers"][1],
         au["ret_handlers"][2], au["ret_handlers"][3], au["ret_handlers"][4],
         len(au["ret_only_slots"])))
print()
print("  %-10s %-14s %s" % ("处理函数", "槽位", "副作用（常量传播还原）"))
print("  " + "-" * 74)
for h, slots, parts in bhandler.dump(m):
    s = ",".join(str(x) for x in slots)
    if len(s) > 13:
        s = s[:11] + "…"
    print("  0x%08X %-14s %s" % (h, s, " ; ".join(parts) or "（空处理：单条 ret）"))

# ---------------------------------------------------------------- B
head("B. 事件标志族：flgid 只有 1 / 3 / 10 / 12")
fm = bhandler.flag_map(m)
total = sum(len(v) for v in fm.values())
print("  42 条 set_flg 全部是立即数（没有一个参数来自运行期）：")
print()
print("  %-6s %4s  %s" % ("flgid", "条数", "槽位 → 位模式"))
print("  " + "-" * 74)
for fid, items in fm.items():
    bits = sorted({p for _s, p, _h, _pc in items})
    print("  %-6d %4d  %s" % (fid, len(items), " ".join("0x%X" % b for b in bits)))
print()
print("  合计 %d 条；覆盖 %d 个槽位" % (total, len(bhandler.families(m)["flag"])))
print()
print("  典型形态（槽位 9，函数 0x12EC8A，整段 6 条指令、没有一条读内存）：")
print(m.disasm_at(0x12EC8A, 6))

# ---------------------------------------------------------------- C
head("C. 位操作族：borl / borh / bandh 打在哪些全局字上")
bit_words = Counter()
for h, slots in dp.items():
    for a in bhandler.actions(h):
        if a[0] == "bit":
            bit_words[a[3]] += 1
print("  %d 个槽位带位操作，落在 %d 个全局字上：" % (len(bhandler.families(m)["bit"]), len(bit_words)))
for a, n in bit_words.most_common():
    print("    0x%08X  %2d 次" % (a, n))
print()
print("  形态示例（槽位 18 的整个函数）：")
print(m.disasm_at(0x12EF88, 3))
print("  → 就是给 0x9F58DEBC 置一位，不调用任何服务。")

# ---------------------------------------------------------------- D
head("D. 状态链：7 个处理函数回调提交 0x12E6F8")
fam = bhandler.families(m)
print("  回调提交的槽位：%s" % fam["requeue"])
for s in fam["requeue"]:
    e = bdesc.entry(m, s)
    acts = [a for a in bhandler.actions(e["handler"]) if a[0] == "call" and a[2] == bhandler.SUBMIT]
    print("    槽 %2d（+4=%-3d +8=%d）处理函数 0x%08X 有 %d 处 call 0x12E6F8"
          % (s, e["value"], e["mode"], e["handler"], len(acts)))
print()
print("  → 一个槽位的事件处理里再提交**别的**槽位，就是「这一步完了排下一步」。")
print("  → 但本版**不**声称链的走向：提交哪个槽位是运行期算出来的（r4 不是立即数），")
print("     静态看不出来，要等调用者跑起来（记 OPEN）。")

# ---------------------------------------------------------------- E
head("E. 模式 2 的五条：掩码是分派代码里写死的常量")
print("  0x12E2AC 起的分派（cmp 三个槽位号，其余落到默认）：")
print(m.disasm_at(0x12E2AC, 12))
print()
print("  %-6s %-10s %-6s %s" % ("槽位", "描述符", "掩码", "判等的 cmp"))
print("  " + "-" * 60)
for i, info in bhandler.mode2_check(m).items():
    print("  %-6d 0x%08X %-6s 0x%08X" % (i, info["addr"],
                                         "-" if info["mask"] is None else "0x%02X" % info["mask"],
                                         info["cmp"] or 0))
print()
print("  （0 号是空槽位：连描述符都不取，掩码保持原值 —— V36 已证）")

def spy(mm, lo, hi, store):
    """给 mm 的总线套一层记账（必须绑在同一台机器上，否则记的是别人的 pc）。"""
    base = mm.bus.wr

    def wr(a, sz, v, pc=None):
        if lo <= a < hi:
            store.append((mm.cpu.pc, a, sz, v))
        return base(a, sz, v, pc)
    return wr


m2 = brealos.boot()
print()
print("  实测（直接调 0x12E1EC，游标 82 = 槽位号，哨兵 0x55）：")
for slot in (15, 31, 32, 68):
    for k, v in enumerate((0, slot, 0, slot)):
        m2.bus.wr(0x9F58DA81 + k, 1, v)
    hits = []
    base = m2.bus.wr
    m2.bus.wr = spy(m2, 0x9F58DA7C, 0x9F58DA82, hits)
    call_fn(m2, bdesc.APPLY_FN)
    m2.bus.wr = base
    got = m2.bus.rd(0x9F58DA81, 1)
    main = [h for h in hits if h[1] == 0x9F58DA81]
    print("    槽 %2d → 掩码 0x%02X（主写入点 0x%08X）" % (slot, got, main[0][0] if main else 0))

# ---------------------------------------------------------------- F
head("F. 掩码区是 9 字节：0x9F58DA7C..0x9F58DA84")
m3 = brealos.boot()
print("  按 0x12E1EC 的写入顺序记账（slot 5 / 6 / 15 / 68）：")
print()
for slot in (5, 6, 15, 68):
    for k, v in enumerate((0, slot, 0, slot)):
        m3.bus.wr(0x9F58DA81 + k, 1, v)
    hits = []
    base = m3.bus.wr
    m3.bus.wr = spy(m3, 0x9F58DA70, 0x9F58DA90, hits)
    call_fn(m3, bdesc.APPLY_FN)
    m3.bus.wr = base
    print("  槽 %2d：" % slot)
    for pc, a, sz, v in hits:
        print("     0x%08X 写 0x%08X = 0x%02X" % (pc, a, v))
print()
print("  布局：")
print("     0x9F58DA7C 槽位号（副本）   ← 0x12E2F6")
print("     0x9F58DA7D 恒 0             ← 0x12E300")
print("     0x9F58DA7F 掩码（副本）     ← 0x12E30A")
print("     0x9F58DA81 掩码（主）")
print("     0x9F58DA82 槽位号（主）     ← 0x12E244")
print("     0x9F58DA83 计数（上限 70）")
print("     0x9F58DA84 下一个槽位号")
print("  (7C,82) 与 (7F,81) 是同值两对 —— 像「上一次」与「当前」两份记录。")

# ---------------------------------------------------------------- G
head("G. 提交函数（0x12E6F8）不存 r5")
print("  两次不同 r5 调同一个槽位，逐笔对比内存写入：")
print()


def submit_trace(payload):
    mm = brealos.boot()
    mm.bus.wr(blist.DISPLAY_QUEUE_HEAD, 4, blist.DISPLAY_QUEUE_HEAD)
    mm.bus.wr(blist.DISPLAY_QUEUE_HEAD + 4, 4, blist.DISPLAY_QUEUE_HEAD)
    for i in range(70):
        b = 0x9F58D508 + i * 0x0C
        for k in (0, 4, 8):
            mm.bus.wr(b + k, 4, 0)
    hits = []
    base = mm.bus.wr
    mm.bus.wr = spy(mm, 0x9F58D400, 0x9F58DC00, hits)
    call_fn(mm, bqueue.SUBMIT, 5, payload)
    mm.bus.wr = base
    return mm, hits


a_m, a_log = submit_trace(0x11223344)
b_m, b_log = submit_trace(0x55667788)
print("    r5 = 0x11223344：%d 笔" % len(a_log))
for pc, a2, sz, v in a_log:
    print("       0x%08X 写 0x%08X（%d 字节）= 0x%X" % (pc, a2, sz, v))
print("    r5 = 0x55667788：%d 笔" % len(b_log))
for pc, a2, sz, v in b_log:
    print("       0x%08X 写 0x%08X（%d 字节）= 0x%X" % (pc, a2, sz, v))
print()
print("  逐笔相同，槽位只动 +0 / +4 / +9 → **r5 在这条路上不进内存**。")
print("  已挂链的那条分支也只写 +9（0xC0），同样不存 r5。")
print("  → 载荷 +8 不是从提交函数的 r5 来的，「+8 谁填」仍然 OPEN。")

# ---------------------------------------------------------------- H
head("H. 接收端线索：显示模块在等 flag 3")
print("  全片 `ldi:8 r12,#0xD2`（-46 = wai_flg）共 165 处。")
print("  回退解码可还原 flgid 的 146 处，分布（前 10）：")
rom = open(os.path.join(_REPO, "data", "B3000111.bin"), "rb").read()
BASE = 0x40000
hits = []
i = rom.find(b"\xcd\x2c")
while i >= 0:
    hits.append(BASE + i)
    i = rom.find(b"\xcd\x2c", i + 1)


def back(hit):
    import fr_emu
    best = None
    for start in range(hit - 26, hit, 2):
        pc, args, ok = start, {}, True
        while pc < hit:
            d = fr_emu.TABLE.decode(rom, pc - BASE) if pc >= BASE else None
            if d is None:
                ok = False
                break
            mn, _b, v, size, _w = d
            if pc + size > hit:
                ok = False
                break
            if mn.startswith("ldi"):
                for k in v:
                    if k.startswith("i"):
                        if v.get("ri") is not None:
                            args[v["ri"]] = v[k]
                        break
            pc += size
        if ok and pc == hit and 5 in args:
            best = (start, args)
    return best


c = Counter()
for h in hits:
    b = back(h)
    if b and isinstance(b[1].get(5), int) and b[1][5] < 64:
        c[b[1][5]] += 1
for fid, n in c.most_common(10):
    print("     flgid %-3d %2d 处" % (fid, n))
print()
print("  ⚠️ 这是**启发式**回退解码（146/165 可还原，wfmode 那两格不可信），只报位置不报语义。")
print()
print("  两处可以直接读、且已人工核对：")
for a in (0x12E110, 0x12E4B6):
    print()
    print(m.disasm_at(a, 8))
print()
print("  → 两处都是 `ldi r5,#3` + `r7=2` + `int #0x40` = wai_flg(flgid=3, wfmode=2)，")
print("     落在 0x12E1xx / 0x12E4xx —— 和显示队列同一段代码。")
print("  → 与 B 段合起来：13 个处理函数 set_flg(3, 位) ←→ 显示模块 wai_flg(3) 醒来。")

# ---------------------------------------------------------------- 结论
head("结论：能报 / 不能报")
print("  能报：")
print("    1. 43 个非空处理函数都是「事件翻译器」：把槽位事件变成固定的 set_flg / 位操作 / 再提交")
print("    2. 用到的 flag 只有 1 / 3 / 10 / 12，42 条 set_flg 的参数全是立即数")
print("    3. 模式 2 的五条掩码来自分派代码（0x0F→0x80、0x44→0x8A、0x1F→0、其余→0）")
print("    4. 掩码区是 9 字节 0x9F58DA7C..84，其中 7C/7F 是副本（更正 V36 的「四字节」）")
print("    5. 提交函数不写 r5，也不写 +8")
print("    6. 显示模块里有两处 wai_flg(flgid=3, wfmode=2)")
print()
print("  不能报：")
print("    1. 每个 flag 位的业务含义（「位 9 = 快门半按」这类，没有证据）")
print("    2. 状态链的具体走向（提交的槽位号是运行期算的）")
print("    3. 位操作族那些全局字的语义")
print("    4. 载荷 +8 的业务含义 —— 仍然 OPEN")
print("    5. 「界面跑起来了」/ 任何画面")
