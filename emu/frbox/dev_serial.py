# -*- coding: utf-8 -*-
"""FR 串口（UART）模型 —— 直接页 0x60–0x67。

已知事实（来自对 1.11 固件的实测，见报告 V3）：
  * 串口中断 0x1B 的 ISR 在 0x0012FDB4，它先读 0x60，`(值 & 0xE0) == 0` 就直接返回；
    非零时再读 0x62，然后 `sys_set_flg(flag=3, pattern=0x20)`。
  * 0x60 = 0 时走另一支，读 0x61。
  * 直接页 0x64 在固件里没有任何访问（视为未映射）。

由上述行为反推的寄存器布局（**属推断**，厂商未公开）：
  0x60 SSR  状态：bit7 ORE 溢出 / bit6 RDRF 收到数据 / bit5 TDRE 发送就绪
  0x61 TDR  发送数据（写即发）
  0x62 RDR  接收数据（读即取走一个字节）
  0x63 SCR  控制（bit1 TXE / bit0 RXE）
  0x64      —— 未映射
  0x65 SMR  模式（波特率/时钟源，只有符号意义）
  0x66/0x67 波特率分频（低/高字节）

只所以把 bit5–7 当作「事件汇总」，是因为 ISR 用的就是掩码 0xE0 一次判完；
三个位各自的名字是推断，掩码本身是实测。
"""

from collections import deque

from .device import Device


class SerialIf(Device):
    name = "serial"

    SSR = 0x00
    TDR = 0x01
    RDR = 0x02
    SCR = 0x03
    SMR = 0x05
    BRGL = 0x06
    BRGH = 0x07

    ORE = 0x80
    RDRF = 0x40
    TDRE = 0x20

    def __init__(self, machine=None, irq_line=None, tx_sink=None, rx_capacity=4096):
        self.irq_line = irq_line
        self.tx_sink = tx_sink
        self.rx_capacity = rx_capacity
        self.tx = bytearray()
        self.rx = deque()
        self.ssr = self.TDRE
        self.scr = 0x00
        self.smr = 0x00
        self.brg = 0x0000
        self.overruns = 0
        self.tx_count = 0
        self.rx_count = 0
        self.open_bus_reads = 0
        super().__init__(machine)

    def reset(self):
        self.tx = bytearray()
        self.rx.clear()
        self.ssr = self.TDRE
        self.scr = 0
        self.smr = 0
        self.brg = 0
        self.overruns = 0
        self.tx_count = 0
        self.rx_count = 0
        self.open_bus_reads = 0

    # ---- 外部接口（给测试/监视器注入数据）----
    def push_rx(self, data):
        """往接收队列塞字节，并像真硬件一样置 RDRF、拉中断线"""
        if isinstance(data, int):
            data = bytes([data & 0xFF])
        for b in data:
            if len(self.rx) >= self.rx_capacity:
                self.overruns += 1
                self.ssr |= self.ORE
                continue
            self.rx.append(b)
            self.rx_count += 1
        if self.rx:
            self.ssr |= self.RDRF
            self.raise_irq(self.irq_line)
        self.dbg(f"注入接收 {len(data)} 字节，队列 {len(self.rx)}")

    def pop_tx(self):
        out = bytes(self.tx)
        self.tx.clear()
        return out

    # ---- 总线接口 ----
    def read(self, off, size):
        if off == self.SSR:
            self.dbg(f"读 SSR = 0x{self.ssr:02X}")
            return self.ssr
        if off == self.RDR:
            if self.rx:
                b = self.rx.popleft()
                if not self.rx:
                    self.ssr &= ~self.RDRF
                self.dbg(f"读 RDR = 0x{b:02X}（剩 {len(self.rx)}）")
                return b
            self.dbg("读 RDR 但队列空")
            return 0x00
        if off == self.SCR:
            return self.scr
        if off == self.SMR:
            return self.smr
        if off == self.BRGL:
            return self.brg & 0xFF
        if off == self.BRGH:
            return (self.brg >> 8) & 0xFF
        self.open_bus_reads += 1
        self.dbg(f"读未映射寄存器 0x{0x60 + off:02X}")
        return 0x00

    def write(self, off, size, val):
        val &= 0xFF
        if off == self.TDR:
            self.tx.append(val)
            self.tx_count += 1
            if self.tx_sink is not None:
                self.tx_sink(bytes([val]))
            self.dbg(f"发送 0x{val:02X} ({chr(val)!r})" if 32 <= val < 127 else f"发送 0x{val:02X}")
            return
        if off == self.SCR:
            self.scr = val
            self.dbg(f"SCR ← 0x{val:02X}")
            return
        if off == self.SMR:
            self.smr = val
            self.dbg(f"SMR ← 0x{val:02X}")
            return
        if off == self.BRGL:
            self.brg = (self.brg & 0xFF00) | val
            return
        if off == self.BRGH:
            self.brg = (self.brg & 0x00FF) | (val << 8)
            return
        self.dbg(f"写未映射寄存器 0x{0x60 + off:02X} ← 0x{val:02X}")

    # ---- 视图 ----
    def state(self):
        return {
            "SSR": f"0x{self.ssr:02X}",
            "TX": f"{self.tx_count} 字节",
            "RX": f"{len(self.rx)} 待取 / 累计 {self.rx_count}",
            "SCR": f"0x{self.scr:02X}",
        }
