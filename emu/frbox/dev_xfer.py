# -*- coding: utf-8 -*-
"""「置启动位 → 轮询就绪位」型状态寄存器。

A 段固件里有若干处按这个套路握手（全是同构的）：

    0x000DCD26  ldi:20 r0, #0xAF
    0x000DCD2A  borl  #2,  @r0     ; 置 bit1 = 启动
    0x000DCD2C  ldi:20 r0, #0xAF
    0x000DCD30  btsth #8,  @r0     ; 测 bit7
    0x000DCD32  beq   0x000DCD2C   ; bit7 没起来就一直等

真机上「就绪位」是外部器件（或 DMA 引擎）在传输完成后置的。仿真里没有那个
器件，读回来的永远是最后一次写进去的值，于是循环永不退出 —— 这不是固件的问题，
是我们少了一个会把完成位置起来的东西。

这里的建模是**近似**：写进「启动位」之后过 N 个 tick 把「完成位」置上。
N 取一个较小的偶数，目的是让它在人眼可忽略的时间里完成，同时保留「先忙后完成」
的时序特征（这样靠时序判错的固件逻辑仍然会被判错）。

⚠️ 这是模型化假设，不是逆向确认的寄存器语义。三条证据只是「谁被轮询」与
   「哪一位被置起来启动」，寄存器名与内部结构未知。见 KNOWN_ISSUES 第 37 条。
"""

from .device import Device


class XferHandshake(Device):
    """一组「启动位 / 完成位」寄存器。"""

    name = "xfer"

    def __init__(self, machine=None, table=None, delay=64):
        # table: {offset: (start_mask, done_mask)}
        self.table = dict(table or {})
        self.delay = delay
        self.regs = {}
        self.busy = {}            # offset -> 剩余 tick
        self.starts = 0
        self.completions = 0
        super().__init__(machine)

    def reset(self):
        self.regs.clear()
        self.busy.clear()
        self.starts = 0
        self.completions = 0

    # ---- 总线 ----
    def read(self, off, size):
        v = self.regs.get(off, 0) & 0xFF
        return v

    def write(self, off, size, val):
        b = val & 0xFF
        sm, dm = self.table.get(off, (0, 0))
        self.regs[off] = b
        if sm and (b & sm):
            # 置了启动位：把完成位清掉，过 delay 个 tick 再置起来
            self.regs[off] = b & ~dm & 0xFF
            self.busy[off] = self.delay
            self.starts += 1
            self.dbg(f"0x{off:02X} 启动（启动位 0x{sm:02X}），{self.delay} tick 后置完成位 0x{dm:02X}")

    # ---- 时基 ----
    def tick(self, cycles):
        if not self.busy:
            return
        done = []
        for off, left in self.busy.items():
            left -= cycles
            if left <= 0:
                dm = self.table.get(off, (0, 0))[1]
                self.regs[off] = (self.regs.get(off, 0) | dm) & 0xFF
                self.completions += 1
                done.append(off)
            else:
                self.busy[off] = left
        for off in done:
            del self.busy[off]

    # ---- 调试 ----
    def state(self):
        out = {"启动": self.starts, "完成": self.completions}
        if self.busy:
            out["进行中"] = ",".join("0x%02X" % o for o in self.busy)
        return out


# A 段实测到的三处忙等：地址 / (启动位, 完成位)
#   0x0AF  0x0DCD2A 置 bit1，0x0DCD30 测 bit7
#   0x174  0x09C976 置位后 0x09C97C 测 bit6
#   0x524  0x0CE132 置 bit1，0x0CE134 测 bit6（寄存器在 r8，动态）
# 后两处的启动位在反汇编里不是紧邻的 borl，这里按「任意写都可能启动」处理：
# start_mask=0 表示这个偏移不建模启动，只由外部显式 set_ready。
D300_A_TABLE = {
    0x0AF: (0x02, 0x80),
    0x174: (0x02, 0x40),
    0x524: (0x02, 0x40),
}


def attach(machine, table=None, delay=64):
    dev = XferHandshake(machine, table if table is not None else D300_A_TABLE, delay)
    for off in sorted(dev.table):
        machine.dp.attach(off, off + 1, dev, base=off)
    machine.xfer = dev
    return dev
