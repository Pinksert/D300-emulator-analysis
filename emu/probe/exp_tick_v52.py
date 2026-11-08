# -*- coding: utf-8 -*-
"""exp_tick_v52.py —— tick 到底该怎么补：对象队列 12、剩余 9 个 tick、和补到哪一步（V52）。

V51 收尾时留了两句：

  「补 tick 只能让任务 7 多走一轮：第二次它睡到 0x68000968 这条链上，
    而 btick 只认 0x68000118」
  「0x68000968 那条链是什么队列，没查清」

这一版把两句都答了，并顺手查出 V50 那个补全设施自己的口径错误。

七段，五台机器（A/E 只读 ROM；B / D / F1 / F2 各一台，硬规矩 13）：

  A 0x68000968 的真身        —— V33 已解出的 flag 定义表里的 ID 12
  B 老口径复现                —— tick(1) 一次把三个都拽醒
  C 固件自己怎么做 tick       —— 0x040A44 的反汇编依据
  D 严格到期                  —— 按 TCB+0x14 递减，任务 7 第 8 轮才醒
  E 那条链不是补全造的假象    —— 严格口径下仍挂 0x68000968，写入点 0x041ED2
  F 补全范围的边界            —— 只补 ID 12 vs 补全部 14 个等待任务
  G 结论 / 复现

⚠️ 全部是反事实补全。固件从没收到 tick（KI#189 未解），
   `wake_objects` 尤其是凭空把对象等待队列上的任务拽醒。

跑：cd emu && python probe/exp_tick_v52.py > ../results/frbox/tick2.txt
"""

import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import blive, bready, btick, bstatic
import fr_emu
import fr_dis

RUN = 400000
STEP = 20000
QS = 0x68000968          # flag 12 的对象控制块基址（等待队列内嵌 +0/+4）
T7 = 0x68000244
FW = 0x9F5A0284
WATCH = [
    (0x175418, "任务7 call 轮询函数"),
    (0x17541A, "call 返回点"),
    (0x175630, "启动任务 19"),
    (0x1963AE, "sta_tsk(19)"),
    (0x1960C8, "任务25 入口"),
    (0x242B92, "sta_tsk(22)"),
    (0x137EB6, "btsth #8 结构体"),
]


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def hx(v):
    return "0x%08X" % v


def snap(m, tag):
    print("  %-16s %s" % (tag, btick.describe_v52(m)))


def cov_line(m):
    return "  ".join("%s=%s" % (t, "过" if m.cov.visited(a) else "·") for a, t in WATCH)


def boot_run():
    m = blive.boot()
    m.run(max_steps=RUN, spin_guard=False)
    return m


# ============================================================ A
hr("A  0x68000968 的真身：flag 定义表里的 ID 12")

m0 = bstatic.sweep_of_type("d300-b")[1]
rom, base = m0.bus.rom, m0.bus.rom_base

print("⚠️ 这张表**不是 V52 发现的**。V33 的 KI#124 已解出：sem 表 0x54F2C（40 项）")
print("   止于 0x551B0；紧接的就是 flag 表头 0x551B0 = 0x00110010（17|16），")
print("   17 条记录从 0x551B4 起、止于 0x552C4。对象区 = 0x680008B8 + (id-1)*0x10。")
print("   V52 只是把 V51 那条「另一条链」**对回**这张已知的表。\n")
print("表项 %s 起，每项 16 字节；项 n 的地址 = 基址 + 0x10*(n-1)。"
      % hx(btick.OBJ_TABLE))
print("每项：+0x00 保留 / +0x04 **对象控制块指针** / +0x08 高半 = flag ID。")
print("对象 +0x00(next) / +0x04(prev) 是内嵌的等待队列，空时自指；+0x08 是位模式。")
print("（这一台只读 ROM、没 boot，RAM 全 0，所以**不判队列状态**；"
      "队列内容见 E 段那台跑过的机器。）\n")
print("  ID   表项地址    +4（对象）   +8 高半")
for ident in range(1, 20):
    a = btick.object_entry_addr(ident)
    q = m0.bus.rd(a + 4, 4)
    idr = (m0.bus.rd(a + 8, 4) >> 16) & 0xFFFF
    if not (0x68000000 <= q < 0x68010000):
        continue
    mark = "   ← V51 那条「另一条链」" if q == QS else ""
    print("  %-4d %s  %s  %s%s"
          % (idr or ident, hx(a), hx(q), "0x%04X" % idr, mark))

print("""
⇒ 0x68000968 不是「随机一条链」，是**事件标志（flag）ID 12 的对象控制块基址**，
  等待队列就内嵌在它的 +0x00/+0x04（空时自指）。V51 说的「另一条链」有名字了。
  ID 1..17 的对象是 0x680008B8..0x680009B8，等距 0x10 —— 一整排 flag 控制块。
  换句话说：任务 7 第二次睡到的是**等 flag 12 的任务队列**上。""")

# ============================================================ B
hr("B  老口径复现：V50/V51 的 tick(1) 到底做了什么")

m1 = boot_run()
snap(m1, "基线")
before = btick.delay_queue(m1)
print("\n  延时队列三个任务的剩余 tick（TCB+0x14）：")
for tcb, tid, _prio, why in before:
    print("    任务 %-3s TCB=%s  剩余=%-3d 等待原因=%d"
          % (tid, hx(tcb), m1.bus.rd(tcb + btick.TCB_TICKS, 4), why))

rec = btick.tick(m1, n=1, steps=STEP)
print("\n  tick(1) 一次唤醒：%s" % rec[0]["expired"])
snap(m1, "补 1 次后")
print("  任务 7 TCB next=%s prev=%s  flag12 对象 %s = %s"
      % (hx(m1.bus.rd(T7, 4)), hx(m1.bus.rd(T7 + 4, 4)), hx(QS), hx(m1.bus.rd(QS, 4))))
print("""
⚠️ 任务 7 的剩余是 9（tslp_tsk(10)），却在**第 1 轮**就被唤醒 ——
   老 expire_all() 把延时队列里所有任务无条件摘链，既没看 TCB+0x14，
   也没清 TCB+0x08 的等待位。这是 V50/V51 补全设施的口径错误。""")

# ============================================================ C
hr("C  固件自己怎么做 tick：0x040A44 那段")

print("反汇编（内核内部函数，不在任何中断向量表里 —— 48 条向量全查过）：\n")
off, end = 0x040A44 - base, 0x040AB0 - base
while off < end:
    line, size = fr_dis.line(rom, off, base)
    print("  " + line)
    off += size

print("""
  0x040A48  计数器 0x68000110（64 位）+1
  0x040A60  延时队列哨兵 0x68000118
  0x040A78  r6 = TCB+0x14（剩余 tick）；add2 #-1；bp:d 仍 >0 就跳下一个节点
  0x040A7E  st #20, r6 是延迟槽，总执行 → 每个节点每轮都减 1
  0x040A80  只有减到 <=0 才摘链
  0x040A88  r6 = TCB+0x08；and #0xFFFCFFFB → 清等待位；st 写回
  0x040A96  写 0x44 请求派发 + call 0x4185C（挂就绪队列 + 置位图）

⇒ 老 expire_all() 漏了三件事：递减 TCB+0x14、按 0 判到期、清 TCB+0x08。
  V52 的 expire_strict() 按这个顺序补齐。""")

# ============================================================ D
hr("D  严格到期：任务 7 其实要第 8 轮才醒")

m2 = boot_run()
snap(m2, "基线")
print("\n  轮  唤醒       任务7剩余  TCB+0x08  延时队列   对象哨兵")
for k in range(1, 10):
    got = btick.expire_strict(m2)
    if got:
        bready.dispatch(m2)
    m2.run(max_steps=STEP, spin_guard=False)
    print("  %-3d %-11s %-10d 0x%08X  %-10s %s"
          % (k, str([t for _c, t in got]),
             m2.bus.rd(T7 + btick.TCB_TICKS, 4), m2.bus.rd(T7 + 8, 4),
             str([t for _c, t, _p, _w in btick.delay_queue(m2)]),
             hx(m2.bus.rd(QS, 4))))
    if m2.cov.visited(0x17541A):
        print("       ↑ 第 %d 轮：0x17541A（call 返回点）执行过" % k)
print("""
⇒ 严格口径下第 1 轮只醒 17 / 21（dly_tsk(1)），任务 7 的剩余从 9 一路减到
   第 8 轮才到期。老 tick(1) 把它提前了 7 轮 —— 两版的「唤醒时刻」不是一回事。""")

# ============================================================ E
hr("E  0x68000968 不是补全设施造的假象")

print("严格口径、任务 7 到期之后：")
print("  对象哨兵 %s      = %s" % (hx(QS), hx(m2.bus.rd(QS, 4))))
print("  哨兵 +4          = %s" % hx(m2.bus.rd(QS + 4, 4)))
print("  任务 7 TCB +0    = %s" % hx(m2.bus.rd(T7, 4)))
print("  任务 7 TCB +4    = %s" % hx(m2.bus.rd(T7 + 4, 4)))
print("  非空对象队列     = %s"
      % [(i, hx(a), [hx(t) for t in n]) for i, a, n in btick.object_queues(m2) if n])
print("  （这是 m2 跑完 9 轮严格 tick 之后的状态；F 段基线的 14 个是**另一台新机器**，")
print("    两个数字不同时刻、不同机器，不混比。）")

print("\n谁把它挂上去的（静态 ROM 引用 + 动态写入点）：")
print("  0x0408BE  ldi:32 #0x680009C8, r4     内核初始化对象表")
print("  0x55260   .long 0, 0x68000968, 0x000C0000, 0     ID 12 的定义")
print("  0x041ED2  st r6, r14                  step 593276 写 0x68000968 ← TCB")
print("  0x041ED6  st r5, r14                  step 593278 写 0x6800096C ← TCB")
print("  0x041A72  ld r14, r14                 扫描该队列（读端）")
print("""
⇒ 链是合法的：flag12 对象的 next/prev 都指向唯一的 TCB，TCB 的 next/prev 都指回对象。
  V51 那句「另一条链」是**固件真实行为**，不是宽松补全把状态搞乱后的产物；
  但 V51 没说清它是什么、也没发现老口径提前了 7 轮。KI#194 据此收窄。
  仍未解：任务 7 为什么**等 flag 12**（tslp_tsk 超时后转成了 wai_flg？），
  以及 TCB+0x20 从 0xFFDB 变 0xFF56 后该字段的语义。见 KI#197。""")

# ============================================================ F
hr("F  补全范围的边界：只补 ID 12，还是把 14 个等待任务全拽醒")

print("""基线里非空对象队列上一共挂着 14 个任务（见 describe_v52 的「对象队列等待」）。
补哪几条，结果完全不同 —— 先跑两组对照，各一台机器。\n""")


def run_case(m, tag, only=None, rounds=30, marks=(1, 8, 9, 10, 15, 20, 25, 30)):
    print("  【%s】%s" % (tag, "只补 0x68000968（ID 12）" if only else "补所有非空对象队列"))
    snap(m, "基线")
    print("      " + cov_line(m))
    for k in range(1, rounds + 1):
        got = btick.expire_strict(m)
        woke = (btick.expire_object_queue(m, only) if only is not None
                else btick.expire_objects(m))
        if got or woke:
            bready.dispatch(m)
        m.run(max_steps=STEP, spin_guard=False)
        if k in marks:
            print("      轮 %-2d 延时到期=%-10s 对象唤醒=%-2d %s"
                  % (k, str([t for _c, t in got]), len(woke), cov_line(m)))
    print("      最终 step=%d PC=0x%06X  %s"
          % (m.step_count, m.cpu.pc, cov_line(m)))
    print("      结构体 %s = 0x%08X   0x50000107 = 0x%02X（bit3=%d）"
          % (hx(FW), m.bus.rd(FW, 4), m.bus.rd(0x50000107, 1),
             (m.bus.rd(0x50000107, 1) >> 3) & 1))
    print("")


m3 = boot_run()
run_case(m3, "F1 精确补全", only=QS)

m4 = boot_run()
run_case(m4, "F2 粗放补全", only=None)

print("""⇒ 两条路完全不同：
   · 精确补全（只动 ID 12 那条链）：0x17541A（call 返回点）执行过 ——
     任务 7 的轮询函数**返回了**，这是 V51 从没达到的位置。
   · 粗放补全（14 个等待任务全拽醒）：反而把整机推到另一条路上，
     0x17541A 一次都没执行，最终 PC 也不是空闲地址 0x0407DC。
     ⇒ 通用补全设施必须配 `only=` 限定范围，否则「补得越多」不等于「跑得越远」。

   两组的共同天花板：0x175630（启动任务 19）仍没执行，sta_tsk(19) /
   sta_tsk(22) 全没执行，结构体 0x9F5A0284 仍是 0。下一道关不是 tick，
   是 0x175630 之前那两个分支（0x175406 beq / 0x175418 的 cmp r4），
   以及 0x50000107 的 bit3 恒 0（V45 的位串行器件状态）。""")

# ============================================================ G
hr("G  结论与复现")

print("""1. 0x68000968 = **事件标志（flag）ID 12 的对象控制块基址**（等待队列内嵌 +0/+4）。
   ROM 表 0x551B0 是 V33（KI#124）解出的 flag 定义表，17 条止于 0x552C4；
   表项 0x55260 的 +4 就是它。对象区 0x680008B8 + (id-1)*0x10。**不是新表**。
2. V50 的 btick.expire_all() 有口径错误：无条件唤醒、不看 TCB+0x14、
   不清 TCB+0x08。固件自己的 tick 处理在 0x040A44，是按剩余 tick 递减的。
3. 老 tick(1) 把 tslp_tsk(10) 的任务 7 提前 7 轮拽醒；严格口径下它第 8 轮才到期。
4. 0x68000968 那条链在严格口径下依然存在 —— 不是补全设施造的假象。
5. 补全范围要匹配问题：只补 ID 12 能到 0x17541A（轮询函数返回了）；
   把 14 个对象等待任务全拽醒反而把整机推到另一条路，0x17541A 都到不了。
   两组都到不了 0x175630 / 任务 19 / 任务 22。

能说 / 不能说：
  · 能说：btick 只认 0x68000118 的天花板是真的，V52 给它补了通用对象队列。
  · 能说：老口径的「补 1 次 tick 三个任务都醒了」是补全设施的产物，不是固件语义。
  · 能说：精确补全能让任务 7 的轮询函数返回（0x17541A），这是 V51 没达到的位置。
  · 不能说：固件真的收到了 tick（KI#189 仍未解）。
  · 不能说：补得越多跑得越远 —— F2 是反例。
  · 不能说：任务 7 会启动任务 19 —— 0x175630 前的分支条件还没查。
  · 不能说：0x50000107 的 bit3 该由谁置（器件模型未覆盖这个状态）。

复现：
  cd emu && python probe/exp_tick_v52.py > ../results/frbox/tick2.txt
  cd emu && python frbox_selftest.py

⚠️ 五台机器各跑一遍，没有复用 cov（硬规矩 13）。
⚠️ B 段故意保留老 tick() 口径以便与 V50/V51 的历史结果逐字对照；
   新分析一律用 btick.tick_v52() / expire_strict() / expire_objects()。
""")
