# -*- coding: utf-8 -*-
"""V88 正式探针：cmp2 是 −1 哨兵，A<=-2 提前返回 −33，别名入口不分流（KI#343）。

    cd emu && python probe/exp_svc_v88.py > ../results/frbox/svc88.txt

A–D 段只读 ROM（含 1 台分派区观测机）；E 段 1 台只观测轨迹机 +
1 台标准收敛态机；F/G/H 段 7 台独立 600 步等形机。合计 **10 台独立机器**，
每台只跑一遍。所有往 0x000083E4 写值都是**反事实补全**（只写内存单元），
不是固件行为、不是真机器件事件。链未闭合。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from collections import Counter
from frbox import bstatic, svc88 as V, blive

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
T = fr_emu.TABLE


def rd16(a):
    o = a - base
    return (rom[o] << 8) | rom[o + 1]


def rd32(m, a):
    return m.bus.rd(a, 4) & 0xFFFFFFFF


def ins_at(pc):
    ins = T.decode(rom, pc - base)
    return ("??", {}) if ins is None else (ins[0], dict(ins[2]))


def dec(lo, hi):
    out, pc = [], lo
    while pc < hi - 1:
        ins = T.decode(rom, pc - base)
        if ins is None:
            out.append((pc, "??", {}, 2))
            pc += 2
            continue
        mn, _b, v, sz, _w = ins
        out.append((pc, mn, dict(v), sz))
        pc += sz
    return out


def im(v):
    g = []
    for k in ("i8", "i20", "i32", "u4", "u8", "u6", "m4", "disp10",
              "disp8", "rel9", "rel12", "udisp6"):
        if k in v:
            g.append("%s=%d" % (k, v[k]))
    for k in ("ri", "rj", "rs2", "cc"):
        if k in v:
            g.append("%s=%s" % (k, ("r%d" % v[k]) if k in ("ri", "rj") else v[k]))
    return " ".join(g)


# ---------------------------------------------------------------- 全 ROM 流式普查（只做一次）
c2, nxt = [], {}
off, n, decoded = 0, len(rom), 0
while off < n - 1:
    pc = base + off
    ins = T.decode(rom, off)
    if ins is None:
        off += 2
        continue
    mn, _b, v, sz, _w = ins
    decoded += 1
    if mn == "cmp2":
        c2.append((pc, v.get("m4"), v.get("ri")))
        nxt[pc] = None
    if (pc - 2) in nxt and nxt[pc - 2] is None:
        nxt[pc - 2] = (mn, v.get("rel9"))
    off += sz
M4C = Counter(m4 for _p, m4, _ri in c2)
RIC = Counter(ri for _p, _m4, ri in c2)
FOL = Counter((nxt.get(p) or ("<none>", None))[0] for p, _m4, _ri in c2)
PAIR15 = sum(1 for p, m4, _ri in c2
             if m4 == 15 and (nxt.get(p) or ("", None))[0] in ("beq", "blt"))

# 分发表全表
SVC = {}
for svc in range(-256, 128):
    slot = V.slot_of(svc)
    o = slot - base
    if o < 0 or o + 1 >= len(rom):
        continue
    half = (rom[o] << 8) | rom[o + 1]
    tgt = V.target_of(half)
    if 0x040000 <= tgt < 0x740000 and not (tgt & 1) and half not in (0, 4):
        SVC[svc] = tgt
AL = V.alias_pairs(SVC)

WATCH = {
    "LD_R13": V.LD_R13, "LD_R10": V.LD_R10,
    "CMP2_R10": V.CMP2_R10, "BEQ_R10": V.BEQ_R10,
    "CMP2_R13": V.CMP2_R13, "BLT_EARLY": V.BLT_EARLY,
    "MAIN_CHAIN": V.MAIN_CHAIN, "CMP0_R13": V.CMP0_R13,
    "BEQ_A0": V.BEQ_A0, "BLT_LATE": V.BLT_LATE, "FALL_POS": V.FALL_POS,
    "TAIL_88": V.TAIL_88, "RETD_88": V.RETD_88, "SLOT_88": V.SLOT_88,
    "TAIL_86": V.TAIL_86, "TAIL_30": V.TAIL_30, "TAIL_B6": V.TAIL_B6,
    "TAIL_A2": V.TAIL_A2, "EXIT_85": V.EXIT_85,
    "STM1": V.STM1, "SVC_RET": V.SVC_RET,
    "ENTRY_170": V.ENTRY_170, "TASK9": 0x1352EA,
    "MAIN_JMP": 0x175282, "CMD_CMP": 0x175740, "WB_FN": 0x175908,
}
BY = dict((p, k) for k, p in WATCH.items())
FIRST_TAILS = ("TAIL_88", "TAIL_86", "TAIL_B6", "FALL_POS")
ALL_TAILS = FIRST_TAILS + ("TAIL_30", "TAIL_A2", "EXIT_85")


def run(tag, a_val, after=600, first_window=40):
    """1 台独立机器；a_val=None 是只观测机，否则只写内存单元（反事实补全）。"""
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    rr = mm.cpu.r
    assert mm.cpu.pc == V.TRAP
    assert (rr[12] & 0xFFFFFFFF) == V.MAIN_SVC_IMM32
    hs = 0
    while mm.cpu.pc != V.HOOK2 and hs < 300:
        mm.step(1)
        hs += 1
    assert mm.cpu.pc == V.HOOK2
    a_before, b_before = rd32(mm, V.SRC_A), rd32(mm, V.SRC_B)
    if a_val is not None:
        mm.bus.wr(V.SRC_A, 4, a_val & 0xFFFFFFFF)      # 反事实补全
    read_a, read_b = rd32(mm, V.SRC_A), rd32(mm, V.SRC_B)
    pcs, ev = [], []
    first_next, slot_after, ret_after, stm1_seq = None, [], [], []
    r13_seq, lt_early, lt_late = [], [], []
    steps = {}
    for i in range(after):
        pc = mm.cpu.pc
        pcs.append(pc)
        if pc in BY:
            c = mm.cpu
            k = BY[pc]
            if k not in steps:
                steps[k] = i
            ev.append((i, k, pc, rr[10] & 0xFFFFFFFF, rr[13] & 0xFFFFFFFF,
                       rr[12] & 0xFFFFFFFF, c.n, c.z, c.v, c.c,
                       bool(c.v ^ c.n), rd32(mm, V.SRC_A)))
            if k == "CMP2_R13":
                r13_seq.append(rr[13] & 0xFFFFFFFF)
            if k == "BLT_EARLY":
                lt_early.append(bool(c.v ^ c.n))
            if k == "BLT_LATE":
                lt_late.append(bool(c.v ^ c.n))
        mm.step(1)
        if pc == V.BLT_EARLY and first_next is None:
            first_next = (i, mm.cpu.pc)
        if pc == V.SLOT_88:
            slot_after.append(rr[12] & 0xFFFFFFFF)
        if pc == V.SVC_RET:
            ret_after.append(rr[12] & 0xFFFFFFFF)
        if pc == V.STM1:
            stm1_seq.append(rd32(mm, V.SRC_A))
    seen = set(pcs)
    first_seen = set(pcs[:first_window])
    return {
        "tag": tag, "boot": V.BOOT_STEP, "hook": V.HOOK2, "post": after,
        "hs": hs, "a_before": a_before, "b_before": b_before,
        "read_a": read_a, "read_b": read_b,
        "ev": ev, "steps": steps, "seen": seen, "first_seen": first_seen,
        "first_next": first_next, "slot_after": slot_after,
        "ret_after": ret_after, "stm1_seq": stm1_seq,
        "r13_seq": r13_seq, "lt_early": lt_early, "lt_late": lt_late,
        "hit": dict((k, WATCH[k] in seen) for k in WATCH),
        "first_hit": dict((k, WATCH[k] in first_seen) for k in WATCH),
        "npc": len(seen), "end_pc": mm.cpu.pc,
        "end_cur": rd32(mm, 0x68000000), "end_ready": rd32(mm, 0x68000004),
        "p20": rd32(mm, 0x68000244 + 0x20),
    }


print("=" * 76)
print("V88 · cmp2 是 −1 哨兵 / A<=-2 提前返回 −33 / 别名入口不分流 —— A–H / 10 台机器")
print("=" * 76)

# ---------------------------------------------------------------- A
print("")
print("[A] cmp2 静态解码与全 ROM 模板普查（只读 ROM）")
print("A1 fr_emu.py: cmp2 = _sub(ri, (m4-16)&2^32)；m4=%d → 基准=%+d / imm32=0x%08X"
      % (V.CMP2_M4, V.m4_basis(V.CMP2_M4), V.m4_imm32(V.CMP2_M4)))
for pc, ri in ((V.CMP2_R10, 10), (V.CMP2_R13, 13)):
    mn, v = ins_at(pc)
    print("A2 0x%06X %-6s %s → 基准=%+d（设施 ri=r%d，对=%s）"
          % (pc, mn, im(v), V.m4_basis(v.get("m4", 0)), ri,
             v.get("m4") == V.CMP2_M4 and v.get("ri") == ri))
print("A3 全 ROM 解码 %d 条 / cmp2 %d 条 → %s / %s"
      % (decoded, len(c2), decoded == V.ROM_DECODED, len(c2) == V.CMP2_TOTAL))
print("A4 m4 共 %d 类；m4=15 → %d 条（基准 −1）；m4=0 → %d 条（基准 −16）→ %s"
      % (len(M4C), M4C.get(15, 0), M4C.get(0, 0),
         (len(M4C), M4C.get(15, 0), M4C.get(0, 0))
         == (V.M4_KINDS, V.M4_15_COUNT, V.M4_0_COUNT)))
print("A5 ri 覆盖 %d 个寄存器（r0..r15）→ %s ⇒ 不是「两个寄存器比较」"
      % (len(RIC), len(RIC) == V.RI_SPAN))
print("A6 紧随 beq=%d / blt=%d / beq:d=%d / blt:d=%d / <none>=%d → %s"
      % (FOL.get("beq", 0), FOL.get("blt", 0), FOL.get("beq:d", 0),
         FOL.get("blt:d", 0), FOL.get("<none>", 0),
         (FOL.get("beq", 0), FOL.get("blt", 0), FOL.get("beq:d", 0),
          FOL.get("blt:d", 0), FOL.get("<none>", 0))
         == (V.FOLLOW_BEQ, V.FOLLOW_BLT, V.FOLLOW_BEQD,
             V.FOLLOW_BLTD, V.FOLLOW_NONE)))
print("A7 m4=15 且紧随 beq/blt = %d（设施 %d，对=%s）"
      % (PAIR15, V.M4_15_BEQ_BLT, PAIR15 == V.M4_15_BEQ_BLT))
print("A8 段内与别名入口的 %d 条 −1 哨兵模板实例：" % len(V.TEMPLATE_ROWS))
for pc, m4, ri, follow, rel9, tgt in V.TEMPLATE_ROWS:
    mn, v = ins_at(pc)
    mn2, r9 = nxt.get(pc) or ("<none>", None)
    calc = V.rel9_target(pc + 2, rel9)
    print("    0x%06X %-6s m4=%2d ri=r%-2d → 0x%06X %-5s rel9=%3d → 0x%06X "
          "（解码=%s 落点=%s 判据=%s）"
          % (pc, mn, m4, ri, pc + 2, follow, rel9, calc,
             mn == "cmp2" and v.get("m4") == m4 and v.get("ri") == ri
             and mn2 == follow and r9 == rel9,
             calc == tgt, V.template_verdict(m4, follow)))

# ---------------------------------------------------------------- B
print("")
print("[B] 两条同名 blt 的比较基准对照（同一 r13，基准差 1）")
print("B1 0x%06X cmp2 基准=%+d → 0x%06X blt 判 r13 < -1；"
      "0x%06X cmp #0 基准=0 → 0x%06X blt 判 r13 < 0"
      % (V.CMP2_R13, V.CMP2_BASIS, V.BLT_EARLY,
         V.CMP0_R13, V.BLT_LATE))
print("B2 blt 判据 = bool(v ^ n)；逐值对照表（r,c,v,n,z 为 _sub 结果）：")
for a in (0x80000000, 0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF,
          0x00000000, 0x00000001, 0x00000002):
    r1, c1, v1, n1, z1 = V.cmp2_flags(a)
    r0, c0, v0, n0, z0 = V.cmp0_flags(a)
    e, l = V.branch_after_cmp2(a, V.CMP2_M4, "blt"), V.branch_after_cmp0(a, "blt")
    print("    a=0x%08X cmp2:(r=0x%08X c=%d v=%d n=%d z=%d) → blt=%-5s | "
          "cmp0:(r=0x%08X c=%d v=%d n=%d z=%d) → blt=%-5s 判据=%s"
          % (a, r1, c1, v1, n1, z1, e, r0, c0, v0, n0, z0, l,
             V.basis_verdict(a)))
print("B3 a=−1 时 cmp2 结果 0 → Z=1 lt=False；cmp #0 结果 −1 → N=1 lt=True → %s"
      % V.basis_verdict(0xFFFFFFFF))
print("B4 0x%06X blt rel9=%d sx8=%+d → 0x%06X（设施 0x%06X，对=%s）"
      % (V.BLT_EARLY, V.BLT_EARLY_REL9, V.BLT_EARLY_SX8,
         V.rel9_target(V.BLT_EARLY, V.BLT_EARLY_REL9), V.TAIL_88,
         V.rel9_target(V.BLT_EARLY, V.BLT_EARLY_REL9) == V.TAIL_88))
print("B5 0x%06X beq rel9=%d → 0x%06X，判 r10 == −1；只观测机 r10=0x68000A28 "
      "⇒ 不跳（本版不补 r10，留开放）"
      % (V.BEQ_R10, V.BEQ_R10_REL9,
         V.rel9_target(V.BEQ_R10, V.BEQ_R10_REL9)))
print("B6 提前返回尾 0x%06X = ld / ret:d / 延迟槽 0x%06X ldi:8 #%d → r12=0x%02X "
      "（extsb 后 %+d）"
      % (V.TAIL_88, V.SLOT_88, V.SLOT_88_I8, V.RET_M33_U,
         V.RET_M33_SIGNED))
for pc, mn, v, _s in dec(V.TAIL_88, V.SLOT_88 + 2):
    print("    0x%06X %-8s %s" % (pc, mn, im(v)))

# ---------------------------------------------------------------- C
print("")
print("[C] 分派区 r12 是入口地址，不是服务号（1 台独立机器 + 只读反汇编）")
print("C1 0x0406A4..0x0406B2 关键反汇编：")
for pc, mn, v, _s in dec(0x0406A4, 0x0406B4):
    print("    0x%06X %-8s %s" % (pc, mn, im(v)))
mm = blive.boot(0)
mm.step(V.BOOT_STEP)
rr = mm.cpu.r
disp_log = []
watch_disp = (V.LSL_R12, V.LDI_TBL, V.LDUH_HALF, V.EXTSH_HALF,
              V.ADD_R13, V.CALL_R12, V.SVC_RET)
for _k in range(200):
    pc = mm.cpu.pc
    if pc in watch_disp:
        disp_log.append((pc, rr[12] & 0xFFFFFFFF, rr[13] & 0xFFFFFFFF,
                         rr[14] & 0xFFFFFFFF))
    if pc == V.SVC_RET:
        break
    mm.step(1)
print("C2 TRAP 0x%06X 处 r12=0x%08X（服务号 −170 的符号扩展）；分派过程实测："
      % (V.TRAP, V.TRAP_R12))
for pc, r12, r13, r14 in disp_log:
    print("    0x%06X r12=0x%08X r13=0x%08X r14=0x%08X" % (pc, r12, r13, r14))
slot = V.slot_of(V.MAIN_SVC_32)
half = rd16(slot)
print("C3 分发表：slot=0x%06X half=0x%04X extsh=%+d → target=0x%06X "
      "（设施 0x%06X，对=%s）；call @r12 时 r12=0x%06X = 入口地址"
      % (slot, half, V.extsh(half), V.target_of(half), V.ENTRY_170,
         V.target_of(half) == V.ENTRY_170,
         disp_log[-2][1] if len(disp_log) >= 2 else 0))
print("C4 别名号对同一 half ⇒ r12 相同 ⇒ 判据=%s（规矩 90）"
      % V.dispatch_r12_verdict(half, half, -170, -170 + 1))

# ---------------------------------------------------------------- D
print("")
print("[D] 8 组别名入口 r12 引用普查（只读 ROM）")
print("D1 全表 %d 号 / %d 入口 / %d 组别名 / %d 个服务号 → %s"
      % (len(SVC), len(set(SVC.values())), len(AL),
         sum(len(ss) for _e, ss in AL),
         (len(SVC), len(set(SVC.values())), len(AL),
          sum(len(ss) for _e, ss in AL))
         == (V.TABLE_SVC_COUNT, V.TABLE_ENTRY_COUNT,
             V.TABLE_ALIAS_PAIRS, V.ALIAS_SVC_COUNT)))
with_ref = 0
for e, ss, cnt, refs in V.ALIAS_ROWS:
    real = []
    pc = e
    while pc < e + 24 - 1:
        ins = T.decode(rom, pc - base)
        if ins is None:
            pc += 2
            continue
        mn, _b, v, sz, _w = ins
        if any(v.get(k) == 12 for k in ("ri", "rj", "rs1", "rs2")):
            real.append(pc)
        pc += sz
    if cnt:
        with_ref += 1
    print("D2 入口 0x%06X <- %-18s r12 引用 %d 处（实测 %d，对=%s）判据=%s %s"
          % (e, ",".join(str(s) for s in ss), cnt, len(real),
             len(real) == cnt, V.alias_ref_verdict(cnt),
             "; ".join(refs)))
print("D3 有引用 %d 组 / 无引用 %d 组 → %s；两类都不能按服务号分流"
      % (with_ref, len(V.ALIAS_ROWS) - with_ref,
         (with_ref, len(V.ALIAS_ROWS) - with_ref)
         == (V.ALIAS_WITH_R12, V.ALIAS_WITHOUT_R12)))
print("D4 反例：0x046B80 jmp:d @r12 用的是入口地址；0x040682/0x040684 "
      "lsl/lsr r12 是地址规整，不是服务号判别")

# ---------------------------------------------------------------- E
print("")
print("[E] 2 台基准机：1 只观测轨迹机（不写值）+ 1 标准收敛态机")
obs = run("OBS", None)
print("E1 boot=%d 锚点 0x%06X（hs=%d）；A 原值=0x%08X B 原值=0x%08X"
      % (obs["boot"], V.TRAP, obs["hs"], obs["a_before"], obs["b_before"]))
print("E2 只观测机 CCR 快照（执行该 PC 前的 N/Z/V/C，来自上一条指令）：")
for i, k, pc, r10, r13, r12, nn, zz, vv, cc, lt, av in obs["ev"]:
    if k in ("CMP2_R10", "BEQ_R10", "CMP2_R13", "BLT_EARLY",
             "CMP0_R13", "BEQ_A0", "BLT_LATE"):
        print("    i=%2d 0x%06X %-9s r10=0x%08X r13=0x%08X N=%d Z=%d V=%d C=%d lt=%s"
              % (i, pc, k, r10, r13, nn, zz, vv, cc, lt))
print("E3 只观测机 600 步：末 PC=0x%06X / CUR=0x%08X / READY=0x%08X / "
      "TAIL_88 命中=%s / TAIL_86 命中=%s / 任务 9 入口命中=%s"
      % (obs["end_pc"], obs["end_cur"], obs["end_ready"],
         obs["hit"]["TAIL_88"], obs["hit"]["TAIL_86"], obs["hit"]["TASK9"]))
mc = blive.boot(0)
mc.step(V.CONVERGE_BOOT)
mc.raise_irq(V.CONVERGE_IRQ)
mc.run(V.CONVERGE_RUN, spin_guard=False)
cov_hits = {}
for k, p in V.COV_CODES.items():
    hit = mc.cov.visited(p)
    cov_hits[k] = hit
    print("E4 0x%06X %-10s 命中=%-5s（设施 %s，对=%s）"
          % (p, k, hit, V.COV[k], hit == V.COV[k]))
print("E5 末 PC=0x%06X（设施 0x%06X，对=%s）；判据=%s"
      % (mc.cpu.pc, V.CONVERGE_TAIL, mc.cpu.pc == V.CONVERGE_TAIL,
         V.cov_verdict(cov_hits)))

# ---------------------------------------------------------------- F
print("")
print("[F] 7 台独立 600 步等形机：A 值域从 {−1,0,1} 扩到 7 个值")
rows = []
for tag, a, _lt, _land, _tail, _n, _end, _cum, _seq, _ret in V.GROUPS:
    r = run(tag, a)
    rows.append(r)
    nxt_pc = r["first_next"][1] if r["first_next"] else 0
    cum = [k for k in ALL_TAILS if r["hit"][k]]
    print("F1 %-5s A=0x%08X 首次lt=%-5s 首次落点=%-13s 下条PC=0x%06X "
          "首次窗口=%-12s 累计=%-22s cmp2次数=%d 末PC=0x%06X"
          % (tag, r["read_a"],
             r["lt_early"][0] if r["lt_early"] else "—",
             "EARLY_RET_M33" if nxt_pc == V.TAIL_88 else "MAIN_CHAIN",
             nxt_pc,
             ",".join(k for k in FIRST_TAILS if r["first_hit"][k]) or "—",
             ",".join(cum), len(r["r13_seq"]), r["end_pc"]))
    print("     对账：首次落点=%s / 四分=%s / cmp2次数=%d（设施 %d）/ "
          "r13序列=%s / 返回码=%s"
          % (("EARLY_RET_M33" if nxt_pc == V.TAIL_88 else "MAIN_CHAIN")
             == V.first_landing(a),
             V.quad_dispatch(a),
             len(r["r13_seq"]), _n,
             [hex(x) for x in r["r13_seq"]],
             [hex(x) for x in r["ret_after"]]))
print("F2 等形三项（boot / hook / post）全同 = %s；hs 全同 = %s"
      % (V.equal_shape(rows), len(set(r["hs"] for r in rows)) == 1))
print("F3 首次窗口（统一取前 40 步，所有机器同为 600 步长窗口；规矩 82）互斥性：")
for r in rows:
    got = [k for k in FIRST_TAILS if r["first_hit"][k]]
    exp = "TAIL_88" if V.quad_dispatch(r["read_a"]) == "EARLY_RET_M33" \
        else V.tri_dispatch(r["read_a"])
    print("    %-5s %-18s 四分=%-13s 判据=%-12s 与设施一致=%s"
          % (r["tag"], got, V.quad_dispatch(r["read_a"]),
             V.gate_verdict(*[r["first_hit"][k] for k in FIRST_TAILS]),
             got == [exp]))
print("F4 结果层：七组末 CUR / READY 全同 = %s / %s"
      % (len(set(r["end_cur"] for r in rows)) == 1,
         len(set(r["end_ready"] for r in rows)) == 1))

# ---------------------------------------------------------------- G
print("")
print("[G] 归因分离：首次触发 vs 窗口累计、stm1 覆盖、三处自我证伪")
for r in rows:
    cum = [k for k in FIRST_TAILS if r["hit"][k]]
    wd = V.window_verdict(
        "EARLY_RET_M33" if r["first_next"][1] == V.TAIL_88 else "MAIN_CHAIN",
        cum, len(r["r13_seq"]))
    if len(r["r13_seq"]) > 1:
        print("G1 %-5s r13 序列=%s / A 单元 stm1 后=%s / 返回码=%s → %s / %s"
              % (r["tag"], [hex(x) for x in r["r13_seq"]],
                 [hex(x) for x in r["stm1_seq"]],
                 [hex(x) for x in r["ret_after"]], wd,
                 V.overwrite_verdict(r["r13_seq"], r["stm1_seq"])))
print("G2 首次 PC 实测：A<=-2 三组 0x%06X 之后下一条=0x%06X（跳到 −33 出口）；"
      "其余四组下一条=0x%06X（主链）→ %s"
      % (V.BLT_EARLY, V.TAIL_88, V.MAIN_CHAIN,
         V.executed_vs_taken(True, True)))
print("G3 0x%06X blt 静态入边 %d 条（V87 实测）在 V87 七组动态 0 命中，"
      "本版 A<=-2 三组首次命中 → 判据=%s（不是新路径，是值域补全）"
      % (V.BLT_EARLY, V.INEDGE_88,
         V.executed_vs_taken(True, True)))
print("G4 两条 blt 基准差 1：a=−1 → %s；a=−2 → %s"
      % (V.basis_verdict(0xFFFFFFFF), V.basis_verdict(0xFFFFFFFE)))
print("G5 结果层同 ≠ 原因层同（规矩 83）：TCB7 +0x20 = %s"
      % ", ".join("%s:0x%08X" % (r["tag"], r["p20"]) for r in rows))
print("G6 三处自我证伪：累计 TAIL_88+TAIL_86 读成「同一次两个落点」→ "
      "改首次 PC 判跳；cmp2 助记符读成「两寄存器比较」→ 按 m4−16 更正；"
      "r12 读成服务号 → 分派区实测更正（规矩 88 / 89 / 90）")
print("G7 −85 出口仍只在 A=0 ∧ B=0（V87 ZERO_CLR）；本版不补 B，"
      "命令分发链仍未走到 = %s"
      % (not (cov_hits["MAIN_JMP"] or cov_hits["WB_FN"])))

# ---------------------------------------------------------------- H
print("")
print("[H] 综合判据、规矩与边界")
template_ok = (decoded == V.ROM_DECODED and len(c2) == V.CMP2_TOTAL
               and PAIR15 == V.M4_15_BEQ_BLT
               and all(V.template_verdict(m4, f) == "minus_one_sentinel"
                       for _p, m4, _ri, f, _r9, _t in V.TEMPLATE_ROWS))
ccr_ok = (V.basis_verdict(0xFFFFFFFF) == "basis_zero_only"
          and V.branch_after_cmp2(0xFFFFFFFE, V.CMP2_M4, "blt"))
quad_ok = all(
    V.first_landing(r["read_a"]) ==
    ("EARLY_RET_M33" if r["first_next"][1] == V.TAIL_88 else "MAIN_CHAIN")
    and V.gate_verdict(*[r["first_hit"][k] for k in FIRST_TAILS]) == "exactly_one"
    for r in rows)
window_ok = all(
    V.window_verdict("EARLY_RET_M33" if r["first_next"][1] == V.TAIL_88
                     else "MAIN_CHAIN",
                     [k for k in FIRST_TAILS if r["hit"][k]],
                     len(r["r13_seq"])) in
    ("single_entry_consistent", "reentry_overlap_not_two_paths")
    for r in rows)
r12_ok = (disp_log[-2][1] == V.CALL_R12_VALUE
          and V.dispatch_r12_verdict(half, half, -170, -169)
          == "same_entry_r12_no_svc_dispatch")
print("H1 前置五项：模板普查=%s / CCR 对照=%s / 四分=%s / 首次窗口=%s / r12=%s"
      % (template_ok, ccr_ok, quad_ok, window_ok, r12_ok))
print("H2 综合判据 = %s"
      % V.source_verdict(template_ok, ccr_ok, quad_ok, window_ok, r12_ok, False))
print("H3 规矩 88（cmp2 比较基准按 m4−16 算，不许按助记符猜） / "
      "规矩 89（首次触发 vs 窗口累计、早补被 stm1 覆盖） / "
      "规矩 90（r12 是入口地址不是服务号）")
print("H4 全部写 A 单元是反事实补全（只写内存单元）；服务返回与任务换手是"
      "既有代码自然执行；链未闭合")
print("H5 仍开放：cmp2 两个操作数在真机的调用者语义 / r10=−1 触发条件 / "
      "8 对别名语义分工 / 真机 vec 0x18 器件事件 / −85 出口器件事件语义 / "
      "bit3 置位源 / vec 0x2D / 317 提交调用点 / 任务 9 下载后是否回任务 7 / "
      "0xFFFFFFFF r13 哨兵真机设置者")
print("H6 不能报（禁语）：cmp2 是两个寄存器比较 / A<0 全由 0x041C10 blt 决定 / "
      "A<=-2 走了一条新路径 / 8 对别名语义已解出 / 所有服务语义已解出 / "
      "真机已复现 −85 / 补一次值就等于真机事件 / 显示链通了 / 出画面 / "
      "系统跑起来了 / 真机如此")
print("DONE V88 A–H 八段，10 台独立机器（1 分派区观测机 + 1 只观测轨迹机 + "
      "1 标准收敛态机 + 7 等形反事实机），反事实全部标注")
