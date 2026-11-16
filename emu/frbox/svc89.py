# -*- coding: utf-8 -*-
"""V89 设施：``TCB+0x28`` 的 −1 哨兵门、−170 段返回码图谱、服务号槽不分流。

承接 KI#347 的两条遗留问号（``r10=−1`` 触发条件、8 对别名的语义分工），
本版只结**结构上可证**的部分：

1. ``0x041C0A cmp2 r10`` 的 r10 不是栈帧参数，而是 ``0x041C08 ld disp10=10``
   从 **CUR_TCB + 0x28**（``0x6800026C``）读来的内核对象指针；基址 r14
   已被 ``0x041C06 ld @0x68000000`` 换过。对照 ``0x041BFE ld disp10=13``
   用的是陷阱栈帧 r14=``0x000083B0`` ⇒ ``0x000083E4``（调用者 r13）。
   **两道 cmp2 比较的是两个不同来源的字**（规矩 83 的归因分离）。
2. ``0x041C0C beq`` 判 ``r10 == −1``；落点 ``0x041CA2`` 是
   ``ld / ret:d / 延迟槽 ldi:8 #239`` ⇒ 返回码 ``0xEF``（extsb 后 **−17**）。
   把 TCB+0x28 写成 ``0xFFFFFFFF``（反事实，只写内存单元）首次下一条 PC
   实测为 ``0x041CA2``；写回原值 ``0x68000A28`` 与只观测机逐项相同 ⇒
   差异来自值，不来自写动作。写 −2 / 0 都不跳（``beq`` 判 Z）。
3. **−170 段有四个不同的负返回码出口**：``−69``（``0x041BE4``，第一道校验）、
   ``−33``（``0x041B88``）、``−17``（``0x041CA2``）、``−85``（``0x041C9C``），
   另有两处 0 与一处无延迟槽返回。``0x041B88`` 有 **3 条静态入边**
   （``0x041BF6 beq`` / ``0x041BFC bne`` / ``0x041C10 blt``），其中两条是
   与 r13 无关的前置校验 ⇒ **不能把 −33 等同于「A≤−2」**（V88 的隐含口径），
   这是边界修正不是推翻（规矩 91）。
4. ``0x040692 stm1``（reglist-hi-st=254）把规整后的 r12（服务号）存进
   ``0x000083E0``，紧邻的 ``0x000083E4`` 存调用者 r13。改 TRAP 处 r12 后
   槽内值确实不同（0x1c/0x38、0x7FFFFF02/0x7FFFFF2A 等），但 6 组别名
   各两台等形机的 **600/1200 步 PC 逐条全序列、末 PC、CUR、READY 全同**
   ⇒ 服务号**被保存**不等于**被读取并分流**（规矩 92）。全 ROM 里
   ``ld disp10=12`` 有 688 条，但只是各自栈帧偏移；8 组别名入口前 0x80
   字节内唯一一条 ``0x041DE0 ld disp10=12`` 在长窗口内 0 命中，
   按 disp10 相同认定「读服务号槽」是假阳性（延续 85 / 88）。

规矩 91（返回码 ≠ 单一条件，先做出入口静态入边普查）、
92（服务号「被保存」≠「被读取并分流」，须槽位映射 + 等形改号 + 窗口声明）
分别延续 78 / 83 / 85 / 86 / 87 / 89 / 90。

**边界**：写 ``0x6800026C``、改 TRAP 处 r12 都是**反事实补全**（只写内存单元
或寄存器），不是固件行为、不是真机器件事件；部分别名组改号后末 PC 落在
非代码区（``0xFFFFFF9E`` / ``0x000018`` / ``0x0083B0``），这些轨迹**只用于
同组对照，不得用来解释真机服务语义**。本版结的是代码结构与返回码图谱，
不是器件事件语义，**链未闭合**。
不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / 所有服务语义已解出 /
真机已复现 −85 / 「补一次值就等于真机事件」/「8 对别名语义已解出」/
「−33 只由 A≤−2 决定」/「8 组别名全程不分流」/「服务号槽没人读」。
"""

M32 = 0xFFFFFFFF

# ---------------------------------------------------------------- 收敛锚点（V65–V88 逐字相同）
BOOT_STEP = 483841
TRAP = 0x040668
HOOK2 = 0x041BFC
HOOK_STEPS = 43
AFTER_LONG = 600
PAIR_WINDOW = 1200
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC

# ---------------------------------------------------------------- r10 门 / 两道 cmp2 的字源
LD_R13 = 0x041BFE                  # ld disp10=13 ri=r13
LD_R13_DISP10 = 13
LD_R13_BASE = 0x000083B0           # 陷阱栈帧 r14（0x041C06 之前）
LD_R13_ADDR = 0x000083E4           # = 0x000083B0 + 13*4
LD_CUR = 0x041C06                  # ld @0x68000000 → r14 = CUR_TCB
LD_CUR_I32 = 1744830464            # 0x68000000
CUR_TCB = 0x68000244
LD_R10 = 0x041C08                  # ld disp10=10 ri=r10
LD_R10_DISP10 = 10
TCB_R10 = 0x6800026C               # = 0x68000244 + 10*4 = CUR_TCB + 0x28
TCB_R10_OFF = 0x28
TCB_R10_ORIG = 0x68000A28
CMP2_R10 = 0x041C0A
BEQ_R10 = 0x041C0C
BEQ_R10_REL9 = 74
BEQ_R10_SX8 = 74
CMP2_R13 = 0x041C0E
BLT_EARLY = 0x041C10
MAIN_CHAIN = 0x041C12
TAIL_A2 = 0x041CA2
RETD_A2 = 0x041CA4
SLOT_A6 = 0x041CA6
I8_239 = 239
RET_M17_U = 0x000000EF
RET_M17_SIGNED = -17

# ---------------------------------------------------------------- stm1 槽位（动态栈区 dump 实测反推）
STM1 = 0x040692
STM0 = 0x040694
STM1_REGLIST = 254                 # reglist-hi-st=0xFE ⇒ r14..r8
STM0_REGLIST = 255                 # reglist-low-st=0xFF ⇒ r7..r0
SLOT_SVC = 0x000083E0              # 规整后 r12（服务号）
SLOT_R13 = 0x000083E4              # 调用者 r13（V88 的 A 单元）
STM1_SLOTS = (
    (14, 0x000083E8), (13, 0x000083E4), (12, 0x000083E0),
    (11, 0x000083DC), (10, 0x000083D8), (9, 0x000083D4),
    (8, 0x000083D0),
)
TRAP_R15 = 0x000083F8
HOOK_R14 = 0x000083B0
HOOK_R15 = 0x000083AC

# ---------------------------------------------------------------- 返回码图谱
# (出口 PC, ret PC, 延迟槽 PC, i8, extsb 返回码, 名称, 延迟槽有无)
EXIT_ROWS = (
    (0x041BDE, 0x041BE0, 0x041BE2, 0,   0,   "RET_0_SHARED", True),
    (0x041BE4, 0x041BE6, 0x041BE8, 187, -69, "RET_M69",      True),
    (0x041B88, 0x041B8A, 0x041B8C, 223, -33, "RET_M33",      True),
    (0x041B8E, 0x041B90, None,     None, None, "RET_PLAIN",   False),
    (0x041C94, 0x041C96, 0x041C98, 0,   0,   "RET_0_MAIN",   True),
    (0x041C9C, 0x041C9E, 0x041CA0, 171, -85, "RET_M85",      True),
    (0x041CA2, 0x041CA4, 0x041CA6, 239, -17, "RET_M17",      True),
)
EXIT_85 = 0x041C9A                 # orccr，−85 出口的入口点
EXIT_0 = 0x041C92                  # orccr，0 出口的入口点
NEG_CODES = (-69, -33, -17, -85)
NEG_CODE_COUNT = 4
EXIT_COUNT = 7
RET0_COUNT = 2

# ---------------------------------------------------------------- 静态入边（−170 段 rel9 落点）
INEDGE_ROWS = (
    (0x041BF2, "bne", 248, -8,  0x041BE4, "RET_M69"),
    (0x041BF6, "beq", 200, -56, 0x041B88, "RET_M33"),
    (0x041BFC, "bne", 197, -59, 0x041B88, "RET_M33"),
    (0x041C0C, "beq", 74,  74,  0x041CA2, "RET_M17"),
    (0x041C10, "blt", 187, -69, 0x041B88, "RET_M33"),
    (0x041C1E, "bne", 183, -73, 0x041B8E, "RET_PLAIN"),
    (0x041C26, "beq", 199, -57, 0x041BB6, "SHARED_B6"),
    (0x041C28, "blt", 174, -82, 0x041B86, "TAIL_30"),
)
INEDGE_COUNT = 8
INEDGE_M33_COUNT = 3               # 0x041B88 的三条入边
TAIL_88 = 0x041B88
TAIL_86 = 0x041B86
TAIL_B6 = 0x041BB6
TAIL_30 = 0x041B30
ENTRY_170 = 0x041BEA
CALL_R12 = 0x0406B2
SVC_RET = 0x0406B4

# ---------------------------------------------------------------- 全 ROM 普查（流式两遍只读）
ROM_DECODED = 3543659
I20_83E0 = 33760                   # 0x83E0
IMM_83E0_COUNT = 3
IMM_83E0_PCS = (0x246E46, 0x25F1CC, 0x25F346)
SUB_ADDR = 0x15FA6C                # 1440812，三处 call:d 的目标
LD_DISP10_12 = 688
ST_DISP10_12 = 459
ALIAS_D12_HITS = 1
ALIAS_D12_PC = 0x041DE0
TRAP_D12_HITS = 0

# ---------------------------------------------------------------- 8 组别名（V88 静态口径）
TBL_ZERO = 0x040660
TABLE_SVC_COUNT = 100
TABLE_ENTRY_COUNT = 89
TABLE_ALIAS_PAIRS = 8
ALIAS_SVC_COUNT = 19
ALIAS_ROWS = (
    (0x040672, (-254, -214), 2, ("0x040682 lsl[ri]", "0x040684 lsr[ri]")),
    (0x040AA0, (28, 56), 0, ()),
    (0x040D6E, (20, 52, 72), 2, ("0x040D82 ld[ri]", "0x040D84 add[ri]")),
    (0x040DE4, (-236, 64), 4,
     ("0x040DE4 ld[rj]", "0x040DE6 ld[ri]",
      "0x040DEA or[rj]", "0x040DEC cmp[ri]")),
    (0x041D6E, (4, 67), 0, ()),
    (0x041DE4, (-247, 22), 0, ()),
    (0x046B80, (114, 125), 1, ("0x046B80 jmp:d[ri]",)),
    (0x046E60, (-230, 74, 93, 104), 0, ()),
)
# 动态等形只做 6 组；另 2 组（0x040DE4 / 0x041DE4）仅静态普查，不据此宣布全局结论
PAIR_ROWS = (
    ("G1", 0x040AA0, (28, 56)),
    ("G2", 0x041D6E, (4, 67)),
    ("G3", 0x040672, (-254, -214)),
    ("G4", 0x046E60, (-230, 74)),
    ("G5", 0x040D6E, (20, 52)),
    ("G6", 0x046B80, (114, 125)),
)
PAIR_STATIC_ONLY = (0x040DE4, 0x041DE4)
PAIR_COUNT = 6
PAIR_MACHINE_COUNT = 12

# ---------------------------------------------------------------- TCB+0x28 反事实值域
# (tag, 写入值, beq 是否跳, 首次下一条 PC, 首次返回码)
TCB_VALS = (
    ("OBS",   None,         False, 0x041C0E, 0x00),
    ("M1",    0xFFFFFFFF,   True,  0x041CA2, 0xEF),
    ("ORIG",  0x68000A28,   False, 0x041C0E, 0x00),
    ("NEG2",  0xFFFFFFFE,   False, 0x041C0E, 0x00),
    ("ZERO",  0x00000000,   False, 0x041C0E, 0x00),
)
TCB_VAL_COUNT = 5
TCB_VAL_TAKEN_COUNT = 1
MACHINE_TOTAL = 18                 # 5 台 r10 门 + 12 台别名等形 + 1 台标准收敛态

VERDICT = "tcb_plus28_minus1_gate_ret_m17_four_negative_codes_svc_slot_saved_no_dispatch"
VERDICT_OPEN = "open"


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """按 ``bits`` 位补码解释无符号位串。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def sx8(v):
    return sx(v, 8)


def extsb8(i8):
    return sx(i8 & 0xFF, 8)


def extsh(h):
    return sx(h & 0xFFFF, 16)


def rel9_target(pc, rel9):
    """``pc+2+sx8(rel9)*2``（规矩 70 / 85）。"""
    return (pc + 2 + sx8(rel9) * 2) & M32


def disp10_addr(r14, disp10):
    """``ld/st disp10`` 的有效地址：``r14 + sx8(disp10)*4``（fr_emu.py 口径）。"""
    return (r14 + sx8(disp10) * 4) & M32


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


def sub_flags(a, b, bin_=0):
    """复刻 ``fr_emu._sub``：返回 ``(r, c, v, n, z)``；FR 的 C 是借位。"""
    a &= M32
    b &= M32
    s = a + (b ^ M32) + (1 - bin_)
    r = s & M32
    c = 0 if s > M32 else 1
    v = 1 if ((a ^ b) & (a ^ r) & 0x80000000) else 0
    n = 1 if (r & 0x80000000) else 0
    z = 1 if r == 0 else 0
    return r, c, v, n, z


def cmp2_flags(a, m4=15):
    return sub_flags(a, ((m4 & 0xF) - 16) & M32)


def beq_after_cmp2(a, m4=15):
    """``beq`` 判 Z：只当 ``ri == (m4-16)`` 时结果 0。"""
    _r, _c, _v, _n, z = cmp2_flags(a, m4)
    return bool(z)


def ret_code(i8):
    """延迟槽 ``ldi:8`` 的返回码（extsb）。"""
    return None if i8 is None else extsb8(i8)


def in_edge_verdict(target, rows=INEDGE_ROWS):
    """规矩 91：出口的静态入边数；>1 不得与单一条件一一对应。"""
    n = sum(1 for r in rows if r[4] == target)
    if n == 0:
        return "no_static_in_edge"
    if n == 1:
        return "single_in_edge"
    return "multiple_in_edges_not_one_condition"


def in_edge_count(target, rows=INEDGE_ROWS):
    return sum(1 for r in rows if r[4] == target)


def word_source_verdict(ld_r13_addr, ld_r10_addr):
    """两道 cmp2 比较的是两个不同来源的字。"""
    if ld_r13_addr == SLOT_R13 and ld_r10_addr == TCB_R10:
        return "stack_frame_vs_tcb_field"
    return "unresolved"


def disp10_alias_verdict(disp10, r14):
    """``disp10=12`` 是否指向服务号槽，须按当时 r14 实测。"""
    return "reads_svc_slot" if disp10_addr(r14, disp10) == SLOT_SVC \
        else "other_frame_offset"


def svc_saved_vs_dispatch(slot_a, slot_b, seq_same, window):
    """规矩 92：服务号被保存 ≠ 被读取并分流。"""
    if slot_a == slot_b:
        return "service_number_not_changed"
    if seq_same and window > 0:
        return "slot_differs_sequence_identical_no_dispatch_in_window"
    return "unresolved"


def equal_shape(rows):
    """等形三项 boot / trap / post 全同。"""
    if not rows:
        return False
    return all(len(set(r.get(k) for r in rows)) == 1
               for k in ("boot", "trap", "post"))


def first_taken(pc_hit, target_hit):
    """规矩 89：判跳用首次下一条 PC，不用累计命中集合。"""
    if pc_hit and target_hit:
        return "taken"
    if pc_hit and not target_hit:
        return "executed_not_taken"
    return "not_executed"


def patch_causality(only_observe, write_back, minus_one):
    """写回原值与只观测机同 ⇒ 差异来自值，不来自写动作。"""
    if only_observe == write_back and minus_one != only_observe:
        return "value_not_write_action"
    return "unresolved"


def source_verdict(static_ok, gate_ok, exits_ok, pair_ok, event_located=False):
    """综合判据：链未闭合（真机器件事件语义仍未定位）。"""
    if not (static_ok and gate_ok and exits_ok and pair_ok):
        return VERDICT_OPEN
    if event_located:
        return "event_semantics_located"
    return VERDICT
