# -*- coding: utf-8 -*-
"""V79 正式探针：真时基 tick 链定位、call:d rel12 第五形态、清零循环入边（A–H）。

承接 KI#307 第一、二条。设备策略与 V65–V78 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；所有 raise_irq / 直接写 TCB+0x14 / btick.expire_strict
都是**反事实补全**，不是固件行为（真机中断事件流未建模，KI#191）。
独立机器：基线 1 台 + 1M 窗口 1 台 + F 组 5 台 + G 组 6 台 = 13 台。
"""
import sys
import os
import struct
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, btick, isrfam63 as I,
                   tick79 as C)

BOOT, VEC, RUN, POST, WINDOW = (C.BOOT_STEPS, C.VEC_BOOT,
                                C.RUN_STEPS, C.POST_STEPS, C.WINDOW_STEPS)
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
ROMEND = base + len(rom)


def hr(t):
    print("")
    print("=" * 78)
    print(t)
    print("=" * 78)
    sys.stdout.flush()


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


def u32_const(val):
    pat = struct.pack(">I", val)
    out, off = [], rom.find(pat)
    while off >= 0:
        out.append(base + off)
        off = rom.find(pat, off + 1)
    return out


# ================================================================ 全 ROM 一次扫描
hr("A. 七形态普查与关注函数完整入边（只读 ROM）")
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
print("A0 线性解码 %d 条，ROM 0x%06X..0x%06X" %
      (len(INS), INS[0][0], INS[-1][0]))

FORMS = collections.Counter()
TGT = collections.defaultdict(lambda: collections.defaultdict(list))
for pc, mn, v, _s in INS:
    kind = C.call_form(mn, v)
    if kind:
        FORMS[kind] += 1
        if C.is_relative_form(kind):
            t = C.rel12_target(pc, v["rel12"])
            TGT[t][kind].append(pc)
    if mn == "ldi:32":
        FORMS["ldi:32"] += 1
        TGT[v["i32"] & 0xFFFFFFFF]["ldi:32"].append(pc)
PC2FORM = dict((pc, C.call_form(mn, v))
               for pc, mn, v, _s in INS if C.call_form(mn, v))
RES, WHY = C.resolve_indirect(INS)
for pc, (reg, t, src) in RES.items():
    TGT[t][PC2FORM[pc] + "归约"].append(pc)
print("A1 七形态 = %s" % sorted(FORMS.items()))
fc = C.form_census(FORMS)
print("A2 调用点合计 %d，其中 PC 相对 %d 条；Sweep.edges 目标 %d 个" %
      (fc["total"], fc["relative"], len(SW.edges)))
print("A3 间接归约可定 %d 条，不可定原因 top5=%s" %
      (len(RES), WHY.most_common(5)))
for name, addr in (("TICK_FN", C.TICK_FN), ("WAIT_FN", C.WAIT_FN),
                   ("CLEAR_FN", C.CLEAR_FN), ("WB_FN", C.WB_FN),
                   ("SUBMIT_FN", C.SUBMIT_FN)):
    d = TGT.get(addr, {})
    print("A4 %-11s 0x%06X 入边 %d 条：%s" %
          (name, addr, sum(len(x) for x in d.values()),
           [(k, len(x)) for k, x in sorted(d.items())]))
    for k, x in sorted(d.items()):
        if len(x) <= 6:
            print("      %-18s %s" % (k, [hex(p) for p in x]))
only_rel12 = sorted(t for t, d in TGT.items()
                    if "call:d rel12" in d
                    and t not in SW.edges
                    and "call rel12" not in d
                    and not any(k.endswith("归约") for k in d)
                    and "ldi:32" not in d)
print("A5 形态普查与设施常量核对：call:d rel12 %d（常量 %d）；"
      "只被它解出的入口 %d（常量 %d）" %
      (FORMS["call:d rel12"], C.FORM_CENSUS["call:d rel12"],
       len(only_rel12), C.ONLY_CALLD_REL12))
sys.stdout.flush()

# ================================================================ B 真时基链
hr("B. 真时基 vec 0x18 的静态链（规矩 60 倒序）")
VEC_D = dict((x.vec, x.entry) for x in I.vector_table(rom, base))
print("B1 vec 0x%02X 槽 = TBR+(255-0x%02X)*4 = 0x%08X → ISR 0x%08X" %
      (C.VEC_TIMER, C.VEC_TIMER, C.vec_slot(C.VEC_TIMER),
       VEC_D[C.VEC_TIMER]))
print("B2 向量表与设施常量一致 = %s；非默认入口 %d 个" %
      (VEC_D[C.VEC_TIMER] == C.TICK_ISR,
       len({e for e in VEC_D.values() if e and e != 0xFFFFFFFF})))
CALLER = dec(0x040470, 0x040492)
for pc, mn, v, _s in CALLER:
    print("   0x%06X %-9s %s%s" %
          (pc, mn, v, "  ★" if pc == C.TICK_CALL else ""))
print("B3 ISR 体含 reti = %s（返回中断现场；0x040490 的 nop 是填充，"
      "不是函数结尾判据）" %
      any(mn == "reti" for _p, mn, _v, _s in CALLER))
CHAIN = dec(C.TICK_FN, 0x040A96)
cc = C.clock_chain_verdict(CHAIN)
print("B4 时钟链判据 = %s" % cc)
for pc, mn, v, _s in CHAIN:
    if mn in ("add", "addc", "add2", "ld", "st", "ldi:32"):
        if pc in (0x040A52, 0x040A54, 0x040A56, 0x040A58, 0x040A5A,
                  0x040A5C, 0x040A78, 0x040A7A, 0x040A7E):
            print("   0x%06X %-8s %s" % (pc, mn, v))
print("B5 0x040A44 入边 = %s；全 ROM 4 字节常量出现 %d 处" %
      ({k: [hex(x) for x in v] for k, v in C.TICK_IN_EDGES.items()},
       len(u32_const(C.TICK_FN))))
sys.stdout.flush()

# ================================================================ C 常量表
hr("C. 函数指针常量表补查（全 ROM 4 字节大端）")
for name, addr in (("TICK_FN", C.TICK_FN), ("TICK_ISR", C.TICK_ISR),
                   ("CLEAR_FN", C.CLEAR_FN), ("WAIT_FN", C.WAIT_FN),
                   ("WB_FN", C.WB_FN), ("SUBMIT_FN", C.SUBMIT_FN)):
    hits = u32_const(addr)
    print("C1 %-11s 0x%06X 出现 %3d 处，前 5 = %s" %
          (name, addr, len(hits), [hex(x) for x in hits[:5]]))
print("C2 三个 task7 尾部函数作为常量 0 处 ⇒ 不靠 ROM 函数指针表调用；"
      "清零循环靠 call:d rel12（A 段）")
sys.stdout.flush()

# ================================================================ D 等待函数
hr("D. 等待函数的分支目标与稳定循环（规矩 57）")
WAIT = dec(C.WAIT_FN, C.CLEAR_FN)
BR = [(pc, mn, C.rel9_target(pc, v["rel9"]))
      for pc, mn, v, _s in WAIT if "rel9" in v]
print("D1 5 条 rel9 分支 = %s" % [(hex(p), m, hex(t)) for p, m, t in BR])
sl = C.stable_loop_verdict(0x17589E, 3, 0x1758A6, 10,
                           0x1758AA, 238, 0x175888)
print("D2 稳定循环判据 = %s" % sl)
print("D3 端口 0x%08X 读 ldub，两次比较 cmp r6,r4；"
      "端口值变化 → r5 清零并重新 tslp_tsk(10)；"
      "连续稳定 %d 次才 fall through 到 0x1758AC" %
      (C.PORT, C.STABLE_LIMIT))
sys.stdout.flush()

# ================================================================ E 基线
hr("E. 收敛态基线与 1M 步窗口自证")
mb = boot_run()
CB = cov(mb)
idx = bready.task_index(mb)
t7 = idx[7]
print("E1 500k 覆盖=%d 末PC=0x%06X clock=%d t7_ticks=0x%X "
      "t7_state=0x%02X 8槽非零=%d" %
      (len(CB), mb.cpu.pc, mb.bus.rd(C.CLOCK, 8),
       mb.bus.rd(t7 + C.TCB_TICKS, 4), bready.state_of(mb, t7), nz8(mb)))
KEY = (C.TICK_ISR, C.TICK_CALL, C.TICK_FN, C.CLEAR_FN, C.CLEAR_ST,
       C.WB_FN, 0x175958)
print("E2 关键 PC 命中 = %s" %
      {hex(p): p in CB for p in KEY})
mw = boot_run(WINDOW)
CW = cov(mw)
print("E3 1M 步覆盖=%d，与 500k 对称差集=%d 个，末PC=0x%06X，"
      "clock=%d，8槽非零=%d" %
      (len(CW), len(CB ^ CW), mw.cpu.pc, mw.bus.rd(C.CLOCK, 8), nz8(mw)))
sys.stdout.flush()

# ================================================================ F 等形反事实
hr("F. 等形反事实（G0/G1/G3 注入次数与后续步数相同，只换向量号）")
CLEAR_SEG = set(p for p, _m, _v, _s in dec(C.CLEAR_FN, C.WB_FN))
WB_SEG = set(p for p, _m, _v, _s in dec(C.WB_FN, 0x1759A6))


def run_cf(tag, vec, times, post=POST, write_zero=False):
    m = boot_run()
    c0 = cov(m)
    clk0 = m.bus.rd(C.CLOCK, 8)
    for _ in range(times):
        m.raise_irq(vec)
        m.run(max_steps=2000, spin_guard=False)
    if write_zero:
        t = bready.task_index(m)[7]
        m.bus.wr(t + C.TCB_TICKS, 4, 0, 0)
        btick.expire_strict(m)
    m.run(max_steps=post, spin_guard=False)
    c1 = cov(m)
    t = bready.task_index(m)[7]
    r = {"tag": tag, "vec": vec, "injections": times, "post_steps": post,
         "new": len(c1 - c0), "cov": len(c1),
         "clock_delta": m.bus.rd(C.CLOCK, 8) - clk0,
         "ticks": m.bus.rd(t + C.TCB_TICKS, 4),
         "nz": nz8(m),
         "clear": len(c1 & CLEAR_SEG), "wb": len(c1 & WB_SEG),
         "isr": C.TICK_ISR in c1, "pc": m.cpu.pc}
    print("  %-5s vec=0x%02X 次数=%3d 新增PC=%4d 覆盖=%4d clock+%3d "
          "t7_ticks=0x%08X 清零段=%2d/%d 写回段=%2d/%d 8槽非零=%d "
          "ISR=%s" %
          (tag, vec, times, r["new"], r["cov"], r["clock_delta"],
           r["ticks"], r["clear"], len(CLEAR_SEG), r["wb"], len(WB_SEG),
           r["nz"], r["isr"]))
    sys.stdout.flush()
    return r


F = [run_cf("G0", C.VEC_TIMER, 0),
     run_cf("G1", C.VEC_TIMER, 1),
     run_cf("G2", C.VEC_TIMER, 12),
     run_cf("G3", C.VEC_BOOT, 12),
     run_cf("G4", C.VEC_TIMER, 0, write_zero=True)]
eq = C.equal_shape([{"injections": 12, "post_steps": POST},
                    {"injections": 12, "post_steps": POST}])
att = C.attribution([F[1], F[2]], [F[3]])
print("F1 等形判据 = %s；归因分离 = %s" % (eq, att["separated"]))
print("F2 G4 直接写 TCB+0x14=0 后 expire_strict：t7_ticks=0x%08X、"
      "新增 PC=%d ⇒ 模型化归约 ≠ 固件真链（阴性结果）" %
      (F[4]["ticks"], F[4]["new"]))
sys.stdout.flush()

# ================================================================ G 剂量梯度
hr("G. 剂量梯度：跨过「端口稳定 10 次」阈值")
G = [F[0], F[2]] + [run_cf("N%d" % k, C.VEC_TIMER, k)
                    for k in (48, 110, 160)]
GMIS = run_cf("MIS160", C.VEC_BOOT, 160)
print("G1 五元组（组名 / clock 增量 / 清零段命中 / 写回段命中 / "
      "8 槽非零）= %s" %
      [(r["tag"], r["clock_delta"], r["clear"], r["wb"], r["nz"])
       for r in G + [GMIS]])
att2 = C.attribution([r for r in G if r["vec"] == C.VEC_TIMER], [GMIS])
print("G2 归因分离（clock_delta）= %s" % att2)
print("G3 110 次后清零段 %d/%d、写回段 %d/%d、8 槽非零=%d ⇒ "
      "清零循环跑过但写的是 0" %
      (G[-2]["clear"], len(CLEAR_SEG), G[-2]["wb"], len(WB_SEG),
       G[-2]["nz"]))
print("G4 规矩 71 判据 = %s" % C.slot_zero_verdict(0, G[-2]["nz"]))
sys.stdout.flush()

# ================================================================ H 结案
hr("H. 结案与仍开放")
v = C.tick_verdict(
    C.TICK_IN_EDGES,
    [{"clock_delta": r["clock_delta"]} for r in G
     if r["vec"] == C.VEC_TIMER],
    [{"clock_delta": GMIS["clock_delta"]}],
    G[-2]["clear"], G[-2]["wb"],
    C.slot_zero_verdict(0, G[-2]["nz"]))
print("H0 清零循环首次取指前驱轨迹（单步取证，见 scratch_v79/_v79f3.out）"
      " = %s" %
      " → ".join("0x%06X" % p for p in
                 (0x1758E4, 0x1758E6, 0x1758E8, 0x1758EA,
                  0x17541A, 0x17541C, 0x1754D0, 0x1754D2,
                  0x1754D4, 0x1754D6, 0x1754D8, 0x1758EC)))
print("H1 综合判据 = %s" % v)
print("H2 已结：真时基链静态位置（vec 0x18 ISR → 0x040A44）、"
      "call:d rel12 第五形态与 0x1758EC 的 5 条入边、"
      "SUBMIT 调用边 317 = 302 + 10 + 5、"
      "8 槽全 0 不能证明清零循环没跑。")
print("H3 仍开放：真机让 vec 0x18 周期性发生的硬件事件（KI#191）、"
      "写回函数 0x175908 被调到的条件、两状态字偏移 0 的 bit3 置位源、"
      "vec 0x2D 硬件事件、317 个提交调用点里非根部分的触发任务。")
print("H4 边界：不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /"
      "「补一次 set_flg 就唤醒成功」/「清零循环函数没人调用」。")
print("DONE")
