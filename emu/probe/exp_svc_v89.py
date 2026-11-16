# -*- coding: utf-8 -*-
"""V89 正式探针：TCB+0x28 的 −1 哨兵门 / 返回码图谱 / 服务号槽不分流（KI#347）。

    cd emu && python probe/exp_svc_v89.py > ../results/frbox/svc89.txt

A–C 段只读 ROM（含全 ROM 流式普查）；D 段 5 台 TCB+0x28 反事实机；
E 段 6 组别名 × 2 台等形机；F 段 1 台标准收敛态机；合计 **18 台独立机器**，
每台只跑一遍。写 ``0x6800026C``、改 TRAP 处 r12 都是**反事实补全**
（只写内存单元 / 寄存器），不是固件行为、不是真机器件事件。链未闭合。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from collections import Counter
from frbox import bstatic, svc89 as V, blive

M = V.M32
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
T = fr_emu.TABLE


def rd16(a):
    o = a - base
    return (rom[o] << 8) | rom[o + 1]


def rd32(m, a):
    return m.bus.rd(a, 4) & M


def ins_at(pc):
    ins = T.decode(rom, pc - base)
    return ("??", {}, 2) if ins is None else (ins[0], dict(ins[2]), ins[3])


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
              "disp8", "rel9", "rel12", "udisp6", "rs2",
              "reglist-low-st", "reglist-hi-st"):
        if k in v:
            g.append("%s=%s" % (k, v[k]))
    for k in ("ri", "rj"):
        if k in v:
            g.append("%s=r%d" % (k, v[k]))
    return " ".join(g)


# ---------------------------------------------------------------- 全 ROM 流式普查（只做一次）
imm_hits, ld12, st12 = [], [], []
off, n, decoded = 0, len(rom), 0
while off < n - 1:
    pc = base + off
    ins = T.decode(rom, off)
    if ins is None:
        off += 2
        continue
    mn, _b, v, sz, _w = ins
    decoded += 1
    if v.get("i32") == V.SLOT_SVC or v.get("i20") == V.I20_83E0:
        imm_hits.append((pc, mn, im(v)))
    if v.get("disp10") == 12 and mn in ("ld", "lduh", "ldub"):
        ld12.append(pc)
    if v.get("disp10") == 12 and mn in ("st", "sth", "stb"):
        st12.append(pc)
    off += sz

# 分发表全表（V88 口径）
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


def boot_to_hook():
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    assert mm.cpu.pc == V.TRAP
    rr = mm.cpu.r
    hs = 0
    while mm.cpu.pc != V.HOOK2 and hs < 300:
        mm.step(1)
        hs += 1
    assert mm.cpu.pc == V.HOOK2
    return mm, rr, hs


def run_tcb(tag, val, after=V.AFTER_LONG):
    """1 台独立机器；val=None 只观测，否则只写 TCB+0x28（反事实补全）。"""
    mm, rr, hs = boot_to_hook()
    before = rd32(mm, V.TCB_R10)
    stack = tuple(rd32(mm, a) for a in range(0x000083D0, 0x000083F0, 4))
    if val is not None:
        mm.bus.wr(V.TCB_R10, 4, val & M)     # 反事实补全
    first_next, r10_at, ld_addr, ret_seq = None, None, None, []
    seen, n_cmp2_r10, n_cmp2_r13 = set(), 0, 0
    for i in range(after):
        pc = mm.cpu.pc
        seen.add(pc)
        if pc == V.LD_R10:
            ld_addr = V.disp10_addr(rr[14] & M, V.LD_R10_DISP10)
        if pc == V.CMP2_R10:
            r10_at = rr[10] & M
            n_cmp2_r10 += 1
        if pc == V.CMP2_R13:
            n_cmp2_r13 += 1
        if pc == V.SVC_RET:
            ret_seq.append(rr[12] & M)
        mm.step(1)
        if pc == V.BEQ_R10 and first_next is None:
            first_next = (i, mm.cpu.pc)
    return {
        "tag": tag, "boot": V.BOOT_STEP, "trap": V.TRAP, "post": after,
        "hs": hs, "before": before, "val": val, "stack": stack,
        "r10_at": r10_at, "ld_addr": ld_addr,
        "first_next": first_next, "ret_seq": ret_seq,
        "n_cmp2_r10": n_cmp2_r10, "n_cmp2_r13": n_cmp2_r13,
        "hit_a2": V.TAIL_A2 in seen, "hit_88": V.TAIL_88 in seen,
        "end_pc": mm.cpu.pc, "end_cur": rd32(mm, 0x68000000),
        "end_ready": rd32(mm, 0x68000004),
        "svc_slot": rd32(mm, V.SLOT_SVC), "r13_slot": rd32(mm, V.SLOT_R13),
        "tcb_after": rd32(mm, V.TCB_R10),
    }


def run_pair(tag, svc, after=V.PAIR_WINDOW):
    """1 台独立机器；改 TRAP 处 r12 = svc（反事实补全，只改寄存器）。"""
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    rr = mm.cpu.r
    assert mm.cpu.pc == V.TRAP
    rr[12] = svc & M                              # 反事实补全
    pcs, call_target, d12 = [], None, []
    for i in range(after):
        pc = mm.cpu.pc
        pcs.append(pc & M)
        if pc == V.CALL_R12:
            call_target = rr[12] & M
        if pc == V.ALIAS_D12_PC:
            ea = V.disp10_addr(rr[14] & M, 12)
            d12.append((i, rr[14] & M, ea, rd32(mm, ea)))
        mm.step(1)
    return {
        "tag": tag, "svc": svc, "boot": V.BOOT_STEP, "trap": V.TRAP,
        "post": after, "pcs": pcs, "call_target": call_target,
        "d12": d12, "svc_slot": rd32(mm, V.SLOT_SVC),
        "end_pc": mm.cpu.pc, "end_cur": rd32(mm, 0x68000000),
        "end_ready": rd32(mm, 0x68000004), "npc": len(set(pcs)),
    }


def run_converge():
    """1 台标准收敛态机（V65–V88 逐字相同的策略）。"""
    mm = blive.boot(0)
    mm.step(V.CONVERGE_BOOT)
    mm.raise_irq(V.CONVERGE_IRQ)
    mm.run(V.CONVERGE_RUN, spin_guard=False)
    return {"end_pc": mm.cpu.pc, "cur": rd32(mm, 0x68000000),
            "ready": rd32(mm, 0x68000004)}


print("=" * 76)
print("V89 · TCB+0x28 的 −1 门 / 四条负返回码 / 服务号槽保存但不分流 —— A–H / 18 台机器")
print("=" * 76)

# ---------------------------------------------------------------- A
print("")
print("[A] 两道 cmp2 比较的是两个不同来源的字（只读 ROM + 纯函数）")
mn, v, _s = ins_at(V.LD_CUR)
print("A1 0x%06X %-8s %s → r14 = [0x%08X] = CUR_TCB"
      % (V.LD_CUR, mn, im(v), V.LD_CUR_I32))
for pc, disp10, base_r14, tag in (
        (V.LD_R13, V.LD_R13_DISP10, V.LD_R13_BASE, "r13"),
        (V.LD_R10, V.LD_R10_DISP10, V.CUR_TCB, "r10")):
    mn, v, _s = ins_at(pc)
    ea = V.disp10_addr(base_r14, disp10)
    print("A2 0x%06X %-8s disp10=%-3d → r14=0x%08X + %d*4 = 0x%08X（%s 来源）"
          % (pc, mn, disp10, base_r14, V.sx8(disp10), ea, tag))
print("A3 两道 cmp2 字源判据 = %s（0x%08X 栈帧槽 vs 0x%08X TCB+0x%02X）"
      % (V.word_source_verdict(V.LD_R13_ADDR, V.TCB_R10),
         V.SLOT_R13, V.TCB_R10, V.TCB_R10_OFF))
print("A4 0x%06X cmp2 r10 / 0x%06X beq rel9=%d → 0x%06X；"
      "0x%06X cmp2 r13 / 0x%06X blt 判 r13<−1（V88）"
      % (V.CMP2_R10, V.BEQ_R10, V.BEQ_R10_REL9,
         V.rel9_target(V.BEQ_R10, V.BEQ_R10_REL9),
         V.CMP2_R13, V.BLT_EARLY))
print("A5 beq 判 Z：逐个取值实测（只有 −1 使 cmp2 结果为 0）")
for a in (0xFFFFFFFF, 0xFFFFFFFE, 0x68000A28, 0x00000000):
    print("    a=0x%08X → Z=%d beq=%s"
          % (a, V.cmp2_flags(a)[4], V.beq_after_cmp2(a)))

# ---------------------------------------------------------------- B
print("")
print("[B] −170 段返回码图谱与静态入边普查（只读 ROM）")
for epc, rpd, spc, i8, rc, name, has_slot in V.EXIT_ROWS:
    mn1, v1, _s = ins_at(epc)
    mn2, v2, _s = ins_at(rpd)
    if has_slot:
        mn3, v3, _s = ins_at(spc)
        got = V.ret_code(v3.get("i8"))
        print("B1 0x%06X %-6s / 0x%06X %-6s / 槽 0x%06X %-7s i8=%-3s → %+d（%s，对=%s）"
              % (epc, mn1, rpd, mn2, spc, mn3, v3.get("i8"), rc, name,
                 got == rc and mn3 == "ldi:8"))
    else:
        print("B1 0x%06X %-6s / 0x%06X %-6s / 无延迟槽 → 沿用 r12（%s）"
              % (epc, mn1, rpd, mn2, name))
print("B2 出口 %d 个 / 负返回码 %d 个 %s / 返回 0 的出口 %d 个 / 无延迟槽 %d 个"
      % (V.EXIT_COUNT, V.NEG_CODE_COUNT, V.NEG_CODES, V.RET0_COUNT,
         sum(1 for r in V.EXIT_ROWS if not r[6])))
print("B3 静态入边普查（−170 段全部 rel9 分支落点）：")
for pc, mn, r9, sx8, tgt, name in V.INEDGE_ROWS:
    mn2, v2, _s = ins_at(pc)
    print("    0x%06X %-4s rel9=%3d sx8=%+4d → 0x%06X %-10s（解码=%s 落点=%s）"
          % (pc, mn, r9, sx8, tgt, name,
             mn2 == mn and v2.get("rel9") == r9,
             V.rel9_target(pc, r9) == tgt))
print("B4 0x%06X（−33 出口）静态入边 %d 条 → 判据=%s；"
      "其中 0x%06X / 0x%06X 是与 r13 无关的前置校验"
      % (V.TAIL_88, V.in_edge_count(V.TAIL_88),
         V.in_edge_verdict(V.TAIL_88), 0x041BF6, 0x041BFC))
print("B5 不能把 −33 等同于「A≤−2」：V88 只证了 r13≤−2 这条入边，"
      "本版补齐另外两条（边界修正，不是推翻）")

# ---------------------------------------------------------------- C
print("")
print("[C] 服务号被 stm1 保存进 0x000083E0，但入口段没有读回（只读 ROM）")
mn, v, _s = ins_at(V.STM1)
print("C1 0x%06X %-6s %s → 保存 r14..r8 共 %d 槽"
      % (V.STM1, mn, im(v), len(V.STM1_SLOTS)))
mn, v, _s = ins_at(V.STM0)
print("C2 0x%06X %-6s %s（低组寄存器）" % (V.STM0, mn, im(v)))
print("C3 槽位映射（动态栈区 dump 实测反推）：")
for reg, addr in V.STM1_SLOTS:
    extra = ""
    if addr == V.SLOT_SVC:
        extra = "  <<< 服务号（规整后 r12）"
    if addr == V.SLOT_R13:
        extra = "  <<< 调用者 r13（V88 的 A 单元）"
    print("    r%-2d → 0x%08X%s" % (reg, addr, extra))
print("C4 全 ROM 解码 %d 条；立即数 = 0x%08X 的指令 %d 条；"
      "ld disp10=12 共 %d 条 / st disp10=12 共 %d 条 → %s / %s / %s"
      % (decoded, V.I20_83E0, len(imm_hits), len(ld12), len(st12),
         decoded == V.ROM_DECODED and len(imm_hits) == V.IMM_83E0_COUNT,
         len(ld12) == V.LD_DISP10_12, len(st12) == V.ST_DISP10_12))
for pc, mn, s in imm_hits[:6]:
    print("    0x%06X %-8s %s（把 0x83E0 当参数传给 0x%06X，归属未在窗口内取证）"
          % (pc, mn, s, V.SUB_ADDR))
print("C5 陷阱口 0x040668..0x0406B6 内 disp10=12 读写 = %d 条"
      % sum(1 for pc, mn, v, _s in dec(0x040668, 0x0406B6)
            if v.get("disp10") == 12))
d12_entries = []
for entry, svcs, nref, _refs in V.ALIAS_ROWS:
    hits = [(pc, mn, im(v)) for pc, mn, v, _s in dec(entry, entry + 0x80)
            if v.get("disp10") == 12 or v.get("i32") == V.SLOT_SVC
            or v.get("i20") == V.I20_83E0]
    if hits:
        d12_entries.append((entry, hits))
    print("C6 入口 0x%06X <- %-24s disp10=12/槽立即数引用 %d 条"
          % (entry, str(svcs), len(hits)))
print("C7 8 组里唯一命中 0x%06X：%s；若基址是陷阱栈帧 r14=0x%08X，"
      "disp10=12 会指向服务号槽（判据=%s），但该指令属 0x041D6E 入口段、"
      "其 r14 不是陷阱栈帧，且长窗口内 0 命中 ⇒ 按 disp10 相同认定"
      "「读服务号槽」是假阳性（延续 85 / 88）"
      % (V.ALIAS_D12_PC, V.ALIAS_D12_PC in
         [h[0] for _e, hs in d12_entries for h in hs],
         V.HOOK_R14, V.disp10_alias_verdict(12, V.HOOK_R14)))
print("C8 全表 %d 号 / %d 入口 / %d 组别名 / %d 个服务号 → %s"
      % (len(SVC), len(set(SVC.values())), len(AL),
         sum(len(s) for _e, s in AL),
         (len(SVC), len(set(SVC.values())), len(AL)) ==
         (V.TABLE_SVC_COUNT, V.TABLE_ENTRY_COUNT, V.TABLE_ALIAS_PAIRS)))

# ---------------------------------------------------------------- D
print("")
print("[D] TCB+0x28 反事实：5 台独立 600 步等形机（只写内存单元）")
TCB = {}
for tag, val, exp_taken, exp_next, exp_ret in V.TCB_VALS:
    r = run_tcb(tag, val)
    TCB[tag] = r
    fn = r["first_next"]
    got_next = fn[1] if fn else 0
    got_ret = r["ret_seq"][0] if r["ret_seq"] else None
    print("D1 %-5s 写入=%-12s r10@cmp2=0x%08X 首次下一条=0x%06X 返回码=%-6s "
          "命中0x041CA2=%-5s cmp2次数=%d/%d"
          % (tag, ("0x%08X" % val) if val is not None else "无（只观测）",
             r["r10_at"] or 0, got_next,
             ("0x%02X" % got_ret) if got_ret is not None else "?",
             r["hit_a2"], r["n_cmp2_r10"], r["n_cmp2_r13"]))
    print("     对账：跳=%s（期望 %s）/ 落点=%s / 返回码=%s / ld有效地址=%s / hs=%d"
          % (r["hit_a2"], exp_taken, got_next == exp_next,
             got_ret == exp_ret,
             r["ld_addr"] == V.TCB_R10, r["hs"] == V.HOOK_STEPS))
print("D2 等形三项（boot / trap / post）全同 = %s；hs 全同 = %s"
      % (V.equal_shape(list(TCB.values())),
         len(set(r["hs"] for r in TCB.values())) == 1))
print("D3 写回原值 ORIG 与只观测 OBS 逐项相同 = %s；M1 不同 = %s ⇒ 判据=%s"
      % ((TCB["ORIG"]["first_next"], TCB["ORIG"]["ret_seq"],
          TCB["ORIG"]["end_pc"], TCB["ORIG"]["end_cur"]) ==
         (TCB["OBS"]["first_next"], TCB["OBS"]["ret_seq"],
          TCB["OBS"]["end_pc"], TCB["OBS"]["end_cur"]),
         TCB["M1"]["first_next"] != TCB["OBS"]["first_next"],
         V.patch_causality(
             (TCB["OBS"]["first_next"], TCB["OBS"]["ret_seq"]),
             (TCB["ORIG"]["first_next"], TCB["ORIG"]["ret_seq"]),
             (TCB["M1"]["first_next"], TCB["M1"]["ret_seq"]))))
print("D4 OBS 栈区 0x000083D0..0x000083EC = %s"
      % " ".join("0x%08X" % x for x in TCB["OBS"]["stack"]))
print("D5 首次触发 vs 累计（规矩 89）：M1 返回码序列前 6 项=%s "
      "（同一次提前返回被再次进入重复出现，不是两条路径）"
      % [("0x%02X" % x) for x in TCB["M1"]["ret_seq"][:6]])
print("D6 结果层：OBS/ORIG 末PC=0x%06X CUR=0x%08X READY=0x%08X；"
      "M1 末PC=0x%06X CUR=0x%08X READY=0x%08X"
      % (TCB["OBS"]["end_pc"], TCB["OBS"]["end_cur"],
         TCB["OBS"]["end_ready"], TCB["M1"]["end_pc"],
         TCB["M1"]["end_cur"], TCB["M1"]["end_ready"]))
print("D7 服务号槽 / r13 槽：OBS=0x%08X / 0x%08X（后续 stm1 覆盖）；"
      "M1=0x%08X / 0x%08X（提前返回截断了后续覆盖）"
      % (TCB["OBS"]["svc_slot"], TCB["OBS"]["r13_slot"],
         TCB["M1"]["svc_slot"], TCB["M1"]["r13_slot"]))

# ---------------------------------------------------------------- E
print("")
print("[E] 6 组别名 × 2 台等形机：改 TRAP 处 r12（反事实，只改寄存器）")
PAIRS = {}
for tag, entry, svcs in V.PAIR_ROWS:
    a = run_pair(tag + "a", svcs[0])
    b = run_pair(tag + "b", svcs[1])
    same = a["pcs"] == b["pcs"]
    PAIRS[tag] = (a, b, same)
    print("E1 %s 入口=0x%06X 号=%-14s call@r12=0x%06X/0x%06X "
          "服务号槽=0x%08X/0x%08X 逐条全序列相同=%s"
          % (tag, entry, str(svcs), a["call_target"] or 0,
             b["call_target"] or 0, a["svc_slot"], b["svc_slot"], same))
    print("     末PC=0x%06X/0x%06X CUR=0x%08X/0x%08X READY=0x%08X/0x%08X "
          "不同PC=%d/%d 0x041DE0命中=%d/%d 判据=%s"
          % (a["end_pc"], b["end_pc"], a["end_cur"], b["end_cur"],
             a["end_ready"], b["end_ready"], a["npc"], b["npc"],
             len(a["d12"]), len(b["d12"]),
             V.svc_saved_vs_dispatch(a["svc_slot"], b["svc_slot"],
                                     same, V.PAIR_WINDOW)))
print("E2 6 组全部逐条全序列相同 = %s；窗口长度统一 %d 步（规矩 82）"
      % (all(x[2] for x in PAIRS.values()), V.PAIR_WINDOW))
print("E3 另 2 组 0x%06X / 0x%06X 只做静态普查，未做动态等形 ⇒ "
      "不据此宣布「8 组全程不分流」" % V.PAIR_STATIC_ONLY)
print("E4 反事实边界：部分组末 PC 落在非代码区（0xFFFFFF9E / 0x000018 / "
      "0x0083B0 等），这些轨迹只用于同组对照，不得解释真机服务语义")

# ---------------------------------------------------------------- F
print("")
print("[F] 1 台标准收敛态机（V65–V88 逐字相同策略：boot→step(191520)→irq 0x2B→run(500000)）")
cv = run_converge()
print("F1 末 PC=0x%06X（设施 0x%06X，对=%s）CUR=0x%08X READY=0x%08X"
      % (cv["end_pc"], V.CONVERGE_TAIL, cv["end_pc"] == V.CONVERGE_TAIL,
         cv["cur"], cv["ready"]))
print("F2 本版不改收敛策略；分派区 0x%06X call @r12；r10 门与别名等形机"
      "均从 boot→step(%d) 锚点起算" % (V.CALL_R12, V.BOOT_STEP))

# ---------------------------------------------------------------- G
print("")
print("[G] 归因分离与四处自我证伪")
print("G1 −33 出口静态入边 %d 条（0x041BF6 / 0x041BFC / 0x041C10），"
      "只有最后一条与 r13≤−2 有关 ⇒ 不能报「−33 只由 A≤−2 决定」"
      % V.INEDGE_M33_COUNT)
print("G2 0x%06X ld disp10=12 在 %d 步长窗口内命中 %d 次 ⇒ "
      "按 disp10 相同认定「读服务号槽」是假阳性；同一 disp10=12 在陷阱栈帧"
      " r14 下判据=%s、换成 CUR_TCB 基址判据=%s（延续 85 / 88）"
      % (V.ALIAS_D12_PC, V.PAIR_WINDOW,
         sum(len(a["d12"]) + len(b["d12"]) for a, b, _s in PAIRS.values()),
         V.disp10_alias_verdict(12, V.HOOK_R14),
         V.disp10_alias_verdict(12, V.CUR_TCB)))
print("G3 服务号确实被保存（E 段槽值两两不同），但同组轨迹逐条相同 ⇒ "
      "「被保存」≠「被读取并分流」（规矩 92）")
print("G4 r10 与 r13 不同源：r10 = [CUR_TCB+0x28] 内核对象指针，"
      "r13 = 栈帧 0x%08X 调用者参数 ⇒ 两道 cmp2 不可并为同一状态字（规矩 83）"
      % V.SLOT_R13)
print("G5 写回原值 ORIG 与只观测 OBS 全同 ⇒ 差异来自值不是写动作；"
      "M1 提前返回截断后续 stm1 覆盖 ⇒ 结果层差异不等于「新路径」（规矩 89）")
print("G6 三处 ldi:20 #0x83E0 把常量当参数传给 0x%06X，"
      "该子例程归属未在窗口内取证 ⇒ 不解释、不据此宣布「服务号槽没人读」"
      % V.SUB_ADDR)

# ---------------------------------------------------------------- H
print("")
print("[H] 综合判据、规矩与边界")
pre = (decoded == V.ROM_DECODED and len(imm_hits) == V.IMM_83E0_COUNT,
       TCB["M1"]["first_next"][1] == V.TAIL_A2 and TCB["M1"]["hit_a2"],
       V.NEG_CODE_COUNT == 4 and V.in_edge_count(V.TAIL_88) == 3,
       all(x[2] for x in PAIRS.values()),
       cv["end_pc"] == V.CONVERGE_TAIL)
print("H1 前置五项（普查 / r10 门 / 返回码图谱 / 6 组等形 / 收敛态）= %s" % (pre,))
print("H2 综合判据 = %s" % V.source_verdict(*pre[:4]))
print("H3 规矩 91（返回码 ≠ 单一条件，先做静态入边普查） / "
      "规矩 92（服务号被保存 ≠ 被读取并分流；disp10 相同 ≠ 同一地址）")
print("H4 全部写 TCB+0x28 / 改 TRAP 处 r12 是反事实补全（只写内存单元或寄存器）；"
      "服务返回与任务换手是既有代码自然执行；链未闭合")
print("H5 仍开放：0x%06X 子例程是否使用 0x83E0 / r10=−1 在真机的触发条件 / "
      "8 对别名各自语义分工 / 真机 vec 0x18 器件事件 / −85 器件事件语义 / "
      "bit3 置位源 / vec 0x2D / 317 提交调用点 / 任务 9 下载后是否回任务 7 / "
      "0xFFFFFFFF r13 哨兵真机设置者" % V.SUB_ADDR)
print("H6 不能报（禁语）：−33 只由 A≤−2 决定 / 服务号槽没人读 / "
      "8 组别名全程不分流 / 所有服务语义已解出 / 真机已复现 −85 / "
      "补一次值就等于真机事件 / 显示链通了 / 出画面 / 系统跑起来了 / 真机如此")
print("DONE V89 A–H 八段，18 台独立机器（5 台 r10 门 + 12 台别名等形 + "
      "1 台标准收敛态），反事实全部标注")
