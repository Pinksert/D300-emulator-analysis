# -*- coding: utf-8 -*-
"""taskboot.py —— 把任务表里的「复位就激活」位打开（V16）

REALOS 的任务表在 A 段 ROM 里（`0xFE010` 起每 `0x1C` 一条，共 13 条），每条记录
`+0x0C` 是状态字：最高位 `0x80000000` = **复位时就激活**。真实固件里只有任务
1 / 5 / 0（空闲）这一位是 1，其余 7 个都是 0 —— 它们要等运行时 `sta_tsk` 才被建起来。

V16 的做法不是猜那条 `sta_tsk` 在哪，而是**直接把这一位置 1**，让 REALOS 自己的
初始化去创建它。于是：

    · 能起来 → 说明任务本身和它的栈都是好的，缺的只是「谁叫它」
    · 起不来 / 起来就退出 → 也是结论（它的第一句就在等一个握手位）

⚠️ 两个必须写进报告的边界：

    ① ROM 区 `bus.wr()` 写不进去（实测），只能改 `bus.rom` 数组 —— 这是仿真器的
       特权，不是固件行为；
    ② 这是**改配置**做的实验：真实相机开机的任务集合不含这 7 个。不能说成
       「相机开机就会跑这些任务」。改完必须 `reset()` 再启动 REALOS 才生效。

`activate()` 之后 `records()` 会立刻反映（它读的是同一块内存），不用等 reset。
"""
ACTIVE_BIT = 0x80000000
STATE_OFF = 0x0C


def records(m):
    """借 realos 的记录读法（避免两处各写一份布局）。"""
    from . import realos
    return realos.records(m)


def record(m, tid):
    for r in records(m):
        if r["id"] == tid:
            return r
    return None


def is_active(m, tid):
    r = record(m, tid)
    return bool(r and (r["state"] & ACTIVE_BIT))


def dormant(m):
    """当前 state 最高位为 0 的任务号（= 开机不会被创建的）。"""
    return [r["id"] for r in records(m) if not (r["state"] & ACTIVE_BIT)]


def activate(m, tids, on=True):
    """改任务表状态字的最高位。返回真正改动的任务号列表。

    `tids` 可以给 `"all"` / `"dormant"` / 可迭代的任务号。
    """
    if tids in ("all", "dormant"):
        pool = [r["id"] for r in records(m)] if tids == "all" else dormant(m)
    else:
        pool = list(tids)
    base = m.bus.rom_base
    done = []
    for tid in pool:
        r = record(m, tid)
        if not r:
            continue
        off = r["addr"] + STATE_OFF - base
        if not (0 <= off < len(m.bus.rom) - 3):
            continue
        cur = int.from_bytes(m.bus.rom[off:off + 4], "big")
        new = (cur | ACTIVE_BIT) if on else (cur & ~ACTIVE_BIT & 0xFFFFFFFF)
        if new == cur:
            continue
        m.bus.rom[off:off + 4] = new.to_bytes(4, "big")
        done.append(tid)
    return done
