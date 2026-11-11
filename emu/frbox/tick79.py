# -*- coding: utf-8 -*-
"""tick79.py —— V79 纯函数设施（不读 ROM、不跑机器）。

## 本版回答什么

V78 留下 KI#307 第一条：「`tslp_tsk(10)` 的超时 tick 由谁递减」与
「清零循环 `0x1758EC` 的不可定址入边」。V79 把这两条接成一条链：

* **静态**：真时基 vec 0x18 的 ISR `0x04046E` 在 `0x040482`
  `call @r12` 调到 `0x040A44`，后者递增 64 位系统时钟并递减延时队列
  剩余 tick。向量表按规矩 60 倒序换算：`vec(n) = TBR + (255-n)*4`。
* **形态**：`call:d` 有两种互斥形态 —— `call:d @rN`（寄存器间接）与
  **`call:d rel12`（PC 相对 + 延迟槽）**。V78 的四套口径与
  ``bstatic.Sweep.edges`` 只收前者，漏掉 9642 条 `call:d rel12`，
  其中 **7616 个函数入口只被这一形态解出**。
  `0x1758EC` 的 5 条入边全属这一形态 ⇒ V78 的「四套入边全 0」
  是**假阴性**，不是「没人调用」（规矩 70）。
* **动态**：补 vec 0x18 反事实让 clock 线性增长、task7 剩余 tick 递减；
  约 110 次后等待函数跨过「端口稳定 10 次」阈值，
  `ret` 回 `0x17541A` → `0x1754D6 call:d rel12` → `0x1758EC`，
  清零段 12/12 命中。错配 vec 0x2B 等形对照全 0（归因分离）。
* **方法学**：清零循环写的就是 0，**8 槽全 0 不能当作「没跑过」的证据**
  （规矩 71）。写回函数 `0x175908` 仍 0/75，链未闭合。

## ⚠️ 边界（写报告必须带上）

* 所有 ``raise_irq`` / 直接写 ``TCB+0x14`` / ``btick.expire_strict``
  都是**反事实补全**，不是固件行为；真机中断事件流未建模（KI#191）。
* 间接调用归约是**保守局部传播**：只认 16 条窗口内最近的
  ``ldi:32`` 常量，被 ``ld`` / ``add`` 等改写就记不可定。
  归约不出的 534 条「从内存加载目标」仍可能指向任意地址。
* 不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
  「补一次 `set_flg` 就唤醒成功」/「清零循环函数没人调用」。
"""

__all__ = [
    "TBR", "VEC_TIMER", "VEC_BOOT", "TICK_ISR", "TICK_CALL", "TICK_FN",
    "CLOCK", "SENTINEL", "TCB_TICKS", "PORT", "SLOT8", "CLEAR_FN",
    "CLEAR_ST", "WAIT_FN", "WB_FN", "SUBMIT_FN", "STABLE_LIMIT",
    "BOOT_STEPS", "RUN_STEPS", "POST_STEPS", "WINDOW_STEPS",
    "FORM_CENSUS", "CLEAR_IN_EDGES", "TICK_IN_EDGES",
    "sx", "sx8", "sx11", "rel12_target", "rel9_target", "vec_slot",
    "call_form", "is_relative_form", "form_census", "only_by",
    "resolve_indirect", "merge_targets", "stable_loop_verdict",
    "clock_chain_verdict", "equal_shape", "attribution",
    "slot_zero_verdict", "tick_verdict",
]

# ---------------------------------------------------------------- 常量
TBR = 0x000EFC00
VEC_TIMER = 0x18
VEC_BOOT = 0x2B
TICK_ISR = 0x0004046E
TICK_CALL = 0x00040482
TICK_FN = 0x00040A44
CLOCK = 0x68000110
SENTINEL = 0x68000118
TCB_TICKS = 0x14
PORT = 0x50000000
SLOT8 = 0x9F59F7C8
WAIT_FN = 0x00175876
CLEAR_FN = 0x001758EC
CLEAR_ST = 0x001758FA
WB_FN = 0x00175908
SUBMIT_FN = 0x0012E6F8
STABLE_LIMIT = 10

BOOT_STEPS = 191520
RUN_STEPS = 500000
POST_STEPS = 200000
WINDOW_STEPS = 1000000

#: V79 全 ROM 七形态普查（`_v79g.py` 实测，只作常量固化，不是推算）
FORM_CENSUS = {
    "call:d @r": 16027,
    "call:d rel12": 9642,
    "call rel12": 9160,
    "call @r": 4823,
    "jmp @r": 3559,
    "jmp:d @r": 3266,
    "ldi:32": 48611,
}
#: 各形态可定目标数（去重后）
FORM_TARGETS = {
    "call:d rel12": 8205,
    "call rel12": 8672,
    "ldi:32": 9863,
}
#: 间接形态保守局部归约可定条数
RESOLVED = {
    "call:d @r归约": 15425,
    "call @r归约": 4642,
    "jmp @r归约": 2975,
    "jmp:d @r归约": 1970,
}
#: 只被 `call:d rel12` 解出的函数入口数
ONLY_CALLD_REL12 = 7616

#: `0x1758EC` 的 5 条 `call:d rel12` 入边（V78 报 0 是漏扫）
CLEAR_IN_EDGES = (0x1752E4, 0x175386, 0x1753C4, 0x1754A8, 0x1754D6)
#: `0x040A44` 的入边：ldi:32 引用 + call @r 归约
TICK_IN_EDGES = {"ldi:32": (0x04047C,), "call @r归约": (0x040482,)}
#: `0x12E6F8` 调用边按形态（V76 的 312 个加载点 ≠ 调用边总数）
SUBMIT_EDGES = {
    "call:d @r归约": 302,
    "call @r归约": 10,
    "call:d rel12": 5,
}

_NOWRITE = frozenset({
    "ret", "reti", "ret:d", "enter", "int", "nop", "bra", "bra:d",
    "beq", "beq:d", "bne", "bne:d", "blt", "bgt", "bhi", "bls",
    "bcc", "bcs", "bvc", "bvs", "bn", "bp", "cmp", "btst",
    "st", "stb", "sth", "stm0", "stm1", "andccr", "orccr", "jmp",
    "jmp:d",
})
_MOVLIKE = frozenset({"mov", "add", "add2", "sub", "and", "or", "eor",
                      "ld", "ldub", "lduh", "dmov"})
_BOUNDARY = frozenset({"ret", "reti", "ret:d", "enter", "int"})
_CALLMN = frozenset({"call", "call:d", "jmp", "jmp:d"})


# ---------------------------------------------------------------- 位运算
def sx(v, bits):
    """``bits`` 位补码（与 ``fr_emu`` 的 ``sx`` 同口径）。"""
    m = 1 << (bits - 1)
    v = v & ((1 << bits) - 1)
    return (v ^ m) - m if v & m else v


def sx8(v):
    """8 位有符号（``rel9`` 用，规矩 57）。"""
    return sx(v, 8)


def sx11(v):
    """11 位有符号（``rel12`` 用；**不是 12 位**，fr_emu 实证）。"""
    return sx(v, 11)


def rel12_target(pc, rel12):
    """``call`` / ``call:d rel12`` 的目标：``pc + 2 + sx11(rel12)*2``。"""
    return (pc + 2 + sx11(rel12) * 2) & 0xFFFFFFFF


def rel9_target(pc, rel9):
    """``rel9`` 分支目标：``pc + 2 + sx8(rel9)*2``。"""
    return (pc + 2 + sx8(rel9) * 2) & 0xFFFFFFFF


def vec_slot(vec, tbr=TBR):
    """规矩 60：向量表**倒序**，``vec(n) = TBR + (255-n)*4``。"""
    return tbr + (255 - vec) * 4


# ---------------------------------------------------------------- 形态
def call_form(mn, v):
    """把一条 call/jmp 指令归到七形态之一；非调用返回 ``None``。

    ``v`` 是解码字段 dict。关键分歧：``call:d`` 带 ``rel12`` 时是
    **PC 相对 + 延迟槽**，不是寄存器间接 —— V78 漏的就是这一支。
    """
    if mn not in _CALLMN:
        return None
    if "rel12" in v:
        return mn + " rel12"
    return mn + " @r"


def is_relative_form(kind):
    """是否 PC 相对形态（目标无需寄存器传播即可定）。"""
    return bool(kind) and kind.endswith(" rel12")


def form_census(census):
    """形态计数 → ``(总调用点数, 相对形态数)``（不含 ``ldi:32``）。"""
    total = sum(n for k, n in census.items() if not k.startswith("ldi"))
    rel = sum(n for k, n in census.items() if is_relative_form(k))
    return {"total": total, "relative": rel}


def only_by(kind, targets, known=()):
    """只被 ``kind`` 解出的入口（``targets`` 形态名 → 目标集合）。"""
    own = set(targets.get(kind, ()))
    other = set()
    for k, vals in targets.items():
        if k != kind:
            other |= set(vals)
    other |= set(known)
    return sorted(own - other)


# ---------------------------------------------------------------- 归约
def _writes(mn, v, reg):
    if mn in _NOWRITE:
        return False
    if v.get("ri") == reg:
        return True
    return mn in _MOVLIKE and v.get("rj") == reg


def resolve_indirect(insns, window=16):
    """保守局部传播：把 ``call/jmp @rN`` 归约到最近的 ``ldi:32`` 常量。

    ``insns`` 是 ``[(pc, mn, v, size), ...]``（按地址升序）。
    返回 ``(resolved, reasons)``：``resolved`` 是 ``{pc: (reg, target, src_pc)}``，
    ``reasons`` 是不可定原因计数。只认 ``ldi:32``；中途被别的写点改写、
    跨过函数边界、或窗口用尽都记不可定（规矩 70：不许把不可定说成没有）。
    """
    import collections
    resolved, reasons = {}, collections.Counter()
    for i, (pc, mn, v, _s) in enumerate(insns):
        if mn not in _CALLMN or "rel12" in v:
            continue
        reg = v.get("ri")
        if reg is None:
            reasons["no_ri"] += 1
            continue
        hit = None
        for j in range(i - 1, max(-1, i - 1 - window), -1):
            p2, mn2, v2, _s2 = insns[j]
            if mn2 in _BOUNDARY:
                reasons["boundary_" + mn2] += 1
                break
            if _writes(mn2, v2, reg):
                if mn2 == "ldi:32":
                    hit = (reg, v2["i32"] & 0xFFFFFFFF, p2)
                else:
                    reasons["overwritten_" + mn2] += 1
                break
        else:
            reasons["window_exhausted"] += 1
        if hit:
            resolved[pc] = hit
    return resolved, reasons


def merge_targets(*maps):
    """把多个 ``{目标: [调用点]}`` 合并成一个。"""
    import collections
    out = collections.defaultdict(list)
    for mp in maps:
        for t, pcs in mp.items():
            out[t].extend(pcs)
    return dict(out)


# ---------------------------------------------------------------- 链判据
def stable_loop_verdict(beq_pc, beq_rel9, add_pc, cmp_u4, blt_pc,
                        blt_rel9, loop_head, limit=STABLE_LIMIT):
    """等待函数「端口稳定 N 次才退出」的循环判据（规矩 57 算分支目标）。

    返回各分支目标与是否构成回边闭环。
    """
    beq_t = rel9_target(beq_pc, beq_rel9)
    blt_t = rel9_target(blt_pc, blt_rel9)
    return {
        "beq_target": beq_t, "blt_target": blt_t, "loop_head": loop_head,
        "limit": limit,
        "beq_skips": beq_t == add_pc,
        "blt_loops": blt_t == loop_head,
        "stable_loop": beq_t == add_pc and blt_t == loop_head,
    }


def clock_chain_verdict(insns):
    """在一段反汇编里找 64 位时钟递增与延时 tick 递减链。

    认 ``add #1`` + ``addc``（时钟低/高 32 位）与
    ``ld @(disp10=5)`` + ``add2 m4=15``（``+0x14`` 剩余 tick 减 1）。
    """
    mns = [m for _p, m, _v, _s in insns]
    has_add = "add" in mns
    has_addc = "addc" in mns
    has_add2 = "add2" in mns
    return {
        "clock_increment": has_add and has_addc,
        "tick_decrement": has_add2,
        "chain": has_add and has_addc and has_add2,
        "instructions": len(insns),
    }


# ---------------------------------------------------------------- 反事实
def equal_shape(rows):
    """等形判据：注入次数、后续步数一致才可比（规矩 51）。"""
    import collections
    cols = ("injections", "post_steps")
    out = {}
    for c in cols:
        vals = [r.get(c) for r in rows]
        out[c] = len(set(repr(v) for v in vals)) == 1
    out["equal"] = all(out[c] for c in cols)
    return out


def attribution(timer_rows, mismatch_rows, key="clock_delta"):
    """归因分离：计时向量组相对错配向量组是否确有差异。"""
    a = [r.get(key, 0) for r in timer_rows]
    b = [r.get(key, 0) for r in mismatch_rows]
    return {
        "timer": a, "mismatch": b,
        "timer_nonzero": any(a), "mismatch_all_zero": not any(b),
        "separated": any(a) and not any(b),
    }


def slot_zero_verdict(nz_before, nz_after, writes_zero=True):
    """规矩 71：清零循环写的是 0，8 槽全 0 不能证明「没跑过」。

    只有当写的不是 0、且非零数变化，才能用内存值推断执行。
    """
    if writes_zero:
        return {"usable": False, "reason": "writer_is_zero",
                "nz_before": nz_before, "nz_after": nz_after}
    return {"usable": True, "changed": nz_before != nz_after,
            "nz_before": nz_before, "nz_after": nz_after}


def tick_verdict(timer_edges, timer_dyn, mismatch_dyn,
                 clear_hit, wb_hit, slot):
    """综合判据：静态链闭合 + 动态归因成立 + 清零段命中 + 写回段未命中。"""
    return {
        "static_chain": bool(timer_edges),
        "dynamic_attribution": attribution(timer_dyn, mismatch_dyn)["separated"],
        "clear_segment_hit": bool(clear_hit),
        "writeback_hit": bool(wb_hit),
        "slot_evidence_usable": slot["usable"],
        "chain_closed": bool(clear_hit) and bool(wb_hit),
        "verdict": ("tick_chain_located_but_chain_open"
                    if bool(clear_hit) and not bool(wb_hit)
                    else "inconclusive"),
    }
