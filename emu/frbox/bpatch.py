# -*- coding: utf-8 -*-
"""V53：只读打桩设施（反事实）。

只改「指定 (地址, PC) 上某条 ld 的**读返回值**」，不写内存、不改设备
状态、不动 CPU 时基。用来回答「如果这一位当时是 1，控制流会怎么走」。

为什么不用 `bus.io_value` 打桩：那只对外设（addr < 0x1000）生效，而 V53
要打桩的事件位图在 RAM（`0x9F805AD0`）、轮询寄存器在 `0x50000107`，
两者的读都要经过 `bus.rd()`。

⚠️ 这是**反事实**工具，不是固件行为。结果里必须写清打桩了哪几条指令、
命中多少次 —— 命中 0 次本身就是结论（那条指令根本没执行过）。
"""

import collections

__all__ = ["bit_clear", "bit_set", "patch_read", "patch_write"]


def bit_clear(mask):
    """返回一个纯函数：把 mask 里的位清 0。"""
    inv = (~mask) & 0xFFFFFFFF
    return lambda v: v & inv


def bit_set(mask):
    """返回一个纯函数：把 mask 里的位置 1。"""
    return lambda v: v | mask


def patch_read(bus, rules, watch=(), log=None):
    """给 bus 装一层只读打桩。

    rules : {(addr, pc): fn(v)->v} —— 只有精确匹配 (地址, PC) 才打桩
    watch : 需要记录的 (addr, pc) 集合；log 不为 None 时追加
            `(pc, addr, n, 打桩前的值)`
    log   : list 或 None

    返回 Counter，键 (addr, pc)，值为命中次数。
    """
    orig = bus.rd
    hits = collections.Counter()
    watch = set(watch)

    def rd(a, n, pc=0):
        v = orig(a, n, pc)
        key = (a, pc)
        if key in watch and log is not None:
            log.append((pc, a, n, v))
        fn = rules.get(key)
        if fn is not None:
            hits[key] += 1
            v = fn(v) & 0xFFFFFFFF
        return v

    bus.rd = rd
    return hits


def patch_write(bus, addrs, log):
    """记录落在 addrs 里任一地址的写（含跨边界写），不改写入值。"""
    orig = bus.wr
    targets = tuple(addrs)

    def wr(a, n, v, pc=0):
        for t in targets:
            if a <= t < a + n:
                log.append((pc, a, n, v))
                break
        return orig(a, n, v, pc)

    bus.wr = wr
