# -*- coding: utf-8 -*-
"""V85 正式探针：短窗口把时机差读成路径差（承接 KI#331 最后一条）。

    cd emu && python probe/exp_svc_v85.py > ../results/frbox/svc85.txt

A/D 段只读 ROM；B/C 段 4 台独立短窗口机等形反事实；E 段 2 台独立长窗口机；
F 段 1 台标准收敛态机；G/H 段归约。合计 **7 台独立机器**。
所有往 B 单元 0x68000970 写值都是**反事实补全**，不是固件行为、
不是真机器件事件。链未闭合。
"""
import sys
import struct
import collections

sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from frbox import bstatic, svc85 as V, blive, brealos as R

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


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


def ins_at(pc):
    ins = fr_emu.TABLE.decode(rom, pc - base)
    return ("??", {}) if ins is None else (ins[0], dict(ins[2]))


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


def tcb_label(m, tcb):
    if not (R.TCB_BASE <= tcb < R.TCB_BASE + 40 * R.TCB_STRIDE):
        return "不在TCB数组"
    k = (tcb - R.TCB_BASE) // R.TCB_STRIDE
    if (tcb - R.TCB_BASE) % R.TCB_STRIDE:
        return "非步长整数倍"
    recs = R.records(m, include_idle=True)
    if k < len(recs):
        r = recs[k]
        return "idx=%d 任务号=%d 入口=0x%06X" % (k + 1, r["id"], r["entry"])
    return "idx=%d（表外）" % (k + 1)


print("=" * 76)
print("V85 · 短窗口把时机差读成路径差 —— A–H 八段 / 7 台机器")
print("=" * 76)

# ---------------------------------------------------------------- A
print("")
print("[A] 静态推导：分叉门 / 两条落点 / 末 r4 的真身（只读 ROM）")
ins_gate = dict((p, (mn, v)) for p, mn, v, _s in dec(0x041B28, 0x041B50))
mn_g, v_g = ins_gate[V.LD_B_GATE]
addr_g = V.disp10_addr(V.R14_AT_GATE, v_g.get("disp10", V.LD_B_DISP))
print("A1 0x%06X %s %s；disp10=%d → r14(0x%08X)+%d = 0x%08X（== B ? %s）"
      % (V.LD_B_GATE, mn_g, im(v_g), v_g.get("disp10"),
         V.R14_AT_GATE, v_g.get("disp10", 2) * 4, addr_g,
         addr_g == V.SRC_B))
print("A2 分叉门链：0x041B44 mov r9,r8 → 0x%06X %s → 0x%06X %s %s"
      % (V.AND_R9_R6, ins_at(V.AND_R9_R6)[0], V.BNE_GATE,
         ins_at(V.BNE_GATE)[0], im(ins_at(V.BNE_GATE)[1])))
print("A3 B=0x%08X 判据=%s → 落点 0x%06X；B=0 判据=%s → 落点 0x%06X"
      % (V.SRC_B_SET, V.b_gate(V.SRC_B_SET), V.gate_target("jump"),
         V.b_gate(V.SRC_B_CLR), V.gate_target("fall")))
print("A4 正常返回尾：0x%06X %s（延迟槽 r12=0）；换手尾起于 0x%06X"
      % (V.NORMAL_RET_SLOT, ins_at(V.NORMAL_RET_SLOT)[0], V.TAKE_FALL))
mn_c, v_c = ins_at(V.CONST_LDI)
print("A5 末 r4 来源：0x%06X %s %s ⇒ 是 ROM 常量立即数，不是状态字"
      % (V.CONST_LDI, mn_c, im(v_c)))
print("A6 立即数 == 设施 0x%08X：%s（V46 dev_ctrl40 定性：0x40 器件表 0 源地址）"
      % (V.CONST_IMM, v_c.get("i32") == V.CONST_IMM))
off = V.TABLE0_SRC - base
words = struct.unpack(">256H", rom[off:off + 512])
diffs = set(words[i + 1] - words[i] for i in range(255))
print("A7 表 0 ROM 实测：%d 项，首=0x%04X 末=0x%04X 相邻差集合=%s 线性判据=%s"
      % (len(words), words[0], words[-1],
         "{" + ",".join("0x%X" % d for d in sorted(diffs)) + "}",
         V.table0_shape(words[0], words[-1], len(words), V.TABLE0_DELTA)
         and words[0] == V.TABLE0_FIRST and words[-1] == V.TABLE0_LAST
         and diffs == {V.TABLE0_DELTA}))

# ---------------------------------------------------------------- B
print("")
print("[B] 4 台独立短窗口机等形反事实（boot=%d，挂钩 0x%06X/0x%06X，后续 %d 步）"
      % (V.BOOT_STEP, V.HOOK1, V.HOOK2, V.AFTER))
KEYS = {
    "LD_B_GATE": V.LD_B_GATE, "BNE_GATE": V.BNE_GATE,
    "JUMP": V.TAKE_JUMP, "FALL": V.TAKE_FALL,
    "NORMAL_SLOT": V.NORMAL_RET_SLOT, "DISPATCH": V.DISPATCH,
    "RETI": V.RETI, "TASK9": V.TASK9_ENTRY, "CONST_LDI": V.CONST_LDI,
    "EXIT_85": 0x041C9A,
}


def short(tag, early, late):
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    rr = mm.cpu.r
    ok = (mm.cpu.pc == V.TRAP
          and (rr[12] & 0xFFFFFFFF) == V.MAIN_SVC_IMM32)
    if not ok:
        return {"tag": tag, "bad": True}
    for a, v in early:
        mm.bus.wr(a, 4, v)              # 反事实补全
    pcs = []
    gate = {}
    for i in range(V.AFTER):
        pc = mm.cpu.pc
        pcs.append(pc)
        if pc == V.HOOK2:
            for a, v in late:
                mm.bus.wr(a, 4, v)      # 反事实补全
        if pc == V.LD_B_GATE:
            a14 = rr[14] & 0xFFFFFFFF
            gate = {"r14": a14,
                    "addr": V.disp10_addr(a14, V.LD_B_DISP),
                    "val": rd32(mm, V.SRC_B)}
        if pc == V.BNE_GATE:
            gate["and"] = (rr[9] & rr[6]) & 0xFFFFFFFF
            gate["b"] = rd32(mm, V.SRC_B)
        mm.step(1)
    return {"tag": tag, "boot": V.BOOT_STEP,
            "hooks": (V.HOOK1, V.HOOK2), "post": V.AFTER, "window": V.AFTER,
            "gate": gate, "pcs": pcs, "npc": len(set(pcs)),
            "end_in": pcs[-1], "end_pc": mm.cpu.pc,
            "end_r4": rr[4] & 0xFFFFFFFF,
            "hit": dict((k, p in pcs) for k, p in KEYS.items()),
            "hit_switch": V.TASK9_ENTRY in pcs}


rows = [short(*g) for g in V.CF_GROUPS]
by = dict((x["tag"], x) for x in rows if not x.get("bad"))
print("B1 等形四项（boot / hooks / post / window）全同 = %s"
      % V.equal_shape([x for x in rows if not x.get("bad")]))
print("B2 组        读地址      B值        B&want    跳/落   PC数  末PC    末r4      换手函数")
for x in rows:
    if x.get("bad"):
        print("%-10s ANCHOR_BAD" % x["tag"])
        continue
    g = x["gate"]
    print("  %-9s 0x%08X 0x%08X 0x%08X %-5s %-6d 0x%06X 0x%08X %s"
          % (x["tag"], g.get("addr", 0), g.get("val", 0), g.get("and", 0),
             "JUMP" if x["hit"]["JUMP"] else "FALL",
             x["npc"], x["end_pc"], x["end_r4"], x["hit_switch"]))
print("B3 门实测 r14=0x%08X 读地址=0x%08X（设施 0x%08X / 0x%08X）→ %s"
      % (by["BASE"]["gate"].get("r14", 0), by["BASE"]["gate"].get("addr", 0),
         V.R14_AT_GATE, V.SRC_B,
         by["BASE"]["gate"].get("r14") == V.R14_AT_GATE
         and by["BASE"]["gate"].get("addr") == V.SRC_B))
print("B4 BASE 的 B=0x%08X 与 and=0x%08X（设施 0x%08X / 0x%08X）→ %s；"
      "EARLY_B 两者皆 0 = %s"
      % (by["BASE"]["gate"].get("val", 0), by["BASE"]["gate"].get("and", 0),
         V.SRC_B_SET, V.BASE_AND,
         by["BASE"]["gate"].get("val") == V.SRC_B_SET
         and by["BASE"]["gate"].get("and") == V.BASE_AND,
         by["EARLY_B"]["gate"].get("val") == 0
         and by["EARLY_B"]["gate"].get("and") == 0))

# ---------------------------------------------------------------- C
print("")
print("[C] 归因分离：是「写动作」还是「写进去的值」")
keys4 = ("npc", "end_pc", "end_r4", "hit_switch")
print("C1 SAME_B 与 BASE 四项逐项相同 = %s"
      % all(by["SAME_B"][k] == by["BASE"][k] for k in keys4))
print("C2 EARLY_B 与 SAME_B 换手标志不同 = %s"
      % (by["EARLY_B"]["hit_switch"] != by["SAME_B"]["hit_switch"]))
print("C3 LATE_B 与 EARLY_B 四项逐项相同 = %s（写入时机无关）"
      % all(by["LATE_B"][k] == by["EARLY_B"][k] for k in keys4))
print("C4 只清 B、不动 A 就足以换手：EARLY_B/LATE_B 命中 TASK9=%s/%s，"
      "−85 出口=%s/%s"
      % (by["EARLY_B"]["hit_switch"], by["LATE_B"]["hit_switch"],
         by["EARLY_B"]["hit"]["EXIT_85"], by["LATE_B"]["hit"]["EXIT_85"]))
pvv = V.patch_value_verdict(
    dict((k, by["BASE"][k]) for k in keys4),
    dict((k, by["SAME_B"][k]) for k in keys4),
    dict((k, by["EARLY_B"][k]) for k in keys4))
print("C5 纯函数判据 = %s" % pvv)
print("C6 窗口内末 PC 与跑满后 cpu.pc 口径：BASE 0x%06X/0x%06X = %s；"
      "EARLY_B 0x%06X/0x%06X = %s"
      % (by["BASE"]["end_in"], by["BASE"]["end_pc"],
         V.window_end_kind(by["BASE"]["end_in"], by["BASE"]["end_pc"]),
         by["EARLY_B"]["end_in"], by["EARLY_B"]["end_pc"],
         V.window_end_kind(by["EARLY_B"]["end_in"], by["EARLY_B"]["end_pc"])))

# ---------------------------------------------------------------- D
print("")
print("[D] 换手链静态反汇编（只读 ROM，不按地址就近归服务）")
for tag, lo, hi in (("D1 另一条尾部", 0x041B4A, 0x041B72),
                    ("D2 派发器入口", 0x040D32, 0x040D40),
                    ("D3 调度器", 0x040E18, 0x040E28)):
    print("  %s：" % tag)
    for p, mn, v, _s in dec(lo, hi):
        print("    0x%06X %-8s %s" % (p, mn, im(v)))
print("  D4 换手收尾：0x%06X %s（reti）→ 任务 9 入口 0x%06X"
      % (V.RETI, ins_at(V.RETI)[0], V.TASK9_ENTRY))
print("  D5 表下载函数 0x%06X / 拷贝环 0x%06X（V46 已建模的 0x40 器件窗口）"
      % (V.TABLE_FN, V.COPY_LOOP))

# ---------------------------------------------------------------- E
print("")
print("[E] 2 台独立长窗口机（%d 步）：换手时机与换手后轨迹" % V.LONG_AFTER)


def long(tag, early):
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    rr = mm.cpu.r
    assert mm.cpu.pc == V.TRAP
    for a, v in early:
        mm.bus.wr(a, 4, v)              # 反事实补全
    w0 = len(mm.bus.all_writes)
    pcs, ints, bwrites, retis = [], [], [], []
    for i in range(V.LONG_AFTER):
        pc = mm.cpu.pc
        pcs.append(pc)
        if ins_at(pc)[0] == "int":
            ints.append((i, pc, rr[12] & 0xFFFFFFFF))
        if pc == V.RETI:
            retis.append(i)
        n = len(mm.bus.all_writes)
        for addr, sz, val, wpc in mm.bus.all_writes[w0:n]:
            if addr <= V.SRC_B < addr + sz:
                bwrites.append((i, wpc, val & 0xFFFFFFFF))
        w0 = n
        mm.step(1)
    return {"mm": mm, "tag": tag, "pcs": pcs, "ints": ints,
            "bwrites": bwrites, "reti": retis,
            "end_pc": mm.cpu.pc, "end_r4": rr[4] & 0xFFFFFFFF,
            "end_cur": rd32(mm, V.CUR), "end_ready": rd32(mm, V.READY)}


lb = long("BASE", ())
le = long("EARLY_B", ((V.SRC_B, V.SRC_B_CLR),))
print("E1 第一次 reti step：BASE=%s EARLY_B=%s，提前=%d 步（设施 %d）→ %s"
      % (lb["reti"][0] if lb["reti"] else None,
         le["reti"][0] if le["reti"] else None,
         V.advance_steps(lb["reti"][0], le["reti"][0]),
         V.ADVANCE,
         V.advance_steps(lb["reti"][0], le["reti"][0]) == V.ADVANCE))
sb = lb["pcs"][lb["reti"][0]:lb["reti"][0] + V.ALIGN]
se = le["pcs"][le["reti"][0]:le["reti"][0] + V.ALIGN]
diffs = [j for j in range(min(len(sb), len(se))) if sb[j] != se[j]]
align_ok = (not diffs and len(sb) == V.ALIGN and len(se) == V.ALIGN)
print("E2 两次 reti 之后 %d 步 PC 序列逐项相同 = %s（差异 %s）"
      % (V.ALIGN, align_ok, diffs[:5] or "无"))
print("E3 前 6 项 BASE：%s" % " ".join("0x%06X" % p for p in sb[:6]))
print("E3 前 6 项 EARLY_B：%s" % " ".join("0x%06X" % p for p in se[:6]))
print("E4 长窗口末：BASE 0x%06X / r4=0x%08X / PC数=%d；"
      "EARLY_B 0x%06X / r4=0x%08X / PC数=%d"
      % (lb["end_pc"], lb["end_r4"], len(set(lb["pcs"])),
         le["end_pc"], le["end_r4"], len(set(le["pcs"]))))
print("E5 短窗口 260 步末：BASE 0x%06X / PC数=%d；EARLY_B 0x%06X / PC数=%d"
      % (by["BASE"]["end_pc"], by["BASE"]["npc"],
         by["EARLY_B"]["end_pc"], by["EARLY_B"]["npc"]))
print("E6 末 CUR / READY：BASE 0x%08X / 0x%08X；EARLY_B 0x%08X / 0x%08X"
      % (lb["end_cur"], lb["end_ready"], le["end_cur"], le["end_ready"]))
for x in (lb, le):
    m = x["mm"]
    print("E7 %-8s 末 CUR=0x%08X ⇒ %s" % (x["tag"], x["end_cur"],
                                          tcb_label(m, x["end_cur"])))
    for name, tcb in (("任务7", V.TCB7), ("任务9", V.TCB9)):
        p14, p20 = rd32(m, tcb + 0x14), rd32(m, tcb + 0x20)
        print("E8 %-8s %s state=0x%02X +0x14=0x%08X +0x20=0x%08X 等待服务号=%d"
              % (x["tag"], name, m.bus.rd(tcb + 0x0B, 1), p14, p20,
                 V.reason_code(p20)))
    print("E9 %-8s int #0x40 = %d 次 %s"
          % (x["tag"], len(x["ints"]),
             " ".join("step%d@0x%06X" % (s[0], s[1]) for s in x["ints"][:5]) or "无"))
    print("E10 %-8s B 单元写入：%s"
          % (x["tag"],
             " ".join("step%d@0x%06X=0x%08X" % w for w in x["bwrites"]) or "窗口内无写"))

# ---------------------------------------------------------------- F
print("")
print("[F] 1 台标准收敛态机：这条路径本来就跑过")
mc = blive.boot(0)
mc.step(V.CONVERGE_BOOT)
mc.raise_irq(V.CONVERGE_IRQ)
mc.run(V.CONVERGE_RUN, spin_guard=False)
print("F1 末 PC=0x%06X（设施 0x%06X → %s）"
      % (mc.cpu.pc, V.CONVERGE_TAIL, mc.cpu.pc == V.CONVERGE_TAIL))
cov = [bool(mc.cov and mc.cov.visited(p)) for p in V.CONVERGE_COV_PCS]
for p, c in zip(V.CONVERGE_COV_PCS, cov):
    print("F2 coverage 0x%06X 命中=%s" % (p, c))
print("F3 四 PC 全命中 = %s（不是新功能、不是唤醒）" % all(cov))
print("F4 工具口径：bus.all_writes 只记 RAM ⇒ 判器件窗口用 %s，"
      "用 all_writes 的判定 = %s"
      % (V.tool_scope_verdict("coverage", "device"),
         V.tool_scope_verdict("all_writes", "device")))

# ---------------------------------------------------------------- G
print("")
print("[G] 归因分离与自我证伪")
path_ok = V.path_identity_verdict(
    align_ok, lb["end_pc"] == le["end_pc"],
    lb["end_ready"] == le["end_ready"], lb["end_cur"] == le["end_cur"])
print("G1 结果层判据 = %s" % path_ok)
wrv = V.wait_reason_verdict(V.BASE_TCB7_P20, V.EARLY_TCB7_P20,
                            V.BASE_TCB7_P14, V.EARLY_TCB7_P14)
print("G2 原因层判据 = %s（BASE 等待号=%d / EARLY_B=%d；"
      "BASE int %d 次 / EARLY_B %d 次）"
      % (wrv, V.reason_code(V.BASE_TCB7_P20),
         V.reason_code(V.EARLY_TCB7_P20),
         len(lb["ints"]), len(le["ints"])))
swv = V.short_window_verdict(
    (by["BASE"]["end_pc"], by["EARLY_B"]["end_pc"]),
    (lb["end_pc"], le["end_pc"]), align_ok,
    V.advance_steps(lb["reti"][0], le["reti"][0]))
print("G3 窗口截断判据 = %s（短时末 PC 0x%06X/0x%06X，长时同为 0x%06X）"
      % (swv, by["BASE"]["end_pc"], by["EARLY_B"]["end_pc"], lb["end_pc"]))
print("G4 规矩 82：短窗口把时机差读成路径差；pcs[N-1] 与 cpu.pc 差一条指令")
print("G5 规矩 83：结果层同 ≠ 原因层同；all_writes 只记 RAM")
print("G6 V85C 草稿 C4 曾因混入 all_writes 计数得 False，是判据口径错，不是证据错")

# ---------------------------------------------------------------- H
print("")
print("[H] 综合判据与边界")
window_ok = (swv == "window_artifact_timing_shift")
value_ok = (pvv == "value_not_write_action")
reason_diff = (wrv == "different_reason")
cov_ok = all(cov)
print("H1 窗口判据=%s 值归因=%s 结果层=%s 原因层=%s 收敛态=%s"
      % (window_ok, value_ok, path_ok == "same_path_shifted",
         reason_diff, cov_ok))
print("H2 综合判据 = %s"
      % V.verdict(window_ok, value_ok, path_ok == "same_path_shifted",
                  reason_diff, cov_ok, False))
print("H3 所有写 B 单元都是反事实补全；任务换手是既有代码的自然执行，不等于真机发生过")
print("H4 不能报：清 B 位唤醒了任务 9 / EARLY_B 走了一条新路径 / 显示链通了 / 出画面")
print("H5 链未闭合；开放项见 KNOWN_ISSUES KI#335")
print("")
print("DONE V85 A–H 八段，7 台独立机器（4 短窗口反事实 + 2 长窗口 + 1 标准收敛态），反事实全部标注")
