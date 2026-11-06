# -*- coding: utf-8 -*-
"""exp_bready.py —— V39：B 段真正的就绪结构，以及任务 5 怎么被调度上去。

V38 报了「sta_tsk(5) 改了 TCB，但就绪位图 0x45984 一直是 0x2E80A800 没变」，
把任务 5 起不来的原因归给内核。本探针的答案是：**那个地址在 B 段是 ROM。**

A. 位置仲裁：0x45984 在 A 段是 RAM，在 B 段是 ROM（写丢弃、读回固件字节）
B. 真正的结构：内核区头部 0x68000000（当前 TCB / 位图 / 就绪队列哨兵）
C. sta_tsk(5) 的全部写点：入队 3、置位图第 28 位、**写 0x44 请求派发**
D. 派发器：vec 0x3F → 0x40D32，选「位图最高位」= 队列号最小 = 优先数最小
E. 两条关系：队列号 = 优先数 − 1、位 = 31 − 队列号
F. 任务 2 阻塞在哪：flag 3 的等待链，状态字节 = 4
G. 一路推下去：任务 2 → 任务 4 → **任务 5**，然后 0x12E006 自己跑完
H. 0x44 建模（dispatch=True）为什么现在还不能直接用
I. 边界与口径

跑法：cd emu && python probe/exp_bready.py > ../results/frbox/bready.txt
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from frbox import brealos, bready

RET = 0x00407DC


def hr(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------- A
hr("A. 位置仲裁：0x45984 在 B 段是 ROM，不是就绪位图")

m = brealos.boot()
A = bready.A_READY_MAP
reg = m.bus.region_at(A, 4)
print("B 段内存映射（boot 后）：")
for r in m.bus.regions:
    print("  0x%08X–0x%08X  %-4s %s" % (r.base, r.end, r.kind, r.name))
print()
print("0x%08X 落在：%s" % (A, reg))
print("读 4 字节 = 0x%08X（固件字节）" % m.bus.rd(A, 4))
before = m.bus.rd(A, 4)
m.bus.wr(A, 4, 0xDEADBEEF)
print("写 0xDEADBEEF 后读回 = 0x%08X → %s"
      % (m.bus.rd(A, 4), "写被丢弃" if m.bus.rd(A, 4) == before else "可写"))
print()
print("这项体检在 V35–V38 一直是「写丢弃」——但没人试过写一下。")
print("所以「位图恒 0x2E80A800、从不变化」不是内核没更新，是**读的是 ROM**。")
print()
print("对照 A 段（realos.py 那台）：RAM 是 0x1000–0x80000，0x45984 在 RAM 里，")
print("那才是 A 段 REALOS 的就绪位图。两段同地址不同物 —— 这是本版第一处更正。")


# ---------------------------------------------------------------- B
hr("B. 真正的结构：内核区头部 0x68000000")

m = brealos.boot()
print("boot 后（191,520 步，停在空闲任务 0x407DC）：")
print(bready.dump(m))
print()
print("头部 0x68000000 起 0x90 字节：")
print("  " + m.bus.region_at(0x68000000, 0x90).buf[:0x90].hex(" "))
print()
print("字段读法：")
print("  +0x00 当前 TCB = 0x%08X（%s）"
      % (bready.current(m), bready.tcb_name(m, bready.current(m))))
print("  +0x04 就绪位图 = 0x%08X" % bready.ready_map(m))
print("  +0x08 起每 8 字节一条就绪队列哨兵（空队列自指）")
print("  → boot 后只有队列 0 有任务：%s"
      % ", ".join(bready.tcb_name(m, t) for t in bready.queue_occupants(m, 0)))


# ---------------------------------------------------------------- C
hr("C. sta_tsk(5) 的全部写点：入队、置位、**请求派发**")

m = brealos.boot()
log = []
base_wr = m.bus.wr


def spy(addr, sz, val, pc=None):
    log.append((pc, addr, sz, val))
    return base_wr(addr, sz, val, pc)


t5 = bready.task_index(m)[bready.TASK5_ID]
print("任务 5：TCB = 0x%08X，启动前状态字节 = 0x%02X（0x10 = 休眠）"
      % (t5, bready.state_of(m, t5)))
print("启动前：位图 = 0x%08X，队列 3 = %s"
      % (bready.ready_map(m), bready.queue_occupants(m, 3) or "空"))
print()

m.bus.wr = spy
r = brealos.sta_tsk(m, bready.TASK5_ID)
m.bus.wr = base_wr
print("sta_tsk(5) → %s（%d 步）" % (r, r["steps"]))
print()
print("写点全表（PC / 地址 / 宽 / 值）：")
for pc, a, sz, v in log:
    tag = ""
    if a == bready.READY_MAP:
        tag = "  ← 就绪位图"
    elif a == bready.DISPATCH_REQ:
        tag = "  ← 「请求调度」寄存器"
    elif bready.QUEUE0 <= a < bready.QUEUE0 + 0x100:
        tag = "  ← 就绪队列头"
    elif a == t5 + 0x18:
        tag = "  ← TCB 保存栈顶"
    print("  0x%08X  wr 0x%08X  %dB  0x%08X%s" % (pc or 0, a, sz, v, tag))
print()
print("之后：位图 = 0x%08X（第 28 位 = %d），队列 3 = %s，状态字节 = 0x%02X"
      % (bready.ready_map(m), (bready.ready_map(m) >> 28) & 1,
         [bready.tcb_name(m, t) for t in bready.queue_occupants(m, 3)],
         bready.state_of(m, t5)))
print()
print("⚠️ 最后一笔 0x44 ← 1 是内核自己请求派发。默认机器的 0x44 没建模")
print("   （`dispatch=False` 是 V2–V10 的基线前提），请求落到影子存储、无人应答。")
print("   所以任务 5 起不来的真因是**没有派发请求被受理**，不是内核不认它。")


# ---------------------------------------------------------------- D
hr("D. 派发器：vec 0x3F → 0x40D32，选「位图最高的那一位」")

m = brealos.boot()
print("ICR 0x%03X（vec 0x3F 的级别）= 0x%02X —— 启用向量里级别号最大（最低优先级）"
      % (bready.DISPATCH_ICR, m.bus.rd(bready.DISPATCH_ICR, 1)))
print("向量槽 0x%08X → 0x%08X"
      % (m.cpu.dr[0] + (255 - bready.DISPATCH_VEC) * 4,
         m.bus.rd(m.cpu.dr[0] + (255 - bready.DISPATCH_VEC) * 4, 4)))
print()
print("0x40D32 起 12 条：")
print(m.disasm_at(bready.DISPATCH_ENTRY, 12))
print()
brealos.sta_tsk(m, bready.TASK5_ID)
print("sta_tsk(5) 后：位图 = 0x%08X" % bready.ready_map(m))
d = bready.dispatch(m)
print("挂一次 0x3F → %s → %s（switched=%s，跑了 %d 步）"
      % (bready.tcb_name(m, d["before"]), bready.tcb_name(m, d["after"]),
         d["switched"], d["steps"]))
print("任务 5 入口执行过？%s（它排在队列 3，前面还有队列 0 的任务 2）"
      % (m.cov.visited(bready.TASK5_ENTRY)))
print()
print("→ 选中规则：位图里最高的那一位 = 队列号最小 = 优先数最小。")


# ---------------------------------------------------------------- E
hr("E. 两条关系：队列号 = 优先数 − 1、位 = 31 − 队列号")

for tag, setup in (("boot 后", lambda mm: None),
                   ("sta_tsk(5) 后", lambda mm: brealos.sta_tsk(mm, 5))):
    mm = brealos.boot()
    setup(mm)
    ok, bad, rows = bready.map_consistent(mm)
    print("%s：队列里 %d 条任务，一致 %d / 不一致 %d" % (tag, len(rows), ok, bad))
    for row in rows:
        print("   %-6s 优先数 %-2d 队列 %-2d（预测 %-2d）位 %-2d 位图值 %d  %s"
              % (row["name"], row["prio"], row["queue"], row["expected"],
                 row["bit"], int(row["bit_set"]), "OK" if row["queue"] == row["expected"] else "≠"))
    print()


# ---------------------------------------------------------------- F
hr("F. 任务 2 阻塞在哪：flag 3 的等待链")

m = brealos.boot()
brealos.sta_tsk(m, bready.TASK5_ID)
bready.dispatch(m)                       # 空闲 → 任务 2
m.run(max_steps=200000, spin_guard=False)
cur = bready.current(m)
print("跑 20 万步后：当前 %s（TCB 0x%08X），状态字节 = 0x%02X"
      % (bready.tcb_name(m, cur), cur, bready.state_of(m, cur)))
waiters = bready.flag_waiters(m, 3)
print("flag 3 的等待链：%s"
      % ([bready.tcb_name(m, t) for t in waiters] or "空"))
print("任务 2 在里面？%s" % (brealos.records(m)[1]["tcb"] in waiters))
print()
print("flag 对象 3 @0x680008D8：%s"
      % m.bus.region_at(0x680008D8, 0x10).buf[0x8D8:0x8E8].hex(" "))
print("  +0 自己 / +4 等待者链 / +8 当前事件位 / +0xC 自己的表记录")
print()
print("它的主循环 0x120160 起：")
print(m.disasm_at(0x120160, 10))
print("  clr_flg(3, ~0x200000) → 查门字节 0x9F58D08F → 第二道门 → call 0x12E6F8")
print("  阻塞点是 wai_flg(3)，内核把 TCB 挂进 flag 3 的等待链、状态字节写 4。")


# ---------------------------------------------------------------- G
hr("G. 一路推下去：空闲 → 任务 2 → 任务 4 → **任务 5**，然后显示初始化自己跑")

m = brealos.boot()
brealos.sta_tsk(m, bready.TASK5_ID)
trail = bready.run_until(m, bready.DISPLAY_INIT, chunk=2000, max_rounds=400)
print("轨迹（步数 / 事件 / 任务 / 状态字节）：")
prev = None
for steps, kind, tcb, st in trail:
    if kind == "switch" and tcb != prev:
        print("  %7d  换到 %-6s 状态=0x%02X" % (steps, bready.tcb_name(m, tcb), st))
        prev = tcb
print()
nd = sum(1 for e in trail if e[1] == "dispatch")
sw = len({e[2] for e in trail if e[1] == "switch"})
print("补了 %d 次派发请求，换过 %d 个任务" % (nd, sw))
print()
print("任务 5 入口 0x12DFAE 执行过？%s" % m.cov.visited(bready.TASK5_ENTRY))
print("显示初始化 0x12E006 执行过？%s" % m.cov.visited(bready.DISPLAY_INIT))
print("队列哨兵 0x9F58D500 = 0x%08X（自指 = 已初始化）" % m.bus.rd(0x9F58D500, 4))
print("初始化标志 0x9F58DA85 = %d" % m.bus.rd(0x9F58DA85, 1))
print("槽位 0..4 的 +8 = %s"
      % [m.bus.rd(0x9F58D508 + i * 0x0C + 8, 1) for i in range(5)])
print()
print("→ V38 那次「手动调用 746 步」，这一版是内核把任务 5 送上去之后发生的。")
print("  但派发请求是探针补的（0x44 没建模）—— 谁补的必须说清楚。")


# ---------------------------------------------------------------- H
hr("H. 0x44 建模（dispatch=True）为什么现在还不能直接用")

from frbox.machine import create

m2 = create("d300-b", dispatch=True)
brealos.boot(m2)
print("boot 完：pc = 0x%08X，调度请求累计 = %d" % (m2.cpu.pc, m2.dispatch.requests))
r2 = brealos.sta_tsk(m2, 5)
print("sta_tsk(5) → %s（没回到哨兵 = 服务没跑完就被切走）" % r2)
print("  位图 = 0x%08X（任务 5 那一位 %s）"
      % (bready.ready_map(m2), "在" if (bready.ready_map(m2) >> 28) & 1 else "不在"))
print("  当前 TCB = 0x%08X" % bready.current(m2))
print()
print("原因：服务在临界区里把 ICR 0x46F 写成 0x10，而 0x44 的请求按写入即投递，")
print("      级别 0x10 比临界区 ILM=0x1E 更优先 —— 中断在入队之后、返回之前就打进来，")
print("      派发器在半更新状态上跑。真机上这条请求应当等内核退出临界区才被受理。")
print()
print("→ 所以本版不打开 dispatch=True 跑主线，而是用 bready.run_until() 在")
print("  「当前是空闲任务，或状态字节 ≠ 0（等待/休眠）」时补一次 0x3F：补的是硬件应答，")
print("  选谁仍然完全由固件派发器决定。这一格留给下一版（要接的是 0x44 的投递时机）。")


# ---------------------------------------------------------------- I
hr("I. 边界与口径")

print("可报：")
print("  · 0x45984 在 B 段是 ROM（写入丢弃、读回固件字节 0x2E80A800）")
print("  · 真正的就绪结构 = 内核区头部：+0x00 当前 TCB / +0x04 位图 / +0x08 起队列哨兵")
print("  · 队列号 = 优先数 − 1、位 = 31 − 队列号（boot 后与 sta_tsk 后全部一致）")
print("  · sta_tsk(5) 入队 3、置位图第 28 位、写 0x44 请求派发（PC 0x41886）")
print("  · 派发器 vec 0x3F → 0x40D32 选中优先数最小的就绪任务")
print("  · 任务 2 阻塞在 flag 3（等待链里有它的 TCB、状态字节 4）")
print("  · 任务 2 → 任务 4 → 任务 5 的换手，任务 5 上 CPU 后 0x12E006 自己跑完")
print()
print("不可报：")
print("  · 「整机自己跑起来了」—— 派发请求是探针在任务阻塞时补的，0x44 没建模")
print("  · 各任务优先数为什么是这些值（只报实测数值，业务含义无证据）")
print("  · 队列 6 在派发若干轮后出现过「非空但位图位为 0」，机制未查（只记现象）")
print("  · 任何画面（V26 定论）")
print()
print("⚠️ 轨迹里的步数是一次 boot + 显式注入的顺序结果，不是固件的正常流程。")
print("⚠️ 判「任务上 CPU」用的是覆盖率（入口地址被执行过）+ 内核区 +0x00 的当前 TCB，")
print("   两者同时成立才算，不看 pc 猜。")
