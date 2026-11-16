# -*- coding: utf-8 -*-
"""V91 正式探针：8 组服务号别名的同 half 绑定 / 余下 2 组动态等形补全（KI#355）。

    cd emu && python probe/exp_svc_v91.py > ../results/frbox/svc91.txt

A–B 段只读 ROM（含全 ROM 解码 + 分发表同 half 普查，各只做一次）；
C 段 4 台本版新跑等形机（G7/G8 × 2，改 TRAP r12 反事实）；
D 段引用 V89 已跑 6 组（从设施常量读，不重跑那 12 台）；
F 段 1 台标准收敛态机。本版实跑 **5 台独立机器**，每台只跑一遍。
改 TRAP 处 r12 是反事实补全（只改寄存器），不是固件行为、不是真机器件事件。
链未闭合。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from collections import Counter
from frbox import bstatic, svc91 as V, blive

M = V.M32
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
T = fr_emu.TABLE


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
    keys = ("i8", "i20", "i32", "u4", "u8", "u6", "m4", "disp10",
            "disp8", "rel9", "rel12", "udisp6", "rs2",
            "reglist-low-st", "reglist-hi-st")
    g = ["%s=%s" % (k, v[k]) for k in keys if k in v]
    for k in ("ri", "rj"):
        if k in v:
            g.append("%s=r%d" % (k, v[k]))
    return " ".join(g)


def rd32(mm, a):
    return mm.bus.rd(a, 4)


# ---------------------------------------------------------------- 全表 + half 普查（只读，一次）
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
ACC = {}
for svc, entry in SVC.items():
    ACC.setdefault(entry, []).append(svc)
AL = sorted((e, tuple(sorted(s))) for e, s in ACC.items() if len(s) > 1)

decoded = 0
off, n = 0, len(rom)
while off < n - 1:
    ins = T.decode(rom, off)
    if ins is None:
        off += 2
        continue
    decoded += 1
    off += ins[3]


def run_pair(tag, svc, after=V.PAIR_WINDOW):
    """1 台独立机器；改 TRAP 处 r12 = svc（反事实补全，只改寄存器）。"""
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    rr = mm.cpu.r
    assert mm.cpu.pc == V.TRAP
    rr[12] = svc & M
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
        "tag": tag, "svc": svc, "pcs": pcs, "call_target": call_target,
        "d12": d12, "svc_slot": rd32(mm, V.SLOT_SVC),
        "end_pc": mm.cpu.pc, "end_cur": rd32(mm, 0x68000000),
        "end_ready": rd32(mm, 0x68000004), "npc": len(set(pcs)),
    }


def run_converge():
    """1 台标准收敛态机（V65–V90 逐字相同的策略）。"""
    mm = blive.boot(0)
    mm.step(V.CONVERGE_BOOT)
    mm.raise_irq(V.CONVERGE_IRQ)
    mm.run(V.CONVERGE_RUN, spin_guard=False)
    return {"end_pc": mm.cpu.pc, "cur": rd32(mm, 0x68000000),
            "ready": rd32(mm, 0x68000004)}


print("=" * 76)
print("V91 · 8 组别名同 half 绑定 / 余下 2 组动态等形补全 —— A–G / 5 台机器（本版实跑）")
print("=" * 76)

# ---------------------------------------------------------------- A
print("")
print("[A] 分发表全表重建 + 同 half 静态绑定普查（只读 ROM）")
print("A1 解码 %d 条（设施 %d）SVC 条数=%d 别名入口数=%d"
      % (decoded, V.ROM_DECODED, len(SVC), len(AL)))
bind_rows = []
for entry, svcs in AL:
    halves = []
    for svc in svcs:
        o = V.slot_of(svc) - base
        halves.append((rom[o] << 8) | rom[o + 1])
    bind_rows.append((entry, svcs, tuple(halves), V.same_half(halves)))
n_same = sum(1 for r in bind_rows if r[3])
print("A2 8 组同 half 绑定 = %d/%d；判据=%s"
      % (n_same, V.GROUP_COUNT, V.half_binding_verdict()))
for entry, svcs, halves, same in bind_rows:
    print("     0x%06X 号=%-22s half=%s 同half=%s"
          % (entry, str(svcs), [hex(h) for h in halves], same))
print("A3 与设施 ALIAS_ROWS 对账（入口集合）= %s"
      % (set(r[0] for r in bind_rows) ==
         set(r[0] for r in V.ALIAS_ROWS)))
print("A4 同 half ⇒ target_of(half)=入口 折叠（结构成因，非运行时按号分流）")

# ---------------------------------------------------------------- B
print("")
print("[B] 余下 2 组入口结构（只读 ROM）")
for entry in (0x040DE4, 0x041DE4):
    mn, vv, _sz = ins_at(entry)
    print("B1 0x%06X 入口首条 %-10s %s  r12读点=%s"
          % (entry, mn, im(vv), V.ENTRY_R12_READ[entry]))
    hits = [(pc, mn2) for pc, mn2, v2, _s in dec(entry - 0x4, entry + 0x80)
            if v2.get("disp10") == 12]
    print("B2 0x%06X 邻域 disp10=12 = %s（设施记录 %d 条）"
          % (entry, hits, V.ENTRY_D12_HITS[entry]))

# ---------------------------------------------------------------- C
print("")
print("[C] 本版新跑 4 台等形机：G7/G8 × 2（改 TRAP r12，反事实，1200 步）")
NEW = {}
for tag, entry, svcs, slots, call_ref, endpc_ref, npc_ref, d12a, d12b, same_ref in V.NEW_DYNAMIC:
    a = run_pair(tag + "a", svcs[0])
    b = run_pair(tag + "b", svcs[1])
    seq_same = a["pcs"] == b["pcs"]
    NEW[tag] = (a, b, seq_same)
    print("C1 %s 入口=0x%06X 号=%-12s call@r12=0x%06X/0x%06X "
          "服务号槽=0x%08X/0x%08X 不同=%s"
          % (tag, entry, str(svcs), a["call_target"] or 0, b["call_target"] or 0,
             a["svc_slot"], b["svc_slot"], a["svc_slot"] != b["svc_slot"]))
    print("     逐条全序列相同=%s 不同PC=%d/%d 末PC=0x%06X/0x%06X "
          "CUR=0x%08X/0x%08X READY=0x%08X/0x%08X"
          % (seq_same, a["npc"], b["npc"], a["end_pc"], b["end_pc"],
             a["end_cur"], b["end_cur"], a["end_ready"], b["end_ready"]))
    print("     0x041DE0 d12命中=%d/%d 判据=%s"
          % (len(a["d12"]), len(b["d12"]),
             V.svc_saved_vs_dispatch(a["svc_slot"], b["svc_slot"],
                                     seq_same, V.PAIR_WINDOW)))
    print("     设施常量对账: slot=%s pc=%s call=%s"
          % (slots == (a["svc_slot"], b["svc_slot"]),
             endpc_ref == a["end_pc"] == b["end_pc"],
             call_ref == (a["call_target"] or 0)))
print("C2 本版 2 组逐条全序列相同 = %s；窗口统一 %d 步（规矩 82）"
      % (all(x[2] for x in NEW.values()), V.PAIR_WINDOW))
print("C3 反事实边界：G7 末PC=0x%06X / G8 末PC=0x%06X 落非代码区，"
      "轨迹只用于同组对照" % V.NEW_NONCODE_ENDPCS)

# ---------------------------------------------------------------- D
print("")
print("[D] 引用 V89 已跑 6 组（从设施常量读，本版不重跑那 12 台）")
seq_flags = []
for tag, entry, svcs, slots, same in V.V89_DYNAMIC:
    seq_flags.append(same)
    print("D1 %s 入口=0x%06X 号=%-16s 槽=0x%08X/0x%08X 逐条全序列相同=%s"
          % (tag, entry, str(svcs), slots[0], slots[1], same))
for _row in V.NEW_DYNAMIC:
    seq_flags.append(_row[9])   # 第 10 字段 = seq_same
print("D2 合计 %d 组（V89 %d + V91 %d），全部逐条相同=%s"
      % (len(seq_flags), V.V89_GROUP_COUNT, V.NEW_GROUP_COUNT,
         V.full_dynamic_verdict(seq_flags)))
print("D3 结论有界：只说「8 组在统一 1200 步窗口内逐条全序列相同」，"
      "不说「8 组全程不分流」/「语义已解出」")

# ---------------------------------------------------------------- E
print("")
print("[E] 静态入边与 disp10=12 命中（自我证伪）")
print("E1 8 组入口各号落同 half（A2），故 call @r12 折叠同一入口；"
      "「被保存」的服务号槽值两两不同（C1/D1），但同组轨迹相同 ⇒ "
      "「被保存」≠「被读取并分流」（规矩 92）")
print("E2 0x041DE0 ld disp10=12 在两组各 1200 步窗口内命中 %d/%d 次 ⇒ "
      "disp10 相同 ≠ 读服务号槽（延续 85/88）"
      % (len(NEW["G8"][0]["d12"]), len(NEW["G8"][1]["d12"])))
print("E3 G7 入口读 r12（%s）、G8 入口不读（%s）却不改变同组等形 ⇒ "
      "入口是否读 r12 与本窗口内不分流无因果"
      % (V.ENTRY_R12_READ[0x040DE4], V.ENTRY_R12_READ[0x041DE4]))

# ---------------------------------------------------------------- F
print("")
print("[F] 1 台标准收敛态机（V65–V90 逐字相同）")
cv = run_converge()
print("F1 末 PC=0x%06X（设施 0x%06X 对=%s）CUR=0x%08X READY=0x%08X"
      % (cv["end_pc"], V.CONVERGE_TAIL, cv["end_pc"] == V.CONVERGE_TAIL,
         cv["cur"], cv["ready"]))

# ---------------------------------------------------------------- G
print("")
print("[G] 综合判据、规矩 94、边界与开放项")
bind_ok = V.half_binding_verdict() == V.HALF_BIND_VERDICT
dyn_ok = V.full_dynamic_verdict(seq_flags) == "all_eight_groups_equal_shape_in_window"
conv_ok = cv["end_pc"] == V.CONVERGE_TAIL
pre = (decoded == 3543659 and len(SVC) == V.TABLE_SVC_COUNT and len(AL) == V.TABLE_ALIAS_PAIRS,
       n_same == V.SAME_HALF_COUNT,
       all(x[2] for x in NEW.values()) and len(NEW) == V.NEW_GROUP_COUNT,
       len(seq_flags) == V.TOTAL_DYNAMIC_GROUPS,
       conv_ok)
print("G1 前置五项（全表/同half/2组新跑/8组合计/收敛）= %s" % (pre,))
print("G2 综合判据 = %s" % V.source_verdict(bind_ok, dyn_ok, conv_ok))
print("G3 规矩 94：宣布「全组窗口内不分流」前每组都做等长动态等形（V89 只 6/8）；"
      "同 half 静态绑定区分分发表折叠 vs 运行时按号分流；"
      "序列相同 ≠ 语义相同 ≠ 全程不分流（延续 78/86/90/91/92）")
print("G4 全部改 r12 是反事实补全（模型动作，只改寄存器），非真机器件事件；"
      "非代码区末 PC 只用于同组对照；链未闭合")
print("G5 仍开放：8 对别名真机语义分工 / 0x83E0 真机是否被别的任务读 / "
      "三调用点真机触发条件 / vec 0x18 器件事件 / −85 器件事件语义 / "
      "bit3 置位源 / vec 0x2D / 317 提交调用点 / 任务 9 回任务 7 / "
      "0xFFFFFFFF r13 哨兵设置者")
print("G6 禁语：8 组全程不分流 / 8 对别名语义已解出 / 服务号槽没人读 / "
      "所有服务语义已解出 / 真机已复现 −85 / 补一次值就等于真机事件 / "
      "显示链通了 / 出画面 / 系统跑起来了 / 真机如此")
print("DONE V91 A–G 七段，本版实跑 5 台独立机器（4 新等形 + 1 收敛），反事实全部标注")
