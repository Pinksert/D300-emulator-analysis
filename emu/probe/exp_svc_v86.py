# -*- coding: utf-8 -*-
"""V86 正式探针：没有「优先级」，是返回码层与调度层（承接 KI#335 第一条）。

    cd emu && python probe/exp_svc_v86.py > ../results/frbox/svc86.txt

A 段只读 ROM；B 段 5 台独立 600 步等形组合机；C 段 1 台只观测轨迹机；
D 段 2 台独立 260 步短窗口机；E 段 1 台标准收敛态机；F/G/H 段归约。
合计 **9 台独立机器**，每台只跑一遍。所有往 0x000083E4 / 0x68000970
写值都是**反事实补全**，不是固件行为、不是真机器件事件。链未闭合。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from frbox import bstatic, svc86 as V, blive

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


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


def rd32(m, a):
    return m.bus.rd(a, 4) & 0xFFFFFFFF


WATCH = {}
for _k, _p, _s in V.ORDER:
    WATCH[_k] = _p
WATCH.update({
    "CMP2_R13": V.CMP2_R13, "BLT_EARLY": V.BLT_EARLY,
    "BLT_LATE": V.BLT_LATE, "BNE_106": V.BNE_106,
    "RET_D_85": V.RET_D_85, "TAKE_JUMP": V.TAKE_JUMP,
    "CMD_SEG": V.CMD_SEG, "CMD_CMP": V.CMD_CMP,
    "WB_CALL": V.WB_CALL, "BNE_GATE": V.BNE_GATE,
})

GATE_KEYS = ("MAIN_JMP", "CMD_SEG", "CMD_CMP", "WB_CALL", "WB_FN")
EXIT_KEYS = ("EXIT_85", "RET_D_85", "SLOT_85")


def run(tag, patch, after):
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
        mm.bus.wr(a, 4, v)              # 反事实补全
    read_a, read_b = rd32(mm, V.SRC_A), rd32(mm, V.SRC_B)
    steps, ints, retis, trap_retis, ret12 = {}, [], [], [], None
    pcs = []
    for i in range(after):
        pc = mm.cpu.pc
        pcs.append(pc)
        for k, p in WATCH.items():
            if pc == p and k not in steps:
                steps[k] = i
        if pc == V.RET_PC and ret12 is None:
            ret12 = rr[12] & 0xFFFFFFFF
        if ins_at(pc)[0] == "int":
            ints.append((i, pc, rr[12] & 0xFFFFFFFF))
        if pc == V.SCHED_RETI:
            retis.append(i)
        if pc == V.TRAP_RETI:
            trap_retis.append(i)
        mm.step(1)
    seen = set(pcs)
    hit = dict((k, p in seen) for k, p in WATCH.items())
    return {
        "tag": tag, "boot": V.BOOT_STEP, "hook": V.HOOK2, "post": after,
        "hs": hs, "a_before": a_before, "b_before": b_before,
        "read_a": read_a, "read_b": read_b, "ret12": ret12,
        "steps": steps, "hit": hit, "pcs": pcs, "npc": len(seen),
        "ints": len(ints), "retis": retis, "trap_retis": trap_retis,
        "end_pc": mm.cpu.pc, "end_r4": rr[4] & 0xFFFFFFFF,
        "end_cur": rd32(mm, V.CUR), "end_ready": rd32(mm, V.READY),
        "p14": rd32(mm, V.TCB7 + 0x14), "p20": rd32(mm, V.TCB7 + 0x20),
    }


print("=" * 76)
print("V86 · 没有优先级，是返回码层与调度层 —— A–H 八段 / 9 台机器")
print("=" * 76)

# ---------------------------------------------------------------- A
print("")
print("[A] 静态图：rel9 是 sx8、A 判据跨段跳到 −106 号服务段（只读 ROM）")
print("A1 rel9 落点（规矩 70/85；pc+2+sx8(rel9)*2）：")
for pc, rel9, s, tgt in V.REL9_ROWS:
    mn, v = ins_at(pc)
    calc = V.rel9_target(pc, rel9)
    print("    0x%06X %-5s rel9=%3d sx8=%4d → 0x%06X（设施 0x%06X，对=%s，解码=%s）"
          % (pc, mn, rel9, s, calc, tgt, calc == tgt, v.get("rel9") == rel9))
print("A2 关键：0x%06X beq rel9=%d 的 sx8=%d → 0x%06X（不是 +%d）"
      % (V.BEQ_A_ZERO, V.BEQ_REL9, V.BEQ_SX8, V.SEG106_ENTRY, V.BEQ_REL9))
print("A3 0x%06X / 0x%06X 双双 → 0x%06X（−85 出口入口）= %s"
      % (V.BNE_106, V.BEQ_106, V.EXIT_85,
         V.rel9_target(V.BNE_106, V.BNE_106_REL9) == V.EXIT_85
         and V.rel9_target(V.BEQ_106, V.BEQ_106_REL9) == V.EXIT_85))
print("A4 跨段判据 = %s（V83 已归属 0x041B92 起为 −106 号服务函数体）"
      % V.cross_segment_verdict(V.BEQ_A_ZERO, V.BEQ_REL9, V.SEG106_ENTRY,
                                0x041BEA, 0x041E80))
print("A5 −106 段 B 门反汇编 0x041BB0..0x041BD2：")
for p, mn, v, _s in dec(0x041BB0, 0x041BD2):
    print("    0x%06X %-8s %s" % (p, mn, im(v)))
print("A6 A 判据链：0x%06X cmp2 r13 → 0x%06X blt；0x%06X cmp #0,r13 → "
      "0x%06X beq / 0x%06X blt"
      % (V.CMP2_R13, V.BLT_EARLY, V.CMP0_R13, V.BEQ_A_ZERO, V.BLT_LATE))
print("A7 A 单元推导：r14=0x%08X + sx8(%d)*4 = 0x%08X == 设施 0x%08X → %s"
      % (V.R14_AT_LD13, V.LD_R13_DISP,
         V.disp10_addr(V.R14_AT_LD13, V.LD_R13_DISP), V.SRC_A,
         V.disp10_addr(V.R14_AT_LD13, V.LD_R13_DISP) == V.SRC_A))

# ---------------------------------------------------------------- B
print("")
print("[B] 5 台独立 600 步等形机（boot=%d，HOOK2=0x%06X 之后起算）"
      % (V.BOOT_STEP, V.HOOK2))
GROUPS = (
    ("NO_PATCH", ()),
    ("SAME_AB", ((V.SRC_A, V.A_ORIG), (V.SRC_B, V.B_SET))),
    ("A0_BSET", ((V.SRC_A, V.A_ZERO), (V.SRC_B, V.B_SET))),
    ("AOR_BCLR", ((V.SRC_A, V.A_ORIG), (V.SRC_B, V.B_CLR))),
    ("A0_BCLR", ((V.SRC_A, V.A_ZERO), (V.SRC_B, V.B_CLR))),
)
by = {}
for tag, patch in GROUPS:
    r = run(tag, patch, V.AFTER_LONG)
    by[tag] = r
    h = r["hit"]
    e85 = all(h[k] for k in EXIT_KEYS)
    chain = all(h[k] for k in GATE_KEYS)
    bg = ("not_reached" if not h["LD_B_GATE"]
          else "jump" if h["TAKE_JUMP"] else "fall")
    print("")
    print("  %-9s hook_steps=%d A=0x%08X B=0x%08X 返回点r12=%s"
          % (tag, r["hs"], r["read_a"], r["read_b"],
             "0x%08X" % r["ret12"] if r["ret12"] is not None else "None"))
    print("    −85出口=%s 命令链=%s B门=%s | 调度reti=%s TASK9step=%s"
          % (e85, chain, bg, r["retis"][0] if r["retis"] else "—",
             r["steps"].get("TASK9", "—")))
    print("    末PC=0x%06X 末r4=0x%08X 末CUR=0x%08X READY=0x%08X 去重PC=%d int=%d"
          % (r["end_pc"], r["end_r4"], r["end_cur"], r["end_ready"],
             r["npc"], r["ints"]))
    print("    TCB7 +0x14=0x%08X +0x20=0x%08X（等待服务号=%d）"
          % (r["p14"], r["p20"], V.reason_code(r["p20"])))

print("")
print("B1 等形三项（boot/hook/post）全同 = %s"
      % V.equal_shape([by[t] for t in by]))
k_same = ("npc", "end_pc", "end_r4", "end_cur", "retis", "ints")
print("B2 NO_PATCH ≡ SAME_AB 六项逐项相同 = %s（写原值无效，差异来自值）"
      % V.same_value_verdict(by["NO_PATCH"], by["SAME_AB"], k_same))

# ---------------------------------------------------------------- C
print("")
print("[C] 1 台只观测轨迹机：A 是调用者 r13 的栈帧保存槽")
mc = blive.boot(0)
mc.step(V.BOOT_STEP)
rc = mc.cpu.r
pc_trap = mc.cpu.pc
r13e, r14e, r15e = (rc[13] & 0xFFFFFFFF, rc[14] & 0xFFFFFFFF,
                    rc[15] & 0xFFFFFFFF)
a0 = rd32(mc, V.SRC_A)
stm_rows = []
for i in range(44):
    p = mc.cpu.pc
    mc.step(1)
    if p == V.A_WRITER:
        stm_rows.append((i, p, r13e, rd32(mc, V.SRC_A)))
a1 = rd32(mc, V.SRC_A)
print("C1 boot=%d 停在 0x%06X（设施 0x%06X → %s）"
      % (V.BOOT_STEP, pc_trap, V.TRAP, pc_trap == V.TRAP))
print("C2 陷阱口 r13=0x%08X r14=0x%08X r15=0x%08X（设施 0x%08X/0x%08X/0x%08X）"
      % (r13e, r14e, r15e, V.R13_ENTRY, V.R14_ENTRY, V.R15_ENTRY))
print("C3 A 锚点前 = 0x%08X；单步 44 步后 = 0x%08X" % (a0, a1))
print("C4 0x%06X %s 执行后实测：%s"
      % (V.A_WRITER, V.A_WRITER_MN,
         " ".join("step%d:pc=0x%06X:陷阱口r13=0x%08X:A=0x%08X" % x
                  for x in stm_rows)))
print("C5 stm1 写入值 == 陷阱口 r13 = %s；A 语义判据 = %s（不是器件状态字）"
      % (a1 == r13e == V.R13_ENTRY,
         V.a_semantic_verdict(a1, r13e, V.A_WRITER_MN)))

# ---------------------------------------------------------------- D
print("")
print("[D] 2 台独立 260 步短窗口机：复现 V84 的截断读法")
short = {}
for tag, patch in (("SAME_AB", ((V.SRC_A, V.A_ORIG), (V.SRC_B, V.B_SET))),
                   ("A0_BCLR", ((V.SRC_A, V.A_ZERO), (V.SRC_B, V.B_CLR)))):
    r = run(tag + "_260", patch, V.AFTER_SHORT)
    short[tag] = r
    chain = all(r["hit"][k] for k in GATE_KEYS)
    print("  %-11s 去重PC=%3d 末PC=0x%06X 末r4=0x%08X 调度reti=%s 返回码=%s 链=%s TASK9=%s"
          % (tag + "_260", r["npc"], r["end_pc"], r["end_r4"],
             r["retis"][:1] or "无",
             "0x%08X" % r["ret12"] if r["ret12"] is not None else "None",
             chain, r["steps"].get("TASK9", "—")))
print("D1 260 步两组都无调度 reti = %s（设施 %s）"
      % (not short["SAME_AB"]["retis"] and not short["A0_BCLR"]["retis"],
         V.SHORT_WINDOW_NO_RETI))
print("D2 600 步两组都有调度 reti = %s（设施 %s）"
      % (bool(by["SAME_AB"]["retis"]) and bool(by["A0_BCLR"]["retis"]),
         V.LONG_WINDOW_BOTH_RETI))
print("D3 短窗口判据 = %s（规矩 82 第二次实例）"
      % V.short_window_verdict(not short["SAME_AB"]["retis"]
                               and not short["A0_BCLR"]["retis"],
                               bool(by["SAME_AB"]["retis"])
                               and bool(by["A0_BCLR"]["retis"])))
print("D4 窗口起算点：V84 从 %s 起算，V86 从 %s 起算 → 可比 = %s"
      % (V.window_start_kind("trap"), V.window_start_kind("hook2"),
         V.comparable_windows(V.window_start_kind("trap"),
                              V.window_start_kind("hook2"))))
print("D5 不许把两种起算点的去重 PC 数 / 末 PC 直接对比（规矩 84）")

# ---------------------------------------------------------------- E
print("")
print("[E] 1 台标准收敛态机：coverage 口径分层")
me = blive.boot(0)
me.step(V.CONVERGE_BOOT)
me.raise_irq(V.CONVERGE_IRQ)
me.run(V.CONVERGE_RUN, spin_guard=False)
print("E1 末 PC=0x%06X（设施 0x%06X → %s）"
      % (me.cpu.pc, V.CONVERGE_TAIL, me.cpu.pc == V.CONVERGE_TAIL))
hit_count = sum(1 for p in V.COV_HIT if me.cov and me.cov.visited(p))
miss_count = sum(1 for p in V.COV_MISS if not (me.cov and me.cov.visited(p)))
for p in V.COV_HIT:
    print("E2 coverage 0x%06X 命中=%s" % (p, bool(me.cov and me.cov.visited(p))))
for p in V.COV_MISS:
    print("E3 coverage 0x%06X 命中=%s" % (p, bool(me.cov and me.cov.visited(p))))
print("E4 命中 %d/%d、未命中 %d/%d，判据 = %s"
      % (hit_count, V.COV_HIT_COUNT, miss_count, V.COV_MISS_COUNT,
         V.coverage_scope_verdict(hit_count, miss_count)))
print("E5 与 V81–V83「单次 −170 调用短窗口里 −85 出口覆盖全 0」是不同窗口口径，不合并")

# ---------------------------------------------------------------- F
print("")
print("[F] 2x2 真值表与两层事件判据")
rows = []
for tag in V.COMBO_TAGS:
    r = by[tag]
    e85 = all(r["hit"][k] for k in EXIT_KEYS)
    chain = all(r["hit"][k] for k in GATE_KEYS)
    rows.append({"tag": tag, "e85": e85,
                 "switched": bool(r["retis"]) and r["hit"]["TASK9"]})
    print("  %-9s A=%08X B=%08X → −85=%-5s 命令链=%-5s 换手=%-5s reti=%3d 返回码=0x%08X"
          % (tag, r["read_a"], r["read_b"], e85, chain, rows[-1]["switched"],
             r["retis"][0] if r["retis"] else -1, r["ret12"]))
print("F1 合取判据：A=0∧B=0 → %s；其余三组 → %s"
      % (V.combo_truth(0, 0), V.combo_truth(V.A_ORIG, 0)))
print("F2 优先级判据 = %s（四组都换手、只有 A0_BCLR 返 −85）"
      % V.priority_verdict(rows))
print("F3 设施 COMBO 对账：")
for tag in V.COMBO_TAGS:
    c, r = V.COMBO[tag], by[tag]
    ok = (c["ret"] == r["ret12"] and c["reti"] == (r["retis"][0] if r["retis"] else None)
          and c["npc"] == r["npc"] and c["p20"] == r["p20"]
          and c["p14"] == r["p14"] and c["ints"] == r["ints"]
          and c["end_pc"] == r["end_pc"] and c["end_r4"] == r["end_r4"])
    print("    %-9s 设施/实测 8 项一致 = %s" % (tag, ok))
steps = by["A0_BCLR"]["steps"]
print("F4 A0_BCLR 关键事件 step：%s"
      % " ".join("%s=%s" % (k, steps.get(k, "—")) for k in V.ORDER_KEYS))
print("F5 严格单调 = %s（22 项 ORDER 全在窗口内 = %s）"
      % (V.order_monotonic(steps),
         all(k in steps for k, _p, _s in V.ORDER)))
sa, ab = by["SAME_AB"], by["A0_BCLR"]
s1 = sa["pcs"][sa["retis"][0]:sa["retis"][0] + V.ALIGN]
s2 = ab["pcs"][ab["retis"][0]:ab["retis"][0] + V.ALIGN]
print("F6 两次 reti 后 %d 步 PC 逐项相同 = %s（差异 %s）"
      % (V.ALIGN, s1 == s2,
         [j for j in range(min(len(s1), len(s2))) if s1[j] != s2[j]][:5] or "无"))
print("F7 四组末 CUR/READY 同一值 = %s / %s"
      % (len(set(by[t]["end_cur"] for t in V.COMBO_TAGS)) == 1,
         len(set(by[t]["end_ready"] for t in V.COMBO_TAGS)) == 1))

# ---------------------------------------------------------------- G
print("")
print("[G] 归因分离、结果层/原因层与自我证伪")
print("G1 写原值 ≡ 不写 = %s ⇒ 差异来自写进去的值，不是写动作"
      % V.same_value_verdict(by["NO_PATCH"], by["SAME_AB"], k_same))
print("G2 只清 B、不动 A（AOR_BCLR）：−85=%s 换手=%s reti=%d（延续 V85 结论）"
      % (all(by["AOR_BCLR"]["hit"][k] for k in EXIT_KEYS),
         rows[2]["switched"], by["AOR_BCLR"]["retis"][0]))
print("G3 只清 A、B 保持置位（A0_BSET）：−85=%s 换手=%s reti=%d"
      % (all(by["A0_BSET"]["hit"][k] for k in EXIT_KEYS),
         rows[1]["switched"], by["A0_BSET"]["retis"][0]))
print("G4 原因层四组等待号：%s（结果层换手同为任务 9）"
      % " / ".join("%s=%d" % (t, V.reason_code(by[t]["p20"]))
                   for t in V.COMBO_TAGS))
print("G5 600 步 int 次数：%s"
      % " / ".join("%s=%d" % (t, by[t]["ints"]) for t in V.COMBO_TAGS))
print("G6 自我证伪：草稿曾把 V84 的「LATE_AB 返 −85 未换手」当成互斥分支；"
      "600 步长窗口证四组全换手，是窗口截断（规矩 82）")
print("G7 自我证伪：草稿曾把 0x041C26 beq rel9=199 当成 +199 正跳；"
      "sx8 是 −57，落点 0x041BB6（规矩 85）")
print("G8 自我证伪：草稿 C 段曾用 mc.coverage 取覆盖率报错；"
      "正确口径是 mc.cov.visited(p)（判据口径错，不是证据错）")
print("G9 规矩 84：返回码层 ≠ 调度层；窗口起算点必须声明")
print("G10 规矩 85：rel9 用 sx8；跨段跳转不许按地址就近归属；A 是调用者 r13 栈帧槽")

# ---------------------------------------------------------------- H
print("")
print("[H] 综合判据与边界")
no_priority = V.priority_verdict(rows) == "no_priority_two_layers"
mono = V.order_monotonic(steps)
cross = V.cross_segment_verdict(V.BEQ_A_ZERO, V.BEQ_REL9, V.SEG106_ENTRY,
                                0x041BEA, 0x041E80) == "cross_segment_backward"
shape = V.equal_shape([by[t] for t in by])
value_ok = V.same_value_verdict(by["NO_PATCH"], by["SAME_AB"], k_same)
short_ok = V.short_window_verdict(not short["SAME_AB"]["retis"]
                                  and not short["A0_BCLR"]["retis"],
                                  bool(by["SAME_AB"]["retis"])
                                  and bool(by["A0_BCLR"]["retis"])) == "window_artifact_timing_shift"
cov_ok = V.coverage_scope_verdict(hit_count, miss_count) == "kernel_code_ran_command_chain_not_reached"
print("H1 无优先级=%s 顺序单调=%s 跨段=%s 等形=%s 值归因=%s 短窗口=%s 覆盖分层=%s"
      % (no_priority, mono, cross, shape, value_ok, short_ok, cov_ok))
print("H2 综合判据 = %s"
      % V.verdict(no_priority, mono, cross, shape, value_ok, short_ok, cov_ok, False))
print("H3 所有写 A/B 单元都是反事实补全；任务换手是既有代码自然执行，不等于真机发生过")
print("H4 不能报：A=0 与换手是两条互斥分支 / 补一次 −85 就等于真机事件 / "
      "A 是器件状态字 / 清 B 位唤醒了任务 9 / 显示链通了 / 出画面")
print("H5 链未闭合；%s 开放项见 KNOWN_ISSUES" % V.KI_RANGE)
print("")
print("DONE V86 A–H 八段，%d 台独立机器（5 长窗口组合 + 1 只观测 + 2 短窗口 + 1 标准收敛态），"
      "反事实全部标注" % V.MACHINE_COUNT)
