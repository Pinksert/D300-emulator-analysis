# -*- coding: utf-8 -*-
"""frbox.svc87 —— 不是服务复用，是按 r13 符号三分汇入共享尾（V87，纯函数）。

承接 KNOWN_ISSUES KI#339 里 V86 新增的结构问号：「−170 号服务为何跳入
−106 号服务段（服务复用还是共享尾）」。V87 把「服务复用」证伪：
**跳转目标落在分发表入口之间的中间代码，是多个服务段共用的共享尾**；
−170 号服务按调用者经陷阱栈帧传入的 r13（A = ``0x000083E4``）的符号，
把执行流分派到三段共享尾 / 本段继续。链仍未闭合。

分发表两口径（真内核 32 位有符号，规矩 76）
--------------------------------------------
``slot = (0x040660 + (svc32 << 1)) & 2^32``，
``target = (0x040660 + extsh(lduh @ slot)) & 2^32``。

=================  ==================  =========
服务号             表项槽              入口
=================  ==================  =========
−106 (0x96)        0x04058C/0x1532     0x041B92
−170 (0x56)        0x04050C/0x158A     0x041BEA
−46  (0xD2)        0x040604/0x14AA     0x041B0A
−44  (0xD4)        0x040608/0x1648     0x041CA8
−172 (0x54)        0x040508/0x1776     0x041DD6
=================  ==================  =========

全表 100 个落 ROM 内的服务号 -> **89 个不同入口**、**8 对服务号别名**
（如 ``-247 ≡ 22 -> 0x041DE4``、``4 ≡ 67 -> 0x041D6E``）。
**−170 与 −106 不在任何别名对里**，是两个独立入口。
只在 ``0x041B00..0x041E80`` 段内数，则是 14 个服务号 / 12 个入口
（正好对上 V83「12 个入口共享尾部」）——段内口径 ≠ 全表口径（规矩 78）。

五条 A 判据分支落点（rel9 一律 sx8，规矩 70 / 85）
---------------------------------------------------
``pc + 2 + sx8(rel9) * 2``：

==================  =========  =======  =====================
分支                  rel9       sx8      落点
==================  =========  =======  =====================
0x041C0C beq          74         +74     0x041CA2（−17 出口）
0x041C10 blt         187         −69     0x041B88（未跳，见下）
0x041C1E bne         183         −73     0x041B8E
0x041C26 beq         199         −57     0x041BB6（−106 段共享尾）
0x041C28 blt         174         −82     0x041B86（−46 段共享尾）
==================  =========  =======  =====================

``0x041B86 bra rel9=212`` 的 sx8 = −44 -> ``0x041B30``（另一段共享判定尾）。
只有 ``0x041C2A`` fall-through 留在 −170 号服务自己的段内。

动态真值表：A 的符号三分（7 台独立等形 600 步机）
-------------------------------------------------
全部在 HOOK2 ``0x041BFC`` 晚补，只往内存单元写 4 字节，不改寄存器、
不强制 PC、不注入中断。boot / hook / post 三项全同。

===========  ===========  ===========  ============  =========  ==========
组            A 读值        B 读值        汇入的共享尾   返 −85    返回点 r12
===========  ===========  ===========  ============  =========  ==========
SAME_AB       FFFFFFFF      80000000     TAIL_86/30    False      0x00000000
NEG_SET       FFFFFFFF      80000000     TAIL_86/30    False      0x00000000
NEG_CLR       FFFFFFFF      00000000     TAIL_86/30    False      0x00000000
ZERO_SET      00000000      80000000     TAIL_B6       False      0x00000000
ZERO_CLR      00000000      00000000     TAIL_B6       **True**   **0xAB**
POS_SET       00000001      80000000     FALL_POS      False      0x00000000
POS_CLR       00000001      00000000     FALL_POS      False      0x00000000
===========  ===========  ===========  ============  =========  ==========

七组末 CUR 同为 ``0x6800029C``（任务 9）、READY 同为 ``0x036E0000``；
``SAME_AB`` ≡ ``NEG_SET``（ret12 / npc / endPC / CUR / READY / steps
六项全同），证明差异来自写进去的值而不是写动作。

为什么是「共享尾」而不是「服务复用」
------------------------------------
1. D6：``0x041B86 / 0x041BB6 / 0x041BE4 / 0x041B30 / 0x041B88 /
   0x041CA2`` **六个跳转目标全部不是分发表入口**。调用一个服务必须经
   它自己的入口，−170 号服务跳的是别的服务段**中间**。
2. A5：``0x041BB6`` 全 ROM 只有 **1 条**静态入边（``0x041C26 beq``），
   ``0x041B86`` 也只有 1 条（``0x041C28 blt``）。−106 号服务是从自己
   入口 ``0x041B92`` **顺序落入** ``0x041BB6`` 的（三道校验通过后），
   不是被 −170 号服务调用。
3. D2：−106 号段在 ``0x041BB6`` 之前有 ``0x041BAC ldi:32
   #0x68000132,r0`` / ``0x041BA6 call`` 等**特有前置**；−170 号段在
   ``0x041C22 andccr #0xEF`` 备好等价上下文后直接跳过这些前置汇入。
4. ``0x041BE4`` 公共快速返回尾有 −46 段 ``0x041B12 bne`` 与 −170 段
   ``0x041BF2 bne`` **两条外部入边**，静态证明多个服务段汇流。
   另外 ``0x041C1A call rel12=1554``（−170 段）与
   ``0x041BA6 call rel12=1612``（−106 段）**双双落到
   ``0x041840``** —— 两个服务段还共用同一个公共子例程，
   进一步说明这一片是共享代码池而不是两个独立服务体。
5. D5 收敛态 coverage：共享判定尾 ``0x041B86 / 0x041B30 / 0x041BB6 /
   0x041C9A`` **全命中**（这段代码在收敛态本就跑过），错误/快速返回尾
   ``0x041B88 / 0x041B8E / 0x041BE4`` 未命中，命令分发链
   ``0x175282 / 0x175908`` 仍未命中。

自我证伪三处
-------------
* ``0x041C10 blt``（看起来像 A<0 的第一道门）七组**全执行但全没跳**
  （``TAIL_88`` 全 False）；不能凭 ``cmp2`` 助记符猜语义，真正的三分
  在 ``0x041C24 cmp #0,r13`` 之后 ⇒ 规矩 87。
* A1 只在段内数出「14 服务号 / 12 入口 / 2 对别名」，全表是
  「100 / 89 / 8 对」；段内数字不能当全表口径（延续规矩 78）。
* ``0x041BE8 ldi:8 #187,r12`` 既是 ``0x041BE6 ret:d`` 的延迟槽，
  又紧邻 −170 号入口 ``0x041BEA``；地址就近归属会读错函数边界
  （规矩 77 / 78 / 79 第三次实例）。

硬规矩 86（本版新增）
---------------------
报「服务复用」前先分清**服务号别名**（分发表多号对一入口）与**共享尾**
（多个服务段汇入入口之间的中间代码）：必须同时做「分发表入口普查」
证明跳转目标不是任何入口，以及「静态入边普查」证明汇流；
不许只看地址就近把跳进某服务段中间读成调用了该服务。

硬规矩 87（本版新增）
---------------------
报「按某寄存器符号三分」前要用动态轨迹确认哪条分支**真的跳了**：
执行到某条 ``blt`` / ``beq`` 不等于跳转发生（``cmp2`` 与 ``cmp #0``
是两套判据）；段内计数 ≠ 全表口径（延续规矩 78）。

边界与仍开放
-------------
所有写 ``0x000083E4`` / ``0x68000970`` 都是**反事实补全**，不是固件行为、
不是真机器件事件；任务换手是既有代码的自然执行。仍开放：真机
``vec 0x18`` 周期性器件事件、−85 出口的器件事件语义、bit3 置位源、
``vec 0x2D``、317 个提交调用点、任务 9 表下载完成后是否回到任务 7、
``0xFFFFFFFF`` r13 哨兵在真机里由谁设置、8 对服务号别名的语义、
``cmp2`` 的真实语义（为何 ``0x041C10 blt`` 不跳）。
**不能报**：−170 号服务复用了 −106 号服务 / A<0 由 ``0x041C10 blt``
决定 / 段内 14·12 是全表口径 / 所有服务语义已解出 / 真机已复现 −85 /
显示链通了 / 出画面 / 系统跑起来了 / 真机如此。
"""

# --------------------------------------------------------------- 口径常量
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
A_ZERO = 0x00000000
A_POS = 0x00000001
B_SET = 0x80000000
B_CLR = 0x00000000
RET_85 = 0x000000AB

# --------------------------------------------------------------- 分发表
TBL_ZERO = 0x040660
SEG_LO, SEG_HI = 0x041B00, 0x041E80
SEG_SVC_COUNT = 14
SEG_ENTRY_COUNT = 12
TABLE_SVC_COUNT = 100
TABLE_ENTRY_COUNT = 89
TABLE_ALIAS_PAIRS = 8

ENTRY_46 = 0x041B0A
ENTRY_106 = 0x041B92
ENTRY_170 = 0x041BEA
ENTRY_44 = 0x041CA8
ENTRY_172 = 0x041DD6

SLOT_46, HALF_46 = 0x040604, 0x14AA
SLOT_106, HALF_106 = 0x04058C, 0x1532
SLOT_170, HALF_170 = 0x04050C, 0x158A
SLOT_44, HALF_44 = 0x040608, 0x1648
SLOT_172, HALF_172 = 0x040508, 0x1776

ALIAS_PAIRS = (
    (0x040672, (-254, -214)),
    (0x040AA0, (28, 56)),
    (0x040D6E, (20, 52, 72)),
    (0x040DE4, (-236, 64)),
    (0x041D6E, (4, 67)),
    (0x041DE4, (-247, 22)),
    (0x046B80, (114, 125)),
    (0x046E60, (-230, 74, 93, 104)),
)

# --------------------------------------------------------------- A 判据链
CMP2_R10 = 0x041C0A
BEQ_R10 = 0x041C0C
CMP2_R13 = 0x041C0E
BLT_EARLY = 0x041C10
BNE_1E = 0x041C1E
CMP0_R13 = 0x041C24
BEQ_A0 = 0x041C26
BLT_LATE = 0x041C28
FALL_POS = 0x041C2A

REL9_ROWS = (
    (0x041C0C, 74, 74, 0x041CA2),
    (0x041C10, 187, -69, 0x041B88),
    (0x041C1E, 183, -73, 0x041B8E),
    (0x041C26, 199, -57, 0x041BB6),
    (0x041C28, 174, -82, 0x041B86),
)
BRA_86 = 0x041B86
BRA_86_REL9 = 212
BRA_86_SX8 = -44
BRA_86_TARGET = 0x041B30

# --------------------------------------------------------------- 共享尾
TAIL_86 = 0x041B86
TAIL_88 = 0x041B88
TAIL_8E = 0x041B8E
TAIL_30 = 0x041B30
TAIL_B6 = 0x041BB6
TAIL_E4 = 0x041BE4
TAIL_A2 = 0x041CA2
TAIL_9A = 0x041C9A

ANDCCR_106 = 0x041BB4
LDI_R0_106 = 0x041BAC
CALL_106 = 0x041BA6
CALL_106_REL12 = 1612
CALL_170 = 0x041C1A
CALL_170_REL12 = 1554
COMMON_SUB = 0x041840
ANDCCR_170 = 0x041C22
BNE_46 = 0x041B12
BNE_170 = 0x041BF2
BNE_106 = 0x041BC6
BEQ_106 = 0x041BCC
EXIT_85 = 0x041C9A
RET_D_85 = 0x041C9E
SLOT_85 = 0x041CA0

RET_D_E4 = 0x041BE6
SLOT_E4 = 0x041BE8
SLOT_E4_VAL = 187

INEDGE_B6 = 1
INEDGE_86 = 1
INEDGE_E4 = 2
INEDGE_9A = 2
INEDGE_A2 = 1
INEDGE_ENTRY_106 = 0
INEDGE_ENTRY_170 = 0

# --------------------------------------------------------------- 动态真值表
MACHINE_COUNT = 7
GROUPS = (
    ("SAME_AB", A_ORIG, B_SET, "TAIL_86", 0x00000000, 401, False),
    ("NEG_SET", A_ORIG, B_SET, "TAIL_86", 0x00000000, 401, False),
    ("NEG_CLR", A_ORIG, B_CLR, "TAIL_86", 0x00000000, 198, False),
    ("ZERO_SET", A_ZERO, B_SET, "TAIL_B6", 0x00000000, 398, False),
    ("ZERO_CLR", A_ZERO, B_CLR, "TAIL_B6", RET_85, 314, True),
    ("POS_SET", A_POS, B_SET, "FALL_POS", 0x00000000, 417, False),
    ("POS_CLR", A_POS, B_CLR, "FALL_POS", 0x00000000, 218, False),
)
STEP_BLT_EARLY = 8
STEP_BEQ_A0 = 29
STEP_BLT_LATE = 30
STEP_TAIL_86 = 31
STEP_TAIL_30 = 32
STEP_TAIL_B6 = 30
STEP_FALL_POS = 31
SAME_AB_EQUALS_NEG_SET = True
BLT_EARLY_NEVER_TAKEN = True
TAIL_88_ALL_FALSE = True
END_CUR = 0x6800029C
END_READY = 0x036E0000
END_PC_BY_TAIL = {"TAIL_86": 0x137968, "TAIL_B6": 0x13796E,
                  "FALL_POS": 0x137972}

# --------------------------------------------------------------- 收敛态
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC
MAIN_JMP = 0x175282
WB_FN = 0x175908
COV = {
    "TAIL_86": True, "TAIL_30": True, "TAIL_88": False,
    "TAIL_8E": False, "TAIL_B6": True, "TAIL_E4": False,
    "SEG106_ENTRY": True, "SEG170_ENTRY": True,
    "CMP0_R13": True, "BEQ_A0": True, "BLT_LATE": True,
    "EXIT_85": True, "MAIN_JMP": False, "WB_FN": False,
}
COV_CODES = {
    "TAIL_86": TAIL_86, "TAIL_30": TAIL_30, "TAIL_88": TAIL_88,
    "TAIL_8E": TAIL_8E, "TAIL_B6": TAIL_B6, "TAIL_E4": TAIL_E4,
    "SEG106_ENTRY": ENTRY_106, "SEG170_ENTRY": ENTRY_170,
    "CMP0_R13": CMP0_R13, "BEQ_A0": BEQ_A0, "BLT_LATE": BLT_LATE,
    "EXIT_85": EXIT_85, "MAIN_JMP": MAIN_JMP, "WB_FN": WB_FN,
}

VERDICT = "shared_tail_resolved_r13_sign_tri_dispatch_not_service_reuse"


# --------------------------------------------------------------- 纯函数
def sx(v, bits):
    """按 ``bits`` 位补码解释无符号位串。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def sx8(v):
    return sx(v, 8)


def sx11(v):
    return sx(v, 11)


def extsh(h):
    return sx(h & 0xFFFF, 16)


def rel9_target(pc, rel9):
    """规矩 70 / 85：``rel9`` 是 8 位有符号，``pc+2+sx8*2``。"""
    return (pc + 2 + sx8(rel9) * 2) & 0xFFFFFFFF


def rel12_target(pc, rel12):
    """``call:d rel12`` 是 11 位有符号，``pc+2+sx11*2``（V79）。"""
    return (pc + 2 + sx11(rel12) * 2) & 0xFFFFFFFF


def slot_of(svc32):
    """真内核表项槽（规矩 76：svc32 是完整 32 位有符号服务号）。"""
    return (TBL_ZERO + ((svc32 << 1) & 0xFFFFFFFF)) & 0xFFFFFFFF


def target_of(half):
    return (TBL_ZERO + extsh(half)) & 0xFFFFFFFF


def is_dispatch_entry(addr, entries):
    """目标是不是分发表里的某个服务入口。"""
    return addr in entries


def alias_pairs(svc_to_entry):
    """多号对一入口 -> ``[(entry, (svc...))]``，按入口升序；只收 >=2 个号。"""
    acc = {}
    for svc, entry in svc_to_entry.items():
        acc.setdefault(entry, []).append(svc)
    return sorted((e, tuple(sorted(s))) for e, s in acc.items() if len(s) > 1)


def shared_tail_verdict(target, entries, inedge_count, has_fallthrough_owner):
    """区分「服务复用」与「共享尾」。

    目标**是**入口 ⇒ ``service_entry``（可能伴随别名）；
    目标不是入口、有静态入边、且另有服务段顺序落入 ⇒ ``shared_tail``。
    """
    if is_dispatch_entry(target, entries):
        return "service_entry"
    if inedge_count > 0 and has_fallthrough_owner:
        return "shared_tail"
    return "unresolved"


def sign_class(a):
    """A 的 32 位有符号符号类；V87 的三分只认这一类。"""
    s = sx(a & 0xFFFFFFFF, 32)
    return "negative" if s < 0 else ("zero" if s == 0 else "positive")


def tri_dispatch(a):
    """符号三分 -> 汇入点名字（动态真值表实测）。"""
    return {"negative": "TAIL_86", "zero": "TAIL_B6",
            "positive": "FALL_POS"}[sign_class(a)]


def gate_verdict(hit_beq, hit_blt, hit_fall):
    """三分支互斥性：恰好一个落点被命中才成立。"""
    n = sum(1 for x in (hit_beq, hit_blt, hit_fall) if x)
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


def scope_verdict(seg_svc, seg_entry, table_svc, table_entry):
    """规矩 78 / 87：段内计数不许当全表口径。"""
    return "segment_only" if (seg_svc, seg_entry) != (table_svc, table_entry) \
        else "same_scope"


def boundary_verdict(slot_pc, entry_pc, adjacent=True):
    """规矩 77 / 78 / 79：延迟槽紧邻下一个入口时，不许按地址就近归属。"""
    return "delay_slot_adjacent_entry" if adjacent and slot_pc + 2 == entry_pc \
        else "ordinary"


def cov_verdict(cov):
    """共享判定尾命中而命令分发链未命中 ⇒ 代码跑过、链仍未走到。"""
    shared = all(cov.get(k, False) for k in
                 ("TAIL_86", "TAIL_30", "TAIL_B6", "EXIT_85"))
    cmd = cov.get("MAIN_JMP", True) or cov.get("WB_FN", True)
    return "shared_tail_seen_cmd_chain_unreached" if shared and not cmd else "open"


def source_verdict(entries_ok, inedges_ok, tri_ok, cov_ok, event_located):
    """综合判据：链未闭合（真机器件事件语义仍未定位）。"""
    if not (entries_ok and inedges_ok and tri_ok and cov_ok):
        return "open"
    if event_located:
        return "event_semantics_located"
    return VERDICT
