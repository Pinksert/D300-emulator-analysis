# -*- coding: utf-8 -*-
"""V80：写回段的唯一入口判据、flag 12 通信面、时基「配置 vs 器件」分离（纯函数）。

承接 KNOWN_ISSUES KI#311 第一、二条。V79 补 110 次 ``vec 0x18`` 让清零段
12/12 命中，写回段仍 0/75。V80 静态定位到：

  开机 ICR 配置段 0x040450..0x040468 全命中，0x448 读回 0x12
    ⇒ 固件**已经配好** vec 0x18 的中断级别；基线 TICK_ISR 0 命中
    ⇒ 缺的是周期性拉线的**器件事件**，不是缺配置（KI#191）。

  主循环 0x175270 等待 flag 12（收敛态命中并挂住）
    → 0x17527A beq：返回码 r4 == 0 就跳到 0x175284
    → r4 != 0 才经 0x175282 jmp @r12 进 0x17573C 命令分发段
    → 0x175740 cmp r4, -85 / 0x175742 beq
    → 0x17574C call rel12=221 → 0x175908（**写回函数唯一静态入边**）

  清零段 [0x1758EC,0x175908) 与写回段 [0x175908,0x1759A6) 都引用
  同一块 SLOT8 = 0x9F59F7C8；清零段末尾 0x175904 set_flg(12,1)，
  写回段内 0x17591C / 0x175974 wai_flg(12,1,2)、0x17592C / 0x175984
  clr_flg(12,0xFE)、0x175958 st 写回、0x17598E blt 构成 8 轮 × 12 B 循环。
  两段**不是顺序后继**：清零段 0x175906 ret 回调用者。

规矩 72（附录 S）：报「某 flag 无等待者」前先按服务区分参数寄存器——
``wai_flg``/``twai_flg`` 的 flgid 在 **r5**，``set_flg``/``clr_flg`` 在 **r4**
（``bstatic.CallSite.flgid`` 已设施化）。V80 草稿拿 ``cs.r4 == 12`` 过滤
wai_flg 得 0 处是假阴性；正确口径 12 处，写回段内占 2 处。同理
``ldi:32 #0xFFFFFF56`` 按 32 位是 -170、按 8 位是 +86，宽度口径见 V67。

规矩 73（附录 S）：报「缺真机硬件事件」前分清**配置链**与**器件事件**
（ICR 初始化命中不等于时基已跑）；报「某函数没被调用」前分清
**函数体不可执行**与**唯一入口判据未满足**（强制设 PC 能命中 39/75，
只是模型化反事实，不等于固件自己会调到）。

反事实边界：所有 ``raise_irq(0x18)`` / 直接写 flag 12 位模式 /
强制设 ``cpu.pc = 0x175908`` 都是**反事实补全**，不是固件行为。
``dev_tick18.period`` 是模型参数，不是逆向出的真机频率。
不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
「flag 12 没置所以写回没跑」/「写回函数没人调用」/「补一次 set_flg 就唤醒成功」。
"""

# ---------------------------------------------------------------- 时基配置链
ICR_ADDR = 0x448
ICR_LEVEL = 0x12
ICR_INIT_PC = 0x040456
DIRECT48_PC = 0x04045C
DIRECT4E_PC = 0x040460
BATCH_ICR_PC = 0x040468
TICK_ISR = 0x0004046E
TICK_CALL = 0x00040482
TICK_FN = 0x00040A44
VEC_TIMER = 0x18
VEC_BOOT = 0x2B

# ---------------------------------------------------------------- 主循环 / 命令分发
MAIN_WAIT = 0x00175270
MAIN_BEQ = 0x0017527A
MAIN_BEQ_TARGET = 0x00175284
MAIN_JMP = 0x00175282
CMD_LOAD = 0x0017527C
CMD_SEG = 0x0017573C
CMD_CMP = 0x00175740
CMD_BEQ = 0x00175742
CMD_CODE = -85               # 0xAB；itron.SVC[-85] = sys_dly_tsk
CMD_LDI8 = 171               # ldi:8 #171,r0 → extsb → -85
WB_CALL = 0x0017574C
WB_CALL_REL12 = 221
WB_FN = 0x00175908
WB_END = 0x001759A6
WB_CALL_TARGET_NOTE = "唯一静态入边；七形态全扫后仍只有这一条 call rel12"

# ---------------------------------------------------------------- 清零段 / 写回段
CLEAR_FN = 0x001758EC
CLEAR_ST = 0x001758FA
CLEAR_BGT = 0x001758F8
CLEAR_BGT_TARGET = 0x001758F6
CLEAR_SETFLG = 0x00175904
CLEAR_RET = 0x00175906
CLEAR_SEG_N = 12
WB_SEG_N = 75
WB_ST = 0x00175958
WB_LOOP_BACK = 0x00175930
WB_LOOP_BLT = 0x0017598E
WB_SLOT_STEP = 12
WB_SLOT_BYTES = 96
WB_SLOT_ROUNDS = 8
SLOT8 = 0x9F59F7C8

# ---------------------------------------------------------------- flag 12 通信面
FLAG12 = 12
FLAG12_OBJ = 0x68000968
OBJ_PAT = 8
FLAG12_PAT = FLAG12_OBJ + OBJ_PAT
WAI_FLG = -46
CLR_FLG = -47
SET_FLG = -48
DLY_TSK = -85
WAI12_TOTAL = 12
WAI12_IN_WB = 2
SET12_TOTAL = 49
CLR12_TOTAL = 65
WAI12_IN_WB_PCS = (0x0017591C, 0x00175974)
SETFLG12_CLEAR_PC = CLEAR_SETFLG
SETFLG12_WB_PCS = (0x00175962, 0x00175998)
CLRFLG12_WB_PCS = (0x0017592C, 0x00175984)
#: 静态反汇编落点（草稿 ``scratch_v80/_v80d.out`` / ``_v80e.out`` 实证；
#: 报告里的 <pre> 片段逐条引用）。数值归一后与报告中的六位写法等价。
CHAIN_PCS = (
    0x00175276,   # mov r12,r4 —— 服务返回码进 r4
    0x00175278,   # cmp #0,r4
    0x0017541A,   # 主循环回边（多处 jmp @r12 的目标）
    0x0017573E,   # extsb r0 —— 0xAB → -85
    0x00175744,   # ldi:32 #0x17541A,r12（判据不成立时回主循环）
    0x0017574A,   # jmp @r12
    0x0017574E,   # ldi:32 #0x17541A,r12（WB_CALL 之后回主循环）
    0x00175754,   # jmp @r12
    0x0017593E,   # beq rel9=36 → 0x175988
    0x00175952,   # bls rel9=26 → 0x175988
    0x00175988,   # 写回段内短路落点
)
CHAIN_CALLS = {
    "main_wait": (MAIN_WAIT, 0xFFFFFF56, FLAG12, 0xA2A05E1C, 2),
    "clear_set": (CLEAR_SETFLG, 0xD0, FLAG12, 1, None),
    "wb_wai_1": (0x0017591C, 0xD2, None, FLAG12, 1),
    "wb_clr_1": (0x0017592C, 0xD1, FLAG12, 0xFE, None),
    "wb_set_1": (0x00175962, 0xD0, FLAG12, 1, None),
    "wb_wai_2": (0x00175974, 0xD2, None, FLAG12, 1),
    "wb_clr_2": (0x00175984, 0xD1, FLAG12, 0xFE, None),
    "wb_set_2": (0x00175998, 0xD0, FLAG12, 1, None),
}

# ---------------------------------------------------------------- 设备策略 / 反事实
BOOT_STEPS = 191520
RUN_STEPS = 500000
POST_STEPS = 200000
WINDOW_STEPS = 1000000
CF_STEP = 2000
FORCE_STEPS = 20000
EQUAL_SHAPE_KEYS = ("injections", "post_steps", "write_action")


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """``bits`` 位补码。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def sx8(v):
    return sx(v, 8)


def sx32(v):
    return sx(v, 32)


def svc_num(raw, width=8):
    """按立即数宽度解服务号（V67 口径）。0xFFFFFF56 宽 32 → -170，宽 8 → 86。"""
    return sx(raw, width)


def rel12_target(pc, rel12):
    """``call rel12``：11 位有符号、pc+2 为基准（规矩 70）。"""
    return (pc + 2 + sx(rel12, 11) * 2) & 0xFFFFFFFF


def rel9_target(pc, rel9):
    """``rel9``：8 位有符号、pc+2 为基准（规矩 57）。"""
    return (pc + 2 + sx8(rel9) * 2) & 0xFFFFFFFF


def flg_arg(svc, r4, r5):
    """按服务取 flgid：set/clr 看 r4，wai/twai 看 r5（规矩 72）。"""
    if svc in (SET_FLG, CLR_FLG):
        return r4
    if svc in (WAI_FLG, -170):
        return r5
    return None


def bits_arg(svc, r5, r6):
    """按服务取位模式：set/clr 看 r5，wai/twai 看 r6。"""
    if svc in (SET_FLG, CLR_FLG):
        return r5
    if svc in (WAI_FLG, -170):
        return r6
    return None


def command_gate(r4, code=CMD_CODE):
    """唯一 WB_CALL 的入口判据：返回码等于命令码。"""
    return r4 == code


def loop_verdict(bgt_pc, bgt_rel9, bgt_target, blt_pc, blt_rel9, blt_target,
                 step=WB_SLOT_STEP, total=WB_SLOT_BYTES):
    """清零段 24 轮回跳 / 写回段 8 轮回跳都在各自函数内（规矩 57）。"""
    clear_t = rel9_target(bgt_pc, bgt_rel9)
    wb_t = rel9_target(blt_pc, blt_rel9)
    return {"clear_back": clear_t, "clear_in_fn": clear_t == bgt_target,
            "wb_back": wb_t, "wb_in_fn": wb_t == blt_target,
            "wb_rounds": total // step,
            "all_inside": clear_t == bgt_target and wb_t == blt_target}


def icr_verdict(hits, icr_value, level=ICR_LEVEL):
    """规矩 73：配置链命中且 ICR 值正确，仍不能推出时基已跑。"""
    configured = all(hits.get(k, False) for k in
                     ("ICR_STB", "DIRECT48", "DIRECT4E", "BATCH_ICR"))
    return {"configured": configured, "icr_ok": icr_value == level,
            "isr_hit": bool(hits.get("TICK_ISR", False)),
            "verdict": "config_done_device_event_missing"
            if configured and icr_value == level and not hits.get("TICK_ISR")
            else "inconclusive"}


def equal_shape(rows, keys=EQUAL_SHAPE_KEYS):
    """等形对照：注入次数 / 后续步数 / 写动作次数必须相同。"""
    if not rows:
        return {"equal": False, "reason": "empty"}
    vals = dict((k, [r.get(k) for r in rows]) for k in keys)
    return {"equal": all(len(set(v)) == 1 for v in vals.values()),
            "values": vals}


def attribution(timer_rows, mismatch_rows, key="clock_delta"):
    """归因分离：真时基组非零、错配向量组全 0。"""
    t = [r.get(key, 0) for r in timer_rows]
    m = [r.get(key, 0) for r in mismatch_rows]
    return {"timer": t, "mismatch": m,
            "timer_nonzero": any(x for x in t),
            "mismatch_all_zero": all(x == 0 for x in m),
            "separated": any(x for x in t) and all(x == 0 for x in m)}


def data_gate_verdict(base_wb, write1_wb, write0_wb, pat_after):
    """规矩 53：写 flag 位模式（数据门）对写回段命中无增益。"""
    return {"flag12_set": pat_after == 1,
            "wb_all_zero": base_wb == write1_wb == write0_wb == 0,
            "verdict": "data_write_not_call_event"
            if pat_after == 1 and base_wb == write1_wb == write0_wb == 0
            else "inconclusive"}


def force_entry_verdict(force_wb, force_new, wb_total=WB_SEG_N):
    """模型化强制入口能命中 ≠ 固件自己会调（规矩 73）。"""
    return {"hit": force_wb, "total": wb_total,
            "body_executable": 0 < force_wb <= wb_total,
            "not_full": force_wb < wb_total,
            "verdict": "model_force_is_not_firmware_call"}


def wb_verdict(icr, chain_hits, data_gate, force, attribution):
    """综合判据：静态链完整、动态链未闭合。"""
    static_ok = (chain_hits.get("MAIN_WAIT", False)
                 and chain_hits.get("MAIN_BEQ", False)
                 and not chain_hits.get("MAIN_JMP", True)
                 and not chain_hits.get("WB_CALL", True)
                 and not chain_hits.get("WB_FN", True))
    closed = (chain_hits.get("WB_CALL", False)
              and chain_hits.get("WB_FN", False))
    return {"icr_configured": icr.get("configured", False),
            "static_chain_located": static_ok,
            "attribution_separated": bool(attribution.get("separated", False)),
            "data_gate": data_gate.get("verdict"),
            "force_entry": force.get("verdict"),
            "chain_closed": closed,
            "verdict": "wb_chain_located_but_command_dispatch_not_reached"
            if static_ok and not closed else "inconclusive"}


__all__ = [k for k in globals() if not k.startswith("_")]
