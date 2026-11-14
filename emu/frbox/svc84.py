# -*- coding: utf-8 -*-
"""frbox.svc84 —— 两道内部条件的数据源与干预层级（V84，纯函数）。

承接 KNOWN_ISSUES KI#327 第二条：V83 用等形反事实证明「改 r13 / r8 两个
寄存器可让 −170 号服务自己返回 0xAB（−85）」，但留了一句「只证反事实可达，
未证真机器件事件语义」，且**没有追这两个寄存器从哪里来**。V84 把这一步
落到数据层，并**修正 V83 的一处隐含假定**：两道条件不是「缺某个器件事件」，
而是两个 RAM 单元里的值，且两单元性质完全不同。

数据源（单步实测，1 台只观测轨迹机）
------------------------------------
``0x041BFE ld disp10=13,r13``：FR 的 disp10 是 8 位有符号 ×4（fr_emu.py:660），
r14=0x000083B0，故实际单元 **A = 0x000083E4**；本次读到 0xFFFFFFFF。
``0x041BB8 ld disp10=2,r8``：r14=0x68000968（由 CUR_TCB 链解引用而来），
实际单元 **B = 0x68000970**；本次读到 0x80000000。
两者都在 RAM（``ram 0x1000+0x3F000`` / ``ram-kernel 0x68000000+0x20000``），
**不是 ROM 常量**；但静态 ``sweep.writers_of(A) == writers_of(B) == []``。

A 是陷阱分发器栈帧保存槽，不是器件状态字
------------------------------------------
``bus.all_writes`` 给出真实写者：``0x040692`` 一条 **``stm1`` 批量压栈**
在锚点口径下写了 A **71 次**，值域 14 种（0x4 / 0x34 / 0x100 / 0x305 /
0x0 / 0x8 / 0x280 / 0x1 / 0xDCC / 0x20000000 / 0x9F58C4D0 / 0x1B8A68 /
0x260 / 0xFFFFFFFF）。从陷阱入口 ``0x040668`` 到 ``0x041BFE`` 的第 21 步
（执行 pc=0x040694）正是它把 A 写成 0xFFFFFFFF，第 44 步才读到。
⇒ ``r13`` 是**调用者经陷阱栈帧传进来的参数**，每次 ``int #0x40`` 都被覆写；
V83 在 ``0x041C24`` 直接改 r13，等价于改调用者参数，不是改器件状态。
静态 ``writers_of`` 看不见 ``stm1``（规矩 80）。

B 才是内核对象位标志字
-----------------------
``0x041A5C ld disp10=2,r7`` → ``0x041A5E or r5,r7`` →
``0x041A60 st disp10=2,r5`` 是**置位**，本次写 0x80000000（bit31）；
``0x041AFA ld disp10=2,r7`` → ``0x041AFC and r5,r7`` →
``0x041AFE st disp10=2,r5`` 是**清位**，写 0。
锚点口径下 B 被写 4 次、标准收敛态 5 次，共 4 个写点 PC。

收敛态并不「没人写」
--------------------
标准收敛设备策略（V65–V84 逐字相同）``step(191520)`` + 单次
``raise_irq(0x2B)`` + ``run(500000)``：末 PC 0x0407DC，RAM 写 113859 次，
A 被写 182 次、末值 0x9F58C4D0，B 被写 5 次、末值 0。
V83 陷阱锚点口径 ``step(483841)``：RAM 写 109261 次，A 写 **76 次**
（锚点**之前**）、末值 0x260，B 写 4 次、末值 0x80000000。
再从锚点单步 44 步到 ``0x041BFE`` 时 A 变成 **77 次**：多出来的 1 次
正是第 21 步 ``stm1``。两个口径不可混用（76 = 锚点前 / 77 = 读 A 时）。
三个时刻 A 三个不同值（0x260 / 0xFFFFFFFF / 0x9F58C4D0），
是高频复用槽而非专用状态字。

只补内存单元、不改寄存器的等形反事实
------------------------------------
7 台独立机器，boot=483841、挂钩 PC 集合 ``{0x040668, 0x041BFC}``、
后续 260 步全同；**不强制设 PC、不注入中断、不改任何寄存器**，
只往 A / B 写 4 字节。HOOK2=0x041BFC 是 ``0x041BFE`` 读 A 的前一条。

=================  ===========  ===========  ==============  ==================
组                  早补(陷阱口)  晚补(读前一步)  读 A / 读 B     结果
=================  ===========  ===========  ==============  ==================
BASE                —            —            FFFFFFFF / —     r12=0，链全关
EARLY_A             A=0          —            FFFFFFFF / —     与 BASE 逐项同
EARLY_B             B=0          —            FFFFFFFF / —     路径变，门仍关
EARLY_AB            A=0,B=0      —            FFFFFFFF / —     路径变，门仍关
LATE_A0             —            A=0          00000000 / 80000000  门1 开门2 关
LATE_AB             B=0          A=0          00000000 / 00000000  **两门全开**
LATE_BOTH           —            A=0,B=0      00000000 / 00000000  **两门全开**
=================  ===========  ===========  ==============  ==================

``EARLY_A`` 与 ``BASE`` 逐项相同（166 个不同 PC、末 r4=0xA、返回 0）：
早补的 A=0 被第 21 步 ``stm1`` 覆盖 —— **补早了会被覆盖**（规矩 81）。
``LATE_AB`` / ``LATE_BOTH`` 逐项相同（210 PC、末 r4=0xB、
服务返回点 r12=0x000000AB 经 ``0x0406BE extsb`` 得 −85，
``EXIT_85 / RET_D_85 / SLOT_85 / MAIN_JMP / CMD_SEG / CMD_CMP /
WB_CALL / WB_FN`` 全命中），证明 B 在这段窗口内无中间写入、早补晚补等价；
「服务自己走到 −85 出口」这次是**由内存单元写入触发的自然执行流**，
不再需要改寄存器。不同 PC 210 vs 166（差 44）证明改写确实改变执行。
``EARLY_B`` / ``EARLY_AB`` 在门1 关时把 B 清成 0，服务返回后 PC 集合
166→260、末 r4 0xA→0x1BDBAE，但没打开 −85 出口；具体去向本版未追，
列为开放项，不解读为「唤醒成功」。

硬规矩 80（本版新增）
---------------------
报「某出口缺器件事件」前先分清数据源类型：陷阱分发器栈帧保存槽
（``stm1`` 批量压栈，静态不可见）/ 内核对象位标志字（``or`` 置位、
``and`` 清位）/ ROM 常量。``writers_of(addr) == 0`` **不等于没人写**，
必须再看 ``bus.all_writes`` 的动态写记录与写点 PC 直方图；
不许把高频复用槽叫成「专用状态字」。

硬规矩 81（本版新增）
---------------------
报「补数据有效」前先分清**干预层级**与**写入时机**：
补寄存器 ≠ 补内存单元 ≠ 补器件事件；早补会被后续 ``stm1`` / 置位指令
覆盖，等形对照必须声明挂钩 PC 与写入时刻；「服务自己返回 −85」只说明
这条内部链完整，不等于真机发生过。

反事实边界与共同禁语
---------------------
所有往 ``0x000083E4`` / ``0x68000970`` 写值、改 r13 / r8 / r12、
``raise_irq``、强制设 PC 都是模型化反事实补全，不是固件行为、
不是真机器件事件；链仍未闭合。不能报显示链通了 / 出画面 /
系统跑起来了 / 真机如此 /「所有服务语义已解出」/「真机已复现 −85」/
「−85 出口属于 −170 号函数体」/「补一次 r13 就等于真机事件」/
「写一次内存单元就等于器件事件」/「A 是器件状态字」。

仍开放：真机让 ``vec 0x18`` 周期性发生的器件/事件（KI#191）、
让 −170 号服务在真机走 −85 出口的器件事件语义（本版只定位到具体
写点与干预层级）、两状态字偏移 0 bit3 置位源、``vec 0x2D`` 硬件事件、
317 个提交调用点非根部分的触发任务、``EARLY_B`` 清掉 B 位后
服务返回后路径的具体去向。
"""

# ---------------------------------------------------------------- 静态口径
BOOT_STEP = 483841
TRAP = 0x040668
RET_PC = 0x0406B4
HOOK1 = 0x040668
HOOK2 = 0x041BFC              # 0x041BFE 的前一条
AFTER = 260
MAIN_SVC_IMM32 = 0xFFFFFF56
MAIN_SVC_32 = -170

ROM_BASE, ROM_END = 0x00040000, 0x00740000
RAM_REGION = (0x00001000, 0x0003F000, "ram")
KERN_REGION = (0x68000000, 0x00020000, "ram-kernel")

# ---------------------------------------------------------------- 两道条件
LD_R13_PC = 0x041BFE
LD_R13_DISP = 13
R14_AT_LD13 = 0x000083B0
SRC_A = 0x000083E4
SRC_A_AT_LOAD = 0xFFFFFFFF
LD_R8_PC = 0x041BB8
LD_R8_DISP = 2
R14_AT_LD8 = 0x68000968
SRC_B = 0x68000970
SRC_B_AT_LOAD = 0x80000000

CMP_R13 = 0x041C24
BEQ_ERR = 0x041C26
ERR_MID = 0x041BB6
AND_GATE = 0x041BCA
BEQ_85 = 0x041BCC
EXIT_85 = 0x041C9A
RET_D_85 = 0x041C9E
SLOT_85 = 0x041CA0

MAIN_JMP, CMD_SEG, CMD_CMP = 0x175282, 0x17573C, 0x175740
WB_CALL, WB_FN = 0x17574C, 0x175908
GATE_KEYS = ("MAIN_JMP", "CMD_SEG", "CMD_CMP", "WB_CALL", "WB_FN")
EXIT_KEYS = ("EXIT_85", "RET_D_85", "SLOT_85")

# ---------------------------------------------------------------- 动态写点
A_WRITER = 0x040692
A_WRITER_MN = "stm1"
A_WRITER_EXEC_PC = 0x040694
A_WRITER_STEP = 21
LD_R13_STEP = 44
A_WRITER_COUNT = 71
A_WRITER_VALS = (0x00000004, 0x00000034, 0x00000100, 0x00000305,
                 0x00000000, 0x00000008, 0x00000280, 0x00000001,
                 0x00000DCC, 0x20000000, 0x9F58C4D0, 0x001B8A68,
                 0x00000260, 0xFFFFFFFF)
A_WRITER_PCS = 6
B_WRITER_SET = 0x041A60
B_WRITER_CLR = 0x041AFE
B_SET_VAL = 0x80000000
B_CLR_VAL = 0x00000000
B_WRITER_PCS = 4
STATIC_WRITERS_A = 0
STATIC_WRITERS_B = 0

# ---------------------------------------------------------------- 收敛态
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC
CONVERGE_RAM_WRITES = 113859
ANCHOR_RAM_WRITES = 109261
A_WRITES_ANCHOR = 76        # step(483841) 锚点之前的 A 写入次数
A_WRITES_TO_LD13 = 77       # 再单步 44 步到 0x041BFE（多 1 次 stm1）
A_WRITES_CONVERGE = 182
B_WRITES_ANCHOR = 4
B_WRITES_CONVERGE = 5
A_VAL_ANCHOR = 0x00000260
A_VAL_CONVERGE = 0x9F58C4D0
B_VAL_ANCHOR = 0x80000000
B_VAL_CONVERGE = 0x00000000

# ---------------------------------------------------------------- 等形反事实
# (tag, 早补 tuple((addr,val)), 晚补 tuple((addr,val)))
CF_GROUPS = (
    ("BASE",       (),                     ()),
    ("EARLY_A",    ((SRC_A, 0),),          ()),
    ("EARLY_B",    ((SRC_B, 0),),          ()),
    ("EARLY_AB",   ((SRC_A, 0), (SRC_B, 0)), ()),
    ("LATE_A0",    (),                     ((SRC_A, 0),)),
    ("LATE_AB",    ((SRC_B, 0),),          ((SRC_A, 0),)),
    ("LATE_BOTH",  (),                     ((SRC_A, 0), (SRC_B, 0))),
)
CF_RET_85 = 0x000000AB
CF_BASE_NPC = 166
CF_OPEN_NPC = 210
CF_BASE_R4 = 0x0000000A
CF_OPEN_R4 = 0x0000000B
CF_SIDE_NPC = 260
CF_SIDE_R4 = 0x001BDBAE
CF_OPEN_TAGS = ("LATE_AB", "LATE_BOTH")
MACHINE_COUNT = 9            # 1 只观测轨迹机 + 1 标准收敛态机 + 7 等形反事实机
VERDICT = "source_units_located_memory_patch_natural_flow_gates_open"


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """bits 位补码解码。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def disp10_addr(r14, disp10):
    """FR 的 ld/st disp10 形态：r14 + sx8(disp10)*4（fr_emu.py:660）。"""
    return (r14 + sx(disp10, 8) * 4) & 0xFFFFFFFF


def region_kind(a):
    """按段的纯函数分类；不代替 bus.find() 的实测（规矩 80）。"""
    for base, size, kind in (RAM_REGION, KERN_REGION):
        if base <= a < base + size:
            return kind
    if ROM_BASE <= a < ROM_END:
        return "rom"
    return "unmapped"


def is_rom_constant(a):
    """该单元是不是 ROM 常量（V84 两单元实测均为 False）。"""
    return region_kind(a) == "rom"


def static_writer_verdict(static_n, dynamic_n):
    """规矩 80：静态可定址写点 0 而动态写记录非 0 ⇒ 假阴性。"""
    if static_n == 0 and dynamic_n > 0:
        return "static_false_negative"
    if static_n == 0 and dynamic_n == 0:
        return "no_writers_observed"
    return "static_visible"


def writer_class(mn):
    """把写点助记符归到「批量压栈 / 置位 / 清位 / 其它」。"""
    if mn in ("stm0", "stm1"):
        return "batch_push"
    if mn == "or":
        return "bit_set"
    if mn == "and":
        return "bit_clear"
    return "other"


def overlay_verdict(early_val, at_load_val, written_between):
    """规矩 81：早补是否被后续写入覆盖。"""
    if not written_between:
        return "early_survives"
    return "overwritten" if early_val != at_load_val else "same_value_rewrite"


def patch_layer(target):
    """干预层级：寄存器 / 内存单元 / 器件事件。"""
    return {"register": "register", "memory": "memory",
            "device_event": "device_event"}.get(target, "unknown")


def equal_shape(rows):
    """等形三项：boot / hooks / post 全同（V84 用挂钩 PC 集合替代双锚点）。"""
    if not rows:
        return False
    return all(len(set(r.get(k) for r in rows)) == 1
               for k in ("boot", "hooks", "post"))


def gate_verdict(gate1, gate2, chain):
    """两道门 + 命令分发链的三态判定。"""
    if not gate1:
        return "both_closed"
    if not gate2:
        return "gate1_only"
    return "both_open_chain_hit" if chain else "both_open_chain_miss"


def source_verdict(static_zero, dynamic_nonzero, a_is_ram, b_is_ram,
                   early_overwritten, late_ret85, chain_hit, event_located):
    """综合判据：数据源落定 + 内存补丁自然流两门全开，但器件事件仍未定位。"""
    if not (static_zero and dynamic_nonzero and a_is_ram and b_is_ram):
        return "open"
    if not (early_overwritten and late_ret85 and chain_hit):
        return "open"
    if event_located:
        return "event_semantics_located"
    return VERDICT
