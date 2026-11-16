# -*- coding: utf-8 -*-
"""V90 设施：0x83E0 参数归属的地址错配纠正、r7 参数读点普查、收敛态可见性。

承接 KI#351 的第一条开放项（``0x15FA6C`` 子例程是否把 ``0x83E0`` 当地址用）。
本版只结**结构上可证**的部分，并纠正 V89 的一处十进制↔十六进制错配：

1. **地址错配纠正**。三个调用点的 ``ldi:32`` 立即数是 ``1440812``，
   精确换算是 ``0x15FC2C``（不是 V89 记的 ``0x15FA6C``=1440364）。
   ``0x15FC2C`` 处有标准函数序言 ``stm0 / stm1 / st rs2=1 / enter``；
   全 ROM 里 ``ldi:32 #1440812`` 有 545 条，``ldi:32 #1440364`` 有 **0** 条。
   差 ``0x1C0``（448 字节）⇒ V89 的归属落在了函数中部，结论无效。
2. **r7 参数未被读取**。真入口函数体 ``0x15FC2C..0x15FCB4``（第一个 ret）
   内引用 r7 的指令只有 1 条 ``0x15FC72 ldi:8 #0``（清零，不是读）；
   读内存指令的基址寄存器集合是 {r0,r1,r9,r10,r11,r12}，**不含 r7**。
   动态反事实（模型动作：强制 PC 到入口 + 设 r7=0x83E0）37 步轨迹里 r7
   从入口保持 ``0x83E0`` 直到 ``0x15FC72`` 被清零，全程未被读取 ⇒
   ``parameter_cleared_not_read_as_address``。
3. **阴性结果与边界**。``ldi:20 #0x83E0`` 全 ROM 恰好 3 条（ri 都是 r7），
   正是那三个调用点。它们在标准收敛态机（V65–V89 逐字相同的设备策略）
   500000 步 coverage **全 False** ⇒ 现有收敛策略下无法动态命中，
   只能报结构结论，不得外推真机语义。强制 PC 跳转是**模型动作**，
   r14/r15 用的是陷阱口值而非真实调用点栈帧，轨迹只用于指令流对照。

新增规矩 93（报「子例程把某常量当地址用」前，先把 call 目标立即数按十进制
精确换算成十六进制入口，不得沿用上一版的错配注释；再做函数体内参数寄存器
读点普查 + 动态常量传播跟踪；调用点 coverage 全 False 时只报结构结论）。

**边界**：强制 PC、写 r7 都是**反事实补全**（模型动作），不是固件行为、
不是真机器件事件。本版结的是地址口径、指令流结构与可见性，
不是器件事件语义，**链未闭合**。
不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / 所有服务语义已解出 /
真机已复现 −85 / 「补一次值就等于真机事件」/「0x15FA6C 是目标地址」/
「服务号槽被该子例程读取」/「0x83E0 当地址用了」。
"""

M32 = 0xFFFFFFFF

# ---------------------------------------------------------------- 收敛锚点（V65–V89 逐字相同）
BOOT_STEP = 483841
TRAP = 0x040668
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC

# ---------------------------------------------------------------- 地址错配纠正
I32_TRUE_TARGET = 1440812            # 三个 call:d 的立即数
TARGET_HEX = 0x15FC2C                # 1440812 的精确十六进制
V89_SUB_ADDR = 0x15FA6C              # V89 记错的地址
V89_SUB_DEC = 1440364                # = 0x15FA6C
ADDR_DELTA = 448                     # 0x1C0
CALL_SITE_COUNT = 545                # ldi:32 #1440812 全 ROM 条数
WRONG_TARGET_REF_COUNT = 0           # ldi:32 #1440364 全 ROM 条数
PROLOGUE = (
    (0x15FC2C, "stm0"),
    (0x15FC2E, "stm1"),
    (0x15FC30, "st rs2=1"),
    (0x15FC32, "enter"),
)
PROLOGUE_LEN = 4

# ---------------------------------------------------------------- 函数体与 r7 读点普查
SUB_ENTRY = 0x15FC2C
SUB_FIRST_RET = 0x15FCB4
SUB_BYTES = 0x88                     # 136
R7_CLEAR_PC = 0x15FC72
R7_CLEAR_I8 = 0
R7_REF_COUNT = 1                     # 函数体内引用 r7 的指令数
R7_READ_COUNT = 0                    # 其中读 r7 的条数
MEM_BASE_REGS = (0, 1, 9, 10, 11, 12)
MEM_READ_COUNT = 18                  # 函数体内 ld/st/lduh/ldub/sth/stb 合计（按指令计数）
R7_IN_MEM_BASE = False

# ---------------------------------------------------------------- 立即数 / 调用点
I20_83E0 = 33760                     # 0x83E0
I20_83E0_COUNT = 3
I20_83E0_PCS = (0x246E46, 0x25F1CC, 0x25F346)
I20_RI = 7
CALL_SEQ = ("ldi:20 #0x83E0 r7", "ldi:32 #1440812 r12", "call:d @r12")
CONVERGE_HIT_COUNT = 0               # 三调用点在标准收敛态 coverage 命中数

# ---------------------------------------------------------------- 动态反事实（模型动作）
CF_R7_INIT = 0x000083E0
CF_R7_AFTER = 0x00000000
CF_TRACE_STEPS = 37
CF_LAST_PC = 0x15FC82
CF_MACHINE_COUNT = 3                 # CF_83E0 / CF_ZERO / OBS
CONVERGE_MACHINE_COUNT = 1
MACHINE_TOTAL = 4

VERDICT = "call_target_hex_fix_r7_param_cleared_not_read_convergence_unreachable"
VERDICT_OPEN = "open"


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def extsb8(i8):
    return sx(i8 & 0xFF, 8)


def call_target_verdict(i32, recorded):
    """十进制立即数与上一版记录的十六进制入口是否错配。"""
    if i32 != recorded:
        return "recorded_target_dec_hex_mismatch"
    return "recorded_target_ok"


def addr_delta(i32, recorded):
    return abs(i32 - recorded)


def r7_usage_verdict(r7_refs, r7_reads, r7_in_mem_base):
    """规矩 93：参数寄存器被清零且不作内存基址 ⇒ 不当地址用。"""
    if r7_refs > 0 and r7_reads == 0 and not r7_in_mem_base:
        return "parameter_cleared_not_read_as_address"
    return "unresolved"


def converge_visibility(hits):
    """调用点在标准收敛态机是否可见。"""
    n = sum(1 for h in hits if h)
    if n == 0:
        return "call_sites_not_reachable_in_canonical_convergence"
    if n == len(hits):
        return "call_sites_all_visible"
    return "call_sites_partially_visible"


def constant_propagation(r7_init, clear_i8, r7_after):
    """动态反事实里 r7 是否被清零覆盖。"""
    if r7_init != 0 and (clear_i8 & 0xFF) == 0 and r7_after == 0:
        return "r7_overwritten_by_clear_not_dereferenced"
    return "unresolved"


def source_verdict(target_ok, r7_ok, vis_ok, prop_ok, event_located=False):
    """综合判据：链未闭合（真机器件事件语义仍未定位）。"""
    if not (target_ok and r7_ok and vis_ok and prop_ok):
        return VERDICT_OPEN
    if event_located:
        return "event_semantics_located"
    return VERDICT
