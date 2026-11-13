# -*- coding: utf-8 -*-
"""V82 正式探针：真内核服务分发表索引规整口径与返回码出口（A–H 八段）。

承接 KNOWN_ISSUES KI#319 第三条 / KI#316–318。V65–V81 收敛设备策略
逐字相同：blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。

机器：静态段 A/D/E 只读 ROM（不启动实验机器）；
B 与 C 各用 **1 台只观测轨迹机**（全程不改 PC / 寄存器 / 不注入中断），
F/G 为 **4 台独立等形反事实机**。合计 6 台独立机器。
所有在 0x0406B4 改写 r12 都是**反事实补全**，不是固件行为、不是固件调用边。
"""
import sys
import os
import struct
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import bstatic, blive, isrfam63, svc82 as C

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
HI = base + len(rom)


def hr(t):
    print("")
    print("=" * 78)
    print(t)
    print("=" * 78)
    sys.stdout.flush()


def rd16(a):
    return struct.unpack(">H", rom[a - base:a - base + 2])[0]


def rd32(a):
    return struct.unpack(">I", rom[a - base:a - base + 4])[0]


def dec(lo, hi, limit=400):
    out, pc = [], lo
    while pc < hi - 1 and len(out) < limit:
        ins = fr_emu.TABLE.decode(rom, pc - base)
        if ins is None:
            out.append((pc, "??", {}))
            pc += 2
            continue
        mn, _b, v, sz, _w = ins
        out.append((pc, mn, dict(v)))
        pc += sz
    return out


def prologue(t):
    if not (base <= t < HI - 16) or t & 1:
        return "OUT_OF_ROM"
    return "/".join(x[1] for x in dec(t, t + 12, 4))


def cov(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


# ================================================================ A 静态：向量表与陷阱入口
hr("A. int #0x40 向量槽与真内核陷阱入口（只读 ROM）")
VT = dict((x.vec, x.entry) for x in isrfam63.vector_table(rom, base))
print("A1 isrfam63.TBR=0x%08X；vec 0x%02X 槽=0x%06X → 入口 0x%06X"
      % (isrfam63.TBR, C.VEC_TRAP, C.VEC_TRAP_SLOT, VT[C.VEC_TRAP] & 0xFFFFFF))
print("A2 与设施常量 TRAP_ENTRY 一致 = %s"
      % ((VT[C.VEC_TRAP] & 0xFFFFFF) == (C.TRAP_ENTRY & 0xFFFFFF)))
KEY_TRAP = dec(C.TRAP_ENTRY, C.TRAP_RETI + 2)
for pc, mn, v in KEY_TRAP:
    if pc in (C.TRAP_CLR_LSL, C.TRAP_CLR_LSR, C.TRAP_IDX_LSL,
              C.TRAP_TBL_LDI, C.TRAP_LDUH, C.TRAP_EXTSH, C.TRAP_ADD,
              C.TRAP_CALL, C.TRAP_EXTSB, C.TRAP_RETI):
        extra = ""
        if "i32" in v:
            extra = " imm32=0x%08X" % (v["i32"] & 0xFFFFFFFF)
        print("    0x%06X %-10s%s" % (pc, mn, extra))
print("A3 表零点 ldi:32 常量 = 0x%06X"
      % next(v["i32"] for pc, mn, v in KEY_TRAP if pc == C.TRAP_TBL_LDI))

# ================================================================ B/C 单步轨迹（1 台只观测机）
hr("B. 单步实测：第一次 int #0x40（r12 = -83）")
tm = blive.boot(0)
r = tm.cpu.r


def trap_trace(m, stop_step, tag):
    """批量快进到指定步，只读取陷阱体内各 WATCH 点的 r12。"""
    m.step(stop_step)
    rr = m.cpu.r
    if m.cpu.pc != C.TRAP_ENTRY:
        print("%s ANCHOR_BAD pc=0x%06X" % (tag, m.cpu.pc))
        return None
    watch = (C.TRAP_ENTRY, C.TRAP_CLR_LSL, C.TRAP_IDX_LSL,
             C.TRAP_LDUH, C.TRAP_EXTSH, C.TRAP_CALL,
             C.TRAP_RET, C.TRAP_EXTSB)
    seen = collections.OrderedDict()
    for _ in range(160):
        pc = m.cpu.pc
        if pc in watch and pc not in seen:
            seen[pc] = rr[12] & 0xFFFFFFFF
        if pc == C.TRAP_RETI + 2:
            break
        m.step(1)
    return seen


S1 = trap_trace(tm, C.FIRST_TRAP_STEP, "B")
if S1:
    print("B1 进入陷阱 r12=0x%08X（%d）" % (S1[C.TRAP_ENTRY], C.sx(S1[C.TRAP_ENTRY], 32)))
    print("B2 0x0406A4 lsl#1 前 r12=0x%08X；lduh 后=0x%08X；call 目标=0x%06X"
          % (S1.get(C.TRAP_IDX_LSL, 0), S1.get(C.TRAP_EXTSH, 0),
             S1.get(C.TRAP_CALL, 0) & 0xFFFFFF))
    slot1 = C.slot_of(C.FIRST_TRAP_R12)
    print("B3 设施口径 slot=0x%06X half=0x%04X target=0x%06X；与实测一致 = %s"
          % (slot1, rd16(slot1), C.target_of(rd16(slot1)),
             C.target_of(rd16(slot1)) == (S1.get(C.TRAP_CALL, 0) & 0xFFFFFF)))
    print("B4 服务返回后 r12=0x%08X，extsb 前=0x%08X"
          % (S1.get(C.TRAP_RET, 0), S1.get(C.TRAP_EXTSB, 0)))

hr("C. 单步实测：主循环 0x175270 那次（r12 = 0xFFFFFF56）")
tm2 = blive.boot(0)
S2 = trap_trace(tm2, C.TRAP_BOOT_STEP, "C")
if S2:
    e = S2.get(C.TRAP_ENTRY, 0)
    print("C1 进入陷阱 r12=0x%08X → 8 位=%d / 32 位=%d"
          % (e, C.width_pair(e)[0], C.width_pair(e)[1]))
    print("C2 0x0406A4 lsl#1 前 r12=0x%08X（清符号位后的中间值）"
          % S2.get(C.TRAP_IDX_LSL, 0))
    print("C3 lsl#1 稳健性：0xFFFFFF56 与 0x7FFFFF56 结果相同 = %s"
          % C.lsl1_stable(C.MAIN_IMM32, C.MAIN_SVC_CLEARED))
    slot = C.slot_of(C.MAIN_SVC_32)
    half = rd16(slot)
    tgt = C.target_of(half)
    print("C4 表项槽 0x%06X half=0x%04X off=%+d → 处理函数 0x%06X"
          % (slot, half, C.extsh(half), tgt))
    print("C5 call @r12 实测目标 0x%06X == 0x041BEA ? %s；入口序言 %s"
          % (S2.get(C.TRAP_CALL, 0) & 0xFFFFFF,
             (S2.get(C.TRAP_CALL, 0) & 0xFFFFFF) == (C.MAIN_HANDLER & 0xFFFFFF),
             prologue(tgt)))
    print("C6 服务返回后 r12=0x%08X，extsb 前=0x%08X ⇒ 第一道门 r4 恒 0"
          % (S2.get(C.TRAP_RET, 0), S2.get(C.TRAP_EXTSB, 0)))

# ================================================================ D 静态：两口径全表普查
hr("D. 全表两种口径普查（只读 ROM，256 个 8 位解释）")


def census(conv):
    """与设施常量 CENSUS_* 同口径：不剔空槽，全 256 项逐一算目标。"""
    inrom, pro, valid = 0, 0, 0
    for n in range(256):
        sl = C.slot_of(conv(n))
        if not (base <= sl < HI - 2):
            continue
        h = rd16(sl)
        if not C.is_empty(h):
            valid += 1
        t = C.target_of(h)
        if base <= t < HI and not (t & 1):
            inrom += 1
            p = prologue(t)
            if p.startswith(("enter", "stm0", "stm1", "st/")):
                pro += 1
    return valid, inrom, pro


def sx8(n):
    return n - 256 if n >= 128 else n


D32 = census(sx8)
D8 = census(lambda n: n)
print("D1 32 位有符号（真内核）有效项=%d 落ROM=%d 像序言=%d" % D32)
print("D2 8 位无符号（静态标签）有效项=%d 落ROM=%d 像序言=%d" % D8)
print("D3 设施常量 CENSUS_32BIT=%s / CENSUS_8BIT=%s（按全 256 项、不剔空槽口径）"
      % (C.CENSUS_32BIT, C.CENSUS_8BIT))
neg = [rd16(C.slot_of(s)) for s in range(-256, 0)
       if base <= C.slot_of(s) < HI - 2]
pos = [rd16(C.slot_of(s)) for s in range(0, 128)
       if base <= C.slot_of(s) < HI - 2]
print("D4 负号 -256..-1 空槽=%d 有效=%d；非负 0..127 空槽=%d 有效=%d"
      % (sum(1 for h in neg if C.is_empty(h)), sum(1 for h in neg if not C.is_empty(h)),
         sum(1 for h in pos if C.is_empty(h)), sum(1 for h in pos if not C.is_empty(h))))

# ================================================================ E 静态：表项槽与返回码写点
hr("E. -170 号表项槽、唯一性与返回码出口（只读 ROM）")
print("E1 指向 0x041BEA 的服务号：%s"
      % [s for s in range(-256, 128)
         if base <= C.slot_of(s) < HI - 2
         and not C.is_empty(rd16(C.slot_of(s)))
         and C.target_of(rd16(C.slot_of(s))) == C.MAIN_HANDLER])
print("E2 相邻独立入口（规矩 77）：%s"
      % " ".join("0x%06X=r12%+d" % (a, s) for a, s in sorted(C.ADJACENT_ENTRIES.items())))
print("E3 返回码 r12 写点（ldi:8 → 8 位补码）：")
for pc, imm, val in C.R12_WRITES:
    print("    0x%06X imm=0x%02X → %+d" % (pc, imm, val))
print("E4 其中 -85 写点 = %d 个；命令分发段 cmp r4,-85 的码正是它"
      % sum(1 for _p, _i, v in C.R12_WRITES if v == C.CMD_CODE))
exits = [pc for pc, mn, _v in dec(C.MAIN_HANDLER, 0x041E80)
         if mn in ("ret", "ret:d", "reti")]
print("E5 以 0x041BEA 起线性扫到 0x041E80，ret/ret:d/reti 共 %d 处；"
      "设施常量 EXIT_170 收录前 %d 处（规矩 67/77，边界不在此版闭合）"
      % (len(exits), len(C.EXIT_170)))
print("E6 0x04050C 附近 ±6 槽：")
for s in range(-176, -164):
    sl = C.slot_of(s)
    h = rd16(sl)
    print("    r12=%+5d slot=0x%06X half=0x%04X %s"
          % (s, sl, h, "空槽" if C.is_empty(h) else "有效→0x%06X" % C.target_of(h)))

# ================================================================ F 等形反事实：返回点改 r12
hr("F. 等形反事实四组：在真实服务返回点 0x0406B4 改写 r12")
KEYS = {
    "MAIN_MOV": C.MAIN_MOV_R12_R4, "MAIN_CMP": C.MAIN_CMP0,
    "MAIN_BEQ": C.MAIN_BEQ, "MAIN_JMP": C.MAIN_JMP,
    "CMD_SEG": C.CMD_SEG, "CMD_CMP": C.CMD_CMP,
    "CMD_BEQ": C.CMD_BEQ, "WB_CALL": C.WB_CALL, "WB_FN": C.WB_FN,
}
ROWS = []


def one(tag, write_val):
    m = blive.boot(0)
    m.step(C.TRAP_BOOT_STEP)
    rr = m.cpu.r
    anchor = (m.cpu.pc == C.TRAP_ENTRY
              and (rr[12] & 0xFFFFFFFF) == C.MAIN_IMM32)
    got = False
    for _ in range(200):
        if m.cpu.pc == C.TRAP_RET:
            got = True
            break
        m.step(1)
    before = rr[12] & 0xFFFFFFFF
    if write_val is not None:
        rr[12] = write_val & 0xFFFFFFFF
    written = rr[12] & 0xFFFFFFFF
    pcs = set()
    for _ in range(C.TRAP_AFTER_STEPS):
        pcs.add(m.cpu.pc)
        m.step(1)
    hit = dict((k, p in pcs) for k, p in KEYS.items())
    return {"tag": tag, "boot": C.TRAP_BOOT_STEP, "inject": 0,
            "post": C.TRAP_AFTER_STEPS, "write_pc": C.TRAP_RET,
            "anchor": anchor, "got": got, "before": before,
            "written": written, "end_r12": rr[12] & 0xFFFFFFFF,
            "end_r4": rr[4] & 0xFFFFFFFF, "npc": len(pcs), "hit": hit}


for tag, val in C.CF_GROUPS:
    x = one(tag, val)
    ROWS.append(x)
    hs = " ".join("%s" % k for k, v in x["hit"].items() if v)
    print("  %-5s anchor=%s 返回点命中=%s 返回前r12=0x%08X 写入=0x%08X "
          "末r12=0x%08X 末r4=0x%08X 不同PC=%d 命中=[%s]"
          % (tag, x["anchor"], x["got"], x["before"], x["written"],
             x["end_r12"], x["end_r4"], x["npc"], hs))
print("F5 等形四项（boot/inject/post/write_pc）全同 = %s" % C.equal_shape(ROWS))
print("F6 改写确实改变执行：R85 不同PC=%d vs BASE=%d，差=%d"
      % (ROWS[2]["npc"], ROWS[0]["npc"], ROWS[2]["npc"] - ROWS[0]["npc"]))

# ================================================================ G 归因分离与两道门
hr("G. 两道门归因分离（反事实，非固件行为）")
B = dict((x["tag"], x) for x in ROWS)
for t in ("BASE", "R0", "R85", "R17"):
    h = B[t]["hit"]
    print("G1 %-5s gate1(MAIN_JMP)=%-5s 分发段(CMD_SEG)=%-5s gate2(WB_CALL)=%-5s "
          "写回函数(WB_FN)=%-5s → %s"
          % (t, h["MAIN_JMP"], h["CMD_SEG"], h["WB_CALL"], h["WB_FN"],
             C.gate_verdict(h["MAIN_JMP"], h["CMD_SEG"], h["WB_CALL"])))
print("G2 基线两组 BASE/R0 全 False = %s"
      % (not any(B[t]["hit"][k] for t in ("BASE", "R0")
                 for k in ("MAIN_JMP", "CMD_SEG", "WB_CALL"))))
print("G3 -85 与 -17 的第二道门差异 = %s（归因分离）"
      % (B["R85"]["hit"]["WB_CALL"] and not B["R17"]["hit"]["WB_CALL"]))
print("G4 所有改写 r12 都是反事实补全；WB_FN 命中只发生在 R85 反事实组，"
      "不是固件自然执行、不是真机事件")

# ================================================================ H 综合判据与边界
hr("H. 综合判据与开放项")
slot_ok = (C.slot_of(C.MAIN_SVC_32) == C.MAIN_SLOT
           and rd16(C.MAIN_SLOT) == C.MAIN_HALF)
call_ok = (S2 is not None
           and (S2.get(C.TRAP_CALL, 0) & 0xFFFFFF) == (C.MAIN_HANDLER & 0xFFFFFF))
ret_zero = S2 is not None and (S2.get(C.TRAP_RET, 1) == 0)
cf_both = B["R85"]["hit"]["WB_CALL"]
print("H1 表项槽口径闭合=%s 动态 call 目标闭合=%s 收敛态返回码 0=%s"
      % (slot_ok, call_ok, ret_zero))
print("H2 反事实 -85 让两道门全开=%s；真机触发 -85 的器件事件语义仍未定位" % cf_both)
print("H3 综合判据 = %s"
      % C.svc_verdict(slot_ok, call_ok, ret_zero, cf_both, False))
print("H4 仍开放：真机让 vec 0x18 周期性发生的器件/事件（KI#191）、"
      "让 -170 号处理函数走 -85 出口的器件事件语义、"
      "两状态字偏移 0 bit3 置位源、vec 0x2D 硬件事件、"
      "317 个提交调用点非根部分的触发任务")
print("H5 共同禁语：不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / "
      "「写回函数没人调用」/「命令分发段坏了」/「86 号服务就是 wai_flg」/ "
      "「所有服务语义已解出」/「真机已复现 -85」")
print("")
print("DONE V82 A–H 八段，6 台独立机器（2 台只观测轨迹机 + 4 台等形反事实机），"
      "反事实全部标注")
