# -*- coding: utf-8 -*-
"""V71：任务 5 的四级状态机与第二道等待判据（纯函数，只读不跑机器）。

承接 KI#274 第一条。V70 把 ``0x74`` 叫作「任务 5 等的新位，由谁置」；
V71 证伪这个前提：``0x74`` 是 ``0x12E1C6 ldi:8 #116`` 给出的**等待位模式
与事后检查掩码**，不是需要被置的某个位。真正的通过条件是
``(flgptn & 0x74) == 0x10``：``0x12E1E0 cmp r1,r0`` 紧跟
``0x12E1E2 beq:d``，相等时延迟槽 ``0x12E1E4 ldi:8 #1,r4`` 生效使 r4=1，
不等则多走一条 ``0x12E1E6 ldi:8 #0,r4`` 使 r4=0。任务 5 主体
``0x12DFC2 cmp r4,#0`` / ``0x12DFC4 bne`` 用这个返回值决定进不进下一级。

**规矩 48（服务号按立即数宽度解码）**：``0x12E1CC ldi:32 #0xFFFFFF56``
是 32 位值 −170，``bstatic.Sweep.calls`` 取低 8 位报 86 是假象；运行时
``TCB+0x20`` 高半 ``0xFF56`` 按 16 位补码也是 −170，两路互证。

**规矩 56（把「谁置这个位」问出口之前先分清它是掩码还是等待位）**：
等待位模式（waiptn）、清位掩码（clr_flg 的 mask）、检查常量三种东西
在同一段代码里长得一模一样，必须按它们各自被谁读来定性。

**规矩 57（报「之后的代码没跑」前先算分支目标）**：``0x12DFD0`` 不是
``0x12DFC2`` 的直线后继，而是 ``0x12DFC4 bne rel9=5`` 的直接目标；
V70 把「0x12DFC2 及之后 12 条 call 全没跑」当成执行前沿结论，实际漏了
这条分支边。rel9 的符号位与 ``fr_emu.py`` 一致（8 位有符号，单位 2 字节）。

所有反事实（两级 ``set_flg`` 注入）都是外部补全，不是固件行为。
"""

# ---------------------------------------------------------------- 地址常量
TASK5_ENTRY = 0x0012DFAE
TASK5_FN = 0x0012DFB2
LOOP_HEAD = 0x0012DFBA
WAIT1_FN = 0x0012E0F8
WAIT2_FN = 0x0012E162
CLR_FLG_PC = 0x0012E196
WAIT2_SVC_PC = 0x0012E1D2
DISPATCH_FN = 0x0012E1EC
BIG_FN = 0x0012E490
GATE_PC = 0x0012E6E0
INDIRECT_CALL = 0x0012E6F0
SUBMIT = 0x0012E6F8
NOOP_HANDLER = 0x0012E752
DESC_PTR = 0x9F58D858
WORD_A = 0x9F5A0284
WORD_B = 0x9F59EF50
SLOTS8 = 0x9F59F7C8

# ---------------------------------------------------------------- 语义常量
FLAG_ID = 3
WANTPTN_1 = 0x09
MASK = 0x74                 # 0x12E1C6 / 0x12E1DA 的 ldi:8 #116
EXPECT = 0x10                # 0x12E1DE 的 ldi:8 #16
WFMODE = 2                  # 0x12E1C8 ldi:8 #2（TWF_ORW）
TMOUT = 100                 # 0x12E1CA ldi:8 #100
CLR_BITS = 0x89              # 0x12E196 clr_flg(3, 0x89)
SVC32_RAW = 0xFFFFFF56      # 0x12E1CC ldi:32 原值
SVC32 = -170                # 按 32 位补码
SVC8_FALSE = 86             # 按低 8 位 sx8 的假象

# 四级串联状态机：(call PC, cmp PC, bne PC, bne 目标, 失败分支 bra PC)
LEVELS = (
    (0x12DFBE, 0x12DFC2, 0x12DFC4, 0x12DFD0, 0x12DFCE),
    (0x12DFD0, 0x12DFD2, 0x12DFD4, 0x12DFE0, 0x12DFDE),
    (0x12DFE0, 0x12DFE2, 0x12DFE4, 0x12DFF0, 0x12DFEE),
    (0x12DFF0, 0x12DFF4, 0x12DFF6, 0x12DFFE, 0x12DFFC),
)

KEY_PCS = (
    LOOP_HEAD, 0x12DFBC, 0x12DFBE, 0x12DFC0, 0x12DFC2, 0x12DFC4,
    0x12DFC6, 0x12DFCA, 0x12DFCE, 0x12DFD0, 0x12DFD2, 0x12DFD4,
    0x12DFE0, 0x12DFFE, DISPATCH_FN, BIG_FN, GATE_PC, INDIRECT_CALL,
    SUBMIT, NOOP_HANDLER,
)


# ---------------------------------------------------------------- 位运算
def sx(v, bits):
    """按 ``bits`` 位补码解释一个无符号立即数。"""
    m = 1 << (bits - 1)
    return ((v & ((1 << bits) - 1)) ^ m) - m


def rel9_target(pc, rel9):
    """``bra/bne/beq rel9`` 的目标：8 位有符号、单位 2 字节、基址 pc+2。"""
    return (pc + 2 + sx(rel9, 8) * 2) & 0xFFFFFFFF


def rel12_target(pc, rel12):
    """``call rel12`` 的目标：符号位 bit10（与 ``fr_emu.py`` 一致）。"""
    return (pc + 2 + sx(rel12, 11) * 2) & 0xFFFFFFFF


def mask_check(flgptn, mask=MASK, expect=EXPECT):
    """第二道等待的事后位域判据：``(flgptn & mask) == expect``。"""
    return (flgptn & mask) == expect


def return_value(flgptn, mask=MASK, expect=EXPECT):
    """``0x12E162`` 函数的返回 r4：`beq:d` 延迟槽给 1，落到下一行给 0。"""
    return 1 if mask_check(flgptn, mask, expect) else 0


def branch_taken(r4):
    """``cmp r4,#0`` + ``bne``：非零才进下一级。"""
    return r4 != 0


def cleared_bits(clr_mask, width=8):
    """``clr_flg`` 清的是 mask 里为 0 的位（规矩 20）。"""
    return (~clr_mask) & ((1 << width) - 1)


def mask_covered(clr_mask, mask, width=8):
    """清位范围是否覆盖整个等待位模式。"""
    c = cleared_bits(clr_mask, width)
    return (c & mask) == mask


def svc_width(raw, bits):
    """按立即数宽度解码服务号；``bits=32`` 时不得再取低 8 位（规矩 48）。"""
    return sx(raw, bits)


# ---------------------------------------------------------------- 控制流
def level_targets(levels=LEVELS):
    """四级各层的 bne 目标 PC 列表。"""
    return [lv[3] for lv in levels]


def level_of(pc, levels=LEVELS):
    """某个 cmp/bne PC 属于第几级（1 起，0 表示不在表里）。"""
    for i, lv in enumerate(levels, 1):
        if pc in (lv[1], lv[2]):
            return i
    return 0


def reached_level(cov, levels=LEVELS):
    """覆盖里最高走到了第几级（以 cmp PC 命中为准）。"""
    n = 0
    for i, lv in enumerate(levels, 1):
        if lv[1] in cov:
            n = i
    return n


def advanced(cov, levels=LEVELS):
    """哪些级的 bne 目标被命中（即真正跨过了 cmp 判据）。"""
    return [i for i, lv in enumerate(levels, 1) if lv[3] in cov]


# ---------------------------------------------------------------- 置位源
def static_setters(calls, flgid=FLAG_ID, expect=EXPECT):
    """常量可见、能置上 expect 位的 ``set_flg`` 点（去重、升序）。"""
    return sorted({c.addr for c in calls
                   if c.svc == -48 and c.r4 == flgid
                   and isinstance(c.r5, int) and bool(c.r5 & expect)})


def three_layer(pcs, cov, enclosing=None, isr_range=None):
    """存在性 / 执行性 / 触发性三层判据，不许只报第一层（规矩 50）。"""
    hit = [p for p in pcs if p in cov]
    inside = ([p for p in pcs if isr_range and isr_range[0] <= p < isr_range[1]]
              if isr_range else [])
    return {
        "points": len(pcs), "hit": len(hit),
        "unique_zero_hit": len(pcs) > 0 and len(hit) == 0,
        "in_isr": len(inside), "in_isr_all": len(inside) == len(pcs),
        "enclosing": sorted({enclosing(p) if enclosing else None for p in pcs}),
    }


# ---------------------------------------------------------------- 反事实
def equal_shape(groups):
    """等形校验：预热 / 服务次数 / 中断次数 / 后续步数必须逐项一致。"""
    keys = ("boot", "svc", "irqs", "post")
    out = {}
    for k in keys:
        out[k] = sorted({tuple(g.get(k, [])) for g in groups})
        if len(out[k]) != 1:
            return {"ok": False, "keys": out}
    return {"ok": True, "keys": out}


def window_same(a, b):
    """窗口自证：逐项相同 + PC 覆盖对称差集 0 个。"""
    diff = sorted(k for k in set(a) | set(b)
                  if k not in ("cov", "key") and a.get(k) != b.get(k))
    cov_diff = (a.get("cov", set()) ^ b.get("cov", set()))
    key_diff = set(a.get("key", [])) ^ set(b.get("key", []))
    return {"same": not diff and not cov_diff and not key_diff,
            "diff": diff, "cov_diff": len(cov_diff),
            "key_diff": sorted(key_diff)}


def chain_verdict(level, advanced_levels, gate_hit, indirect_hit,
                  handler_hit, noop_hit, state_zero=True):
    """综合判据：走到派发函数 ≠ 显示链通了。"""
    reached_dispatch = 1 in advanced_levels
    return {
        "level": level, "advanced": advanced_levels,
        "reached_dispatch": reached_dispatch,
        "gate_hit": gate_hit, "indirect_hit": indirect_hit,
        "handler_hit": handler_hit, "noop_hit": noop_hit,
        "state_zero": state_zero,
        "closed": all((gate_hit, indirect_hit, handler_hit,
                       noop_hit, not state_zero)),
    }
