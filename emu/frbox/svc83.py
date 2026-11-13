# -*- coding: utf-8 -*-
"""frbox.svc83 —— 返回码写在 ret:d 延迟槽里，−85 出口的归属与触发条件（V83，纯函数）。

承接 KNOWN_ISSUES KI#323 第二条：V82 只定位了 ``0x041CA0 ldi:8 #0xAB,r12``
这个 −85 写点，并声明「本版只定位写点，未证真机触发条件」。V83 用
「全形态入边 + 单步轨迹 + 等形反事实」把这条收窄，且**修正一处定性**。

V82 需要修正的一处定性
-----------------------
``0x041CA0`` 不是独立基本块，也没有直接跳转入边；它是
``0x041C9E ret:d`` 的**延迟槽**。`fr_emu.step()` 在 ``ret:d`` 之后先执行
下一条指令再跳回 RP，所以返回码是「跳到 ``0x041C9A`` 才被写进 r12」：

    0x041C92 orccr → 0x041C94 ld → 0x041C96 ret:d → 0x041C98 ldi:8 #0   → 0
    0x041C9A orccr → 0x041C9C ld → 0x041C9E ret:d → 0x041CA0 ldi:8 #171 → -85
    0x041CA2 ld    → 0x041CA4 ret:d → 0x041CA6 ldi:8 #239 → -17

前两条出口是同一 ``orccr/ld/ret:d`` 模板，第三条 ``0x041CA2`` 少一条
``orccr``（实测模板为 ``ld/ret:d/ldi:8``）；三者都属于
「ret:d + 延迟槽里放返回码」这一种结构，不能写成三条模板逐字相同。
V82 的「−85 写点 2 个」是 ``0x041B00..0x041E80`` 段内口径；
全内核区 ``0x040000..0x080000`` 有 **167 条 ret:d、105 条返回码延迟槽、
7 处 −85**，按「最近前驱入口」口径分别归到
−37 / −107 / −52 / −170 / −108 / −141 / −167 七个服务号（规矩 79）。

入边归属不等于地址就近归属
---------------------------
−85 出口 ``0x041C9A`` 的**可定址入边只有 2 条**：
``0x041BC6 bne`` / ``0x041BCC beq``，两条都在 ``0x041B92``
（**−106 号服务**独立入口）函数体内，不在 −170 号函数体内。
按「最近前驱入口」``0x041CA0`` 归 −170，但按入边来源归 −106 —— 两种口径
不可混用（延续规矩 67 / 77，本版规矩 79）。

真实收敛态轨迹（只读机器状态）
------------------------------
``blive.boot(0)`` + ``step(483841)`` 到主循环那次 −170 号陷阱，
处理函数 ``0x041BEA`` 实测走：
``0x041BF0 cmp r8,31`` → ``0x041BF4 cmp r6,0`` → ``0x041BFA and #0xFC,r7``
→ ``0x041C1A call 0x041840`` → ``0x041C24 cmp r13,0``（r13=0xFFFFFFFF，不跳）
→ ``0x041C28 blt 0x041B86`` → ``0x041B30..0x041B82 ret:d``
→ 延迟槽 ``0x041B84 ldi:8 #0,r12`` → 服务返回 0。
``0x041B84`` 按最近入口归 **−46 号服务（0x041B0A）尾部**。
⇒ 真实调用**三条并列出口一条都没走**，``0x041C9A`` / ``0x041CA0`` 覆盖 0 次。

两道内部条件的等形反事实（结论，但仍是反事实）
------------------------------------------------
四台独立机器，同 boot 步数 / 同锚点 / 同后续 260 步，
**不强制设 PC、不注入中断**，只改两个寄存器：

=================  =============  ============  ===========================
组                  r13@0x041C24   r8@0x041BCA  结果
=================  =============  ============  ===========================
BASE               不改           不改          全关，走真实尾 r12=0
G1                 0              不改          门1 开、门2 关
G2                 0              0             **两门全开**，服务自己返回 0xAB
G3                 0              全 1          门1 开、门2 关（归因分离）
=================  =============  ============  ===========================

G2 在服务返回点读到 ``r12=0x000000AB``（经 ``0x0406BE extsb`` 得 −85），
``0x041C9A`` / ``0x041C9E`` / ``0x041CA0`` 全部命中，并且
``MAIN_JMP / CMD_SEG / CMD_CMP / WB_CALL / WB_FN`` 全部命中。
这是**让服务内部逻辑自己走到 −85 出口**，不是 V82 那样在返回点直接写 r12；
但与 V82 一样，改寄存器仍是**模型化反事实补全**，不是固件行为、
不是真机器件事件，链仍未闭合。

硬规矩 78（本版新增）
---------------------
报「返回码写点」前先分清 ``ret:d`` 延迟槽与基本块入口：
``ldi:8 → r12`` 紧跟 ``ret:d`` 时是延迟槽，只在跳到 ``ret:d`` 之前的
入口时才执行；入边要按「跳到 ``ret:d`` 前一条」算。不许把延迟槽 ``ldi``
当独立写点，也不许把段内计数当全内核计数。

硬规矩 79（本版新增）
---------------------
报「某返回码出口属于某服务」前先分清**入边来源**与**地址就近归属**：
这一段是多个服务共享尾部（V83 反查到 12 个入口落在 0x041B00..0x041E80），
按地址区间判函数归属会同时把 −106 的入边和 −170 的延迟槽算成同一函数。

反事实边界与共同禁语
---------------------
所有改 r13 / r8 / r12、``raise_irq``、强制设 PC 都是反事实补全；
不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
「所有服务语义已解出」/「真机已复现 −85」/「−85 出口属于 −170 号函数体」/
「补一次 r13 就等于真机事件」。

仍开放：真机让 ``vec 0x18`` 周期性发生的器件/事件（KI#191）、
让 −170 号服务在真机走 −85 出口的**器件事件语义**（本版只证反事实可达）、
两状态字偏移 0 bit3 置位源、``vec 0x2D`` 硬件事件、
317 个提交调用点非根部分的触发任务。
"""

# ---------------------------------------------------------------- 静态口径
KERNEL_LO, KERNEL_HI = 0x040000, 0x080000
SEG_LO, SEG_HI = 0x041B00, 0x041E80
RET_D_TOTAL = 167            # 内核区 ret:d 总数
SLOT_WRITES = 105            # 其中延迟槽 ldi→r12（返回码模板）
RET_D_IN_SEG = 20            # 0x041B00..0x041E80 段内 ret:d
SLOTS_85_IN_SEG = 2          # V82 报的「-85 写点 2 个」是段内口径
SLOTS_85_KERNEL = 7          # 全内核口径
CODE_TOP10 = ((0, 57), (-33, 12), (-69, 12), (-85, 7), (-17, 5),
              (-63, 4), (-73, 4), (-20, 1), (-24, 1), (-52, 1))
CODE_TOP10_SUM = 104

# (ret:d, 延迟槽, 按最近入口归到的服务号)
SLOTS_85 = (
    (0x0413FE, 0x041400, -37),
    (0x041974, 0x041976, -107),
    (0x041A3E, 0x041A40, -52),
    (0x041C9E, 0x041CA0, -170),
    (0x041DC6, 0x041DC8, -108),
    (0x041FC8, 0x041FCA, -141),
    (0x04243A, 0x04243C, -167),
)

# 入口 → (ret:d, 延迟槽, 立即数, 8 位有符号返回码)
EXIT_TRIPLE = {
    0x041C92: (0x041C96, 0x041C98, 0x00, 0),
    0x041C9A: (0x041C9E, 0x041CA0, 0xAB, -85),
    0x041CA2: (0x041CA4, 0x041CA6, 0xEF, -17),
}
EDGES_85 = (0x041BC6, 0x041BCC)
EDGES_0 = (0x041C82, 0x041C8A)
EDGE_17 = (0x041C0C,)
BRANCH_FORMS = {"rel9": 61, "rel12": 14, "ind": 6}
EXIT_TEMPLATE = ("orccr", "ld", "ret:d", "ldi:8")  # 前两条；第三条 0x041CA2 少 orccr

# 服务号归属（V82 真内核 32 位有符号口径反查）
OWNER_ENTRIES = {
    0x041B0A: (-46,), 0x041B92: (-106,), 0x041BEA: (-170,),
    0x041CA8: (-44,), 0x041CDE: (-63,), 0x041D20: (-202,),
    0x041D38: (-61,), 0x041D6E: (4, 67), 0x041D88: (-108,),
    0x041DD6: (-172,), 0x041DE4: (-247, 22), 0x041E58: (-60,),
}
ENTRIES_IN_SEG = 12
ERR_MID = 0x041BB6
ERR_MID_OWNER = 0x041B92
REAL_TAIL = 0x041B82
REAL_SLOT0 = 0x041B84
REAL_TAIL_OWNER = 0x041B0A
REAL_PATH_KEYS = (0x041BEA, 0x041BF0, 0x041BF4, 0x041BFA, 0x041C1A,
                  0x041C24, 0x041C26, 0x041C28, 0x041B86, 0x041B82,
                  0x041B84)
REAL_R13 = 0xFFFFFFFF
REAL_RET = 0
REAL_EXITS_COVERED = 0

# ---------------------------------------------------------------- 动态口径
BOOT_STEP = 483841
TRAP = 0x040668
RET_PC = 0x0406B4
ANCHOR1 = 0x041C24
ANCHOR2 = 0x041BCA
AFTER = 260
CF_GROUPS = (("BASE", None, None), ("G1", 0x00000000, None),
             ("G2", 0x00000000, 0x00000000),
             ("G3", 0x00000000, 0xFFFFFFFF))
CF_G2_RET = 0x000000AB
GATE_KEYS = ("MAIN_JMP", "CMD_SEG", "CMD_CMP", "WB_CALL", "WB_FN")
MAIN_JMP, CMD_SEG, CMD_CMP = 0x175282, 0x17573C, 0x175740
WB_CALL, WB_FN = 0x17574C, 0x175908
CF_EXPECT = {
    "BASE": (False, False, False, 0x00000000),
    "G1": (True, False, False, 0x00000000),
    "G2": (True, True, True, 0x000000AB),
    "G3": (True, False, False, 0x00000000),
}
VERDICT = "delay_slot_exit_resolved_counterfactual_two_gates_open"


def sx8(v):
    """8 位补码解码（返回码立即数口径，与 0x0406BE extsb 一致）。"""
    v &= 0xFF
    return v - 256 if v & 0x80 else v


def slot_code(raw8):
    """延迟槽 ldi:8 立即数 → 服务返回码。"""
    return sx8(raw8)


def is_delay_slot(insns, pc):
    """pc 是否是某条 ret:d 的下一条指令（指令表为 (pc, mn, v, sz) 序列）。"""
    for i, (p, mn, _v, _s) in enumerate(insns[:-1]):
        if mn == "ret:d" and insns[i + 1][0] == pc:
            return True
    return False


def delay_slot_owner(insns, pc):
    """返回把 pc 当延迟槽的那条 ret:d 地址；不是延迟槽返回 None。"""
    for i, (p, mn, _v, _s) in enumerate(insns[:-1]):
        if mn == "ret:d" and insns[i + 1][0] == pc:
            return p
    return None


def template_of(seq):
    """把指令序列归约成助记符元组，用于三条并列出口的模板比对。"""
    return tuple(mn for _p, mn, _v, _s in seq)


def exit_record(entry):
    """给定并列出口入口，返回 (ret:d, 延迟槽, 返回码)。"""
    rec = EXIT_TRIPLE.get(entry)
    return None if rec is None else (rec[0], rec[1], rec[3])


def nearest_entry(addr, entries):
    """按地址取最近前驱入口；这只是口径之一，不等于函数归属（规矩 79）。"""
    prev = [e for e in entries if e <= addr]
    return prev[-1] if prev else None


def owner_of(addr, entries, owners):
    """最近前驱入口对应的服务号元组。"""
    e = nearest_entry(addr, entries)
    return owners.get(e, ()) if e is not None else ()


def scope_verdict(segment_count, kernel_count):
    """段内计数与全内核计数的口径判定。"""
    if segment_count > kernel_count:
        return "inconsistent"
    return "segment_only" if segment_count < kernel_count else "kernel_wide"


def equal_shape(rows):
    """等形四项：boot / anchor1 / anchor2 / post 全同。"""
    if not rows:
        return False
    return all(len(set(r.get(k) for r in rows)) == 1
               for k in ("boot", "anchor1", "anchor2", "post"))


def gate_verdict(gate1, gate2):
    """两道内部条件：门1 = r13==0 跳 0x041BB6；门2 = 命中 -85 出口。"""
    if not gate1:
        return "both_closed"
    return "both_open" if gate2 else "gate1_only"


def cf_verdict(slot_ok, ret85, both_gates, wb_fn, event_located):
    """综合判据：静态/动态/反事实三层都成立但真机器件事件仍未定位。"""
    if not (slot_ok and ret85 and both_gates and wb_fn):
        return "open"
    if event_located:
        return "event_semantics_located"
    return VERDICT
