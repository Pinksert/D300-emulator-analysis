# -*- coding: utf-8 -*-
"""最高优先级编码器：写 0x3F4（位图）→ 读 0x3FC（优先级号）。

调度器 0x0FE830 那几条的实测访问序列：

    0x0FE830  ldi:32 #0x45984, r12
    0x0FE836  ld     @r12, r13        ; r13 = 就绪位图
    0x0FE838  ldi:20 #0x3F4,  r14
    0x0FE83C  st     r13, @(r14,0)    ; W 0x003F4 ← 位图（4 字节）
    0x0FE83E  ld     @(r14,8), r13    ; R 0x003FC → 结果（4 字节）
    0x0FE840  add    #4, r12          ; r12 = 0x45988 就绪队列数组
    0x0FE842  lsl    #3, r13          ; 结果 × 8 = 队列节点大小
    0x0FE844  ld     @(r12,r13), r13  ; 取该优先级的队首 TCB

结果必须等于"最高优先级号"，否则下标必然越界：就绪队列数组只有 13 条
（优先级 0–12），就绪位图的置位规律是 bit(31 − 优先级)。于是

    结果 = 前导零个数 = 31 − 最高置位下标 = 32 − bit_length

实测那一次位图 = 0x50800000，置位 bit30/28/23（优先级 1/3/8），
前导零 = 1 → 取优先级 1 的队首，正好是任务 5（TCB 0x045AAC）。

⚠️ 这是从"写什么、读什么、读完怎么用"三点唯一确定出来的语义，寄存器名
   与真实内部结构未知。它和 dev_dispatch.py 的 0x44 是同一条推断链上的
   两环：少任何一环，调度器都会读到 0 号（空）优先级。
   见 KNOWN_ISSUES 第 43 条。

实现注意：直接页是按**字节**路由的（DirectPage.read/write 逐字节转发），
所以这里开一个 0x3F4–0x400 的窗口，自己按大端拼装。
"""

from .device import Device

PRIO_IN = 0x3F4        # 写：32 位位图（占 0x3F4–0x3F7）
PRIO_OUT = 0x3FC       # 读：最高优先级号（占 0x3FC–0x3FF）
PRIO_WIN = 0x3F4       # 窗口起点
PRIO_END = 0x400
NPRIO = 13             # 就绪队列条数（优先级 0–12）
PRIO_BASE = 0x45988    # 就绪队列数组（每条 8 字节：next, prev）


class PrioEncoder(Device):
    """写位图进 0x3F4，从 0x3FC 读回"最高优先级号"（= 前导零个数）。"""

    name = "prio"

    def __init__(self, machine=None):
        self.buf = [0, 0, 0, 0]      # 位图的 4 个字节（大端）
        self.last_in = None
        self.last_out = 0
        self.lookups = 0
        self.empty = 0               # 位图为 0 的次数（不该发生）
        super().__init__(machine)

    def reset(self):
        self.buf = [0, 0, 0, 0]
        self.last_in = None
        self.last_out = 0
        self.lookups = 0
        self.empty = 0

    @staticmethod
    def clz(v):
        """32 位前导零个数；全 0 返回 32。"""
        v &= 0xFFFFFFFF
        return 32 - v.bit_length()

    # ---- 总线（按字节） ----
    def read(self, off, size=1):
        i = off - (PRIO_OUT - PRIO_WIN)          # 0..3
        if 0 <= i < 4:
            return (self.last_out >> (8 * (3 - i))) & 0xFF
        j = off                                   # 0x3F4 口
        if 0 <= j < 4:
            return self.buf[j] & 0xFF
        return 0

    def write(self, off, size=1, val=0):
        if 0 <= off < 4:                          # 0x3F4 口：位图
            self.buf[off] = val & 0xFF
            if off != 3:
                return          # 直接页逐字节转发，等最后一个字节落地再结算
            v = ((self.buf[0] << 24) | (self.buf[1] << 16)
                 | (self.buf[2] << 8) | self.buf[3])
            self.last_in = v
            r = self.clz(v)
            if v == 0:
                self.empty += 1
            self.last_out = r
            self.lookups += 1
            self.dbg(f"0x3F4 ← 0x{v:08X} → 最高优先级 {r}"
                     + (f"（队首 TCB 0x{self._head(r):06X}）"
                        if self.machine is not None and r < NPRIO else "  ← 越界！"))
        # 写 0x3FC 口没意义（结果只读），忽略

    def _head(self, p):
        return self.machine.bus.rd(PRIO_BASE + p * 8, 4)

    # ---- 调试 ----
    def state(self):
        out = {"查询次数": self.lookups}
        if self.last_in is not None:
            out["上次位图"] = "0x%08X" % self.last_in
            out["上次结果"] = self.last_out
        if self.empty:
            out["位图为 0 的次数"] = self.empty
        return out


def attach(machine):
    dev = PrioEncoder(machine)
    # base 是加在访问偏移上的常数，要相对偏移就必须传 0（与 dev_xfer 同一个坑）
    machine.dp.attach(PRIO_WIN, PRIO_END, dev, base=0)
    machine.prio = dev
    return dev
