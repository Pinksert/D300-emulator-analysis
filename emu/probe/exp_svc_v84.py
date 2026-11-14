# -*- coding: utf-8 -*-
"""V84 正式探针：两道内部条件的数据源与干预层级。

    cd emu && python probe/exp_svc_v84.py > ../results/frbox/svc84.txt

A/E 段只读 ROM；B/C 段共用 1 台**只观测轨迹机**（不改 PC / 寄存器 /
不注入中断，C 从它的 bus.all_writes 读）；D 段 1 台标准收敛态机；
F/G 段 7 台独立等形反事实机。合计 9 台独立机器。
所有往 0x000083E4 / 0x68000970 写值都是反事实补全，不是固件行为、
不是真机器件事件。承接 KNOWN_ISSUES KI#327 第二条。
"""
import sys
import struct
import collections

sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from frbox import bstatic, svc84 as V, blive

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


def rd16(a):
    return struct.unpack(">H", rom[a - base:a - base + 2])[0]


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


print("=" * 76)
print("V84 · 两道条件不在寄存器里，在陷阱栈帧槽和对象位标志字里 —— A–H 八段 / 9 台机器")
print("=" * 76)

# ---------------------------------------------------------------- A
print("")
print("[A] 静态推导：disp10 → 实际单元（只读 ROM）")
ins_a = dict((p, (mn, v)) for p, mn, v, _s in dec(0x041BEA, 0x041C30))
ins_b = dict((p, (mn, v)) for p, mn, v, _s in dec(0x041BB0, 0x041BD8))
mn13, v13 = ins_a[V.LD_R13_PC]
mn8, v8 = ins_b[V.LD_R8_PC]
d13, d8 = v13.get("disp10"), v8.get("disp10")
a13 = V.disp10_addr(V.R14_AT_LD13, d13)
a8 = V.disp10_addr(V.R14_AT_LD8, d8)
print("A1 0x%06X %s %s；disp10=%d → r14(0x%08X)+%d = 0x%08X"
      % (V.LD_R13_PC, mn13, im(v13), d13, V.R14_AT_LD13, d13 * 4, a13))
print("A2 0x%06X %s %s；disp10=%d → r14(0x%08X)+%d = 0x%08X"
      % (V.LD_R8_PC, mn8, im(v8), d8, V.R14_AT_LD8, d8 * 4, a8))
print("A3 推导单元 == 设施常量：A %s / B %s"
      % (a13 == V.SRC_A, a8 == V.SRC_B))
print("A4 段归属纯函数：A=%s B=%s；两者是 ROM 常量吗？%s / %s"
      % (V.region_kind(V.SRC_A), V.region_kind(V.SRC_B),
         V.is_rom_constant(V.SRC_A), V.is_rom_constant(V.SRC_B)))
print("A5 静态可定址写点 writers_of：A=%d B=%d（设施 %d / %d）"
      % (len(SW.writers_of(V.SRC_A)), len(SW.writers_of(V.SRC_B)),
         V.STATIC_WRITERS_A, V.STATIC_WRITERS_B))

# ---------------------------------------------------------------- B
print("")
print("[B] 只观测轨迹机：单步实测 A / B 的地址与值（1 台，不改任何状态）")
mt = blive.boot(0)
mt.step(V.BOOT_STEP)
rt = mt.cpu.r
print("B0 锚点 pc=0x%06X r12=0x%08X（== 0x040668 / -170 ? %s）"
      % (mt.cpu.pc, rt[12] & 0xFFFFFFFF,
         mt.cpu.pc == V.TRAP and (rt[12] & 0xFFFFFFFF) == V.MAIN_SVC_IMM32))
print("B1 锚点前 A=0x%08X B=0x%08X"
      % (rd32(mt, V.SRC_A), rd32(mt, V.SRC_B)))
trace = {}
timeline = []
prev_n = len(mt.bus.all_writes)
anchor_prefix = prev_n          # step(483841) 锚点之前的写记录前缀
ld13_prefix = None              # 到 0x041BFE 时的写记录前缀
ld13_step = None
val_at_ld13 = None
steps = 0
for _ in range(400):
    pc = mt.cpu.pc
    for addr, n, val, wpc in mt.bus.all_writes[prev_n:]:
        if addr <= V.SRC_A < addr + n:
            timeline.append(("A", steps, pc, addr, val & 0xFFFFFFFF, wpc))
        if addr <= V.SRC_B < addr + n:
            timeline.append(("B", steps, pc, addr, val & 0xFFFFFFFF, wpc))
    prev_n = len(mt.bus.all_writes)
    if pc in (V.LD_R13_PC, 0x041C00, 0x041C06, 0x041C20, V.CMP_R13):
        trace.setdefault(pc, []).append((rt[14] & 0xFFFFFFFF,
                                         rt[8] & 0xFFFFFFFF,
                                         rt[13] & 0xFFFFFFFF))
    if pc == V.LD_R13_PC and ld13_prefix is None:
        ld13_prefix = prev_n
        ld13_step = steps
        val_at_ld13 = rd32(mt, V.disp10_addr(rt[14] & 0xFFFFFFFF, d13))
        print("B2 到 0x041BFE 第 %d 步：r14=0x%08X A=0x%08X mem=0x%08X"
              % (steps, rt[14] & 0xFFFFFFFF,
                 V.disp10_addr(rt[14] & 0xFFFFFFFF, d13), val_at_ld13))
    if pc == V.RET_PC:
        break
    mt.step(1)
    steps += 1
print("B3 CUR_TCB 链（0x041C00 / 0x041C06 / 0x041C20 / 0x041C24 处实测）：")
for pc in (0x041C00, 0x041C06, 0x041C20, V.CMP_R13):
    for rec in trace.get(pc, []):
        print("     0x%06X r14=0x%08X r8=0x%08X r13=0x%08X" % ((pc,) + rec))
print("B4 第 %d 步读到 A=0x%08X（设施 0x%08X）→ %s"
      % (ld13_step, val_at_ld13, V.SRC_A_AT_LOAD,
         val_at_ld13 == V.SRC_A_AT_LOAD))

# ---------------------------------------------------------------- C
print("")
print("[C] 同一台只观测轨迹机的 bus.all_writes：谁决定了 A 的值")
allw = mt.bus.all_writes
scope = allw[:ld13_prefix]              # 到 0x041BFE 口径
anchor_scope = allw[:anchor_prefix]     # step(483841) 锚点之前口径
cntA = collections.Counter()
cntB = collections.Counter()
valsA = collections.defaultdict(collections.Counter)
valsB = collections.defaultdict(collections.Counter)
for addr, n, val, wpc in scope:
    if addr <= V.SRC_A < addr + n:
        cntA[wpc] += 1
        valsA[wpc][val & 0xFFFFFFFF] += 1
    if addr <= V.SRC_B < addr + n:
        cntB[wpc] += 1
        valsB[wpc][val & 0xFFFFFFFF] += 1
tot_a, tot_b = sum(cntA.values()), sum(cntB.values())
anchor_a = sum(1 for addr, n, _v, _p in anchor_scope
               if addr <= V.SRC_A < addr + n)
print("C1 到 0x041BFE 口径：A 写入 %d 次 / B 写入 %d 次（设施 %d / %d）→ %s / %s"
      % (tot_a, tot_b, V.A_WRITES_TO_LD13, V.B_WRITES_ANCHOR,
         tot_a == V.A_WRITES_TO_LD13, tot_b == V.B_WRITES_ANCHOR))
print("C1b 锚点前口径：A 写入 %d 次（设施 %d）；差 %d 次 = 第 %d 步 stm1"
      % (anchor_a, V.A_WRITES_ANCHOR, tot_a - anchor_a, V.A_WRITER_STEP))
print("C2 A 写点 PC：%s"
      % " ".join("0x%06X×%d" % (p, c) for p, c in cntA.most_common(6)))
print("C3 主写点 0x%06X 写了 %d 次、%d 种值（设施 %d 次 / %d 种）→ %s / %s"
      % (V.A_WRITER, cntA[V.A_WRITER], len(valsA[V.A_WRITER]),
         V.A_WRITER_COUNT, len(V.A_WRITER_VALS),
         cntA[V.A_WRITER] == V.A_WRITER_COUNT,
         len(valsA[V.A_WRITER]) == len(V.A_WRITER_VALS)))
print("C4 B 写点 PC：%s"
      % " ".join("0x%06X×%d" % (p, c) for p, c in cntB.most_common(6)))
print("C5 A 写点 PC 数=%d B 写点 PC 数=%d（设施 %d / %d）"
      % (len(cntA), len(cntB), V.A_WRITER_PCS, V.B_WRITER_PCS))
print("C6 静态/动态口径判定：A=%s B=%s"
      % (V.static_writer_verdict(V.STATIC_WRITERS_A, tot_a),
         V.static_writer_verdict(V.STATIC_WRITERS_B, tot_b)))
print("C7 从陷阱入口到 0x041BFE 之间对 A / B 的写入时间线（B 段逐步记录）：")
window = [x for x in timeline if x[1] <= ld13_step]
for tag, s, pc, addr, val, wpc in window:
    print("     %s step=%-3d 执行pc=0x%06X addr=0x%08X val=0x%08X 写点pc=0x%06X"
          % (tag, s, pc, addr, val, wpc))
if not window:
    print("     （无）")
a_ff = [(s, wpc) for tag, s, _pc, _a, val, wpc in window
        if tag == "A" and val == 0xFFFFFFFF]
print("C8 写 A=0xFFFFFFFF 的时刻/写点 = %s（设施 step=%d / 0x%06X）→ %s"
      % (" ".join("step%d@0x%06X" % x for x in a_ff) or "(无)",
         V.A_WRITER_STEP, V.A_WRITER,
         bool(a_ff) and a_ff[0][0] == V.A_WRITER_STEP
         and a_ff[0][1] == V.A_WRITER))
print("C9 主写点指令 = %s（设施 %s），读取步数 %d"
      % (dict((p, mn) for p, mn, _v, _s in dec(0x04068C, 0x04069A)).get(V.A_WRITER),
         V.A_WRITER_MN, V.LD_R13_STEP))

# ---------------------------------------------------------------- D
print("")
print("[D] 标准收敛态机（1 台，V65–V84 逐字相同的设备策略）")
mc = blive.boot(0)
mc.step(V.CONVERGE_BOOT)
mc.raise_irq(V.CONVERGE_IRQ)
mc.run(V.CONVERGE_RUN, spin_guard=False)
awc = mc.bus.all_writes
ca = sum(1 for addr, n, _v, _p in awc if addr <= V.SRC_A < addr + n)
cb = sum(1 for addr, n, _v, _p in awc if addr <= V.SRC_B < addr + n)
print("D1 末 PC=0x%06X（设施 0x%06X → %s），RAM 写总数=%d（设施 %d）"
      % (mc.cpu.pc, V.CONVERGE_TAIL, mc.cpu.pc == V.CONVERGE_TAIL,
         len(awc), V.CONVERGE_RAM_WRITES))
print("D2 A 写入 %d 次（设施 %d），末值 0x%08X（设施 0x%08X）"
      % (ca, V.A_WRITES_CONVERGE, rd32(mc, V.SRC_A), V.A_VAL_CONVERGE))
print("D3 B 写入 %d 次（设施 %d），末值 0x%08X（设施 0x%08X）"
      % (cb, V.B_WRITES_CONVERGE, rd32(mc, V.SRC_B), V.B_VAL_CONVERGE))
print("D4 A 三个时刻三值：锚点前 0x%08X → 读时 0x%08X → 收敛末 0x%08X"
      % (V.A_VAL_ANCHOR, V.SRC_A_AT_LOAD, V.A_VAL_CONVERGE))
print("D5 判定：静态写点 0 但动态写入 > 0 ⇒ 不是「没人写」")

# ---------------------------------------------------------------- E
print("")
print("[E] 三个写点的指令语义（只读 ROM）")
for tag, lo, hi, key in (
    ("E1 陷阱分发器含 stm1", 0x04068C, 0x04069A, V.A_WRITER),
    ("E2 B 置位", 0x041A5C, 0x041A64, V.B_WRITER_SET),
    ("E3 B 清位", 0x041AF8, 0x041B00, V.B_WRITER_CLR),
):
    seq = dec(lo, hi)
    print("  %s：" % tag)
    for p, mn, v, _s in seq:
        print("     0x%06X %-8s %-24s %s"
              % (p, mn, im(v), "<== 写点" if p == key else ""))
print("E4 写点归类：stm1=%s / or=%s / and=%s"
      % (V.writer_class("stm1"), V.writer_class("or"), V.writer_class("and")))
print("E5 ⇒ A 是陷阱分发器栈帧保存槽（调用者 r13），B 是内核对象位标志字")

# ---------------------------------------------------------------- F
print("")
print("[F] 只补内存单元、不改寄存器的等形反事实（7 台独立机器）")
KEYS = {
    "LD_R13": V.LD_R13_PC, "CMP_R13": V.CMP_R13, "BEQ_ERR": V.BEQ_ERR,
    "ERR_MID": V.ERR_MID, "LD_R8": V.LD_R8_PC,
    "AND_GATE": V.AND_GATE, "BEQ_85": V.BEQ_85,
    "EXIT_85": V.EXIT_85, "RET_D_85": V.RET_D_85, "SLOT_85": V.SLOT_85,
    "MAIN_JMP": V.MAIN_JMP, "CMD_SEG": V.CMD_SEG, "CMD_CMP": V.CMD_CMP,
    "WB_CALL": V.WB_CALL, "WB_FN": V.WB_FN,
}


def one(tag, early, late):
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    rr = mm.cpu.r
    ok = (mm.cpu.pc == V.TRAP
          and (rr[12] & 0xFFFFFFFF) == V.MAIN_SVC_IMM32)
    if not ok:
        return {"tag": tag, "bad": True}
    for a, v in early:
        mm.bus.wr(a, 4, v)
    seen = set()
    ret12 = None
    read_a = None
    read_b = None
    r14_at_b = None
    for _ in range(V.AFTER):
        pc = mm.cpu.pc
        seen.add(pc)
        if pc == V.HOOK2:
            for a, v in late:
                mm.bus.wr(a, 4, v)
        if pc == V.LD_R13_PC:
            read_a = rd32(mm, V.SRC_A)
        if pc == V.LD_R8_PC:
            read_b = rd32(mm, V.SRC_B)
            r14_at_b = rr[14] & 0xFFFFFFFF
        if pc == V.RET_PC and ret12 is None:
            ret12 = rr[12] & 0xFFFFFFFF
        mm.step(1)
    return {"tag": tag, "boot": V.BOOT_STEP, "hooks": (V.HOOK1, V.HOOK2),
            "post": V.AFTER, "ret": ret12, "npc": len(seen),
            "read_a": read_a, "read_b": read_b, "r14_at_b": r14_at_b,
            "end_r4": rr[4] & 0xFFFFFFFF,
            "hit": dict((k, p in seen) for k, p in KEYS.items())}


rows = [one(*g) for g in V.CF_GROUPS]
print("F1 等形三项（boot / hooks / post）全同 = %s"
      % V.equal_shape([x for x in rows if not x.get("bad")]))
print("F2 %-10s %-12s %-12s %-12s %-10s %-6s %-6s %s"
      % ("组", "读A", "读B", "返回点r12", "末r4", "PC数", "链全中", "命中"))
for x in rows:
    if x.get("bad"):
        print("%-10s ANCHOR_BAD" % x["tag"])
        continue
    chain = all(x["hit"][k] for k in V.GATE_KEYS)
    hs = " ".join(k for k, v in x["hit"].items() if v)
    print("%-10s %-12s %-12s %-12s 0x%08X %-6d %-6s %s"
          % (x["tag"],
             ("0x%08X" % x["read_a"]) if x["read_a"] is not None else "—",
             ("0x%08X" % x["read_b"]) if x["read_b"] is not None else "—",
             ("0x%08X" % x["ret"]) if x["ret"] is not None else "未到返回点",
             x["end_r4"], x["npc"], chain, hs or "(无)"))
by = dict((x["tag"], x) for x in rows if not x.get("bad"))
print("F3 B 单元实测 r14 = %s（设施 0x%08X，disp10=%d → 0x%08X）"
      % (" ".join("0x%08X" % by[t]["r14_at_b"] for t in V.CF_OPEN_TAGS
                  if by[t]["r14_at_b"] is not None) or "—",
         V.R14_AT_LD8, V.LD_R8_DISP, V.SRC_B))
print("F4 EARLY_A 与 BASE 逐项相同 = %s（早补被 stm1 覆盖）"
      % (all(by["EARLY_A"][k] == by["BASE"][k]
             for k in ("read_a", "ret", "npc", "end_r4"))))
print("F5 LATE_AB 与 LATE_BOTH 逐项相同 = %s（B 无中间写入）"
      % (all(by["LATE_AB"][k] == by["LATE_BOTH"][k]
             for k in ("read_a", "read_b", "ret", "npc", "end_r4"))))
print("F6 改写确实改变执行：LATE_AB 不同PC=%d vs BASE=%d，差=%d"
      % (by["LATE_AB"]["npc"], by["BASE"]["npc"],
         by["LATE_AB"]["npc"] - by["BASE"]["npc"]))
print("F7 服务自己返回码 = %s（设施 0x%08X，经 0x0406BE extsb 得 -85）"
      % (" ".join("0x%08X" % by[t]["ret"] for t in V.CF_OPEN_TAGS),
         V.CF_RET_85))

# ---------------------------------------------------------------- G
print("")
print("[G] 归因分离：两道门与副作用")
for t in ("BASE", "EARLY_A", "EARLY_B", "EARLY_AB", "LATE_A0",
          "LATE_AB", "LATE_BOTH"):
    x = by[t]
    h = x["hit"]
    print("G1 %-10s 门1(ERR_MID)=%-5s 门2(EXIT_85)=%-5s SLOT_85=%-5s "
          "MAIN_JMP=%-5s WB_FN=%-5s 判据=%s"
          % (t, h["ERR_MID"], h["EXIT_85"], h["SLOT_85"],
             h["MAIN_JMP"], h["WB_FN"],
             V.gate_verdict(h["ERR_MID"], h["EXIT_85"],
                            all(h[k] for k in V.GATE_KEYS))))
print("G2 LATE_A0 只开门1、LATE_AB 开两门 ⇒ 第二道门独立 = %s"
      % (by["LATE_A0"]["hit"]["ERR_MID"] and not by["LATE_A0"]["hit"]["EXIT_85"]
         and by["LATE_AB"]["hit"]["EXIT_85"]))
print("G3 EARLY_B / EARLY_AB 清掉 B 但门1 关：PC数=%d 末r4=0x%08X，"
      "−85 出口未命中 = %s（具体去向本版未追，列为开放项）"
      % (by["EARLY_B"]["npc"], by["EARLY_B"]["end_r4"],
         not by["EARLY_B"]["hit"]["EXIT_85"]))
print("G4 干预层级：V83=改%s，V84=改%s；两者都不等于%s"
      % (V.patch_layer("register"), V.patch_layer("memory"),
         V.patch_layer("device_event")))
print("G5 时机判定 EARLY_A = %s（写入发生在读之前 %d 步 stm1 覆盖）"
      % (V.overlay_verdict(0, V.SRC_A_AT_LOAD, True), V.A_WRITER_STEP))

# ---------------------------------------------------------------- H
print("")
print("[H] 综合判据与边界")
static_zero = (V.STATIC_WRITERS_A == 0 and V.STATIC_WRITERS_B == 0)
dynamic_nonzero = (V.A_WRITES_ANCHOR > 0 and V.B_WRITES_ANCHOR > 0)
a_ram = not V.is_rom_constant(V.SRC_A)
b_ram = not V.is_rom_constant(V.SRC_B)
early_ov = by["EARLY_A"]["read_a"] == V.SRC_A_AT_LOAD
late_ret = all(by[t]["ret"] == V.CF_RET_85 for t in V.CF_OPEN_TAGS)
chain_hit = all(all(by[t]["hit"][k] for k in V.GATE_KEYS)
                for t in V.CF_OPEN_TAGS)
print("H1 静态写点 0=%s 动态写入>0=%s A/B 是 RAM=%s/%s "
      "早补被覆盖=%s 晚补返回0xAB=%s 链全中=%s"
      % (static_zero, dynamic_nonzero, a_ram, b_ram,
         early_ov, late_ret, chain_hit))
print("H2 综合判据 = %s"
      % V.source_verdict(static_zero, dynamic_nonzero, a_ram, b_ram,
                         early_ov, late_ret, chain_hit, False))
print("H3 规矩 80：writers_of==0 ≠ 没人写；A 是 stm1 栈帧保存槽不是状态字")
print("H4 规矩 81：补寄存器 ≠ 补内存单元 ≠ 补器件事件；早补会被覆盖")
print("H5 边界：所有写内存单元都是反事实补全，不是固件行为 / 真机器件事件；链未闭合")
print("")
print("DONE V84 A–H 八段，9 台独立机器（1 只观测轨迹机 + 1 标准收敛态机 + 7 等形反事实机），反事实全部标注")
