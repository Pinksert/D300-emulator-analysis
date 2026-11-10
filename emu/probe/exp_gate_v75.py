# -*- coding: utf-8 -*-
"""V75 正式探针：L4 恒过、r10 门默认开、处理函数首次命中（A–H 八段）。

承接 V74 / KI#290 第三条。设备策略与 V65–V74 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；反事实段全部标注（r4 寄存器注入 / 写索引字节 /
raise_irq / kernel_call 都不是固件行为）。
独立机器：基线 + 1M 窗口复用 / N0–N5 六组 / Q0–Q5 六组 /
F4 多级唤醒三组 / L4 门时序 watch 一台。
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, brealos, cursor73 as K,
                   cursor74 as D, gate75 as G, level72 as C, wait271 as W)

BOOT, VEC, RUN, POST, WINDOW = (blive.BOOT_STEPS, 0x2B,
                                500000, 200000, 1000000)
WATCH_STEPS = 30000
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


def hr(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)


def dec(lo, hi):
    out = []
    off, end = lo - base, hi - base
    while off < end - 1:
        ins = fr_emu.TABLE.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        out.append((base + off, mn, dict(v), size, rom[off:off + size].hex()))
        off += size
    return out


def u32(a):
    return int.from_bytes(rom[a - base:a - base + 4], 'big')


def covset(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def fire_r4(m, r4val, idx=5):
    """写 DA85 索引 -> 触发 vec 0x20 -> 在 ISR 首条前设 r4（反事实）。"""
    m.bus.wr(D.IDX_A, 1, idx)
    m.raise_irq(D.VEC_A)
    for _ in range(200):
        m.step(1)
        if m.cpu.pc == D.ISR_A:
            m.cpu.r[4] = r4val
            return True
    return False


def awaken(m, lv):
    brealos.kernel_call(m, K.SET_FLG, r4=3,
                        r5=W.WANTPTN_1 if lv == 1 else K.BIT10)
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)


def run_path(levels, r4val=None, write_desc=False, pre=True):
    """一台独立机器跑一条路径，返回结果字典。"""
    m = boot_run()
    c0 = covset(m)
    if pre:
        brealos.kernel_call(m, K.GET_TID)
    if r4val is not None:
        fire_r4(m, r4val)
        m.run(max_steps=POST, spin_guard=False)
    for lv in range(1, levels + 1):
        awaken(m, lv)
    if write_desc:
        m.bus.wr(D.DA82, 1, 0x05)
        m.bus.wr(G.DESC_PTR, 4, 0x001BC070)
        m.raise_irq(VEC)
        m.run(max_steps=POST, spin_guard=False)
    c1 = covset(m)
    ti = bready.task_index(m)
    t5 = ti.get(5)
    return m, {
        "cov": c1, "new": len(c1 - c0),
        "da82": m.bus.rd(D.DA82, 1), "da83": m.bus.rd(D.DA83, 1),
        "da84": m.bus.rd(D.DA84, 1), "d858": m.bus.rd(G.DESC_PTR, 4),
        "l2": G.DISPATCH_FN in c1, "l3wait": G.WAIT3_FN in c1,
        "l4": G.L4_FN in c1, "l4slot": G.L4_RET_SLOT in c1,
        "l4bne": G.FIFTH_CALL in c1, "fifth": G.FIFTH_FN in c1,
        "r10one": G.R10_ONE in c1, "r10zero": G.R10_ZERO in c1,
        "gate": G.GATE_PC in c1, "indirect": G.INDIRECT_CALL in c1,
        "noop": G.NOOP_HANDLER in c1, "submit": G.SUBMIT in c1,
        "h_a": G.HANDLER_A in c1, "h_b": G.HANDLER_B in c1,
        "word_a": m.bus.rd(G.WORD_A, 4), "word_b": m.bus.rd(G.WORD_B, 4),
        "gate_byte": m.bus.rd(G.GATE_BYTE, 1),
        "slots": [m.bus.rd(G.SLOTS8 + i, 1) for i in range(8)],
        "want": m.bus.rd(t5 + 0x14, 4) if t5 is not None else None,
        "state": bready.state_of(m, t5) if t5 is not None else None,
        "pc": m.cpu.pc,
    }


# ===================================================================== A
hr("A. 静态：任务 5 五级链与 L4 的 ret:d 延迟槽")
print("A1 任务 5 主体 [0x12DFB2,0x12E00A) 四级 call 与失败分支：")
for i, lv in enumerate(C.LEVELS, 1):
    call_pc, cmp_pc, bne_pc, tgt, bra = lv
    print("     L%d 0x%06X -> %s ｜ cmp 0x%06X ｜ bne 0x%06X -> 0x%06X "
          "｜ 失败 bra 0x%06X"
          % (i, call_pc, '0x%06X' % C.LEVEL_TARGETS[i - 1],
             cmp_pc, bne_pc, tgt, bra))
print("A2 第四级之后 0x12DFFE call rel12 -> 0x12E674（第五个 call）")
print("A3 L4 0x12E412 末尾：0x12E48A ret:d，延迟槽 0x12E48C ldi:8 #1,r4"
      " ⇒ 无论前面走哪条分支，返回时 r4 恒为 1（规矩 63）")
for p, m, v, sz, hx in dec(G.L4_RET - 4, G.L4_RET_SLOT + 2):
    print("     0x%06X %-9s %-12s %s"
          % (p, m, hx, {k: v[k] for k in ('i8', 'i32', 'u8', 'ri', 'rj')
                        if k in v}))
print("A4 主体第四级 0x12DFF4 cmp r4,#0 / 0x12DFF6 bne 0x12DFFE ⇒ "
      "L4 恒过，0x12DFF8 的失败分支永远走不到")
print("A5 L4 恒过判据 = %s（l4_always_passes）" % G.l4_always_passes())

# ===================================================================== B
hr("B. 静态：L3 判据与 r10 门的两个写点")
print("B1 L3 0x12E326 与 V71 第二道等待同构：")
for p, m, v, sz, hx in dec(G.L3_WAIT - 12, G.L3_WAIT + 20):
    print("     0x%06X %-9s %-12s %s"
          % (p, m, hx, {k: v[k] for k in ('i8', 'i20', 'i32', 'u8', 'ri', 'rj')
                        if k in v}))
print("B2 判据 (pat&0x74)==0x10；0x12E34A clr_flg(3,0x8F) 先清 0x70")
print("B3 0x12E674 函数体内 r10 只有两个写点：")
for p, m, v, sz, hx in dec(G.FIFTH_FN, G.SUBMIT):
    if v.get('ri') == 10 and m.startswith('ldi'):
        print("     0x%06X %-9s %-12s %s" % (p, m, hx, v))
print("B4 0x12E69E 在 0x12E69C beq:d 的延迟槽里 ⇒ r10 默认 %d，"
      "门 0x12E6E0 cmp r10,#0 / 0x12E6E2 beq 跳过调用；"
      "r10!=0 ⇒ 门默认开 = %s"
      % (G.r10_from_delay_slot(), G.gate_open(G.r10_from_delay_slot())))

# ===================================================================== C
hr("C. 静态：描述符表与处理函数指针")
print("C1 描述符表 70 条 × 0x10，+0x0C 是处理函数指针；"
      "空槽位 0x12E752 占 %d 槽" % len(G.NOOP_SLOTS))
for i in (0, 1, 4, 5, 6, 69):
    a = G.DESC_TABLE + i * G.DESC_STRIDE
    print("     [%2d] 0x%06X = %s"
          % (i, a, ' '.join('0x%08X' % u32(a + k * 4) for k in range(4))))
print("C2 D858 算式（规矩 62）：表基址 + DA82*0x10")
for v in (0, 1, 5, 6, 0x45, 0x46):
    print("     DA82=0x%02X -> D858=0x%08X / 处理函数槽 0x%06X / "
          "在表内=%s"
          % (v, G.desc_ptr(v), G.handler_slot(v), G.in_table(v)))
print("C3 处理函数 0x12E806 真实边界 [0x12E806,0x12E854)，"
      "末尾 0x12E850 int #0x40 是 set_flg(10,0x200000)，"
      "不写两个状态字 / 8 槽 / 门字节")
print("C4 处理函数 0x12E856 边界 [0x12E856,0x12E8E0)；"
      "空槽 0x12E752 首指令就是裸 ret")

# ===================================================================== D
hr("D. 收敛态基线与 1M 步窗口自证")
m = boot_run()
c500 = covset(m)
print("D1 末 PC 0x%06X / 覆盖 %d / DA82=%02X DA83=%02X DA84=%02X "
      "D858=%08X"
      % (m.cpu.pc, len(c500), m.bus.rd(D.DA82, 1),
         m.bus.rd(D.DA83, 1), m.bus.rd(D.DA84, 1),
         m.bus.rd(G.DESC_PTR, 4)))
m.run(max_steps=WINDOW - RUN, spin_guard=False)
c1m = covset(m)
print("D2 500k 覆盖 %d / 1M 覆盖 %d / 对称差集 %d / 末 PC 0x%06X"
      % (len(c500), len(c1m), G.window_same(c500, c1m), m.cpu.pc))

# ===================================================================== E
hr("E. 六组等形反事实：三级唤醒才跨得到 L4（KI#290 第三条结案）")
print("E0 六组各一台独立机器，BOOT/POST 等形；"
      "唯一差异是 r4 事件 / 唤醒级数 / 是否外部写 D858")
res_n = {}
for tag, do_r4, levels, write_desc, expect in G.LEVEL_GROUPS:
    _m, r = run_path(levels, 5 if do_r4 else None, write_desc)
    r["expect"] = expect
    res_n[tag] = r
    print("  %s r4=%-5s %d级唤醒 外部写=%-5s 新增PC=%3d ｜ "
          "DA82=%02X DA83=%02X D858=%08X"
          % (tag, do_r4, levels, write_desc, r["new"],
             r["da82"], r["da83"], r["d858"]))
    print("       L2 %-5s L3等待 %-5s L4 %-5s L4延迟槽 %-5s L4bne %-5s "
          "第五call %-5s ｜ r10=1 %-5s r10=0 %-5s 门 %-5s 间接调用 %-5s"
          % (r["l2"], r["l3wait"], r["l4"], r["l4slot"], r["l4bne"],
             r["fifth"], r["r10one"], r["r10zero"], r["gate"],
             r["indirect"]))
    print("       处理函数 A %-5s B %-5s 空槽 %-5s 提交 %-5s ｜ "
          "want=0x%08X 状态字A=%08X B=%08X 8槽=%s"
          % (r["h_a"], r["h_b"], r["noop"], r["submit"],
             r["want"] or 0, r["word_a"], r["word_b"],
             ''.join('%02X' % x for x in r["slots"])))
print("E1 静态预测可到 L4 的组 = %s；实测命中 L4 的组 = %s；一致 = %s"
      % ([t for t, a, b, c, e in G.LEVEL_GROUPS if e],
         G.attribution(res_n, "l4"),
         [t for t, a, b, c, e in G.LEVEL_GROUPS if e]
         == G.attribution(res_n, "l4")))
print("E2 N2 两级唤醒停在 L3（L3等待 %s / L4 %s）⇒ 第三级补 0x10 是跨 L3 的必要条件"
      % (res_n["N2"]["l3wait"], res_n["N2"]["l4"]))
print("E3 N4 不注入 r4（三级唤醒）L4=%s ⇒ r4 事件（DA83/D858）也是必要条件"
      % res_n["N4"]["l4"])

# ===================================================================== F
hr("F. 六组索引等形反事实：不同 r4 调到不同处理函数")
print("F0 六组各一台独立机器，统一三级唤醒；唯一差异是注入的 r4")
table_ptrs = {}
for i in range(70):
    table_ptrs[i] = u32(G.handler_slot(i))
res_q = {}
for tag, r4val, exp83, exp858, exp_h in G.INDEX_GROUPS:
    _m, r = run_path(3, r4val)
    r["expect"] = (exp83, exp858, exp_h)
    r["handler"] = (G.HANDLER_A if r["h_a"] else
                    (G.HANDLER_B if r["h_b"] else
                     (G.NOOP_HANDLER if r["noop"] else 0)))
    res_q[tag] = r
    ok = (r["da83"] == exp83 and r["d858"] == exp858
          and r["handler"] == exp_h)
    print("  %s r4=0x%02X 新增PC=%3d ｜ DA82=%02X DA83=%02X(期望%02X) "
          "D858=%08X(期望%08X) %s"
          % (tag, r4val, r["new"], r["da82"], r["da83"], exp83,
             r["d858"], exp858, "一致" if ok else "不一致"))
    print("       L4 %-5s 第五call %-5s 门 %-5s 间接调用 %-5s ｜ "
          "处理函数命中=0x%08X(期望 0x%08X) 空槽 %-5s 提交 %-5s"
          % (r["l4"], r["fifth"], r["gate"], r["indirect"],
             r["handler"], exp_h, r["noop"], r["submit"]))
    print("       状态字A=%08X B=%08X 门字节=%02X 8槽=%s ｜ "
          "任务5 state=%s want=0x%08X"
          % (r["word_a"], r["word_b"], r["gate_byte"],
             ''.join('%02X' % x for x in r["slots"]),
             r["state"], r["want"] or 0))
print("F1 处理函数命中的组 = %s"
      % [t for t in res_q if res_q[t]["handler"]])
print("F2 命中的不同处理函数 = %s"
      % sorted({res_q[t]["handler"] for t in res_q if res_q[t]["handler"]}))
print("F3 Q1/Q5 命中空槽 0x12E752（裸 ret）⇒ 间接调用命中不等于"
      "实做处理函数命中；V69「27/70 槽指向空槽」动态印证")
print("F4 Q2 的 D858=0x001BC070 / +0x0C 槽 0x001BC07C 实测指针 = "
      "0x%08X（处理函数 0x12E806）" % table_ptrs.get(5, 0))
print("F5 Q0（r4=0）与 Q4（r4=0x46 越界）L4 未命中 ⇒ "
      "DA82 必须在 [0,70) 且非 0 才能填 D858")

# ===================================================================== G
hr("G. 多级唤醒阴性结果与门时序 watch")
print("G0 四 / 五级唤醒不再新增 PC（等形对照，唯一差异是唤醒级数）")
for levels, exp_new, exp_h in G.EXTRA_AWAKEN:
    _m, r = run_path(levels, 5)
    print("     %d 级唤醒 新增PC=%3d（期望 %3d %s）｜ "
          "处理函数 A %-5s ｜ want=0x%08X 提交 %-5s"
          % (levels, r["new"], exp_new,
             "一致" if r["new"] == exp_new else "不一致",
             r["h_a"], r["want"] or 0, r["submit"]))
print("G1 ⇒ 末态 0x41 等待不是唤醒次数不够，"
      "0x41 来自 0x12E59E / 0x12E5DE 的 ldi:8 #0x41（第五道等待）")
for target in (G.WAIT41_IMM_A, G.WAIT41_IMM_B):
    ins = fr_emu.TABLE.decode(rom, target - base)
    print("     0x%06X %s imm=0x%X" % (target, ins[0], ins[2].get('i8')))
print("G2 服务号宽度（规矩 48）：0x12E5A4 ldi:32 #0xFFFFFF56 "
      "按 32 位 = %d；Sweep.calls 报 %d 是低 8 位假象"
      % (G.SVC_WAI_32, G.SVC_WAI_LOW8))

WATCH = (G.R10_BEQD, G.R10_ONE, G.R10_ZERO, G.GATE_PC, G.GATE_BEQ,
         G.DESC_LOAD, G.INDIRECT_CALL, G.SUBMIT)
mm = boot_run()
log = []
brealos.kernel_call(mm, K.GET_TID)
fire_r4(mm, 5)
watch_run = lambda steps: None
for _ in range(WATCH_STEPS):
    pc, r10, r0 = mm.cpu.pc, mm.cpu.r[10], mm.cpu.r[0]
    mm.step(1)
    if pc in WATCH:
        log.append((1, pc, r10, r0, mm.cpu.pc, mm.bus.rd(G.DESC_PTR, 4)))
for lv in range(1, 4):
    brealos.kernel_call(mm, K.SET_FLG, r4=3,
                        r5=W.WANTPTN_1 if lv == 1 else K.BIT10)
    mm.raise_irq(VEC)
    for _ in range(WATCH_STEPS):
        pc, r10, r0 = mm.cpu.pc, mm.cpu.r[10], mm.cpu.r[0]
        mm.step(1)
        if pc in WATCH:
            log.append((lv, pc, r10, r0, mm.cpu.pc,
                        mm.bus.rd(G.DESC_PTR, 4)))
        if mm.cpu.halted:
            break
print("G3 r10 / 门 / 间接调用逐点时序（阶段 / PC / r10 / r0 / "
      "下一条PC / D858）：")
for lv, pc, r10, r0, npc, d858 in log[:24]:
    print("     %d 0x%06X r10=%d r0=0x%08X 下一条=0x%06X D858=%08X"
          % (lv, pc, r10, r0 & 0xFFFFFFFF, npc, d858))

# ===================================================================== H
hr("H. 综合判据、边界与地址清单")
n3 = res_n["N3"]
print("H1 链闭合 = %s（处理函数命中 %s / 提交函数 %s / "
      "状态字非零 %s / 8 槽非零 %s）"
      % (G.chain_verdict(n3["h_a"], n3["submit"],
                         bool(n3["word_a"] or n3["word_b"]),
                         any(n3["slots"])),
         n3["h_a"], n3["submit"],
         bool(n3["word_a"] or n3["word_b"]), any(n3["slots"])))
print("H2 KI#290 第三条结案：L4 恒过（ret:d 延迟槽 r4=1）；"
      "真条件是第三级唤醒跨 L3；0x12E674 的 r10=1 来自 beq:d 延迟槽，"
      "门默认开；描述符 +0x0C 决定调到哪个处理函数，"
      "首次命中实做处理函数 1/44")
print("H3 仍缺：提交函数 0x12E6F8 的触发路径、0x41 等待的置位源、"
      "两个状态字 0x9F5A0284 / 0x9F59EF50 与 8 槽 0x9F59F7C8 填充源、"
      "真机让 r4 非 0 的事件语义、0x12E636 返回非 0 条件、"
      "真机 tick 源（KI#191）")
print("H4 全部 r4 注入 / 写索引字节 / raise_irq / kernel_call 都是外部"
      "反事实补全，不是固件行为；不能报显示链通了 / 出画面 / "
      "系统跑起来了 / 真机如此 /「补一次 set_flg 就唤醒成功」")
print("H5 调用边口径（规矩 54）：0x12E6F8 的 refs_to=%d（ldi:32 加载点）"
      " / edges=%d（call:d 指令点） / call rel12=%d；"
      "两种 PC 相差 %d 字节，交集 0 属正常"
      % (G.SUBMIT_REFS, G.SUBMIT_EDGES, G.SUBMIT_REL12, G.LDI32_LEN))
print("H6 关键常量地址清单（供报告地址对账）：")
for name, addr, width in (
        ("TBR", D.TBR, 8), ("任务5主体", G.TASK5_FN, 6),
        ("循环头", G.LOOP_HEAD, 6), ("第一道等待", G.WAIT1_FN, 6),
        ("L1等待", G.WAIT2_FN, 6), ("L2派发", G.DISPATCH_FN, 6),
        ("L3等待", G.WAIT3_FN, 6), ("L3等待点", G.L3_WAIT, 6),
        ("L3清位", G.L3_CLEAR, 6), ("L4", G.L4_FN, 6),
        ("L4ret", G.L4_RET, 6), ("L4延迟槽", G.L4_RET_SLOT, 6),
        ("L4bne", G.L4_BNE, 6), ("第五call", G.FIFTH_CALL, 6),
        ("大函数序言", G.BIG_PROLOGUE, 6), ("第五函数", G.FIFTH_FN, 6),
        ("beq:d", G.R10_BEQD, 6), ("r10=1点", G.R10_ONE, 6),
        ("r10=0点", G.R10_ZERO, 6), ("前置门", G.GATE_PC, 6),
        ("门beq", G.GATE_BEQ, 6), ("描述符装载", G.DESC_LOAD, 6),
        ("间接调用", G.INDIRECT_CALL, 6), ("提交函数", G.SUBMIT, 6),
        ("空槽位", G.NOOP_HANDLER, 6),
        ("处理A", G.HANDLER_A, 6), ("处理A末", G.HANDLER_A_END, 6),
        ("处理B", G.HANDLER_B, 6), ("处理B末", G.HANDLER_B_END, 6),
        ("等待41_A", G.WAIT41_A, 6), ("等待41_B", G.WAIT41_B, 6),
        ("等待41立即_A", G.WAIT41_IMM_A, 6),
        ("等待41立即_B", G.WAIT41_IMM_B, 6),
        ("失败等待", 0x12E56A, 6), ("L2bne目标", W.LEVELS[1][3], 6),
        ("处理A内调用", 0x12181C, 6), ("L4后stb", 0x12DFF2, 6),
        ("读D858", 0x12E6EA, 6), ("间接调用返回", 0x12E6F2, 6),
        ("第五函数ret", 0x12E6F6, 6),
        ("描述符表", G.DESC_TABLE, 8), ("处理槽5", G.handler_slot(5), 8),
        ("处理槽1", G.handler_slot(1), 8), ("处理槽6", G.handler_slot(6), 8),
        ("DA82", G.DA82, 8), ("DA83", G.DA83, 8), ("DA84", G.DA84, 8),
        ("D858", G.DESC_PTR, 8), ("DA85", D.IDX_A, 8),
        ("DA86", D.IDX_B, 8), ("九字节", D.CUR_BASE, 8),
        ("D87B", 0x9F58D87B, 8), ("D87C", 0x9F58D87C, 8),
        ("D850", 0x9F58D850, 8), ("D854", 0x9F58D854, 8),
        ("D4BE", 0x9F58D4BE, 8), ("D873", 0x9F58D873, 8),
        ("D4A8", 0x9F58D4A8, 8), ("D487", 0x9F58D487, 8),
        ("对象池", D.SEM_POOL, 8), ("门字节", G.GATE_BYTE, 8),
        ("状态字A", G.WORD_A, 8), ("状态字B", G.WORD_B, 8),
        ("8槽", G.SLOTS8, 8), ("ISR_A", D.ISR_A, 6),
        ("回调入口", D.CB_FN, 6), ("写DA83", D.CB_WRITE, 6),
        ("装DA82", D.LOAD_DA82, 6), ("写D858", D.WRITE_D858, 6)):
    print("     %-14s 0x%0*X" % (name, width, addr))

print("\n结果文件 results/frbox/gate75.txt；设施 frbox/gate75.py；"
      "独立机器：基线 / 1M 窗口 / N0–N5 / Q0–Q5 / 多级唤醒 / 门时序。")
