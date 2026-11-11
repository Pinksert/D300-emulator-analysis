# -*- coding: utf-8 -*-
"""V77：结构体字段写归约与不可定址 store 的形态分类（纯函数，只读 ROM 由探针做）。

承接 KI#298。V76 留下两句需要收紧的话：

* 「两个状态字 / 8 槽静态可定址写点 0，但 ``Sweep.stores_unknown``
  有 224489 条 ⇒ 只能报无精确写点，不能报固件没写」。这话过于宽松：
  224489 条里绝大多数是 ``@(disp,R14)`` **栈帧局部写**，与全局状态字
  无关；真正需要归约的是剩下那一小部分。
* 「另三个 8 槽加载点 ``0x175930`` / ``0x1759AC`` / ``0x175A20``
  是读 / 等，不是生产者」。局部寄存器生命周期显示 ``0x175930`` 所在
  函数里 ``0x175958 st @r8`` 通过 ``add r9,r8`` 中转后写回槽 0。

本模块只做三件**纯计算**：(1) store 形态分类与栈写占比；
(2) 从 ``ldi:32 #base`` 起做局部寄存器传播，把 ``st/stb/sth`` 归约成
``base + off`` 的字段写；(3) vec 0x2D ISR 的位掩码 / 设备面判据。
所有 ROM 读取、反汇编、机器运行都在 ``probe/exp_reduce_v77.py`` 里，
这样 ``test_v77()`` 不必加载固件也能覆盖全部判据。

硬规矩 65：报「静态无写点」前先分清**基址写 / 字段写 / 栈写**三类。
``@(R13,Rj)`` 中 Rj 常装结构体基址、R13 装字段偏移，
``writers_of(base) == 0`` 只说明偏移 0 没写，不等于结构体没人写。
硬规矩 66：``borh/bandh #u4`` 的立即数是**高半字节掩码** ``u4<<4``，
不是位号；报「拉了哪根线」前先按 ``fr_emu`` 位操作分支换算。
"""

# ---------------------------------------------------------------- 实测常量
#: 全 ROM 线性扫（V77 A 段，A1/A2）
STORE_TOTAL = 244342
STORE_DECIDABLE = 19853
STORE_UNKNOWN = 224489

#: 全 ROM store 按 opcode base 的形态普查（A 段 A1）
SHAPE_CENSUS = {
    '@(disp8,R14):8': 111949,
    '@(disp10,R14):32': 61495,
    '@(disp9,R14):16': 23424,
    '@(R13,Rj):16': 9386,
    '@Rj:8': 7002,
    '@(R13,Rj):32': 6373,
    '@(R13,Rj):8': 5659,
    '@Rj:32': 5114,
    '@Rj:16': 4992,
    'other:0x1780': 4176,
    '@(udisp6,R15):32': 3577,
    'other:0x1700': 1192,
    'other:0x1790': 3,
}
#: 栈帧形态（相对 R14 / R15），与全局数据对象无关
STACK_SHAPES = ('@(disp8,R14):8', '@(disp10,R14):32',
                '@(disp9,R14):16', '@(udisp6,R15):32')

#: 形态 × 可定址交叉后的不可定址明细（V77 A2 实测）
UNKNOWN_BY_SHAPE = {
    '@(disp8,R14):8': 107582,
    '@(disp10,R14):32': 55066,
    '@(disp9,R14):16': 22094,
    '@(R13,Rj):16': 7339,
    '@Rj:8': 5962,
    '@(R13,Rj):32': 5363,
    'other': 5371,
    '@(R13,Rj):8': 5121,
    '@Rj:16': 4128,
    '@(udisp6,R15):32': 3513,
    '@Rj:32': 2950,
}
UNKNOWN_STACK = 188255          # R14 三类 + R15
UNKNOWN_STACK_R14 = 184742      # 只算 R14 三类
UNKNOWN_INDEXED = 17823         # @(R13,Rj)
UNKNOWN_DIRECT = 13040          # @Rj
UNKNOWN_OTHER = 5371
NONSTACK_UNKNOWN = 36234

#: 三个数据对象（V76 submit76.py 同名常量）
WORD_A = 0x9F5A0284
WORD_B = 0x9F59EF50
SLOT8 = 0x9F59F7C8
OBJECTS = (WORD_A, WORD_B, SLOT8)
OBJECT_NAMES = ('WORD_A', 'WORD_B', 'SLOT8')
LOAD_COUNTS = (63, 31, 4)
EXACT_WRITERS = (0, 0, 0)
NEAR32_WRITERS = (12, 30, 0)
#: 局部归约出的字段写点数与偏移集合（C 段；以 inv77.json 为准）
REDUCED_COUNTS = (31, 109, 2)
REDUCED_OFFSETS = (
    (-4, 4, 12, 16, 20, 24, 28, 32, 36, 44, 45, 46),
    (16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60),
    (0, 92),
)
KI192_FIELD_OFF = 45          # KI#192 举例的 +0x2D 写 0x80
WORD_A_BIT3_OFF = 0           # 0x137EB6 btsth #8,@r0 测的是偏移 0

#: 8 槽表开机清零循环（V76 已定性，V77 枚举地址）
SLOT_CLEAR_PC = 0x001758FA
SLOT_CLEAR_LDI = 0x001758F4   # ldi:8 #96,r13
SLOT_CLEAR_ADD2 = 0x001758F6  # add2 #-4,r13
SLOT_CLEAR_M4 = 0x0C          # 4 位有符号 = -4
SLOT_CLEAR_LEN = 96
SLOT_CLEAR_WIDTH = 4
SLOT_CLEAR_ROUNDS = 24
SLOT_CLEAR_FIRST = 92         # 首轮 r13 = 96-4
SLOT_STRIDE = 12              # 8 项 × 0xC
SLOT_COUNT = 8
SLOT_WRITEBACK_PC = 0x00175958   # st @r8，经 add r9,r8 中转（更正 V76）

#: vec 0x2D ISR（V76 找到入口，V77 拆设备面）
TBR = 0x000EFC00
VEC_2D = 0x2D
ISR2D = 0x0012FE84
ISR2D_END = 0x0012FEB4       # reti 之后
DEV_COUNTER = 0x5C003000     # V23 硬件自由运行计数器窗口
DEV_EXPEED = 0x7000000A      # B<->EXPEED 位串行接口
DA86 = 0x9F58DA86
BORH_U4 = 1
BORH_MASK = 0x10             # u4<<4
CLK_MASK = 0x02              # 0x7000000A bit1
DATA_MASK = 0x04             # 0x7000000A bit2
SETFLG_40_PC = 0x0012FEAC
VEC_FAMILY = ((0x2C, 0x00042E72), (0x2D, 0x0012FE84),
              (0x2E, 0x0012D9AA), (0x2F, 0x0012D9DE))

#: 收敛设备策略与 V65–V76 逐字相同
BOOT_STEPS = 191520
VEC_TICK = 0x2B
RUN_STEPS = 500000
POST_STEPS = 200000
WINDOW_STEPS = 1000000

#: store 形态表（照抄 bstatic._ST_SHAPE，写成纯数据便于自检）
SHAPE_BY_BASE = {
    0x1400: ('@Rj', 4), 0x1500: ('@Rj', 2), 0x1600: ('@Rj', 1),
    0x1000: ('@(R13,Rj)', 4), 0x1100: ('@(R13,Rj)', 2),
    0x1200: ('@(R13,Rj)', 1),
    0x3000: ('@(disp10,R14)', 4), 0x5000: ('@(disp9,R14)', 2),
    0x7000: ('@(disp8,R14)', 1),
    0x1300: ('@(udisp6,R15)', 4),
}
DISP_BITS = {0x3000: ('disp10', 10), 0x5000: ('disp9', 9),
             0x7000: ('disp8', 8)}


# ---------------------------------------------------------------- 小工具
def sx(v, bits):
    """bits 位有符号立即数。"""
    m = (1 << bits) - 1
    v &= m
    return v - (1 << bits) if (v >> (bits - 1)) & 1 else v


def m4_signed(m4):
    """``add2`` 的 m4 是 4 位有符号（V65 已核）；0xC = -4。"""
    return sx(m4, 4)


def shape_of(bs):
    """opcode base -> (形态名, 访问宽度)；不在表里返回 (None, None)。"""
    return SHAPE_BY_BASE.get(bs, (None, None))


def is_stack_shape(shape):
    return shape.startswith('@(disp') or shape.startswith('@(udisp6')


def shape_share(count, total):
    return 0.0 if not total else count / float(total)


def census_stack_total(census=SHAPE_CENSUS):
    return sum(v for k, v in census.items() if k in STACK_SHAPES)


def census_by_class(census=SHAPE_CENSUS):
    """全普查按 栈 / 索引 / 直接 / 其它 四类汇总。"""
    out = {'stack': 0, 'indexed': 0, 'direct': 0, 'other': 0}
    for k, v in census.items():
        if k in STACK_SHAPES:
            out['stack'] += v
        elif k.startswith('@(R13'):
            out['indexed'] += v
        elif k.startswith('@Rj'):
            out['direct'] += v
        else:
            out['other'] += v
    return out


def nonstack_unknown(stack_unknown, unknown=STORE_UNKNOWN):
    return unknown - stack_unknown


def decidable_rate(decidable=STORE_DECIDABLE, total=STORE_TOTAL):
    return shape_share(decidable, total)


# ------------------------------------------------------- 局部寄存器传播
def prop_ldi(regs, ri, imm):
    out = list(regs)
    if ri is not None:
        out[ri] = imm
    return out


def prop_mov(regs, ri, rj):
    out = list(regs)
    if ri is not None:
        out[ri] = regs[rj] if rj is not None else None
    return out


def prop_add(regs, ri, rj):
    out = list(regs)
    if ri is None:
        return out
    a, b = regs[ri], (regs[rj] if rj is not None else None)
    out[ri] = (a + b) & 0xFFFFFFFF if (a is not None and b is not None) else None
    return out


def prop_add2(regs, ri, m4):
    out = list(regs)
    if ri is not None:
        out[ri] = ((regs[ri] + m4_signed(m4)) & 0xFFFFFFFF
                   if regs[ri] is not None else None)
    return out


def addr_at_rj(regs, rj):
    return None if rj is None or regs[rj] is None else regs[rj] & 0xFFFFFFFF


def addr_r13_rj(regs, rj):
    if rj is None or regs[13] is None or regs[rj] is None:
        return None
    return (regs[13] + regs[rj]) & 0xFFFFFFFF


def addr_disp_r14(regs, disp, bits, scale=8):
    if regs[14] is None or disp is None:
        return None
    return (regs[14] + sx(disp, bits) * scale) & 0xFFFFFFFF


def addr_udisp_r15(regs, udisp6):
    return None if regs[15] is None else (regs[15] + udisp6 * 4) & 0xFFFFFFFF


def reduce_store(bs, v, regs):
    """把一条 store 归约成 ``(addr, width)``；算不出返回 ``(None, width)``。"""
    kind, width = shape_of(bs)
    if kind is None:
        return None, None
    if kind == '@Rj':
        return addr_at_rj(regs, v.get('rj')), width
    if kind == '@(R13,Rj)':
        return addr_r13_rj(regs, v.get('rj')), width
    if kind.startswith('@(disp'):
        key, bits = DISP_BITS[bs]
        return addr_disp_r14(regs, v.get(key), bits), width
    if kind == '@(udisp6,R15)':
        return addr_udisp_r15(regs, v.get('udisp6', 0)), width
    return None, width


def is_field_write(addr, base, span, back=0x20):
    return addr is not None and base - back <= addr < base + span


def offset_of(addr, base):
    return None if addr is None else addr - base


def offset_hist(rows):
    """rows: [(pc, off)] -> {off: count}（升序键）。"""
    out = {}
    for _pc, off in rows:
        out[off] = out.get(off, 0) + 1
    return {k: out[k] for k in sorted(out)}


def zero_offset_written(rows):
    return any(off == 0 for _pc, off in rows)


def field_write_verdict(exact, rows, stack_unknown, unknown=STORE_UNKNOWN):
    """规矩 65：三类分开报，不能把字段写说成基址写，也不能把栈写算进候选。"""
    return {
        'exact_base_writes': exact,
        'field_writes': len(rows),
        'zero_offset_written': zero_offset_written(rows),
        'stack_unknown': stack_unknown,
        'nonstack_unknown': nonstack_unknown(stack_unknown, unknown),
        'may_claim_no_firmware_write': (exact == 0 and not rows
                                        and nonstack_unknown(stack_unknown,
                                                             unknown) == 0),
    }


# ---------------------------------------------------------- 8 槽清零循环
def slot_clear_addrs(base=SLOT8, first=SLOT_CLEAR_FIRST,
                     step=SLOT_CLEAR_WIDTH, rounds=SLOT_CLEAR_ROUNDS):
    """枚举 24 轮延迟槽 ``st`` 的地址（线性追踪只看得见第一轮）。"""
    return [base + first - step * i for i in range(rounds)]


def slot_clear_rounds(length=SLOT_CLEAR_LEN, width=SLOT_CLEAR_WIDTH):
    return length // width


def slot_clear_covers(addrs, base=SLOT8, span=SLOT_CLEAR_LEN,
                      width=SLOT_CLEAR_WIDTH):
    """24 轮 × 4B 是否恰好铺满 [base, base+span)。"""
    if len(addrs) != span // width:
        return False
    touched = set()
    for a in addrs:
        for k in range(width):
            touched.add(a + k - base)
    return touched == set(range(span))


def slot_table_bytes(count=SLOT_COUNT, stride=SLOT_STRIDE):
    return count * stride


# ------------------------------------------------------- 位掩码 / 设备面
def borh_mask(u4):
    """``borh #u4`` 的立即数是**高半字节掩码**（fr_emu.py:532-545）。"""
    return (u4 << 4) & 0xFF


def is_low_half_mask(mask):
    return bool(mask & 0x0F)


def expeed_line_touched(mask, clk=CLK_MASK, data=DATA_MASK):
    """返回这次掩码实际碰到的低半字节线名；高半字节返回空元组。"""
    out = []
    if mask & clk:
        out.append('CLK')
    if mask & data:
        out.append('DATA')
    return tuple(out)


def vec_slot(vec, tbr=TBR):
    """向量表倒序（规矩 60）：vector(n) = TBR + (255-n)*4。"""
    return tbr + (255 - vec) * 4


def vec_of_slot(slot, tbr=TBR):
    return 255 - (slot - tbr) // 4


def isr_device_plane(writes):
    """writes: [(addr, mnemonic)] -> 去重后的设备地址集合（升序）。"""
    return tuple(sorted({a for a, _mn in writes}))


def ki192_revision(offsets, field_off=KI192_FIELD_OFF):
    """KI#192 的「38 个假写点」里，至少 +0x2D 是可归约的真实字段写。"""
    return field_off in offsets


# --------------------------------------------------------------- 反事实
def equal_shape(shapes):
    """所有组的前缀（boot / tick 向量 / run 预算）逐字相同（V51/V76 口径）。"""
    return len(set(tuple(s) for s in shapes)) <= 1


def attribution(groups, flag):
    """等形反事实：只有带 flag 的组出现目标变化才算归因成立。"""
    hits = [name for name, changed, tagged in groups if changed and tagged == flag]
    noise = [name for name, changed, tagged in groups if changed and tagged != flag]
    return (bool(hits) and not noise), hits, noise


def coverage_hits(pcs, cov):
    return sum(1 for p in pcs if p in cov)


def coverage_delta(cov_a, cov_b):
    return len(set(cov_b) - set(cov_a))


def window_same(cov_a, cov_b):
    return coverage_delta(cov_a, cov_b) == 0 and coverage_delta(cov_b, cov_a) == 0


def chain_verdict(field_hits, exact_writes, words_nonzero, slots_nonzero):
    """字段写被跑到 ≠ 基址状态字被写 ≠ 显示链闭合。"""
    return {
        'field_paths_executed': bool(field_hits),
        'base_written': bool(exact_writes),
        'words_nonzero': bool(words_nonzero),
        'slots_nonzero': bool(slots_nonzero),
        'display_chain_closed': False,
    }
