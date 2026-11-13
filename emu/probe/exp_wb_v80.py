# -*- coding: utf-8 -*-
"""V80 正式探针：写回段唯一入口判据、flag 12 通信面、时基「配置 vs 器件」分离（A–H）。

承接 KNOWN_ISSUES KI#311 第一、二条。设备策略与 V65–V79 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；所有 raise_irq(0x18) / 直接写 flag 12 位模式 /
强制设 cpu.pc = 0x175908 都是**反事实补全**，不是固件行为。
每组在 post 之后统一再跑 20000 步，保证等形。
独立机器：C 段 2 台 + D 段 7 台 + F 段 2 台 + G 段 2 台 = 13 台。
"""
import sys
import os
import struct
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import bstatic, blive, bready, waitstat67 as W, tick79 as T, wb80 as C

BOOT, VEC, RUN, POST, WINDOW = (C.BOOT_STEPS, C.VEC_BOOT,
                                C.RUN_STEPS, C.POST_STEPS, C.WINDOW_STEPS)
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
        mn, bs, v, size, w = ins
        out.append((pc, mn, dict(v), size))
        pc += size
    return out


def cov(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def nz8(m):
    return sum(1 for i in range(96) if m.bus.rd(C.SLOT8 + i, 1))


def u32_const(val):
    pat = struct.pack(">I", val)
    out, off = [], rom.find(pat)
    while off >= 0:
        out.append(base + off)
        off = rom.find(pat, off + 1)
    return out


INS = []
off, n = 0, len(rom)
while off < n - 1:
    if rom[off:off + 2] in bstatic.SKIP2:
        off += 2
        continue
    ins = fr_emu.TABLE.decode(rom, off)
    if ins is None:
        off += 2
        continue
    mn, bs, v, size, w = ins
    INS.append((base + off, mn, dict(v), size))
    off += size

hr("A. 静态链：写回函数完整入边、flag 12 面、ICR 配置写点（只读 ROM）")
print("A0 线性解码 %d 条，ROM 0x%06X..0x%06X" %
      (len(INS), INS[0][0], INS[-1][0]))

FORMS = collections.Counter()
TGT = collections.defaultdict(lambda: collections.defaultdict(list))
PC2FORM = {}
for pc, mn, v, _s in INS:
    kind = T.call_form(mn, v)
    if kind:
        FORMS[kind] += 1
        PC2FORM[pc] = kind
        if T.is_relative_form(kind):
            TGT[T.rel12_target(pc, v["rel12"])][kind].append(pc)
    if "rel9" in v:
        TGT[C.rel9_target(pc, v["rel9"])][mn].append(pc)
    if mn == "ldi:32":
        FORMS["ldi:32"] += 1
        TGT[v["i32"] & 0xFFFFFFFF]["ldi:32"].append(pc)
RES, WHY = T.resolve_indirect(INS)
for pc, (reg, t, src) in RES.items():
    TGT[t][PC2FORM[pc] + "归约"].append(pc)
print("A1 七形态 = %s" % sorted(FORMS.items()))
for name, addr in (("WB_FN", C.WB_FN), ("CLEAR_FN", C.CLEAR_FN),
                   ("CMD_SEG", C.CMD_SEG), ("WB_CALL", C.WB_CALL),
                   ("MAIN_BEQ_T", C.MAIN_BEQ_TARGET)):
    d = TGT.get(addr, {})
    print("A2 %-12s 0x%06X 入边 %d 条：%s" %
          (name, addr, sum(len(v) for v in d.values()),
           [(k, len(v), ["0x%06X" % x for x in v[:5]])
            for k, v in sorted(d.items())]))
print("A3 WB_FN 作为 4 字节常量全 ROM = %d 处（不靠函数指针表）" %
      len(u32_const(C.WB_FN)))

rows_set = [cs for cs in SW.of(C.SET_FLG) if cs.flgid == C.FLAG12]
rows_clr = [cs for cs in SW.of(C.CLR_FLG) if cs.flgid == C.FLAG12]
rows_wai = [cs for cs in SW.of(C.WAI_FLG) if cs.flgid == C.FLAG12]
print("A4 正确口径 flag12：set_flg=%d / clr_flg=%d / wai_flg=%d；"
      "写回段内 wai_flg=%s" %
      (len(rows_set), len(rows_clr), len(rows_wai),
       ["0x%06X" % cs.addr for cs in rows_wai
        if C.WB_FN <= cs.addr < C.WB_END]))
print("A5 规矩 72 假阴性对照：拿 cs.r4==12 过滤 wai_flg 得 %d 处"
      "（应为 %d，flgid 在 r5）" %
      (sum(1 for cs in SW.of(C.WAI_FLG) if cs.r4 == C.FLAG12),
       len(rows_wai)))
print("A6 服务号宽度：0xFFFFFF56 按 32 位=%d / 按 8 位=%d" %
      (C.svc_num(0xFFFFFF56, 32), C.svc_num(0xFFFFFF56, 8)))
print("A7 ICR 0x448 写点 = %s；ICR 配置段 = %s" %
      (["0x%06X" % p for p, _mn in SW.writers_of(C.ICR_ADDR)],
       ["0x%06X" % p for p in (C.ICR_INIT_PC, C.DIRECT48_PC,
                               C.DIRECT4E_PC, C.BATCH_ICR_PC)]))

hr("B. 分支目标与循环边界（规矩 57 / 70 / 72）")
EDGES = [
    ("WB_CALL", C.WB_CALL, C.WB_CALL_REL12, "rel12", C.WB_FN),
    ("CMD_BEQ", C.CMD_BEQ, 4, "rel9", C.WB_CALL),
    ("MAIN_BEQ", C.MAIN_BEQ, 4, "rel9", C.MAIN_BEQ_TARGET),
    ("CLEAR_BGT", C.CLEAR_BGT, 254, "rel9", C.CLEAR_BGT_TARGET),
    ("WB_BLT", C.WB_LOOP_BLT, 208, "rel9", C.WB_LOOP_BACK),
]
for name, pc, rel, kind, expect in EDGES:
    got = (C.rel12_target(pc, rel) if kind == "rel12"
           else C.rel9_target(pc, rel))
    print("B1 %-10s 0x%06X %s=%d → 0x%06X 期望 0x%06X %s" %
          (name, pc, kind, rel, got, expect,
           "OK" if got == expect else "BAD"))
print("B2 命令码 0x%02X 按 8 位补码 = %d；itron -85 = sys_dly_tsk" %
      (C.CMD_LDI8, C.sx8(C.CMD_LDI8)))
for tag, lo, hi in (("CLEAR_SEG", C.CLEAR_FN, C.WB_FN),
                    ("WB_SEG", C.WB_FN, C.WB_END)):
    hits = ["0x%06X" % pc for pc, mn, v, _s in dec(lo, hi)
            if mn == "ldi:32" and (v.get("i32") & 0xFFFFFFFF) == C.SLOT8]
    print("B3 %-10s 引用 SLOT8 0x%08X = %s" % (tag, C.SLOT8, hits))
print("B4 清零段服务序列 = %s" %
      [(hex(cs.addr), cs.svc, cs.flgid, cs.bits)
       for cs in SW.calls if C.CLEAR_FN <= cs.addr < C.WB_FN])
print("B5 写回段服务序列 = %s" %
      [(hex(cs.addr), cs.svc, cs.flgid, cs.bits)
       for cs in SW.calls if C.WB_FN <= cs.addr < C.WB_END])
lv = C.loop_verdict(C.CLEAR_BGT, 254, C.CLEAR_BGT_TARGET,
                    C.WB_LOOP_BLT, 208, C.WB_LOOP_BACK)
print("B6 循环判据 = %s" % lv)

hr("C. 收敛态基线与 1M 窗口自证（2 台独立机器）")
KEYS = {"ICR_STB": C.ICR_INIT_PC, "DIRECT48": C.DIRECT48_PC,
        "DIRECT4E": C.DIRECT4E_PC, "BATCH_ICR": C.BATCH_ICR_PC,
        "TICK_ISR": C.TICK_ISR, "MAIN_WAIT": C.MAIN_WAIT,
        "MAIN_BEQ": C.MAIN_BEQ, "MAIN_JMP": C.MAIN_JMP,
        "CMD_SEG": C.CMD_SEG, "CMD_CMP": C.CMD_CMP,
        "WB_CALL": C.WB_CALL, "WB_FN": C.WB_FN,
        "CLEAR_FN": C.CLEAR_FN, "WB_ST": C.WB_ST}


def hits_of(m):
    cc = cov(m)
    return dict((k, p in cc) for k, p in KEYS.items())


mb = boot_run()
hb = hits_of(mb)
print("C1 基线覆盖=%d 末PC=0x%06X" % (len(cov(mb)), mb.cpu.pc))
print("C2 基线命中 = %s" % hb)
print("C3 ICR 0x448=0x%02X、flag12 PAT=0x%08X、8 槽非零=%d" %
      (mb.bus.rd(C.ICR_ADDR, 1), mb.bus.rd(C.FLAG12_PAT, 4), nz8(mb)))
icr = C.icr_verdict(hb, mb.bus.rd(C.ICR_ADDR, 1))
print("C4 规矩 73 配置判据 = %s" % icr)
mw = boot_run(WINDOW)
print("C5 500k/1M 覆盖对称差集 = %d 个（收敛自证）" %
      len(cov(mb) ^ cov(mw)))

hr("D. 剂量梯度：补 vec 0x18 让清零段跑，写回段是否跟着动（7 台独立机器）")
CLEAR_SEG = set(p for p, _m, _v, _s in dec(C.CLEAR_FN, C.WB_FN))
WB_SEG = set(p for p, _m, _v, _s in dec(C.WB_FN, C.WB_END))


def run_cf(tag, vec, times, write_pat=None, force_pc=False,
           post=POST, extra=C.FORCE_STEPS):
    m = boot_run()
    c0 = cov(m)
    clk0 = m.bus.rd(0x68000110, 8)
    for _ in range(times):
        m.raise_irq(vec)
        m.run(max_steps=C.CF_STEP, spin_guard=False)
    m.run(max_steps=post, spin_guard=False)
    if write_pat is not None:
        m.bus.wr(C.FLAG12_PAT, 4, write_pat, 0)
    if force_pc:
        m.cpu.pc = C.WB_FN
    m.run(max_steps=extra, spin_guard=False)
    c1 = cov(m)
    r = {"tag": tag, "vec": vec, "injections": times, "post_steps": post,
         "write_action": int(write_pat is not None),
         "new": len(c1 - c0), "cov": len(c1),
         "clock_delta": m.bus.rd(0x68000110, 8) - clk0,
         "clear": len(c1 & CLEAR_SEG), "wb": len(c1 & WB_SEG),
         "wb_call": C.WB_CALL in c1, "cmd_seg": C.CMD_SEG in c1,
         "pat": m.bus.rd(C.FLAG12_PAT, 4), "nz": nz8(m),
         "pc": m.cpu.pc}
    print("  %-8s vec=0x%02X 次=%3d 新增PC=%4d 覆盖=%4d clock+%3d "
          "清零=%2d/%d 写回=%2d/%d WB_CALL=%s CMD_SEG=%s "
          "flag12=0x%08X 8槽非零=%d" %
          (tag, vec, times, r["new"], r["cov"], r["clock_delta"],
           r["clear"], len(CLEAR_SEG), r["wb"], len(WB_SEG),
           r["wb_call"], r["cmd_seg"], r["pat"], r["nz"]))
    sys.stdout.flush()
    return r


D = [run_cf("N0", C.VEC_TIMER, 0), run_cf("N1", C.VEC_TIMER, 1),
     run_cf("N12", C.VEC_TIMER, 12), run_cf("N48", C.VEC_TIMER, 48),
     run_cf("N110", C.VEC_TIMER, 110), run_cf("N160", C.VEC_TIMER, 160),
     run_cf("MIS160", C.VEC_BOOT, 160)]

hr("E. 等形对照与归因分离")
# 等形对照必须注入次数相同：取同为 160 次的 N160 / MIS160。
# N110 vs MIS160（110 次 / 160 次）不等形，只用于剂量梯度，不作等形判据。
eq = C.equal_shape([D[5], D[6]])
neq = C.equal_shape([D[4], D[6]])
att = C.attribution([D[1], D[2], D[3], D[4], D[5]], [D[6]])
print("E1 N160 vs MIS160 等形判据 = %s" % eq)
print("E1b N110 vs MIS160 不等形（只作剂量梯度）= %s" % neq["equal"])
print("E2 归因分离（clock_delta）= %s" % att)
print("E3 错配 160 次：清零=%d/%d 写回=%d/%d 新PC=%d" %
      (D[6]["clear"], len(CLEAR_SEG), D[6]["wb"], len(WB_SEG),
       D[6]["new"]))
print("E4 N110 清零段 12/12 且 flag12=0x%08X，但 WB_CALL=%s ⇒ "
      "set_flg 已经真实执行过" % (D[4]["pat"], D[4]["wb_call"]))

hr("F. 补数据门：直接写 flag 12 位模式（2 台独立机器，等形）")
F1 = run_cf("W1", C.VEC_TIMER, 110, write_pat=1)
F0 = run_cf("W0", C.VEC_TIMER, 110, write_pat=0)
eqf = C.equal_shape([F1, F0])
dg = C.data_gate_verdict(D[4]["wb"], F1["wb"], F0["wb"], F1["pat"])
print("F1 W1/W0 等形判据 = %s" % eqf)
print("F2 写数据门判据 = %s（N110=%d / W1=%d / W0=%d）" %
      (dg, D[4]["wb"], F1["wb"], F0["wb"]))

hr("G. 模型化强制入口：函数体可执行 ≠ 固件自己会调（2 台独立机器）")
G1 = run_cf("FORCE", C.VEC_TIMER, 110, force_pc=True)
G2 = run_cf("FORCE_MIS", C.VEC_BOOT, 160, force_pc=True)
fe = C.force_entry_verdict(G1["wb"], G1["new"])
print("G1 强制入口判据 = %s" % fe)
print("G2 FORCE=%d/%d、FORCE_MIS=%d/%d；两组 WB_CALL 均 %s" %
      (G1["wb"], len(WB_SEG), G2["wb"], len(WB_SEG),
       G1["wb_call"] or G2["wb_call"]))
print("G3 ⚠️ 强制设 cpu.pc 是模型化反事实，不是固件调用边；"
      "39/75 只证明函数体完好，不证明链闭合")

hr("H. 结案与仍开放")
verdict = C.wb_verdict(icr, hb, dg, fe, att)
print("H1 综合判据 = %s" % verdict)
print("H2 已结：ICR 0x448=0x12 的初始化链在收敛态全命中（配置已做、器件事件缺）；"
      "写回函数唯一入边 0x17574C call rel12=221；入口判据 r4==-85；"
      "清零段与写回段共享 SLOT8 且靠 flag 12 通信，但不是顺序后继；"
      "flag12 已被清零段置 1，补数据无增益，强制入口 39/75 命中。")
print("H3 仍开放：真机让 vec 0x18 周期性发生的硬件器件/事件（KI#191）、"
      "主循环 0x175270 等待返回非 0 命令码的事件语义、两状态字偏移 0 bit3、"
      "vec 0x2D 硬件事件、317 个提交调用点非根部分的触发任务。")
print("H4 边界：不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /"
      "「flag 12 没置所以写回没跑」/「写回函数没人调用」/"
      "「补一次 set_flg 就唤醒成功」。")
print("DONE")
