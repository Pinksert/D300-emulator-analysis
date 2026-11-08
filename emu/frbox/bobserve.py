# -*- coding: utf-8 -*-
"""只读观察钩子（V55）—— 只统计，不改任何返回值、不写内存、不动时基。

仿真台里三层「补/看」手段必须分开，不能混称：

    frbox.dev_tick18   补硬件拉线：周期 raise IRQ，不写内存（V54）
    frbox.bpatch       反事实打桩：改指定 (addr,pc) 上 ld 的**读返回值**（V53）
    frbox.bobserve     纯观察：只把读写记下来，**返回值原样透传**（V55）

V53/V54 的探针里各写了一份「包 bus.rd 统计读值」的临时代码，V55 收成这里，
并明确一个容易踩的顺序问题：

    m = blive.boot(0)
    reads, writes = bobserve.observe(m.bus, (PIO, EVT, ...))   # 先装
    hits = bpatch.patch_read(m.bus, rules)                     # 后装

这样 `observe` 的 `orig_rd` 是未打桩的，记录的是**器件真实读值**；
而固件看到的是 `patch_read` 改过的值。两者分开才敢说「原始值恒 0x12，
是我们把 bit3 读成 1」。反过来装，观察日志里会混进打桩后的假值。

⚠️ 一台机器只装一次；重复装会套两层，计数翻倍。与硬规矩 13 同理。
⚠️ 命中 0 次也是结论（V53 G3 的 0 命中就是硬证据）。
"""

import collections

__all__ = ["observe", "bit_samples", "value_counts"]


def observe(bus, addrs=()):
    """在 bus 上装纯观察钩子。

    返回 ``(reads, writes)``：
      * ``reads[(addr, pc)]`` → ``Counter(原始读值)``
        （pc 为 0 表示该读不带 PC，例如位操作 RMW 的内部读，见硬规矩 16）
      * ``writes`` → ``(addr, pc, a, n, v)`` 列表，a/n 是这次写的实际地址/宽度

    `addrs` 是关心的地址集合；一次读写若跨到多个目标地址，会各记一笔。
    """
    targets = tuple(addrs)
    orig_rd, orig_wr = bus.rd, bus.wr
    reads = collections.defaultdict(collections.Counter)
    writes = []

    def rd(a, n, pc=0):
        v = orig_rd(a, n, pc)          # 先取真值
        for t in targets:
            if a <= t < a + n:
                reads[(t, pc)][v] += 1
        return v                       # 原样透传，绝不修改

    def wr(a, n, v, pc=0):
        for t in targets:
            if a <= t < a + n:
                writes.append((t, pc, a, n, v))
        return orig_wr(a, n, v, pc)    # 原样透传

    bus.rd, bus.wr = rd, wr
    return reads, writes


def value_counts(reads, addr):
    """某个被观察地址的所有原始读值汇总（不分 PC）。"""
    out = collections.Counter()
    for (a, _pc), cnt in reads.items():
        if a == addr:
            out.update(cnt)
    return out


def bit_samples(reads, addr, mask):
    """返回 ``(总采样数, 该位为 1 的采样数)``，按原始读值统计。

    用「某一位到底有没有被固件/器件置过 1」这类问题，避免把「打了桩之后
    读到 1」错当成「固件置了 1」。
    """
    total = set_count = 0
    for v, c in value_counts(reads, addr).items():
        total += c
        if v & mask:
            set_count += c
    return total, set_count
