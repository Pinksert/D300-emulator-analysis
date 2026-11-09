# -*- coding: utf-8 -*-
"""setsrc68.py —— V68：等待位的静态置位源 / 动态覆盖 / 反事实差分（纯函数）。

承接 V67 的 KI#261「谁来置这 14 个等待者要求的位」。V68 的答案是三层，
三层必须一起报，缺一层就会得出相反结论：

1. **存在性**：全 ROM 里 14/14 个等待位都有**常量可见**的 ``set_flg``
   静态点（133 个去重 PC）。所以不能说「固件里没人能置这些位」。
2. **执行性**：这 133 个点在 500000 步收敛机器里**命中 0 个**；收敛态
   命中的 10 个 ``set_flg`` 没有一个能置等待位，命中的 16 个 ``clr_flg``
   里倒有 11 个会清掉等待位。所以不能说「置位代码跑过了」。
3. **触发性**：13 组置位点落在非默认 ISR 的函数体里（尤其
   ``0x12D3BA..0x12DA02`` 这个多向量共享的巨型 ISR）。模拟器只给过一次
   ``raise_irq(0x2B)``，其余真机中断事件没建模 ⇒ 缺口是**事件流**，不是
   代码缺席，也不是「补一次服务调用就等于真机」。

另外两条口径，别再混：

* ``clr_flg(id, mask)`` 清的是 **mask 里为 0 的位**（硬规矩 20），
  所以判「会不会清掉等待位」要用 ``~mask & want``，不是 ``mask & want``。
* 反事实注入 ``brealos.kernel_call`` 会**恢复 CPU 上下文**，只把服务的
  内存副作用留下；对照组必须做等形（同次数、同预热、同中断）的空调用，
  否则差分里混进的是注入工具本身。

本模块全部是纯函数：不建机器、不读总线、不解码 ROM，便于自检。
调用点用 duck typing，只要有 ``addr / svc / flgid / bits`` 四个属性。
"""
from __future__ import unicode_literals

from collections import Counter, defaultdict

# ---- 服务号 ---------------------------------------------------------------
SVC_SET_FLG = -48
SVC_CLR_FLG = -47
SVC_WAI_FLG = -46
SVC_GET_TID = -24
#: 向量表里 208 个槽填的默认 ISR 入口（V63）
DEFAULT_ISR = 0x0404F8
#: 不算「真实向量」的哨兵值
NON_VECTOR = (None, 0xFFFFFFFF, DEFAULT_ISR)

#: V67 收敛态实测的 14 个 flag 等待者（flgid, 任务号, 等待位模式）
#: 来源 results/frbox/wait67.txt E 段；写在这里只为自检和小工具用，
#: 正式探针一律从机器现场读，不用这张表当结论。
WAITERS_V67 = (
    (3, 5, 0x00000009), (3, 2, 0x00200000), (3, 26, 0x00010000),
    (5, 3, 0x0000970F),
    (6, 4, 0x00004000), (6, 6, 0x00002000), (6, 9, 0x00000020),
    (6, 10, 0x00000040), (6, 11, 0x00000080), (6, 27, 0x00000100),
    (6, 28, 0x00000200),
    (8, 1, 0x80000000),
    (14, 20, 0x80000000),
    (17, 16, 0x00000002),
)


# ------------------------------------------------------------------ 位
def bits_of(mask):
    """32 位掩码 → 置 1 的位列表（升序）。非整数返回空表。"""
    if not isinstance(mask, int):
        return []
    return [1 << i for i in range(32) if mask & (1 << i)]


def wants_by_flag(waiters):
    """``[(flgid, tid, want)]`` → ``{flgid: 所有等待位模式的 OR}``。"""
    out = defaultdict(int)
    for f, _tid, want in waiters:
        out[f] |= want
    return dict(out)


# -------------------------------------------------------------- 置位源
def is_setter(call, flgid, want):
    """一个调用点是不是「常量可见、且能置上该等待位」的 set_flg。"""
    return (call.svc == SVC_SET_FLG and call.flgid == flgid
            and isinstance(call.bits, int) and bool(call.bits & want))


def is_clearer(call, flgid, want):
    """一个调用点是不是会**清掉**等待位的 clr_flg（清 mask 里为 0 的位）。"""
    return (call.svc == SVC_CLR_FLG and call.flgid == flgid
            and isinstance(call.bits, int)
            and bool((~call.bits & 0xFFFFFFFF) & want))


def setters_for(calls, flgid, want):
    return [c for c in calls if is_setter(c, flgid, want)]


def clearers_for(calls, flgid, want):
    return [c for c in calls if is_clearer(c, flgid, want)]


def unique_pcs(calls):
    """调用点 PC 去重（同一个点可能同时满足多个等待者）。"""
    return sorted({c.addr for c in calls})


# -------------------------------------------------------------- 覆盖
def covered(pcs, cov):
    return [p for p in pcs if p in cov]


def coverage_report(waiters, calls, cov):
    """每个等待者的「置位源 / 收敛态命中」逐项表。

    返回 dict 列表（按 waiters 原序）：``flgid / tid / want / pcs / hit /
    points / hit_count``。``pcs`` 是常量可见置位点的去重 PC，``hit`` 是
    其中落在覆盖率里的那些。
    """
    out = []
    for f, tid, want in waiters:
        pcs = unique_pcs(setters_for(calls, f, want))
        hit = covered(pcs, cov)
        out.append({"flgid": f, "tid": tid, "want": want,
                    "pcs": pcs, "hit": hit,
                    "points": len(pcs), "hit_count": len(hit)})
    return out


def gap_total(report):
    """覆盖总账：按等待者累加与全局去重两种口径分开报，别混。

    ``points`` / ``hit`` 是**按等待者累加**（同一个 PC 若服务多个等待者，
    会被计多次）；``unique_points`` / ``unique_hit`` 是全局去重后的 PC 数。
    """
    all_pcs = [p for row in report for p in row["pcs"]]
    all_hit = [p for row in report for p in row["hit"]]
    return {
        "points": sum(row["points"] for row in report),
        "hit": sum(row["hit_count"] for row in report),
        "unique_points": len(set(all_pcs)),
        "unique_hit": len(set(all_hit)),
        "waiters": len(report),
        "zero_hit": sum(1 for row in report if row["hit_count"] == 0),
    }


# -------------------------------------------------------------- ISR
def real_vectors(vecs, ignore=NON_VECTOR):
    """``[Vec]`` → 非默认向量 ``[(vec, entry)]``（升序、entry 去重再单独给）。"""
    return sorted((v.vec, v.entry) for v in vecs if v.entry not in ignore)


def isr_ranges(entries, funcs, span=0x200):
    """ISR 入口 → 按最近 ``enter`` 划出的粗粒度函数区间。

    ``funcs`` 是升序的 ``enter`` 地址表。返回 ``[(entry, lo, hi)]``。
    ⚠️ 硬规矩 43：存在无 ``enter`` 的 ISR 序言，``enclosing()`` 可能归错；
    多个向量落在同一个 ``enter`` 区间是**巨型共享 ISR**，不是工具 bug。
    """
    import bisect
    out = []
    for e in entries:
        i = bisect.bisect_right(funcs, e) - 1
        lo = funcs[i] if i >= 0 else e
        j = bisect.bisect_right(funcs, e)
        hi = funcs[j] if j < len(funcs) else e + span
        out.append((e, lo, hi))
    return out


def points_in_ranges(points, ranges):
    """点落在哪些区间 → ``[(entry, lo, hi, [pc...])]``，只留有命中的。"""
    out = []
    for e, lo, hi in ranges:
        got = [p for p in points if lo <= p < hi]
        if got:
            out.append((e, lo, hi, got))
    return out


# -------------------------------------------------------------- 差分
def state_diff(before, after):
    """两个 ``{任务号: state}`` → 变了的任务号（升序）。"""
    return sorted(t for t in set(before) | set(after)
                  if before.get(t) != after.get(t))


def new_coverage(base, after):
    return set(after) - set(base)


def pattern_diff(before, after):
    """两个 ``{flgid: 位模式}`` → ``[(flgid, old, new)]``（升序）。"""
    return [(f, before.get(f), after.get(f))
            for f in sorted(set(before) | set(after))
            if before.get(f) != after.get(f)]


def diff_summary(before, after, dsp_pcs=()):
    """反事实差分总账：状态差异 / 新增覆盖 / 显示链新增命中。"""
    new = new_coverage(before.get("cov", ()), after.get("cov", ()))
    return {
        "state_diff": state_diff(before.get("st", {}), after.get("st", {})),
        "new_cov": len(new),
        "dsp_new": [p for p in dsp_pcs if p in new],
        "pattern_diff": pattern_diff(before.get("pats", {}),
                                     after.get("pats", {})),
    }
