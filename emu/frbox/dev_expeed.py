# -*- coding: utf-8 -*-
"""dev_expeed.py —— B ↔ EXPEED 位串行链路的**器件侧**模型（V28）。

V27 只做了「听」（ExpeedTap：抓主机写出去的位解成字节）。这一版补上「答」：
主机读 0x7000000A 时，器件必须把数据位驱动在 bit2 上 —— 否则读回恒 0。

引脚（实测，见 bexpeed.py 头注释）：

    CS   = 0x70000002 bit2   1 = 选中
    CLK  = 0x7000000A bit1   上升沿采/发一位
    DATA = 0x7000000A bit2   主机写=主机驱动，主机读=器件驱动
    命令字节 = 寄存器号 | 读标志 0x80，LSB 先

时序（实测 0x19EF0C–0x19EF5E 的位引擎）：

    读-改-写 0x7000000A  → 置/清 DATA → CLK 拉高（0x19EF2A）
    → 调延时 0x125A02 → `btstl #4` 采 bit2

也就是说**采样发生在 CLK 上升沿之后**，所以器件必须在上升沿那一刻就把
数据位摆好 —— 本模块在 `_on_clk_rise()` 里 pop 一位到 `_din`。

边界（重要）：
  * 寄存器内容**没有实证**。默认全 0；给的 5 字节读长是从「主机读一次
    打了 40 个时钟」反推来的，不是器件手册数据。
  * 只建模位串行本身，不建模 EXPEED 的时序要求（忙等、应答延迟）。
  * 写命令的数据相位没有样本（整场 0 条写命令），所以写路径只记账不生效。
"""

from .device import Device


class ExpeedLink(Device):
    """0x70000000 窗口的位串行从机。"""

    name = "expeed"

    CS_OFF = 0x02
    BUS_OFF = 0x0A
    CS_BIT = 0x04          # bit2
    CLK_BIT = 0x02         # bit1
    DATA_BIT = 0x04        # bit2
    READ_FLAG = 0x80

    def __init__(self, machine=None, base=0x70000000, regs=None, read_len=5):
        # Device.__init__ 会调 reset()，所以字段必须先就位（V23 的坑）
        self.base = base
        self.read_len = read_len
        self.regs = dict(regs) if regs else {}
        super().__init__(machine)

    # ---------------------------------------------------------------- 生命周期
    def reset(self):
        self._cs = 0
        self._clk = 0
        self._dout = 0
        self._din = 0
        self._shift = 0
        self._nbits = 0
        self._collect = False
        self._tx = []
        self._payload = None       # 写命令之后的载荷字节（只记账）
        self.cmds = []             # [{'kind','reg','raw'}]
        self.bits_out = 0
        self.bits_in = 0
        self.payload_bytes = []

    # ---------------------------------------------------------------- 寄存器
    def set_reg(self, reg, data):
        """写一个寄存器（int 或 bytes）。"""
        if isinstance(data, int):
            data = bytes([data & 0xFF])
        self.regs[reg] = bytes(data)

    def reg_bytes(self, reg, n=None):
        n = self.read_len if n is None else n
        v = self.regs.get(reg, b"")
        if isinstance(v, int):
            v = bytes([v & 0xFF])
        if len(v) < n:
            v = v + b"\x00" * (n - len(v))
        return v[:n]

    def _response_bits(self, reg):
        out = []
        for byte in self.reg_bytes(reg):
            for k in range(8):
                out.append((byte >> k) & 1)      # LSB 先
        return out

    # ---------------------------------------------------------------- 总线
    def read(self, off, size):
        if off == self.BUS_OFF:
            v = (self._clk << 1) | (self._din << 2)
            return v & 0xFF if size == 1 else v
        if off == self.CS_OFF:
            return (self._cs << 2) if size == 1 else (self._cs << 2)
        return 0

    def write(self, off, size, val):
        if off == self.CS_OFF:
            cs = 1 if (val & self.CS_BIT) else 0
            if cs and not self._cs:                 # CS 上升沿 = 新一帧
                self._begin_frame()
            self._cs = cs
            return
        if off == self.BUS_OFF:
            clk = 1 if (val & self.CLK_BIT) else 0
            # DATA 是**粘滞**的：主机先单独写一次 DATA（0x04 / 0x00），
            # 再用 0x02 打时钟脉冲 —— 那一笔不携带 DATA 信息，不能把 DATA 清掉。
            # 判据与 V27 的抓包器一致：v&4 → 置 1；(v&0x0A)==0 → 清 0；
            # 只写 CLK 位（0x02）时 DATA 保持不变。实测只有这个规则能解出 0x86。
            if val & self.DATA_BIT:
                self._dout = 1
            elif (val & (self.CLK_BIT | self.DATA_BIT)) == 0:
                self._dout = 0
            if clk and not self._clk:
                self._on_clk_rise()
            self._clk = clk
            return

    # ---------------------------------------------------------------- 位引擎
    def _begin_frame(self):
        # 读命令之后的数据相位会落在**下一个 CS 窗口**里（实测：命令 16 位一帧、
        # 数据 48 位一帧）。所以还有没发完的应答位时，不能把状态重置掉。
        if self._tx:
            return
        self._shift = 0
        self._nbits = 0
        self._collect = True
        self._payload = None

    def _on_clk_rise(self):
        if self._collect:
            self._shift |= (self._dout & 1) << self._nbits
            self._nbits += 1
            self.bits_in += 1
            if self._nbits == 8:
                self._finish_byte()
            return
        # 读数据相位：把器件的数据位摆到总线上
        if self._tx:
            self._din = self._tx.pop(0)
        else:
            self._din = 0
        self.bits_out += 1

    def _finish_byte(self):
        b = self._shift & 0xFF
        self._shift = 0
        self._nbits = 0
        if self._payload is not None:              # 写命令的载荷
            self._payload.append(b)
            self.payload_bytes.append(b)
            return
        if b & self.READ_FLAG:
            reg = b & 0x7F
            self.cmds.append({"kind": "read", "reg": reg, "raw": b})
            self._collect = False
            self._tx = self._response_bits(reg)
        else:
            self.cmds.append({"kind": "write", "reg": b, "raw": b})
            self._payload = []
            self._collect = False

    # ---------------------------------------------------------------- 视图
    def state(self):
        return {
            "cs": self._cs,
            "clk": self._clk,
            "cmds": len(self.cmds),
            "bits_out": self.bits_out,
        }

    def summary(self):
        return {
            "cmds": list(self.cmds),
            "bits_in": self.bits_in,
            "bits_out": self.bits_out,
            "payload": list(self.payload_bytes),
        }
