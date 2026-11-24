# -*- coding: utf-8 -*-
"""svcnum.py —— 服务号口径（V94）：**按装载宽度解码**，不要一律 `& 0xFF`。

## 这个模块解决什么

REALOS 的服务号放在 `r12`，但**装法有两种**，两种的补码宽度不同：

    0x1A4BE0  ldi:8  #0xD2, r12      ; r12 = 0x000000D2 → −46（wai_flg）
              int    #0x40
    0x1A4BE2  ldi:32 #0xFFFFFF56,r12 ; r12 = 0xFFFFFF56 → −170（twai_flg）
              int    #0x40

V48 起的 `bstatic` 只用 `sx8(v & 0xFF)`：8 位那条对，32 位那条把 −170 读成 **+86**。
V92 在判据侧打了个补丁（`tick92.fix_wide`，`{86: −170, 85: −171}`），
但它只覆盖「判某个点是否在等待之后」，`CallSite.svc` 本身还是错的 ——
于是所有「按服务号筛等待点」的静态统计都少算了宽服务号那一类。

## 实测（V94，全 ROM 2297 个 `int #0x40`）

| 装法 | 点数 | r12 值域 |
|---|---|---|
| `ldi:8` | 2189 | 全在 `0x95..0xF9`，**最高位全 1** ⇒ 8 位补码 |
| `ldi:32` | 93 | 91 个 `0xFFFFFF56`、2 个 `0xFFFFFF55` ⇒ 32 位补码 |
| 未定（寄存器/参数） | 15 | `None` |

**没有一例歧义**：8 位装法全部 `bit7=1`，32 位装法全部 `≥0xFFFFFF00`。
所以按宽度解码不需要「猜」，只取决于上一条写 r12 的指令是哪种 `ldi`。

## 边界

* `ldi:20` 在这份固件里**没出现过**（0 点）。这里仍按 20 位补码实现，
  是因为指令集里有这种形态；不要拿它去解释现存的任何一个调用点。
* 15 个「未定」点（`r12` 来自 `mov` / 参数）本模块**不解**，仍返回 `None`；
  它们集中在 `0x1BBFE2` / `0x1BCABE` / `0x1C3AAA` 等库函数里，未查。
* `of_why()` 读的是 `TCB+0x20` 高半（16 位）。`btick.sx8` 那套 `& 0xFF` 的写法
  在 `0xFF56` 上会读成 +86 —— 同一个坑的另一处入口。
"""

#: V92 在判据侧用过的对照表（旧的 8 位口径 → 正确的 32 位值）。
#: V94 之后 `bstatic` 直接给对的，这张表只用于**跨版本对照**。
LEGACY_MISREAD = {86: -170, 85: -171}


def sx8(v):
    """低 8 位补码。"""
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def sx16(v):
    """低 16 位补码（`TCB+0x20` 高半就是这个宽度）。"""
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def sx32(v):
    """32 位补码。"""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def of_reg(v, width):
    """寄存器里的服务号：按**上一条 ldi 的宽度**取补码。

    `width` 取 8 / 16 / 20 / 32；`None` 退回 8 位（历史口径，只用于兜底）。
    """
    if v is None:
        return None
    if width is None:
        return sx8(v)
    if width == 20:
        x = v & 0xFFFFF
        return x - 0x100000 if x & 0x80000 else x
    if width == 8:
        return sx8(v)
    if width == 16:
        return sx16(v)
    if width == 32:
        return sx32(v)
    raise ValueError("width must be 8/16/20/32, got %r" % (width,))


def of_why(raw):
    """`TCB+0x20` 高半 → 服务号。"""
    return sx16(raw)


def is_legacy_misread(svc):
    """是不是 V94 之前那种 8 位口径的误读值（+86 / +85）。"""
    return svc in LEGACY_MISREAD
