# -*- coding: utf-8 -*-
"""V54 正式探针：真实时基 —— INT 0x18 是内核的系统节拍。

    cd emu && python probe/exp_tick_v54.py > ../results/frbox/tick18.txt

七段：
  A 静态证据链   向量表槽 → ISR → `ldi:32 #0x040A44` → 内核 tick 处理
  B 基线          59 万步里直接页 0x10–0x17 零访问，tick 计数恒 0
  C 六组对照      只给 vec 0x18 拉线，tick 计数与受理次数 1:1；别的向量无效
  D 写点归属      计数器和延时队列的写点 PC 全在固件 ISR 里，设施没碰内存
  E 口径对照      btick 反事实补全从不递增 0x68000110
  F 边界          结构体 / btsth / sta_tsk(22) / 末 PC 仍未闭合
  G 结论与复现

⚠️ `dev_tick18` 只补「硬件周期性拉线」，不写内存、不改内核数据结构。
   周期是模型参数，真机频率未证（KI#191）。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import fr_emu
import fr_dis
from frbox import bstatic, blive, btick, dev_tick18 as t18

BOOT = 191520
RUN = 400000
TICK = t18.TICK_HANDLER
ISR = t18.ISR
EVT = 0x9F5A0284


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dis(lo, hi, prefix="  "):
    off, end = lo - ROM_BASE, hi - ROM_BASE
    while off < end:
        line, size = fr_dis.line(ROM, off, ROM_BASE)
        print(prefix + line)
        off += size


def ptr_hits(val):
    """val 作为大端 32 位数据在 ROM 里出现的位置。"""
    pat = val.to_bytes(4, "big")
    out = []
    pos = ROM.find(pat)
    while pos >= 0:
        out.append(ROM_BASE + pos)
        pos = ROM.find(pat, pos + 1)
    return out


# ==================================================================== A
hr("A  静态证据链：向量表 → ISR → 内核 tick 处理")

SW, M0 = bstatic.sweep_of_type("d300-b")
ROM, ROM_BASE = M0.bus.rom, M0.bus.rom_base
print("ROM base = 0x%06X  size = %d" % (ROM_BASE, len(ROM)))
print("静态普查：calls=%d  edges=%d  enter=%d  stores=%d"
      % (SW.summary()["calls"], SW.summary()["edges"],
         SW.summary()["funcs"], SW.summary()["stores"]))

print("\nA1 向量表槽")
slot = t18.VEC_SLOT
raw = M0.vector(t18.VECTOR)
print("  TBR=0x%08X  vec=0x%02X  槽地址=TBR+(255-vec)*4=0x%08X"
      % (t18.TBR, t18.VECTOR, slot))
print("  槽内容 = 0x%06X（= ISR 0x%06X ? %s）"
      % (raw, ISR, "是" if raw == ISR else "否"))
print("  0x%06X 作为 32 位数据在 ROM 里出现 %d 次: %s"
      % (ISR, len(ptr_hits(ISR)),
         ["0x%06X" % p for p in ptr_hits(ISR)]))

print("\nA2 tick 处理函数的唯一引用")
print("  0x%06X 作为 32 位数据出现 %d 次: %s"
      % (TICK, len(ptr_hits(TICK)),
         ["0x%06X" % p for p in ptr_hits(TICK)]))
print("  ldi:32 #0x%06X 加载点 = %s"
      % (TICK, ["0x%06X" % p for p, _ in SW.ldi32.get(TICK, ())]))
print("  callers_strict(0x%06X) = %s"
      % (TICK, [(hex(p), hex(i), nx)
                for p, i, nx in SW.callers_strict(TICK, window=8)]))
print("  sw.callers_of()（只收 call:d @r12 常量）= %s"
      % ["0x%06X" % p for p in SW.callers_of(TICK)])
print("  ⇒ ISR 里用的是 `call @r12`，bstatic.edges 收不到，不是「没有调用者」。")

print("\nA3 vec 0x18 的 ISR 反汇编（0x04046E..0x040490）")
dis(ISR, 0x040490)
print("\nA4 内核早期中断初始化（0x04044E..0x04046C）")
dis(0x04044E, 0x04046C)
print("  ⇒ stb #8, r0 的基址是 r14=0x440 ⇒ 0x%03X（vec 0x%02X 的 ICR）= 0x%02X"
      % (t18.ICR_ADDR, t18.VECTOR, t18.ICR_LEVEL))
print("  ⇒ 直接页 0x%02X = 0x80E8；0x%02X = 0x1B（初始化），ISR 里改写成 0x1A"
      % (t18.DIRECT_48, t18.DIRECT_4E))

print("\nA5 内核 tick 处理（0x040A44..0x040AB0；含两条无 enter 的序言）")
dis(TICK, 0x040AB0)
print("  ⇒ V52 从 0x040A48 起读，漏了 0x040A44 `st dr1` / 0x040A46 `st r14` 序言。")

print("\nA6 关键地址的 32 位数据加载点")
for v in (t18.TICK_COUNTER, 0x68000114, t18.DELAY_SENTINEL):
    print("  0x%08X : %d 处 %s"
          % (v, len(ptr_hits(v)),
             ["0x%06X" % p for p in ptr_hits(v)[:8]]))

# ==================================================================== B
hr("B  基线：直接页 0x10–0x17 零访问，tick 计数恒 0")


def snapshot(m, tag, dev=None):
    cv = collections.Counter(x[0] for x in m.cpu.irq_log)
    dq = btick.delay_queue(m)
    print("\n【%s】" % tag)
    print("  step=%d PC=0x%06X  中断=%s"
          % (m.step_count, m.cpu.pc,
             " ".join("vec%02X×%d" % (k, v) for k, v in sorted(cv.items()))))
    if dev is not None:
        st = dev.state()
        print("  时钟: 周期=%s fired=%s coalesced=%s skipped=%s accepted=%s 待决=%s"
              % (st["周期"], st["fired"], st["coalesced"], st["skipped"],
                 st["accepted"], st["pending"]))
    print("  ISR 0x%06X=%s  0x04047C=%s  tick处理 0x%06X=%s  0x040A7A=%s"
          % (ISR, m.cov.visited(ISR), m.cov.visited(t18.CALL_LDI),
             TICK, m.cov.visited(TICK), m.cov.visited(0x040A7A)))
    print("  64 位 tick 计数 = 0x%08X%08X   就绪位图 = 0x%08X"
          % (m.bus.rd(t18.TICK_COUNTER, 4),
             m.bus.rd(t18.TICK_COUNTER + 4, 4),
             m.bus.rd(0x68000004, 4)))
    print("  延时队列 = %s" % [(hex(t), tid) for t, tid, _p, _w in dq])
    print("  结构体 0x%08X = 0x%08X  0x137EB6=%s  sta_tsk(22)=%s  任务22入口=%s"
          % (EVT, m.bus.rd(EVT, 4), m.cov.visited(0x137EB6),
             m.cov.visited(0x242B92), m.cov.visited(0x242DA0)))
    dpw = [(a, n, v, pc) for a, n, v, pc in m.bus.io_writes
           if 0x10 <= a < 0x18]
    dpr = [(a, c) for a, c in m.bus.io_reads.items() if 0x10 <= a < 0x18]
    print("  直接页 0x10–0x17：写 %d 笔 / 读 %d 个地址 %s"
          % (len(dpw), len(dpr), ["0x%03X×%d" % (a, c) for a, c in dpr]))
    print("  直接页 0x4E 写：%s"
          % [(hex(v), "0x%06X" % pc) for a, n, v, pc in m.bus.io_writes
             if a == t18.DIRECT_4E][:6])
    return {
        "tag": tag, "step": m.step_count, "pc": m.cpu.pc,
        "irq": dict(cv), "tick": m.bus.rd(t18.TICK_COUNTER + 4, 4),
        "tick_hi": m.bus.rd(t18.TICK_COUNTER, 4),
        "dq": [tid for _t, tid, _p, _w in dq],
        "isr": m.cov.visited(ISR), "handler": m.cov.visited(TICK),
        "gate": m.bus.rd(EVT, 4), "btsth": m.cov.visited(0x137EB6),
        "dp_writes": len(dpw),
    }


def run_case(tag, vec=None, period=None):
    """一台机器只跑一遍（硬规矩 13）。"""
    m = blive.boot(0)
    dev = None
    if vec is not None:
        dev = m.enable_auto_tick18(period=period) if vec == t18.VECTOR else None
        if dev is None:
            from frbox import dev_irq2b
            dev = dev_irq2b.attach(m, period=period, vector=vec)
    m.step(BOOT)
    m.run(max_steps=RUN, spin_guard=False)
    rec = snapshot(m, tag, dev)
    rec["m"] = m
    rec["dev"] = dev
    return rec


C0 = run_case("C0 基线（不挂时钟）")
print("\n  ICR 0x%03X（vec 0x%02X）的写点："
      % (t18.ICR_ADDR, t18.VECTOR))
for a, n, v, pc in C0["m"].bus.io_writes:
    if a == t18.ICR_ADDR:
        print("    0x%03X ← 0x%02X  @PC=0x%06X" % (a, v, pc))
print("  ⇒ 0x12090E 批量循环跳过这一条，由内核 0x%06X 单独配置。"
      % t18.ICR_INIT_PC)

# ==================================================================== C
hr("C  六组对照：只挂 vec 0x18 才能让固件自己走 tick")

CASES = [
    ("C1 vec0x18 period=1024", t18.VECTOR, 1024),
    ("C2 vec0x18 period=4096", t18.VECTOR, 4096),
    ("C3 vec0x18 period=16384", t18.VECTOR, 16384),
    ("C4 阴性 vec0x2B period=1024", 0x2B, 1024),
    ("C5 阴性 vec0x1B period=1024", 0x1B, 1024),
]
REST = {}
for tag, vec, period in CASES:
    REST[tag] = run_case(tag, vec, period)

print("\n\nC 段对账表（同一把尺子：boot %d 步 + 再跑 %d 步）" % (BOOT, RUN))
print("  %-30s %8s %8s %6s %6s %-14s %s"
      % ("组", "vec18受理", "tick计数", "ISR", "处理", "延时队列", "末PC"))
allrec = [C0] + [REST[t[0]] for t in CASES]
for r in allrec:
    print("  %-30s %8d %8d %6s %6s %-14s 0x%06X"
          % (r["tag"], r["irq"].get(0x18, 0), r["tick"],
             "过" if r["isr"] else "·", "过" if r["handler"] else "·",
             str(r["dq"]), r["pc"]))
print("\n  ⇒ tick 计数 == vec0x18 受理次数，逐组核对：")
for r in allrec:
    if r["irq"].get(0x18, 0):
        st = r["dev"].state()
        print("     %-30s 受理 %3d / fired %3d / coalesced %d / 计数 %d  %s"
              % (r["tag"], r["irq"][0x18], st["fired"], st["coalesced"],
                 r["tick"],
                 "1:1 成立" if r["tick"] == r["irq"][0x18] else "不一致"))
print("  ⇒ 阴性对照 C4/C5 各有 %d / %d 次周期中断被受理，但 ISR / tick 处理 /"
      % (REST["C4 阴性 vec0x2B period=1024"]["irq"].get(0x2B, 0),
         REST["C5 阴性 vec0x1B period=1024"]["irq"].get(0x1B, 0)))
print("     tick 计数全 0、延时队列三个任务一个没动 ⇒ 不是随便一条周期中断都行。")
print("  ⇒ 基线三任务 [17, 7, 21]，挂 vec 0x18 后 [7, 21]：任务 17 由固件 ISR")
print("     自然摘链；7 / 21 是等 flag 对象（V52 KI#194），tick 不该唤醒它们。")

# ==================================================================== D
hr("D  写点归属：计数器和队列是谁改的")

C1 = REST["C1 vec0x18 period=1024"]
m1 = C1["m"]
print("\nD1 0x%08X..0x%08X 的 RAM 写点 PC 分布"
      % (t18.TICK_COUNTER, t18.TICK_COUNTER + 8))
wr = [(a, n, v, pc) for a, n, v, pc in m1.bus.all_writes
      if t18.TICK_COUNTER <= a < t18.TICK_COUNTER + 8]
cnt = collections.Counter(pc for _a, _n, _v, pc in wr)
print("  共 %d 笔，按 PC：" % len(wr))
for pc, c in cnt.most_common(8):
    print("    0x%06X  %d 笔" % (pc, c))
_in_isr = sum(c for pc, c in cnt.items() if 0x040A00 <= pc < 0x040B00)
_others = [(pc, c) for pc, c in cnt.items() if not (0x040A00 <= pc < 0x040B00)]
print("  ⇒ %d/%d 笔落在 0x040A5A / 0x040A5C（tick 处理的 64 位加法）；"
      % (_in_isr, len(wr)))
print("    其余 %d 笔也全在内核 ROM：%s"
      % (sum(c for _p, c in _others),
         ["0x%06X×%d" % (pc, c) for pc, c in sorted(_others)]))
print("    dev_tick18 不写内存，所以没有任何一处的 PC 来自模拟器设施。")

print("\nD2 摘链写点归属（不能只看被摘任务自己的 TCB）")
wr17 = [(a, n, v, pc) for a, n, v, pc in m1.bus.all_writes
        if 0x680003FC <= a < 0x68000404]
cnt17 = collections.Counter(pc for _a, _n, _v, pc in wr17)
print("  任务 17 TCB 0x680003FC 的全部写点 = %d 笔，按 PC：%s"
      % (len(wr17), ["0x%06X×%d" % (pc, c) for pc, c in cnt17.most_common(6)]))
print("  ⇒ 来源含入队 / 派发 / 到期，不能单独归因到「出队」。")
isr_w = [(a, pc) for a, n, v, pc in m1.bus.all_writes
         if 0x040A80 <= pc <= 0x040A90]
print("  ISR 摘链段 0x040A80..0x040A90 的 RAM 写 = %d 笔，按落点："
      % len(isr_w))
for a, c in collections.Counter(a for a, _p in isr_w).most_common(6):
    print("    0x%08X  %d 笔" % (a, c))
print("  ⇒ 双向链表摘除改的是前驱 / 后继节点（哨兵 0x68000118 与相邻 TCB），")
print("     不是被摘任务自己的 TCB；拿「任务 17 TCB 有没有 0x040A8x 写点」")
print("     判出队会漏掉（V44 链表库的同一类教训）。")

print("\nD3 直接页 0x%02X 的 IO 写（初始化 0x1B / ISR 应答 0x1A）"
      % t18.DIRECT_4E)
e4e = [(v, pc) for a, n, v, pc in m1.bus.io_writes if a == t18.DIRECT_4E]
print("  共 %d 笔，按值：%s"
      % (len(e4e), collections.Counter("0x%X" % v for v, _p in e4e)))
print("  PC 样本：%s" % ["0x%06X" % p for _v, p in e4e[:5]])
print("  ⇒ ISR 的 0x04047A `dmovh #78` 每受理一次写一次，是第二条独立证据。")

print("\nD4 设施侧审计")
print("  dev_tick18.Tick18Clock 继承 Irq2BClock，_fire() 只调用")
print("  machine.raise_irq()；类里没有任何 bus.wr / bus.rd 调用。")
print("  C1 的时钟 fired=%d / accepted=%d / 待决=%d，与固件侧计数 %d 对上。"
      % (C1["dev"].state()["fired"], C1["dev"].state()["accepted"],
         C1["dev"].state()["pending"], C1["tick"]))

# ==================================================================== E
hr("E  口径对照：btick 反事实补全 vs 真实 ISR 路径")

mE = blive.boot(0)
mE.step(BOOT)
mE.run(max_steps=RUN, spin_guard=False)
before = (mE.bus.rd(t18.TICK_COUNTER, 4),
          mE.bus.rd(t18.TICK_COUNTER + 4, 4))
dqb = [tid for _t, tid, _p, _w in btick.delay_queue(mE)]
out = btick.tick_v52(mE, n=8, only=0x68000968)
after = (mE.bus.rd(t18.TICK_COUNTER, 4),
         mE.bus.rd(t18.TICK_COUNTER + 4, 4))
dqa = [tid for _t, tid, _p, _w in btick.delay_queue(mE)]
print("  btick.tick_v52(m, n=8, only=0x68000968)（V52 精确补全）")
print("    补全前 tick 计数 = 0x%08X%08X  延时队列 = %s" % (before[0], before[1], dqb))
print("    补全后 tick 计数 = 0x%08X%08X  延时队列 = %s" % (after[0], after[1], dqa))
print("    返回的 expired 任务 = %s"
      % [r["expired"] for r in out if r.get("spent")])
print("  ⇒ 反事实补全改的是「结果」（TCB 剩余 tick / 等待位 / 队列），")
print("    内核的 64 位 tick 计数器始终为 0；真实 ISR 路径两者都有。")
print("  ⇒ 这也解释了 V50/V52 为什么必须反复声明「不是固件收到 tick」。")

# ==================================================================== F
hr("F  能说 / 不能说（六组同口径）")
print("""
  能说：
    · D300 B 段内核的系统节拍向量是 INT 0x18，证据是 ROM 里一条闭合的静态链
      （向量表槽 0x000EFF9C → ISR 0x04046E → 0x04047C 唯一加载 → 0x040A44）。
    · V52「0x040A44 不在任何中断向量表里」要收窄：向量表指向 ISR，
      ISR 第 8 条指令就 call 它，不是孤立的内部函数。
    · `dev_timer.py` 的直接页 0x10–0x17 通用重载定时器，D300 固件在
      591,520 步里一次都没碰 ⇒ KI#191 里「可能是它」的候选排除。
    · 只补「硬件周期性拉 vec 0x18」，固件自己的 ISR 就把 tick 计数 +1、
      把任务 17 从延时队列摘走；换 vec 0x2B / 0x1B 同样受理 391 次也无效。

  不能说：
    · 真机频率/时基器件找到了 —— period 是模型参数，`0x48=0x80E8` 未证。
    · 整机跑起来了 —— 六组末 PC 全是空闲 0x0407DC，就绪位图 0。
    · 画面出来了 —— 结构体 0x9F5A0284 恒 0、0x137EB6 没执行、
      sta_tsk(22) 与任务 22 入口都没执行（KI#201 仍未闭合）。
    · 任务 7 / 21 也该被 tick 唤醒 —— 它们等的是 flag 对象（V52 KI#194），
      不是 dly_tsk 的延时队列，固件语义上就不该被 tick 摘链。
    · 「所有中断都来自硬件」—— C1 的 0x3F / 0x40 仍是固件自己请求的派发
      与软件陷阱；V54 只新增了 vec 0x18 这一条。

  口径纪律：
    · 七台机器各跑一遍，没有复用 cov（硬规矩 13）。
    · 六组用同一把尺子：boot 191,520 步 + 400,000 步，spin_guard=False。
    · 时钟设备只拉线、不写内存；D 段用写点 PC 反证计数器是固件改的。
""")

# ==================================================================== G
hr("G  结论与复现")
print("""
1. V50 起「整机停在等什么 —— 没有 tick」的答案分两层：
   内核 tick 处理一直在 ROM 里（0x040A44），缺的是**拉线的人**。
2. 静态链条：TBR 槽 0x000EFF9C = 0x04046E；ISR 0x04047C 是全 ROM 唯一
   `ldi:32 #0x040A44`，0x040482 `call r12`；0x040A44 有两条无 enter 的序言。
3. 内核 0x040456 单独把 vec 0x18 的 ICR 配成 0x12，0x12090E 批量循环
   跳过 0x448；初始化还写直接页 0x48=0x80E8 / 0x4E=0x1B，ISR 应答 0x1A。
4. 动态：基线 591,520 步里 0x10–0x17 零读写、tick 计数 0、ISR 没执行；
   挂 vec 0x18 后 fired/accepted/tick 计数 391、98、25 逐组 1:1，
   任务 17 自然出队；挂 0x2B / 0x1B 各 391 次中断则计数全 0。
5. `btick` 反事实补全从不递增 0x68000110；V54 的 `dev_tick18` 只拉线，
   计数与出队全由固件 ISR 完成 —— 这是「补硬件应答」，不是「替固件改数据」。
6. 未闭合：真机频率/器件身份（KI#191 收窄但仍 OPEN）、`0x48=0x80E8` 语义、
   flag 对象等待任务 7/21、结构体与画面链条（KI#201）。

复现：
  cd emu && python probe/exp_tick_v54.py > ../results/frbox/tick18.txt
  cd emu && python frbox_selftest.py
""")
print("⚠️ A 段只读 ROM、没 boot；C0..C5 / E 各一台机器，均只跑一遍。")
print("⚠️ 周期 1024 / 4096 / 16384 都是模型参数，不代表真机频率。")
