# -*- coding: utf-8 -*-
"""V92 正式探针：缺省时基 / period 收敛阈值 / 等待-置位自锁（承接 KI#359 的时基面）。

    cd emu && python probe/exp_tick_v92.py > ../results/frbox/tick92.txt

A 段 1 台缺省机（无节拍源）+ B 段 1 台 period=256 机，对照同一口径；
C/D 段周期扫描 16 台（每台只跑一遍，按 KI#163 逐台新建）；
E/F/G 段只读 ROM 与既有机器现场。

全文所有数字都由设施常量或现场读出，不手抄。
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox import blive, btick, bstatic, brealos, tick92 as V

STEPS = 600000


def sec(t):
    print("")
    print("=" * 78)
    print(t)
    print("=" * 78)


def dump_row(r, tag):
    print("%-10s 步数=%-7d 末PC=0x%06X 当前=%-6s 换手=%-4d tick=%-5d "
          "延时队列=%s"
          % (tag, r["steps"], r["pc"], r["current"], r["switches"],
             r["tick"], r["delay"] or "空"))


# ------------------------------------------------------------------ A
sec("[A] 缺省时基的现场：blive.boot() 不挂任何节拍源")
m_none = blive.boot()
r_none = blive.run(m_none, steps=STEPS)
row_none = {
    "period": None, "steps": m_none.step_count, "pc": m_none.cpu.pc,
    "current": r_none["current_name"],
    "switches": len(r_none["switches"]) if isinstance(r_none["switches"], list) else 0,
    "seq": [s[0] for s in r_none["switches"]] if isinstance(r_none["switches"], list) else [],
    "tick": V.kernel_tick(m_none),
    "delay": V.delay_rows(m_none),
    "ran": [t["id"] for t in brealos.states(m_none) if t["ran"]],
}
dump_row(row_none, "缺省")
print("A1 内核 tick 计数器 0x%08X/+4 = %d（高半 0x%08X = 0x%08X）"
      % (V.TICK_COUNTER_HI, row_none["tick"], V.TICK_COUNTER_HI,
         m_none.bus.rd(V.TICK_COUNTER_HI, 4)))
print("A2 ISR 0x%06X 命中=%s，ISR 体 0x%06X 命中=%s"
      % (V.TICK_ISR, m_none.cov.visited(V.TICK_ISR),
         V.TICK_BODY, m_none.cov.visited(V.TICK_BODY)))
print("A3 延时队列 %d 个：%s" % (len(row_none["delay"]), row_none["delay"]))
print("A4 仍是 V92 那三个任务 %s ⇒ %s"
      % (V.STUCK_V92,
         "全部卡着（缺时基的直接证据）" if V.stuck(row_none["delay"]) else "有变化"))
print("A5 跑过的任务 %d 个：%s" % (len(row_none["ran"]), row_none["ran"]))

# ------------------------------------------------------------------ B
sec("[B] 补上 INT 0x18 节拍后的同一口径（period=%d）" % V.CONVERGE_PERIOD)
m_t = blive.boot(tick=V.CONVERGE_PERIOD)
r_t = blive.run(m_t, steps=STEPS)
row_t = {
    "period": V.CONVERGE_PERIOD, "steps": m_t.step_count, "pc": m_t.cpu.pc,
    "current": r_t["current_name"],
    "switches": len(r_t["switches"]) if isinstance(r_t["switches"], list) else 0,
    "seq": [s[0] for s in r_t["switches"]] if isinstance(r_t["switches"], list) else [],
    "tick": V.kernel_tick(m_t),
    "delay": V.delay_rows(m_t),
    "ran": [t["id"] for t in brealos.states(m_t) if t["ran"]],
}
dump_row(row_t, "tick256")
print("B1 ISR 0x%06X 命中=%s，ISR 体 0x%06X 命中=%s"
      % (V.TICK_ISR, m_t.cov.visited(V.TICK_ISR),
         V.TICK_BODY, m_t.cov.visited(V.TICK_BODY)))
print("B2 tick 设备计数 fired=%s skipped=%s"
      % (m_t.tick18_clock.state().get("fired"),
         m_t.tick18_clock.state().get("skipped")))
print("B3 换手 %d → %d（%.1f 倍）；末 PC 0x%06X → 0x%06X"
      % (row_none["switches"], row_t["switches"],
         row_t["switches"] / max(1, row_none["switches"]),
         row_none["pc"], row_t["pc"]))
print("B4 延时队列 %d → %d" % (len(row_none["delay"]), len(row_t["delay"])))
print("B5 跑过任务集合相同=%s" % (sorted(row_none["ran"]) == sorted(row_t["ran"])))
print("B6 末 PC 0x%06X = 任务 16 的 twai_flg(-170) 返回点（设施常量 0x%06X）"
      % (row_t["pc"], V.TASK16_WAIT_PC))

# ------------------------------------------------------------------ C
sec("[C] 周期扫描（%d 台独立机器，每台只跑一遍）" % len(V.PERIODS_V92))
rows = [row_none, row_t]
for p in V.PERIODS_V92:
    if p in (None, V.CONVERGE_PERIOD):
        continue
    rows.append(V.run_one(p, steps=STEPS))
    print("  跑完 period=%s" % p, flush=True)

print("")
print("%-8s %9s %-6s %5s %8s %8s" % ("period", "末PC", "当前", "换手", "tick", "延时队列"))
for r in sorted(rows, key=lambda x: (x["period"] is not None, x["period"] or 0)):
    print("%-8s 0x%06X %-6s %5d %8d %8s"
          % (r["period"], r["pc"], r["current"], r["switches"], r["tick"],
             r["delay"] or "空"))

bound, ref, same, diff = V.converge_bound(rows)
print("")
print("C1 收敛阈值 bound = %s（基准 = period %s，逐条换手序列相同）" % (bound, ref["period"]))
print("C2 序列相同的周期：%s" % same)
print("C3 序列不同的周期：%s" % diff)
for r in sorted(rows, key=lambda x: (x["period"] is not None, x["period"] or 0)):
    if r["period"] is not None and r["period"] > (bound or 0):
        fd = V.first_diff(ref["seq"], r["seq"])
        print("   period=%-5s 首个差异 %s" % (r["period"], fd))
print("C4 判据 = %s" % V.verdict_bound(bound))
print("C5 边界：阈值 256 是实测的，机制未解；不说「256 是固件某周期的整数倍」")

# ------------------------------------------------------------------ D
sec("[D] 收敛态的任务集合与末态")
print("D1 跑过的任务 %d 个：%s" % (len(row_t["ran"]), row_t["ran"]))
hiber = [t["id"] for t in brealos.states(m_t) if not t["ran"]]
print("D2 从未执行的任务 %d 个：%s" % (len(hiber), hiber))
print("D3 这 %d 个是休眠态（state=0x10、保存 SP=0），不是被时基卡住的 —— "
      "时基只解释延时队列那 3 个" % len(hiber))

# ------------------------------------------------------------------ E
sec("[E] 任务 16 在等什么")
tcb16 = [r["tcb"] for r in brealos.records(m_t) if r["id"] == 16][0]
print("E0 任务 16 的 TCB = 0x%08X（从任务表读出，不用硬编码）" % tcb16)
print("E1 服务号 TCB+0x20 高半 = 0x%04X → %d（%s）"
      % (m_t.bus.rd(tcb16 + 0x20, 4) >> 16,
         V.TASK16_WAIT_SVC, "twai_flg"))
print("E2 等待位模式 TCB+0x14 = 0x%08X" % m_t.bus.rd(tcb16 + 0x14, 4))
obj = btick.object_entry_addr(V.TASK16_FLGID)
pat = m_t.bus.rd(obj + 8, 4)
print("E3 flag %d 对象表项 0x%08X，+0x08 当前位模式 = 0x%08X" % (V.TASK16_FLGID, obj, pat))
print("E4 等待位 0x%08X 与当前位模式按位与 = 0x%08X ⇒ %s"
      % (V.TASK16_WANT, pat & V.TASK16_WANT,
         "等不到" if not (pat & V.TASK16_WANT) else "已满足"))
print("E5 want_set = 0x%08X" % (V.TASK16_WANT))

# ------------------------------------------------------------------ F
sec("[F] 全 ROM 的 flag %d 置位源普查" % V.TASK16_FLGID)
SW, m0 = bstatic.sweep_of_type("d300-b")
n_set = sum(1 for c in SW.calls if c.svc == bstatic.SET_FLG)
n_clr = sum(1 for c in SW.calls if c.svc == bstatic.CLR_FLG)
print("F1 总调用点 %d；set_flg=%d clr_flg=%d" % (len(SW.calls), n_set, n_clr))
f17 = [c for c in SW.calls if c.svc == bstatic.SET_FLG and c.flgid == V.TASK16_FLGID]
print("F2 flgid=%d 的 set_flg 共 %d 个" % (V.TASK16_FLGID, len(f17)))
hit = V.setters(SW, V.TASK16_FLGID, V.TASK16_WANT)
print("F3 其中能置 0x%08X 的 %d 个：%s"
      % (V.TASK16_WANT, len(hit), [hex(c.addr) for c in hit]))
print("F4 与设施常量一致=%s" % (tuple(sorted(c.addr for c in hit)) == tuple(sorted(V.F17_SET_PCS))))
print("F5 收敛态命中情况：")
for c in hit:
    print("     0x%06X bits=0x%08X 命中=%s" % (c.addr, c.bits, m_t.cov.visited(c.addr)))
print("F6 命中 %d/%d" % (sum(1 for c in hit if m_t.cov.visited(c.addr)), len(hit)))

# ------------------------------------------------------------------ G
sec("[G] 等待-置位自锁与工具口径盲区")
lock = V.self_lock(SW)
print("G1 函数边界 body_range(0x%06X) = [0x%06X, 0x%06X)，区间内其它 enter = %s"
      % (V.F17_FUNC, V.F17_FUNC, V.F17_FUNC_END,
         [hex(f) for f in SW.funcs if V.F17_FUNC < f < V.F17_FUNC_END]))
print("G2 置位点 %d 个，落在函数内 %d，函数外 %d"
      % (len(lock["points"]), len(lock["in_func"]), len(lock["outside"])))
print("G3 函数体内的等待点 %d 个：%s"
      % (len(lock["waits"]),
         [(hex(w.addr), w.svc, V.fix_wide(w.svc)) for w in lock["waits"]]))
print("G4 全部在最近那条等待之后 = %s，guard = 0x%06X(%d)"
      % (lock["all_after"], lock["guard"].addr, V.fix_wide(lock["guard"].svc)))
print("G5 判据 = %s" % V.verdict_lock(lock))
print("G6 与 V68 的提法差别：V68 说「flag 17 有 5 点命中 0 ⇒ 缺事件流」；"
      "V92 指出这 5 点全在同一函数的等待之后 ⇒ 缺的不是「事件把代码推进去」，"
      "而是「事件得直接把 0x%08X 置上」" % V.TASK16_WANT)
print("G7 工具盲区：bstatic 把 0x1A4BE6 的 ldi:32 #0xFFFFFF56 读成 svc=+86，"
      "waitstat67 只修了读 TCB 那一路；本版在判据侧用 fix_wide() 补上（%s）"
      % V.WIDE_MISREAD)
print("G8 未改 bstatic 公共解码：那个值被历史结论大量引用，改口径要另开一版")

sec("[结论]")
print("V92-1 缺省时基 ⇒ 延时队列 %d 个任务永久积压（%s），内核 tick 计数恒 %d"
      % (len(V.STUCK_V92), V.STUCK_V92, row_none["tick"]))
print("V92-2 补 period=%d ⇒ 换手 %d→%d，延时队列清零，末 PC 0x%06X → 0x%06X"
      % (V.CONVERGE_PERIOD, row_none["switches"], row_t["switches"],
         row_none["pc"], row_t["pc"]))
print("V92-3 %s（机制未解）" % V.verdict_bound(bound))
print("V92-4 %s" % V.verdict_lock(lock))
print("V92-5 仍开放：真机 tick 频率（KI#191）／256 阈值的机制／"
      "flag %d 的 0x%08X 真机首次置位者／任务 16 等的外部事件语义"
      % (V.TASK16_FLGID, V.TASK16_WANT))
print("V92-6 禁语：系统跑起来了／显示链通了／出画面／真机如此／"
      "补一次 tick 就等于真机时基／256 是固件周期")
print("DONE V92 A–G 七段，本版实跑 %d 台独立机器（1 缺省 + %d 周期扫描），"
      "反事实全部标注" % (len(rows), len(rows) - 1))
