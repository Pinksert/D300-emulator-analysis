# -*- coding: utf-8 -*-
"""V87 正式探针：不是服务复用，是按 r13 符号三分汇入共享尾（KI#339）。

    cd emu && python probe/exp_svc_v87.py > ../results/frbox/svc87.txt

A–C 段只读 ROM；D 段 1 台只观测轨迹机；E 段 1 台标准收敛态机；
F/G/H 段 7 台独立 600 步等形机。合计 **9 台独立机器**，每台只跑一遍。
所有往 0x000083E4 / 0x68000970 写值都是**反事实补全**，不是固件行为、
不是真机器件事件。链未闭合。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from frbox import bstatic, svc87 as V, blive

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


def rd16(a):
    o = a - base
    return (rom[o] << 8) | rom[o + 1]


def rd32(m, a):
    return m.bus.rd(a, 4) & 0xFFFFFFFF


def ins_at(pc):
    ins = fr_emu.TABLE.decode(rom, pc - base)
    return ("??", {}) if ins is None else (ins[0], dict(ins[2]))


def dec(lo, hi):
    out, pc = [], lo
    while pc < hi - 1:
        ins = fr_emu.TABLE.decode(rom, pc - base)
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
    for k in ("i8", "i20", "i32", "u4", "u8", "u6", "disp10", "disp9",
              "disp8", "rel9", "rel12", "udisp6"):
        if k in v:
            g.append("%s=%d(0x%X)" % (k, v[k], v[k] & 0xFFFFFFFF))
    for k in ("ri", "rj", "rs2", "cc"):
        if k in v:
            g.append("%s=%s" % (k, ("r%d" % v[k]) if k in ("ri", "rj") else v[k]))
    return " ".join(g)


# 全 ROM 静态入边普查（只做一次，B 段复用）
REL9 = ("bra", "beq", "bne", "blt", "bgt", "ble", "bge", "bls", "bhi",
        "bc", "bnc", "bn", "bp", "bv", "bnv", "beq:d", "bne:d")
REL12 = ("call", "jmp", "call:d", "jmp:d")
EDGES = {}
off, n = 0, len(rom)
while off < n - 1:
    pc = base + off
    ins = fr_emu.TABLE.decode(rom, off)
    if ins is None:
        off += 2
        continue
    mn, _b, v, sz, _w = ins
    tgt = None
    if mn in REL9 and "rel9" in v:
        tgt = V.rel9_target(pc, v["rel9"])
    elif mn in REL12 and "rel12" in v:
        tgt = V.rel12_target(pc, v["rel12"])
    if tgt is not None:
        EDGES.setdefault(tgt, []).append((pc, mn))
    off += sz

# 分发表全表
SVC_TO_ENTRY = {}
for svc in range(-256, 128):
    slot = V.slot_of(svc)
    o = slot - base
    if o < 0 or o + 1 >= len(rom):
        continue
    half = (rom[o] << 8) | rom[o + 1]
    tgt = V.target_of(half)
    if 0x040000 <= tgt < 0x740000 and not (tgt & 1) and half not in (0, 4):
        SVC_TO_ENTRY[svc] = tgt
ALL_ENTRIES = set(SVC_TO_ENTRY.values())
ALIASES = V.alias_pairs(SVC_TO_ENTRY)

WATCH = {
    "CMP2_R10": V.CMP2_R10, "BEQ_R10": V.BEQ_R10,
    "CMP2_R13": V.CMP2_R13, "BLT_EARLY": V.BLT_EARLY,
    "CALL_170": V.CALL_170, "BNE_1E": V.BNE_1E,
    "COMMON_SUB": V.COMMON_SUB,
    "CMP0_R13": V.CMP0_R13, "BEQ_A0": V.BEQ_A0,
    "BLT_LATE": V.BLT_LATE, "FALL_POS": V.FALL_POS,
    "TAIL_86": V.TAIL_86, "TAIL_88": V.TAIL_88, "TAIL_8E": V.TAIL_8E,
    "TAIL_30": V.TAIL_30, "TAIL_B6": V.TAIL_B6, "TAIL_E4": V.TAIL_E4,
    "TAIL_A2": V.TAIL_A2, "TAIL_9A": V.TAIL_9A,
    "SEG106_ENTRY": V.ENTRY_106, "SEG170_ENTRY": V.ENTRY_170,
    "ANDCCR_106": V.ANDCCR_106, "ANDCCR_170": V.ANDCCR_170,
    "LDI_R0_106": V.LDI_R0_106, "CALL_106": V.CALL_106,
    "BNE_106": V.BNE_106, "BEQ_106": V.BEQ_106,
    "EXIT_85": V.EXIT_85, "RET_D_85": V.RET_D_85, "SLOT_85": V.SLOT_85,
    "MAIN_JMP": V.MAIN_JMP, "CMD_SEG": 0x17573C, "CMD_CMP": 0x175740,
    "WB_CALL": 0x17574C, "WB_FN": V.WB_FN,
    "SVC_RET": 0x0406B4, "EXTSB": 0x0406BE,
    "TRAP_RETI": 0x0406E2, "SCHED_RETI": 0x040EB6,
    "TASK9": 0x1352EA, "CONST_LDI": 0x1378FA,
}
GATE_KEYS = ("MAIN_JMP", "CMD_SEG", "CMD_CMP", "WB_CALL", "WB_FN")


def run(tag, patch, after=600):
    """1 台独立机器；patch 为空就是只观测机。"""
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
    for a, v in patch:
        mm.bus.wr(a, 4, v)                     # 反事实补全
    read_a, read_b = rd32(mm, V.SRC_A), rd32(mm, V.SRC_B)
    steps, pcs, ret12, r13_cmp, r13_fall = {}, [], None, None, None
    for i in range(after):
        pc = mm.cpu.pc
        pcs.append(pc)
        for k, p in WATCH.items():
            if pc == p and k not in steps:
                steps[k] = i
        if pc == V.CMP0_R13 and r13_cmp is None:
            r13_cmp = rr[13] & 0xFFFFFFFF
        if pc == V.FALL_POS and r13_fall is None:
            r13_fall = rr[13] & 0xFFFFFFFF
        if pc == 0x0406B4 and ret12 is None:
            ret12 = rr[12] & 0xFFFFFFFF
        mm.step(1)
    seen = set(pcs)
    return {
        "tag": tag, "boot": V.BOOT_STEP, "hook": V.HOOK2, "post": after,
        "hs": hs, "a_before": a_before, "b_before": b_before,
        "read_a": read_a, "read_b": read_b, "ret12": ret12,
        "r13_cmp": r13_cmp, "r13_fall": r13_fall,
        "steps": steps, "hit": dict((k, p in seen) for k, p in WATCH.items()),
        "npc": len(seen), "end_pc": mm.cpu.pc,
        "end_r4": rr[4] & 0xFFFFFFFF,
        "end_cur": rd32(mm, 0x68000000),
        "end_ready": rd32(mm, 0x68000004),
        "p14": rd32(mm, 0x68000244 + 0x14), "p20": rd32(mm, 0x68000244 + 0x20),
    }


print("=" * 76)
print("V87 · 不是服务复用，是按 r13 符号三分汇入共享尾 —— A–H / 9 台机器")
print("=" * 76)

# ---------------------------------------------------------------- A
print("")
print("[A] 静态图：分发表两口径、五条 rel9 与 bra 落点（只读 ROM）")
print("A1 真内核 32 位有符号口径（规矩 76）：slot=(0x040660+(svc<<1))&2^32，"
      "target=(0x040660+extsh(half))&2^32")
for svc, slot, half, entry in (
        (-46, V.SLOT_46, V.HALF_46, V.ENTRY_46),
        (-106, V.SLOT_106, V.HALF_106, V.ENTRY_106),
        (-170, V.SLOT_170, V.HALF_170, V.ENTRY_170),
        (-44, V.SLOT_44, V.HALF_44, V.ENTRY_44),
        (-172, V.SLOT_172, V.HALF_172, V.ENTRY_172)):
    calc_slot, half_real, calc_tgt = V.slot_of(svc), rd16(V.slot_of(svc)), V.target_of(rd16(V.slot_of(svc)))
    print("    svc=%4d slot=0x%06X(对=%s) half=0x%04X(对=%s) -> 0x%06X(对=%s)"
          % (svc, calc_slot, calc_slot == slot, half_real, half_real == half,
             calc_tgt, calc_tgt == entry))
print("A2 段内 0x041B00..0x041E80：%d 个服务号 / %d 个不同入口（V83「12 入口共享尾」）"
      % (V.SEG_SVC_COUNT, V.SEG_ENTRY_COUNT))
seg = [(s, e) for s, e in SVC_TO_ENTRY.items() if V.SEG_LO <= e < V.SEG_HI]
print("    实测段内 = %d 个服务号 / %d 个入口 → %s / %s"
      % (len(seg), len(set(e for _s, e in seg)),
         len(seg) == V.SEG_SVC_COUNT, len(set(e for _s, e in seg)) == V.SEG_ENTRY_COUNT))
print("A3 全表：%d 个服务号落 ROM 内 / %d 个不同入口 / %d 对别名 → %s"
      % (len(SVC_TO_ENTRY), len(ALL_ENTRIES), len(ALIASES),
         (len(SVC_TO_ENTRY), len(ALL_ENTRIES), len(ALIASES))
         == (V.TABLE_SVC_COUNT, V.TABLE_ENTRY_COUNT, V.TABLE_ALIAS_PAIRS)))
print("A4 段内 ≠ 全表口径判据 = %s（规矩 78 / 87）"
      % V.scope_verdict(V.SEG_SVC_COUNT, V.SEG_ENTRY_COUNT,
                        V.TABLE_SVC_COUNT, V.TABLE_ENTRY_COUNT))
for e, ss in ALIASES:
    print("    别名 0x%06X <- %s" % (e, ", ".join(str(s) for s in ss)))
print("A5 −106 入口 %s / −170 入口 %s；两个服务号是否同入口（别名）= %s"
      % ([s for s, e in SVC_TO_ENTRY.items() if e == V.ENTRY_106],
         [s for s, e in SVC_TO_ENTRY.items() if e == V.ENTRY_170],
         SVC_TO_ENTRY.get(-106) == SVC_TO_ENTRY.get(-170)))
print("A6 五条 rel9 落点（pc+2+sx8*2）：")
for pc, rel9, s, tgt in V.REL9_ROWS:
    mn, v = ins_at(pc)
    calc = V.rel9_target(pc, rel9)
    print("    0x%06X %-5s rel9=%3d sx8=%+4d → 0x%06X（设施 0x%06X，对=%s，解码=%s）"
          % (pc, mn, rel9, s, calc, tgt, calc == tgt, v.get("rel9") == rel9))
print("A7 0x041B86 bra rel9=%d sx8=%+d → 0x%06X（设施 0x%06X，对=%s）"
      % (V.BRA_86_REL9, V.BRA_86_SX8, V.rel9_target(V.BRA_86, V.BRA_86_REL9),
         V.BRA_86_TARGET, V.rel9_target(V.BRA_86, V.BRA_86_REL9) == V.BRA_86_TARGET))

# ---------------------------------------------------------------- B
print("")
print("[B] 共享尾 vs 服务入口：六个跳转目标都不是分发表入口（只读 ROM）")
TARGETS = (
    ("TAIL_86", V.TAIL_86, V.INEDGE_86),
    ("TAIL_B6", V.TAIL_B6, V.INEDGE_B6),
    ("TAIL_E4", V.TAIL_E4, V.INEDGE_E4),
    ("TAIL_30", V.TAIL_30, None),
    ("TAIL_88", V.TAIL_88, None),
    ("TAIL_A2", V.TAIL_A2, V.INEDGE_A2),
)
for k, t, exp in TARGETS:
    rows = EDGES.get(t, [])
    is_e = V.is_dispatch_entry(t, ALL_ENTRIES)
    vd = V.shared_tail_verdict(t, ALL_ENTRIES, len(rows), True)
    print("B1 %-8s 0x%06X 是入口=%-5s 静态入边=%d%s 判据=%s"
          % (k, t, is_e, len(rows),
             ("（设施 %d，对=%s）" % (exp, len(rows) == exp)) if exp is not None else "",
             vd))
print("B2 六个目标全不是入口 = %s；综合「不是服务复用」= %s"
      % (all(not V.is_dispatch_entry(t, ALL_ENTRIES) for _k, t, _e in TARGETS),
         V.shared_tail_verdict(V.TAIL_B6, ALL_ENTRIES, V.INEDGE_B6, True)
         == "shared_tail"))
print("B3 0x041BE4 公共快速返回尾的两条外部入边：")
for t, p, mn in [(t, p, m) for t, rows in EDGES.items() for p, m in rows
                 if t == V.TAIL_E4]:
    segname = "−46 段" if p < V.ENTRY_106 else (
        "−106 段" if p < V.ENTRY_170 else "−170 段")
    print("    0x%06X <- 0x%06X %-5s 归属 %s" % (t, p, mn, segname))
print("B4 −85 出口 0x%06X 入边 %d 条：%s"
      % (V.TAIL_9A, len(EDGES.get(V.TAIL_9A, [])),
         ", ".join("0x%06X/%s" % (p, m) for p, m in EDGES.get(V.TAIL_9A, []))))
print("B5 分发表入口静态入边（间接 call @r12，静态扫不到；规矩 76 / 68）："
      "0x041B92=%d 0x041BEA=%d"
      % (len(EDGES.get(V.ENTRY_106, [])), len(EDGES.get(V.ENTRY_170, []))))

# ---------------------------------------------------------------- C
print("")
print("[C] −106 号服务顺序落入共享尾；延迟槽紧邻 −170 入口（只读 ROM）")
print("C1 0x041B92..0x041BB6 分支落点（三道校验通过才顺序落入 0x041BB6）：")
for pc, mn, v, _s in dec(V.ENTRY_106, V.TAIL_B6 + 2):
    if "rel9" in v:
        print("    0x%06X  %-8s %-24s -> 0x%06X"
              % (pc, mn, im(v), V.rel9_target(pc, v["rel9"])))
    else:
        print("    0x%06X  %-10s %s" % (pc, mn, im(v)))
print("C2 −106 特有前置 0x%06X ldi:32 #0x68000132,r0 / 0x%06X call；"
      "−170 备上下文 0x%06X andccr #0xEF 后跳过这些前置"
      % (V.LDI_R0_106, V.CALL_106, V.ANDCCR_170))
print("C3 0x041BE0..0x041BEC 反汇编：")
for pc, mn, v, _s in dec(0x041BE0, 0x041BEC):
    print("    0x%06X  %-10s %s" % (pc, mn, im(v)))
print("C4 0x%06X 是 0x%06X ret:d 的延迟槽且紧邻 −170 入口 0x%06X → 判据=%s"
      % (V.SLOT_E4, V.RET_D_E4, V.ENTRY_170,
         V.boundary_verdict(V.SLOT_E4, V.ENTRY_170, True)))
print("C5 两段共用子例程：0x%06X call rel12=%d → 0x%06X；"
      "0x%06X call rel12=%d → 0x%06X；公共子例程 0x%06X（设施一致=%s）"
      % (V.CALL_170, V.CALL_170_REL12, V.rel12_target(V.CALL_170, V.CALL_170_REL12),
         V.CALL_106, V.CALL_106_REL12, V.rel12_target(V.CALL_106, V.CALL_106_REL12),
         V.COMMON_SUB,
         V.rel12_target(V.CALL_170, V.CALL_170_REL12) == V.COMMON_SUB
         and V.rel12_target(V.CALL_106, V.CALL_106_REL12) == V.COMMON_SUB))

# ---------------------------------------------------------------- D
print("")
print("[D] 1 台只观测轨迹机（不写值、不改寄存器、不强制 PC、不注入中断）")
obs = run("NO_PATCH", ())
print("D1 boot=%d 锚点 0x%06X（hs=%d）；A 原值=0x%08X B 原值=0x%08X"
      % (obs["boot"], V.TRAP, obs["hs"], obs["a_before"], obs["b_before"]))
print("D2 0x%06X 读 A 值 r13=0x%08X；A 是调用者 r13 的栈帧保存槽（V84/V86）"
      % (V.CMP0_R13, obs["r13_cmp"]))
print("D3 只观测机 600 步：末 PC=0x%06X / CUR=0x%08X / 返 −85=%s / "
      "任务 9 入口 0x1352EA 命中=%s"
      % (obs["end_pc"], obs["end_cur"], obs["hit"]["EXIT_85"],
         obs["hit"]["TASK9"]))

# ---------------------------------------------------------------- E
print("")
print("[E] 1 台标准收敛态机（V65–V86 逐字相同）")
mc = blive.boot(0)
mc.step(V.CONVERGE_BOOT)
mc.raise_irq(V.CONVERGE_IRQ)
mc.run(V.CONVERGE_RUN, spin_guard=False)
cov_hits = {}
for k, p in V.COV_CODES.items():
    hit = mc.cov.visited(p)
    cov_hits[k] = hit
    print("E1 0x%06X %-14s 命中=%-5s（设施 %s，对=%s）"
          % (p, k, hit, V.COV[k], hit == V.COV[k]))
print("E2 末 PC=0x%06X（设施 0x%06X，对=%s）"
      % (mc.cpu.pc, V.CONVERGE_TAIL, mc.cpu.pc == V.CONVERGE_TAIL))
print("E3 共享判定尾已跑过 / 命令分发链仍未走到 = %s"
      % V.cov_verdict(cov_hits))

# ---------------------------------------------------------------- F
print("")
print("[F] 7 台独立 600 步等形机：A 符号三分 × B 两值")
rows = []
for tag, a, b, tail, ret, reti, minus85 in V.GROUPS:
    r = run(tag, ((V.SRC_A, a), (V.SRC_B, b)))
    rows.append(r)
    print("F1 %-9s A=0x%08X B=0x%08X 汇入=%-8s ret12=%s npc=%3d 返−85=%-5s 调度reti=%3d"
          % (tag, r["read_a"], r["read_b"], tail,
             ("0x%08X" % r["ret12"]) if r["ret12"] is not None else "—",
             r["npc"], r["hit"]["EXIT_85"], r["steps"].get("SCHED_RETI", -1)))
    print("     对账：汇入=%s / ret12=%s / 调度reti=%s / −85出口=%s"
          % (r["hit"][tail], r["ret12"] == ret,
             r["steps"].get("SCHED_RETI", -1) == reti,
             r["hit"]["EXIT_85"] == minus85))
print("F2 等形三项（boot / hook / post）全同 = %s；hs 全同 = %s"
      % (V.equal_shape(rows), len(set(r["hs"] for r in rows)) == 1))
print("F3 每台恰好命中一个落点（用落点判定，不用分支指令执行判定；规矩 87）：")
for r in rows:
    got = [k for k in ("TAIL_86", "TAIL_B6", "FALL_POS") if r["hit"][k]]
    print("    %-9s %-18s 设施三分=%-8s 判据=%s 与设施一致=%s"
          % (r["tag"], got, V.tri_dispatch(r["read_a"]),
             V.gate_verdict(r["hit"]["TAIL_86"], r["hit"]["TAIL_B6"],
                            r["hit"]["FALL_POS"]),
             got == [V.tri_dispatch(r["read_a"])]))
print("F4 结果层：七组末 CUR / READY 全同 = %s / %s"
      % (len(set(r["end_cur"] for r in rows)) == 1,
         len(set(r["end_ready"] for r in rows)) == 1))

# ---------------------------------------------------------------- G
print("")
print("[G] 归因分离与三处自我证伪")
a, b = rows[0], rows[1]
print("G1 SAME_AB ≡ NEG_SET（写原值无效）六项：ret12=%s npc=%s endPC=%s "
      "CUR=%s READY=%s steps=%s"
      % (a["ret12"] == b["ret12"], a["npc"] == b["npc"],
         a["end_pc"] == b["end_pc"], a["end_cur"] == b["end_cur"],
         a["end_ready"] == b["end_ready"], a["steps"] == b["steps"]))
print("G2 0x041C10 blt 七组全执行但全没跳：BLT_EARLY 命中=%d 组 / TAIL_88 命中=%d 组 → %s"
      % (sum(1 for r in rows if r["hit"]["BLT_EARLY"]),
         sum(1 for r in rows if r["hit"]["TAIL_88"]),
         V.executed_vs_taken(True, False)))
print("G2b TAIL_88 静态入边 %d 条（全 ROM）却动态 0 命中 ⇒ 静态可达 ≠ 已经执行（规矩 5 / 87）"
      % len(EDGES.get(V.TAIL_88, [])))
print("G3 真正三分在 0x041C24 cmp #0,r13 之后：BEQ_A0(29)=%s BLT_LATE(30)=%s "
      "FALL_POS(31)=%s；ZERO 组 BLT_LATE=%s"
      % (rows[0]["steps"].get("BEQ_A0"), rows[0]["steps"].get("BLT_LATE"),
         rows[5]["steps"].get("FALL_POS"),
         rows[4]["steps"].get("BLT_LATE", "—")))
print("G4 −85 只在 A=0 ∧ B=0：%s；命令链全命中组=%s"
      % ([r["tag"] for r in rows if r["hit"]["EXIT_85"]],
         [r["tag"] for r in rows
          if all(r["hit"][k] for k in GATE_KEYS)]))
print("G5 结果层同 ≠ 原因层同（规矩 83）：TCB7 +0x20 = %s"
      % ", ".join("%s:0x%08X" % (r["tag"], r["p20"]) for r in rows))
print("G6 自我证伪：段内 14/12 不是全表口径 → %s；"
      "0x041BE8 延迟槽紧邻入口 → %s"
      % (V.scope_verdict(V.SEG_SVC_COUNT, V.SEG_ENTRY_COUNT,
                         V.TABLE_SVC_COUNT, V.TABLE_ENTRY_COUNT),
         V.boundary_verdict(V.SLOT_E4, V.ENTRY_170, True)))

# ---------------------------------------------------------------- H
print("")
print("[H] 综合判据、规矩与边界")
entries_ok = all(not V.is_dispatch_entry(t, ALL_ENTRIES) for _k, t, _e in TARGETS)
inedges_ok = (len(EDGES.get(V.TAIL_B6, [])) == V.INEDGE_B6
              and len(EDGES.get(V.TAIL_86, [])) == V.INEDGE_86
              and len(EDGES.get(V.TAIL_E4, [])) == V.INEDGE_E4)
tri_ok = all(r["hit"][V.tri_dispatch(r["read_a"])] and
             sum(1 for k in ("TAIL_86", "TAIL_B6", "FALL_POS") if r["hit"][k]) == 1
             for r in rows)
cov_ok = V.cov_verdict(cov_hits) == "shared_tail_seen_cmd_chain_unreached"
print("H1 前置四项：目标非入口=%s / 静态入边=%s / 三分=%s / 收敛态覆盖=%s"
      % (entries_ok, inedges_ok, tri_ok, cov_ok))
print("H2 综合判据 = %s"
      % V.source_verdict(entries_ok, inedges_ok, tri_ok, cov_ok, False))
print("H3 规矩 86（服务号别名 vs 共享尾）/ 规矩 87（执行 ≠ 跳转；段内 ≠ 全表）")
print("H4 全部写内存单元是反事实补全；任务换手是既有代码自然执行；链未闭合")
print("H5 仍开放：真机 vec 0x18 器件事件 / −85 出口器件事件语义 / bit3 置位源 / "
      "vec 0x2D / 317 提交调用点 / 任务 9 下载后是否回任务 7 / "
      "0xFFFFFFFF r13 哨兵真机设置者 / 8 对别名语义 / cmp2 真实语义")
print("H6 不能报（禁语）：−170 号服务复用了 −106 号服务 / A<0 由 0x041C10 blt 决定 / "
      "段内 14·12 是全表口径 / 所有服务语义已解出 / 真机已复现 −85 / "
      "显示链通了 / 出画面 / 系统跑起来了 / 真机如此")
print("DONE V87 A–H 八段，9 台独立机器（1 只观测轨迹机 + 1 标准收敛态机 + "
      "7 等形反事实机），反事实全部标注")
