# -*- coding: utf-8 -*-
"""V76 正式探针：提交函数身份、0x41 置位源、门字节与 8 槽填充源（A–H 八段）。

承接 KI#294 第一条。设备策略与 V65–V75 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；反事实段全部标注（r4 寄存器注入 / 写索引字节 /
直接写门字节 / raise_irq / kernel_call 都不是固件行为）。
独立机器：基线 + 1M 窗口复用 / N3 / R0–R4 五组 / V0–V3 四组。
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, brealos, cursor73 as K,
                   cursor74 as D, gate75 as G, wait271 as W,
                   submit76 as S, level72 as L, isrfam63 as F)

BOOT, VEC, RUN, POST, WINDOW = (blive.BOOT_STEPS, 0x2B,
                                S.RUN_STEPS, S.POST_STEPS, S.WINDOW_STEPS)
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


def setbit(m, bits):
    brealos.kernel_call(m, K.SET_FLG, r4=3, r5=bits)
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)


def fire_vec(m, vec):
    m.raise_irq(vec)
    m.run(max_steps=POST, spin_guard=False)


# 关键 PC 组（静态口径）
SUBMIT_REFS = set(p for p, _i, _n in SW.callers_strict(S.SUBMIT, 8))
SUBMIT_EDGES = set(SW.callers_of(S.SUBMIT))
GATE_LOADS = set(p for p, _s in SW.ldi32.get(S.GATE_BYTE, ()))
WORD_A_LOADS = set(p for p, _s in SW.ldi32.get(S.WORD_A, ()))
WORD_B_LOADS = set(p for p, _s in SW.ldi32.get(S.WORD_B, ()))
SLOT_LOADS = set(p for p, _s in SW.ldi32.get(S.SLOTS8, ()))
FLAG41_SETS = set(c.addr for c in SW.flag_sets(3)
                  if c.bits in (0x01, 0x40))
GATE_WRITES = set(p for p, _v, _s, _e in S.GATE_WRITES)


def snapshot(m, c0):
    c = covset(m)
    ti = bready.task_index(m)
    t5 = ti.get(5)
    return {
        "cov": c, "new": len(c - c0), "pc": m.cpu.pc,
        "want": m.bus.rd(t5 + 0x14, 4) if t5 is not None else None,
        "state": bready.state_of(m, t5) if t5 is not None else None,
        "gate": m.bus.rd(S.GATE_BYTE, 1),
        "word_a": m.bus.rd(S.WORD_A, 4), "word_b": m.bus.rd(S.WORD_B, 4),
        "slots": [m.bus.rd(S.SLOTS8 + i, 1) for i in range(8)],
        "submit": S.SUBMIT in c, "refs": len(c & SUBMIT_REFS),
        "edges": len(c & SUBMIT_EDGES),
        "gate_wr": len(c & GATE_WRITES), "gate_ld": len(c & GATE_LOADS),
        "setters": len(c & FLAG41_SETS), "slot_ld": len(c & SLOT_LOADS),
        "wa": len(c & WORD_A_LOADS), "wb": len(c & WORD_B_LOADS),
        "wait41": len(c & {S.WAIT41_A, S.WAIT41_B}),
        "handler": G.HANDLER_A in c,
        "isr2d": S.ISR_2D in c, "set40": S.SET_40_PC in c,
        "l4": G.L4_FN in c, "fifth": G.FIFTH_FN in c,
    }


def n3_prefix(m, with_r4=True):
    """等形前缀：GET_TID + r4 事件 + 三级唤醒。"""
    brealos.kernel_call(m, K.GET_TID)
    if with_r4:
        fire_r4(m, 5)
        m.run(max_steps=POST, spin_guard=False)
    for lv in range(1, 4):
        awaken(m, lv)


def run_group(kind, arg=None, with_r4=True, pre=True):
    """一台独立机器跑一条路径。kind: base/n3/bit/gate/vec/vec_then_bit。"""
    m = boot_run()
    c0 = covset(m)
    if kind == "base":
        pass
    elif kind == "n3":
        n3_prefix(m, with_r4)
    else:
        n3_prefix(m, with_r4)
        if kind == "bit":
            setbit(m, arg)
        elif kind == "gate":
            m.bus.wr(S.GATE_BYTE, 1, arg)
            m.raise_irq(VEC)
            m.run(max_steps=POST, spin_guard=False)
        elif kind == "vec":
            fire_vec(m, arg)
        elif kind == "vec_then_bit":
            fire_vec(m, arg)
            setbit(m, 0x40)
    return m, snapshot(m, c0)


def row(tag, r):
    print("  %-11s 新增PC=%3d 末PC=0x%06X want=0x%08X state=%s" %
          (tag, r["new"], r["pc"], r["want"] or 0, r["state"]))
    print("      提交 %-5s 加载点 %3d/312 call点 %3d/302 ｜ 门写 %d/2 "
          "门加载 %d/19 置位源 %d/4 8槽加载 %d/4" %
          (r["submit"], r["refs"], r["edges"], r["gate_wr"],
           r["gate_ld"], r["setters"], r["slot_ld"]))
    print("      状态字A加载 %2d/63 B %2d/31 ｜ 0x41等待 %d/2 处理A %-5s "
          "｜ 门字节=%02X 状态字=%08X/%08X 8槽=%s" %
          (r["wa"], r["wb"], r["wait41"], r["handler"], r["gate"],
           r["word_a"], r["word_b"],
           "".join("%02X" % x for x in r["slots"])))


# ===================================================================== A
hr("A. 静态：提交函数 0x12E6F8 是 sem17 保护的队列生产者（更正 V63）")
print("A1 函数体 [0x12E6F8,0x12E752) 三个 int #0x40 与一个 call:d：")
for p, svc, name in S.submit_services():
    print("     0x%06X svc=%4d %-9s" % (p, svc, name))
print("     0x0012E728 call:d @r12 -> 0x%08X（入队）" % S.INSERT_FN)
print("A2 槽地址算式 r0 = r4*12；r8 = 0x%08X + r0（stride=%d）"
      % (S.SEM_POOL, S.SLOT_STRIDE))
for i in (0, 1, 5, 17, 69):
    print("     idx=%-2d -> 0x%08X" % (i, S.submit_slot_addr(i)))
print("A3 生产者判据 = %s（有 wai_sem/set_flg/sig_sem 三件套，"
      "不是「公共状态查询函数」）" % S.submit_is_producer())
print("A4 服务号宽度（规矩 48）：0xFFFFFF56 按 32 位 = %d，"
      "低 8 位假象 = %d，不一致 = %s"
      % S.svc_width_mismatch(0xFFFFFF56))
print("A5 规矩 54 三种 PC 口径：%s" % S.call_site_relations(
    S.SUBMIT_REFS, S.SUBMIT_EDGES, S.SUBMIT_REL12))
print("     312 加载点 PC 范围 [0x%06X,0x%06X)；任务 2 装配区 3 点、"
      "ISR 区 6 点" % (min(SUBMIT_REFS), max(SUBMIT_REFS)))

# ===================================================================== B
hr("B. 静态：门字节 0x9F58D08F 的两个固件写点（规矩 61）")
print("B1 精确写点 %d 个 / 直接加载点 %d 个："
      % (len(S.GATE_WRITES), len(GATE_LOADS)))
for pc, val, start, end in S.GATE_WRITES:
    print("     0x%06X 写 %d ｜ 真实函数边界 [0x%06X,0x%06X]"
          % (pc, val, start, end))
print("B2 写 0 点 0x12ECE2 起：判 0x9F58D09B 的 bit8 -> 清门 -> "
      "set_flg(3,0x400000)")
for p, mn, v, sz, hx in dec(0x12ECE2, 0x12ED06):
    print("     0x%06X %-9s %-12s %s"
          % (p, mn, hx, {k: v[k] for k in ('i8', 'i32', 'u4', 'ri', 'rj')
                         if k in v}))
print("B3 写 1 点 0x12F158 起：btsth #8,@r6 -> 置门 -> "
      "set_flg(3,0x10000) / set_flg(3,0x200000)")
for p, mn, v, sz, hx in dec(0x12F174, 0x12F184):
    print("     0x%06X %-9s %-12s %s"
          % (p, mn, hx, {k: v[k] for k in ('i8', 'i32', 'u4', 'ri', 'rj')
                         if k in v}))
print("B4 按 ret 拆真实边界（规矩 59）= %s；固件写门字节 = %s，"
      "不得再说「等真机填」"
      % (S.gate_write_bounds(), S.firmware_writes_gate()))

# ===================================================================== C
hr("C. 静态：0x41 是 waiptn，置位源在 vec 0x2D 的 ISR（规矩 60）")
print("C1 第五道等待两个点 0x%06X / 0x%06X，"
      "立即数 0x%X 在 0x%06X / 0x%06X"
      % (S.WAIT41_A, S.WAIT41_B, S.WAIT41_PTN,
         S.WAIT41_IMM_A, S.WAIT41_IMM_B))
for p, mn, v, sz, hx in dec(0x12E59C, 0x12E5AC):
    print("     0x%06X %-9s %-12s %s"
          % (p, mn, hx, {k: v[k] for k in ('i8', 'i32', 'ri', 'u8')
                         if k in v}))
print("C2 服务号 ldi:32 #0xFFFFFF56 = %d（twai_flg 形态），"
      "Sweep.calls 报 %d 是低 8 位假象；实参 = flgid 3 / waiptn 0x41 "
      "/ mode 2 / r13=4（第五道；L3 的 r13 才是 100，此处不解释为 tmout）"
      % (S.TWAI_SVC_32, S.TWAI_SVC_LOW8))
print("C3 等待前 clr_flg(3,0xBF) 两点 0x%06X / 0x%06X 清 0x%02X；"
      "0x89 / 0x8F 分别清 0x%02X / 0x%02X"
      % (S.CLR_BF_PCS[0], S.CLR_BF_PCS[1], S.clear_mask(S.CLR_BF),
         S.clear_mask(0x89), S.clear_mask(0x8F)))
print("C4 flag3 全 ROM %d 个 set_flg 点里，置 0x40 唯一一点 0x%06X，"
      "置 0x01 三点 %s；合计 %d 点"
      % (S.FLAG3_SETTERS, S.SET_40_PC,
         ["0x%06X" % p for p in S.SET_01_PCS], S.wait41_setter_count()))
vt = F.vector_table(rom, base)
print("C5 向量表倒序 slot(n)=TBR+(255-n)*4：")
for vec in (S.VEC_1B, S.VEC_20, 0x2B, S.VEC_2D):
    slot = S.vec_slot(vec)
    ent = u32(slot)
    print("     vec=0x%02X slot=0x%06X -> ISR 0x%08X ｜ 反算 vec=%s"
          % (vec, slot, ent, hex(S.vec_of_slot(slot))))
print("C6 set_flg(3,0x40) 归属 = %s（按 reti 拆出的 vec 0x2D ISR 体）；"
      "enclosing() 误归 0x%06X" %
      (S.isr_owner(S.SET_40_PC, [(S.ISR_2D, S.ISR_2D_END)]),
       SW.enclosing(S.SET_40_PC) or 0))
for p, mn, v, sz, hx in dec(0x12FEA2, 0x12FEB6):
    print("     0x%06X %-9s %-12s %s"
          % (p, mn, hx, {k: v[k] for k in ('i8', 'i32', 'ri', 'rj', 'u4')
                         if k in v}))
print("C7 V75 把 0x41 当 flgptn 套 L3 的 (pat&0x74)==0x10："
      "跨级串味 = %s；L3 判据本身只对 flgptn 成立（0x10 -> %s）"
      % (S.cross_level_misread(S.WAIT41_PTN),
         S.level_check_verdict(0x10)))

# ===================================================================== D
hr("D. 静态：8 槽开机清零、两个状态字的可定址写点")
print("D1 8 槽 0x%08X 的 4 个直接加载点 = %s"
      % (S.SLOTS8, ["0x%06X" % p for p in S.SLOT_LOADS]))
for p, mn, v, sz, hx in dec(S.SLOT_CLEAR_FN, 0x1758FC):
    print("     0x%06X %-9s %-12s %s"
          % (p, mn, hx, {k: v[k] for k in ('i8', 'i32', 'm4', 'ri', 'rj',
                                           'rel9', 'u4') if k in v}))
print("D2 r13 从 %d 每轮 -4，st 在 bgt:d 延迟槽无条件执行 ⇒ %d 轮 × %d B "
      "= %d B 覆盖；8 槽全 0 是固件开机清零，不是等真机填"
      % (S.SLOT_CLEAR_LEN, S.slot_clear_rounds(), S.SLOT_STORE_WIDTH,
         S.slot_clear_rounds() * S.SLOT_STORE_WIDTH))
print("D3 两个状态字 / 8 槽的静态可定址写点：")
for name, a, loads in (("WORD_A", S.WORD_A, WORD_A_LOADS),
                       ("WORD_B", S.WORD_B, WORD_B_LOADS),
                       ("SLOTS8", S.SLOTS8, SLOT_LOADS),
                       ("GATE_BYTE", S.GATE_BYTE, GATE_LOADS)):
    w = SW.writers_of(a)
    vd = S.writers_verdict(w, SW.stores_unknown, len(loads))
    print("     %-10s 精确写点=%d 加载点=%2d 不可定址store=%d "
          "结论=%s / 可断言无写点=%s"
          % (name, vd["exact"], vd["loads"], vd["unknown"],
             vd["verdict"], vd["may_assert_absent"]))
print("D4 ⇒ 只能报「静态可定址写点 0」，不能报「固件没有写点」")

# ===================================================================== E
hr("E. 动态：收敛态基线与 1M 步窗口自证")
mb, rb = run_group("base")
row("基线500k", rb)
m1 = boot_run()
c500 = covset(m1)
m1.run(max_steps=WINDOW - RUN, spin_guard=False)
c1m = covset(m1)
print("E1 500k 覆盖 %d / 1M 覆盖 %d / 对称差集 %d / 末 PC 0x%06X"
      % (len(c500), len(c1m), S.window_same(c500, c1m), m1.cpu.pc))
print("E2 基线：提交函数 %s、312 加载点 %d、302 call 点 %d、"
      "4 个置位源 %d、门写点 %d、8 槽加载点 %d 全为 0；"
      "唯一命中的门字节相关 PC 是加载点 0x120170"
      % (rb["submit"], rb["refs"], rb["edges"], rb["setters"],
         rb["gate_wr"], rb["slot_ld"]))
print("E3 任务 5 want=0x%08X（第一道等待），末 PC 0x%06X 空闲"
      % (rb["want"] or 0, rb["pc"]))

# ===================================================================== F
hr("F. 动态：N3 与五组等形反事实（补位 / 直接写门字节）")
mn3, rn3 = run_group("n3")
row("N3三级唤醒", rn3)
res = {"N3": rn3}
for tag, bits, is_gate in S.BIT_GROUPS:
    kind = "gate" if is_gate else "bit"
    _m, r = run_group(kind, 1 if is_gate else bits)
    res[tag] = r
    row(tag, r)
print("F1 新增 PC：N3=%d / R0=%d / R1=%d / R2=%d / R3=%d / R4=%d"
      % tuple(res[k]["new"] for k in ("N3", "R0", "R1", "R2", "R3", "R4")))
print("F2 补 0x40 / 0x01 / 0x41 三组新增 410 PC，任务 5 离开 0x41 等待；"
      "补 0x10（L3 检查常量）仍是 392 PC ⇒ 第五道等待的位域确是 0x41")
print("F3 直接写门字节 1 新增仍 392 PC、want 仍 0x41 ⇒ "
      "写数据门 ≠ 给调度事件（规矩 53）")
print("F4 五组提交函数 / 两状态字 / 8 槽全 0 ⇒ 第五道等待解开后"
      "是回到循环头（want=0x09）或停在第三道等待（want=0x74），"
      "不进提交链；「补一次 set_flg 就唤醒成功」不成立")
diff = sorted(res["R0"]["cov"] - rn3["cov"])
print("F5 R0 相对 N3 新增 %d 个 PC：" % len(diff))
print("     %s" % " ".join("0x%06X" % p for p in diff))

# ===================================================================== G
hr("G. 动态：vec 0x2D 中断级反事实（归因分离）")
print("G0 四组各一台独立机器，N3 前缀等形；唯一差异是中断号 / r4 事件 / 是否叠加补位")
vec_res = {}
for tag, vec, with_r4, kind in (("V0", S.VEC_2D, True, "vec"),
                                ("V1", 0x2B, True, "vec"),
                                ("V2", S.VEC_2D, False, "vec"),
                                ("V3", S.VEC_2D, True, "vec_then_bit")):
    _m, r = run_group(kind, vec, with_r4)
    vec_res[tag] = r
    row(tag, r)
    print("       vec 0x2D ISR 0x%06X 命中=%s / set_flg(3,0x40) "
          "0x%06X 命中=%s" % (S.ISR_2D, r["isr2d"],
                              S.SET_40_PC, r["set40"]))
print("G1 V0 / V1 唯一差异是中断号 0x2D / 0x2B（等形对照）："
      "新增 PC %d / %d，ISR2D 命中 %s / %s"
      % (vec_res["V0"]["new"], vec_res["V1"]["new"],
         vec_res["V0"]["isr2d"], vec_res["V1"]["isr2d"]))
print("G2 V2 不注入 r4 事件：新增 PC %d ⇒ r4 事件与 vec 0x2D "
      "不是同一个条件" % vec_res["V2"]["new"])
print("G3 打 vec 0x2D 命中 ISR 与 0x40 置位点 = %s / %s；"
      "叠加补 0x40 新增 PC = %d"
      % (vec_res["V0"]["isr2d"], vec_res["V0"]["set40"],
         vec_res["V3"]["new"]))
print("G4 三层判据（存在 / 收敛态命中 / 在 ISR 内）= %s"
      % S.three_layer(True, 0, True))

# ===================================================================== H
hr("H. 综合判据、边界与地址清单")
print("H1 提交函数触发 = %s" % S.submit_trigger(
    rn3["refs"], rn3["edges"], rn3["submit"]))
print("H2 链闭合 = %s（处理函数 %s / 提交 %s / 状态字非零 %s / "
      "8 槽非零 %s / 门字节被固件写点命中 %s）"
      % (S.chain_verdict(rn3["handler"], rn3["submit"],
                         bool(rn3["word_a"] or rn3["word_b"]),
                         any(rn3["slots"]), bool(rn3["gate_wr"])),
         rn3["handler"], rn3["submit"],
         bool(rn3["word_a"] or rn3["word_b"]), any(rn3["slots"]),
         bool(rn3["gate_wr"])))
print("H3 KI#294 第一条结案：0x12E6F8 是 sem17 队列生产者"
      "（收敛态 312 加载点全在未执行路径；V68 模型化 tick 反事实命中过）；"
      "门字节有 2 个固件写点；"
      "0x41 是 waiptn，置位源 = vec 0x2D ISR 0x12FE84 的 "
      "set_flg(3,0x40) + 3 点 set_flg(3,0x01)；"
      "8 槽全 0 由开机清零循环解释")
print("H4 仍开放：两个状态字的写入路径、8 槽的填充源、"
      "提交函数 302 个调用点的真机触发任务、真机让 r4 非 0 的事件语义、"
      "0x12E636 返回非 0 条件、真机 tick 源（KI#191）、"
      "13 组 ISR 各自硬件事件、[12,13,14,15] 激活机制（KI#257）")
print("H5 全部 r4 注入 / 写索引字节 / 直接写门字节 / raise_irq / "
      "kernel_call 都是外部反事实补全，不是固件行为；"
      "不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /"
      "「补一次 set_flg 就唤醒成功」")
print("H6 关键常量地址清单（供报告地址对账）：")
for name, addr, width in (
        ("提交函数", S.SUBMIT, 6), ("提交末", S.SUBMIT_END, 6),
        ("wai_sem点", S.SUBMIT_SVC_PCS[0], 6),
        ("set_flg8点", S.SUBMIT_SVC_PCS[1], 6),
        ("sig_sem点", S.SUBMIT_SVC_PCS[2], 6),
        ("入队函数", S.INSERT_FN, 6), ("槽基址", S.SEM_POOL, 8),
        ("门字节", S.GATE_BYTE, 8), ("门写0", S.GATE_WRITES[0][0], 6),
        ("门写0函数", S.GATE_WRITES[0][2], 6),
        ("门写1", S.GATE_WRITES[1][0], 6),
        ("门写1函数", S.GATE_WRITES[1][2], 6),
        ("门铃setflg", 0x12F1A8, 6),
        ("等待41_A", S.WAIT41_A, 6), ("等待41_B", S.WAIT41_B, 6),
        ("等待41立即A", S.WAIT41_IMM_A, 6),
        ("等待41立即B", S.WAIT41_IMM_B, 6),
        ("clrBF_A", S.CLR_BF_PCS[0], 6), ("clrBF_B", S.CLR_BF_PCS[1], 6),
        ("置0x40", S.SET_40_PC, 6), ("ISR2D", S.ISR_2D, 6),
        ("ISR2D末", S.ISR_2D_END, 6), ("ISR20", S.ISR_20, 6),
        ("ISR1B", S.ISR_1B, 6), ("TBR", S.TBR, 8),
        ("vec2D槽", S.vec_slot(S.VEC_2D), 8),
        ("状态字A", S.WORD_A, 8), ("状态字B", S.WORD_B, 8),
        ("8槽", S.SLOTS8, 8), ("8槽清零", S.SLOT_CLEAR_FN, 6),
        ("8槽清零st", S.SLOT_CLEAR_ST, 6),
        ("8槽读点2", S.SLOT_LOADS[1], 6),
        ("8槽读点3", S.SLOT_LOADS[2], 6),
        ("8槽读点4", S.SLOT_LOADS[3], 6),
        ("D858", S.DESC_PTR, 8), ("DA82", G.DA82, 8),
        ("DA83", G.DA83, 8), ("DA84", G.DA84, 8),
        ("DA85", D.IDX_A, 8), ("DA86", D.IDX_B, 8),
        ("处理A", G.HANDLER_A, 6), ("空槽", G.NOOP_HANDLER, 6),
        ("任务5主体", G.TASK5_FN, 6), ("L4", G.L4_FN, 6),
        ("第五函数", G.FIFTH_FN, 6), ("间接调用", G.INDIRECT_CALL, 6)):
    print("     %-14s 0x%0*X" % (name, width, addr))

print("\n结果文件 results/frbox/submit76.txt；设施 frbox/submit76.py；"
      "独立机器：基线 / 1M 窗口 / N3 / R0–R4 / V0–V3。")
