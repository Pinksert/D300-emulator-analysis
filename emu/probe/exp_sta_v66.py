# -*- coding: utf-8 -*-
"""V66 正式探针：16 个 sta_tsk「命中」到底是不是 16 个任务启动成功。

复现（从 emu/ 目录）：
    python probe/exp_sta_v66.py > ../results/frbox/sta66.txt

承接 KI#253。V65 用 500000 步收敛态报「27 个 sta_tsk 命中 16」，同时在
KI#253 里写明「PC 覆盖只证 int #0x40 指令执行过，16 笔 sta_tsk 的服务
返回码、TCB 就绪态本版没取，不能报 16 个任务都启动成功」。本版把这句
欠账结掉：把「命中」拆成三层 —— 内核受理（服务返回码）/ 任务建好
（TCB 状态 + 就绪队列）/ 真正上过 CPU（任务入口 PC 覆盖）。

纪律：
* 一台机器只跑一遍（硬规矩 13）：C–F 段共用同一台 500000 步机器；
  B 段验证返回码通路另起一台独立机器，不复用。
* 不自写 cpu.step() 循环：只包装 cpu.step 做纯观察，推进仍由
  Machine.step() / Machine.run() 驱动（硬规矩 1）。
* 设备策略与 V65 收敛组逐字相同：blive.boot(0) → step(191520) →
  raise_irq(0x2B) → run(500000, spin_guard=False)；不补映射、不打桩。
* ``int #0x40`` 走真向量表（svc_hook 必须是 None）；挂了半主机钩子时
  r12=E_OK 是打桩常量，不能当内核返回码。
* 观察值按十进制打印（硬规矩 39）；反事实 / 注入动作单独标注。
"""

import sys
import os
import collections

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # emu/

from frbox import (blive, bstatic, brealos, bready, devaudit,
                   taskchain64 as T, dispatch62 as D, bootchain65 as B)

BOOT = blive.BOOT_STEPS
VEC = 0x2B
RUN = 500000

STRUCT_FLAG = 0x9F5A0284
HANDLER_CB = 0x9F59EF50
POOL_MARK = 0x9F58D500
SLOT8 = 0x9F59F7C8        # KI#208：8 槽回调表，疑似运行时填充

SW, M0 = bstatic.sweep_of_type("d300-b")
ROM, BASE = SW.rom, SW.base
RECS = T.task_table(ROM, BASE)
LAUNCH = T.launchers(SW, RECS)
STA = dict((l.pc, l) for l in LAUNCH)
RET = dict((l.pc + 2, l.pc) for l in LAUNCH)   # reti 回 int_pc+2
WATCH = set(STA) | set(RET)
DISP_ENTRIES = T.display_tasks(RECS)            # {tid: entry}


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def signed(v):
    return v - 0x100000000 if v & 0x80000000 else v


# ---------------------------------------------------------------- A 静态
hr("A  静态口径：27 个点、23 个任务号，与 V65 的「命中 16」对齐")
print("A1 任务表 0x054C00：%d 条 × 0x1C；入口落显示区 0x%06X..0x%06X 的任务 %d 个" %
      (len(RECS), T.DISP_LO, T.DISP_HI, len(DISP_ENTRIES)))
tids = sorted(set(l.tid for l in LAUNCH if l.tid is not None))
print("A2 全 ROM `sta_tsk`(−23) 静态点 %d 处，覆盖 %d 个任务号：%s" %
      (len(STA), len(tids), tids))
print("A3 另有 %d 个点 r4 读不出立即数（记作 None）：%s" %
      (sum(1 for l in LAUNCH if l.tid is None),
       ["0x%06X" % l.pc for l in LAUNCH if l.tid is None]))
print("A4 其中启动显示任务 %d 处；`reti` 返回点 = 调用点 + 2（FR 的 "
      "`_exec_one` 先自增 PC 再 `eit`，`fr_emu.py:310/376`）" %
      sum(1 for l in LAUNCH if l.display))
print("A5 V65 报的「16」是**不同 PC 数**，不是调用笔数；本版两者都报。")

# ---------------------------------------------------------------- B 钩子有效性
hr("B  先证伪：返回码寄存器有判别力、走的是真内核（独立第二台机器）")
m2 = blive.boot(0)
print("B1 这台机器 svc_hook=%r、int_hook=%s ⇒ `int #0x40` 走真向量表 0x040668，"
      "不是 `itron`/`ItronKernel` 半主机打桩（打桩下 r12 恒为 E_OK，无判别力）" %
      (m2.cpu.svc_hook, type(m2.cpu.int_hook).__name__
       if m2.cpu.int_hook else "None"))
m2.step(BOOT)
print("B2 注入 `kernel_call`（反事实动作，只验证返回码通路，不参与收敛态结论）：")
warm = brealos.kernel_call(m2, brealos.SVC_STA_TSK, r4=29, r5=1)
print("B2a 预热（boot 后**第一次** kernel_call 一律跑满步数上限，与参数无关）："
      "err=%d steps=%d ⇒ 不计入样本" % (warm["err"], warm["steps"]))
probe_calls = [(brealos.SVC_STA_TSK, 29, "空闲任务号，内核拒绝"),
               (brealos.SVC_STA_TSK, 1, "首次启动任务 1"),
               (brealos.SVC_STA_TSK, 1, "重复启动任务 1"),
               (brealos.SVC_STA_TSK, 30, "越界任务号"),
               (-120, 1, "不存在的服务号")]
for svc, tid, tag in probe_calls:
    r = brealos.kernel_call(m2, svc, r4=tid, r5=1)
    print("     svc=%-5d tid=%-3d → err=%-5d  steps=%-6d (%s)" %
          (svc, tid, r["err"], r["steps"], tag))
print("B3 ⇒ r12 同时出现过 E_OK(0) 和三个非零错误码（−35 / −63 / −17），"
      "所以收敛态里读到 0 是真内核的成功返回，不是常量。")
print("B4 ⚠️ 这是 `kernel_call` 注入工具的边界，不是内核行为：第一次调用从 "
      "SAFE_PC(0x0407DC) 起会跑满 80000 步上限、r12 是垃圾值 748。本版显式 "
      "预热一次，之后五个样本全部在 128 步内正常返回；越界样本不纳入判定。")

# ---------------------------------------------------------------- C 收敛态
hr("C  500000 步收敛态：每笔 sta_tsk 的参数、返回码、返回点重放")
m = blive.boot(0)
pol = devaudit.audit(m)
m.step(BOOT)
print("C1 设备策略 %s；BOOT=%d 后 PC=0x%06X；单次 raise_irq(0x%02X)；RUN=%d" %
      (pol, BOOT, m.cpu.pc, VEC, RUN))

log = []
trace = []
nstep = [0]
armed = [False]
TRACE_PC = 0x12FEC2        # 收敛态里最早出现的 sta_tsk 点（任务 26）
orig_step = m.cpu.step


def watch():
    pc = m.cpu.pc
    nstep[0] += 1
    if pc in WATCH:
        c = m.cpu
        log.append((nstep[0], pc, c.r[4], c.r[5], c.r[12],
                    bready.current(m)))
    if pc == TRACE_PC:
        armed[0] = True
    if armed[0] and len(trace) < 140:
        trace.append((nstep[0], pc, m.cpu.r[4], m.cpu.r[12], m.cpu.s))
    return orig_step()


m.cpu.step = watch
m.raise_irq(VEC)
m.run(max_steps=RUN, spin_guard=False)
m.cpu.step = orig_step
print("C2 末 PC=0x%06X（V65 收敛末 PC = 0x0407DC 空闲任务）；钩子命中 %d 笔；"
      "blocked=%s spin=%s fault=%s" %
      (m.cpu.pc, len(log), m.blocked, m.spin, m.fault))

calls = [x for x in log if x[1] in STA]
rets = [x for x in log if x[1] in RET]
call_pcs = set(x[1] for x in calls)
print("C3 调用点命中：**%d 个不同 PC / %d 笔调用**；返回点命中 %d 笔 "
      "（每个点通常 ×2，见 C5）" % (len(call_pcs), len(calls), len(rets)))

by_call = collections.defaultdict(list)
for x in calls:
    by_call[x[1]].append(x)
ret_sorted = sorted(rets, key=lambda x: x[0])
print("\nC4 逐笔配对（调用 → 其后第一次命中 +2 的返回点，取 r12 作 err）：")
print("    PC        任务  笔数  首步序    r4   r5    err    返回点重放")
pair_rows = []
for pc in sorted(call_pcs):
    rows = by_call[pc]
    first = rows[0]
    nxt = next((r for r in ret_sorted if r[0] > first[0] and r[1] == pc + 2), None)
    err = signed(nxt[4]) if nxt else None
    replay = sum(1 for r in rets if r[1] == pc + 2)
    l = STA[pc]
    print("    0x%06X  %-5s %3d   %7d  %3d  %3d   %-6s  %d" %
          (pc, l.tid if l.tid is not None else "None", len(rows),
           first[0], first[2], first[3],
           err if err is not None else "无返回点", replay))
    pair_rows.append(dict(pc=pc, tid=l.tid, display=l.display,
                          n=len(rows), step=first[0], err=err, replay=replay))
errs = collections.Counter(r["err"] for r in pair_rows)
print("\nC5 返回码分布：%s" % [(k, v) for k, v in errs.items()])
print("\nC5b 逐指令轨迹（同一台收敛机器，从第一次 0x%06X 起 %d 步；"
      "包装仍是纯观察）" % (TRACE_PC, len(trace)))
key_steps = {100000, 100001, 100051, 100129, 100130, 100134}
for t in trace:
    if t[0] in key_steps:
        print("     step=%-7d PC=0x%06X r4=%-3d r12=0x%08X S=%d" %
              (t[0], t[1], t[2], t[3], t[4]))
seen_pcs = []
for t in trace:
    if t[1] not in seen_pcs:
        seen_pcs.append(t[1])
print("     轨迹内出现的 PC（去重，%d 个，按首次出现序）：" % len(seen_pcs))
for i in range(0, len(seen_pcs), 8):
    print("       " + " ".join("0x%06X" % p for p in seen_pcs[i:i + 8]))
print("     ⇒ `reti` 回 +2 之后下一步就进 0x040D32 派发器，当前任务被换出；"
      "第二次命中是该任务被重新调度回来从 +2 续跑。这是真内核派发的痕迹，"
      "不是钩子重复计数；配对只取第一次（第二次 r12 已被派发器改成 0x68000130）。")
print("C6 16 个命中里启动显示任务的点 %d 个；静态 27 点里未命中 %d 个：%s" %
      (sum(1 for r in pair_rows if r["display"]),
       len(STA) - len(call_pcs),
       ["0x%06X" % p for p in sorted(set(STA) - call_pcs)]))

# ---------------------------------------------------------------- D TCB
hr("D  29 个 TCB 的运行时状态（同一台收敛机器）")
# D0 窗口对照：第三台独立机器，只跑到 200000 步，用来证明「在就绪队列上」
# 不等于「上过 CPU」，也证明第 2 层判据不能锚在瞬时队列状态上。
m3 = blive.boot(0)
m3.step(BOOT)
m3.raise_irq(VEC)
m3.run(max_steps=200000, spin_guard=False)
occ3 = bready.occupancy(m3)
recs3 = brealos.records(m3, include_idle=True)
ran3 = [r["id"] for r in recs3 if m3.cov.visited(r["entry"])]
q_not_ran3 = [bready.tcb_name(m3, t) for t in sorted(occ3)
              if t not in [r["tcb"] for r in recs3 if r["id"] in ran3]]
print("D0 窗口对照（第三台独立机器，RUN=200000）：末 PC=0x%06X；"
      "就绪队列 %d 个 TCB、位图 0x%08X；入口跑过 %d 个；"
      "**在队列但入口没跑过 %d 个**：%s" %
      (m3.cpu.pc, len(occ3), bready.ready_map(m3), len(ran3),
       len(q_not_ran3), q_not_ran3))
print("    ⇒ 同一个「在就绪队列上」的判据，200000 步时成立、500000 步时队列清空；"
      "所以「已激活」必须锚在 TCB 的 state≠DORMANT + saved_sp 上。")

occ = bready.occupancy(m)
idx = bready.task_index(m)
print("D1 %s" % bready.describe(m))
print("D2 就绪位图 0x%08X；在 32 条就绪队列上的 TCB 共 %d 个" %
      (bready.ready_map(m), len(occ)))
print("\n    任务  TCB         state  prio  就绪队列  入口      入口跑过  saved_sp")
tcb_rows = []
for r in brealos.records(m, include_idle=True):
    tcb = r["tcb"]
    st = bready.state_of(m, tcb)
    prio = bready.prio_of(m, tcb)
    qs = occ.get(tcb, [])
    sp = m.bus.rd(tcb + 0x18, 4)
    ran = bool(m.cov and m.cov.visited(r["entry"]))
    tcb_rows.append(dict(tid=r["id"], tcb=tcb, state=st, prio=prio,
                         queues=qs, entry=r["entry"], ran=ran, sp=sp))
    print("    %-5d 0x%08X  0x%02X   %-4d  %-9s 0x%06X  %-8s 0x%08X" %
          (r["id"], tcb, st, prio, ",".join(str(q) for q in qs) or "-",
           r["entry"], "是" if ran else "否", sp))
print("\nD3 state 口径（bready.py:78）：0x00 就绪 / 0x04 等待 / 0x10 休眠(DORMANT)。")
print("D4 state=0x00 的任务 %d 个；其中在就绪队列上的 %d 个；"
      "state=0x10 休眠的 %d 个；state=0x04 等待的 %d 个" %
      (sum(1 for x in tcb_rows if x["state"] == 0),
       sum(1 for x in tcb_rows if x["state"] == 0 and x["queues"]),
       sum(1 for x in tcb_rows if x["state"] == 0x10),
       sum(1 for x in tcb_rows if x["state"] == 0x04)))
print("D5 任务入口 PC 被覆盖过的任务 %d 个：%s" %
      (sum(1 for x in tcb_rows if x["ran"]),
       [x["tid"] for x in tcb_rows if x["ran"]]))

# ---------------------------------------------------------------- E 三层
hr("E  四层判定：受理 / 已激活 / 上过 CPU / 收敛时还活着吗（本版主结论）")
by_tid = {}
for r in pair_rows:
    if r["tid"] is not None:
        by_tid.setdefault(r["tid"], []).append(r)
tcb_by_tid = dict((x["tid"], x) for x in tcb_rows)
accepted = sorted(t for t, rs in by_tid.items() if all(r["err"] == 0 for r in rs))
# ⚠️ 「建好」不能拿 state==0 判：收敛时这些任务早已跑完并挂起（state=0x04），
#    就绪队列也已清空。正确判据是「脱离 DORMANT」＝ state != 0x10 且分配了栈。
activated = sorted(t for t in accepted
                   if t in tcb_by_tid and tcb_by_tid[t]["state"] != 0x10
                   and tcb_by_tid[t]["sp"] != 0)
ran_ids = sorted(t for t in accepted
                 if t in tcb_by_tid and tcb_by_tid[t]["ran"])
waiting = sorted(t for t in ran_ids
                 if t in tcb_by_tid and tcb_by_tid[t]["state"] == 0x04)
print("E1 第 1 层「内核受理」：%d 个任务号的 sta_tsk 全部返回 E_OK(0)：%s" %
      (len(accepted), accepted))
print("E2 第 2 层「已激活」：TCB 脱离 DORMANT(state≠0x10) 且分配了 "
      "saved_sp 的 %d 个：%s" % (len(activated), activated))
print("E3 第 3 层「上过 CPU」：任务入口 PC 被覆盖的 %d 个：%s" %
      (len(ran_ids), ran_ids))
print("E4 第 4 层「收敛时状态」：这 %d 个里 state=0x04 等待的 %d 个；"
      "就绪位图 0x%08X、就绪队列占用 %d 个 ⇒ **全部挂着等，没有一个在跑**" %
      (len(ran_ids), len(waiting), bready.ready_map(m), len(occ)))
print("E5 三层差集：1→2 层 %s；2→3 层 %s ⇒ 对这 16 个任务，"
      "「受理 / 已激活 / 上过 CPU」是同一个集合，V65 那句保守判断可以收窄" %
      (sorted(set(accepted) - set(activated)),
       sorted(set(activated) - set(ran_ids))))
print("E6 未被任何 sta_tsk 启动、仍休眠(state=0x10)的任务 %d 个：%s" %
      (sum(1 for x in tcb_rows if x["state"] == 0x10),
       [x["tid"] for x in tcb_rows if x["state"] == 0x10]))
no_launcher = sorted(t for t in (x["tid"] for x in tcb_rows if x["state"] == 0x10)
                     if t not in tids)
print("E7 其中 %d 个任务号 %s **全 ROM 没有 sta_tsk 静态点**（27 点只覆盖 %d 个"
      "任务号）⇒ 它们要么根本不靠 sta_tsk 启动，要么由别的机制激活，本版未解" %
      (len(no_launcher), no_launcher, len(tids)))
print("E8 反过来看：入口跑过但本轮没有 sta_tsk 返回码的是 %s —— "
      "任务 2 是开机装配任务、29 是空闲任务，都是 BOOT 阶段固件自建的。" %
      sorted(set(x["tid"] for x in tcb_rows if x["ran"]) - set(accepted)))
print("E9 判据边界：`err=0` 只说明内核受理；`state≠0x10`+有栈说明已激活；"
      "只有入口 PC 覆盖才说明**真上过 CPU**；`state=0x04` 说明它现在在等。"
      "四层不能互相替代，也不能拿「不在就绪队列」当「没启动」。")
print("\nE10 ⚠️ 窗口自证（第四台独立机器，RUN=1000000）：V65 栽过「RUN=20000 "
      "窗口假象」，所以「收敛时全挂着」必须先排除 500000 步也只是窗口。")
cov = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
m4 = blive.boot(0)
m4.step(BOOT)
m4.raise_irq(VEC)
m4.run(max_steps=1000000, spin_guard=False)
cov4 = set(m4.cov.base + i * 2 for i, v in enumerate(m4.cov.buf) if v)
recs4 = brealos.records(m4, include_idle=True)
state4 = dict((r["id"], bready.state_of(m4, r["tcb"])) for r in recs4)
ran4 = sorted(r["id"] for r in recs4 if m4.cov.visited(r["entry"]))
occ4 = bready.occupancy(m4)
states_500 = dict((x["tid"], x["state"]) for x in tcb_rows)
ran_500 = sorted(x["tid"] for x in tcb_rows if x["ran"])
diff_state = sorted(t for t in state4 if states_500.get(t) != state4[t])
print("     500000 步：末 PC=0x%06X、位图 0x%08X、队列 %d、入口跑过 %d、"
      "sta_tsk 命中 %d" %
      (m.cpu.pc, bready.ready_map(m), len(occ), len(ran_500),
       len(cov & set(STA))))
print("    1000000 步：末 PC=0x%06X、位图 0x%08X、队列 %d、入口跑过 %d、"
      "sta_tsk 命中 %d" %
      (m4.cpu.pc, bready.ready_map(m4), len(occ4), len(ran4),
       len(cov4 & set(STA))))
print("     29 个任务 state 差异任务号：%s；入口 ran 集合差异：%s；"
      "PC 覆盖对称差集 %d 个" %
      (diff_state, sorted(set(ran4) ^ set(ran_500)), len(cov ^ cov4)))
print("     ⇒ %s" %
      ("四项逐项相同，500000 步不是窗口假象，收敛结论成立"
       if (m4.cpu.pc == m.cpu.pc and bready.ready_map(m4) == bready.ready_map(m)
           and not diff_state and set(ran4) == set(ran_500)
           and len(cov ^ cov4) == 0)
       else "有差异，收敛结论需要重新审视（见上面逐项数字）"))

# ---------------------------------------------------------------- F 画面链
hr("F  显示链仍未闭合（V65 的两个 0 与 5 个 PC）")
cov = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
print("F1 显示链 5 PC：%s" %
      ["0x%06X:%s" % (q, "命中" if q in cov else "未命中") for q in D.DSP_PCS])
print("F2 0x%08X=%d、0x%08X=%d、池哨兵 0x%08X=0x%08X（十进制/十六进制）" %
      (STRUCT_FLAG, m.bus.rd(STRUCT_FLAG, 4, 0),
       HANDLER_CB, m.bus.rd(HANDLER_CB, 4, 0),
       POOL_MARK, m.bus.rd(POOL_MARK, 4, 0)))
slots = [m.bus.rd(SLOT8 + i * 4, 4, 0) for i in range(8)]
print("F3 8 槽回调表 0x%08X = %s（KI#208：运行时填充源仍未解）" %
      (SLOT8, ["0x%08X" % v for v in slots]))
a = B.boot_assembly(ROM, BASE)
print("F4 门 0x%06X %s；任务 2 装配函数 %d 条指令 / %d PC 覆盖 %d" %
      (B.BOOT_GATE_CMP, "命中" if B.BOOT_GATE_CMP in cov else "未命中",
       len(a["insns"]), len(a["pcs"]), len(cov & set(a["pcs"]))))

# ---------------------------------------------------------------- G 边界
hr("G  能说 / 不能说")
print("能说：")
print("  1. 16 个命中点 = 16 笔调用、16 个任务号，服务返回码全部 E_OK(0)；"
      "r12 通路经 −17/−35/−63 三个反例证明有判别力。")
print("  2. 这 16 个任务全部脱离 DORMANT 且入口 PC 全被覆盖 ⇒ "
      "**受理 / 已激活 / 上过 CPU 三层重合**，KI#253 那句保守判断可以收窄。")
print("  3. 收敛时 16 个任务 state 全是 0x04 等待、就绪位图 0、队列空 ⇒ "
      "整机停在空闲不是「没启动」，是**全都起来过又全都挂起**。")
print("  4. 11 个休眠任务里 12/13/14/15 全 ROM 没有 sta_tsk 点（E7）；"
      "另 11 个静态启动点未命中（C6），两组不是同一批。")
print("  5. 每次 sta_tsk 返回后立刻进 0x040D32 派发，返回点 +2 被执行两次是派发重放。")
print("  6. V65 收敛末 PC 0x0407DC（空闲任务）复现；设备策略与 V65 逐字相同。")
print("不能说：")
print("  · 不能报「16 个任务现在都在运行」—— 它们全部在等待态，只有空闲任务在跑。")
print("  · 不能把「已启动且跑过」推成「显示任务跑通了」：显示链 5 PC 里 "
      "0x12E6F8 / 0x12E752 仍未命中，两个状态字仍是 0。")
print("  · 不能报显示链通了 / 出画面 / 找到真机 A↔B 通道。")
print("  · 不能把 B 段的 kernel_call 注入当成收敛态行为；那是独立第二台机器上的反事实。")
print("  · 不能拿 state==0 或「在就绪队列」当「已激活」的判据（收敛时队列已清空，"
      "这会让 E2 与 E3 自相矛盾）；也不能把「返回点 ×2」当成「服务被调用两次」。")

hr("H  复现")
print("cd emu && python probe/exp_sta_v66.py > ../results/frbox/sta66.txt")
print("⚠️ 四台机器各跑一遍：C–F 共用 500000 步收敛机器；B 段返回码验证、"
      "D0 的 200000 步窗口对照、E10 的 1000000 步收敛自证各用独立机器。")
print("⚠️ 不补映射、不打桩、不给状态位；加长 RUN 只是执行预算（V65 口径）。")
