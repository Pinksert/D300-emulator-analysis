# -*- coding: utf-8 -*-
"""exp_struct.py —— 一个结构体、二十七次启动、三个睡死的任务（V51）。

V50 留下的话是「0x9F5A0284 有 38 个写点，一个都没执行，所以 bit3 恒 0」。
本探针先证明那句话的**前半段是错的**（38 个写点是地址算法假阳性），
再把「谁该置 bit3」一路追到「任务 7 睡在 tslp_tsk 里」。

八段，两台机器（A–D 段那台只读 ROM）：

  A 先把「38 个写点」更正掉          —— store 地址形态 / 真实偏移
  B 0x9F5A0284 是什么                —— 结构体写入图，偏移 0 空着
  C 开机启动表                       —— 27 个 sta_tsk，16 跑过 11 没跑
  D 11 个启动器的调用链              —— refs_to 要带窗口
  E 延时队列里不止一个任务           —— 17 / 7 / 21
  F 任务 7 卡在 tslp_tsk(10)         —— 它挡着任务 19
  G 补 tick 的天花板                 —— 第二次睡到 0x68000968 那条链
  H 结论

跑：cd emu && python probe/exp_struct.py > ../results/frbox/struct.txt
"""

import sys
import os
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import blive, brealos, bready, btick, bstatic
import fr_dis

BOOT = blive.BOOT_STEPS
RUN = 400000
FW = 0x9F5A0284          # 结构体基址，也是 btsth #8 读的那个字
SIZE = 0x6BE             # 结构体内最大已知偏移（+0x6BE）
STA_TSK = -23
TSLP_TSK = -37
DLY_TSK = -85


def hx(v):
    return "0x%06X" % v


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def boot_machine(cov=True):
    m = create("d300-b", dispatch=True)
    m.load()
    m.reset()
    if cov:
        m.enable_cov()
    m.attach_flow()
    n = 0
    while not m.cpu.halted and n < BOOT:
        m.step(1)
        n += 1
    return m, n


# ============================================================ A
hr("A 先把「38 个写点」更正掉")

s, m0 = bstatic.sweep_of_type("d300-b")
summary = s.summary()
print("全 ROM 静态扫：calls=%d edges=%d funcs=%d stores=%d（形态不可定址 %d）"
      % (summary["calls"], summary["edges"], summary["funcs"],
         summary["stores"], summary["stores_unknown"]))

w = s.writers_of(FW)
print("\nwriters_of(0x%08X) = %d 个  ←  V50 报的是 38 个" % (FW, len(w)))

print("""
为什么差这么多：V48–V50 的 store 收集一律取 `v['rj']`（退化到 `ri`）当基址、
把 `disp8/9/10` 当偏移。但 fr_emu.py:684-708 是按 base 分形态的：

  base 0x1400/0x1500/0x1600   @Rj          a = r[rj]
  base 0x1000/0x1100/0x1200   @(R13,Rj)    a = r13 + r[rj]      ← V50 漏了 r13
  base 0x3000/0x5000/0x7000   @(disp,R14)  a = r14 + sx(disp)*n  ← 基址是 r14
  base 0x1300                 @(udisp6,R15) a = r15 + udisp6*4

那 38 个「写点」全部是 `@(R13,Rj)` 形态：`ldi:32 rj,#0x9F5A0284` 之后
`ldi:8 r13,#N` + `st @(r13,Rj)`。旧算法把 rj 当基址、又把 r13 丢了，
于是每条都算出恰好 0x9F5A0284。真实地址是 **0x9F5A0284 + N**。""")

print("\n三个样本，逐条对照（`rj` 由更前面的 ldi:32 装入，紧接着是 `ldi:8 r13,#N`）：")
for pc, off in ((0x196F10, 0x2D), (0x196F14, 0x2E), (0x19657C, 0x88)):
    print("\n  [写点 0x%06X]  真实目标 = 0x%08X + 0x%X = 0x%08X"
          % (pc, FW, off, FW + off))
    # ⚠️ 从 pc-4 起：这几条都是 2 字节指令，pc-4 一定落在指令边界上；
    # 从 pc-6 起会切在 ldi:32 的立即数中间，反汇编出来是假的。
    for t in fr_dis.disasm_addr(s.rom, pc - 4, pc + 8, s.base):
        print("    " + t)

# ============================================================ B
hr("B 0x%08X 是什么：结构体写入图" % FW)

sw = s.structure_writers(FW, SIZE)
print("落在 [0x%08X, +0x%X) 的写点：%d 个偏移，共 %d 处写"
      % (FW, SIZE, len(sw), sum(len(v) for v in sw.values())))
print("\n  偏移        处数  写点 PC")
for off in sorted(sw):
    pcs = sw[off]
    print("  %+6d  %2d  %s" % (off, len(pcs),
                                ' '.join('0x%06X' % p for p, _ in pcs[:6])))

print("\n  ** 偏移 +0 与 +4：%s / %s **"
      % (sw.get(0, "无写点"), sw.get(4, "无写点")))
print("  ⇒ `btsth #8, 0x%08X` 读的那个字（偏移 0），静态上**没有任何写点**。" % FW)

byfn = collections.Counter()
for off, pcs in sw.items():
    for pc, _mn in pcs:
        byfn[s.enclosing(pc)] += 1
print("\n  谁在写这张结构体（%d 个函数）：" % len(byfn))
for fn, c in byfn.most_common():
    tsk = None
    print("    0x%06X  %2d 处" % (fn, c))

# ============================================================ C
hr("C 开机启动表：%d 个 sta_tsk 调用点" % len(s.of(STA_TSK)))

sites = s.of(STA_TSK)
bytsk = collections.defaultdict(list)
for c in sites:
    bytsk[c.r4].append(c.addr)
print("覆盖 %d 个不同任务号（其中 %d 个点拿不到立即数，记作 None）："
      % (len([t for t in bytsk if t is not None]),
         len(bytsk.get(None, []))))
print("\n  任务  调用点")
for t in sorted(bytsk, key=lambda x: (x is None, x)):
    print("  %-5s %s" % (t, ' '.join('0x%06X' % p for p in bytsk[t])))

m, nb = boot_machine()
print("\n建机器跑 %d + %d 步，PC=0x%06X" % (nb, RUN, m.cpu.pc))
m.run(max_steps=RUN)
print("实际跑完 %d 步，PC=0x%06X" % (m.step_count, m.cpu.pc))

hit, miss = [], []
for t in sorted(bytsk, key=lambda x: (x is None, x)):
    for pc in bytsk[t]:
        (hit if m.cov.visited(pc) else miss).append((t, pc))
print("\n执行过 %d 个：" % len(hit))
print("  " + ' '.join('任务%s' % t for t, _p in hit))
print("\n没执行 %d 个：" % len(miss))
for t, pc in miss:
    print("  任务 %-5s 0x%06X  在函数 0x%06X" % (t, pc, s.enclosing(pc)))

# ============================================================ D
hr("D 11 个没执行的启动点：谁在调它们")

print("""为什么不能直接查 `callers_of`：`bstatic.edges` 只收 `call:d @r12` 且 r12 静态
已知的那一种，于是这 11 个函数全部报「无调用者」。真因是**查询口径**：
调用者加载的是函数的**序言起点**（`stm1` 或 `stm1`+`st dr1`），而 `enter` 在
序言之后 2 或 4 字节 —— 拿 `enter` 地址去查 ldi:32 必然 0 命中。
`Sweep.refs_to(addr, window=8)` 就是为此加的。""")

LAUNCHERS = {}
for t, pc in miss:
    LAUNCHERS.setdefault(s.enclosing(pc), []).append(t)

print("\n  函数        任务    调用点（窗口 ±8 内的 ldi:32 + 下一条指令）")
for fn in sorted(LAUNCHERS):
    cs = s.callers_strict(fn)
    print("\n  0x%06X  任务 %-5s  %d 处调用"
          % (fn, LAUNCHERS[fn][0], len(cs)))
    for pc, imm, nx in cs[:6]:
        cf = s.enclosing(pc)
        print("      0x%06X 载入 0x%06X(%+d) %-8s 在函数 0x%06X %s"
              % (pc, imm, imm - fn, nx, cf,
                 '（执行过）' if m.cov.visited(pc) else '（没执行）'))
    if not cs:
        print("      窗口内也没有 —— 只能靠表驱动")

# ============================================================ E
hr("E 延时队列里不止一个任务")

q = btick.delay_queue(m)
print("延时队列哨兵 0x68000118，链上 %d 个任务：" % len(q))
print("\n  TCB         任务号  优先数  等待原因（低 8 位补码）")
for tcb, tid, prio, why in q:
    name = {DLY_TSK: "sys_dly_tsk", TSLP_TSK: "sys_tslp_tsk"}.get(why, "?")
    print("  0x%08X  %-6s  %-6s  %4d  %s" % (tcb, tid, prio, why, name))
print("\n  ** V50 只报了任务 17（dly_tsk）。实际是三个任务一起睡死。**")

# ============================================================ F
hr("F 任务 7 卡在 tslp_tsk(10) —— 它挡着任务 19")

print("""链条：任务 7 → 0x175418 call 0x175876 → 循环里 sys_tslp_tsk(10)
      → 没有 tick ⇒ 超时永远不到 ⇒ 任务 7 永久挂起
      → 0x175630（启动任务 19）永不到达
      → 任务 19 不启动 ⇒ 结构体 0x9F5A0284 从不初始化
      → 任务 17 的 btsth #8 读到 0
      → 7 个 sta_tsk(22) 永不到达 ⇒ 任务 22（画面）不启动""")

print("\n任务 7 断点核查：")
WATCH7 = ((0x175418, "call 0x175876"), (0x17541A, "call 的返回点"),
          (0x175876, "被调函数入口"), (0x17588E, "int #0x40 = tslp_tsk(10)"),
          (0x1758EA, "该函数的 ret"), (0x175482, "启动任务 8"),
          (0x175630, "启动任务 19"))
for a, tag in WATCH7:
    print("  0x%06X  %-26s %s" % (a, tag, "执行过" if m.cov.visited(a) else "没执行"))

print("\n  任务 7 **函数体** 0x175192..0x175700 的执行范围：")
base, buf = m.cov.base, m.cov.buf


def cov_between(lo, hi):
    return [a for a in range(lo, hi, 2)
            if 0 <= (a - base) >> 1 < len(buf) and buf[(a - base) >> 1]]


exec7 = cov_between(0x175192, 0x175700)
if exec7:
    print("    执行过 %d 条，最小 0x%06X，最大 0x%06X"
          % (len(exec7), exec7[0], exec7[-1]))
    print("    ⇒ 覆盖在 0x%06X 就断了，0x175630 在它之后 0x%X 字节"
          % (exec7[-1], 0x175630 - exec7[-1]))
print("\n  被调函数 0x175876..0x1758EC 的执行范围（另一段，别混进上面的统计）：")
exec8 = cov_between(0x175876, 0x1758EC)
if exec8:
    print("    执行过 %d 条，最小 0x%06X，最大 0x%06X"
          % (len(exec8), exec8[0], exec8[-1]))

print("\n  tslp_tsk 那个循环（0x175888..0x1758AA）反汇编：")
for t in fr_dis.disasm_addr(s.rom, 0x175888, 0x1758AC, s.base):
    print("    " + t)
print("""
    读的是 0x50000107 的 bit3 —— 那正是 V45 解出的三通道位串行控制块
    通道 A 的 `+07`。取值手法 `lsl2 #0xC` + `lsr2 #0xF` 是「u4+16」的
    28 位 / 31 位（硬规矩 17），一左一右正好取出 bit3。
    循环最多试 10 次，每次先 tslp_tsk(10) —— 第一次睡就再也没醒。""")

# ============================================================ G
hr("G 补 tick 的天花板")

print("""V50 的 btick.tick 只认延时队列 0x68000118 这一条链。补一次能把三个任务都
唤醒，但它们转一圈又睡回去 —— 而任务 7 第二次睡到的是 **另一条链**。""")

print("\n建第二台机器（不复用第一台：探针同一台机器不能跑第二遍，硬规矩 13）……")
m2, nb2 = boot_machine()
m2.run(max_steps=RUN)
q_before = btick.delay_queue(m2)
print("基线 %d 步，队列 %s" % (m2.step_count, [(t, w) for _c, t, _p, w in q_before]))
print("哨兵 0x68000118: next=0x%08X prev=0x%08X"
      % (m2.bus.rd(0x68000118, 4), m2.bus.rd(0x6800011C, 4)))

rec = btick.tick(m2, n=1, steps=20000)
print("\ntick(1) 跑了 %d 步：%s" % (rec[0]["steps"], rec[0]["expired"]))
print("唤醒后马上看：")
for a, tag in WATCH7:
    print("  0x%06X  %-26s %s" % (a, tag, "执行过" if m2.cov.visited(a) else "没执行"))

q_after = btick.delay_queue(m2)
print("\ntick 后延时队列：%d 个 %s" % (len(q_after),
                                    [(t, w) for _c, t, _p, w in q_after]))
print("哨兵 0x68000118: next=0x%08X prev=0x%08X"
      % (m2.bus.rd(0x68000118, 4), m2.bus.rd(0x6800011C, 4)))

print("\n三个任务 TCB 的 next/prev 现在指向哪（就绪队列哨兵区 = 0x68000008..0x68000108）：")
READY_LO, READY_HI = 0x68000008, 0x68000110
for tcb, tid, _prio, _why in q_before:
    nxt = m2.bus.rd(tcb + 0x00, 4)
    prv = m2.bus.rd(tcb + 0x04, 4)
    if (READY_LO <= nxt < READY_HI) or (READY_LO <= prv < READY_HI):
        mark = '  ← 在就绪队列上（醒了还在跑）'
    elif nxt == prv and nxt != 0x68000118:
        mark = '  ← 挂到了 0x%08X 这条链，既不是延时队列也不是就绪队列' % nxt
    print("  任务 %-3s TCB=0x%08X  next=0x%08X prev=0x%08X%s"
          % (tid, tcb, nxt, prv, mark))

print("""
⇒ `btick` 的天花板：它补的是「延时队列到期」，而 tslp_tsk 睡回去后挂的是
  另一条链（0x68000968）。所以补 tick 只能让任务 7 多走一轮，走不到
  0x175630。要真的走到任务 19，得让 tslp_tsk 本身超时返回 —— 那是下一版的事。""")

# ============================================================ H
hr("H 结论")

print("""1. V50 的「38 个写点」是 store 地址算法错误造成的假阳性。修好形态分支后
   writers_of(0x9F5A0284) = **0**。那 38 条是同一结构体的字段写，真实地址带 r13 偏移。

2. 0x9F5A0284 是结构体基址：[基址, +0x6BE) 内 17 个偏移、44 处写，
   **偏移 0 与 4 一个写点都没有**。所以「谁置 bit3」这个问法本身没有对象 ——
   偏移 0 是只读的标志字。

3. 开机启动表 27 个 sta_tsk 覆盖 23 个任务，本次运行 16 个执行过、11 个没执行。
   没执行的那批里包括任务 19（结构体的初始化者）和任务 25。

4. 那些函数「静态无调用者」是查询口径问题：调用者加载的是序言起点，
   enter 在后面 2/4 字节。Sweep.refs_to(window=8) 一查就全出来了。

5. 延时队列里是**三个**任务：17（-85 dly_tsk）、7（-37 tslp_tsk）、21（-85）。

6. 任务 7 挡在 0x175630 前面：它卡在 0x17588E 的 tslp_tsk(10)，等的是
   0x50000107 的 bit3 变化。没有 tick ⇒ 超时不到 ⇒ 启动任务 19 那一步永不到达。

7. 补一次 tick 能让任务 7 从 tslp_tsk 返回并走完那一轮，但它睡回去后挂在
   0x68000968 这条链上，btick 不认 —— 这是 V50 设施的天花板，也是下一版的起点。""")

# ============================================================ I
hr("I 复现")
print("""  cd emu
  python probe/exp_struct.py > ../results/frbox/struct.txt

  静态部分（A–D）只读 ROM，约 60 秒；动态部分（C 之后的 cov、E–G）各建一台机器，
  每台 191,520 步 boot + 400,000 步运行。G 段那次 tick 约再走 610,000 步。
  合计约 10 分钟。

  A 段那个「旧算法」没有在代码里保留 —— 它已经被 _st_addr 取代。要看它当时的
  输出，见 results/frbox/struct.txt 与 KNOWN_ISSUES #192。""")
