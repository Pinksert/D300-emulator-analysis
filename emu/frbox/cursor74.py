# -*- coding: utf-8 -*-
"""V74：KI#286 第一条结案、24 项回调表语义与 D858 的 DA82 中转（纯函数）。

承接 V73。V73 的 J3 命中回调 ``0x12F77A``，但它写 ``0x9F58DA83``
用的是参数 ``r4``，而该 ISR 上下文里 ``r4=0``，DA83 仍为 0。
KI#286 第一条问「回调参数 r4 的来源」。V74 的答案：

* **r4 是中断现场透传**。派发器 A（``0x12FE54``）在 ``call @r0``
  前只有 ``stm0/stm1``（保存）与索引装载，没有给 r4 赋任何常量；
  ``0x12F780 stb r4,@DA83`` 写的是「中断发生那一刻 CPU 的 r4」。
  派发器 B（``0x12FDB4``）走回调分支前用 ``0x12FDF0 mov r13,r4``
  显式设置 r4，r13 来自 ``0x12FDEE dmovb`` 直接页读值。
  等形反事实 K0–K3 注入 r4 = 0 / 5 / 0x46 / 1，DA83 逐一变成
  00 / 05 / 46 / 01，四组新增 PC 全是 31 ⇒ 归因成立。
* **24 项回调表语义已解出**（``0x1BC480``）：按置位值分组
  0x1×2 / 0x10×3 / 0x10+0x20×1 / 0x2×1 / 0x20×3 /
  0x4000+0x20×1 / 0x6×1 / 0x804000+0x4000+0x20×1 / 不置位×11。
  11 项只写游标字节（DA7C/DA7D/DA7E/DA7F/DA80/DA86 等），
  不置 flag；2 项是裸 ret 空槽。
* **KI#286 里「L3 所需 0x74 置位源」是掩码误读**（规矩 56）。
  全 ROM flag 3 的 44 个 ``set_flg`` 里没有任何一个置 0x74：
  0x10 有 4 点、0x20 有 7 点、0x40 有 1 点。判据
  ``(flgptn & 0x74) == 0x10`` 要求 0x20 / 0x40 **必须为 0**，
  所以 0x20 / 0x40 是干扰位。六组判据位域反事实 P0–P5：
  只有单独 0x10 跨到 L2 派发（二级新增 129 PC），
  0x30 / 0x50 / 0x74 / 0x40 全部停在 107 PC 且 L2 未命中。
* **D858 算式的 idx 是 DA82 中转，不是 DA83**（更正 V72 口径）。
  ``0x12E242 stb r0,@r5``（r5=DA82）把 r0 装入 DA82，
  ``0x12E268 ldub @r5,r0`` 读出，``0x12E270 lsl #4``，
  ``0x12E274 st r0,@r7`` 写 D858。时序实测：
  L4（DA83=5 / DA84=0）经 ``0x12E242`` 时 r0=5 ⇒ DA82=5
  ⇒ D858=``0x001BC070``；L2（DA84=1）⇒ DA82=1 ⇒ ``0x001BC030``。
* **L4 首次不靠外部写 DA82/DA83/DA84** 就由固件自己的事件链
  （r4 事件 + 两级唤醒）算出 D858，L2 派发 / L2 bne / L3 等待
  全部命中。但 L4 末态仍挂在 ``0x74`` 等待、处理函数 0/44，
  链未闭合。

**规矩 62（本版新增）**：报「地址算式的 idx 是哪个字节」前先追
中转寄存器与装载点；V72 把 DA83 当 idx 是只读了常量表、
没跟 ``stb @r5`` 那一步。

**规矩 60 / 56 沿用**：向量表仍是倒序映射
``vector(n)=TBR+(255-n)*4``；``0x74`` 仍是掩码不是要置的位。
本版更正 V73 结果文件 C1 时，把探针改为从向量表实测读 ISR，
不再手填槽内容。

**已交付口径更正**：V73 结果文件 C1 曾把 ``vec 0x2B`` 的 ISR
手填为 ``0x12FDB4``；槽 212 实测是 ``0x00042E1C``。V74 已把
V73 探针 C1 改为从向量表实测读取并重跑，结果文件 198 行不变。

所有 r4 寄存器注入 / 写索引字节 / ``raise_irq`` / ``kernel_call``
都是外部反事实补全，不是固件行为；不能报显示链通了 / 出画面 /
真机如此 /「补一次 set_flg 就唤醒成功」。
"""

# ---------------------------------------------------------------- 地址常量
TBR = 0x000EFC00
VEC_CONV = 0x2B
VEC_A, VEC_B = 0x20, 0x1B
ISR_A, ISR_B = 0x0012FE54, 0x0012FDB4
CB_TABLE_A, CB_TABLE_B = 0x001BC480, 0x001BC490
CB_COUNT = 24
CB_FN, CB_WRITE, CB_SETFLG = 0x0012F77A, 0x0012F780, 0x0012F794
IDX_A, IDX_B = 0x9F58DA85, 0x9F58DA86

CUR_BASE = 0x9F58DA7C
DA82, DA83, DA84 = 0x9F58DA82, 0x9F58DA83, 0x9F58DA84
DESC_TABLE, DESC_STRIDE, DESC_COUNT = 0x001BC020, 0x10, 70
DESC_PTR = 0x9F58D858
SEM_POOL = 0x9F58D500
QUERY_FN = 0x0012E636
WAIT1_FN, WAIT2_FN = 0x0012E0F8, 0x0012E162
L2_FN, L3_FN = 0x0012E1EC, 0x0012E326

# D858 算式的三个关键 PC
LOAD_DA82 = 0x0012E242      # stb r0,@r5（r5=DA82）
READ_DA82 = 0x0012E268      # ldub @r5,r0
SHIFT_DA82 = 0x0012E270     # lsl r0,#4
WRITE_D858 = 0x0012E274     # st r0,@r7
# 三个 DA84 写点（V73）
DA84_WRITERS = (0x12E112, 0x12E140, 0x12E158)

MASK, EXPECT = 0x74, 0x10
BIT10, BIT20, BIT40 = 0x10, 0x20, 0x40
SET_FLG, CLR_FLG, WAI_FLG = -48, -47, -46
GET_TID, FLAG_ID, WANTPTN_1 = -24, 3, 9

# flag 3 置位源分类（全 ROM 44 点中的低三位部分）
SETTERS_10 = (0x12F794, 0x12F964, 0x12FB10, 0x12FDB0)
SETTERS_20 = (0x12F882, 0x12FB1C, 0x12FB54, 0x12FC4C,
              0x12FC84, 0x12FD98, 0x12FDE8)
SETTERS_40 = (0x12FEAC,)
CLR_76 = (0x12E196, 0x12E34A)     # clr_flg(3,0x89/0x8F) 清 0x76 / 0x70

# 24 项回调：索引 -> (入口, 置位值元组, 说明)
CALLBACKS = (
    (0x0012FE12, (0x1,), "裸 ret 槽 + 置 0x1"),
    (0x0012FE14, (0x1,), "置 0x1"),
    (0x0012FE20, (0x6,), "置 0x6 + 清 DA86"),
    (0x0012FE48, (0x2,), "置 0x2"),
    (0x0012F778, (0x10,), "裸 ret 槽 + 置 0x10 / 写 DA83"),
    (0x0012F77A, (0x10,), "写 DA83=r4 + 清 DA86 + 置 0x10"),
    (0x0012F798, (), "只写 DA7D/DA80/DA7E/DA86"),
    (0x0012F7DC, (), "只写 DA7D/D850/DA7E/DA86"),
    (0x0012F82C, (0x20,), "写游标 + 置 0x20"),
    (0x0012F888, (), "写 D854/DA7C/DA86/DA85"),
    (0x0012F8EC, (), "写 DA7F/D854/DA7C/DA86/DA85"),
    (0x0012F952, (0x10,), "清 DA86 + 置 0x10"),
    (0x0012F968, (), "写 D854/DA7C/DA86"),
    (0x0012F9C0, (), "写 DA7F/D854/DA7C/DA86"),
    (0x0012FA1C, (), "清 DA86"),
    (0x0012FA3E, (), "写游标 + DA85"),
    (0x0012FA90, (), "写游标 + D850 + DA85"),
    (0x0012FAEA, (0x10, 0x20), "置 0x10 + 0x20（判据必失败）"),
    (0x0012FB22, (0x20,), "写游标 + 置 0x20"),
    (0x0012FB76, (), "写游标 + D850"),
    (0x0012FBC6, (0x4000, 0x20), "置 0x4000 + 0x20"),
    (0x0012FC52, (0x20,), "写游标 + 置 0x20"),
    (0x0012FCA6, (), "写游标 + D850"),
    (0x0012FCF6, (0x804000, 0x4000, 0x20), "多项置位"),
)

# 六组判据位域反事实：(标签, bits, 二级新增PC, L2 派发命中)
BIT_GROUPS = (
    ("P0", 0x00, 0, False),
    ("P1", 0x10, 129, True),
    ("P2", 0x30, 107, False),
    ("P3", 0x50, 107, False),
    ("P4", 0x74, 107, False),
    ("P5", 0x40, 107, False),
)
# 四组 r4 注入反事实：(标签, r4, 期望 DA83)
R4_GROUPS = (("K0", 0, 0), ("K1", 5, 5),
             ("K2", 0x46, 0x46), ("K3", 1, 1))
# 五组组合反事实：(标签, 注入r4, 两级唤醒, 外部写字节)
COMBO_GROUPS = (("L0", False, False, False),
                ("L1", True, False, False),
                ("L2", False, True, True),
                ("L3", True, True, True),
                ("L4", True, True, False))


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """有符号扩展。"""
    m = 1 << (bits - 1)
    return (v ^ m) - m if v & m else v


def slot_of_vec(vec, tbr=TBR):
    """倒序映射 vec -> 向量表槽地址（规矩 60）。"""
    return tbr + (255 - (vec & 0xFF)) * 4


def vec_of_slot(addr, tbr=TBR):
    d = addr - tbr
    if d < 0 or d > 0x3FC or d % 4:
        return None
    return 255 - d // 4


def mask_verdict(flgptn, mask=MASK, expect=EXPECT):
    """V71 第二道 / V72 L3 的判据：(flgptn & 0x74) == 0x10。"""
    return (flgptn & mask) == expect


def interfering_bits(bits, mask=MASK, expect=EXPECT):
    """bits 里会破坏判据的干扰位（0x20 / 0x40）。"""
    return bits & mask & ~expect


def bit_group(bits):
    """把置位值归到 V74 的四类。"""
    if bits == BIT10:
        return '0x10'
    if bits == BIT20:
        return '0x20'
    if bits == BIT40:
        return '0x40'
    return 'other'


def setter_count():
    """flag 3 低三位置位源计数。"""
    return {'0x10': len(SETTERS_10), '0x20': len(SETTERS_20),
            '0x40': len(SETTERS_40)}


FLAG3_LOW_BITS = (0x1, 0x1, 0x6, 0x2, 0x10, 0x10, 0x20, 0x10,
                  0x10, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20, 0x40,
                  0x6, 0x2, 0x1)  # 全 ROM flag3 set_flg 中与低三位相关的值


def has_074_setter(values=FLAG3_LOW_BITS):
    """全 ROM flag 3 是否存在 0x74 置位源（答案：没有）。"""
    return any(v == 0x74 for v in values)


def cb_entry_addr(idx, table=CB_TABLE_A, stride=4):
    return table + (idx & 0xFF) * stride


def cb_table_target(idx, table=CB_TABLE_A):
    """第 idx 项的回调入口（从静态表，纯函数查表）。"""
    if not 0 <= idx < CB_COUNT:
        return None
    if table == CB_TABLE_A:
        return CALLBACKS[idx][0]
    # 表 B 是同一数组第 4 项起
    return CALLBACKS[idx + 4][0] if idx + 4 < CB_COUNT else None


def cb_bits(idx):
    """第 idx 项回调的置位值元组。"""
    return CALLBACKS[idx][1] if 0 <= idx < CB_COUNT else ()


def cb_groupkey(bits_seq):
    """把置位元组归成 B6 的分组键。"""
    return ','.join('0x%X' % b for b in bits_seq) or 'none'


def cb_semantics():
    """24 项按置位值分组 -> {组键: [索引]}。"""
    out = {}
    for i, (_fn, bits, _desc) in enumerate(CALLBACKS):
        out.setdefault(cb_groupkey(bits), []).append(i)
    return out


def cb_write_only_count():
    """不置 flag、只写游标字节的回调项数。"""
    return sum(1 for _fn, bits, _d in CALLBACKS if not bits)


def desc_ptr(idx_byte, table=DESC_TABLE, stride=DESC_STRIDE):
    """D858 算式：表基址 + idx*0x10（idx 实际来自 DA82）。"""
    return table + (idx_byte & 0xFF) * stride


def da82_middle(load_value):
    """0x12E242 装入 DA82 的值（就是算式 idx）。"""
    return load_value & 0xFF


def desc_ptr_chain(load_value):
    """从 0x12E242 的装载值一路算出 D858（纯函数链）。"""
    return desc_ptr(da82_middle(load_value))


def r4_source(vec):
    """派发器对 r4 的处理口径。"""
    if vec == VEC_A:
        return 'passthrough'      # 中断现场透传，未赋常量
    if vec == VEC_B:
        return 'mov_r13_r4'       # 0x12FDF0 mov r13,r4
    return None


def r4_to_da83(r4val):
    """回调 0x12F780 stb r4,@DA83 的效果。"""
    return r4val & 0xFF


def in_table(idx, count=DESC_COUNT):
    return 0 <= (idx & 0xFF) < count


def equal_shape(shapes):
    keys = {'boot': {s[0] for s in shapes},
            'post': {s[2] for s in shapes}}
    return {'ok': all(len(v) == 1 for v in keys.values()), 'keys': keys}


def attribution(results, flag):
    return [tag for tag, r in results.items() if r.get(flag)]


def chain_verdict(l2_hit, desc_ptr_filled, handler_hit):
    """链闭合需要三者同时成立。"""
    return bool(l2_hit and desc_ptr_filled and handler_hit)


def window_same(cov_a, cov_b):
    return len(set(cov_a) ^ set(cov_b))
