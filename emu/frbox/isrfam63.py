# -*- coding: utf-8 -*-
"""frbox.isrfam63 —— 同族门 ISR 家族 / 中断向量表 / 显示状态查询函数（V63）

只读 ROM，不建机器、不挂设备、不写内存；所有函数都是纯函数。

V62 留下 KI#237「三个外部输入槽与第二条同构 ISR」、KI#238「显示队列
真正的驱动路径 / 补映射实验」。V63 先做三件静态更正，再做补映射实验：

1. **向量表**：ROM `0x000EFC00..0x000F0000` 是 256 槽中断向量表，
   槽地址 ``TBR + (255 - v) * 4``（V54 实证 vec 0x18 槽 `0x000EFF9C`）。
   反查得三个门分别挂在 vec 0x26 / 0x2B / 0x2C，另有一条无槽的
   vec 0x3C。⚠️ vec 0x26 的 ISR 序言是 ``stm0/stm1/st...`` **没有
   `enter`**，`Sweep.enclosing()` 会把它归到上一个无关函数（硬规矩 30），
   判 ISR 归属必须先反查向量表 —— 硬规矩 43。
2. **`0x12E6F8` 不是数据对象**：它有 ``st/st/enter`` 序言、39 条函数体、
   312 个 `ldi:32` 加载点（302 条 `call:d` + 10 条 `call`），调用者跨
   `0x12xxxx..0x25xxxx` 共 168 个 enclosing 函数；函数体按 ``r4*12``
   索引 SDRAM 对象池 `0x9F58D508` 并做 `int #0x40`。V62 写的
   「显示模块内部全局对象、引用者全在 0x120xxx」是错的（KI#239）。
3. **状态块写点自举环**：`0x65000000` 六字共 47 个静态可定址写点，
   35 个落在按位派发器的 handler 家族区 —— 「不派发就不写状态位，
   没有状态位就不派发」。

口径纪律：
  * `call rel12` 按 `fr_emu.py:777` 的 ``pc + 2 + sx(v,11)*2``（硬规矩 40）。
  * `call:d` / `call` 的下一条是延迟槽（硬规矩 19）；双调用的两个 r4
    参数就在延迟槽里装，不是死代码。
  * `writers_of()` 只看可定址立即数形态，寄存器间接写看不见
    （硬规矩 25/31）；`0x6B7335B0` 就是这种情形。
  * `Sweep.enclosing()` 只是「最近的 enter」，无 enter 的 ISR 会归错
    （硬规矩 30 / 43）。
"""
from collections import namedtuple, Counter
import struct

#: V54 实证：TBR=0x000EFC00，vec 0x18 槽 = 0x000EFF9C
TBR = 0x000EFC00
VEC_COUNT = 256
DEFAULT_HANDLER = 0x0404F8          # 向量表里 208 个槽填的默认入口

#: V62 的显示链 5 个关键 PC（口径不变）
DSP_PCS = (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752)

#: 被 V62 认错的公共函数
DSP_FN = 0x12E6F8
DSP_FN_END = 0x12E750
DSP_FN_SIZE = 39
DSP_FN_REFS = 312
DSP_FN_CALLD = 302
DSP_FN_OBJ = 0x9F58D508
DSP_FN_OBJ_ALT = 0x9F58D500
DSP_FN_OBJ_STRIDE = 12

STATUS_BASE = 0x65000000
GATE_BASE = 0x6B000000
GATE_SLOTS = (0x6B000020, 0x6B000030, 0x6B000034)
EXTRA_SLOT = 0x6B7335B0
EXTRA_SLOT_REF = 0x1E5CC4

#: 同族门：(槽, 向量, ISR 入口, 门 PC, 判位 mask, 出口, call 次数)
Gate = namedtuple("Gate", "slot vec isr gate_pc mask exits calls")
GATES = (
    Gate(0x6B000020, 0x26, 0x042B2A, 0x042B46, 0x04000000,
         (0x042C36,), 2),
    Gate(0x6B000030, 0x2B, 0x042E1C, 0x042E36, 0x1,
         (0x17171E,), 1),
    Gate(0x6B000034, 0x2C, 0x042E72, 0x042E82, 0x1,
         (0x1A4902,), 2),
    Gate(None, 0x3C, 0x042ECC, None, None,
         (0x1A4A32,), 1),
)

#: 第三个门的双调用目标（两次 call:d 的 rel12 都指向它）
THIRD_DOUBLE = 0x042C36
THIRD_DOUBLE_PCS = (0x042B58, 0x042B66)
THIRD_DOUBLE_RELS = (110, 103)
#: vec 0x2C 的双调用
SIBLING_DOUBLE_PCS = (0x042E9A, 0x042EAC)
SIBLING_TARGET = 0x1A4902
#: 通道控制块表（r4 是通道号）
CHAN_TABLE_80 = 0x00008400
CHAN_TABLE_4 = 0x001BBD30
CHAN_TABLE_4_ALT = 0x001BBD28

Vec = namedtuple("Vec", "vec slot entry")
RefKind = namedtuple("RefKind", "pc imm next")


def vec_slot(vec):
    """向量号 → 槽地址。"""
    return TBR + (255 - vec) * 4


def vec_of_slot(slot):
    """槽地址 → 向量号（V54 公式的反函数）。"""
    return 255 - (slot - TBR) // 4


def vector_table(rom, base, tbr=TBR):
    """解析 256 槽中断向量表 → ``[Vec]``（按 vec 升序）。"""
    out = []
    for v in range(VEC_COUNT):
        slot = tbr + (255 - v) * 4
        off = slot - base
        if off < 0 or off + 4 > len(rom):
            out.append(Vec(v, slot, None))
            continue
        entry = struct.unpack('>I', rom[off:off + 4])[0]
        out.append(Vec(v, slot, entry))
    return out


def gate_rows(table, gates=GATES):
    """核验常量表与真实向量表一致 → ``[(Gate, entry, ok)]``。"""
    d = dict((v.vec, v.entry) for v in table)
    return [(g, d.get(g.vec), d.get(g.vec) == g.isr) for g in gates]


def decode_range(rom, base, lo, hi):
    """线性反汇编 `[lo, hi)`（硬规矩 21），复用 V62 的实现。"""
    from frbox import dispatch62
    return dispatch62.decode_range(rom, base, lo, hi)


def is_prologue(insns, max_scan=8):
    """判一段开头是不是函数/ISR 序言。

    两种形态：
      * 有 `enter`：``st/st/enter`` 或 ``stm0/stm1/.../enter``；
      * 无 `enter` 的 ISR：``stm0/stm1/st...`` 直接开跑（vec 0x26）。

    ⚠️ 扫描窗口要够长：vec 0x2B 的序言是
    ``stm0/stm1/st/st/st/enter``，`enter` 在**第 6 条**；初版
    `max_scan=5` 会把它误判成无 `enter` 的 ISR。8 条覆盖两种实证形态，
    又不至于把函数体里偶然出现的 `enter` 算进来。
    """
    mns = [x.mn for x in insns[:max_scan]]
    if 'enter' in mns:
        return 'enter'
    if mns and mns[0] in ('stm0', 'stm1') and any(
            m.startswith('st') for m in mns[1:]):
        return 'no-enter-isr'
    return None


def delayed_args(rom, base, insns, reg=4):
    """`call:d` 延迟槽里 `ldi #N, rN` 的参数 → ``[(call_pc, value)]``。

    `insns` 必须是包含 call 及其后一条指令的连续线性反汇编结果。
    """
    out = []
    for i, x in enumerate(insns[:-1]):
        if x.mn not in ('call:d', 'call'):
            continue
        nxt = insns[i + 1]
        if nxt.mn.startswith('ldi') and nxt.vals.get('ri') == reg:
            v = nxt.vals.get('i8')
            if v is None:
                v = nxt.vals.get('i20')
            if v is None:
                v = nxt.vals.get('i32')
            out.append((x.pc, v))
    return out


def ref_kinds(sweep, addr, window=0):
    """`refs_to()` 的加载点按下一条指令形态分类 + 64KB 段分布。

    返回 ``(list[RefKind], Counter(next), Counter(pc>>16))``。
    """
    raw = sweep.refs_to(addr, window=window)
    refs = [RefKind(pc, imm, nxt) for pc, imm, nxt in raw]
    return (refs,
            Counter(str(r.next) for r in refs),
            Counter(r.pc >> 16 for r in refs))


def status_writer_buckets(sweep, addrs, lo=0x1714EE, hi=0x172400):
    """状态字写点归类 → ``(全部写点, handler 区内数量, 其它数量)``。

    `sweep.writers_of()` 只覆盖可定址立即数形态（硬规矩 25/31）。
    """
    allw = sorted(set(pc for a in addrs for pc, _mn in sweep.writers_of(a)))
    inside = [p for p in allw if lo <= p < hi]
    return allw, len(inside), len(allw) - len(inside)


def imm_refs(insns, lo, hi):
    """区间内落在 `[lo, hi)` 的可定址立即数引用（复用 V62）。"""
    from frbox import dispatch62
    return dispatch62.imm_refs(insns, lo, hi)
