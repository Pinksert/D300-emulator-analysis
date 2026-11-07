# -*- coding: utf-8 -*-
"""exp_btick.py —— 整机为什么会停：缺 tick、缺 bit3、缺任务 25（V50）。

六段，三台机器（静态那台只读 ROM）：

  A 这台机器有没有 tick            —— 定时器 / 中断来源 / 延时队列
  B 任务 17 睡在哪、为什么         —— 它的第一条 dly_tsk
  C 补一次 tick 之后               —— 醒了，转一圈，又睡
  D 7 个 sta_tsk(22) 被什么挡住    —— 三重条件，最后一关是 bit3
  E 谁该置 bit3                    —— 任务 25 与它的启动器
  F 结论 / G 复现

跑：cd emu && python probe/exp_btick.py > ../results/frbox/btick.txt
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import blive, brealos, bready, btick, bstatic
import fr_dis

BOOT = blive.BOOT_STEPS
RUN = 600000
T17 = 0x137B12
T17_END = 0x1384BC
T25 = 0x1960C8
STUB = 0x242B8A
LAUNCH22 = 0x242B92
FLAGWORD = 0x9F5A0284
SITES = (0x137EC8, 0x13809A, 0x138118, 0x13816E, 0x138192, 0x1381C2, 0x13826E)


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
    return m


# =============================================================== A
hr("A  这台机器上到底有没有 tick")
m = boot_machine()
print("boot 完 %d 步：%s" % (m.step_count, btick.describe(m)))
m.run(max_steps=RUN, spin_guard=False)
d = btick.diagnose(m)
print("再跑 %d 步：%s" % (RUN, btick.describe(m)))
print()
print("重载定时器（直接页 0x10–0x17）：csr=0x%04X  reload=%d  下溢 %d 次"
      % (d["timer_csr"], m.timer.reload, d["timer_underflows"]))
print("硬件中断被接受 %d 次。按向量分：" % d["irq_taken"])
from collections import Counter
cv = Counter(e[0] for e in m.cpu.irq_log)
for v, k in sorted(cv.items()):
    why = ""
    if v == 0x3F:
        why = "  ← 0x44 请求锁存器（V45）软件派发"
    elif v == 0x40:
        why = "  ← int #0x40 软件陷阱（不是硬件中断）"
    print("   向量 0x%02X  %d 次%s" % (v, k, why))
print("  → 没有一条是周期中断：既没有定时器下溢，也没有别的设备按周期敲门。")
print()
print("延时队列（哨兵 0x%08X）里有 %d 个任务：" % (btick.SENTINEL, d["delay_len"]))
SVC = {-85: "dly_tsk", -37: "?", -46: "wai_flg", -21: "slp_tsk"}
for tcb, tid, prio, why in d["delay"]:
    print("   TCB=0x%08X  任务 %-2s  优先 %2d  等的服务 %4d（%s）"
          % (tcb, tid, prio, why, SVC.get(why, "?")))
print()
print("就绪位图 = 0x%08X，就绪任务 %d 个，当前 TCB=0x%08X（%s）"
      % (d["ready_map"], d["ready_tasks"], d["cur"],
         bready.tcb_name(m, d["cur"])))

# =============================================================== B
hr("B  任务 17 睡在哪、为什么")
m = boot_machine()
first = []
_orig = m.cpu.step
def spy_b():
    if m.cpu.pc == 0x1391F0 and not first:
        first.append(m.step_count)
    _orig()
m.cpu.step = spy_b
m.run(max_steps=600000, spin_guard=False)
m.cpu.step = _orig
SLEPT = first[0] if first else 0
print("任务 17 第一次执行 0x1391F0 是在 step %d" % SLEPT)
print("  那一条指令：",
      list(fr_dis.disasm_addr(m.bus.rom, 0x1391F0, 0x1391F4, m.bus.rom_base))[0])
print("  服务号 0xAB → %d = dly_tsk（延时 r4 个 tick，这里 r4=1）"
      % btick.sx8(0xFFAB))
t17 = bready.task_index(m)[17]
print("  之后的终态：任务 17 状态=%d（4=等待）  TCB+0x20 高半=0x%04X → 服务 %d"
      % (bready.state_of(m, t17),
         (m.bus.rd(t17 + btick.TCB_WHY, 4) >> 16) & 0xFFFF,
         btick.sx8(m.bus.rd(t17 + btick.TCB_WHY, 4) >> 16)))
cov17 = [a for a in range(T17, T17_END, 2) if m.cov.visited(a)]
print("  它最后执行到 0x%06X（入口 +0x%X），后面 %d 字节一次都没跑"
      % (cov17[-1], cov17[-1] - T17, T17_END - cov17[-1]))
dq = btick.delay_queue(m)
print("  延时队列里现在有 %d 个：%s" % (len(dq), [(t[1], t[3]) for t in dq]))
print("  任务 17 在链上：%s" % any(t[0] == t17 for t in dq))

# =============================================================== C
hr("C  补一次 tick 之后：醒了，走多远，又停在哪")
m = boot_machine()
n = 0
while not m.cpu.halted and n < BOOT:
    m.step(1)
    n += 1
m.run(max_steps=400000, spin_guard=False)
body = []
_orig = m.cpu.step
def spy():
    if T17 <= m.cpu.pc < T17_END:
        body.append(m.cpu.pc)
    _orig()
m.cpu.step = spy
rec = btick.tick(m, n=1, steps=120000)
m.cpu.step = _orig
for r in rec:
    print("tick %d：到期 %s  → 跑完 pc=0x%06X  cur=%s"
          % (r["tick"], r["expired"], r["pc"],
             bready.tcb_name(m, r["cur"])))
hit = sorted(set(body))
print("\n任务 17 体内执行了 %d 个不同地址，范围 0x%06X..0x%06X"
      % (len(hit), hit[0], hit[-1]))
print("  不给 tick 时只到 0x137B5E（入口 +0x4C）—— 差了 %d 字节" % (hit[-1] - 0x137B5E))
for a in (0x137B60, 0x137EC8, 0x13842A, STUB, LAUNCH22):
    print("    %s  %s" % (hx(a), "跑过" if m.cov.visited(a) else "—"))
print("\n最后：%s" % btick.describe(m))
left = btick.delay_queue(m)
print("  延时队列还剩 %d 个：%s" % (len(left), [(t[1], t[3]) for t in left]))
print("  → 任务 17 转完一整圈又 dly_tsk(1) 睡回去了；就绪 0 个，整机再次静止。")
print("  → 也就是说：**只要 tick 一直来，它就一直转；转多少圈都过不去 bit3 那一关。**")

# =============================================================== D
hr("D  7 个 sta_tsk(22) 被什么挡住")
s, ms = bstatic.sweep_of_type("d300-b")
rom, base = ms.bus.rom, ms.bus.rom_base
print("第 1 个调用点 0x137EC8 前面的条件链：")
for t in fr_dis.disasm_addr(rom, 0x137E80, 0x137ED4, base):
    mark = ""
    if t.startswith("00137EB6") or t.startswith("00137EB8") or t.startswith("00137EC8"):
        mark = "   ←"
    print("  " + t + mark)
print()
print("「0x9F5A0284 的 bit3」这一关：")
print("  0x137EB0  ldi:32 r0, #0x9F5A0284 / 0x137EB6 btsth #8, r0（测 bit3）")
print("  0x137EB8  bne → 0x137EC2（bit3=1 才去 call 0x242B8A）")
print("  0x137EBA  jmp 0x13832C（bit3=0 直接跳走）")
print()
m = boot_machine()
seen = []
_ow = m.bus.wr
def hw(a, sz, v, pc=0):
    if FLAGWORD <= a < FLAGWORD + 4:
        seen.append((m.step_count, pc, v))
    return _ow(a, sz, v, pc)
m.bus.wr = hw
m.run(max_steps=990000, spin_guard=False)
m.bus.wr = _ow
print("动态（99 万步）：0x%08X 现在 = 0x%08X，被写 %d 次 %s"
      % (FLAGWORD, m.bus.rd(FLAGWORD, 4), len(seen), seen[:4]))
w = s.writers_of(FLAGWORD)
print("静态：能算出确切地址的写点 %d 个，按 64 KB 段分：" % len(w))
from collections import Counter
buck = Counter((pc >> 16) << 16 for pc, _mn in w)
for k, v in sorted(buck.items()):
    print("   0x%06X–0x%06X  %d 个" % (k, k + 0xFFFF, v))
print("  前 8 个：%s" % [hx(pc) for pc, _mn in w[:8]])

# =============================================================== E
hr("E  谁该置 bit3：任务 25 从没启动")
allsta = [c for c in s.calls if c.svc == -23]
print("全 ROM sta_tsk 调用点 %d 个，按任务号：" % len(allsta))
print("  %s" % sorted(Counter(c.r4 for c in allsta).items(),
                     key=lambda kv: (kv[0] is None, kv[0])))
print()
for tid in (22, 25):
    c = [x for x in allsta if x.r4 == tid]
    print("任务 %d：%d 个启动点 %s" % (tid, len(c), [hx(x.addr) for x in c]))
    for x in c:
        f = s.enclosing(x.addr)
        print("     %s 在函数 %s 里，该函数的静态调用者 %s"
              % (hx(x.addr), hx(f), [hx(a) for a in s.callers_of(f)]))
print()
print("任务 25 的启动器 0x195F96 里的系统调用（服务名见 flow.SVC_NAMES）：")
SVCN = {-20: "ref_tsk（查状态）", -23: "sta_tsk（启动）", -24: "?",
        -25: "ter_tsk（终止）", -46: "wai_flg（等）", -47: "clr_flg",
        -48: "set_flg"}
for x in [y for y in s.calls if 0x195F96 <= y.addr < 0x1960CC]:
    nm = SVCN.get(x.svc, "?")
    print("   %s  %4d %-16s r4=%s r5=%s" % (hx(x.addr), x.svc, nm, x.r4, x.r5))
print("   → 起一个子任务干活、等它回话、再把它干掉 —— 典型的一次性协作者。")
print()
print("动态（D 段那台 99 万步）验证这两个入口有没有跑过：")
for tid, ent in ((25, 0x1960C8), (22, 0x242DA0), (19, 0x1963EC), (24, 0x1985BE)):
    print("   任务 %2d 入口 %s  %s" % (tid, hx(ent),
                                   "跑过" if m.cov.visited(ent) else "从没执行"))

# =============================================================== F
hr("F  结论")
print("整机不是「跑不动」，是「跑完了所有能跑的事，然后全停在等」。")
print()
print("1) **缺 tick。** 重载定时器从没被固件使能（csr=0、下溢 0 次）；")
print("   中断记录里没有一条周期中断（69 次 vec 0x3F 是 0x44 软件派发、")
print("   161 次 vec 0x40 是 int #0x40 软件陷阱）。延时队列 0x68000118")
print("   只进不出，dly_tsk 的任务永久挂起 —— 任务 17 在 step %d 睡了就再没醒。" % SLEPT)
print()
print("2) **补上 tick 才看得到真分叉。** 补 tick 后任务 17 一路跑到 0x13842A，")
print("   但每圈都会在 0x137EBA jmp 0x13832C，跳过 7 个 sta_tsk(22)。")
print("   最后一关是 0x9F5A0284 的 bit3（0x137EB6 btsth #8）——")
print("   实测 99 万步里那个字一次都没被写过，恒 0x00000000。")
print()
print("3) **该置 bit3 的任务自己没启动。** 静态写点分布在 0x168xxx(4) /")
print("   0x196xxx(30) / 0x1FFxxx(2) / 0x244xxx(2)；任务 25（入口 0x1960C8）")
print("   从没执行，它的启动器 0x195F96 静态无引用（全 ROM 没有一处")
print("   ldi:32 #0x195F96）。")
print()
print("所以画面链路上缺的按顺序是：tick → bit3 → 任务 25 → 任务 22。")
print()
print("不可报：真实画面；「补上 tick 就能出画面」（bit3 那关还是死的）；")
print("        tick 的真实来源（重载定时器只是模型，不代表 D300 的时基）；")
print("        0x195F96 属哪只任务（静态找不到引用，只能靠动态才可能撞上）。")

hr("G  复现")
print("cd emu && python probe/exp_btick.py > ../results/frbox/btick.txt")
print()
print("btick 做的事（每次 tick）：摘延时链 → 状态清 0 → 挂就绪队列 → 置位图 → 派发")
print("⚠️ 这是反事实补全，不是固件行为。")
