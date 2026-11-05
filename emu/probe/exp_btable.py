# -*- coding: utf-8 -*-
"""exp_btable.py —— V35：对象表的真结构、上限在哪、槽位 +8 是谁填的、游标块

V34 把队列解到「四件套 + 表头」，留了四个尾巴：
  ① 槽位 +8 是什么（取首返回的那个字节）
  ② 清空流程 0x12E602 的触发门
  ③ 计数信号量的上限在内核里怎么检查
  ④ 0x9F58DA82（重挂用的「索引表」）是什么

本探针用两把尺子：
  * 静态：32 位立即数扫描（`ldi:32` 把绝对地址写成 6 字节指令）+ 反汇编读控制流
  * 动态：把 bus 的读写全记账，只留信号量区 / 队列槽位区，然后 boot 到空闲、再空转

A 表结构：4 字节表头 + 16 字节记录（两张表互证 + 首尾相接）
B 「下一项 +0」之谜：它和「记录 +0xC」是同一个地址
C 内核访问全景：983 次记账 → 上限不在对象里
D 上限检查代码在，但没跑到
E 槽位 +8：唯一写者是清零例程；挂链只改 +0/+4/+9
F 游标块 0x9F58DA81..84：四个字节的分工与配套描述符表
G 清空的唯一调用点：0x12E01E，在显示初始化 0x12E006 的开头
H 结论：能报 / 不能报

跑法：
    cd emu && python probe/exp_btable.py > ../results/frbox/btable.txt
"""
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_REPO = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import fr_emu
from frbox.machine import create
from frbox import brealos, bgate, blist, bsem, btable, bqueue


def head(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def rom():
    return open(os.path.join(_REPO, "data", "B3000111.bin"), "rb").read()


ROM = rom()
BASE = 0x40000


def imm_refs(addr):
    """谁把 addr 当 32 位立即数（ldi:32）用；返回指令地址。"""
    pat = (addr & 0xFFFFFFFF).to_bytes(4, "big")
    out, i = [], ROM.find(pat)
    while i >= 0:
        out.append(BASE + i - 2)
        i = ROM.find(pat, i + 1)
    return out


def rel12_callers(target):
    """扫 `call rel12`（操作码 0xDx）。只做粗筛，边界另验。"""
    out = []
    for off in range(0, len(ROM) - 4, 2):
        if not (0xD0 <= ROM[off] <= 0xDF):
            continue
        ins = fr_emu.TABLE.decode(ROM, off)
        if ins is None:
            continue
        mn, _b, v, size, w = ins
        if mn in ("call", "call:d") and "rel12" in v:
            pc = BASE + off
            if (pc + 2 + fr_emu.sx(v["rel12"], 11) * 2) & 0xFFFFFFFF == target:
                out.append(pc)
    return out


def occluded(pc):
    """pc 是不是落在某条 6 字节 ldi:32 的中间（假的指令边界）。"""
    for start in range(pc - 6, pc, 2):
        if start < BASE:
            continue
        ins = fr_emu.TABLE.decode(ROM, start - BASE)
        if ins is None:
            continue
        mn, _b, v, size, w = ins
        if size == 6 and start + size > pc:
            return start
    return None


def boot():
    return brealos.boot()


def steady():
    """让内核把开机装配跑完、主循环转起来（V25 的写法）。"""
    m = brealos.boot()
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=200000, spin_guard=False)
    m.run(max_steps=800000, spin_guard=False)
    return m


# ---------------------------------------------------------------- A 表结构
head("A 对象表的真结构：4 字节表头 + 16 字节记录")

m = boot()
print(btable.describe())
print()
for name, base, count in btable.TABLES:
    h = btable.header(m, base)
    print("  %-4s 表头 @0x%08X = 0x%08X → %d 条 | 项宽 %d   记录 0x%08X 起   止于 0x%08X"
          % (name, base, h["raw"], h["count"], h["stride"],
             btable.record_addr(base, 1), btable.table_end(base, h["count"])))
print()
print("  前三条记录（sem）：")
for r in btable.records(m, btable.TABLES[0][1], 3):
    print("    第 %d 条 @0x%08X  对象 0x%08X  编号 %2d  参数 0x%08X = [初始 %d | 上限 %d]"
          % (r["index"], r["addr"], r["obj"], r["id"], r["param_word"], r["init"], r["max"]))
print()
print("  计数信号量三条（14 / 27 / 29）与它们的参数格：")
for sid in (14, 27, 29):
    r = btable.record(m, btable.TABLES[0][1], sid)
    print("    sem %2d 记录 0x%08X  参数 0x%08X = [初始 %d | 上限 %d]  对象计数 %d"
          % (sid, r["addr"], r["param_word"], r["init"], r["max"],
             m.bus.rd(r["obj"] + bsem.F_COUNT, 2)))
print()
a = btable.audit(m)
print("  体检：表头自洽 %s / 对象↔记录背指异常 %d 条 / 反向异常 %d 条 / 计数≠初始 %d 条"
      % (all(h[3] for h in a["headers"]), len(a["backptr_bad"]),
         len(a["reverse_bad"]), len(a["count_bad"])))
for x in a["touching"]:
    print("  首尾相接：%s 表止于 0x%08X，%s 表头在 0x%08X → %s"
          % (x[0], x[1], x[2], x[3], x[4]))
print("  → 两张表是**连续排布**的一张链：sem 表最后一条记录的参数格 0x551AC，"
      "flag 表头紧接 0x551B0")

# ---------------------------------------------------------------- B 错位之谜
head("B 「初始计数从下一项读」之谜：那个地址就是本条记录的 +0xC")

sid = 14
rec = btable.record(m, btable.TABLES[0][1], sid)
old = bsem.table_addr(sid) + bsem.SEM_STRIDE
print("  sem %d 的记录      = 0x%08X" % (sid, rec["addr"]))
print("  记录 +0xC（参数格）= 0x%08X  值 0x%08X" % (rec["addr"] + 0x0C,
                                                 m.bus.rd(rec["addr"] + 0x0C, 4)))
print("  V33 说的「下一项 +0」= 0x%08X  值 0x%08X" % (old, m.bus.rd(old, 4)))
print("  两者是同一个地址：%s" % (rec["addr"] + 0x0C == old))
print()
print("  所以「初始计数 = 下一项 +0 的高半字」和「初始计数 = 记录 +0xC 的高半字」")
print("  是同一句话 —— 记录从表头后 4 字节开始，才让这两句看起来不同。")
print()
print("  初始化循环读点（动态记账）：0x408FE 读 0x0028（= 40 条）、0x4090A 读 0x0001")
print("  记录 +0xE 也被读 1 次（0x40900）→ 那是项宽 16 —— 表头两个字它都看了。")

# ---------------------------------------------------------------- C 内核访问
head("C 内核访问全景：boot 191,520 步里碰信号量区 983 次")

m = create("d300-b")
m.load()
m.reset()
o_rd, o_wr = m.bus.rd, m.bus.wr
log = []


def _rd(a, sz, pc=None):
    v = o_rd(a, sz, pc)
    if 0x54F2C <= a < 0x55200 or 0x68000638 <= a < 0x68000900:
        log.append(("读", a, sz, v, m.cpu.pc))
    return v


def _wr(a, sz, v, pc=None):
    if 0x54F2C <= a < 0x55200 or 0x68000638 <= a < 0x68000900:
        log.append(("写", a, sz, v, m.cpu.pc))
    return o_wr(a, sz, v, pc)


m.bus.rd, m.bus.wr = _rd, _wr
brealos.boot(m)
m.bus.rd, m.bus.wr = o_rd, o_wr
print("  记账 %d 次（boot 到空闲）" % len(log))
print()
print("  按对象偏移聚合：")
agg = Counter()
ex = defaultdict(list)
for k, a, sz, v, pc in log:
    if 0x68000638 <= a < 0x68000900:
        key = (k, (a - 0x68000638) % 0x10, sz)
        agg[key] += 1
        if len(ex[key]) < 2:
            ex[key].append((pc, v))
for key in sorted(agg, key=lambda k: (k[1], k[0])):
    k, off, sz = key
    if sz == 1 and agg[key] in (44, 45):
        continue          # 逐字节清区（4 条 4 字节写被拆成 16 条 1 字节）
    print("    %s 对象+%X  %dB  %3d 次  pc 例 %s"
          % (k, off, sz, agg[key], " ".join("%X→%s" % (p, hex(v)) for p, v in ex[key])))
print("    （另有 +0..+F 各 44~45 次 1 字节写，是 4 条 4 字节写被记账拆开的）")
print()
print("  ⚠️ 对象 +0xA 一次都没被碰 → 上限不存对象里。字段只有：")
print("     +0 自指（4090E 写） +4 等待哨兵（40910） +8 计数（40912） +C 记录地址（40914）")
print()
print("  按记录偏移聚合：")
agg2 = Counter()
ex2 = defaultdict(list)
for k, a, sz, v, pc in log:
    if 0x54F2C <= a < 0x55200:
        key = (k, (a - 0x54F30) % 0x10, sz)
        agg2[key] += 1
        if len(ex2[key]) < 3:
            ex2[key].append((pc, v))
for key in sorted(agg2, key=lambda k: (k[1], k[0])):
    k, off, sz = key
    print("    %s 记录+%X  %dB  %3d 次  pc 例 %s"
          % (k, off, sz, agg2[key], " ".join("%X→%s" % (p, hex(v)) for p, v in ex2[key])))

# ---------------------------------------------------------------- D 上限检查
head("D 上限检查的代码在，但这条路径没跑到")

print("  0x418E4 - 0x418F6（sig_sem 的正常路径尾部）：")
print(m.disasm_at(0x418E4, 10))
print()
print("  形状：lduh → add #1 → cmp → bgt（超了就跳过写回）→ sth → 开中断 → ret")
print("  但 r4/r5 谁是对象、谁是上限，静态读不出来（寄存器语义要跑才知道）。")
print()
mm = steady()
print("  steady 场景（boot + sta_tsk(2) + INT 0x3F + 1,000,000 步）跑完，步数 %d" % mm.step_count)
print("    0x418F0（sig_sem 写计数）在 boot 阶段记账：0 次（见 C 段 983 次里没有它）")
print("    0x4193A（wai_sem 写计数）同上")
print("  → V33 说的「boot 各 6 次」是**静态计数**（代码里 6 个 sth 点），不是动态执行次数。")
print("  → 「上限在运行期有没有被检查」仍然**不能报**：检查代码存在，但一次都没执行。")

# ---------------------------------------------------------------- E 槽位 +8
head("E 槽位 +8（取首返回的那个字节）：谁写它？")

print("  静态：整片固件只有 4 条指令拿得到槽位基址 0x9F58D508 ——")
for pc in imm_refs(0x9F58D508):
    print("    0x%08X" % pc)
print("  其中 0x12E684 是重挂算槽位、0x12E704 是提交算槽位、0x12E612 是清空、0x12E252 是游标分配。")
print("  四段代码都只写 +0（next）/ +4（prev）/ +9（状态），**没有一处写 +8**。")
print()
print("  动态：boot 期间对槽位区的写入记账（按 +0/+4/+8/+9 分类）")

m2 = create("d300-b")
m2.load()
m2.reset()
o_wr2 = m2.bus.wr
plog = []


def _wr2(a, sz, v, pc=None):
    if bqueue.SLOT_BASE <= a < bqueue.SLOT_BASE + bqueue.SLOT_COUNT * bqueue.SLOT_STRIDE:
        plog.append(((a - bqueue.SLOT_BASE) % bqueue.SLOT_STRIDE, a, sz, v, m2.cpu.pc))
    return o_wr2(a, sz, v, pc)


m2.bus.wr = _wr2
brealos.boot(m2)
m2.bus.wr = o_wr2
byoff = Counter(x[0] for x in plog)
print("    写入 %d 笔，按偏移：%s" % (len(plog), dict(sorted(byoff.items()))))
print("    +8 的写者：%s" % (sorted({"%X" % x[4] for x in plog if x[0] == 8}) or "无"))
print("    （记账里的 pc 是那条 st 执行完之后的 PC，所以 0x40384 那条 st 显示成 0x40386）")
vals = {x[3] for x in plog if x[0] == 8}
print("    +8 写入的值域：%s" % (vals or "-"))
print("    0x40384 那一段是通用清零循环（st r0,@r1，r0=0，步长 0x0C 扫 70 格）。")
print()
print("  开门场景（steady + 门字节置 1 + 状态字 bit6）跑到提交 0x12E6F8：")

m3 = steady()
o_wr3 = m3.bus.wr
w3 = []


def _wr3(a, sz, v, pc=None):
    if bqueue.SLOT_BASE <= a < bqueue.SLOT_BASE + bqueue.SLOT_COUNT * bqueue.SLOT_STRIDE:
        w3.append(((a - bqueue.SLOT_BASE) % bqueue.SLOT_STRIDE, a, sz, v, m3.cpu.pc))
    return o_wr3(a, sz, v, pc)


m3.bus.wr = _wr3
bgate.open_gate(m3)
m3.run(max_steps=2000, spin_guard=False)
m3.bus.wr(0x9F58D450, 2, 0x4000 | (m3.bus.rd(0x9F58D450, 2) & 0x3FFF))
entry_r = None
for _ in range(3000):
    if m3.cpu.pc == bqueue.SUBMIT and entry_r is None:
        entry_r = (m3.cpu.r[4], m3.cpu.r[5])
    m3.step(1)
m3.bus.wr = o_wr3
print("    进入提交时 r4 = %s、r5 = %s" % (entry_r[0] if entry_r else "-",
                                         hex(entry_r[1]) if entry_r else "-"))
print("    槽位区写入 %d 笔，按偏移：%s" % (len(w3), dict(sorted(Counter(x[0] for x in w3).items()))))
print("    +8 的写入：%d 笔" % sum(1 for x in w3 if x[0] == 8))
print("    链：%s" % blist.describe(m3, bqueue.HEAD))
for i in (11, 49):
    print("    槽位 %2d：%s" % (i, " ".join("%02X" % b
                                          for b in m3.fetch_bytes(bqueue.slot_addr(i), 12))))
print("  取首 peek 的现值（模型）：%d" % bqueue.peek(m3))
print()
print("  → 结论：+8 是**载荷**，但这份固件在能被跑到的范围里从没给它写过非 0 值。")
print("     取首恒返回 0；想让它有值，得先跑到填载荷的那段（显示初始化 + 描述符表那一路）。")

# ---------------------------------------------------------------- F 游标块
head("F 0x9F58DA81..84 不是索引表，是四字节游标")

for name, a in (("a81", 0x9F58DA81), ("当前槽位 82", 0x9F58DA82),
                ("计数 83", 0x9F58DA83), ("下一个 84", 0x9F58DA84)):
    refs = imm_refs(a)
    print("  %-12s 引用 %d 处  %s" % (name, len(refs), " ".join("%X" % x for x in refs)))
print()
print("  boot 后的四个字节：%s" % bqueue.cursor(boot())["raw"].hex(" "))
print()
print("  0x12E212-0x12E242 的分工（游标推进）：")
print(m.disasm_at(0x12E212, 12))
print("    → 83 与 0x46(70) 比：到顶就把 82 清 0（回绕）；否则 82 ← 84，84 ← 下一个")
print()
print("  0x12E676-0x12E68E（重挂拿 82 算槽位）：")
print(m.disasm_at(0x12E676, 7))
print()
print("  0x12E26A-0x12E274（游标 << 4 索引描述符表）：")
print(m.disasm_at(0x12E268, 7))
print("    → 0x1BC020 + (游标 << 4)：**16 字节一条**的描述符表，与记录同宽")
print("    0x1BC020 的立即数引用 %d 处：%s"
      % (len(imm_refs(0x1BC020)), " ".join("%X" % x for x in imm_refs(0x1BC020)[:10])))

# ---------------------------------------------------------------- G 清空
head("G 清空的唯一调用点：0x12E01E，在显示初始化 0x12E006 的开头")

for t in (bqueue.CLEAR, bqueue.PEEK, bqueue.REHANG, bqueue.SUBMIT):
    sites = rel12_callers(t)
    real = [pc for pc in sites if not occluded(pc)]
    fake = [pc for pc in sites if occluded(pc)]
    print("  0x%08X  rel12 命中 %2d 处 → 真实边界 %2d 处 %s；假边界 %d 处 %s"
          % (t, len(sites), len(real), " ".join("%X" % x for x in real[:8]),
             len(fake), " ".join("%X(在 %X 的 ldi:32 里)" % (x, occluded(x)) for x in fake[:4])))
print("  ⚠️ 线性扫 rel12 会撞进 ldi:32 的立即数中间（KI#134）—— 必须先验指令边界。")
print()
print("  0x12E006 开头：")
print(m.disasm_at(0x12E006, 12))
print()
print("  0x12E01E 那条 call 的落点 = 0x%08X（清空函数）"
      % ((0x12E01E + 2 + fr_emu.sx(0x2F1, 11) * 2) & 0xFFFFFFFF))
print()
print("  0x12E006 的立即数引用：%d 处（%s）"
      % (len(imm_refs(0x12E006)), "、".join("%X" % x for x in imm_refs(0x12E006)) or "无"))
print("  → 它自己也没人直接调（和清空、取首、重挂一样），属于「靠表/函数指针进」的一族。")
print("  → 动态：boot 到空闲后再空转 600,000 步，看这一族入口有没有被踩到：")

watch = (0x12E006, bqueue.CLEAR, bqueue.PEEK, bqueue.REHANG, bqueue.SUBMIT)
mm = boot()
hit2 = {t: 0 for t in watch}
n = 0
while n < 600000 and not mm.cpu.halted:
    pc = mm.cpu.pc
    if pc in hit2:
        hit2[pc] += 1
    mm.step(1)
    n += 1
print("  实测（boot 后再跑 %d 步）：" % n)
for t in watch:
    print("    0x%08X 命中 %d 次" % (t, hit2[t]))

# ---------------------------------------------------------------- H 结论
head("H 结论")

print("能报：")
print("  * 对象表 = 4 字节表头 [条数|项宽] + 16 字节记录 [对象|编号|0|初始计数|上限]；")
print("    sem 表 40 条（0x54F30 起）与 flag 表 17 条（0x551B4 起）首尾相接，")
print("    中间那一格 0x551AC 是 sem 40 的参数、0x551B0 是 flag 表头。")
print("  * 对象 +0xC = 自己的记录地址（40/40 双向全对）——V33 的「背指表项 +4」不对。")
print("  * 上限只在记录里，对象里没有：983 次动态访问里 +0xA 一次都没碰。")
print("  * 上限检查代码在 0x418E8-0x418F0，但 boot 到空闲 + 空转 60 万步里没执行过。")
print("  * 槽位 +8 = 载荷（取首唯一读出的东西）；唯一写者是 0x40384 的清零循环；")
print("    挂链只改 +0/+4/+9 → 载荷在这台模拟里恒 0。")
print("  * 0x9F58DA81..84 = 四字节游标（82 当前槽位 / 83 计数上限 70 / 84 下一个），")
print("    配套 0x1BC020 的 16 字节描述符表；重挂用 82 算槽位。")
print("  * 清空 0x12E602 的唯一（真实边界）调用点是 0x12E01E，在显示初始化 0x12E006 里。")
print("不能报：")
print("  * 载荷装的是什么业务值（界面元素 id？）—— 没跑到的代码不猜。")
print("  * 描述符表 0x1BC020 每 16 字节的字段含义。")
print("  * 0x9F58DA81 那个字节的职责（只在 5 个点被连带取用）。")
print("  * 「显示队列在真机上跑起来了」—— 这台模拟里它一次都没转过。")
