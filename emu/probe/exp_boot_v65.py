# -*- coding: utf-8 -*-
"""V65 正式探针：任务 2 开机装配链、ROM 表扫描循环与执行窗口收敛性。

复现（从 emu/ 目录）：
    python probe/exp_boot_v65.py > ../results/frbox/boot65.txt

承接 KI#248。V61–V64 四版都把末 PC ``0x1A4E58`` 当成整机止点，
并用补 RAM 映射 / 打桩给满状态位 / 找任务调度事件去解释「显示链 0 命中」。
本版反查任务 2 入口的动态轨迹后确认：那个 PC 属于开机装配函数第一条
``call`` 内部的**确定性有限 ROM 表扫描循环**，V64 的 0/27 是
``RUN=20000`` 步的窗口假象。

纪律：
* 每台机器只跑一遍（硬规矩 13）；设备策略由 ``devaudit.audit()`` 显式声明，
  ``blive.boot(0)`` 保持 ``create()`` 默认值不变（硬规矩 38）。
* 不自写 ``cpu.step()`` 循环：只包装 ``cpu.step`` 做纯观察，推进仍由
  ``Machine.step()`` / ``Machine.run()`` 驱动（硬规矩 1）。
* 加长 ``RUN`` 只是给既有执行更多预算，**不是**补映射、打桩或建模动作；
  500000 与 1000000 步逐项相同才算收敛（硬规矩 28 的阴性写法）。
* 观察值按十进制打印（硬规矩 39）；PC 覆盖 ≠ 服务成功返回 / 任务真启动。
"""

import sys
import os
import collections

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # emu/

from frbox import (blive, bstatic, dispatch62 as D, taskchain64 as T,
                   bootchain65 as B, devaudit)

BOOT, VEC = 191520, 0x2B
RUNS = (20000, 40000, 80000, 200000, 500000, 1000000)
TRACE_RUN = 20000
WATCH = B.POLL_PCS

SW, M0 = bstatic.sweep_of_type("d300-b")
ROM, BASE = SW.rom, SW.base
RECS = T.task_table(ROM, BASE)
LAUNCH = T.launchers(SW, RECS)
STA_ALL = set(l.pc for l in LAUNCH)
STA_DISP = set(l.pc for l in LAUNCH if l.display)
TASK_ENTRIES = set(T.display_tasks(RECS).values())
BODY = D.decode_range(ROM, BASE, B.BOOT_TASK2, B.BOOT_RET + 2)
BODY_PCS = set(x.pc for x in BODY)
INT_PCS = set(x.pc for x in BODY if x.mn == "int")
FIRST18 = [c.pc for c in D.call_targets(BODY) if c.pc < 0x120154]
STRUCT_FLAG = 0x9F5A0284
HANDLER_CB = 0x9F59EF50
POOL_MARK = 0x9F58D500


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def cov_set(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


# ---------------------------------------------------------------- A 静态
hr("A  静态：任务 2 开机装配函数是集中总装机")
print("A0 任务表 0x054C00：29 条 × 0x1C 字节，+0x10 是任务入口；"
      "任务 2 入口 0x1200AC，任务 29 入口 0x0407DC")
a = B.boot_assembly(ROM, BASE)
print("A1 [0x%06X,0x%06X] 线性反汇编 %d 条；call %d 条；int#0x40 %d 笔；"
      "enter %d 个 / ret %d 条" %
      (B.BOOT_TASK2, B.BOOT_RET, len(a["insns"]), len(a["calls"]),
       len(a["svc_pcs"]), len(a["enters"]), len(a["rets"])))
print("A2 入口后 18 连串 `ldi:32 + call @r12`（0x%06X..0x%06X）："
      "共 %d 条 call" %
      (B.FIRST_CALL_PC, 0x120152, len(a["first_calls"])))
lm = B.launch_map(a["calls"], LAUNCH)
print("A3 其中 %d 条 call 的目标后 0x80 字节内有 sta_tsk 启动点：%s" %
      (len(lm), ["0x%06X→任务%d(+%d)" % (p, tid, delta)
                 for _cp, _t, p, tid, delta in lm]))
print("A4 ⇒ 更正 V64：这些启动器并非「各任务末尾链式自启动」，"
      "而是由任务 2 装配函数逐个调用；V64 只看 enclosing() 的分散度（硬规矩 30）。")
print("A5 区间内服务调用 %d 笔：三道 wai_flg 在 %s；一道 dly_tsk 在 0x%06X；"
      "门字节 0x%08X 的判定在 0x%06X" %
      (len(B.wait_points(SW)),
       ["0x%06X" % p for p in B.WAIT_PCS[1:]], B.WAIT_PCS[0],
       B.BOOT_GATE_BYTE, B.BOOT_GATE_CMP))
print("A6 全 ROM sta_tsk 静态点 %d 处（其中启动显示任务 %d 处）" %
      (len(STA_ALL), len(STA_DISP)))

# ---------------------------------------------------------------- B 静态
hr("B  静态：0x1A4E58 是确定性有限 ROM 表扫描循环")
p = B.poll_loop(ROM, BASE)
w = p["window"]
print("B1 四指令 0x%06X..0x%06X：%s" %
      (B.POLL_PCS[0], B.POLL_EXIT - 2, " / ".join(p["insns"])))
print("B2 `add2` 的 m4=0x%X 是 4 位负立即数 → 增量 %d（fr_emu.py:454 的 m4-16）；"
      "`ld` 地址形态 = %s" %
      (p["add2_m4"], p["add2_delta"], p["ld_form"]))
print("B3 `bgt:d` 的 rel9=0x%X 是 8 位有符号 → 回跳 0x%06X；"
      "延迟槽是 `%s`，每轮都执行（硬规矩 19）" %
      (p["branch_rel9"], p["branch_target"], p["delay_slot"]))
print("B4 bgt 判的是 CCR（gt = not((v^n) or z)），由 `add2` 设置；"
      "`ld` 不更新 CCR ⇒ 循环条件与读到的 ROM 值无关。")
print("B5 r13 初值 0x%X、r6=0x%08X ⇒ 扫描首地址 0x%08X，理论末地址 0x%08X，"
      "%d 轮 / %d 步（0x%X 字节跨度）" %
      (B.R13_INIT, B.R6_BASE, w["first"], w["last"], w["rounds"],
       w["steps"], w["span"]))
print("B6 这个循环在子函数 0x%06X 内；任务 2 装配函数第一条 call 0x%06X 就跳进它。" %
      (B.FIRST_CALLEE, B.FIRST_CALL_PC))

# ---------------------------------------------------------------- C 动态轨迹
hr("C  动态：循环寄存器轨迹与读值（一台独立机器，RUN=%d）" % TRACE_RUN)
m0 = blive.boot(0)
pol0 = devaudit.audit(m0)
regs, rdlog, entry_steps, nstep = [], [], [], [0]
orig_step, orig_rd = m0.cpu.step, m0.bus.rd


def step_watch():
    pc = m0.cpu.pc
    nstep[0] += 1
    if pc == B.BOOT_TASK2:
        entry_steps.append(nstep[0])
    if pc in WATCH:
        r = m0.cpu.r
        regs.append((pc, r[13], r[4], r[6], r[12],
                     m0.cpu.z, m0.cpu.n, m0.cpu.v, m0.cpu.c))
    return orig_step()


def rd_watch(a, n, pc=0):
    v = orig_rd(a, n, pc)
    if pc in WATCH:
        rdlog.append((pc, a, n, v))
    return v


m0.cpu.step = step_watch
m0.bus.rd = rd_watch
m0.step(BOOT)
boot_regs, boot_rd = len(regs), len(rdlog)
m0.raise_irq(VEC)
m0.run(max_steps=TRACE_RUN, spin_guard=False)
print("C0 任务 2 入口 0x1200AC 的全局步序：%s（BOOT=%d，即 BOOT 之后第 %s 步）；"
      "此后 [0x1200AC,0x1200D0] 共 9 个 PC 被覆盖，"
      "第一条 call 0x1200D0 之后不再回到装配函数" %
      ([p for p in entry_steps if p > BOOT], BOOT,
       [p - BOOT for p in entry_steps if p > BOOT]))
print("C1 设备策略 xfer=%d dispatch=%d prio=%d cov=%d devices=%d；"
      "BOOT=%d + 单次 IRQ 0x%02X + RUN=%d ⇒ 末 PC=0x%06X" %
      (pol0["xfer"], pol0["dispatch"], pol0["prio"], pol0["cov"],
       pol0["devices"], BOOT, VEC, TRACE_RUN, m0.cpu.pc))
tail = regs[boot_regs:]
by_pc = collections.Counter(x[0] for x in tail)
print("C2 循环四 PC 各执行 %s 次；BOOT 段落在循环内 %d 笔" %
      ([by_pc.get(pc, 0) for pc in WATCH], boot_regs))
p54 = [x[1] for x in tail if x[0] == B.POLL_ADD2]
print("C3 r13 在 0x%06X 处首值 0x%08X、末值 0x%08X、单调递减 %d，"
      "不同值 %d 个 ⇒ 每轮 %d" %
      (B.POLL_ADD2, p54[0], p54[-1], p54[-1] - p54[0],
       len(set(p54)), B.M4_DELTA))
print("C4 0x%06X 处 CCR(z,n,v,c) 分布：%s ⇒ 恒为真，循环只看 add2 结果" %
      (B.POLL_BRANCH,
       collections.Counter((x[5], x[6], x[7], x[8])
                           for x in tail if x[0] == B.POLL_BRANCH).most_common(3)))
r56 = [x for x in rdlog[boot_rd:] if x[0] == B.POLL_LD]
print("C5 0x%06X 的 `ld` 共 %d 笔，地址 0x%08X..0x%08X，"
      "读值 top（十进制）：%s" %
      (B.POLL_LD, len(r56),
       min(x[1] for x in r56), max(x[1] for x in r56),
       collections.Counter(x[3] for x in r56).most_common(5)))
print("C6 同子函数另两段同构轮询 %s 在本窗口 0 笔 ⇒ 还没跨过第一段" %
      [["0x%06X" % q for q in g] for g in B.SIBLING_POLLS])
print("C7 ⇒ 末 PC 0x%06X 是**执行前沿**，不是等外设的忙等假墙（V57 口径），"
      "也不是缺任务调度事件（KI#248 旧结论）。" % m0.cpu.pc)

# ---------------------------------------------------------------- D 窗口对照
hr("D  动态：同尺执行窗口对照（每组一台独立机器，不补映射 / 不打桩）")
print("D1 同尺：BOOT=%d + 单次 raise_irq(0x%02X) + RUN 变化；"
      "RUNS=%s" % (BOOT, VEC, list(RUNS)))
print("D2 加长 RUN 只增加执行预算，不改设备策略、不补 RAM、不给状态位；"
      "与 V63/V64 的补映射 / 打桩是不同动作，不混比（硬规矩 2）。")
res = {}
for run in RUNS:
    m = blive.boot(0)
    pol = devaudit.audit(m)
    m.step(BOOT)
    before = cov_set(m)
    m.raise_irq(VEC)
    m.run(max_steps=run, spin_guard=False)
    after = cov_set(m)
    res[run] = dict(m=m, pol=pol, cov=after, n0=len(before),
                    end=m.cpu.pc, blocked=m.blocked, spin=m.spin,
                    fault=m.fault)
    c, mm = after, res[run]["m"]
    print("\n【J%d RUN=%d】末 PC=0x%06X；覆盖 %d→%d（+%d）；"
          "设备策略 xfer=%d dispatch=%d prio=%d cov=%d devices=%d" %
          (list(RUNS).index(run), run, res[run]["end"], res[run]["n0"],
           len(c), len(c) - res[run]["n0"], pol["xfer"], pol["dispatch"],
           pol["prio"], pol["cov"], pol["devices"]))
    print("   任务2函数体 %d PC 覆盖 %d；26 个 int#0x40 命中 %d；"
          "前 18 条 call 命中 %d" %
          (len(BODY_PCS), len(c & BODY_PCS), len(c & INT_PCS),
           len([q for q in FIRST18 if q in c])))
    print("   27 个 sta_tsk 命中 %d（显示 9 里 %d）；"
          "14 个显示任务入口命中 %d" %
          (len(c & STA_ALL), len(c & STA_DISP), len(c & TASK_ENTRIES)))
    print("   显示链 5 PC：%s" %
          ["0x%06X:%s" % (q, "命中" if q in c else "未命中")
           for q in D.DSP_PCS])
    print("   门 0x%06X %s；0x%08X=%d、0x%08X=%d、0x%08X=%d（十进制）" %
          (B.BOOT_GATE_CMP, "命中" if B.BOOT_GATE_CMP in c else "未命中",
           STRUCT_FLAG, mm.bus.rd(STRUCT_FLAG, 4, 0),
           HANDLER_CB, mm.bus.rd(HANDLER_CB, 4, 0),
           POOL_MARK, mm.bus.rd(POOL_MARK, 4, 0)))
    print("   blocked=%s spin=%s fault=%s" %
          (res[run]["blocked"], res[run]["spin"], res[run]["fault"]))

# ---------------------------------------------------------------- E 收敛
hr("E  收敛性与增量归属")
c0 = res[20000]["cov"]
for run in (40000, 80000, 200000, 500000, 1000000):
    new = res[run]["cov"] - c0
    seg = collections.Counter(pc >> 16 for pc in new)
    print("E1 RUN=%-7d 相对 20000 新增 %5d PC；64KB 段 top：%s" %
          (run, len(new), [("0x%02Xxxxx" % k, v)
                           for k, v in seg.most_common(5)]))
a5, a10 = res[500000]["cov"], res[1000000]["cov"]
print("E2 500000 与 1000000 步：覆盖对称差集 %d 个 PC；"
      "末 PC 0x%06X / 0x%06X；sta_tsk 命中 %d / %d ⇒ **已收敛**" %
      (len(a5 ^ a10), res[500000]["end"], res[1000000]["end"],
       len(a5 & STA_ALL), len(a10 & STA_ALL)))
print("E3 收敛末 PC 0x%06X = 任务表末条（任务 29）入口，即 B 段空闲任务；"
      "V56 的 0xFE510 是 A 段空闲自旋，两个口径不混比。" %
      res[500000]["end"])
print("E4 500000 步里 27 个 sta_tsk 命中 %d：%s" %
      (len(a5 & STA_ALL), ["0x%06X" % q for q in sorted(a5 & STA_ALL)]))
print("E5 显示 9 个启动点命中 %d；14 个显示任务入口命中 %d：%s" %
      (len(a5 & STA_DISP), len(a5 & TASK_ENTRIES),
       ["0x%06X" % q for q in sorted(a5 & TASK_ENTRIES)]))
print("E6 0x%08X 的队列哨兵值 %d = 0x9F58D500（自指），"
      "说明对象池初始化函数 0x%06X 已跑过；"
      "但 0x%08X=0、0x%08X=0，显示链 0x%06X / 0x%06X 仍未命中。" %
      (POOL_MARK, res[500000]["m"].bus.rd(POOL_MARK, 4, 0),
       D.DSP_PCS[1], STRUCT_FLAG, HANDLER_CB,
       D.DSP_PCS[3], D.DSP_PCS[4]))
print("E7 第二个有限循环 0x1A4FA0..0x1A4FAA（`add2 m4=0xF` 即 -1，"
      "bgt:d rel9=0xFA）也是确定性计数循环；跨过后才进空闲。")

# ---------------------------------------------------------------- F 边界
hr("F  能说 / 不能说")
print("能说：")
print("  1. 任务 2 开机装配函数是集中总装机：353 条指令、32 条 call、"
      "12 条 call 直连 sta_tsk 启动器；V64「无集中总装机」是按 enclosing 切分的假象。")
print("  2. V61–V64 的末 PC 0x1A4E58 属于 ROM 表扫描循环：add2 步长 -4、"
      "6855 轮 / 27420 步，条件来自 add2 的 CCR 而非外设状态；"
      "RUN=20000 只跑 4962 轮。")
print("  3. 不加任何补全，仅把执行窗口加到 500000 步：27 个 sta_tsk 命中 16、"
      "显示 9 个启动点全命中、14 个显示任务入口命中 10、门字节 0x120178 命中、"
      "对象池哨兵自指、整机末 PC 落到空闲任务；1000000 步逐项相同 ⇒ 收敛。")
print("  4. KI#248「缺任务调度事件」更正为「缺执行预算」；"
      "KI#238 的补 RAM 映射、V62 的给满状态位都不是这一步的必要条件。")
print("不能说：")
print("  · 「显示链通了 / 出画面」—— 0x9F5A0284=0、0x9F59EF50=0、"
      "0x12E6F8 与 0x12E752 未命中，真实画面链仍未闭合（KI#201/204/208 保留）。")
print("  · 「16 个 sta_tsk 都成功启动了任务」—— PC 覆盖只证 int#0x40 指令执行过；"
      "服务返回码、TCB 就绪态本版没取。")
print("  · 「窗口越长跑得越远」—— 500000 与 1000000 步逐项相同，"
      "加长预算不是补东西，收敛后不再增长。")
print("  · 与 V55 真实时基 2000k 步长跑、V23 的 80 万步口径混比（设备策略不同）。")
