# -*- coding: utf-8 -*-
"""V83 正式探针：返回码写在 ret:d 延迟槽里，−85 出口的归属与触发条件。

    cd emu && python probe/exp_svc_v83.py > ../results/frbox/svc83.txt

A/B/C/E 段只读 ROM；D 段用 1 台**只观测轨迹机**（不改 PC / 寄存器 /
不注入中断）；F/G 段用 4 台独立等形反事实机。合计 5 台独立机器。
所有改 r13 / r8 都是反事实补全，不是固件行为、不是真机器件事件。
承接 KNOWN_ISSUES KI#323 第二条；设备策略与 V65–V82 逐字相同。
"""
import sys
import struct
import collections

sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from frbox import bstatic, tick79 as T, svc82 as S, svc83 as V, blive

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


def btarget(p, mn, v):
    if mn.startswith("b") and "rel9" in v:
        return ("rel9", T.rel9_target(p, v["rel9"]))
    if mn.startswith("call") and "rel12" in v:
        return ("rel12", T.rel12_target(p, v["rel12"]))
    if mn in ("jmp", "jmp:d", "call:d", "call") or mn.startswith("b"):
        return ("ind", None)
    return (None, None)


print("=" * 74)
print("V83 · 返回码写在 ret:d 延迟槽里 —— A–H 八段，5 台独立机器")
print("=" * 74)

# ---------------------------------------------------------------- A
print("")
print("[A] 内核区 ret:d 延迟槽模板族（只读 ROM，0x040000..0x080000）")
KINS = dec(V.KERNEL_LO, V.KERNEL_HI)
KPCS = [p for p, _m, _v, _s in KINS]
KNEXT = dict((KPCS[i], KPCS[i + 1]) for i in range(len(KPCS) - 1))
KBY = dict((p, (mn, v)) for p, mn, v, _s in KINS)
retd = [p for p, mn, _v, _s in KINS if mn == "ret:d"]
slots = []
for p in retd:
    q = KNEXT.get(p)
    if q is None or q not in KBY:
        continue
    mn, v = KBY[q]
    if mn.startswith("ldi") and v.get("ri") == 12:
        raw = v.get("i8", v.get("i20", v.get("i32")))
        if raw is not None:
            slots.append((p, q, raw & 0xFF, V.slot_code(raw)))
dist = collections.Counter(x[3] for x in slots)
s85 = [(p, q) for p, q, raw, code in slots if code == -85]
print("A1 ret:d 总数 = %d（设施常量 %d）→ %s"
      % (len(retd), V.RET_D_TOTAL, len(retd) == V.RET_D_TOTAL))
print("A2 延迟槽 ldi→r12 = %d（设施常量 %d）→ %s"
      % (len(slots), V.SLOT_WRITES, len(slots) == V.SLOT_WRITES))
print("A3 返回码分布 top10 = %s" % (dist.most_common(10),))
print("A4 top10 合计 = %d（设施常量 %d），余额 = %d"
      % (sum(n for _c, n in dist.most_common(10)), V.CODE_TOP10_SUM,
         len(slots) - V.CODE_TOP10_SUM))
print("A5 -85 延迟槽 = %d 处（设施常量 %d）→ %s"
      % (len(s85), V.SLOTS_85_KERNEL, len(s85) == V.SLOTS_85_KERNEL))
print("A6 7 处写点 = %s" % " ".join("0x%06X/0x%06X" % x for x in s85))

# ---------------------------------------------------------------- B
print("")
print("[B] 三条并列出口与可定址入边（只读 ROM，0x041B00..0x041E80）")
SEG = dec(V.SEG_LO, V.SEG_HI)
SPCS = [p for p, _m, _v, _s in SEG]
SNEXT = dict((SPCS[i], SPCS[i + 1]) for i in range(len(SPCS) - 1))
SBY = dict((p, (mn, v)) for p, mn, v, _s in SEG)
edges = dict((e, []) for e in V.EXIT_TRIPLE)
forms = collections.Counter()
for p, mn, v, _s in SEG:
    form, tgt = btarget(p, mn, v)
    if form is None:
        continue
    forms[form] += 1
    if tgt in edges:
        edges[tgt].append(p)
print("B1 分支形态 = %s（设施常量 %s）"
      % (dict(forms), V.BRANCH_FORMS))
for entry in sorted(V.EXIT_TRIPLE):
    r, slot, raw, code = V.EXIT_TRIPLE[entry]
    seq = []
    q = entry
    for _ in range(4):
        if q not in SBY:
            break
        seq.append(SBY[q][0])
        q = SNEXT.get(q, 0)
    got = edges[entry]
    print("B2 入口 0x%06X 模板=%s ret:d=0x%06X 延迟槽=0x%06X 返回码=%+d 入边=%s"
          % (entry, "/".join(seq[:3]), r, slot, code,
             " ".join("0x%06X" % x for x in got) or "(无)"))
print("B3 -85 出口入边 = %s（设施常量 %s）→ %s"
      % (" ".join("0x%06X" % x for x in edges[0x041C9A]),
         " ".join("0x%06X" % x for x in V.EDGES_85),
         tuple(edges[0x041C9A]) == V.EDGES_85))
print("B4 0x041CA0 直接入边 = %d 条，是 ret:d 延迟槽 = %s"
      % (sum(1 for _p, _mn, v, _s in SEG
             if btarget(_p, _mn, v)[1] == 0x041CA0),
         V.is_delay_slot(SEG, 0x041CA0)))

# ---------------------------------------------------------------- C
print("")
print("[C] 服务号归属反查（V82 真内核 32 位有符号口径，只读 ROM）")
by_target = collections.defaultdict(list)
for svc in range(-256, 128):
    slot, half, off, tgt, ok = S.resolve(rd16, svc)
    if ok:
        by_target[tgt].append(svc)
entries = sorted(e for e in by_target if V.SEG_LO <= e <= V.SEG_HI)
print("C1 落在 0x041B00..0x041E80 的入口 = %d（设施常量 %d）→ %s"
      % (len(entries), V.ENTRIES_IN_SEG, len(entries) == V.ENTRIES_IN_SEG))
print("C2 入口清单 = %s" % " ".join("0x%06X:%s" % (e, by_target[e]) for e in entries))
print("C3 0x041BEA 服务号 = %s（唯一 -170 = %s）"
      % (by_target.get(0x041BEA), by_target.get(0x041BEA) == [-170]))
print("C4 7 处 -85 延迟槽的「最近前驱入口」服务号 = %s"
      % " ".join("%s" % (V.owner_of(q, sorted(by_target), by_target),)
                 for _r, q, _svc in V.SLOTS_85))
print("C5 -85 入边 0x041BC6 / 0x041BCC 最近入口 = 0x%06X（服务号 %s，设施 -106）"
      % (V.nearest_entry(0x041BC6, sorted(by_target)),
         V.owner_of(0x041BC6, sorted(by_target), by_target)))
print("C6 真实返回出口 0x041B84 最近入口 = 0x%06X（服务号 %s，设施 -46）"
      % (V.nearest_entry(V.REAL_SLOT0, sorted(by_target)),
         V.owner_of(V.REAL_SLOT0, sorted(by_target), by_target)))
print("C7 规矩 79：-85 出口按地址归 -170、按入边来源归 -106，两种口径不可混用")

# ---------------------------------------------------------------- D
print("")
print("[D] 真实 -170 号服务调用轨迹（1 台只观测轨迹机，不改任何状态）")
m = blive.boot(0)
m.step(V.BOOT_STEP)
r = m.cpu.r
anchor = (m.cpu.pc == V.TRAP and (r[12] & 0xFFFFFFFF) == 0xFFFFFF56)
print("D1 锚点 pc=0x%06X r12=0x%08X → %s" % (m.cpu.pc, r[12] & 0xFFFFFFFF, anchor))
pcs = set()
r13_at = None
ret = None
for _ in range(400):
    pc = m.cpu.pc
    pcs.add(pc)
    if pc == V.ANCHOR1:
        r13_at = r[13] & 0xFFFFFFFF
    if pc == V.RET_PC:
        ret = r[12] & 0xFFFFFFFF
        break
    m.step(1)
print("D2 关键路径命中 = %s"
      % " ".join("0x%06X:%s" % (p, "Y" if p in pcs else "N") for p in V.REAL_PATH_KEYS))
print("D3 0x041C24 处 r13 = 0x%08X（设施 0x%08X）→ %s"
      % (r13_at or 0, V.REAL_R13, r13_at == V.REAL_R13))
print("D4 服务返回点 r12 = 0x%08X（设施 0）→ %s"
      % (ret if ret is not None else 0, ret == V.REAL_RET))
print("D5 三条并列出口覆盖 = 0x%06X:%s / 0x%06X:%s / 0x%06X:%s；"
      "-85 延迟槽 0x041CA0 = %s"
      % (0x041C92, 0x041C92 in pcs, 0x041C9A, 0x041C9A in pcs,
         0x041CA2, 0x041CA2 in pcs, 0x041CA0 in pcs))

# ---------------------------------------------------------------- E
print("")
print("[E] V82 定性修正与口径边界（只读 ROM）")
seg85 = [(p, q) for p, q, raw, code in slots if code == -85
         and V.SEG_LO <= p <= V.SEG_HI]
print("E1 V82「-85 写点 2 个」= 段内口径，实测段内 = %d（设施 %d）"
      % (len(seg85), V.SLOTS_85_IN_SEG))
print("E2 全内核 = %d（设施 %d）→ 口径判定 = %s"
      % (len(s85), V.SLOTS_85_KERNEL,
         V.scope_verdict(len(seg85), len(s85))))
print("E3 0x041CA0 的 ret:d 宿主 = 0x%06X；它没有直接跳转入边"
      % (V.delay_slot_owner(SEG, 0x041CA0) or 0))
print("E4 真正入口是 0x041C9A（orccr/ld/ret:d + 延迟槽），不是 0x041CA0")

# ---------------------------------------------------------------- F
print("")
print("[F] 两道内部条件的等形反事实（4 台独立机器，不强制 PC / 不注入中断）")
KEYS = {
    "ERR_MID": V.ERR_MID, "AND_GATE2": V.ANCHOR2, "BEQ_85": 0x041BCC,
    "EXIT_85": 0x041C9A, "RET_D_85": 0x041C9E, "SLOT_85": 0x041CA0,
    "MAIN_JMP": V.MAIN_JMP, "CMD_SEG": V.CMD_SEG, "CMD_CMP": V.CMD_CMP,
    "WB_CALL": V.WB_CALL, "WB_FN": V.WB_FN,
}


def one(tag, v13, v8):
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    rr = mm.cpu.r
    ok = (mm.cpu.pc == V.TRAP and (rr[12] & 0xFFFFFFFF) == 0xFFFFFF56)
    got = False
    for _ in range(200):
        if mm.cpu.pc == V.ANCHOR1:
            got = True
            break
        mm.step(1)
    if not (ok and got):
        return {"tag": tag, "bad": True}
    if v13 is not None:
        rr[13] = v13 & 0xFFFFFFFF
    seen = set()
    ret12 = None
    saw2 = False
    for _ in range(V.AFTER):
        pc = mm.cpu.pc
        seen.add(pc)
        if pc == V.ANCHOR2:
            saw2 = True
            if v8 is not None:
                rr[8] = v8 & 0xFFFFFFFF
        if pc == V.RET_PC and ret12 is None:
            ret12 = rr[12] & 0xFFFFFFFF
        mm.step(1)
    return {"tag": tag, "boot": V.BOOT_STEP, "anchor1": V.ANCHOR1,
            "anchor2": V.ANCHOR2, "post": V.AFTER, "saw2": saw2,
            "ret": ret12, "npc": len(seen),
            "hit": dict((k, p in seen) for k, p in KEYS.items())}


rows = [one(tag, a, b) for tag, a, b in V.CF_GROUPS]
print("F1 等形四项（boot/anchor1/anchor2/post）全同 = %s"
      % V.equal_shape([x for x in rows if not x.get("bad")]))
print("F2 %-5s %-10s %-10s %-12s %-6s %-6s %s"
      % ("组", "写r13", "写r8", "返回点r12", "PC数", "到锚2", "命中"))
for x in rows:
    if x.get("bad"):
        print("F2 %-5s ANCHOR_BAD" % x["tag"])
        continue
    hs = " ".join(k for k, v in x["hit"].items() if v)
    print("F2 %-5s %-10s %-10s %-12s %-6d %-6s %s"
          % (x["tag"],
             "0x%08X" % dict((t, a) for t, a, _b in V.CF_GROUPS)[x["tag"]]
             if dict((t, a) for t, a, _b in V.CF_GROUPS)[x["tag"]] is not None
             else "(不改)",
             "0x%08X" % dict((t, b) for t, _a, b in V.CF_GROUPS)[x["tag"]]
             if dict((t, b) for t, _a, b in V.CF_GROUPS)[x["tag"]] is not None
             else "(不改)",
             "0x%08X" % x["ret"] if x["ret"] is not None else "未到",
             x["npc"], x["saw2"], hs or "(无)"))
by = dict((x["tag"], x) for x in rows if not x.get("bad"))
print("F3 G2 服务返回点 r12 = 0x%08X（设施 0x%08X）→ %s；"
      "经 0x0406BE extsb 为 -85"
      % (by["G2"]["ret"] or 0, V.CF_G2_RET, by["G2"]["ret"] == V.CF_G2_RET))
print("F4 改写确实改变执行：G2 不同 PC = %d vs BASE = %d，差 = %d"
      % (by["G2"]["npc"], by["BASE"]["npc"],
         by["G2"]["npc"] - by["BASE"]["npc"]))
print("F5 所有改 r13 / r8 都是反事实补全，不是固件行为、不是真机器件事件")

# ---------------------------------------------------------------- G
print("")
print("[G] 两道门与命令分发链（归因分离）")
for tag in ("BASE", "G1", "G2", "G3"):
    x = by[tag]
    h = x["hit"]
    g1, g2 = h["ERR_MID"], h["EXIT_85"]
    chain = all(h[k] for k in V.GATE_KEYS)
    print("G1 %-5s 门1(0x041BB6)=%-5s 门2(-85出口)=%-5s 判定=%-12s "
          "分发链全命中=%-5s 末r12=0x%08X"
          % (tag, g1, g2, V.gate_verdict(g1, g2), chain, x["ret"] or 0))
print("G2 G1 与 G2 只差 r8 一个写入，门2 从关到开 = %s"
      % (by["G1"]["hit"]["EXIT_85"] != by["G2"]["hit"]["EXIT_85"]))
print("G3 G2 与 G3 只差 r8 取值，门2 从开到关（归因分离）= %s"
      % (by["G2"]["hit"]["EXIT_85"] != by["G3"]["hit"]["EXIT_85"]))
print("G4 G2 命中 EXIT_85 / RET_D_85 / SLOT_85 三项 = %s"
      % all(by["G2"]["hit"][k] for k in ("EXIT_85", "RET_D_85", "SLOT_85")))

# ---------------------------------------------------------------- H
print("")
print("[H] 综合判据与边界")
slot_ok = (len(retd) == V.RET_D_TOTAL and len(s85) == V.SLOTS_85_KERNEL
           and V.is_delay_slot(SEG, 0x041CA0))
ret85 = (by["G2"]["ret"] == V.CF_G2_RET)
both = all(by["G2"]["hit"][k] for k in ("EXIT_85", "SLOT_85"))
chain = all(by["G2"]["hit"][k] for k in V.GATE_KEYS)
print("H1 静态/动态/反事实三层的四项前置 = slot_ok:%s ret85:%s "
      "both_gates:%s wb_fn:%s" % (slot_ok, ret85, both, chain))
print("H2 综合判据 = %s" % V.cf_verdict(slot_ok, ret85, both, chain, False))
print("H3 规矩 78：返回码写点先分清 ret:d 延迟槽与基本块入口；"
      "规矩 79：出口归属分清入边来源与地址就近")
print("H4 不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / "
      "所有服务语义已解出 / 真机已复现 -85")
print("H5 仍开放：真机让 vec 0x18 周期性发生的器件事件（KI#191）、"
      "让 -170 号服务走 -85 出口的器件事件语义、两状态字 bit3 置位源、"
      "vec 0x2D 硬件事件、317 个提交调用点非根触发任务")
print("")
print("DONE V83 A–H 八段，5 台独立机器（1 台只观测轨迹机 + 4 台等形反事实机），"
      "反事实全部标注，链未闭合")
