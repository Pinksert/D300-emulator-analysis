# -*- coding: utf-8 -*-
"""位操作立即数的真语义 —— **掩码**，不是位号（V15 结案）

FR30 的 `BANDL / BORL / BEORL / BTSTL #u4, @Ri` 里 u4 是「**低半字节的 4 位掩码**」；
`...H` 版本是「**高半字节掩码**」（实际掩码 = u4 << 4）。所以：

    btstl #0x2   → 掩码 0x02 → 测 bit1
    btstl #0x8   → 掩码 0x08 → 测 bit3
    btsth #0x1   → 掩码 0x10 → 测 bit4
    btsth #0x8   → 掩码 0x80 → 测 bit7

反汇编里显示的 `#N` **一直是掩码值，不是位号**。V14 记为「`btstl #2` 却是 bit1、
映射规则未解」，其实就是这一条 —— 不是芯片怪癖，是我们把立即数读成了位号。

线索：`borh #0x8` 出现在「把 0x80 写到某个状态字节」的场合（`0x0AD672`），
`bandl #0xD` 出现在「把低半字节 3 个位一起清/置」的场合（`0x0AD63E`）——
只有掩码读法能自洽。
"""

__all__ = ["mask", "bits", "bit", "nibble", "describe"]

_HIGH = ("bandh", "borh", "beorh", "btsth")


def nibble(mn):
    """'high' / 'low' —— 判这条指令动的是高半字节还是低半字节。"""
    return "high" if mn in _HIGH else "low"


def mask(mn, imm):
    """立即数 → 真正的字节掩码。"""
    m = int(imm) & 0xF
    return (m << 4) & 0xFF if mn in _HIGH else m


def bits(mn, imm):
    """立即数 → 被操作的位号列表（0..7）。"""
    m = mask(mn, imm)
    return [i for i in range(8) if m & (1 << i)]


def bit(mn, imm):
    """单一一位时返回位号，多位置 1 时返回 None。"""
    b = bits(mn, imm)
    return b[0] if len(b) == 1 else None


def describe(mn, imm):
    """给人看的写法：`btsth #0x1` → `btsth #1 (mask 0x10, bit4)`。"""
    m = mask(mn, imm)
    b = bits(mn, imm)
    where = ",".join("bit%d" % x for x in b) if b else "无"
    return "%s #%d (mask 0x%02X, %s)" % (mn, int(imm) & 0xF, m, where)
