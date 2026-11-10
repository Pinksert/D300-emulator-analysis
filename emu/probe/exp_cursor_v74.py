# -*- coding: utf-8 -*-
"""V74 正式探针：KI#286 第一条结案、24 项回调表语义与 D858 的 DA82 中转（A–H 八段）。

承接 V73。设备策略与 V65–V73 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；反事实段全部标注（r4 寄存器注入 / 写索引字节 /
raise_irq / kernel_call 都不是固件行为）。独立机器：基线 + 1M 窗口 +
K0–K3 四组 + P0–P5 六组 + L0–L4 五组，另有 L4 时序与 L2 对照。
"""
import sys
import os
import bisect

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, brealos,
                   cursor73 as K, cursor74 as D, level72 as C, wait271 as W)

BOOT, VEC, RUN, POST, WINDOW = (blive.BOOT_STEPS, 0x2B,
                                500000, 200000, 1000000)
# H 段逐点时序只需覆盖「唤醒后关键写点命中」的窗口；草稿实测 6 条日志
# 全部落在阶段 2/3 的前段，3 万步足够，避免 60 万次 Python 单步。
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


def nine(m):
    return [m.bus.rd(C.CUR_BASE + i, 1) for i in range(9)]


def fire_r4(m, vec, idx, r4val):
    """写索引 -> 触发中断 -> 单步到 ISR 入口在首条指令前设 r4。

    等价于「真机被中断那一刻 CPU 现场 r4=N」（反事实补全）。
    """
    m.bus.wr(K.IDX_A, 1, idx)
    m.raise_irq(vec)
    for _ in range(200):
        m.step(1)
        if m.cpu.pc == K.ISR_A:
            m.cpu.r[4] = r4val
            return True
    return False


def two_level(m, give_bytes=True):
    """V72 G1 路径：预热 + 一级 set_flg(3,9) + 二级 set_flg(3,0x10)。"""
    brealos.kernel_call(m, K.GET_TID)
    brealos.kernel_call(m, K.SET_FLG, r4=3, r5=W.WANTPTN_1)
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)
    if give_bytes:
        m.bus.wr(K.IDX_BYTE, 1, 0x00)
        m.bus.wr(K.VALID_BYTE, 1, 0x01)
    brealos.kernel_call(m, K.SET_FLG, r4=3, r5=K.BIT10)
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)


# ===================================================================== A
hr("A. 静态：24 项回调表全量语义（KI#286 第一条的上下文）")
print("A1 回调表 A 基址 0x001BC480 / 24 项 / 表 B 0x001BC490（第 4 项起）")
ALL_RET = [p for p, m, v, sz, hx in dec(0x12F740, 0x12FF00)
           if m in ('ret', 'ret:d')]
for i, (fn, bits, desc) in enumerate(D.CALLBACKS):
    j = bisect.bisect_right(ALL_RET, fn)
    hi = ALL_RET[j] if j < len(ALL_RET) else fn + 0x100
    body = dec(fn, hi + 2)
    svcs = [(c.svc, c.flgid, c.bits) for c in SW.calls
            if fn <= c.addr < hi and c.svc == -48]
    bare = '裸ret空槽' if body and body[0][1] in ('ret', 'ret:d') else ''
    print("  [%2d] 0x%06X -> 0x%08X [%0.6X,%0.6X) %2d条 置位=%-22s %s %s"
          % (i, D.cb_entry_addr(i), fn, fn, hi, len(body),
             D.cb_groupkey(bits), bare, desc))
print("A2 按置位值分组：%s" % D.cb_semantics())
print("A3 不置 flag、只写游标字节的回调 = %d 项" % D.cb_write_only_count())
print("A4 DA83 唯一写点 0x12F780 / DA84 三个写点 %s / DA85 9 点 / DA86 35 点"
      % ' '.join('0x%06X' % p for p in D.DA84_WRITERS))
print("A4b 对象池 0x9F58D500 的 +0x10 字节 0x9F58D510 只有 1 个写点 "
      "0x12E61C（KI#290：0x12E636 查询函数返回非 0 条件仍未建模）")

# ===================================================================== B
hr("B. 静态：r4 来源与 0x74 判据位域（KI#286 第二条更正）")
print("B1 派发器 A 0x12FE54 逐条：")
for p, m, v, sz, hx in dec(K.ISR_A, 0x12FE84):
    print("     0x%06X %-9s %-12s %s"
          % (p, m, hx, {k: v[k] for k in ('i8', 'i32', 'u8', 'ri', 'rj')
                        if k in v}))
print("B2 r4 口径：A = %s（stm0/stm1 只保存，call @r0 前未赋常量）；"
      "B = %s（0x12FDF0 mov r13,r4，r13 来自 0x12FDEE dmovb）"
      % (D.r4_source(D.VEC_A), D.r4_source(D.VEC_B)))
print("B3 判据 (pat & 0x74) == 0x10 位域真值表：")
for pat in (0x00, 0x10, 0x20, 0x40, 0x30, 0x50, 0x60, 0x74, 0x11, 0x90):
    print("     pat=0x%02X -> (pat&0x74)=0x%02X 判据=%-5s 干扰位=0x%02X"
          % (pat, pat & 0x74, D.mask_verdict(pat),
             D.interfering_bits(pat)))
print("B4 flag 3 置位源计数 = %s；存在 0x74 置位源 = %s"
      % (D.setter_count(), D.has_074_setter()))
print("     ⇒ KI#286 写「L3 所需 0x74 置位源」是把掩码当成要置的位（规矩 56）")
print("B5 置 0x10 的 4 点：%s"
      % ' '.join('0x%06X' % a for a in D.SETTERS_10))
print("     置 0x20 的 7 点：%s"
      % ' '.join('0x%06X' % a for a in D.SETTERS_20))
print("     置 0x40 的 1 点：%s"
      % ' '.join('0x%06X' % a for a in D.SETTERS_40))
print("B6 clr_flg(3,0x89) @0x12E196 清 0x76、clr_flg(3,0x8F) @0x12E34A 清 0x70"
      " ⇒ 每进第二 / 第三道等待先把 0x10/0x20/0x40 清掉")
print("B7 向量表实测（修正 V73 结果 C1 的手填错误）：")
for vec in (D.VEC_A, D.VEC_B, VEC):
    a = D.slot_of_vec(vec)
    print("     vec 0x%02X -> 槽 %3d @0x%06X -> ISR 0x%08X"
          % (vec, (a - D.TBR) // 4, a, u32(a)))

# ===================================================================== C
hr("C. 静态：D858 算式的 DA82 中转（更正 V72 口径）")
print("C1 0x12E1EC 起常量装载：r5=DA82 / r6=DA81 / r9=DA84 / r7=D858 / "
      "r3=DA83 / r10=0x9F58D97C / r11=0x9F58D08D")
for p, m, v, sz, hx in dec(0x12E230, 0x12E278):
    print("     0x%06X %-9s %-12s %s"
          % (p, m, hx, {k: v[k] for k in ('i8', 'i32', 'u8', 'ri', 'rj')
                        if k in v}))
print("C2 算式链：0x12E242 stb r0,@r5（装 DA82）→ 0x12E268 ldub @r5,r0 "
      "→ 0x12E270 lsl #4 → 0x12E274 st r0,@r7（写 D858）")
for v in (0, 1, 5, 0x46):
    print("     装载值 0x%02X -> DA82=0x%02X -> D858=0x%08X"
          % (v, D.da82_middle(v), D.desc_ptr_chain(v)))
print("C3 规矩 62：报「idx 是哪个字节」前先追中转寄存器与装载点；"
      "V72 记成 DA83 是只读常量表、没跟 stb @r5 那一步")

# ===================================================================== D
hr("D. 收敛态基线与 1M 步窗口自证")
m = boot_run()
c500 = covset(m)
nb = nine(m)
print("D1 末 PC 0x%06X / 覆盖 %d / 九字节 = %s"
      % (m.cpu.pc, len(c500), ' '.join('%02X' % x for x in nb)))
print("D2 DA82=0x%02X DA83=0x%02X DA84=0x%02X DA85=0x%02X DA86=0x%02X "
      "D858=0x%08X"
      % (m.bus.rd(D.DA82, 1), m.bus.rd(D.DA83, 1), m.bus.rd(D.DA84, 1),
         m.bus.rd(D.IDX_A, 1), m.bus.rd(D.IDX_B, 1),
         m.bus.rd(D.DESC_PTR, 4)))
m.run(max_steps=WINDOW - RUN, spin_guard=False)
c1m = covset(m)
print("D3 500k 覆盖 %d / 1M 覆盖 %d / 对称差集 %d / 末 PC 0x%06X"
      % (len(c500), len(c1m), D.window_same(c500, c1m), m.cpu.pc))

# ===================================================================== E
hr("E. 四组等形反事实：r4 是中断现场透传（KI#286 第一条结案）")
print("E0 四组各一台独立机器，BOOT/POST 等形；唯一差异是 ISR 入口的 r4 现场值")
res_k = {}
for tag, r4val, expect in D.R4_GROUPS:
    mm = boot_run()
    c0 = covset(mm)
    hit = fire_r4(mm, D.VEC_A, 5, r4val)
    mm.run(max_steps=POST, spin_guard=False)
    c1 = covset(mm)
    ti = bready.task_index(mm)
    t5 = ti.get(5)
    r = {"new": len(c1 - c0), "hit": hit,
         "cb": K.CB_FN in c1, "write": K.CB_WRITE in c1,
         "setflg": K.CB_SETFLG in c1,
         "da83": mm.bus.rd(D.DA83, 1), "da84": mm.bus.rd(D.DA84, 1),
         "d858": mm.bus.rd(D.DESC_PTR, 4),
         "l1": C.WAIT2_FN in c1, "l2": C.DISPATCH_FN in c1,
         "want": mm.bus.rd(t5 + 0x14, 4) if t5 is not None else None,
         "shape": (BOOT, 1, POST)}
    res_k[tag] = r
    print("  %s r4=0x%02X 期望DA83=0x%02X 实测=0x%02X %s ｜ 新增PC=%3d "
          "回调 %s 写DA83 %s setflg %s"
          % (tag, r4val, expect, r["da83"],
             "一致" if r["da83"] == expect else "不一致",
             r["new"], r["cb"], r["write"], r["setflg"]))
    print("       DA84=%02X D858=%08X ｜ L1 %s L2派发 %s ｜ 任务5 want=0x%08X"
          % (r["da84"], r["d858"], r["l1"], r["l2"], r["want"] or 0))
print("E1 等形校验 %s" % D.equal_shape([res_k[t]["shape"]
                                       for t, *_ in D.R4_GROUPS])["ok"])
print("E2 DA83 非 0 的组 = %s；新增 PC 全为 31 = %s"
      % (D.attribution(res_k, "da83"),
         len({res_k[t]["new"] for t in res_k}) == 1))
print("E3 结案：KI#286 第一条「回调参数 r4 来源」= 中断发生时的 CPU 现场，"
      "派发器 A 不赋值；真机需由被中断任务此刻的 r4（或派发器 B 的 "
      "mov r13,r4 分支）给出非 0 索引")

# ===================================================================== F
hr("F. 六组判据位域反事实：只有单独 0x10 能跨到 L2 派发")
print("F0 六组各一台独立机器，两级唤醒等形，唯一差异是二级 set_flg 的 bits")
for tag, bits, exp_new, exp_l2 in D.BIT_GROUPS:
    mm = boot_run()
    c0 = covset(mm)
    brealos.kernel_call(mm, K.GET_TID)
    brealos.kernel_call(mm, K.SET_FLG, r4=3, r5=W.WANTPTN_1)
    mm.raise_irq(VEC)
    mm.run(max_steps=POST, spin_guard=False)
    c1 = covset(mm)
    brealos.kernel_call(mm, K.SET_FLG, r4=3, r5=bits)
    mm.raise_irq(VEC)
    mm.run(max_steps=POST, spin_guard=False)
    c2 = covset(mm)
    ti = bready.task_index(mm)
    t5 = ti.get(5)
    l2 = C.DISPATCH_FN in c2
    print("  %s bits=0x%02X (pat&0x74)=0x%02X 判据=%-5s ｜ "
          "一级新增 %3d 二级新增 %3d（期望 %3d %s）｜ L2派发 %-5s（期望 %-5s）"
          " ｜ want=0x%08X"
          % (tag, bits, bits & 0x74, D.mask_verdict(bits),
             len(c1 - c0), len(c2 - c1), exp_new,
             "一致" if len(c2 - c1) == exp_new else "不一致",
             l2, exp_l2,
             mm.bus.rd(t5 + 0x14, 4) if t5 is not None else 0))
print("F1 干扰位：0x20/0x40 任一置起 ⇒ 判据必失败；"
      "24 项回调里 [17] 同时置 0x10+0x20 ⇒ 该事件反而阻断 L2")

# ===================================================================== G
hr("G. 五组组合反事实：L4 首次由固件自己的事件链算出 D858")
print("G0 五组各一台独立机器，唯一差异是「注入 r4 事件 / 两级唤醒 / 外部写字节」")
res_l = {}
for tag, do_r4, do_two, give_bytes in D.COMBO_GROUPS:
    mm = boot_run()
    c0 = covset(mm)
    if do_r4:
        fire_r4(mm, D.VEC_A, 5, 5)
        mm.run(max_steps=POST, spin_guard=False)
    if do_two:
        two_level(mm, give_bytes)
    else:
        mm.run(max_steps=POST, spin_guard=False)
    c1 = covset(mm)
    ti = bready.task_index(mm)
    t5 = ti.get(5)
    r = {"new": len(c1 - c0),
         "da82": mm.bus.rd(D.DA82, 1), "da83": mm.bus.rd(D.DA83, 1),
         "da84": mm.bus.rd(D.DA84, 1),
         "d858": mm.bus.rd(D.DESC_PTR, 4),
         "cb": K.CB_FN in c1, "l2": C.DISPATCH_FN in c1,
         "bne2": W.LEVELS[1][3] in c1, "l3": C.WAIT3_FN in c1,
         "w158": 0x12E158 in c1,
         "want": mm.bus.rd(t5 + 0x14, 4) if t5 is not None else None}
    res_l[tag] = r
    print("  %s r4=%-5s 两级唤醒=%-5s 外部写字节=%-5s 新增PC=%3d ｜ "
          "DA82=%02X DA83=%02X DA84=%02X D858=%08X"
          % (tag, do_r4, do_two, give_bytes, r["new"],
             r["da82"], r["da83"], r["da84"], r["d858"]))
    print("       回调 %-5s L2派发 %-5s L2bne %-5s L3等待 %-5s ｜ "
          "0x12E158 %-5s ｜ want=0x%08X"
          % (r["cb"], r["l2"], r["bne2"], r["l3"], r["w158"],
             r["want"] or 0))
print("G1 DA83 非 0 的组 = %s" % [t for t in res_l if res_l[t]["da83"]])
print("G2 L2 派发命中的组 = %s" % D.attribution(res_l, "l2"))
print("G3 D858 非 0 的组 = %s" % [t for t in res_l if res_l[t]["d858"]])
print("G4 L4 未外部写 DA82/DA83/DA84，仍得 D858=0x%08X "
      "（= 0x001BC020 + DA82 0x%02X × 0x10）"
      % (res_l["L4"]["d858"], res_l["L4"]["da82"]))

# ===================================================================== H
hr("H. L4 逐点时序、综合判据与边界")
WATCH = (0x12E158, D.LOAD_DA82, D.READ_DA82, D.SHIFT_DA82, D.WRITE_D858,
         K.CB_WRITE)


def watch_run(mm, steps, log, phase):
    for _ in range(steps):
        pc = mm.cpu.pc
        mm.step(1)
        if pc in WATCH:
            log.append((phase, pc, mm.cpu.r[0] & 0xFF,
                        mm.bus.rd(D.DA82, 1), mm.bus.rd(D.DA83, 1),
                        mm.bus.rd(D.DA84, 1), mm.bus.rd(D.DESC_PTR, 4)))
        if mm.cpu.halted:
            break


mm = boot_run()
log = []
fire_r4(mm, D.VEC_A, 5, 5)
watch_run(mm, WATCH_STEPS, log, "阶段1")
brealos.kernel_call(mm, K.GET_TID)
brealos.kernel_call(mm, K.SET_FLG, r4=3, r5=W.WANTPTN_1)
mm.raise_irq(VEC)
watch_run(mm, WATCH_STEPS, log, "阶段2")
brealos.kernel_call(mm, K.SET_FLG, r4=3, r5=K.BIT10)
mm.raise_irq(VEC)
watch_run(mm, WATCH_STEPS, log, "阶段3")
print("H1 L4 路径关键 PC 时序（阶段 / PC / r0 / DA82 / DA83 / DA84 / D858）：")
for ph, pc, r0, v82, v83, v84, d858 in log:
    print("     %-6s 0x%06X r0=%02X DA82=%02X DA83=%02X DA84=%02X D858=%08X"
          % (ph, pc, r0, v82, v83, v84, d858))
print("H2 末态 DA82=0x%02X DA83=0x%02X DA84=0x%02X D858=0x%08X"
      % (mm.bus.rd(D.DA82, 1), mm.bus.rd(D.DA83, 1),
         mm.bus.rd(D.DA84, 1), mm.bus.rd(D.DESC_PTR, 4)))
print("H3 链闭合 = %s（L2 命中 True / D858 已填 True / 处理函数 0/44 False）"
      % D.chain_verdict(res_l["L4"]["l2"], bool(res_l["L4"]["d858"]), False))
print("H4 仍缺：真机给 r4 非 0 索引的事件语义（哪个任务 / 哪个时刻 r4 非 0）、"
      "0x12E636 查询函数返回非 0 条件、0x12E412(L4) 与 0x12E674 前置条件、"
      "真机 tick 源（KI#191）、两个状态字 0x9F5A0284 / 0x9F59EF50 与 "
      "8 槽 0x9F59F7C8 填充源")
print("H5 全部 r4 注入 / 写索引字节 / raise_irq / kernel_call 都是外部反事实"
      "补全，不是固件行为；不能报显示链通了 / 出画面 / 系统跑起来了 / "
      "真机如此 /「补一次 set_flg 就唤醒成功」")
print("H6 关键常量地址清单（供报告地址对账）：")
for name, addr, width in (
        ("TBR", D.TBR, 8), ("槽223", D.slot_of_vec(D.VEC_A), 8),
        ("槽228", D.slot_of_vec(D.VEC_B), 8), ("槽212", D.slot_of_vec(VEC), 8),
        ("回调表A", D.CB_TABLE_A, 8), ("回调表B", D.CB_TABLE_B, 8),
        ("表项0x1BC494", 0x1BC494, 8), ("描述符表", D.DESC_TABLE, 8),
        ("ISR_A", D.ISR_A, 6), ("ISR_B", D.ISR_B, 6),
        ("派发call_A", 0x12FE70, 6), ("派发call_B", 0x12FE04, 6),
        ("回调入口", D.CB_FN, 6), ("写DA83", D.CB_WRITE, 6),
        ("setflg", D.CB_SETFLG, 6), ("mov_r13_r4", 0x12FDF0, 6),
        ("装DA82", D.LOAD_DA82, 6), ("读DA82", D.READ_DA82, 6),
        ("移DA82", D.SHIFT_DA82, 6), ("写D858", D.WRITE_D858, 6),
        ("第一道等待", D.WAIT1_FN, 6), ("第二道等待", D.WAIT2_FN, 6),
        ("L2派发", D.L2_FN, 6), ("L3等待", D.L3_FN, 6),
        ("L4", C.L4_FN, 6), ("第五call", C.FIFTH_FN, 6),
        ("前置门", C.GATE_PC, 6), ("间接调用", C.INDIRECT_CALL, 6),
        ("提交函数", C.SUBMIT, 6), ("空槽位", C.NOOP_HANDLER, 6),
        ("查询函数", D.QUERY_FN, 6), ("任务5循环头", C.LOOP_HEAD, 6),
        ("写DA84_1", 0x12E112, 6), ("写DA84_2", 0x12E140, 6),
        ("写DA84_3", 0x12E158, 6), ("清位0x89", 0x12E196, 6),
        ("清位0x8F", 0x12E34A, 6),
        ("置0x10_1", 0x12F794, 6), ("置0x10_2", 0x12F964, 6),
        ("置0x10_3", 0x12FB10, 6), ("置0x10_4", 0x12FDB0, 6),
        ("置0x20_1", 0x12F882, 6), ("置0x20_2", 0x12FB1C, 6),
        ("置0x20_3", 0x12FB54, 6), ("置0x20_4", 0x12FC4C, 6),
        ("置0x20_5", 0x12FC84, 6), ("置0x20_6", 0x12FD98, 6),
        ("置0x20_7", 0x12FDE8, 6), ("置0x40", 0x12FEAC, 6),
        ("回调6", 0x12F798, 6), ("回调7", 0x12F7DC, 6),
        ("回调8", 0x12F82C, 6), ("回调17", 0x12FAEA, 6),
        ("回调20", 0x12FBC6, 6), ("回调23", 0x12FCF6, 6),
        ("九字节", D.CUR_BASE, 8), ("DA7C", 0x9F58DA7C, 8),
        ("DA7D", 0x9F58DA7D, 8), ("DA7E", 0x9F58DA7E, 8),
        ("DA7F", 0x9F58DA7F, 8), ("DA80", 0x9F58DA80, 8),
        ("DA81", 0x9F58DA81, 8), ("DA82", D.DA82, 8),
        ("DA83", D.DA83, 8), ("DA84", D.DA84, 8),
        ("DA85", D.IDX_A, 8), ("DA86", D.IDX_B, 8),
        ("D850", 0x9F58D850, 8), ("D854", 0x9F58D854, 8),
        ("D858", D.DESC_PTR, 8), ("D87B", 0x9F58D87B, 8),
        ("D87C", 0x9F58D87C, 8), ("对象池", D.SEM_POOL, 8),
        ("门字节", 0x9F58D08F, 8), ("状态字A", C.WORD_A, 8),
        ("状态字B", C.WORD_B, 8), ("8槽", C.SLOTS8, 8),
        ("L2bne目标", W.LEVELS[1][3], 6)):
    print("     %-14s 0x%0*X" % (name, width, addr))

print("\n结果文件 results/frbox/cursor74.txt；设施 frbox/cursor74.py；"
      "独立机器：基线 / 1M 窗口 / K0–K3 / P0–P5 / L0–L4 / L4 时序。")
