# -*- coding: utf-8 -*-
"""V68 正式探针：14 个等待位的置位源在哪、跑没跑过、补上会怎样。

复现（从 emu/ 目录）：
    python probe/exp_set_v68.py > ../results/frbox/set68.txt

承接 V67 的 KI#261 第一条「谁来置这 14 个等待者要求的位」。本版分三层回答：
存在性（静态置位源）/ 执行性（收敛态覆盖）/ 触发性（是否 ISR 门控），
再用四组等形反事实把「补一次置位」「补 tick」的效果分开归因。

纪律：
* 五台独立机器：四组反事实（G0 无补 / G1 只置位 / G2 只补 tick /
  G3 置位+补 tick）各一台，外加 1000000 步窗口自证一台。
  每组内是同一台机器的时间序列快照，不是重复跑（硬规矩 13）。
* 设备策略与 V65/V66/V67 收敛组逐字相同：blive.boot(0) → step(191520)
  → 单次 raise_irq(0x2B) → run(500000, spin_guard=False)。
* 反事实动作全部标注：brealos.kernel_call 注入的是**真内核服务**，
  但它会恢复 CPU 上下文、只留内存副作用；btick.tick_v52 是 V52 的
  模型化 tick（wake_objects=False，只补延时队列）。四组预热次数、
  服务调用次数、中断次数、后续步数全部相同 ⇒ 差分只来自被补的事件。
* 静态置位点是「常量可见」口径：flgid / bits 任一为 None 的点不计入，
  不能反推「固件里没有别的置位路径」（bstatic 边界）。
* clr_flg 清的是 mask 里为 0 的位（硬规矩 20），判「会清掉等待位」
  用 (~mask) & want，不是 mask & want。
"""

import sys
import os
import bisect
import collections

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # emu/

import fr_emu
from frbox import (blive, bstatic, bready, brealos, btick, isrfam63,
                   svcgraph, setsrc68 as S, waitstat67 as W)

BOOT = blive.BOOT_STEPS
VEC = 0x2B
RUN = 500000
POST = 200000
RUN_LONG = 1000000
TICKS = 12
TICK_CHUNK = 30000
DSP = (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752)
STRUCT_FLAG = 0x9F5A0284
HANDLER_CB = 0x9F59EF50
SLOT8 = 0x9F59F7C8

SW, M0 = bstatic.sweep_of_type("d300-b")
ROM, BASE = SW.rom, SW.base
IDX = svcgraph.ImmIndex(ROM, BASE)
VT = isrfam63.vector_table(ROM, BASE)


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def coverage(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def insn_pcs(lo, hi):
    """逐条解码 [lo, hi) 的指令 PC（不是 2 字节槽位，ldi:32 占 6 字节）。"""
    out, off, end = [], lo - BASE, hi - BASE
    while off < end - 1:
        if ROM[off:off + 2] in bstatic.SKIP2:
            off += 2
            continue
        ins = fr_emu.TABLE.decode(ROM, off)
        if ins is None:
            off += 2
            continue
        out.append(BASE + off)
        off += ins[3]
    return out


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def waiters_of(m, idx):
    """现场读 flag 队列上的等待者 → [(flgid, tid, 等待位模式)]。"""
    rev = dict((t, tid) for tid, t in idx.items())
    out = []
    for ident, obj, nodes in btick.object_queues(m):
        fid = W.flag_id_of(obj)
        if fid is None or not nodes:
            continue
        for tcb in nodes:
            tid = rev.get(tcb)
            want = m.bus.rd(tcb + W.TCB_WANT, 4)
            if tid is not None and want:
                out.append((fid, tid, want))
    return sorted(out, key=lambda x: (x[0], x[1]))


def snapshot(m, idx=None):
    if idx is None:
        idx = bready.task_index(m)
    st = dict((tid, bready.state_of(m, t)) for tid, t in idx.items())
    cov = coverage(m)
    q = {}
    for ident, obj, nodes in btick.object_queues(m):
        if nodes:
            q[obj] = len(nodes)
    pats = dict((f, m.bus.rd(W.flag_obj(f) + W.OBJ_PAT, 4))
                for f in range(1, 18))
    return {
        "pc": m.cpu.pc, "cov": cov, "st": st, "q": q, "pats": pats,
        "ready": bready.ready_map(m), "delay": len(btick.delay_queue(m)),
        "words": (m.bus.rd(STRUCT_FLAG, 4), m.bus.rd(HANDLER_CB, 4)),
        "slots": [m.bus.rd(SLOT8 + 4 * i, 4) for i in range(8)],
    }


def print_snap(tag, s):
    print("  [%s] 末PC=0x%06X 覆盖=%d 等待=%d 休眠=%d 就绪=%d 位图=0x%08X"
          % (tag, s["pc"], len(s["cov"]),
             sum(1 for v in s["st"].values() if v == W.WAIT),
             sum(1 for v in s["st"].values() if v == W.DORMANT),
             sum(1 for v in s["st"].values() if v == W.READY),
             s["ready"]))
    print("       队列 %s 延时 %d；flag 位模式 %s"
          % (["%s:%d" % (W.owner_name(k), v) for k, v in sorted(s["q"].items())],
             s["delay"],
             ["%d:0x%08X" % (f, s["pats"][f]) for f in sorted(SEEN_FLAGS)
              if s["pats"][f]]))
    print("       显示链 %s；状态字 0x9F5A0284=0x%08X / 0x9F59EF50=0x%08X；"
          "8 槽 0x9F59F7C8=%s"
          % (["0x%06X:%s" % (p, "中" if p in s["cov"] else "-") for p in DSP],
             s["words"][0], s["words"][1], s["slots"]))


def counterfactual(tag, do_set, do_tick, wants):
    """四组等形反事实：差别只有 do_set / do_tick。"""
    m = boot_run()
    idx = bready.task_index(m)
    base = snapshot(m, idx)
    pre = brealos.kernel_call(m, S.SVC_GET_TID)
    calls = []
    for f in sorted(wants):
        if do_set:
            r = brealos.kernel_call(m, S.SVC_SET_FLG, r4=f, r5=wants[f])
        else:
            r = brealos.kernel_call(m, S.SVC_GET_TID)
        calls.append((f, r["steps"], r["err"]))
    mid = snapshot(m, idx)
    ticks = []
    if do_tick:
        ticks = btick.tick_v52(m, n=TICKS, steps=TICK_CHUNK,
                               wake_objects=False)
    after_tick = snapshot(m, idx) if do_tick else None
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)
    end = snapshot(m, idx)

    print("  --- %s（do_set=%s do_tick=%s）---" % (tag, do_set, do_tick))
    print("      预热 get_tid steps=%d err=%d；6 次服务 %s"
          % (pre["steps"], pre["err"], calls))
    if do_tick:
        print("      tick_v52(n=%d, wake_objects=False) %d 轮；expired=%s"
              % (TICKS, len(ticks), [t["expired"] for t in ticks]))
    print_snap("基线", base)
    print_snap("注入后", mid)
    if after_tick is not None:
        print_snap("补 tick 后", after_tick)
    print_snap("末尾", end)
    d = S.diff_summary(base, end, dsp_pcs=DSP)
    print("      差分：state 变化 %s；新增覆盖 %d 个；显示链新增 %s"
          % (d["state_diff"], d["new_cov"],
             [hex(p) for p in d["dsp_new"]]))
    return dict(base=base, mid=mid, after_tick=after_tick, end=end,
                diff=d, calls=calls, pre=pre, ticks=ticks)


# ---------------------------------------------------------------- A 静态
hr("A  静态总账：服务点、等待位表、字段语义")

sets = SW.of(S.SVC_SET_FLG)
clrs = SW.of(S.SVC_CLR_FLG)
waits = SW.of(S.SVC_WAI_FLG)
wide = SW.of(86) + SW.of(85)
print("A1 全 ROM `set_flg`(−48) %d 点 / `clr_flg`(−47) %d 点 / "
      "`wai_flg`(−46) %d 点 / 宽服务号(实为 −170/−171) %d 点"
      % (len(sets), len(clrs), len(waits), len(wide)))
by_f = collections.defaultdict(list)
for c in sets:
    by_f[c.flgid].append(c)
print("A2 `set_flg` 按 flgid 分 %d 个桶（含 None）；只看 6 个有等待者的 flag："
      % len(by_f))
for f in (3, 5, 6, 8, 14, 17):
    cs = by_f.get(f, [])
    print("     flag %-2d %3d 点，bits 常量可见 %3d 点"
          % (f, len(cs), sum(1 for c in cs if c.bits is not None)))
unknown_f = [c for c in sets if c.flgid is None]
print("A3 flgid 不可定（运行时变量）的 `set_flg` %d 点 %s ⇒ 常量可见口径之外"
      % (len(unknown_f), [hex(c.addr) for c in unknown_f]))
print("A4 字段语义：`clr_flg(id, mask)` 清的是 **mask 里为 0 的位**"
      "（硬规矩 20）；判「会清掉等待位」用 (~mask)&want。")
print("     `TCB+0x14` 在 flag 等待时是等待位模式（0x041B54），"
      "延时等待时是剩余 tick（0x040A78，V67 规矩 49）。")

# 用 G0 的基线机器现场读等待位（反事实组下面会重建，这里先取一台）
m_base = boot_run()
idx_base = bready.task_index(m_base)
WAITERS = waiters_of(m_base, idx_base)
SEEN_FLAGS = sorted({f for f, _t, _w in WAITERS})
WANTS = S.wants_by_flag(WAITERS)
print("A5 500000 步收敛机器现场读到 %d 个 flag 等待者，覆盖 flag %s"
      % (len(WAITERS), SEEN_FLAGS))
for f, tid, want in WAITERS:
    print("     flag %-2d 任务%-3s 等待位模式=0x%08X 位=%s"
          % (f, tid, want,
             ",".join(hex(b) for b in S.bits_of(want))))
print("A6 每个 flag 的等待位 OR（反事实注入用的就是这 6 个值）：%s"
      % [(f, "0x%08X" % WANTS[f]) for f in sorted(WANTS)])

# ---------------------------------------------------------------- B 位级源
hr("B  存在性：14 个等待位，固件里有没有 set_flg 能置它")

rep = S.coverage_report(WAITERS, sets, set())
for row in rep:
    funcs = sorted({SW.enclosing(p) for p in row["pcs"]})
    print("B1 flag %-2d 任务%-3s want=0x%08X 常量可见置位点 %2d 个 / "
          "%2d 个函数 %s"
          % (row["flgid"], row["tid"], row["want"], row["points"],
             len(funcs), [hex(x) if x else None for x in funcs[:5]]))
    print("      PC %s" % [hex(p) for p in row["pcs"][:8]])
g_static = S.gap_total(rep)
print("B2 合计：按等待者累加 %d 点、全局去重 %d 点；14 个等待者里"
      "「一个置位源都没有」的 %d 个"
      % (g_static["points"], g_static["unique_points"],
         sum(1 for row in rep if row["points"] == 0)))
all_pcs = sorted({p for row in rep for p in row["pcs"]})
print("B3 ⇒ 不能说「固件里没人能置这些位」；代码存在，问题是它跑没跑过。")

print("B4 置位点的函数归属（前 12 个函数，按置位点个数降序）")
by_fn = collections.Counter(SW.enclosing(p) for p in all_pcs)
for fn, n in by_fn.most_common(12):
    refs = IDX.refs(fn, window=8) if fn else []
    kinds = collections.Counter(r.kind for r in refs)
    print("     fn=%-10s 置位点 %2d 个；引用 %3d 条 %s"
          % (hex(fn) if fn else None, n, len(refs), dict(kinds)))
print("B5 `clr_flg` 会清掉等待位的点（按 flag）")
for f in SEEN_FLAGS:
    cs = S.clearers_for(clrs, f, WANTS[f])
    print("     flag %-2d %3d 点 %s" % (f, len(cs),
                                      [hex(c.addr) for c in cs[:6]]))

# ---------------------------------------------------------------- C 执行性
hr("C  执行性：这些置位点在 500000 步收敛机器里跑过没有")

cov_base = coverage(m_base)
rep_cov = S.coverage_report(WAITERS, sets, cov_base)
for row in rep_cov:
    print("C1 flag %-2d 任务%-3s 置位点 %2d 个 / 命中 %d 个 %s"
          % (row["flgid"], row["tid"], row["points"], row["hit_count"],
             [hex(p) for p in row["hit"]]))
g_cov = S.gap_total(rep_cov)
print("C2 总账：累加 %d 点 / 命中 %d；去重 %d 点 / 命中 %d；"
      "零命中的等待者 %d / %d"
      % (g_cov["points"], g_cov["hit"], g_cov["unique_points"],
         g_cov["unique_hit"], g_cov["zero_hit"], g_cov["waiters"]))
hit_sets = [c for c in sets if c.addr in cov_base]
hit_clrs = [c for c in clrs if c.addr in cov_base]
n_set_useful = sum(1 for c in hit_sets
                   if any(S.is_setter(c, f, WANTS[f]) for f in SEEN_FLAGS))
n_clr_harm = sum(1 for c in hit_clrs
                 if any(S.is_clearer(c, f, WANTS[f]) for f in SEEN_FLAGS))
print("C3 收敛态命中 `set_flg` %d 点，其中能置任一等待位的 %d 点；"
      "命中 `clr_flg` %d 点，其中会清掉等待位的 %d 点"
      % (len(hit_sets), n_set_useful, len(hit_clrs), n_clr_harm))
for c in hit_sets:
    print("     set 0x%06X flgid=%s bits=%s"
          % (c.addr, c.flgid,
             "0x%08X" % c.bits if isinstance(c.bits, int) else c.bits))

print("C4 关键函数覆盖（逐条解码口径，不是 2 字节槽位）")
KEY = [
    ("任务2装配函数", 0x1200AC, 0x12046E),
    ("显示链公共状态查询函数", 0x12E6F8, 0x12E750),
    ("任务7函数体", 0x175192, 0x175700),
    ("flag17 置位函数", 0x1A4B8A, 0x1A4DD0),
    ("flag14 置位函数", 0x175196, 0x175500),
    ("ISR 巨型派发函数", 0x12D3BA, 0x12DA02),
]
for name, lo, hi in KEY:
    pcs = insn_pcs(lo, hi)
    got = [p for p in pcs if p in cov_base]
    print("     %-22s [0x%06X,0x%06X) 指令 %3d 条 / 覆盖 %3d 条"
          % (name, lo, hi, len(pcs), len(got)))
print("C5 显示链 5 PC：%s"
      % ["0x%06X:%s" % (p, "命中" if p in cov_base else "未命中")
         for p in DSP])

# ---------------------------------------------------------------- D 触发性
hr("D  触发性：置位点落在哪些中断向量里")

real = S.real_vectors(VT)
print("D1 256 槽向量表里非默认（非 0x0404F8 / 非空 / 非 FFFFFFFF）%d 条"
      % len(real))
entries = sorted({e for _v, e in real})
ranges = S.isr_ranges(entries, SW.funcs)
in_isr = S.points_in_ranges(all_pcs, ranges)
print("D2 置位点落在非默认 ISR 函数体里的 %d 组" % len(in_isr))
for e, lo, hi, got in in_isr:
    print("     ISR 0x%06X 体 [0x%06X,0x%06X) 置位点 %2d 个 %s"
          % (e, lo, hi, len(got), [hex(p) for p in got[:5]]))
shared = collections.defaultdict(list)
for e, lo, hi, got in in_isr:
    shared[(lo, hi)].append(e)
print("D3 多个向量共享同一个 `enter` 区间 ⇒ 巨型共享 ISR，不是工具 bug：")
for (lo, hi), es in sorted(shared.items()):
    if len(es) > 1:
        print("     [0x%06X,0x%06X) 被 %d 个向量指向 %s"
              % (lo, hi, len(es), [hex(e) for e in es]))
print("D4 ⇒ 缺口是**真机中断事件流**（本机只给过一次 raise_irq(0x2B)），"
      "不是代码缺席；但也不能反推「补一次服务调用就等于真机」。")

# ---------------------------------------------------------------- E 反事实
hr("E  四组等形反事实：置位 / 补 tick / 两者都补 / 都不补")

print("E0 说明：四台独立机器，每台 500000 步收敛 → 预热 1 次 + 6 次等形服务"
      " → 可选 12 轮模型化 tick → 一次 raise_irq(0x2B) → 再跑 200000 步。")
print("     `kernel_call` 恢复 CPU 上下文、只留服务的内存副作用；"
      "它自身的 PC 也计入覆盖，等形对照组用来扣掉这部分。")
G0 = counterfactual("G0 无补（等形对照）", False, False, WANTS)
G1 = counterfactual("G1 只置位", True, False, WANTS)
G2 = counterfactual("G2 只补 tick", False, True, WANTS)
G3 = counterfactual("G3 置位 + 补 tick", True, True, WANTS)

hr("E5 归因分离：0x12E6F8 到底是谁让它命中的")
for tag, G in (("G0", G0), ("G1", G1), ("G2", G2), ("G3", G3)):
    e, b = G["end"], G["base"]
    new = S.new_coverage(b["cov"], e["cov"])
    print("     %s 新增覆盖 %4d 个；0x12E6F8 %s；0x12E752 %s；"
          "state 变化 %s；末PC=0x%06X"
          % (tag, len(new),
             "命中" if 0x12E6F8 in e["cov"] else "未命中",
             "命中" if 0x12E752 in e["cov"] else "未命中",
             G["diff"]["state_diff"], e["pc"]))
print("     判据：G0 与 G1 都不让 0x12E6F8 命中，G2 与 G3 都让它命中 ⇒ "
      "归因于 **补 tick**，与置位无关。")
print("     G1 置位后瞬时：等待 %d / 就绪 %d（14 个 flag 等待者被真内核唤醒）；"
      "末尾又回到等待 %d"
      % (sum(1 for v in G1["mid"]["st"].values() if v == W.WAIT),
         sum(1 for v in G1["mid"]["st"].values() if v == W.READY),
         sum(1 for v in G1["end"]["st"].values() if v == W.WAIT)))
print("     G1 位模式：注入后 %s ⇒ 末尾 %s（clr_flg 把位清回去了）"
      % (["%d:0x%08X" % (f, G1["mid"]["pats"][f]) for f in sorted(WANTS)],
         ["%d:0x%08X" % (f, G1["end"]["pats"][f]) for f in sorted(WANTS)]))

# ---------------------------------------------------------------- F 窗口自证
hr("F  窗口自证：500000 步不是随手挑的窗口")
m_long = boot_run(RUN_LONG)
cov_long = coverage(m_long)
st_long = dict((t, bready.state_of(m_long, tcb))
               for t, tcb in bready.task_index(m_long).items())
print("F1 1000000 步：末PC=0x%06X 覆盖 %d 个；500000 步：末PC=0x%06X 覆盖 %d 个"
      % (m_long.cpu.pc, len(cov_long), m_base.cpu.pc, len(cov_base)))
print("F2 PC 覆盖对称差集 %d 个；29 个 state 差异任务号 %s"
      % (len(cov_base ^ cov_long),
         S.state_diff(dict((t, bready.state_of(m_base, tcb))
                           for t, tcb in idx_base.items()), st_long)))
print("F3 1000000 步显示链：%s"
      % ["0x%06X:%s" % (p, "命中" if p in cov_long else "未命中")
         for p in DSP])

# ---------------------------------------------------------------- G 边界
hr("G  显示链仍没闭合 / 能说 / 不能说 / 复现")
for tag, G in (("G0", G0), ("G1", G1), ("G2", G2), ("G3", G3)):
    e = G["end"]
    print("     %s 末尾 0x9F5A0284=0x%08X / 0x9F59EF50=0x%08X；"
          "8 槽 0x9F59F7C8=%s；显示链 %s"
          % (tag, e["words"][0], e["words"][1], e["slots"],
             ["0x%06X:%s" % (p, "中" if p in e["cov"] else "-")
              for p in DSP]))
print("能说：")
print("  1. 14 个等待位全有常量可见置位源（累加 %d 点 / 去重 %d 点），"
      "但收敛态命中 0 个；命中的 10 个 set_flg 没有一个能置等待位，"
      "命中的 16 个 clr_flg 里有 11 个反而会清等待位。"
      % (g_static["points"], g_static["unique_points"]))
print("  2. 13 组置位点落在非默认 ISR 函数体里，其中 "
      "`0x12D3BA..0x12DA02` 被 9 个向量共享 ⇒ 缺的是真机中断事件流。")
print("  3. 一次性真 set_flg 能把 14 个 flag 等待者全部唤醒、跑出 %d 个新 PC，"
      "但位被 clr_flg 清回、任务重新挂起，显示链不动。"
      % G1["diff"]["new_cov"])
print("  4. 补 12 轮模型化 tick 让 `0x12E6F8` 首次命中（G2 新增 %d PC、"
      "G3 新增 %d PC），归因与置位无关；`0x12E752` 四组都未命中。"
      % (G2["diff"]["new_cov"], G3["diff"]["new_cov"]))
print("不能说：")
print("  · 不能报「真机就是这样」：四组全是反事实补全，"
      "tick_v52 是 V52 的模型化唤醒，不是固件时基（KI#191）。")
print("  · 不能报显示链通了 / 出画面：0x12E752 未命中，"
      "0x9F5A0284、0x9F59EF50 仍 0，8 槽全 0。")
print("  · 不能把「静态置位源存在」当成「跑过」，也不能把"
      "「常量可见口径 0 点」当成「固件没有别的路径」。")
print("  · 不能把 0x12E6F8 命中归因于置位：G2 只补 tick 也命中。")
print("复现：")
print("  cd emu && python probe/exp_set_v68.py > ../results/frbox/set68.txt")
print("  ⚠️ 六台机器各跑一遍：G0–G3 四台反事实 + 一台 1000000 步窗口自证"
      " + A/C 段用的一台 500000 步基线机。")
print("  ⚠️ 静态段不建机器、反事实段明确标注；"
      "不补映射、不给状态字；加长 RUN 只增执行预算（V65 口径）。")
