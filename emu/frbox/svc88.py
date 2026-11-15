# -*- coding: utf-8 -*-
"""V88 设施：``cmp2`` 是 −1 哨兵比较，不是「两个寄存器比较」。

承接 KI#343 的两条遗留问号（``cmp2`` 真实比较语义、8 对服务号别名的语义分工），
本版只结**结构上可证**的部分：

1. ``cmp2`` 在 ``fr_emu.py`` 的实现是 ``_sub(ri, (m4-16)&2^32)``；
   ``m4=15`` ⇒ 比较基准 **−1**，即「ri 与 −1 比较」，不是两个寄存器比较。
   ``0x041C0C beq`` 判 ``r10 == -1``；``0x041C10 blt`` 判 ``r13 < -1``。
   而 ``0x041C24 cmp #0,r13`` 基准是 **0**，``0x041C28 blt`` 判 ``r13 < 0``。
   同一 r13、两条同名 ``blt``、比较基准差 1 ⇒ V87 七组「前一条全不跳、
   后一条三分」不是「执行≠跳转」的玄学，是比较基准不同（规矩 88）。
2. 把 A 值域从 V87 的 ``{-1, 0, 1}`` 补到 ``{-3, -2, -1, 0, 1, 2, 0x80000000}``：
   ``A <= -2`` 时 ``0x041C10 blt`` **真的跳**，落点 ``0x041B88`` 是
   ``ld / ret:d / ldi:8 #223`` 的提前返回出口，返回码 ``0xDF``（extsb 后 −33）。
   ⇒ V87 的「符号三分」只在 ``A >= -1`` 子域成立；完整值域是四分。
3. 长窗口里同一服务可被**再次进入**：早补的内存单元会被后续 ``stm1``
   栈帧保存覆盖，第二次进入时 r13 已变回 ``0xFFFFFFFF``。因此
   「600 步累计命中 TAIL_88 与 TAIL_86」不等于「同一次走了两个落点」，
   必须用**首次触发**的下一条 PC 判跳（规矩 89）。
4. 分派区 ``0x0406B2 call @r12`` 处的 r12 是**入口地址**
   （``0x040660 + extsh(half)``，由表项 half 决定，与服务号无关），不是服务号。
   8 组别名里同 half 的两个（或多个）服务号 r12 完全相同 ⇒
   **入口层不可能按服务号分流**；4 组入口对 r12 的引用是在用入口地址或
   通用寄存器，不是服务号判别。语义分工（若有）只能来自调用者上下文，仍开放
   （规矩 90）。

规矩 88（比较基准不得按助记符猜）、89（首次触发 vs 窗口累计、早补被覆盖）、
90（r12 是入口地址不是服务号）分别延续 81 / 82 / 87 / 78 / 86。

**边界**：写 ``0x000083E4`` 是反事实补全（只写内存单元），不是固件行为、
不是真机器件事件；服务返回、任务换手是既有代码自然执行。本版结的是
指令语义与代码结构，不是器件事件语义，**链未闭合**。
不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / 所有服务语义已解出 /
真机已复现 −85 / 「补一次值就等于真机事件」/「8 对别名语义已解出」。
"""

M32 = 0xFFFFFFFF

# ---------------------------------------------------------------- 收敛锚点
BOOT_STEP = 483841
TRAP = 0x040668
HOOK2 = 0x041BFC
HOOK_STEPS = 43
AFTER_LONG = 600
MAIN_SVC_IMM32 = 0xFFFFFF56
MAIN_SVC_32 = -170
SRC_A = 0x000083E4
SRC_B = 0x68000970
A_ORIG = 0xFFFFFFFF
B_ORIG = 0x80000000
END_CUR = 0x6800029C
END_READY = 0x036E0000

TBL_ZERO = 0x040660
TABLE_SVC_COUNT = 100
TABLE_ENTRY_COUNT = 89
TABLE_ALIAS_PAIRS = 8
ALIAS_SVC_COUNT = 19
ALIAS_WITH_R12 = 4
ALIAS_WITHOUT_R12 = 4

# ---------------------------------------------------------------- cmp2 语义
CMP2_M4 = 15
CMP2_BASIS = -1                     # m4 - 16
CMP2_IMM32 = 0xFFFFFFFF             # (m4-16) & 2^32
CMP0_BASIS = 0
SUB_BIN = 0                         # _sub 的 bin_ 默认 0

CMP2_R10 = 0x041C0A
BEQ_R10 = 0x041C0C
CMP2_R13 = 0x041C0E
BLT_EARLY = 0x041C10
CMP0_R13 = 0x041C24
BEQ_A0 = 0x041C26
BLT_LATE = 0x041C28
FALL_POS = 0x041C2A
MAIN_CHAIN = 0x041C12               # blt 不跳时的下一条
LD_R13 = 0x041BFE
LD_R10 = 0x041C08
ANDCCR_170 = 0x041C22

# 提前返回尾 0x041B88：ld / ret:d / 延迟槽 ldi:8 #223
TAIL_88 = 0x041B88
RETD_88 = 0x041B8A
SLOT_88 = 0x041B8C
SLOT_88_I8 = 223                    # 0xDF
RET_M33_U = 0x000000DF
RET_M33_SIGNED = -33
TAIL_86 = 0x041B86
TAIL_30 = 0x041B30
TAIL_B6 = 0x041BB6
TAIL_A2 = 0x041CA2
TAIL_8E = 0x041B8E
EXIT_85 = 0x041C9A
INEDGE_88 = 7                       # V87 实测全 ROM 静态入边
BLT_EARLY_REL9 = 187
BLT_EARLY_SX8 = -69
BEQ_R10_REL9 = 74
BEQ_R10_SX8 = 74

# ---------------------------------------------------------------- 全 ROM 模板普查
ROM_DECODED = 3543659
CMP2_TOTAL = 2665
M4_15_COUNT = 247
M4_0_COUNT = 1447
M4_15_BEQ_BLT = 82
FOLLOW_BEQ = 75
FOLLOW_BLT = 48
FOLLOW_BEQD = 28
FOLLOW_BLTD = 2
FOLLOW_NONE = 52
RI_SPAN = 16                        # ri 覆盖 r0..r15
M4_KINDS = 16                       # m4 取值 0..15

# 段内与别名入口的 7 条 m4=15 模板实例：(pc, m4, ri, 后继, rel9, 落点)
TEMPLATE_ROWS = (
    (0x040D6E, 15, 11, "beq",   30,  0x040DAE),
    (0x041B66, 15, 14, "beq:d", 10,  0x041B7E),
    (0x041C0A, 15, 10, "beq",   74,  0x041CA2),
    (0x041C0E, 15, 13, "blt",   187, 0x041B88),
    (0x041D6A, 15, 14, "beq:d",  9,  0x041D80),
    (0x041DEC, 15, 10, "beq",   240, 0x041DD0),
    (0x041DF0, 15,  6, "blt",   235, 0x041DCA),
)

# ---------------------------------------------------------------- 分派区 r12
TRAP_R12 = 0xFFFFFF56               # −170 的 32 位有符号原值
LSL_R12 = 0x0406A4
LSL_AFTER = 0xFFFFFEAC              # (svc << 1)
LDI_TBL = 0x0406A6
LDI_TBL_IMM = 263776                # 0x40660
LDUH_HALF = 0x0406AC
EXTSH_HALF = 0x0406AE
ADD_R13 = 0x0406B0
CALL_R12 = 0x0406B2
SVC_RET = 0x0406B4
EXTSB_R12 = 0x0406BE
STM1 = 0x040692
HALF_170 = 0x158A
CALL_R12_VALUE = 0x00041BEA        # 动态实测：调用时 r12 = 入口地址
ENTRY_170 = 0x041BEA
SLOT_170 = 0x04050C

# ---------------------------------------------------------------- 8 组别名入口 r12 引用普查
# (入口, 服务号元组, 前 24 字节内 r12 引用条数, 引用指令)
ALIAS_ROWS = (
    (0x040672, (-254, -214), 2, ("0x040682 lsl[ri]", "0x040684 lsr[ri]")),
    (0x040AA0, (28, 56), 0, ()),
    (0x040D6E, (20, 52, 72), 2,
     ("0x040D82 ld[ri]", "0x040D84 add[ri]")),
    (0x040DE4, (-236, 64), 4,
     ("0x040DE4 ld[rj]", "0x040DE6 ld[ri]",
      "0x040DEA or[rj]", "0x040DEC cmp[ri]")),
    (0x041D6E, (4, 67), 0, ()),
    (0x041DE4, (-247, 22), 0, ()),
    (0x046B80, (114, 125), 1, ("0x046B80 jmp:d[ri]",)),
    (0x046E60, (-230, 74, 93, 104), 0, ()),
)

# ---------------------------------------------------------------- A 值域 7 组
# (tag, A, 首次 blt lt, 首次落点, 首次汇入, cmp2 次数, 末 PC, 累计命中, r13 序列, 返回码序列)
AVALS = (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0x80000000,
         0x00000000, 0x00000001, 0x00000002)
GROUPS = (
    ("NEG1", 0xFFFFFFFF,  False, "MAIN_CHAIN",    "TAIL_86",   1, 0x137968,
     ("TAIL_86",), (0xFFFFFFFF,), (0x0, 0x0, 0x0)),
    ("NEG2", 0xFFFFFFFE,  True,  "EARLY_RET_M33", "TAIL_88",   2, 0x137968,
     ("TAIL_88", "TAIL_86"), (0xFFFFFFFE, 0xFFFFFFFF),
     (0xDF, 0x0, 0x0, 0x0)),
    ("NEG3", 0xFFFFFFFD,  True,  "EARLY_RET_M33", "TAIL_88",   2, 0x137968,
     ("TAIL_88", "TAIL_86"), (0xFFFFFFFD, 0xFFFFFFFF),
     (0xDF, 0x0, 0x0, 0x0)),
    ("MIN",  0x80000000,  True,  "EARLY_RET_M33", "TAIL_88",   2, 0x137968,
     ("TAIL_88", "TAIL_86"), (0x80000000, 0xFFFFFFFF),
     (0xDF, 0x0, 0x0, 0x0)),
    ("ZERO", 0x00000000,  False, "MAIN_CHAIN",    "TAIL_B6",   1, 0x13796E,
     ("TAIL_B6",), (0x0,), (0x0, 0x0, 0x0)),
    ("POS1", 0x00000001,  False, "MAIN_CHAIN",    "FALL_POS",  1, 0x137972,
     ("FALL_POS",), (0x1,), (0x0, 0x0, 0x0)),
    ("POS2", 0x00000002,  False, "MAIN_CHAIN",    "FALL_POS",  1, 0x137972,
     ("FALL_POS",), (0x2,), (0x0, 0x0, 0x0)),
)
MACHINE_COUNT = 7                   # F 段等形反事实机
MACHINE_TOTAL = 10                  # 1 只观测 + 1 分派 r12 + 1 收敛态 + 7 等形

# 只观测机 CCR 快照（执行该 PC 指令**前**的 N/Z/V/C，来自上一条指令）
CCR_ROWS = (
    ("CMP2_R10",  0x041C0A, 0x68000A28, 0xFFFFFFFF, 0, 1, 0, 0),
    ("BEQ_R10",   0x041C0C, 0x68000A28, 0xFFFFFFFF, 0, 0, 0, 1),
    ("CMP2_R13",  0x041C0E, 0x68000A28, 0xFFFFFFFF, 0, 0, 0, 1),
    ("BLT_EARLY", 0x041C10, 0x68000A28, 0xFFFFFFFF, 0, 1, 0, 0),
    ("CMP0_R13",  0x041C24, 0x68000A28, 0xFFFFFFFF, 0, 1, 0, 0),
    ("BEQ_A0",    0x041C26, 0x68000A28, 0xFFFFFFFF, 1, 0, 0, 0),
    ("BLT_LATE",  0x041C28, 0x68000A28, 0xFFFFFFFF, 1, 0, 0, 0),
)

# 标准收敛态（V65–V87 逐字相同）
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC
COV = {
    "CMP2_R10": True, "CMP2_R13": True, "BLT_EARLY": True,
    "CMP0_R13": True, "BEQ_A0": True, "BLT_LATE": True,
    "TAIL_88": False, "TAIL_86": True, "TAIL_30": True,
    "EXIT_85": True, "MAIN_JMP": False, "WB_FN": False,
}
COV_CODES = {
    "CMP2_R10": CMP2_R10, "CMP2_R13": CMP2_R13, "BLT_EARLY": BLT_EARLY,
    "CMP0_R13": CMP0_R13, "BEQ_A0": BEQ_A0, "BLT_LATE": BLT_LATE,
    "TAIL_88": TAIL_88, "TAIL_86": TAIL_86, "TAIL_30": TAIL_30,
    "EXIT_85": EXIT_85, "MAIN_JMP": 0x175282, "WB_FN": 0x175908,
}

VERDICT = "cmp2_minus_one_sentinel_early_ret_m33_alias_entry_no_svc_dispatch"
VERDICT_OPEN = "open"


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """按 ``bits`` 位补码解释无符号位串。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def sx8(v):
    return sx(v, 8)


def extsh(h):
    return sx(h & 0xFFFF, 16)


def rel9_target(pc, rel9):
    """``pc+2+sx8(rel9)*2``（规矩 70 / 85）。"""
    return (pc + 2 + sx8(rel9) * 2) & M32


def slot_of(svc32):
    """真内核 32 位有符号服务号表项槽（规矩 76）。"""
    return (TBL_ZERO + ((svc32 << 1) & M32)) & M32


def target_of(half):
    return (TBL_ZERO + extsh(half)) & M32


def alias_pairs(svc_to_entry):
    """多号对一入口 → ``[(entry, (svc...))]``，按入口升序。"""
    acc = {}
    for svc, entry in svc_to_entry.items():
        acc.setdefault(entry, []).append(svc)
    return sorted((e, tuple(sorted(s))) for e, s in acc.items() if len(s) > 1)


def m4_basis(m4):
    """``cmp2`` 的真实比较基准（有符号）：``m4 - 16``。"""
    return (m4 & 0xF) - 16


def m4_imm32(m4):
    """``_sub`` 收到的 32 位无符号减数。"""
    return (m4_basis(m4) & M32)


def sub_flags(a, b, bin_=0):
    """复刻 ``fr_emu._sub``：返回 ``(r, c, v, n, z)``。

    FR 的 C 是「借位」（a<b 时为 1）；V = (a^b)&(a^r)&符号位。
    """
    a &= M32
    b &= M32
    s = a + (b ^ M32) + (1 - bin_)
    r = s & M32
    c = 0 if s > M32 else 1
    v = 1 if ((a ^ b) & (a ^ r) & 0x80000000) else 0
    n = 1 if (r & 0x80000000) else 0
    z = 1 if r == 0 else 0
    return r, c, v, n, z


def lt_from_ccr(n, z, v, c=0):
    """``blt`` 的判据 ``bool(v ^ n)``（``fr_emu.py`` 条件分支表）。"""
    return bool(v ^ n)


def eq_from_ccr(n, z, v, c=0):
    """``beq`` 的判据 ``bool(z)``。"""
    return bool(z)


def cmp2_flags(a, m4=CMP2_M4):
    return sub_flags(a, m4_imm32(m4))


def cmp0_flags(a):
    return sub_flags(a, CMP0_BASIS & M32)


def branch_after_cmp2(a, m4, kind):
    """``cmp2`` 后紧跟的 ``beq`` / ``blt`` 是否跳转。"""
    r, c, v, n, z = cmp2_flags(a, m4)
    return eq_from_ccr(n, z, v, c) if kind == "beq" else lt_from_ccr(n, z, v, c)


def branch_after_cmp0(a, kind):
    r, c, v, n, z = cmp0_flags(a)
    return eq_from_ccr(n, z, v, c) if kind == "beq" else lt_from_ccr(n, z, v, c)


def basis_verdict(a, m4=CMP2_M4):
    """同一 a 在基准 −1 与基准 0 下两条 ``blt`` 的对照判据。"""
    early = branch_after_cmp2(a, m4, "blt")
    late = branch_after_cmp0(a, "blt")
    if early and not late:
        return "basis_minus_one_only"
    if (not early) and late:
        return "basis_zero_only"          # a == -1：V87 七组所见
    if early and late:
        return "both_basis"
    return "neither_basis"


def sign_class(a):
    s = sx(a & M32, 32)
    return "negative" if s < 0 else ("zero" if s == 0 else "positive")


def tri_dispatch(a):
    """V87 口径：符号三分（只在 ``a >= -1`` 子域成立）。"""
    return {"negative": "TAIL_86", "zero": "TAIL_B6",
            "positive": "FALL_POS"}[sign_class(a)]


def quad_dispatch(a):
    """V88 完整值域四分：``a <= -2`` 提前返回 −33，其余按 V87 三分。"""
    if sx(a & M32, 32) <= -2:
        return "EARLY_RET_M33"
    return tri_dispatch(a)


def first_landing(a):
    """首次触发的落点名（用下一条 PC 实测判跳，不用累计命中集合）。"""
    return "EARLY_RET_M33" if branch_after_cmp2(a, CMP2_M4, "blt") \
        else "MAIN_CHAIN"


def gate_verdict(*hits):
    """互斥性：恰好一个落点被命中才成立。"""
    n = sum(1 for x in hits if x)
    return "exactly_one" if n == 1 else ("none" if n == 0 else "ambiguous")


def equal_shape(rows):
    """等形三项 boot / hook / post 全同。"""
    if not rows:
        return False
    return all(len(set(r.get(k) for r in rows)) == 1
               for k in ("boot", "hook", "post"))


def executed_vs_taken(pc_hit, target_hit):
    """规矩 87：执行到分支指令 ≠ 该分支跳转发生。"""
    if pc_hit and target_hit:
        return "taken"
    if pc_hit and not target_hit:
        return "executed_not_taken"
    return "not_executed"


def window_verdict(first, cumulative, entry_count):
    """规矩 89：分清首次触发与窗口累计命中。"""
    if entry_count > 1 and len(cumulative) > 1:
        return "reentry_overlap_not_two_paths"
    if first in cumulative or len(cumulative) == 1:
        return "single_entry_consistent"
    return "unresolved"


def overwrite_verdict(r13_seq, stm1_seq):
    """早补的内存单元被后续 ``stm1`` 栈帧保存覆盖。"""
    if len(r13_seq) > 1 and r13_seq[0] != r13_seq[-1] and len(stm1_seq) > 1:
        return "stm1_overwrote_patch"
    return "patch_persists"


def template_verdict(m4, follow):
    """``cmp2`` 模板判据：m4=15 且紧随 beq/blt（含 :d）⇒ −1 哨兵检查。"""
    if m4_basis(m4) != -1:
        return "other_basis"
    if follow in ("beq", "blt", "beq:d", "blt:d", "bge", "bgt", "ble"):
        return "minus_one_sentinel"
    return "minus_one_nobranch"


def dispatch_r12_verdict(half_a, half_b, svc_a, svc_b):
    """规矩 90：同 half ⇒ 同入口地址 ⇒ r12 相同 ⇒ 入口层不能按号分流。"""
    if svc_a == svc_b:
        return "same_service"
    if half_a == half_b:
        return "same_entry_r12_no_svc_dispatch"
    return "different_entry"


def alias_ref_verdict(ref_count, r12_is_entry_addr=True):
    """入口层引用 r12 ≠ 按服务号分流（r12 是入口地址）。"""
    if not r12_is_entry_addr:
        return "unresolved"
    return "r12_used_as_address" if ref_count > 0 else "r12_absent_no_split"


def cov_verdict(cov):
    """判定尾跑过、命令分发链仍未走到。"""
    shared = all(cov.get(k, False) for k in
                 ("CMP2_R13", "BLT_EARLY", "CMP0_R13", "TAIL_86", "TAIL_30"))
    cmd = cov.get("MAIN_JMP", True) or cov.get("WB_FN", True)
    return "shared_tail_seen_cmd_chain_unreached" if shared and not cmd else "open"


def source_verdict(template_ok, ccr_ok, quad_ok, window_ok, r12_ok,
                   event_located=False):
    """综合判据：链未闭合（真机器件事件语义仍未定位）。"""
    if not (template_ok and ccr_ok and quad_ok and window_ok and r12_ok):
        return VERDICT_OPEN
    if event_located:
        return "event_semantics_located"
    return VERDICT
