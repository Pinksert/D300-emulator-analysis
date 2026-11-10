# -*- coding: utf-8 -*-
"""caller70.py —— V70：直接调用边的四形态口径 / r10 局部标志 / 执行前沿（纯函数）。

V69 B3 用 ``bstatic.Sweep.callers_of()`` 报「派发函数 ``0x12E1EC``、干活分支、
清空队列、取首、显示初始化静态直接调用边全 0」，据此把它们写成「只能经
函数指针或数据表间接到达」。V70 用 ``svcgraph.ImmIndex`` 的 ``rel12`` 边
重查，发现这些函数全在 ``0x12DFB2`` 被 ``call rel12`` **直接调用**：
那个函数是任务表的任务 5 入口 ``0x12DFAE``（字段 ``0x54C80``），
12 条 call 里 1 条到 ``0x12E1EC``、5 条到大函数序言 ``0x12E48E``。

所以不是固件缺调用者，是扫描器口径假阴性：``Sweep.edges`` 只收
``call:d @rN`` 一种，``call rel12`` 根本不在图里（KI#175 / 硬规矩 32，
V54 栽过、V58 补过、V69 又踩了一次）。

两条本版新增纪律（硬规矩 54 / 55）：

* **54** 报「静态直接调用边 0」之前先问扫了几种 call 形态。
  ``call:d @rN`` 与 ``call rel12`` 是两套；四形态
  （``ldi20`` / ``ldi32`` / ``rel12`` / ``data32``）都查过才能报「没有引用」。
  另外「有静态调用边」不等于「会执行」（硬规矩 5）。
* **55** 查「谁置这个寄存器」先在**同一个函数体内**找写点，别默认是
  调用者参数。``0x12E6E0 cmp r10,#0`` 的 r10 由 ``0x12E490`` 函数内
  ``0x12E69E``（置 1）与 ``0x12E6BA``（置 0）给出，函数内写 5 次、读 2 次。

边界：本模块是纯函数，不建机器、不扫 ROM、不写内存。全 ROM 数字
（8 个关键 PC 的引用形态 / 12 条 call / 262 条指令 / 执行前沿 / 反事实
54 个新 PC）由 ``probe/exp_caller_v70.py`` 与 ``results/frbox/caller70.txt``
覆盖。反事实注入（``kernel_call`` / ``tick_v52``）不是固件行为。
"""

import fr_emu

DISPATCH_FN = 0x0012DFB2
TASK5_ENTRY = 0x0012DFAE
TASK5_FIELD = 0x00054C80
CURSOR_FN = 0x0012E1EC
BIG_FN = 0x0012E490
BIG_PRE = 0x0012E48E
CALL_CURSOR = 0x0012DFD0
GATE_PC = 0x0012E6E0
R10_ONE = 0x0012E69E
R10_ZERO = 0x0012E6BA
FRONTIER_BASE = 0x0012DFBA
FRONTIER_WAKE = 0x0012DFC0
WAIT_SVC = -46                  # wai_flg
FLAG_ID = 3
WANT_BASE = 0x00000009
QUEUE_TASK5 = 0x680008D8
TCB_TASK5 = 0x680001EC
NOOP_HANDLER = 0x0012E752
SUBMIT = 0x0012E6F8

KEY_PCS = (TASK5_ENTRY, DISPATCH_FN, 0x12DFB8, FRONTIER_BASE, 0x12DFBC,
           0x12DFBE, FRONTIER_WAKE, 0x12DFC2, 0x12DFCA, CALL_CURSOR,
           0x12E006, CURSOR_FN, BIG_PRE, BIG_FN, R10_ONE, R10_ZERO,
           GATE_PC, 0x12E6F0, SUBMIT, NOOP_HANDLER)

__all__ = ["DISPATCH_FN", "TASK5_ENTRY", "TASK5_FIELD", "CURSOR_FN",
           "BIG_FN", "BIG_PRE", "CALL_CURSOR", "GATE_PC", "R10_ONE",
           "R10_ZERO", "FRONTIER_BASE", "FRONTIER_WAKE", "WAIT_SVC",
           "FLAG_ID", "WANT_BASE", "QUEUE_TASK5", "TCB_TASK5",
           "NOOP_HANDLER", "SUBMIT", "KEY_PCS",
           "rel12_target", "rel9_target", "kind_counts", "direct_edges",
           "missing_edge_verdict", "is_write_ri", "split_reg_writes",
           "sources_before", "frontier", "wait_reason", "flag_ready",
           "advance", "equal_shape", "window_same", "chain_verdict"]


def rel12_target(pc, rel12):
    """``call rel12`` 的目标；符号位 bit10，与 ``fr_emu.py:777`` 逐字对齐。"""
    return (pc + 2 + fr_emu.sx(rel12, 11) * 2) & 0xFFFFFFFF


def rel9_target(pc, rel9):
    """``rel9`` 是 **8 位有符号**（硬规矩 44，V64 修过），不是 9 位。"""
    return (pc + 2 + fr_emu.sx(rel9, 8) * 2) & 0xFFFFFFFF


def kind_counts(refs):
    """``[Ref(kind=...)]`` → ``{kind: n}``；只统计，不去重、不互斥。"""
    c = {}
    for r in refs:
        k = r["kind"] if isinstance(r, dict) else r.kind
        c[k] = c.get(k, 0) + 1
    return dict(sorted(c.items()))


def direct_edges(rel12_n, calld_n):
    """两种直接调用边合并；``Sweep.edges`` 只有后者，会漏掉前者。"""
    return int(rel12_n) + int(calld_n)


def missing_edge_verdict(edges_old, edges_all, rel12_n):
    """判「edges 0」是真阴性还是口径假阴性。

    返回 ``{"old", "all", "rel12", "verdict"}``；
    ``verdict`` ∈ ``blindspot`` / ``true_negative`` / ``consistent``。
    """
    old, all_, r = int(edges_old), int(edges_all), int(rel12_n)
    v = "blindspot" if old == 0 and r > 0 else (
        "true_negative" if all_ == 0 else "consistent")
    return {"old": old, "all": all_, "rel12": r, "verdict": v}


#: 会写 ``ri`` 的指令族；``st*`` / ``cmp`` / ``btst*`` / ``band*`` 只读
WRITE_RI = ("ldi", "mov", "ld", "add", "sub", "mul", "and", "or", "xor",
            "ext", "lsl", "lsr", "asr", "neg", "not", "dmov")
READ_ONLY = ("st", "cmp", "btst", "band", "bor", "bxor")


def is_write_ri(mn):
    """这条指令会不会写 ``ri`` 寄存器（用于数据流分类，不解析语义）。"""
    return mn.startswith(WRITE_RI) and not mn.startswith(READ_ONLY)


def split_reg_writes(rows, reg=10):
    """``[(pc, mn, v)]`` → ``{"writes": [...], "reads": [...]}``。

    只按 ``v['ri'] == reg`` 精确匹配，不用 repr 子串（V70 草稿 A 把
    ``op1: 10`` 当寄存器，造出一批假写点）。
    """
    w, r = [], []
    for p, mn, v in rows:
        if v.get("ri") == reg:
            (w if is_write_ri(mn) else r).append((p, mn, v))
    return {"writes": w, "reads": r,
            "n_writes": len(w), "n_reads": len(r)}


def sources_before(writes, gate, n=2):
    """门之前最近的 ``n`` 条写点（按 pc 升序返回）。"""
    prev = [x for x in writes if x[0] < gate]
    return sorted(prev, key=lambda x: x[0])[-n:]


def frontier(cov, pcs):
    """执行前沿：``pcs`` 里最后一条命中；全不命中返回 ``None``。"""
    hit = [p for p in sorted(pcs) if p in cov]
    return {"frontier": hit[-1] if hit else None,
            "n_hit": len(hit), "n_total": len(pcs),
            "first_miss": next((p for p in sorted(pcs) if p not in cov), None)}


def wait_reason(half16):
    """``TCB+0x20`` 高半按 **16 位补码** 读服务号（V67 口径）。"""
    v = half16 & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def flag_ready(pat, want):
    """OR / AND 两种等待模式共用的必要条件：``pat & want``。"""
    return (pat & want) != 0 and want != 0


def advance(base_cov, end_cov, pcs):
    """反事实把哪些关键 PC 从没命中推成命中（只报新增，不报原有）。

    ``frontier`` 是**新增命中里地址最大的一条**，不是全局执行前沿：
    ``pcs`` 跨多个代码区时（KEY_PCS 同时含 0x12DFxx 与 0x12E0xx），
    用 ``frontier(end_cov, pcs)`` 会挑到另一个区里本来就命中的高地址。
    要报某一段的执行前沿，得只对那一段的连续 PC 调 ``frontier()``。
    """
    new = sorted(p for p in pcs if p not in base_cov and p in end_cov)
    return {"new": new, "n_new": len(new),
            "frontier": new[-1] if new else None}


def equal_shape(groups):
    """等形对照：boot / post / svc / irqs 四个共有键是否一致。"""
    keys = ("boot", "post", "svc", "irqs")
    vals = {k: {g[k] for g in groups.values() if k in g} for k in keys}
    bad = [k for k, s in vals.items() if len(s) > 1]
    return {"ok": not bad, "bad": bad,
            "values": {k: sorted(s) for k, s in vals.items()}}


def window_same(a, b):
    """两个窗口的收敛自证：末 PC / PC 覆盖 / 关键命中逐项相同。"""
    diff = []
    if a.get("pc") != b.get("pc"):
        diff.append("pc")
    if set(a.get("cov", ())) != set(b.get("cov", ())):
        diff.append("cov")
    if dict(a.get("key", {})) != dict(b.get("key", {})):
        diff.append("key")
    return {"same": not diff, "diff": diff,
            "symdiff_cov": len(set(a.get("cov", ())) ^ set(b.get("cov", ())))}


def chain_verdict(caller_exists, frontier_pc, wake_frontier, gate_hit,
                  noop_hit=False):
    """综合判据：调用链存在 ≠ 链跑通。

    返回 ``{"caller", "stuck", "wake_advanced", "gate_hit", "closed"}``；
    ``closed`` 恒要求 ``gate_hit``，且不许拿空实现槽位命中当证据。
    """
    return {
        "caller": bool(caller_exists),
        "stuck": frontier_pc,
        "wake_advanced": wake_frontier != frontier_pc,
        "wake_frontier": wake_frontier,
        "gate_hit": bool(gate_hit),
        "noop_hit": bool(noop_hit),
        "closed": bool(gate_hit) and not bool(noop_hit),
    }
