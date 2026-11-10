# -*- coding: utf-8 -*-
"""V73 正式探针：KI#282 第一条前提证伪与真机事件链定位（A–H 八段）。

承接 V72。设备策略与 V65–V72 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；反事实段全部标注（写索引字节 / raise_irq /
kernel_call 都不是固件行为）。七台独立机器：基线 + 1M 步窗口 +
六组等形反事实各一台，另有 V72 复现对照一台。
"""
import sys
import os
import bisect

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, brealos,
                   level72 as C, wait271 as W, cursor73 as K)

BOOT, VEC, RUN, POST, WINDOW = (blive.BOOT_STEPS, 0x2B,
                                500000, 200000, 1000000)

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


def nine(m):
    return [m.bus.rd(C.CUR_BASE + i, 1) for i in range(9)]


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


# ===================================================================== A
hr("A. 静态：DA83 / DA84 的固件写入源（KI#282 第一条前提检查）")
print("A1 ROM 全量可定址 store %d 条 / 形态不可定址 %d 条 / enter %d 个"
      % (len(SW.stores), SW.stores_unknown, len(SW.funcs)))
for name, a in (("DA83 索引", C.IDX_BYTE), ("DA84 有效项", C.VALID_BYTE),
                ("DA82 清位", C.CLEAR_BYTE), ("D858 描述符指针", C.DESC_PTR)):
    w = SW.writers_of(a)
    print("A2 %-14s 0x%08X 写点 %2d 个：%s"
          % (name, a, len(w),
             ' '.join('0x%06X(%s)' % (pc, mn) for pc, mn in w)))
print("A3 九字节 DA7C..DA84 逐字节写点数：")
for i in range(9):
    a = C.CUR_BASE + i
    print("     DA%02X %2d 点" % ((C.CUR_BASE & 0xFF) + i,
                                  len(SW.writers_of(a))))
print("A4 描述符表 %d 条 × 0x%X，表尾 0x%06X；回调表 A 基址 0x%06X"
      % (C.DESC_COUNT, C.DESC_STRIDE,
         C.DESC_TABLE + C.DESC_COUNT * C.DESC_STRIDE, K.CB_TABLE_A))
print("A5 结论：DA84 有 %d 个固件写点，不是「等真机填」；"
      "DA83 只有 %d 个写点，落在 0x12F77A"
      % (len(SW.writers_of(C.VALID_BYTE)),
         len(SW.writers_of(C.IDX_BYTE))))

# ===================================================================== B
hr("B. 静态：第一道等待函数 0x12E0F8 与 DA84 的写入值")
print("B1 真实函数边界 [0x12E0FC(enter),0x12E15E(ret:d)]，"
      "唯一静态直接调用边 = 0x12DFBA call rel12 -> 0x12E0F8")
for pc, kind, slot in K.DA84_WRITERS:
    print("B2 DA84 写点 0x%06X 值来源=%-10s 在 bne:d 延迟槽=%s"
          % (pc, kind, slot))
print("B3 0x12E106 ldi:32 #DA84,r8 → 0x12E10C cmp r0,#0 / "
      "0x12E10E beq 0x12E12A；0x12E110 ldi:8 #0,r0 / 0x12E112 stb")
print("B4 0x12E138 call rel12 -> 0x12E636；0x12E13A mov r0,r4 / "
      "0x12E13C cmp r0,#0 / 0x12E13E bne:d -> 0x12E15A；"
      "延迟槽 0x12E140 stb r0,@r8 写 DA84")
print("B5 0x12E154 call:d -> 0x12E636，0x12E158 stb r4,@r8 再写 DA84")
print("B6 值来源分类 = %s（规矩 58 推广：写点存在 != 值非 0）"
      % K.da84_writers_kinds())
print("B7 决定值的查询函数 0x12E636：wai_sem(17) / 读对象池 0x9F58D500，"
      "返回 0x9F58D500+8 那个字节；收敛态返回 0 ⇒ DA84=0")

# ===================================================================== C
hr("C. 静态：真机事件链定位（向量表倒序 / 规矩 60）")
print("C1 向量表倒序映射 vector(n) = TBR + (255-n)*4；TBR=0x000EFC00")
for vec in (K.VEC_A, K.VEC_B, VEC):
    slot_addr = K.slot_of_vec(vec)
    print("     vec 0x%02X -> 槽 %3d @0x%06X -> ISR 0x%08X（向量表实测）"
          % (vec, (slot_addr - K.TBR) // 4, slot_addr, u32(slot_addr)))
print("C2 槽 223 实测字 = 0x%08X / 槽 228 实测字 = 0x%08X"
      % (u32(K.slot_of_vec(K.VEC_A)), u32(K.slot_of_vec(K.VEC_B))))
print("C3 派发器 A 0x12FE54：0x12FE5E ldi:32 #0x9F58DA85,r12 / "
      "0x12FE64 ldub @r12,r0 / 0x12FE66 lsl r0,#2 / "
      "0x12FE68 ldi:32 #0x001BC480,r13 / 0x12FE6E ld @(r0,r13),r0 / "
      "0x12FE70 call @r0 / 0x12FE82 reti")
print("C4 派发器 B 0x12FDB4：0x12FDF2 ldi:32 #0x9F58DA86,r12 / "
      "0x12FDF8 ldub @r12,r0 / 0x12FDFA lsl r0,#2 / "
      "0x12FDFC ldi:32 #0x001BC490,r13 / 0x12FE02 ld @(r0,r13),r0 / "
      "0x12FE04 call @r0 / 0x12FE10 reti")
print("C5 回调表 A（0x1BC480，24 项）关键项：")
for i in (0, 1, 4, 5, 6, 20, 23):
    a = K.cb_entry_addr(K.CB_TABLE_A, i)
    print("     [%2d] 0x%06X -> 0x%08X" % (i, a, u32(a)))
print("C6 回调 0x12F77A 在表 A 索引 = %d，在表 B 索引 = %d"
      % (K.cb_index_of(0x1BC494, K.CB_TABLE_A),
         K.cb_index_of(0x1BC494, K.CB_TABLE_B)))
print("C7 回调 0x12F77A 函数体逐条（真实边界 [0x12F77A,0x12F796)）：")
for p, mn, v, sz, hx in dec(K.CB_FN, K.CB_RET + 2):
    ex = {k: v[k] for k in ('i8', 'i32', 'u8', 'ri', 'rj') if k in v}
    print("     0x%06X %-8s %-12s %s" % (p, mn, hx, ex))
print("C7b 0x12F782 ldi:8 #0,r0 / 0x12F784 ldi:32 #0x9F58DA86,r12 / "
      "0x12F78C ldi:8 #3,r4 / 0x12F78E ldi:8 #0x10,r5 / "
      "0x12F790 ldi:8 #0xD0,r12 (extsb=-48) / 0x12F794 int #0x40")
print("C7c L2 bne 目标 0x12DFE0；V72 复现的 D858 = 0x001BC030")
print("C8 事件链 = %s"
      % K.event_chain(K.VEC_A, 5, K.CB_TABLE_A, K.CB_FN))

# ===================================================================== D
hr("D. 静态：规矩 59 归属更正（V71 四个置位点）")
rets = [p for p, m, v, sz, hx in dec(0x12F540, 0x12FE00)
        if m in ('ret', 'ret:d')]
print("D1 [0x12F540,0x12FE00) 内 ret %d 条；"
      "V71 报「4 点全在 0x12F540 ISR 体」是 enclosing() 归属假象" % len(rets))
for pc, lo, hi in K.SETFLG_POINTS:
    real_lo, real_hi = K.real_bounds(pc, rets, 0x12F540)
    print("D2 set_flg 点 0x%06X enclosing=0x%06X 真实区间="
          "[0x%06X,0x%06X) 声明区间=[0x%06X,0x%06X) 一致=%s"
          % (pc, K.SETFLG_ENCLOSING, real_lo, real_hi or 0, lo, hi,
             real_lo == lo))
print("D3 DA83 写点 0x12F780 的真实函数起点 = 0x%06X；"
      "归属不一致 = %s"
      % (K.real_bounds(K.CB_WRITE, rets, 0x12F540)[0],
         K.enclosing_mismatch(K.CB_WRITE, K.SETFLG_ENCLOSING,
                              rets, 0x12F540)))
print("D4 DA86 全 ROM 写点 %d 个（不只是 4 个回调清它）"
      % len(SW.writers_of(K.IDX_B)))
print("D5 G 段实验错误留痕：曾用 vec 0x2B 打事件链，"
      "四组全 0 命中；规矩 60 要求按倒序换算到 0x20 / 0x1B")

# ===================================================================== E
hr("E. 收敛态：DA84 写点已命中，九字节全 0 不是没人写")
m = boot_run()
c500 = covset(m)
nb = nine(m)
print("E1 末 PC 0x%06X / 覆盖 %d / 九字节 = %s"
      % (m.cpu.pc, len(c500), ' '.join('%02X' % x for x in nb)))
print("E2 D87B=0x%02X DA85=0x%02X DA86=0x%02X D858=0x%08X"
      % (m.bus.rd(K.D87B, 1), m.bus.rd(K.IDX_A, 1),
         m.bus.rd(K.IDX_B, 1), m.bus.rd(C.DESC_PTR, 4)))
for name, pc in (("第一道等待 0x12E0FC", 0x12E0FC),
                 ("DA84 写点 0x12E112", 0x12E112),
                 ("DA84 写点 0x12E140", 0x12E140),
                 ("DA84 写点 0x12E158", 0x12E158),
                 ("查询函数 0x12E636", K.QUERY_FN),
                 ("DA83 写点 0x12F780", K.CB_WRITE),
                 ("回调 0x12F77A", K.CB_FN)):
    print("     %-26s 命中=%s" % (name, pc in c500))
print("E3 固件写过 DA84 = %s（至少一个写点命中）"
      % K.da84_is_firmware_written(c500))
ti = bready.task_index(m)
t5 = ti.get(5)
print("E4 任务 5 TCB=0x%08X state=0x%02X want=0x%08X reason=%d"
      % (t5, bready.state_of(m, t5), m.bus.rd(t5 + 0x14, 4),
         W.sx(m.bus.rd(t5 + 0x20, 2), 16)))

# ===================================================================== F
hr("F. 1M 步窗口自证（防执行窗口假象）")
m.run(max_steps=WINDOW - RUN, spin_guard=False)
c1m = covset(m)
print("F1 500k 覆盖 %d / 1M 覆盖 %d / 对称差集 %d"
      % (len(c500), len(c1m), K.window_same(c500, c1m)))
print("F2 1M 步后 DA83=0x%02X DA84=0x%02X D858=0x%08X / 末 PC 0x%06X"
      % (m.bus.rd(C.IDX_BYTE, 1), m.bus.rd(C.VALID_BYTE, 1),
         m.bus.rd(C.DESC_PTR, 4), m.cpu.pc))

# ===================================================================== G
hr("G. 六组等形反事实：索引字节与中断事件归因分离")
print("G0 六组各一台独立机器，BOOT/POST 等形；"
      "唯一差异是「索引字节」与「中断向量」（均为外部反事实补全）")
res = {}
for tag, ia, iv, vec, desc in K.GROUPS:
    mm = boot_run()
    c0 = covset(mm)
    if ia is not None:
        mm.bus.wr(ia, 1, iv)
    n_irq = 0
    if vec is not None:
        mm.raise_irq(vec)
        n_irq += 1
    mm.run(max_steps=POST, spin_guard=False)
    c1 = covset(mm)
    ti = bready.task_index(mm)
    t5b = ti.get(5)
    r = {
        "new": len(c1 - c0),
        "isr_a": K.ISR_A in c1, "isr_b": K.ISR_B in c1,
        "disp_call": K.DISPATCH_CALL_A in c1,
        "cb": K.CB_FN in c1, "cb_write": K.CB_WRITE in c1,
        "cb_setflg": K.CB_SETFLG in c1,
        "da83": mm.bus.rd(C.IDX_BYTE, 1),
        "da84": mm.bus.rd(C.VALID_BYTE, 1),
        "da85": mm.bus.rd(K.IDX_A, 1), "da86": mm.bus.rd(K.IDX_B, 1),
        "d858": mm.bus.rd(C.DESC_PTR, 4),
        "w1": C.WAIT1_FN in c1, "l2": C.DISPATCH_FN in c1,
        "bne2": W.LEVELS[1][3] in c1,
        "state": bready.state_of(mm, t5b) if t5b is not None else None,
        "want": mm.bus.rd(t5b + 0x14, 4) if t5b is not None else None,
        "shape": (BOOT, n_irq, POST),
    }
    res[tag] = r
    print("  %s %-30s 新增PC=%3d ｜ ISR_A %-5s ISR_B %-5s 派发call %-5s "
          "回调 %-5s 写DA83 %-5s setflg %-5s"
          % (tag, desc, r["new"], r["isr_a"], r["isr_b"], r["disp_call"],
             r["cb"], r["cb_write"], r["cb_setflg"]))
    print("       DA83=%02X DA84=%02X DA85=%02X DA86=%02X D858=%08X ｜ "
          "第一道 %s L2派发 %s L2bne %s ｜ 任务5 state=%s want=0x%08X"
          % (r["da83"], r["da84"], r["da85"], r["da86"], r["d858"],
             r["w1"], r["l2"], r["bne2"], r["state"], r["want"] or 0))
es = K.equal_shape([res[t]["shape"] for t, *_ in K.GROUPS])
print("G1 等形校验 %s；逐项 %s" % (es["ok"], es["keys"]))
print("G2 ISR 派发器命中组 = %s"
      % K.attribution(res, "isr_a"))
print("G3 回调命中组 = %s；写 DA83 点命中组 = %s；set_flg 点命中组 = %s"
      % (K.attribution(res, "cb"), K.attribution(res, "cb_write"),
         K.attribution(res, "cb_setflg")))
print("G4 DA83 被写成非 0 的组 = %s（回调写的是 r4，此上下文 r4=0）"
      % [t for t in res if res[t]["da83"] != 0])
print("G5 J1 只给索引不打中断新增 %d PC ⇒ 写数据门 ≠ 给调度事件（规矩 53）"
      % res["J1"]["new"])
print("G6 J5 错配 vec 0x2B 新增 %d PC ⇒ 向量号必须按倒序换算（规矩 60）"
      % res["J5"]["new"])
print("G7 J2 任务 5 want=0x%08X（K.wants_after_event 预测 0x%08X）"
      % (res["J2"]["want"] or 0,
         K.wants_after_event(9, res["J2"]["isr_a"])))

# ===================================================================== H
hr("H. 综合判据与边界")
print("H1 V72 两级唤醒原样复现（对照：DA84 是否被固件自己改写）")
m2 = boot_run()
c0 = covset(m2)
brealos.kernel_call(m2, K.GET_TID)
brealos.kernel_call(m2, K.SET_FLG, r4=K.FLAG_ID, r5=K.WANTPTN_1)
m2.raise_irq(VEC)
m2.run(max_steps=POST, spin_guard=False)
c1 = covset(m2)
m2.bus.wr(C.IDX_BYTE, 1, 0x00)
m2.bus.wr(C.VALID_BYTE, 1, 0x01)
brealos.kernel_call(m2, K.SET_FLG, r4=K.FLAG_ID, r5=K.BIT10)
m2.raise_irq(VEC)
m2.run(max_steps=POST, spin_guard=False)
c2 = covset(m2)
print("     一级新增 %d / 二级新增 %d / 总覆盖 %d"
      % (len(c1 - c0), len(c2 - c1), len(c2)))
print("     末 DA83=%02X DA84=%02X D858=%08X ｜ L2派发 %s"
      % (m2.bus.rd(C.IDX_BYTE, 1), m2.bus.rd(C.VALID_BYTE, 1),
         m2.bus.rd(C.DESC_PTR, 4), C.DISPATCH_FN in c2))
print("     DA84 写点命中：0x12E112 %s / 0x12E140 %s / 0x12E158 %s"
      % (0x12E112 in c2, 0x12E140 in c2, 0x12E158 in c2))
print("H2 三层判据：DA83 写点 %s"
      % K.three_layer(True, res["J0"]["cb_write"], True))
print("H3 链闭合 = %s（J3 回调命中但 L2 未跨、处理函数 0/44）"
      % K.chain_verdict(res["J3"]["cb"], res["J3"]["l2"], False,
                        res["J3"]["d858"]))
print("H4 KI#282 第一条更正：DA84 不是等真机填，固件自己在 0x12E0F8 写；"
      "真机事件链已定位到 vec 0x20 / 0x1B 两个 ISR 派发器与"
      "0x1BC480 回调表，驱动的是 DA83 与 flag 3")
print("H5 仍缺：回调参数 r4 的来源（真机给 DA83 填非 0 索引的事件语义）、"
      "L3 所需 0x74 置位源、0x12E412(L4) 与 0x12E674 前置条件、"
      "真机 tick 源（KI#191）、两个状态字与 8 槽填充源")
print("H6 全部反事实（写索引字节 / raise_irq / kernel_call）都是外部补全，"
      "不是固件行为；不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / "
      "「补一次 set_flg 就唤醒成功」")
print("H7 关键常量地址清单（供报告地址对账）：")
for name, addr, width in (
        ("TBR", K.TBR, 8), ("槽223", K.slot_of_vec(K.VEC_A), 8),
        ("槽228", K.slot_of_vec(K.VEC_B), 8),
        ("回调表A", K.CB_TABLE_A, 8), ("回调表B", K.CB_TABLE_B, 8),
        ("表项0x1BC494", 0x1BC494, 8), ("描述符表", C.DESC_TABLE, 8),
        ("ISR_A", K.ISR_A, 6), ("ISR_B", K.ISR_B, 6),
        ("派发call_A", K.DISPATCH_CALL_A, 6),
        ("派发call_B", K.DISPATCH_CALL_B, 6),
        ("回调入口", K.CB_FN, 6), ("写DA83", K.CB_WRITE, 6),
        ("清DA86", K.CB_CLEAR, 6), ("setflg", K.CB_SETFLG, 6),
        ("回调ret", K.CB_RET, 6),
        ("第一道等待", K.WAIT1_FN, 6), ("等待enter", K.WAIT1_ENTER, 6),
        ("写DA84_1", 0x12E112, 6), ("写DA84_2", 0x12E140, 6),
        ("写DA84_3", 0x12E158, 6), ("查询函数", K.QUERY_FN, 6),
        ("任务5循环头", C.LOOP_HEAD, 6),
        ("L2派发", C.DISPATCH_FN, 6), ("L3等待", C.WAIT3_FN, 6),
        ("L4", C.L4_FN, 6), ("第五call", C.FIFTH_FN, 6),
        ("前置门", C.GATE_PC, 6), ("间接调用", C.INDIRECT_CALL, 6),
        ("提交函数", C.SUBMIT, 6), ("空槽位", C.NOOP_HANDLER, 6),
        ("失败等待", 0x12E56A, 6), ("L3等待点", 0x12E3F6, 6),
        ("置位点1", 0x12F794, 6), ("置位点2", 0x12F964, 6),
        ("置位点3", 0x12FB10, 6), ("置位点4", 0x12FDB0, 6),
        ("归属错值", K.SETFLG_ENCLOSING, 6),
        ("九字节", C.CUR_BASE, 8), ("DA7D", 0x9F58DA7D, 8),
        ("DA7F", 0x9F58DA7F, 8), ("DA82", C.CLEAR_BYTE, 8),
        ("DA83", C.IDX_BYTE, 8), ("DA84", C.VALID_BYTE, 8),
        ("DA85", K.IDX_A, 8), ("DA86", K.IDX_B, 8),
        ("D87B", K.D87B, 8), ("D858", C.DESC_PTR, 8),
        ("D850", 0x9F58D850, 8), ("D854", 0x9F58D854, 8),
        ("D87C", 0x9F58D87C, 8), ("对象池", K.SEMPOOL, 8),
        ("门字节", 0x9F58D08F, 8), ("状态字A", C.WORD_A, 8),
        ("状态字B", C.WORD_B, 8), ("8槽", C.SLOTS8, 8)):
    print("     %-14s 0x%0*X" % (name, width, addr))

print("\n结果文件 results/frbox/cursor73.txt；设施 frbox/cursor73.py；"
      "八台独立机器（基线 / 1M 窗口 / J0–J5 六组），另 V72 复现一台。")
