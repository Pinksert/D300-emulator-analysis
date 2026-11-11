# -*- coding: utf-8 -*-
"""V77 正式探针：不可定址 store 归类、结构体字段写归约、vec 0x2D 设备面（A–H 八段）。

承接 KI#298。设备策略与 V65–V76 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；归约段是**静态数据流**，不跑机器；反事实段的 r4 注入 /
raise_irq / kernel_call 全部标注为模型动作，不是固件行为。
独立机器：基线 / 1M 窗口 / G0–G4 五组。
"""
import sys
import os
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, brealos, cursor73 as K,
                   cursor74 as D, gate75 as G, wait271 as W,
                   submit76 as S, reduce77 as R, isrfam63 as F, level72 as L)

BOOT, VEC, RUN, POST, WINDOW = (blive.BOOT_STEPS, 0x2B,
                                R.RUN_STEPS, R.POST_STEPS, R.WINDOW_STEPS)
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
ROMEND = base + len(rom)


def hr(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)
    sys.stdout.flush()


def decode_at(pc):
    ins = fr_emu.TABLE.decode(rom, pc - base)
    if ins is None:
        return None
    mn, bs, v, size, w = ins
    return (pc, mn, bs, dict(v), size, rom[pc - base:pc - base + size].hex())


def dec(lo, hi):
    out, pc = [], lo
    while pc < hi - 1:
        d = decode_at(pc)
        if d is None:
            pc += 2
            continue
        out.append(d)
        if d[1] in ('ret', 'reti', 'ret:d') and pc != lo:
            break
        pc += d[4]
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


# ---------------------------------------------------------------- 形态普查
SHAPE_LABEL = {
    0x1400: '@Rj:32', 0x1500: '@Rj:16', 0x1600: '@Rj:8',
    0x1000: '@(R13,Rj):32', 0x1100: '@(R13,Rj):16', 0x1200: '@(R13,Rj):8',
    0x3000: '@(disp10,R14):32', 0x5000: '@(disp9,R14):16',
    0x7000: '@(disp8,R14):8', 0x1300: '@(udisp6,R15):32',
}
DECIDABLE_PCS = set(pc for pc, _a, _mn in SW.stores)
CENSUS = collections.Counter()
CROSSTAB = collections.Counter()
off, n = 0, len(rom)
while off < n - 1:
    if rom[off:off + 2] in bstatic.SKIP2:
        off += 2
        continue
    ins = fr_emu.TABLE.decode(rom, off)
    if ins is None:
        off += 2
        continue
    mn, bs, v, size, w = ins
    if mn in ('st', 'stb', 'sth'):
        key = SHAPE_LABEL.get(bs, 'other')
        CENSUS[key] += 1
        CROSSTAB[(key, 'decidable' if base + off in DECIDABLE_PCS
                  else 'unknown')] += 1
    off += size

UNKNOWN_STACK = sum(CROSSTAB[(k, 'unknown')] for k in R.STACK_SHAPES)
UNKNOWN_STACK_R14 = sum(CROSSTAB[(k, 'unknown')] for k in R.STACK_SHAPES
                        if k.startswith('@(disp'))
UNKNOWN_INDEXED = sum(CROSSTAB[(k, 'unknown')] for k in SHAPE_LABEL.values()
                      if k.startswith('@(R13'))
UNKNOWN_DIRECT = sum(CROSSTAB[(k, 'unknown')] for k in SHAPE_LABEL.values()
                     if k.startswith('@Rj'))
UNKNOWN_OTHER = sum(CROSSTAB[(k, 'unknown')] for k in ('other',))

# ------------------------------------------------------------ 局部数据流归约
def fn_start(pc, back=0x900):
    """从对齐起点顺序解码，取最近一条 ret/reti 之后的边界（规矩 59）。"""
    lo, last = max(base, pc - back), None
    p = lo
    while p < pc - 1:
        d = decode_at(p)
        if d is None:
            p += 2
            continue
        if d[1] in ('ret', 'reti', 'ret:d'):
            last = p + d[4]
        p += d[4]
    return last if last is not None else lo


def track(lo, hi, max_steps=1200):
    """线性局部寄存器传播；store 交给 R.reduce_store 归约。"""
    regs = [None] * 16
    out, pc, steps = [], lo, 0
    while pc < hi - 1 and steps < max_steps:
        d = decode_at(pc)
        if d is None:
            pc += 2
            steps += 1
            continue
        _, mn, bs, v, size, _hx = d
        ri, rj = v.get('ri'), v.get('rj')
        if mn.startswith('ldi'):
            regs = R.prop_ldi(regs, ri, v.get('i8', v.get('i20', v.get('i32'))))
        elif mn == 'mov':
            regs = R.prop_mov(regs, ri, rj)
        elif mn == 'add':
            regs = R.prop_add(regs, ri, rj)
        elif mn == 'add2':
            regs = R.prop_add2(regs, ri, v.get('m4', 0))
        elif mn in ('st', 'stb', 'sth'):
            a, w = R.reduce_store(bs, v, regs)
            out.append((pc, mn, a, w, regs[ri] if ri is not None else None))
        elif mn in ('ld', 'ldub', 'lduh', 'ldm0', 'ldm1', 'mul', 'div',
                    'movh', 'movl', 'extsb', 'extsh', 'extub', 'extuh'):
            if mn.startswith(('ld', 'mov', 'mul', 'div')) and ri is not None:
                regs[ri] = None
        elif mn in ('call', 'call:d', 'jmp', 'int'):
            for k in (0, 1, 2, 3, 12):
                regs[k] = None
        elif ri is not None and mn not in ('cmp', 'btsth', 'btstl', 'bandh',
                                          'borh', 'beorh', 'bandl', 'borl',
                                          'beorl', 'nop', 'stm0', 'stm1',
                                          'enter', 'leave', 'ret', 'reti'):
            regs[ri] = None
        if mn in ('ret', 'reti', 'ret:d'):
            break
        pc += size
        steps += 1
    return out


TARGETS = (('WORD_A', R.WORD_A, 0x40), ('WORD_B', R.WORD_B, 0x40),
           ('SLOT8', R.SLOT8, 0x60))
INVENTORY = {}
for name, addr, span in TARGETS:
    refs = [p for p, _i, _nx in SW.refs_to(addr, window=0)]
    seen, rows = set(), []
    for pc in refs:
        lo = fn_start(pc)
        for spc, mn, a, w, src in track(lo, min(pc + 0x400, ROMEND - 2)):
            if not R.is_field_write(a, addr, span) or spc in seen:
                continue
            seen.add(spc)
            rows.append((spc, mn, R.offset_of(a, addr), w, src))
    rows.sort()
    INVENTORY[name] = {'addr': addr, 'refs': refs, 'rows': rows}

ALL_FIELD_PCS = {p for v in INVENTORY.values() for p, *_ in v['rows']}
FIELD_PCS_BY_OBJ = {k: {p for p, *_ in v['rows']}
                    for k, v in INVENTORY.items()}
ISR_DEV_PCS = (0x0012FE90, 0x0012FE98, 0x0012FEA2, R.SETFLG_40_PC)


# ------------------------------------------------------------------ 快照
def snapshot(m, c0):
    c = covset(m)
    ti = bready.task_index(m)
    t5 = ti.get(5)
    return {
        'cov': c, 'new': len(c - c0), 'pc': m.cpu.pc,
        'want': m.bus.rd(t5 + 0x14, 4) if t5 is not None else None,
        'gate': m.bus.rd(S.GATE_BYTE, 1),
        'word_a': m.bus.rd(R.WORD_A, 4), 'word_b': m.bus.rd(R.WORD_B, 4),
        'slots': [m.bus.rd(R.SLOT8 + i * R.SLOT_STRIDE, 1)
                  for i in range(R.SLOT_COUNT)],
        'field': len(c & ALL_FIELD_PCS),
        'field_by': {k: len(c & p) for k, p in FIELD_PCS_BY_OBJ.items()},
        'isr_dev': len(c & set(ISR_DEV_PCS)),
        'isr2d': R.ISR2D in c, 'set40': R.SETFLG_40_PC in c,
        'clear_loop': R.SLOT_CLEAR_PC in c,
    }


def run_group(kind, arg=None):
    """一台独立机器一条路径；kind: base / vec / misvec / vec_bit / vec_n3。"""
    m = boot_run()
    c0 = covset(m)
    if kind == 'vec_n3':
        brealos.kernel_call(m, K.GET_TID)
        fire_r4(m, 5)
        m.run(max_steps=POST, spin_guard=False)
        for lv in range(1, 4):
            awaken(m, lv)
    if kind in ('vec', 'vec_bit', 'vec_n3'):
        fire_vec(m, R.VEC_2D)
    elif kind == 'misvec':
        fire_vec(m, S.VEC_TICK)
    if kind == 'vec_bit':
        setbit(m, 0x40)
    return m, snapshot(m, c0)


def row(tag, r):
    print("  %-9s 新增PC=%3d 末PC=0x%06X want=0x%08X ｜ 字段写命中 %2d/%d "
          "ISR设备 %d/4" %
          (tag, r['new'], r['pc'], r['want'] or 0, r['field'],
           len(ALL_FIELD_PCS), r['isr_dev']))
    print("      分对象 A/B/槽 = %d/%d/%d ｜ vec0x2D ISR=%-5s set40=%-5s "
          "清零循环=%-5s" %
          (r['field_by']['WORD_A'], r['field_by']['WORD_B'],
           r['field_by']['SLOT8'], r['isr2d'], r['set40'], r['clear_loop']))
    print("      门字节=%02X 状态字=%08X/%08X 8槽各项首字节=%s" %
          (r['gate'], r['word_a'], r['word_b'],
           "".join("%02X" % x for x in r['slots'])))


# ====================================================================== A
hr("A. 静态：全 ROM store 形态普查（收紧 V76 / 规矩 64 的措辞）")
print("A0 ROM [0x%06X,0x%06X) len=%d；Sweep.summary stores=%d unknown=%d" %
      (base, ROMEND, len(rom), len(SW.stores), SW.stores_unknown))
print("A1 全 ROM store %d 条，按 opcode base 分形态：" % sum(CENSUS.values()))
for k, c in CENSUS.most_common():
    print("     %-22s %6d  (%.2f%%)" % (k, c, 100 * R.shape_share(c, sum(CENSUS.values()))))
print("A2 形态 × 可定址交叉表：")
for k in sorted({k for k, _ in CROSSTAB}):
    d, u = CROSSTAB[(k, 'decidable')], CROSSTAB[(k, 'unknown')]
    print("     %-22s 可定址 %6d  不可定址 %6d" % (k, d, u))
print("     %-22s 可定址 %6d  不可定址 %6d" %
      ('TOTAL', sum(CROSSTAB[(k, 'decidable')] for k in {k for k, _ in CROSSTAB}),
       sum(CROSSTAB[(k, 'unknown')] for k in {k for k, _ in CROSSTAB})))
print("A3 不可定址 %d 条拆分：栈帧 R14 三类 %d（%.2f%%）、R15 %d、"
      "@(R13,Rj) %d、@Rj %d、其它 %d" %
      (R.STORE_UNKNOWN, UNKNOWN_STACK_R14,
       100 * R.shape_share(UNKNOWN_STACK_R14, R.STORE_UNKNOWN),
       CROSSTAB[('@(udisp6,R15):32', 'unknown')],
       UNKNOWN_INDEXED, UNKNOWN_DIRECT, UNKNOWN_OTHER))
print("     栈形态合计（R14+R15）= %d，占 unknown %.2f%%；非栈 %d 条" %
      (UNKNOWN_STACK, 100 * R.shape_share(UNKNOWN_STACK, R.STORE_UNKNOWN),
       R.nonstack_unknown(UNKNOWN_STACK)))
print("A4 更正 V76：不能笼统说「有 %d 条不可定址 store 所以不能报固件没写」；"
      "其中 %.2f%% 是栈帧局部写，与三个全局数据对象无关。可定址率 %.2f%%" %
      (R.STORE_UNKNOWN, 100 * R.shape_share(UNKNOWN_STACK, R.STORE_UNKNOWN),
       100 * R.decidable_rate()))
print("A5 四类汇总 = %s；栈形态总数（全普查）= %d" %
      (R.census_by_class(dict(CENSUS)), R.census_stack_total(dict(CENSUS))))

# ====================================================================== B
hr("B. 静态：三个数据对象的旧口径与新口径")
for i, (name, addr, _span) in enumerate(TARGETS):
    inv = INVENTORY[name]
    print("B%d %-7s 0x%08X ｜ 直接加载点 %2d / 精确写点 %d / "
          "near32 写点 %2d" %
          (i + 1, name, addr, len(inv['refs']), len(SW.writers_of(addr)),
           len(SW.writers_near(addr, 0x20))))
print("B4 V76 口径：三者「静态可定址写点 0」；V77 口径："
      "``writers_of(base)=0`` 只说明**没有可定址的基址写点**，"
      "不等于结构体内字段没人写（规矩 65）。")

# ====================================================================== C
hr("C. 静态：局部寄存器归约 —— 把字段写从 unknown 里捞出来")
print("C0 方法：对每个 ldi:32 加载点按 ret/reti 回溯到函数起点（规矩 59），"
      "顺序做 ldi / mov / add / add2 传播，store 由 R.reduce_store 归约；"
      "跨调用只清 r0–r3 / r12，遇 ld/mul 清目标。纯静态，不跑机器。")
for name, addr, _span in TARGETS:
    inv = INVENTORY[name]
    rows = inv['rows']
    offs = R.offset_hist([(p, o) for p, _mn, o, _w, _s in rows])
    print("C %-7s 归约字段写 %3d 条 / 偏移 %2d 个 ｜ 偏移 0 被写 = %s" %
          (name, len(rows), len(offs), R.zero_offset_written(
              [(p, o) for p, _mn, o, _w, _s in rows])))
    print("     偏移: %s" % ", ".join("%+d:%d" % (o, c) for o, c in offs.items()))
    for p, mn, o, w, src in rows[:4]:
        print("     0x%06X %-4s off=%+3d w=%d src=%s" %
              (p, mn, o, w, 'unknown' if src is None else hex(src)))
print("C4 KI#192 举例的 0x196F10 写 +0x2D(=%d) -> 0x80："
      "归约偏移里含 +0x2D = %s。旧算法报的 38 个「假写点」不是都不存在，"
      "是被算成了 base+0；V77 重新定位出 %d 个真实字段写（不同 PC 集合）。" %
      (R.KI192_FIELD_OFF,
       R.ki192_revision(INVENTORY['WORD_A']['rows'] and
                        [o for _p, _m, o, _w, _s in INVENTORY['WORD_A']['rows']]),
       len(INVENTORY['WORD_A']['rows'])))
print("C5 WORD_A 的 bit3 判据 0x137EB6 btsth #8,@r0 读的是偏移 %d；"
      "归约结果里 WORD_A 偏移 0 写点 = 0 ⇒ 「状态字恒 0」正确说法是"
      "「没人写偏移 0」，不是「没人写这个结构体」。" %
      R.WORD_A_BIT3_OFF)
print("C6 WORD_B 是 %d 字节控制块，字段写集中在 +16..+60（12 个 4 字节字段）；"
      "near32=%d 与归约 %d 互证" %
      (64, len(SW.writers_near(R.WORD_B, 0x20)),
       len(INVENTORY['WORD_B']['rows'])))
v = R.field_write_verdict(0, [(p, o) for p, _m, o, _w, _s in
                              INVENTORY['WORD_A']['rows']], UNKNOWN_STACK)
print("C7 三类判据 = %s" % v)

# ====================================================================== D
hr("D. 静态：8 槽表的两个真实写路径（更正 V76）")
addrs = R.slot_clear_addrs()
print("D0 8 槽 4 个直接加载点 = %s（B 段口径）；V76 把后三个统称读/等。" %
      ", ".join("0x%06X" % p for p in INVENTORY['SLOT8']['refs']))
print("D1 开机清零循环 0x1758EC：r13 从 %d 每轮 %d，首轮地址 base%+d；"
      "%d 轮枚举 %d 个互不相同的地址，覆盖 [0x%08X,0x%08X) = %s" %
      (R.SLOT_CLEAR_LEN, R.m4_signed(R.SLOT_CLEAR_M4), R.SLOT_CLEAR_FIRST,
       R.slot_clear_rounds(), len(set(addrs)), R.SLOT8,
       R.SLOT8 + R.SLOT_CLEAR_LEN, R.slot_clear_covers(addrs)))
print("     首三个 0x%X / 0x%X / 0x%X，末个 0x%X；表长 = %d 项 × %d = %d B" %
      (addrs[0], addrs[1], addrs[2], addrs[-1],
       R.SLOT_COUNT, R.SLOT_STRIDE, R.slot_table_bytes()))
print("D2 线性追踪只看得见首轮 0x%06X（归约 off=+92）；"
      "循环展开归约出全部 %d 笔。" % (R.SLOT_CLEAR_PC, len(addrs)))
print("D3 更正 V76「另三个加载点是读/等，不是生产者」：0x175930 所在函数的"
      " 0x%06X 通过 add r9,r8 中转后 st @r8 写回槽 0：" % R.SLOT_WRITEBACK_PC)
for p, mn, bs, vv, sz, hx in dec(0x17592E, 0x17595C):
    print("     0x%06X %-8s %-8s %s" %
          (p, mn, hx, {k: vv[k] for k in ('i8', 'i32', 'ri', 'rj', 'm4')
                       if k in vv}))
print("     归约结果 SLOT8 偏移 = %s（+0 是写回，+92 是清零首轮）" %
      sorted({o for _p, _m, o, _w, _s in INVENTORY['SLOT8']['rows']}))
print("D4 仍不能报「8 槽已被填充」：清零写的是 0，写回只在"
      " 0x175930 所在函数被跑到时发生；收敛态命中数见 F/G 段。")

# ====================================================================== E
hr("E. 静态：vec 0x2D ISR 触碰的设备寄存器（规矩 66）")
print("E1 向量表倒序 slot(n)=TBR+(255-n)*4，vec 0x2D 槽 0x%06X -> ISR 0x%08X；"
      "函数体 [0x%06X,0x%06X)" %
      (R.vec_slot(R.VEC_2D), u32(R.vec_slot(R.VEC_2D)),
       R.ISR2D, R.ISR2D_END))
for vec, ent in R.VEC_FAMILY:
    print("     vec=0x%02X slot=0x%06X -> 0x%08X ｜ 反算 vec=%s" %
          (vec, R.vec_slot(vec), u32(R.vec_slot(vec)),
           hex(R.vec_of_slot(R.vec_slot(vec)))))
print("E2 ISR 体（按指令长度解码，不是逐 2 字节）：")
for p, mn, bs, vv, sz, hx in dec(R.ISR2D, R.ISR2D_END + 2):
    print("     0x%06X %-8s %-12s %s" %
          (p, mn, hx, {k: vv[k] for k in ('i8', 'i32', 'ri', 'rj', 'u4', 'u8')
                       if k in vv}))
print("E3 三条设备写：sth r0,@0x%08X 写 0（V23 硬件计数器窗口）；"
      "borh #%d,@0x%08X；stb r0,@0x%08X 清 DA86；"
      "末尾 int #0x40 = set_flg(3,0x40)" %
      (R.DEV_COUNTER, R.BORH_U4, R.DEV_EXPEED, R.DA86))
print("E4 规矩 66：borh 的 u4 是**高半字节掩码**，#%d -> 0x%02X；"
      "低半字节掩码判定 = %s，碰到的低半位线 = %s。既有建模中"
      " 0x7000000A 的 CLK=0x%02X / DATA=0x%02X 都在低半字节，"
      "所以不能报「这条 ISR 拉了 CLK 线」。" %
      (R.BORH_U4, R.borh_mask(R.BORH_U4),
       R.is_low_half_mask(R.borh_mask(R.BORH_U4)),
       R.expeed_line_touched(R.borh_mask(R.BORH_U4)),
       R.CLK_MASK, R.DATA_MASK))
for dev in (R.DEV_COUNTER, R.DEV_EXPEED, R.DA86):
    print("E5 dev=0x%08X 全 ROM ldi:32 引用 %2d / 精确写点 %2d / near8 %2d" %
          (dev, len(SW.refs_to(dev, window=0)), len(SW.writers_of(dev)),
           len(SW.writers_near(dev, 8))))
print("E6 边界：只能报「这条 ISR 触碰了哪些设备寄存器」，"
      "不能报「vec 0x2D 是什么硬件事件」；引脚语义来自既有建模，不是本版实测。")

# ====================================================================== F
hr("F. 动态：收敛态覆盖与归约写点命中（无反事实注入）")
m_base = boot_run()
c_base = covset(m_base)
m_win = boot_run(steps=WINDOW)
c_win = covset(m_win)
r_base = snapshot(m_base, c_base)
print("F1 500k 覆盖 %d / 1M 覆盖 %d，对称差集 500k→1M %d、1M→500k %d，"
      "窗口相同 = %s；末 PC 0x%06X" %
      (len(c_base), len(c_win), R.coverage_delta(c_base, c_win),
       R.coverage_delta(c_win, c_base), R.window_same(c_base, c_win),
       m_base.cpu.pc))
print("F2 归约字段写 PC 共 %d 个，收敛态命中 %d 个（A %d / B %d / 槽 %d）" %
      (len(ALL_FIELD_PCS), r_base['field'], r_base['field_by']['WORD_A'],
       r_base['field_by']['WORD_B'], r_base['field_by']['SLOT8']))
print("F3 开机清零循环 0x%06X 命中 = %s；vec 0x2D ISR 设备写命中 %d/4" %
      (R.SLOT_CLEAR_PC, r_base['clear_loop'], r_base['isr_dev']))
print("F4 状态字 A=%08X B=%08X，8 槽首字 = %s，门字节=%02X" %
      (r_base['word_a'], r_base['word_b'],
       "".join("%02X" % x for x in r_base['slots']), r_base['gate']))

# ====================================================================== G
hr("G. 动态：五组等形反事实（设备策略与 V65–V76 逐字相同）")
groups = [('G0', 'base', None), ('G1', 'vec', None),
          ('G2', 'misvec', None), ('G3', 'vec_bit', None),
          ('G4', 'vec_n3', None)]
res = {}
for tag, kind, arg in groups:
    m, r = run_group(kind, arg)
    res[tag] = r
    row(tag, r)
shapes = [(R.BOOT_STEPS, VEC, RUN) for _tag, _k, _a in groups]
print("G6 等形前缀一致 = %s（boot %d / 单次 vec 0x%02X / run %d 相同）" %
      (R.equal_shape(shapes), BOOT, VEC, RUN))
print("G7 归因分离：目标变化（ISR 设备写命中）只出现在打 vec 0x2D 的组 = %s" %
      R.attribution([(t, res[t]['isr_dev'] > 0,
                      'vec2d' if t in ('G1', 'G3', 'G4') else 'other')
                     for t in res], 'vec2d')[0])
print("G8 字段写命中在五组里的最大增量 = %d；"
      "打中断不解开字段写路径 ⇒ 归约出的是**静态存在、收敛态未执行**的路径" %
      max(r['field'] for r in res.values()))

# ====================================================================== H
hr("H. 结案、开放项与边界")
print("H1 KI#298 第一条（部分结案）：两个状态字的**字段写路径**已定位 ——"
      " WORD_A %d 条 / %d 偏移、WORD_B %d 条 / %d 偏移；"
      "偏移 0 无写点，bit3 恒 0 的原因不是「固件没有写点」。" %
      (len(INVENTORY['WORD_A']['rows']),
       len({o for _p, _m, o, _w, _s in INVENTORY['WORD_A']['rows']}),
       len(INVENTORY['WORD_B']['rows']),
       len({o for _p, _m, o, _w, _s in INVENTORY['WORD_B']['rows']})))
print("H2 KI#298 第二条（部分结案）：8 槽填充源 = 0x1758EC 的 %d 轮开机清零"
      "（%d 个可枚举地址）+ 0x%06X 的槽 0 写回；更正 V76 "
      "「另三个加载点不是生产者」。" %
      (R.slot_clear_rounds(), len(set(addrs)), R.SLOT_WRITEBACK_PC))
print("H3 KI#298 第三条（部分结案）：vec 0x2D ISR 触碰 0x%08X / 0x%08X / "
      "0x%08X；仍不知对应什么硬件事件。" %
      (R.DEV_COUNTER, R.DEV_EXPEED, R.DA86))
print("H4 新硬规矩 65（基址写 / 字段写 / 栈写三分）与 66（borh u4 是高半字节掩码）。")
print("H5 仍开放：偏移 0 由谁在真机上置 bit3、字段写路径的触发任务与时刻、"
      "8 槽写回函数被谁调用、302 个提交调用点的真机触发任务、"
      "vec 0x2D 的硬件事件、真机让 r4 非 0 的事件语义、"
      "0x12E636 返回非 0 条件、真机 tick 源（KI#191）、"
      "13 组 ISR 各自硬件事件、[12,13,14,15] 激活机制（KI#257）。")
print("H6 边界：全部 r4 注入 / raise_irq / kernel_call 都是反事实补全；"
      "归约是静态数据流，不等于真机执行过。不能报显示链通了 / 出画面 / "
      "系统跑起来了 / 真机如此 /「补一次 set_flg 就唤醒成功」。")
print("H7 设施：frbox/reduce77.py（%d 个纯函数）、probe/exp_reduce_v77.py、"
      "results/frbox/reduce77.txt；test_v77() 与版本见 frbox_selftest.py / "
      "frbox/__init__.py。" % len([x for x in dir(R)
                                   if callable(getattr(R, x))
                                   and not x.startswith('_')]))
sys.stdout.flush()
