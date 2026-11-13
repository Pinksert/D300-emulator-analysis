# -*- coding: utf-8 -*-
"""frbox.svc82 —— 真内核服务分发表索引规整口径与返回码出口（V82，纯函数）。

承接 KNOWN_ISSUES KI#319 第三条与 KI#316–318。V81 证伪了
「0x040660 + svc*2 是 16 位相对偏移表」里的 *svc* 取法（用 8 位无符号 86
算出 0x03D766 落 ROM 外），但留下了 ``0x0406A4 lsl #1,r12`` 这条线索。
V82 用**单步轨迹**把口径实测出来，不是拼一个自洽算法。

真内核链路（单步实测，只读机器状态）
------------------------------------
``int #0x40`` → 向量表 vec 0x40 槽 0x000EFEFC → 陷阱入口 0x040668。

第一次陷阱（boot 早期 0x120926）::
    r12 = 0xFFFFFFAD (-83)
    0x0406A4 lsl #1,r12      → 0xFFFFFF5A
    0x0406A6 ldi:32 #0x040660,r13
    0x0406AC lduh @(r12,r13),r12   读 (0x040660 + 0xFFFFFF5A) & 2^32
                                    = 0x0405BA 的半字 0x087E
    0x0406AE extsh r12        → +2174
    0x0406B0 add r12,r13      → 0x040EDE
    0x0406B2 call @r12        → 实测 0x040EDE

主循环那次（0x175270，boot 第 483841 步）::
    r12 = 0xFFFFFF56 (-170，静态扫描按 8 位叫 86)
    0x040682 lsl #1 / 0x040684 lsr #1（条件执行，清符号位）→ 0x7FFFFF56
    0x0406A4 lsl #1           → 0xFFFFFEAC
    表项槽 = (0x040660 + 0xFFFFFEAC) & 2^32 = 0x04050C，半字 0x158A
    0x0406AE extsh            → +5514
    0x0406B2 call @r12        → 实测 0x041BEA
    0x0406B4 r12 = 0；0x0406BE extsb r12 仍 0
    → 0x175276 mov r12,r4 得 r4==0 → 第一道门关（V81 结论的归因）

要点：``lsl#1`` 会丢掉最高位，所以「0xFFFFFF56」与「0x7FFFFF56」
两种中间值左移后**同为 0xFFFFFEAC**，口径对条件执行稳健；
0x040660 是表的**零点**不是起点，负服务号往低地址索引，
有效表项集中在 0x040500..0x040660；0x0000 / 0x0004 是空槽占位。

全表两口径对照（只读 ROM 实测）
--------------------------------
按 32 位有符号解释：166/256 个目标落 ROM 内、37 个开头像序言；
按 8 位无符号解释：84/256、仅 1 个像序言。
分负号/非负：-256..-1 有效项 134 / 落 ROM 71 / 像序言 57；
0..127 有效项 119 / 落 ROM 29 / 像序言 4。
指向 0x041BEA 的 r12 **只有 -170**（唯一）。

返回码出口与两道门
-------------------
0x041BEA 入口后 0xB6 字节处 ``0x041CA0 ldi:8 #171,r12`` 写 0xAB，
经 0x0406BE extsb 得 **-85**，正是 V80/V81 命令分发段
``0x175740 cmp r4,-85`` 相等才 ``0x17574C call`` 的那个码。
同一紧凑区还有 0x041C98→0、0x041CA6→-17 等写点；
相邻 0x041CA8 是 -44 号入口、0x041DD6 是 -172 号入口，
**多出口 + 相邻服务共享尾部**，本版不宣称这些写点都属于同一函数体。

硬规矩 76（本版新增）
---------------------
报「某服务号对应的内核处理函数」前必须用**真内核口径**（r12 的完整
32 位有符号值）算表项槽，不能拿静态扫描 ``r12 & 0xFF`` 的 8 位分类标签
直算；规矩 74 的两种宽度不是「任选」，真内核用的是 32 位。

硬规矩 77（本版新增）
---------------------
报「返回码出口属于某函数」前先分清相邻服务入口：这一带多个服务
共用同一模板与尾部，表项目标 0x041CA8 / 0x041DD6 都是独立入口；
按规矩 67 不许把一个 `ret` 当函数边界，也不许把相邻函数的写点并进来。

反事实边界
----------
在 0x0406B4 改写 r12、以及所有 raise_irq / 写 flag 位 / 强制设 PC
都是**模型化反事实补全**，不是固件行为、不是固件调用边。
V65–V81 收敛设备策略逐字相同：blive.boot(0) → step(191520) →
单次 raise_irq(0x2B) → run(500000, spin_guard=False)。
V82 的动态轨迹用 blive.boot(0) + step(483841) 抓主循环那次陷阱，
不改 PC、不改寄存器、不注入中断。

仍开放：真机让 vec 0x18 周期性发生的器件/事件（KI#191）、
让 -170 号处理函数走 -85 出口的**器件事件语义**（本版只定位写点，
未证真机触发条件）、两状态字偏移 0 bit3 置位源、vec 0x2D 硬件事件、
317 个提交调用点非根部分的触发任务。

共同禁语：不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
「写回函数没人调用」/「命令分发段坏了」/「补一次 flag 就唤醒成功」/
「86 号服务就是 wai_flg」/「所有服务语义已解出」/「真机已复现 -85」。
"""
import collections

# ---------------------------------------------------------------- 收敛设备策略
BOOT_STEPS = 191520
VEC_BOOT = 0x2B
VEC_TIMER = 0x18
RUN_STEPS = 500000
WINDOW_STEPS = 1000000
TRAP_BOOT_STEP = 483841
TRAP_AFTER_STEPS = 260

# ---------------------------------------------------------------- 向量表 / 陷阱入口
TBR = 0x000EFC00
VEC_TRAP = 0x40
VEC_TRAP_SLOT = 0x000EFEFC
TRAP_ENTRY = 0x00040668
TRAP_CLR_LSL = 0x00040682
TRAP_CLR_LSR = 0x00040684
TRAP_IDX_LSL = 0x000406A4
TRAP_TBL_LDI = 0x000406A6
TRAP_LDUH = 0x000406AC
TRAP_EXTSH = 0x000406AE
TRAP_ADD = 0x000406B0
TRAP_CALL = 0x000406B2
TRAP_RET = 0x000406B4
TRAP_RESTORE = 0x000406B6
TRAP_EXTSB = 0x000406BE
TRAP_RETI = 0x000406E2
TRAP_RESTORE_FN = 0x000404E6
TRAP_NORM_FN = 0x000404E2

# ---------------------------------------------------------------- 分发表口径
TBL_ZERO = 0x00040660
EMPTY_HALVES = (0x0000, 0x0004)
TBL_LO = 0x00040500
TBL_HI = 0x00040660
FIRST_TRAP_STEP = 191279
FIRST_TRAP_R12 = 0xFFFFFFAD
FIRST_TRAP_SLOT = 0x000405BA
FIRST_TRAP_HALF = 0x087E
FIRST_TRAP_OFF = 2174
FIRST_TRAP_TARGET = 0x00040EDE

# ---------------------------------------------------------------- 主循环 / -170 号服务
MAIN_WAIT = 0x00175270
MAIN_NOP = 0x00175272
MAIN_MOV_R12_R4 = 0x00175276
MAIN_CMP0 = 0x00175278
MAIN_BEQ = 0x0017527A
MAIN_JMP = 0x00175282
CMD_SEG = 0x0017573C
CMD_CMP = 0x00175740
CMD_BEQ = 0x00175742
WB_CALL = 0x0017574C
WB_FN = 0x00175908
MAIN_IMM32 = 0xFFFFFF56
MAIN_SVC_32 = -170
MAIN_SVC_8 = 86
MAIN_SVC_LSL1 = 0xFFFFFEAC
MAIN_SVC_CLEARED = 0x7FFFFF56
MAIN_SLOT = 0x0004050C
MAIN_HALF = 0x158A
MAIN_OFF = 5514
MAIN_HANDLER = 0x00041BEA
MAIN_FLGID = 12
MAIN_WAIPTN = 0xA2A05E1C
MAIN_WFMODE = 2
CMD_CODE = -85

# ---------------------------------------------------------------- 全表统计（实测）
CENSUS_32BIT = (166, 37)          # 落 ROM 内 / 像序言（256 个 8 位解释）
CENSUS_8BIT = (84, 1)
NEG_VALID, NEG_INROM, NEG_PRO = 134, 71, 57
POS_VALID, POS_INROM, POS_PRO = 119, 29, 4
HANDLER_170_UNIQUE = (-170,)
ADJACENT_ENTRIES = {0x00041B0A: -46, 0x00041CA8: -44,
                    0x00041DD6: -172, 0x00040F1A: -85,
                    0x00040EDE: -83, 0x0004137E: -37}

# ---------------------------------------------------------------- 返回码写点
R12_WRITES = (
    (0x00041C98, 0x00, 0),
    (0x00041CA0, 0xAB, -85),
    (0x00041CA6, 0xEF, -17),
    (0x00041CDC, 0x00, 0),
    (0x00041D34, 0x00, 0),
    (0x00041D86, 0x00, 0),
    (0x00041DB6, 0x00, 0),
    (0x00041DC0, 0xBB, -69),
    (0x00041DC8, 0xAB, -85),
    (0x00041DCE, 0xDF, -33),
    (0x00041DD4, 0xEF, -17),
)
EXIT_170 = (0x00041C96, 0x00041C9E, 0x00041CA4, 0x00041CDA,
            0x00041D32, 0x00041D84, 0x00041DB4, 0x00041DBA,
            0x00041DBE, 0x00041DC6, 0x00041DCC, 0x00041DD2)

# 静态链地址：报告里出现、但正式结果只打印关键停点的那些 PC。
# 沿用 V81 CHAIN_PCS 的先例，把它们设施化，保证报告每个地址都有据可查。
TRAP_PROLOGUE_PCS = (0x0004066C, 0x00040672, 0x00040696, 0x000406BC)
STALE_SLOT_86 = 0x0004070C         # V81 按 8 位 +86 误算到的槽
STALE_HALF_86 = 0xD106             # 误算读出的半字
STALE_OFF_86 = -12026              # extsh 后的偏移
STALE_TARGET_86 = 0x0003D766       # 误算目标，落 ROM 外
MAIN_BEQ_TARGET = 0x00175284       # 第一道门 r4==0 的回边目标
SCAN_HI = 0x00041E80               # E5 线性扫描上界

# ---------------------------------------------------------------- 反事实四组
CF_GROUPS = (("BASE", None), ("R0", 0x00000000),
             ("R85", 0xFFFFFFAB), ("R17", 0xFFFFFFEF))


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """有符号扩展（与 fr_emu / tick79 同口径）。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def width_pair(imm32):
    """同一个立即数的两种服务号口径：(8 位分类标签, 32 位有符号真值)。"""
    return imm32 & 0xFF, sx(imm32, 32)


def extsh(h):
    """16 位半字按有符号解释。"""
    return sx(h, 16)


def slot_of(svc32):
    """真内核表项槽：``(TBL_ZERO + (r12 << 1)) & 2^32``。

    负服务号往低地址索引；0x040660 是**零点**不是起点。
    """
    return (TBL_ZERO + ((svc32 << 1) & 0xFFFFFFFF)) & 0xFFFFFFFF


def target_of(half):
    """表项 → 处理函数入口：``(TBL_ZERO + extsh(half)) & 2^32``。"""
    return (TBL_ZERO + extsh(half)) & 0xFFFFFFFF


def resolve(rd16, svc32, rom_lo=0x040000, rom_hi=0x740000):
    """给一个「读半字」函数，返回完整链路 (slot, half, off, target, in_rom)。"""
    slot = slot_of(svc32)
    half = rd16(slot) & 0xFFFF
    off = extsh(half)
    target = target_of(half)
    return (slot, half, off, target,
            rom_lo <= target < rom_hi and not (target & 1))


def is_empty(half):
    """0x0000 / 0x0004 是空槽占位，不是服务入口。"""
    return (half & 0xFFFF) in EMPTY_HALVES


def lsl1_stable(*vals):
    """``lsl#1`` 丢最高位 ⇒ 0xFFFFFF56 与 0x7FFFFF56 左移结果相同。"""
    outs = set((v << 1) & 0xFFFFFFFF for v in vals)
    return len(outs) == 1


def equal_shape(rows):
    """等形四项：同 boot 步数 / 同注入次数 / 同后续步数 / 同改写点。"""
    if not rows:
        return False
    keys = ("boot", "inject", "post", "write_pc")
    return all(len(set(r.get(k) for r in rows)) == 1 for k in keys)


def gate_verdict(gate1, cmd_seg, gate2):
    """两道门分离：非 0 开第一道，-85 才开第二道。"""
    if not gate1:
        return "gate1_closed"
    if not cmd_seg:
        return "gate1_open_but_dispatch_unreached"
    if gate2:
        return "both_gates_open"
    return "gate2_code_mismatch"


def svc_verdict(slot_ok, call_ok, ret_zero, cf_both_open, event_located):
    """综合判据：静态口径与动态链都闭合，但真机事件语义仍开放。"""
    if not (slot_ok and call_ok):
        return "open"
    if not ret_zero:
        return "open"
    if event_located:
        return "event_semantics_located"
    return ("kernel_dispatch_resolved_counterfactual_gates_open"
            if cf_both_open else "kernel_dispatch_resolved")
