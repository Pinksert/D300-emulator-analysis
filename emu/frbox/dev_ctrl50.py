# -*- coding: utf-8 -*-
"""dev_ctrl50.py —— `0x5000_0000` 窗口：三通道位串行控制块（V45）。

## 这块地址是什么（证据）

V44 把它笼统记成「外设窗口，80 笔写、读 `0x50000105` 最频繁」。V45 把每一笔
访问拆到寄存器级（`emu/probe/exp_bctrl.py`、`results/frbox/bctrl.txt`）之后，
证据指向**软件打时钟的位串行口**，三组，每组 16 字节：

    +0x100 / +0x200 / +0x300   三通道基址（步长 0x100）
        +04   配置 / 状态（boot 写 0x7F / 0x09 / 0x00）
        +05   串口：bit5 = CLK、bit3 = DATA、bit6-7 = 状态（读）
        +06   控制：bit2 = 片选/使能（传输前置位、传完清位）
        +07/+09/+0A  配置（boot 写 0x12/0x87/0x00 等）
    0x0000–0x000E   全局配置（boot 写 0x0001 / 0x7377 / 0x3C37 / 0x2000 …）

判据（逐条都能在 `bctrl.txt` 里核到）：

1. `0x125812` 起是**逐位发送循环**：`r4` 从 `0x8000` 每次 `lsr #1` 走 16 圈
   （`0x125842 ldi:20 #0x8000` / `0x125868 lsr` / `0x12586E bne *-42`）；
2. 每圈的动作固定三步：`bandh #0xD, @0x105`（掩码 0xD0，把 CLK/DATA 清掉、留下
   状态位）→ 若该位为 1 则 `borl #0x8, @0x105`（DATA=bit3）→ `borh #0x2, @0x105`
   （CLK=bit5 拉高）；
3. 实测写序列 `20 00 00 20 00 00 20 00 08 20 …`：`0x20` 与 `0x00` 严格交替 16 次，
   中间只出现一次 `0x08` —— 正是「时钟每圈一个脉冲、数据位只在 0x2000 那一位
   置起」（该次传输的载荷 r6 = `0x2000`，MSB 先）；
4. `0x125830 borl #0x4, @0x106` / `0x125840 or 0x125876 bandl #0xB`
   ⇒ 传输前给 `0x106` 置 bit2、传完清掉 = 片选；
5. **整块窗口一条读都没有**（除了上面这些状态位），0x40/0x6F 两个窗口更是
   0 笔读 —— 它们是**只写数据汇**，不是可读寄存器（见 KI#174-b 的更正）。
6. 全局 `+01` 的 **bit0 是状态位**，固件只做一次「检测 + 清掉」，不是轮询：

       0x40006  ldi:8   r1 = 0x01
       0x40008  sth     @0x50000000 = 0x0001   ; +00=0x00 / +01=0x01（16 位写）
       0x40144  ldi:32  r0 = 0x50000001
       0x4014A  btstl   #0x1, @r0              ; 测 bit0
       0x4014C  beq     *+4                    ; bit0=0 → 跳过
       0x4014E  bandl   #0xE, @r0              ; bit0=1 → 就地清掉 bit0
       0x40150  ...                             ; 两条路在此汇合

   **这一位怎么处理会改变 boot 步数，但不改变路径**：置 1 时多执行 `bandl`
   一条指令，`brealos.boot()` 的固定预算 191,520 步于是落在空闲循环前一条
   （`0x407D8`），再走一步就同样到达 `0x407DC`（见 KI#178 的逐拍对照）。
   所以默认取 0（与 V44 未映射读回 0 完全等价），要试就 `status_rdy=1`。

## 模型做什么、不做什么

做的：
  * 全局 0x00–0x0E 与各通道 `+04/+06/+07/+09/+0A` **如实回读写入值**（这些是配置）；
  * `+04` 的 bit1 / bit6 是**器件状态**，读回时被状态位替换（默认 0 = 空闲）；
  * `+01` 的 bit0 是**器件状态**（一次性检测位），读回时被状态位替换（默认 0）；
  * `+05` **不回读**写入的时钟/数据位，只回状态位 bit6/7（默认 0 = 空闲/无应答）；
  * 采样 CLK 上升沿的 DATA，按 MSB 先拼成 16 位字，记在 `tx[ch]`（每次传输一条），
    传满 16 个时钟就收一条 —— 于是「固件往这块硬件的串口发了什么」可以直接读出来。

不做的（都是**没有实证**的部分，不许编）：
  * 状态位 bit1/bit6（`+04`）、bit6/7（`+05`）、bit0（`+01`）什么时候置起 —— 真实
    器件由谁、隔多久应答，固件里看不出来；默认恒 0（与 V44 的「未映射读回 0」
    等价，因此**不改控制流**），要试就用 `status_cfg` / `status_cmd` / `status_rdy`
    显式打开（反事实实验用）；
  * `+04/+07/+09/+0A` 的**字段含义**（0x7F / 0x12 / 0x87 之类只是记下值）；
  * 时钟/数据的**电气极性**：这里按「上升沿采样、MSB 先」实现，是**由代码顺序推的**，
    不是器件手册。
"""

from .device import Device


class CtrlSerial50(Device):
    """`0x50000000` 窗口：三通道位串行控制块 + 全局配置寄存器。"""

    name = "ctrl50"

    BASE = 0x50000000
    SIZE = 0x400

    CH_BASE = 0x100
    CH_STRIDE = 0x100
    CH_COUNT = 3

    R_RDY = 0x01          # 全局状态：bit0 = 一次性检测位（固件 btstl 后 bandl 清掉）
    R_CFG = 0x04          # 配置 / 状态（读：bit1、bit6 是器件状态）
    R_SER = 0x05          # 串口：bit5 CLK、bit3 DATA、bit6-7 状态
    R_CS = 0x06           # 控制：bit2 片选

    DATA_BIT = 0x08
    CLK_BIT = 0x20
    CS_BIT = 0x04
    RDY_BIT = 0x01        # R_RDY 的状态位
    CFG_STATUS = 0x42     # bit1 | bit6
    SER_STATUS = 0xC0     # bit6 | bit7
    BITS_PER_WORD = 16

    def __init__(self, machine=None, status_cfg=0x00, status_cmd=0x00,
                 status_rdy=0x00):
        # Device.__init__ 会调 reset()，字段必须先就位（V23 的坑）
        self.status_cfg = status_cfg & self.CFG_STATUS
        self.status_cmd = status_cmd & self.SER_STATUS
        self.status_rdy = status_rdy & self.RDY_BIT
        super().__init__(machine)

    # ---------------------------------------------------------------- 生命周期
    def reset(self):
        self.regs = bytearray(self.SIZE)
        self.status_cfg = getattr(self, "status_cfg", 0)
        self.status_cmd = getattr(self, "status_cmd", 0)
        self.status_rdy = getattr(self, "status_rdy", 0)
        self._clk = [0] * self.CH_COUNT
        self._armed = [False] * self.CH_COUNT
        self._shift = [0] * self.CH_COUNT
        self._nbits = [0] * self.CH_COUNT
        self.tx = [[] for _ in range(self.CH_COUNT)]     # 每次传输的 16 位字
        self.clocks = [0] * self.CH_COUNT
        self.starts = [0] * self.CH_COUNT
        self.cs_edges = [0] * self.CH_COUNT

    # ---------------------------------------------------------------- 地址换算
    def channel(self, off):
        """偏移 → 通道号；不在三通道里返回 None。"""
        if self.CH_BASE <= off < self.CH_BASE + self.CH_STRIDE * self.CH_COUNT:
            return (off - self.CH_BASE) // self.CH_STRIDE
        return None

    # ---------------------------------------------------------------- 总线接口
    def write(self, off, size, val):
        if not 0 <= off < self.SIZE:
            return
        if size > 1:                       # 多字节访问按大端拆成单字节（与总线口径一致）
            for i in range(size):
                self.write(off + i, 1, (val >> (8 * (size - 1 - i))) & 0xFF)
            return
        b = val & 0xFF
        ch = self.channel(off)
        local = None if ch is None else (off - self.CH_BASE) % self.CH_STRIDE

        if ch is not None and local == self.R_SER:
            self._ser_write(ch, off, b)
            return
        if ch is not None and local == self.R_CS:
            old = self.regs[off]
            if (old ^ b) & self.CS_BIT:
                self.cs_edges[ch] += 1
                if b & self.CS_BIT:       # 片选拉高 = 一次传输开始
                    self._armed[ch] = True
                    self._shift[ch] = 0
                    self._nbits[ch] = 0
        self.regs[off] = b

    def read(self, off, size):
        if not 0 <= off < self.SIZE:
            return 0
        if size > 1:
            v = 0
            for i in range(size):
                v = (v << 8) | (self.read(off + i, 1) & 0xFF)
            return v
        ch = self.channel(off)
        local = None if ch is None else (off - self.CH_BASE) % self.CH_STRIDE
        if off == self.R_RDY:
            # bit0 是器件状态，其余位如实回读（boot 写的是 0x0001，所以其余位恒 0）
            return (self.regs[off] & ~self.RDY_BIT) | self.status_rdy
        if ch is not None and local == self.R_CFG:
            # 配置位如实回读；状态位由器件驱动（默认 0）
            return (self.regs[off] & ~self.CFG_STATUS) | self.status_cfg
        if ch is not None and local == self.R_SER:
            # 时钟/数据位是**可回读的控制位**（固件靠 RMW 把它们叠在一起：
            # `bandh #0xD` 清 → `borl #0x8` 置数据 → `borh #0x2` 叠时钟，
            # 若不回读，那一步会把 DATA 位冲掉）；状态位 bit6/7 由器件驱动。
            return (self.regs[off] & ~self.SER_STATUS) | self.status_cmd
        return self.regs[off]

    # ---------------------------------------------------------------- 位串行
    def _ser_write(self, ch, off, b):
        """`+05`：CLK 上升沿采 DATA，MSB 先，凑满 16 位收一条。

        片选（`+06` bit2）拉高之后的**第一个**上升沿算起始位，不计数据 ——
        实测每次传输是 17 个时钟、载荷正好落在后 16 个（`bctrl.txt` 的可核对项）。
        """
        clk = 1 if b & self.CLK_BIT else 0
        data = 1 if b & self.DATA_BIT else 0
        if clk and not self._clk[ch]:
            self.clocks[ch] += 1
            if self._armed[ch]:
                self._armed[ch] = False
                self.starts[ch] += 1
            else:
                self._shift[ch] = ((self._shift[ch] << 1) | data) & 0xFFFF
                self._nbits[ch] += 1
                if self._nbits[ch] >= self.BITS_PER_WORD:
                    self.tx[ch].append(self._shift[ch])
                    self._shift[ch] = 0
                    self._nbits[ch] = 0
        self._clk[ch] = clk
        self.regs[off] = b

    # ---------------------------------------------------------------- 调试视图
    def state(self):
        return {"tx": ["%s" % " ".join("0x%04X" % w for w in t) for t in self.tx],
                "clocks": self.clocks}

    def info(self):
        parts = []
        for ch in range(self.CH_COUNT):
            if self.clocks[ch]:
                parts.append("ch%d %d 时钟%s" % (
                    ch, self.clocks[ch],
                    (" / 载荷 " + " ".join("0x%04X" % w for w in self.tx[ch]))
                    if self.tx[ch] else ""))
        return "%s: %s" % (self.name, "; ".join(parts) if parts else "（无传输）")

    # ---------------------------------------------------------------- 观察口
    def reg(self, ch, local):
        """读某通道某寄存器的当前存值（探针/自检用，不经总线）。"""
        return self.regs[self.CH_BASE + ch * self.CH_STRIDE + local]

    def config(self, ch):
        """该通道配置字节里**非状态位**的部分。"""
        off = self.CH_BASE + ch * self.CH_STRIDE + self.R_CFG
        return self.regs[off] & ~self.CFG_STATUS

    def set_status(self, cfg=None, cmd=None, rdy=None):
        """改状态位（反事实实验用）：`cfg` 打 `+04` 的 bit1/bit6，`cmd` 打 `+05` 的
        bit6/7，`rdy` 打 `+01` 的 bit0。"""
        if cfg is not None:
            self.status_cfg = cfg & self.CFG_STATUS
        if cmd is not None:
            self.status_cmd = cmd & self.SER_STATUS
        if rdy is not None:
            self.status_rdy = rdy & self.RDY_BIT


def attach(machine, base=CtrlSerial50.BASE, size=CtrlSerial50.SIZE,
           status_cfg=0x00, status_cmd=0x00, status_rdy=0x00):
    """把设备挂到机器上（`machine._build()` 里调用）。"""
    dev = CtrlSerial50(machine, status_cfg=status_cfg, status_cmd=status_cmd,
                       status_rdy=status_rdy)
    machine.bus.map_io(base, size, dev, dev.name)
    machine.devices.append(dev)
    return dev
