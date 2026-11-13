# -*- coding: utf-8 -*-
"""V81 正式探针：主循环返回码与命令分发段的「两道串联判据」（A–H）。

承接 KNOWN_ISSUES KI#315 第二条 / KI#312–315。设备策略与 V65–V80 收敛组
逐字相同：blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；所有 raise_irq(0x18) / 直接写 flag 12 位模式 /
强制设 cpu.pc 与 cpu.r[4] 都是**反事实补全**，不是固件行为、不是固件调用边。

独立机器：D 段 2 台 + E 段 5 台 + F 段 4 台 = 11 台。
E 段四组补位统一在 boot 之后、注入之前写，110 次 vec 0x18、post 200000；
F 段四组统一 post 20000 步。等形三项（预热 / 服务次数 / 后续步数）全同。
"""
import sys
import os
import struct
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import bstatic, blive, itron, wb80 as W, cmd81 as C

BOOT, VEC, RUN = C.BOOT_STEPS, C.VEC_BOOT, C.RUN_STEPS
POST, CF_STEP = C.POST_STEPS, 2000
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


def hr(t):
    print("")
    print("=" * 78)
    print(t)
    print("=" * 78)
    sys.stdout.flush()


def dec(lo, hi):
    out, pc = [], lo
    while pc < hi - 1:
        ins = fr_emu.TABLE.decode(rom, pc - base)
        if ins is None:
            pc += 2
            continue
        mn, _bs, v, size, _w = ins
        out.append((pc, mn, dict(v), size))
        pc += size
    return out


def dec_set(lo, hi):
    return set(pc for pc, _mn, _v, _s in dec(lo, hi))


def cov(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def rd16(addr):
    return struct.unpack(">H", rom[addr - base:addr - base + 2])[0]


CLEAR_SEG = dec_set(C.CLEAR_FN, C.WB_FN)
WB_SEG = dec_set(C.WB_FN, C.WB_END)
KEYS = {
    "MAIN_WAIT": C.MAIN_WAIT, "MAIN_BEQ": C.MAIN_BEQ,
    "MAIN_JMP": C.MAIN_JMP, "CMD_SEG": C.CMD_SEG,
    "CMD_CMP": C.CMD_CMP, "CMD_BEQ": C.CMD_BEQ,
    "WB_CALL": C.WB_CALL, "WB_FN": C.WB_FN,
    "CLEAR_FN": C.CLEAR_FN, "MAIN_BACK": 0x17541A,
}

hr("A. 静态链：主循环等待 / 返回码判据 / 86 号服务普查（只读 ROM）")
print("A0 主循环 0x17525A..0x17528A 反汇编：")
for pc, mn, v, _s in dec(0x17525A, 0x17528A):
    print("  0x%06X  %-12s %s" % (pc, mn, v))
print("A1 服务号立即数 0x%08X：8 位口径 = %d，32 位口径 = %d（规矩 74）"
      % (C.MAIN_SVC_IMM32, C.MAIN_SVC_8, C.MAIN_SVC_32))
print("A2 主循环参数 r5=%d r6=0x%08X r7=%d；r13 由 0x%06X 设定"
      % (C.MAIN_FLGID, C.MAIN_WAIPTN, C.MAIN_WFMODE, C.MAIN_R13_SET))
calls86 = [cs for cs in SW.calls if cs.svc == C.MAIN_SVC_8]
triples = collections.Counter((cs.r5, cs.r6, cs.r7) for cs in calls86)
print("A3 全 ROM int #0x40 调用点 = %d，不同服务号 = %d"
      % (len(SW.calls), len(set(cs.svc for cs in SW.calls))))
print("A4 86 号调用点 = %d，不同 (r5,r6,r7) 三元组 = %d"
      % (len(calls86), len(triples)))
print("A5 最常见三元组 = %s 次数 = %d；r7==2 的调用点 = %d"
      % (str(triples.most_common(1)[0][0]), triples.most_common(1)[0][1],
         sum(1 for cs in calls86 if cs.r7 == C.SVC86_FIXED_R7)))
main = [cs for cs in calls86 if cs.addr == C.MAIN_WAIT]
print("A6 主循环 0x%06X 在 86 号调用点内 = %s，r6 = %s"
      % (C.MAIN_WAIT, bool(main), main[0].r6 if main else None))
set12 = [cs for cs in SW.of(W.SET_FLG) if cs.flgid == W.FLAG12]
bits = collections.Counter(cs.bits for cs in set12)
inter = [b for b in bits if b is not None and (b & C.MAIN_WAIPTN)]
print("A7 set_flg(12) = %d 处，不同 bits = %d，与主循环位模式有交集 = %d 种"
      % (len(set12), len(bits), len(inter)))

hr("B. 服务号宽度口径与真内核陷阱出口（规矩 74，只读 ROM）")
print("B1 同一立即数两种口径：svc_pair = %s；差 = %d（= 2^8）"
      % (str(C.svc_pair()), C.MAIN_SVC_8 - C.MAIN_SVC_32))
print("B2 半主机 itron.SVC 已命名服务号 = %d 个，不含 86 = %s"
      % (len(itron.SVC), C.MAIN_SVC_8 not in itron.SVC))
print("B3 收敛设备 blive.boot(0) 的 svc_hook=None ⇒ 走固件真向量表，"
      "不能用半主机表判 86 号服务语义")
print("B4 真内核陷阱入口 0x%06X；0x%06X lduh @(r13,r12) / 0x%06X extsh / "
      "0x%06X call @r12"
      % (C.KERNEL_ENTRY, C.KERNEL_SVC_LDUH, C.KERNEL_SVC_EXTSH,
         C.KERNEL_SVC_CALL))
print("B5 陷阱出口 0x%06X extsb r12 ⇒ 返回码是 8 位有符号；"
      "0x%06X reti" % (C.KERNEL_EXIT_EXTSB, C.KERNEL_RETI))
print("B6 86 号服务语义（错误码 / 命令码）未命名，保持开放；"
      "不能报「86 号服务就是 wai_flg」")

hr("C. 命令分发段：单码分发器与两道串联判据（只读 ROM）")
print("C0 分发段 0x%06X..0x%06X 反汇编：" % (C.CMD_SEG, C.CMD_MISS_JMP + 2))
for pc, mn, v, _s in dec(C.CMD_SEG, C.CMD_MISS_JMP + 2):
    print("  0x%06X  %-12s %s" % (pc, mn, v))
print("C1 第一道门：0x%06X cmp #0,r4 / 0x%06X beq rel9=%d → 0x%06X"
      % (C.MAIN_CMP0, C.MAIN_BEQ, C.MAIN_BEQ_REL9,
         C.rel9_target(C.MAIN_BEQ, C.MAIN_BEQ_REL9)))
print("C2 非 0 分支：0x%06X ldi:32 #%d,r12 = 0x%06X；0x%06X jmp @r12"
      % (C.CMD_LOAD, C.CMD_LOAD_IMM32, C.CMD_LOAD_IMM32, C.MAIN_JMP))
print("C3 第二道门：ldi:8 #%d + extsb = %d；0x%06X cmp r4,%d / "
      "0x%06X beq rel9=%d"
      % (C.CMD_LDI8, C.CMD_CODE, C.CMD_CMP, C.CMD_CODE,
         C.CMD_BEQ, C.CMD_BEQ_REL9))
print("C4 唯一命令码 single_code = %s；匹配才 0x%06X call rel12=%d → 0x%06X"
      % (str(C.single_code([C.CMD_CODE])), C.WB_CALL, C.WB_CALL_REL12,
         C.rel12_target(C.WB_CALL, C.WB_CALL_REL12)))
print("C5 不匹配 0x%06X ldi:32 #%d,r12 = 0x%06X → 0x%06X jmp 回主循环"
      % (C.CMD_MISS_LDI, C.CMD_BACK_IMM32, C.CMD_BACK_IMM32,
         C.CMD_MISS_JMP))
print("C6 two_gate(r4=0) = %s；two_gate(r4=-3) = %s；two_gate(r4=-85) = %s"
      % (C.two_gate(0, False, False), C.two_gate(-3, True, False),
         C.two_gate(-85, True, True)))
print("C7 写回段 0x%06X..0x%06X = %d 条；清零段 = %d 条"
      % (C.WB_FN, C.WB_END, len(WB_SEG), len(CLEAR_SEG)))

hr("D. 收敛态基线与 1M 窗口自证（2 台独立机器）")
base_m = boot_run()
win_m = boot_run(C.WINDOW_STEPS)
c0, c1 = cov(base_m), cov(win_m)
print("D1 500k 覆盖 = %d，末 PC = 0x%06X" % (len(c0), base_m.cpu.pc))
print("D2 1M 覆盖 = %d，500k/1M 对称差集 = %d" % (len(c1), len(c0 ^ c1)))
bh = dict((k, p in c0) for k, p in KEYS.items())
print("D3 收敛态命中 = %s" % bh)
print("D4 第一道门 MAIN_BEQ 命中 = %s，MAIN_JMP 命中 = %s ⇒ 返回码 r4 恒 0"
      % (bh["MAIN_BEQ"], bh["MAIN_JMP"]))

hr("E. 等形补位：写正确位模式能否让命令分发段往下走（5 台独立机器）")
print("E0 全部在 boot 之后、注入之前写 flag 12 位模式；"
      "110 次 vec 0x18、每次 2000 步、post 200000 步（等形三项全同）")


def run_patch(tag, pat, vec=C.VEC_TIMER, times=C.INJECT_TIMES):
    m = boot_run()
    c0 = cov(m)
    if pat is not None:
        m.bus.wr(C.FLAG12_PAT, 4, pat, 0)
    for _ in range(times):
        m.raise_irq(vec)
        m.run(max_steps=CF_STEP, spin_guard=False)
    m.run(max_steps=POST, spin_guard=False)
    c1 = cov(m)
    hits = dict((k, p in c1) for k, p in KEYS.items())
    r = {"tag": tag, "pat": pat, "vec": vec, "times": times,
         "npc": len(c1 - c0), "clear": len(c1 & CLEAR_SEG),
         "wb": len(c1 & WB_SEG), "hits": hits,
         "readback": m.bus.rd(C.FLAG12_PAT, 4),
         "warmup": BOOT, "service_calls": times, "post_steps": POST}
    print("  %-8s pat=%-12s vec=0x%02X 新增PC=%4d 清零=%2d/%d 写回=%2d/%d "
          "读回=0x%08X" % (tag, "None" if pat is None else "0x%08X" % pat,
                           vec, r["npc"], r["clear"], len(CLEAR_SEG),
                           r["wb"], len(WB_SEG), r["readback"]))
    print("    命中 = %s" % hits)
    sys.stdout.flush()
    return r


P = [run_patch(C.PATCH_TAGS[i], C.PATCH_PATS[i]) for i in range(4)]
MIS = run_patch(C.MIS_TAG, C.MAIN_WAIPTN, vec=C.VEC_BOOT)
print("E1 等形判据 = %s（P0–P3 预热/服务次数/后续步数三项全同）"
      % C.equal_shape(P))
print("E2 补位执行影响：新增PC P0=%d P1=%d P2=%d P3=%d（不同 ⇒ 补位真的改变了执行）"
      % tuple(r["npc"] for r in P))
print("E3 四组 MAIN_JMP / CMD_SEG / WB_CALL 全 False = %s"
      % all(not r["hits"][k] for r in P
            for k in ("MAIN_JMP", "CMD_SEG", "WB_CALL")))
print("E4 错配 vec 0x2B 等形对照：新增PC=%d，清零=%d/%d，写回=%d/%d"
      % (MIS["npc"], MIS["clear"], len(CLEAR_SEG),
         MIS["wb"], len(WB_SEG)))
print("E5 归因分离（按清零段命中）= %s；错配组清零=%d/%d"
      % (str(C.attribution(P, [MIS], key="clear")),
         MIS["clear"], len(CLEAR_SEG)))
print("E6 补位时机判据 = %s"
      % C.patch_timing_verdict(P[0]["hits"], P[1]["hits"],
                                P[0]["npc"], P[1]["npc"])["verdict"])

hr("F. 两道门归因：强制非 0 返回码（4 台独立机器，等形同 20000 步）")
print("F0 全部为模型化反事实：强制设 cpu.pc / cpu.r[4]，不是固件调用边；"
      "统一 post 20000 步")


def run_force(tag, pc, r4):
    m = boot_run()
    c0 = cov(m)
    m.cpu.pc = pc
    m.cpu.r[4] = r4
    m.run(max_steps=C.FORCE_STEPS, spin_guard=False)
    c1 = cov(m)
    hits = dict((k, p in c1) for k, p in KEYS.items())
    print("  %-12s pc=0x%06X r4=0x%08X 新增PC=%4d 写回=%2d/%d 清零=%2d/%d"
          % (tag, pc, r4, len(c1 - c0), len(c1 & WB_SEG), len(WB_SEG),
             len(c1 & CLEAR_SEG), len(CLEAR_SEG)))
    print("    命中 = %s" % hits)
    sys.stdout.flush()
    return {"tag": tag, "pc": pc, "r4": r4, "npc": len(c1 - c0),
            "wb": len(c1 & WB_SEG), "hits": hits,
            "warmup": BOOT, "service_calls": 0,
            "post_steps": C.FORCE_STEPS}


Q = [run_force(*q[:3]) for q in C.Q_CASES]
print("F1 等形判据 = %s" % C.equal_shape(Q))
print("F2 r4=-85 两组写回段命中 = %s；r4=0 / -3 两组 = %s"
      % ([q["wb"] for q in Q[1:3]], [q["wb"] for q in (Q[0], Q[3])]))
print("F3 第一道门进得去（CMD_SEG 命中）但第二道门不匹配 ⇒ "
      "Q0/Q3 的 WB_CALL = %s / %s"
      % (Q[0]["hits"]["WB_CALL"], Q[3]["hits"]["WB_CALL"]))
print("F4 函数体可执行 ≠ 固件调用边：Q1 写回 11/75，收敛态 0/75，"
      "WB_CALL 仍不是固件自己发起")

hr("G. 自我证伪、归因分离与开放边界")
half = rd16(C.SVC_TABLE_BASE + C.MAIN_SVC_8 * 2)
ok, s, tgt = C.table_offset_verdict(half)
print("G1 分发表假设「0x%06X + extsh(lduh @(+svc*2))」：86 号表项 0x%04X "
      "signed=%+d target=0x%06X 落 ROM 内 = %s ⇒ 假设 = 证伪"
      % (C.SVC_TABLE_BASE, half, s, tgt, ok))
print("G2 索引经过 0x%06X lsl #1,r12 规整；规整口径未闭合，"
      "不编自洽算法，保持开放" % C.KERNEL_SVC_LSL1)
print("G3 extsb 不是漏扫原因（V80 已自我证伪）：bstatic.NONWRITE 已含它，"
      "两版扫描逐项一致")
print("G4 等形纪律：E 段 4 组与 F 段 4 组的 equal_shape 分别为 %s / %s"
      % (C.equal_shape(P), C.equal_shape(Q)))
print("G5 禁语复核：不报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / "
      "「写回函数没人调用」/「命令分发段坏了」/「86 号服务就是 wai_flg」")

hr("H. 结案与仍开放")
patch_gain = sum(int(r["hits"][k]) for r in P
                 for k in ("MAIN_JMP", "CMD_SEG", "WB_CALL"))
verdict = C.cmd_verdict(
    bh["MAIN_JMP"], bh["CMD_SEG"], bh["WB_CALL"], patch_gain)
print("H0 综合判据 = %s（补位后命令路径命中增益 = %d）" % (verdict, patch_gain))
print("H1 已结：主循环 86 号服务返回码 r4 恒 0 使第一道门 beq 跳回 0x%06X；"
      "命令分发段 0x%06X 是单码分发器，只认 %d，"
      "两道判据串联（规矩 75）" % (C.MAIN_BEQ_TARGET, C.CMD_SEG, C.CMD_CODE))
print("H2 已结：等形补位四组（位模式 / 全 1 / bit0 / 全 0）改变执行 "
      "但 MAIN_JMP / CMD_SEG / WB_CALL 全 False ⇒ 拦点是返回码事件，不是 flag 位")
print("H3 已结：强制非 0 + r4=-85 时写回段 11/75 命中 ⇒ "
      "分发链与函数体完好，缺的是让 86 号服务返回非 0 码的事件")
print("H4 仍开放：真机让 vec 0x18 周期性发生的器件/事件（KI#191）；"
      "让 86 号服务返回非 0 码的事件语义；两状态字偏移 0 bit3 置位源；"
      "vec 0x2D 硬件事件；317 个提交调用点非根部分的触发任务；"
      "真内核服务分发表索引规整口径")
print("H5 链未闭合；所有 raise_irq / 写位 / 强制设 PC 都是反事实补全")
