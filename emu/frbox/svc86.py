# -*- coding: utf-8 -*-
"""frbox.svc86 —— 没有「优先级」，是返回码层与调度层（V86，纯函数）。

承接 KNOWN_ISSUES KI#335 第一条：V85 交付时留了一句「A 的状态如何决定
『返 −85』与「挂起换手」两条大分支的优先级」。V86 把这句证伪并结案：
**这两件事不是两条互斥大分支，也不存在优先级** —— 它们是同一执行流上
两个不同观察层的事件，长窗口下四种 A/B 组合**全部**发生同一次任务换手。

2x2 真值表（600 步长窗口，5 台独立等形机）
--------------------------------------------
全部在 HOOK2=0x041BFC 晚补（此时 step20 的 ``stm1`` 已写过 A），
只往内存单元写 4 字节，不改寄存器、不强制 PC、不注入中断：

=================  ===========  ===========  =======  ========  ==========
组                  A 读值        B 读值       返 −85   调度 reti  返回点 r12
=================  ===========  ===========  =======  ========  ==========
SAME_AB             FFFFFFFF     80000000     False    401       0x00000000
A0_BSET             00000000     80000000     False    398       0x00000000
AOR_BCLR            FFFFFFFF     00000000     False    198       0x00000000
A0_BCLR             00000000     00000000     **True** 314       **0x000000AB**
=================  ===========  ===========  =======  ========  ==========

四组末 CUR 同为 0x6800029C（任务 9）、READY 同为 0x036E0000；
``SAME_AB`` 与 ``A0_BCLR`` 两次 reti 之后 **80 步 PC 序列逐项相同**。
``NO_PATCH`` ≡ ``SAME_AB``（写原值无效，差异来自值不是写动作）；
等形三项 boot / hook / post 全同。⇒ −85 是 ``A=0 ∧ B=0`` 的合取叠加在
同一执行流上的一条**返回码分支**，换手是四组共有的**调度层结果**。

严格单调的事件顺序（A0_BCLR，600 步，从 HOOK2 起算）
-----------------------------------------------------
``0x041C24 cmp #0,r13``(28) → ``0x041C26 beq``(29) → ``0x041BB6``(30) →
``0x041BB8``(31) → ``0x041BCC beq``(35) → ``0x041C9A``(−85 出口, 36) →
``0x041CA0`` 延迟槽(39) → ``0x0406B4`` 服务返回点 r12=0xAB(40) →
``0x0406BE extsb``(44) → ``0x0406E2`` 陷阱 reti(61) →
``0x175282 MAIN_JMP``(68) → ``0x17573C / 0x175740 / 0x17574C /
0x175908`` 命令分发链(69/71/73/74) → 共享尾 ``0x041B32``(149) →
``0x041B48 bne``(154) → 落 ``0x041B4A``(155) → 派发器 ``0x040D32``(236) →
调度 ``0x040EB6 reti``(314) → 任务 9 入口 ``0x1352EA``(315) →
``0x1378FA`` 表源地址常量(325)。全部 22 项 step 单调递增。

A 不是状态字，是调用者 r13 的栈帧保存槽
----------------------------------------
``0x041BFE ld disp10=13,r13`` 在 ``r14=0x000083B0`` 时读
**A = 0x000083E4**。1 台只观测机实测：陷阱口 ``r13=0xFFFFFFFF``、
``r14=0x9F805ADC``、``r15=0x000083F8``；``0x040692 stm1`` 在
step 索引 20（执行 pc=0x040694）把 A 写成 **0xFFFFFFFF**，与陷阱口 r13
逐位相等。A 在锚点前是 0x260，标准收敛态末值是 0x9F58C4D0
（V84 已证高频复用槽，规矩 80）。所以「A 的状态」其实是
**调用者经陷阱栈帧传进来的 r13 参数**，``0xFFFFFFFF`` 是 −1 哨兵值。

两道门是跨函数串联，不是同一服务内的并列条件
------------------------------------------------
``rel9`` 是 8 位有符号（规矩 70：``pc+2+sx8(rel9)*2``）。
``0x041C26 beq rel9=199(0xC7)`` 的 sx8 是 **−57**，落点
**0x041BB6** —— 而这一段是 V83 已归属的 **−106 号服务**函数体
（``0x041B92`` 起），−85 出口的两条可定址入边 ``0x041BC6 bne rel9=105`` /
``0x041BCC beq rel9=102`` 双双落到 ``0x041C9A``，都在这一侧。
B 的另一处读点 ``0x041BB8 ld disp10=2,r8`` 也在这一段。
⇒ 正确结构是「**先由 A 选服务段，再由 B 定返回码**」的跨函数串联；
V84 把 ``0x041BFE`` 读 A 与 ``0x041BB8`` 读 B 当成同一服务里两道
并列内部条件，是隐含假定，本版修正（延续规矩 77 / 79）。

规矩 82 的第二次实例：260 步又把时机差读成了路径差
----------------------------------------------------
2 台 260 步短窗口机：``SAME_AB_260`` 与 ``A0_BCLR_260`` **都没有**
调度 reti；后者链命中、返回码 0xAB、末 PC 0x0404E8。
同样两组拉到 600 步：两组**都**有调度 reti（401 / 314），
换手后都到任务 9。V84 记的「LATE_AB 返 −85 且链全命中，未换手」
是 260 步截断读法，不是「不换手」。
**窗口起算点必须声明**：V84 的 260 步从陷阱入口 ``0x040668`` 起算，
V86 的 260 / 600 步从 HOOK2 ``0x041BFC`` 之后起算（先单步 43 步到挂钩），
两种口径的去重 PC 数、末 PC 不可直接对比。

结果层同 ≠ 原因层同（规矩 83）
--------------------------------
四组 TCB7 ``+0x20`` 高半服务号：``SAME_AB`` / ``A0_BSET`` = −37
（``+0x14=0x0000000A`` 剩余 tick）、``AOR_BCLR`` = −170
（``+0x14=0xA2A05E1C`` 等待位模式）、``A0_BCLR`` = −46
（``+0x14=0x00000001``）；600 步 int 次数 2 / 2 / 0 / 1。
所以只能报「换手结果和换手后轨迹相同」，不能报「同一等待事件」。

收敛态 coverage 口径分层
-------------------------
标准收敛态（V65–V85 逐字相同）全窗口对 ``0x041BB6 / 0x041BB8 /
0x041B32 / 0x041B48 / 0x041C26 / 0x041C9A / 0x041CA0 / 0x040EB6 /
0x1352EA`` **九个 PC 全命中**（−85 出口这段代码在别的调用点跑过），
但 ``0x175282 / 0x175908`` **未命中**（命令分发链在收敛态仍未走到）。
这与 V81–V83「单次 −170 调用短窗口里 −85 出口覆盖全 0」不冲突：
两者是**不同窗口口径**，不许合并（延续规矩 78 / 82）。

硬规矩 84（本版新增）
---------------------
报「两个条件决定两条互斥分支 / 谈优先级」前先分清**返回码层**与
**调度层**：−85 是服务出口的返回码，任务换手是后续调度结果；
必须先证所有组合在足够长窗口下是否都换手。不许把「某窗口内未观察到
换手」写成「不换手」。等形对照必须声明**窗口起算点**（陷阱口起算还是
挂钩后起算），不同起算点的去重 PC 数 / 末 PC / 步数不可直接比较。

硬规矩 85（本版新增）
---------------------
报「同一函数内两道条件」前先用 **sx8** 算 ``rel9`` 落点并核归属：
``beq rel9=199`` 的 sx8 是 −57，会把执行流从 ``0x041C26`` 带到
``0x041BB6``（−106 号服务段），B 的读点与 −85 出口入边都在那一侧。
不许按地址就近把跨段跳转读成「同一服务的两道门」（延续规矩 77 / 79）；
不许把栈帧保存槽（= 调用者 r13 参数，−1 哨兵）叫成「状态字」
（延续规矩 80）。

反事实边界与共同禁语
---------------------
所有往 ``0x000083E4`` / ``0x68000970`` 写值都是**反事实补全**，
不是固件行为、不是真机器件事件；任务换手是仿真台既有代码的自然执行。
链仍未闭合。不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
「所有服务语义已解出」/「真机已复现 −85」/「−85 出口属于 −170 号函数体」/
「写一次内存单元就等于器件事件」/「A 是器件状态字」/
「清 B 位唤醒了任务 9」/「EARLY_B 走了一条新路径」/
「A=0 与换手是两条互斥分支」/「补一次 −85 就等于真机事件」。

仍开放：真机让 ``vec 0x18`` 周期性发生的器件/事件（KI#191）、
让 −170 号服务在真机走 −85 出口的器件事件语义、两状态字偏移 0 bit3
置位源、``vec 0x2D`` 硬件事件、317 个提交调用点非根部分的触发任务、
任务 9 表下载完成后是否回到任务 7；本版新增：``0xFFFFFFFF`` 这个
调用者 r13 哨兵在真机里由谁设置、−170 号服务为何跳入 −106 号服务段
（服务复用还是共享尾）。

本版登记 **KI#336–339**：336 结 KI#335 第一条（无优先级，两层事件）、
337 跨段 rel9 与 A 的值语义、338 规矩 84 / 85 与工具口径、
339 仍开放清单。链未闭合。
"""

# ---------------------------------------------------------------- 静态口径
BOOT_STEP = 483841
TRAP = 0x040668
HOOK2 = 0x041BFC
HOOK_STEPS = 43
AFTER_LONG = 600
AFTER_SHORT = 260
ALIGN = 80
MAIN_SVC_IMM32 = 0xFFFFFF56
MAIN_SVC_32 = -170

# ---------------------------------------------------------------- 两单元与取值
SRC_A = 0x000083E4
SRC_B = 0x68000970
A_ORIG = 0xFFFFFFFF
A_ZERO = 0x00000000
B_SET = 0x80000000
B_CLR = 0x00000000
RET_85 = 0x000000AB
CMD_CODE = -85

# ---------------------------------------------------------------- A 判据 / 跨段
CMP2_R13 = 0x041C0E
BLT_EARLY = 0x041C10
CMP0_R13 = 0x041C24
BEQ_A_ZERO = 0x041C26
BLT_LATE = 0x041C28
BEQ_REL9 = 199
BEQ_SX8 = -57
SEG106_ENTRY = 0x041BB6
SEG106_LDB = 0x041BB8
BNE_106 = 0x041BC6
BEQ_106 = 0x041BCC
BNE_106_REL9 = 105
BEQ_106_REL9 = 102
EXIT_85 = 0x041C9A
RET_D_85 = 0x041C9E
SLOT_85 = 0x041CA0
A_WRITER = 0x040692
A_WRITER_MN = "stm1"
A_WRITER_STEP_IDX = 20
A_WRITER_EXEC_PC = 0x040694
R13_ENTRY = 0xFFFFFFFF
R14_ENTRY = 0x9F805ADC
R15_ENTRY = 0x000083F8
R14_AT_LD13 = 0x000083B0
LD_R13_DISP = 13

REL9_ROWS = (
    (0x041C26, 199, -57, 0x041BB6),
    (0x041C10, 187, -69, 0x041B88),
    (0x041C28, 174, -82, 0x041B86),
    (0x041BC6, 105, 105, 0x041C9A),
    (0x041BCC, 102, 102, 0x041C9A),
    (0x041C0C, 74, 74, 0x041CA2),
    (0x041BF2, 248, -8, 0x041BE4),
    (0x041BF6, 200, -56, 0x041B88),
    (0x041BFC, 197, -59, 0x041B88),
    (0x041C1E, 183, -73, 0x041B8E),
    (0x041B48, 20, 20, 0x041B72),
)

# ---------------------------------------------------------------- 两层事件
RET_PC = 0x0406B4
EXTSB_PC = 0x0406BE
TRAP_RETI = 0x0406E2
SCHED_RETI = 0x040EB6
DISPATCH = 0x040D32
TASK9_ENTRY = 0x1352EA
CONST_LDI = 0x1378FA
LD_B_GATE = 0x041B32
BNE_GATE = 0x041B48
TAKE_FALL = 0x041B4A
TAKE_JUMP = 0x041B72
MAIN_JMP = 0x175282
CMD_SEG = 0x17573C
CMD_CMP = 0x175740
WB_CALL = 0x17574C
WB_FN = 0x175908

ORDER = (
    ("CMP0_R13", CMP0_R13, 28),
    ("BEQ_JUMP", BEQ_A_ZERO, 29),
    ("SEG106_ENTRY", SEG106_ENTRY, 30),
    ("SEG106_LDB", SEG106_LDB, 31),
    ("BEQ_106", BEQ_106, 35),
    ("EXIT_85", EXIT_85, 36),
    ("SLOT_85", SLOT_85, 39),
    ("SVC_RET", RET_PC, 40),
    ("EXTSB", EXTSB_PC, 44),
    ("TRAP_RETI", TRAP_RETI, 61),
    ("MAIN_JMP", MAIN_JMP, 68),
    ("CMD_SEG", CMD_SEG, 69),
    ("CMD_CMP", CMD_CMP, 71),
    ("WB_CALL", WB_CALL, 73),
    ("WB_FN", WB_FN, 74),
    ("LD_B_GATE", LD_B_GATE, 149),
    ("BNE_GATE", BNE_GATE, 154),
    ("TAKE_FALL", TAKE_FALL, 155),
    ("DISPATCH", DISPATCH, 236),
    ("SCHED_RETI", SCHED_RETI, 314),
    ("TASK9", TASK9_ENTRY, 315),
    ("CONST_LDI", CONST_LDI, 325),
)
ORDER_KEYS = ("EXIT_85", "SVC_RET", "MAIN_JMP", "WB_FN", "SCHED_RETI", "TASK9")

# ---------------------------------------------------------------- 2x2 实测
CUR = 0x68000000
READY = 0x68000004
TCB7 = 0x68000244
TCB9 = 0x6800029C
READY_RETI = 0x036E0000

COMBO = {
    "SAME_AB": dict(a=A_ORIG, b=B_SET, ret=0x00000000, reti=401,
                    p20=0xFFDB0000, p14=0x0000000A, ints=2, npc=334,
                    end_pc=0x137968, end_r4=0x001BDBE2,
                    e85=False, chain=False, b_gate="jump"),
    "A0_BSET": dict(a=A_ZERO, b=B_SET, ret=0x00000000, reti=398,
                    p20=0xFFDB0000, p14=0x0000000A, ints=2, npc=331,
                    end_pc=0x13796E, end_r4=0x001BDBE2,
                    e85=False, chain=False, b_gate="not_reached"),
    "AOR_BCLR": dict(a=A_ORIG, b=B_CLR, ret=0x00000000, reti=198,
                     p20=0xFF560000, p14=0xA2A05E1C, ints=0, npc=221,
                     end_pc=0x137968, end_r4=0x001BDC1C,
                     e85=False, chain=False, b_gate="fall"),
    "A0_BCLR": dict(a=A_ZERO, b=B_CLR, ret=RET_85, reti=314,
                    p20=0xFFD20000, p14=0x00000001, ints=1, npc=303,
                    end_pc=0x13796E, end_r4=0x001BDBFA,
                    e85=True, chain=True, b_gate="fall"),
}
COMBO_TAGS = ("SAME_AB", "A0_BSET", "AOR_BCLR", "A0_BCLR")
ONLY_85_TAG = "A0_BCLR"

# ---------------------------------------------------------------- 短/长窗口
SHORT = {
    "SAME_AB": dict(npc=192, end_pc=0x0413B4, end_r4=0x0000000A,
                    reti=None, ret=0x00000000, chain=False, task9=None),
    "A0_BCLR": dict(npc=226, end_pc=0x0404E8, end_r4=0x9F805AB0,
                    reti=None, ret=RET_85, chain=True, task9=None),
}
LONG = {
    "SAME_AB": dict(npc=334, end_pc=0x137968, reti=401, task9=402),
    "A0_BCLR": dict(npc=303, end_pc=0x13796E, reti=314, task9=315),
}
SHORT_WINDOW_NO_RETI = True
LONG_WINDOW_BOTH_RETI = True

# ---------------------------------------------------------------- 标准收敛态
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC
COV_HIT = (0x041BB6, 0x041BB8, 0x041B32, 0x041B48, 0x041C26,
           0x041C9A, 0x041CA0, 0x040EB6, 0x1352EA)
COV_MISS = (0x175282, 0x175908)
COV_HIT_COUNT = 9
COV_MISS_COUNT = 2

# ---------------------------------------------------------------- 机器与判据
MACHINE_COUNT = 9            # 5 长窗口组合 + 1 只观测 + 2 短窗口 + 1 标准收敛态
NEW_RULES = (84, 85)
KI_RANGE = "KI#336-339"
VERDICT = "two_layer_resolved_cross_segment_gate_chain_window_artifact"


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """按 bits 位补码读无符号值。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def sx8(v):
    return sx(v, 8)


def rel9_target(pc, rel9):
    """规矩 70 / 85：rel9 是 8 位有符号，pc+2+sx8(rel9)*2。"""
    return (pc + 2 + sx8(rel9) * 2) & 0xFFFFFFFF


def disp10_addr(r14, disp10):
    """FR 的 ld/st disp10 形态：r14 + sx8(disp10)*4（fr_emu.py:660）。"""
    return (r14 + sx(disp10, 8) * 4) & 0xFFFFFFFF


def reason_code(p20):
    """TCB+0x20 高半是等待服务号，按 16 位补码读。"""
    return sx((p20 >> 16) & 0xFFFF, 16)


def combo_truth(a, b):
    """−85 返回码只在 A=0 且 B=0 时出现（合取，不是二选一）。"""
    return "ret_minus_85" if a == 0 and b == 0 else "ret_zero"


def priority_verdict(rows):
    """四组都换手、只有一组返 −85 ⇒ 不是优先级，是两层事件。"""
    if not rows:
        return "inconclusive"
    all_switch = all(r.get("switched") for r in rows)
    e85 = [r.get("tag") for r in rows if r.get("e85")]
    if all_switch and len(e85) == 1 and e85[0] == ONLY_85_TAG:
        return "no_priority_two_layers"
    if not all_switch and len(e85) == 1:
        return "mutually_exclusive_branches"
    return "inconclusive"


def window_start_kind(start):
    """等形对照必须声明窗口起算点（规矩 84）。"""
    return {"trap": "trap_entry", "hook2": "after_hook2"}.get(start, "unknown")


def comparable_windows(a, b):
    return a == b and a != "unknown"


def order_monotonic(steps, keys=ORDER_KEYS):
    """关键事件的 step 序列是否严格单调递增。"""
    vals = [steps.get(k) for k in keys]
    if any(v is None for v in vals):
        return False
    return all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))


def a_semantic_verdict(a_val, r13_entry, writer_mn):
    """A = 调用者 r13 的栈帧保存槽，不是器件状态字（规矩 80 / 85）。"""
    if writer_mn in ("stm0", "stm1") and a_val == r13_entry:
        return "caller_r13_stack_slot"
    return "unknown"


def cross_segment_verdict(pc, rel9, target, owner_segment_lo, owner_segment_hi):
    """beq rel9=199 的 sx8 是负数 ⇒ 跨函数往低地址跳（规矩 85）。"""
    if sx8(rel9) >= 0:
        return "same_direction"
    if not (owner_segment_lo <= pc <= owner_segment_hi):
        return "unknown"
    if target < pc:
        return "cross_segment_backward"
    return "unknown"


def equal_shape(rows):
    """等形对照：boot / hook / post 三项必须全同。"""
    if not rows:
        return False
    return all(len(set(r.get(k) for r in rows)) == 1
               for k in ("boot", "hook", "post"))


def same_value_verdict(no_patch, same_value, keys):
    """写原值 ≡ 不写 ⇒ 差异来自值不是写动作。"""
    return all(no_patch.get(k) == same_value.get(k) for k in keys)


def coverage_scope_verdict(hit_count, miss_count,
                           hit_expect=COV_HIT_COUNT, miss_expect=COV_MISS_COUNT):
    """内核段跑过、命令分发链没跑到；与单次调用短窗口口径不冲突。"""
    if hit_count == hit_expect and miss_count == miss_expect:
        return "kernel_code_ran_command_chain_not_reached"
    return "inconclusive"


def short_window_verdict(short_no_reti, long_both_reti):
    """规矩 82 第二次实例：短窗口都无 reti、长窗口都有。"""
    if short_no_reti and long_both_reti:
        return "window_artifact_timing_shift"
    return "inconclusive"


def verdict(no_priority, monotone, cross_segment, equal_shape_ok,
            value_ok, short_ok, cov_ok, event_located=False):
    if not all((no_priority, monotone, cross_segment, equal_shape_ok,
                value_ok, short_ok, cov_ok)):
        return "open"
    if event_located:
        return "event_semantics_located"
    return VERDICT
