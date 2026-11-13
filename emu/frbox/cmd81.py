# -*- coding: utf-8 -*-
"""frbox.cmd81 —— 主循环返回码与命令分发段的「两道串联判据」（V81，纯函数）。

承接 KNOWN_ISSUES KI#315 第二条 / KI#312–315。V80 已定位写回函数
0x175908 的唯一入边 0x17574C call rel12=221 与第二道门 cmp r4,-85；
V81 补齐它上游的第一道门，并证伪「补 flag 位模式能让链往下走」。

静态链（只读 ROM 实测）
----------------------
主循环 0x175270 int #0x40，服务号立即数 ldi:32 #0xFFFFFF56：
  * 按 8 位补码读 = +86（bstatic.Sweep.calls 的 cs.svc 口径）
  * 按 32 位补码读 = -170
参数 r5=12（flgid）/ r6=0xA2A05E1C（位模式）/ r7=2；
全 ROM 同形态调用点 91 处、57 种 (r5,r6,r7) 三元组。
返回后 0x175276 mov r12,r4 → 0x175278 cmp #0,r4 → 0x17527A beq rel9=4
跳 0x175284（r4==0）；非 0 才 0x17527C ldi:32 #0x17573C,r12 →
0x175282 jmp @r12 进命令分发段。

命令分发段 0x17573C 是**单码分发器**：
  0x17573C ldi:8 #171,r0 → 0x17573E extsb r0 → -85；
  0x175740 cmp r4,-85 / 0x175742 beq → 0x17574C call rel12=221
  才调 0x175908；不等则 0x175744 ldi:32 #0x17525A,r12 →
  0x17574A jmp @r12 回主循环。

收敛态第一道门 r4 恒 0（MAIN_WAIT / MAIN_BEQ 命中，MAIN_JMP 0 命中），
所以命令分发段一次都没进过——这是写回段 0/75 的直接原因，
不是函数体坏（强制设 pc + r4=-85 时写回段 11/75 命中），
也不是 flag 12 没置（等形补位四组 MAIN_JMP 全 False）。

硬规矩 74（本版新增）
--------------------
同一个立即数 0xFFFFFF56 在静态扫描与真内核可以是两种宽度口径（86 / -170）。
报「服务号 86」必须声明宽度；itron.SVC 半主机表不含 86，
**不能**拿它判断固件真内核（blive.boot(0) 的 svc_hook=None）的 86 号服务行为。
「0x040660 是 16 位相对偏移分发表」的假设已被证伪：86 号表项 0xD106
按 extsh = -12026，目标 0x03D766 落在 ROM 外；索引用的是规整后的值，
口径未闭合，保持为开放项，不编一个能自洽的算法出来。

硬规矩 75（本版新增）
--------------------
报「补位无效」前先证补位**发生在等待之前**且等形（同 boot、
同注入次数、同 post 步数），并证补位真的改变了执行（新增 PC 读数变化）。
写数据门 ≠ 给等待事件（规矩 53）；「命令分发段没进」要分清
第一道门（r4 非 0）与第二道门（r4 == -85），不能只报后者。

反事实边界
----------
所有 raise_irq(0x18) / 直接写 flag 12 位模式 / 强制设 cpu.pc 与 cpu.r[4]
都是**模型化反事实补全**，不是固件行为，也不是固件调用边。
V65–V80 收敛设备策略逐字相同：blive.boot(0) → step(191520) →
单次 raise_irq(0x2B) → run(500000, spin_guard=False)。

仍开放：真机让 vec 0x18 周期性发生的器件/事件（KI#191）、
让 86 号服务返回非 0 码的事件语义、真内核服务分发表索引规整口径、
两状态字偏移 0 bit3 置位源、vec 0x2D 硬件事件、
317 个提交调用点非根部分的触发任务。

共同禁语：不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
「写回函数没人调用」/「命令分发段坏了」/「补一次 flag 就唤醒成功」/
「86 号服务就是 wai_flg」/「分发表算法已经解出」。
"""
import collections

# ---------------------------------------------------------------- 收敛设备策略
BOOT_STEPS = 191520
VEC_BOOT = 0x2B
VEC_TIMER = 0x18
RUN_STEPS = 500000
WINDOW_STEPS = 1000000
POST_STEPS = 200000
FORCE_STEPS = 20000
INJECT_TIMES = 110
COV_BASE = 5439
SLOT8 = 0x9F59F7C8

# ---------------------------------------------------------------- 主循环 / 86 号服务
MAIN_WAIT = 0x00175270
MAIN_NOP = 0x00175272
MAIN_ST_R4 = 0x00175274
MAIN_MOV_R12_R4 = 0x00175276
MAIN_CMP0 = 0x00175278
MAIN_BEQ = 0x0017527A
MAIN_BEQ_REL9 = 4
MAIN_BEQ_TARGET = 0x00175284
CMD_LOAD = 0x0017527C
CMD_LOAD_IMM32 = 1529660          # = 0x0017573C
MAIN_JMP = 0x00175282
MAIN_SVC_IMM32 = 0xFFFFFF56
MAIN_SVC_8 = 86
MAIN_SVC_32 = -170
MAIN_FLGID = 12
MAIN_WAIPTN = 0xA2A05E1C
MAIN_WFMODE = 2
MAIN_R13_SET = 0x00175268

SVC86_CALLS = 91
SVC86_TRIPLES = 57
ALL_CALLS = 2297
ALL_SVC_KINDS = 25
SVC_NAMED = (-85, -63, -61, -55, -53, -48, -47, -46,
             -44, -40, -39, -38, -37, -25, -23, -21)
SVC86_TOP_TRIPLE = (12, 262144, 2)
SVC86_TOP_TRIPLE_N = 6
SVC86_FIXED_R7 = 2

# ---------------------------------------------------------------- 命令分发段（单码分发器）
CMD_SEG = 0x0017573C
CMD_LDI8 = 171
CMD_LDI8_PC = 0x0017573C
CMD_EXTSB_PC = 0x0017573E
CMD_CMP = 0x00175740
CMD_BEQ = 0x00175742
CMD_BEQ_REL9 = 4
CMD_CODE = -85
CMD_MISS_LDI = 0x00175744
CMD_MISS_JMP = 0x0017574A
CMD_BACK_IMM32 = 1528410          # = 0x0017525A
CMD_BACK_TARGET = 0x0017525A
WB_CALL = 0x0017574C
WB_CALL_REL12 = 221
WB_FN = 0x00175908
WB_END = 0x001759A6
WB_SEG_N = 75
CLEAR_FN = 0x001758EC
CLEAR_SEG_N = 12

# ---------------------------------------------------------------- 真内核陷阱与分发表假设
KERNEL_ENTRY = 0x040668
KERNEL_SVC_LSL1 = 0x0406A4
KERNEL_SVC_LDUH = 0x0406AC
KERNEL_SVC_EXTSH = 0x0406AE
KERNEL_SVC_ADD = 0x0406B0
KERNEL_SVC_CALL = 0x0406B2
KERNEL_EXIT_EXTSB = 0x0406BE
KERNEL_RETI = 0x0406E2
SVC_TABLE_BASE = 0x040660
SVC86_TABLE_HALF = 0xD106
SVC86_TABLE_SIGNED = -12026
SVC86_TABLE_TARGET = 0x03D766
ROM_LO = 0x040000

# ---------------------------------------------------------------- 等形补位反事实
FLAG12_OBJ = 0x68000968
OBJ_PAT = 8
FLAG12_PAT = FLAG12_OBJ + OBJ_PAT
PATCH_PATS = (0x00000000, 0xA2A05E1C, 0xFFFFFFFF, 0x00000001)
PATCH_NPC = (2291, 2383, 2383, 2291)
PATCH_READBACK = (0x00000001, 0x02205E19, 0x5F7FFFFB, 0x00000001)
PATCH_JMP_HITS = (False, False, False, False)
PATCH_CLEAR_HITS = (12, 12, 12, 12)
PATCH_WB_HITS = (0, 0, 0, 0)
PATCH_TAGS = ("P0", "P1", "P2", "P3")
PATCH_EQUAL_SHAPE = True
MIS_TAG = "MIS110"
MIS_NPC = 0

# ---------------------------------------------------------------- 两道门归因反事实
Q_CASES = (
    ("Q0_r4_zero", 0x0017527C, 0x00000000, 11, 0, False, True),
    ("Q1_r4_neg85", 0x0017527C, 0xFFFFFFAB, 28, 11, True, True),
    ("Q2_cmdseg", 0x0017573C, 0xFFFFFFAB, 26, 11, True, False),
    ("Q3_other", 0x0017527C, 0xFFFFFFFD, 11, 0, False, True),
)
Q_EQUAL_SHAPE = True

# ---------------------------------------------------------------- 静态链常量（报告地址落点）
CHAIN_PCS = {
    "main_r13": MAIN_R13_SET,
    "main_wait": MAIN_WAIT,
    "main_mov": MAIN_MOV_R12_R4,
    "main_cmp0": MAIN_CMP0,
    "main_beq": MAIN_BEQ,
    "main_beq_target": MAIN_BEQ_TARGET,
    "cmd_load": CMD_LOAD,
    "main_jmp": MAIN_JMP,
    "cmd_seg": CMD_SEG,
    "cmd_extsb": CMD_EXTSB_PC,
    "cmd_cmp": CMD_CMP,
    "cmd_beq": CMD_BEQ,
    "cmd_miss_ldi": CMD_MISS_LDI,
    "cmd_miss_jmp": CMD_MISS_JMP,
    "cmd_back": CMD_BACK_TARGET,
    "wb_call": WB_CALL,
    "wb_fn": WB_FN,
    "kernel_entry": KERNEL_ENTRY,
    "kernel_lduh": KERNEL_SVC_LDUH,
    "kernel_extsb": KERNEL_EXIT_EXTSB,
    "kernel_reti": KERNEL_RETI,
}


def sx(v, bits):
    """bits 位补码。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def imm_code(imm32, bits=8):
    """从 32 位立即数按 bits 宽度读补码（规矩 74）。"""
    return sx(imm32, bits)


def svc_pair(imm32=MAIN_SVC_IMM32):
    """同一立即数的 (8 位口径, 32 位口径)。"""
    return (sx(imm32, 8), sx(imm32, 32))


def rel9_target(pc, rel9):
    """FR rel9：pc+2+sx8(rel9)*2。"""
    return (pc + 2 + sx(rel9, 8) * 2) & 0xFFFFFFFF


def rel12_target(pc, rel12):
    """FR call rel12：pc+2+sx11(rel12)*2。"""
    return (pc + 2 + sx(rel12, 11) * 2) & 0xFFFFFFFF


def two_gate(r4, entered, dispatched):
    """两道串联判据：(第一道 r4!=0, 第二道 r4==-85, 综合结论)。

    entered / dispatched 是覆盖率命中，用于把「静态判据」与「实际走到」
    分开；函数体可执行 ≠ 固件自己调用。
    """
    g1 = r4 != 0
    g2 = r4 == CMD_CODE
    if not g1:
        verdict = "gate1_zero_return_code"
    elif not g2:
        verdict = "gate2_other_code"
    else:
        verdict = "dispatch_to_writeback"
    return (g1, g2, verdict)


def single_code(codes):
    """命令分发段只认一个码：返回 (是否单码, 该码)。"""
    uniq = sorted(set(codes))
    return (len(uniq) == 1, uniq[0] if len(uniq) == 1 else None)


def equal_shape(rows):
    """等形：预热 / 服务次数 / 后续步数三项全同（V68 规矩）。"""
    if not rows:
        return False
    keys = ("warmup", "service_calls", "post_steps")
    return all(len(set(r[k] for r in rows)) == 1 for k in keys)


def patch_timing_verdict(before_hits, after_hits, npc_before, npc_after):
    """补位时机判据：写位在等待之前才有资格谈「补位无效」；
    还要证明补位真的改变了执行（新增 PC 不同），否则是无效对照。
    """
    changed = npc_before != npc_after
    return {
        "before_wait": True,
        "execution_changed": changed,
        "hit_delta": dict(
            (k, int(after_hits.get(k, False)) - int(before_hits.get(k, False)))
            for k in set(list(before_hits) + list(after_hits))),
        "verdict": ("patch_changed_execution_but_not_command_path"
                    if changed else "patch_had_no_effect__not_a_valid_control"),
    }


def attribution(pos_rows, neg_rows, key="npc"):
    """归因分离：实验组有增量、等形对照组无增量，才算归因成立。"""
    pos = max((r[key] for r in pos_rows), default=0)
    neg = max((r[key] for r in neg_rows), default=0)
    return (pos > neg, pos, neg)


def table_offset_verdict(half, base=SVC_TABLE_BASE, rom_lo=ROM_LO):
    """16 位相对偏移分发表假设的证伪判据：目标落在 ROM 外则假设不成立。"""
    s = sx(half, 16)
    tgt = (base + s) & 0xFFFFFFFF
    return (tgt >= rom_lo, s, tgt)


def triple_census(calls):
    """86 号服务调用点 (r5,r6,r7) 三元组普查。"""
    c = collections.Counter((x[0], x[1], x[2]) for x in calls)
    return (len(calls), len(c), c.most_common(1)[0] if c else None)


def cmd_verdict(gate1_hit, gate2_hit, wb_hit, patch_gain=0):
    """综合判据：链未闭合的归因落到第一道门。

    patch_gain 是「补位后命令路径命中的增量」，只作记录：
    补位改变了执行轨迹（新增 PC 不同）不等于命令路径有增益；
    只要两道门都没进，一律判命令分发段没走到（规矩 75）。
    """
    _ = patch_gain
    if wb_hit:
        return "writeback_reached_by_counterfactual_only"
    if gate2_hit and not wb_hit:
        return "gate2_reached_but_code_mismatch"
    if gate1_hit and not gate2_hit:
        return "gate1_reached_but_second_gate_closed"
    return "wb_chain_located_but_command_dispatch_not_reached"


__all__ = [k for k in list(globals()) if k.isupper()] + [
    "sx", "imm_code", "svc_pair", "rel9_target", "rel12_target",
    "two_gate", "single_code", "equal_shape", "patch_timing_verdict",
    "attribution", "table_offset_verdict", "triple_census", "cmd_verdict",
]
