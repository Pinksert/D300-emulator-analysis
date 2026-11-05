# -*- coding: utf-8 -*-
"""V24 探针：B 段内核的门 —— 服务分发表、事件标志对象、wfmode 语义。

V23 收尾留下两条边界：
  ① flag 3 位 0x200000 / 0x80 的置位者是谁；
  ② wai_flg mode=2「立即返回 E_OK」到底是什么语义（r13=0x305 来源未证）。

V24 把两条都追到底，手段三样：
  * 静态：线性扫全部 `int #0x40`，回溯 R12/R4/R5/R6/R7 常量，得到
    2300 个调用点的服务号与实参分布（set_flg 863 处、clr_flg 473 处…），
    再按 flag 3 过滤出 65 处 —— 位 0x80 的置位者是 0x12E630（显示绘制路径），
    位 0x200000 的置位者是 0x12F1A8（0x12F0xx 一组函数）。
  * 动态（内核）：陷阱 0x40668 的服务派发不是读表，而是 0x406AC 用 R12
    做索引取 16 位偏移 —— 静态读 0x40660 会读成代码。改在派发点
    0x406B2 抓 `call r12` 的目标，13 个服务一次解出。
  * 动态（对象）：在真机主循环里抓 wai_flg 的入参与查表结果，
    拿到 flag 对象的真实布局，再逐 mode 做对照实验。

A 服务分发表 / B flag 对象布局 / C wfmode 语义矩阵 / D 主事件循环结构
E 注入实验 / F 门字节 0x9F58D08F / G 42 条 ISR / H 计数器速率标定
"""
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import brealos, bflag


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def steady(idle_extra=800000):
    """boot + 拉起任务 2 + 一次派发 → 开机装配完成的主事件循环稳态。"""
    m = brealos.boot()
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=200000, spin_guard=False)
    if idle_extra:
        m.run(max_steps=idle_extra, spin_guard=False)
    return m


# ---------------------------------------------------------------- A 分发表
head("A 服务分发表：动态抓 `call r12` 的目标（静态读表会被 0x40660 的代码骗）")

m = brealos.boot()
print("陷阱入口 0x40668 的派发序列：")
for ln in m.disasm_at(0x40696, 12).splitlines():
    print("   " + ln)
print()
print("服务号 → 处理函数（实测）：")
names = {-23: "sta_tsk", -25: "ter_tsk", -27: "chg_pri", -41: "slp_tsk",
         -45: "ext_tsk", -46: "wai_flg", -47: "clr_flg", -48: "set_flg",
         -53: "wai_sem", -55: "sig_sem", -61: "rcv_msg", -63: "snd_msg",
         -83: "set_tim"}
got = {}
for svc in sorted(names):
    t = brealos.service_handler(m, svc)
    got[svc] = t
    note = ""
    if t in (0x40660, 0x40664):
        note = "  ← 空桩 `ret:d`（B 段未实现）"
    print("  %4d %-8s → %s%s" % (svc, names[svc], ("0x%08X" % t) if t else None, note))
ok = all(got[s] == brealos.SERVICES[s] for s in names)
print("\n与 brealos.SERVICES 常量表一致：", ok)

# ---------------------------------------------------------------- B flag 对象
head("B flag 对象布局：0x680008B8 起，步长 0x10")

m = steady()
print("对象表 @0x551B0 前 6 项（首字 = 对象指针）：")
for i in range(1, 7):
    e = bflag.table_entry(m, i)
    print("   flag %2d  表项 %08X → 对象 %08X" %
          (i, bflag.FLAG_TABLE + (i - 1) * 0x10, e))
o = bflag.object_addr(3)
print()
print("flag 3 对象 @%08X 原始 16 字节：%s" % (o, m.fetch_bytes(o, 16).hex(" ")))
print("  +0x00 自指       = %08X" % m.bus.rd(o + 0x00, 4))
print("  +0x04 等待 TCB   = %08X  （任务 2 的 TCB = 0x68000168）" % m.bus.rd(o + 0x04, 4))
print("  +0x08 位模式     = %08X" % m.bus.rd(o + 0x08, 4))
print("  +0x0C 表项背指   = %08X" % m.bus.rd(o + 0x0C, 4))
print()
print("17 个对象的位模式非零者：")
for r in bflag.objects(m):
    if r["pattern"] or r["waiter"] not in (0, r["addr"]):
        print("   flag %2d @%08X 位模式=%08X 等待 TCB=%08X"
              % (r["id"], r["addr"], r["pattern"], r["waiter"]))

# ---------------------------------------------------------------- C 语义矩阵
head("C wfmode 语义矩阵：bit1 = OR 等待，bit0 = 满足后清位（与教科书相反）")

print("干净机（boot 到空闲、任务 2 未起），先 set_flg(3,0x300) 再 wai_flg：")
print("  ptn     mode  说明            →  err  steps  satisfied  位模式(后)")
CASES = ((0x300, 0, "AND 成立"), (0x900, 0, "AND 不成立"),
         (0x300, 2, "OR  成立"), (0x900, 2, "OR  成立(只看一位)"),
         (0x800, 2, "OR  不成立"), (0x100, 1, "AND 成立+清位"),
         (0x300, 3, "OR  成立+清位"), (0x100, 0, "AND 成立不清"))
for ptn, mode, label in CASES:
    mm = brealos.boot()
    bflag.set_pattern(mm, 3, 0)
    brealos.kernel_call(mm, brealos.SVC_SET_FLG, r4=3, r5=0x300)
    r = bflag.wait(mm, 3, ptn, mode)
    print("  %06X  %d     %-14s →  %-4d %-6d %-9s  %08X"
          % (ptn, mode, label, r["err"], r["steps"], r["satisfied"], r["after"]))
print()
print("读法：满足 ≈100 步、不满足 ≈147–150 步 —— 多出来的那段是")
print("「登记等待 → 请求一次重调度 → 复查」的兜底路径。r12 恒为 0：")
print("内核只表示「受理」，等没等到要看位模式。mode=1/3 跑完后位模式被清。")

# ---------------------------------------------------------------- D 主循环
head("D 主事件循环：clr_flg(3,~0x200000) → 查门字节 → wai_flg → 无条件跳回")

m = steady(idle_extra=0)
print("清位点 0x120160-0x12017A：")
for ln in m.disasm_at(0x120160, 14).splitlines():
    print("   " + ln)
print()
print("等待点 0x1203AC-0x1203C8：")
for ln in m.disasm_at(0x1203AC, 15).splitlines():
    print("   " + ln)
print("   ← 0x1203C8 无条件 jmp 回 0x120160：wai_flg 的返回值没人用，")
print("     它在这里的作用只是「把 CPU 让出去一次」。")
print()
c0, n0 = m.cov.words(), len(m.flow.calls)
m.run(max_steps=50000, spin_guard=False)
win = m.flow.calls[n0:]
print("再跑 5 万步：调用 +%d 覆盖 +%d PC=%08X"
      % (len(win), m.cov.words() - c0, m.cpu.pc))
print("  本窗口服务分布：", dict(sorted(Counter(x[2] for x in win).items())))
print("  调用点分布：", dict(sorted(Counter(hex(x[1]) for x in win).items())))
print("  典型实参：", [(hex(x[3]), x[4], hex(x[5]), x[6]) for x in win[:2]])
print()
print("静态：flag 3 的 65 个调用点里，置位（set_flg）按位分组 ——")
print("  位 0x80     ← 0x12E630（显示绘制路径，0x12E138 / 0x12E154 调用）")
print("  位 0x200000 ← 0x12F1A8（0x12F0xx 一组，内部还 call 0x1263E0）")
print("  位 0x1/0x2/0x6/0x8/0x10/0x20/0x40/0x100/0x200/0x400/0x800…")
print("              ← 0x12E0EC 0x12E51A 0x12E6D4 0x12E738 0x12E796 0x12E800")
print("                0x12F05E 0x12F152 0x12F202 0x12F794 0x12F882 0x12F964 …")
print("  任务 2 自己也会置位：0x1204D6 set_flg(3,0x500)、0x120748/0x1207BC 等")

# ---------------------------------------------------------------- E 注入
head("E 注入实验：把位放进去，主循环会不会动")

for bits, label in ((0, "对照（不注入）"), (0x200000, "注入 0x200000")):
    mm = steady()
    c0, n0 = mm.cov.words(), len(mm.flow.calls)
    prev = bytes(mm.cov.buf)
    pat0 = bflag.pattern(mm, 3)
    if bits:
        r = bflag.set(mm, 3, bits)
    mm.run(max_steps=50000, spin_guard=False)
    cur = bytes(mm.cov.buf)
    new = [mm.cov.base + (i << 1) for i in range(mm.cov.n) if cur[i] and not prev[i]]
    print("  %-20s 覆盖 +%-4d 调用 +%-4d 位模式 %08X→%08X 结束 PC=%08X"
          % (label, mm.cov.words() - c0, len(mm.flow.calls) - n0,
             pat0, bflag.pattern(mm, 3), mm.cpu.pc))
    if new:
        print("      新地址：", " ".join("%X" % a for a in new[:12]))
print()
print("读法：注入 0x200000 只让 wai_flg 多走 4 个地址（41B72–41B78，满足分支）；")
print("调用方拿完结果就无条件跳回 clr_flg，位立刻被清掉 —— 所以主循环照旧。")

# ---------------------------------------------------------------- F 门字节
head("F 主循环真正的门：0x9F58D08F 那个字节，从来没被写过")

GATE = 0x9F58D08F
m = brealos.boot()
print("boot 后门字节 = %02X" % m.bus.rd(GATE, 1))
brealos.sta_tsk(m, 2)
m.raise_irq(0x3F)

writes = []
_orig_wr = m.bus.wr


def _cap(a, sz, v, pc=None):
    if a <= GATE < a + sz:
        writes.append((m.step_count, pc, v))
    return _orig_wr(a, sz, v, pc)


m.bus.wr = _cap
m.run(max_steps=1000000, spin_guard=False)
print("装配 + 主循环 100 万步：门字节写入 %d 次，当前值 %02X，调用 %d 次"
      % (len(writes), m.bus.rd(GATE, 1), len(m.flow.calls)))
prev = bytes(m.cov.buf)
for vec in range(0x10, 0x3F):
    m.raise_irq(vec)
    m.run(max_steps=20000, spin_guard=False)
m.bus.wr = _orig_wr
cur = bytes(m.cov.buf)
new = sum(1 for i in range(m.cov.n) if cur[i] and not prev[i])
print("42 条 ISR 全扫后：门字节写入 %d 次，值 %02X（新地址 %d）"
      % (len(writes), m.bus.rd(GATE, 1), new))
print("结论：门字节 boot 后是 0，装配、100 万步主循环、42 条 ISR 都没人写它 ——")
print("主循环一直走「字节为 0」那条边，wai_flg 只是空转的让出点。")
print("这就是 V23 说的「等外部事件」：缺的是**写这个字节的那个硬件**，不是中断。")

# ---------------------------------------------------------------- G ISR
head("G 42 条 ISR 谁能改 flag 3：只有两条，而且都不是 0x200000")

m = steady(idle_extra=0)
prev = bytes(m.cov.buf)
rows = []
for vec in range(0x10, 0x3F):
    n0 = len(m.flow.calls)
    p0 = bflag.pattern(m, 3)
    m.raise_irq(vec)
    m.run(max_steps=20000, spin_guard=False)
    cur = bytes(m.cov.buf)
    new = sum(1 for i in range(m.cov.n) if cur[i] and not prev[i])
    prev = cur
    rows.append((vec, new, len(m.flow.calls) - n0, p0, bflag.pattern(m, 3)))
hit = [r for r in rows if r[3] != r[4]]
print("  能改变 flag 3 位模式的向量：%d 条" % len(hit))
for v, new, dc, a, b in hit:
    print("    vec 0x%02X → 位模式 %08X→%08X（本窗口新地址 %d、调用 +%d）" % (v, a, b, new, dc))
print("  全扫结论：没有任何一条 ISR 能置 0x200000 —— 它的产生者在任务侧")
print("  0x12F1A8（0x12F0xx 组），不在中断里。0x80 同理（显示绘制路径）。")
print("  跑过 CPU 的任务：", [t["idx"] for t in brealos.states(m) if t["ran"]])

# ---------------------------------------------------------------- H 速率
head("H 计数器速率标定：只要动一个字就够，跟 0x813 没关系")

for rate in (1, 16):
    mm = create("d300-b")
    mm.hw_counter_rate = rate
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
    mm.run(max_steps=400000, spin_guard=False)
    calls = Counter(x[2] for x in mm.flow.calls)
    print("  rate=%2d：总步数 %-8d 调用 %-5d 覆盖 %-5d PC=%08X 服务分布=%s"
          % (rate, mm.step_count, len(mm.flow.calls), mm.cov.words(), mm.cpu.pc,
             dict(sorted(calls.items()))))
print()
print("读法：两档速率的总步数**完全相同** —— 等走字循环的容差是从内存读的一个字节")
print("（实测 0/1 量级），计数器动一个 tick 就满足。所以 0x813 不是这条路的延时参数，")
print("这道墙的语义只要求「会走字」。（V23 报告里「延时比例不代表真实时长」这句不再适用。）")

# ---------------------------------------------------------------- 能报不能报
head("能报 / 不能报")
print("能报：")
print("  * B 段服务分发表 13 条（服务号 → 处理函数地址），slp_tsk/ext_tsk 是空桩")
print("  * flag 对象布局：0x680008B8+(N-1)*0x10，+4 等待 TCB、+8 位模式、+0xC 表项背指")
print("  * wfmode 语义：bit1=OR、bit0=满足后清位（与教科书 μITRON 相反）")
print("  * wai_flg 不是「立即返回 E_OK」：内核只表示受理，等没等到看位模式；")
print("    主循环是忙轮询（5 万步 188 圈，clr_flg @0x12016C + wai_flg @0x1203BE）")
print("  * 主循环结构：清 0x200000 → 查门字节 0x9F58D08F → wai_flg → 无条件跳回；")
print("    wai_flg 的返回值没人用，它只是「让出一次 CPU」")
print("  * 门字节 boot 后是 0，装配/百万步/42 条 ISR 都没人写它 —— 缺的是写这个字节的硬件")
print("  * 0x80 的静态置位者在显示绘制路径 0x12E630；0x200000 在 0x12F1A8（任务侧）")
print("  * 42 条 ISR 没有一条能置 0x200000")
print("  * 计数器速率 1 vs 16 总步数完全相同：容差 0/1 量级，一个 tick 就够，0x813 与之无关")
print("不能报：")
print("  * 门字节什么时候会被写（写它的那个硬件没建模）")
print("  * 0x200000 位对应什么逻辑事件（只定位到置位点，没定位到触发条件）")
print("  * 0x813 的实际用途（已排除「等走字循环的延时参数」这一种）")
print("  * 「相机跑起来了」（真实外设事件源仍未建模）")
print()
print("⚠️ 口径：所有 set/clr/wait 都是替固件调内核，不是硬件或别的任务真的产生了事件。")
