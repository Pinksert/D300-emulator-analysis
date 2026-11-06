# -*- coding: utf-8 -*-
"""dev_dual60.py —— `0x6000_0000` / `0x6100_0000`：双通道控制器（V46）。

## 这块地址是什么（证据）

V45 之后全机只剩 5 个地址 9 次未映射读，其中 3 个地址 5 次落在 0x60/0x61 两个
窗口里，而且**是唯一还会影响控制流的一笔读**。逐笔拆开（`results/frbox/dual40.txt`（A–H 八段））
之后，结构非常整齐：`0x60000000` 与 `0x61000000` 跑的是**同一段代码**，只是基址
不同（`0x1A35C2` / `0x1A35E2` 两处 `ldi:32`），两个通道的基址被写进 `0x8400` 起的
描述符表（每项 0x50 字节，由任务 2 在开机装配里填）。

单通道寄存器面（实测偏移与值，`_s46a` 的逐条记录）：

    +0x02   控制：`borh #0x8`（掩码 0x80）→ `borl #0x2`（掩码 0x02），两步 RMW
    +0x03   ← 0xFF      +0x04 ← 0x01      +0x06 ← 0x00
    +0x100  状态字节（**只读，固件从不写**），**bit2–3 = 状态字段，== 3 表示就绪**

判据：

1. `0x1A36BC`–`0x1A36D8` 是完整的单通道初始化：三次 `stb`（+03/+04/+06）后
   对 +02 连做两次「读-改-写」，两次读回的初值都是 0，所以写下去的是 0x80 和 0x02；
2. `0x1A36DA`–`0x1A36FC` 是**另一支**函数：

       0x1A36EC  ldub  r0, @(base+0x100)   ; 读状态字节
       0x1A36EE  lsl2  #12, r0             ; r0 <<= 28
       0x1A36F0  lsr2  #14, r0             ; r0 >>= 30  ⇒ 取出 bit2–3
       0x1A36F2  cmp   r0, #3
       0x1A36F4  bne   *+6                 ; 字段 != 3 → 返回 0
       0x1A36F6  ldi:8 r0, 1
       0x1A36FA  lsl   r12, r4             ; 否则返回 1 << 通道号
       0x1A3702  ldi:8 r4, 0               ; 「未就绪」出口

   ⚠️ `lsl2/lsr2` 的位移是 **u4 + 16**（`fr30-opc.c` 的 `ifmt_addi`），所以
   `#12`/`#14` 是 28/30 位 —— 合起来就是「(b<<28)>>30」，即取 **bit2–3**。
   一开始按 `bit1` 判是错的：`_s46j` 把 256 个状态值逐個喂进去才定死是 bit2–3。
3. 调用点 `0x15CB76` 拿到返回值后 `cmp` / `bne` —— **这个字段直接决定分支**，
   V45 之前「未映射读回 0」等于永远走「未就绪」那条路（KI#179）；
4. 两个通道的基址都来自 `0x8400` 描述符表，表项 +0x18/+0x1C/+0x20 分别是
   `0xDC0`/`0x46C`/`0x98`（A）与 `0xDE0`/`0x46D`/`0xA0`（B）。

## 模型做什么、不做什么

做的：
  * `+0x00–+0x0F` 等**配置字节如实回读**（RMW 必须能读回，否则 `borh`/`borl`
    叠不出 0x82）；
  * `+0x100` 整个字节由器件驱动（固件从不写它），默认 0（= V44 未映射读回 0，
    逐拍等价、不改任何旧数字），要试就 `set_status(byte=0x0C)` 或 `set_status(field=3)`；
  * 每个通道一个实例，各自有寄存器面（两个通道的写入值本来就不同）。

不做的（没有实证，不许编）：
  * 状态字段什么时候变成 3 —— 谁应答、隔多久，固件里看不出来；
  * 除 +0x02/+0x03/+0x04/+0x06/+0x100 之外的寄存器语义（偏上 +0x1000 / +0x2000
    两个子窗口在描述符表里出现，但这一轮 70 万步里一次都没被访问）；
  * 这块硬件**是什么**：只能说到「双通道、每通道一个控制寄存器 + 一个状态字节，
    由任务级代码在两处调用点分头触发/查询」，**不猜具体功能**。
"""

from .device import Device


class DualCtrl60(Device):
    """`0x60000000` / `0x61000000` 单通道视图（每次 attach 一个通道）。"""

    name = "dual60"

    BASE = 0x60000000
    SIZE = 0x4000              # 描述符表里出现 +0x1000 / +0x2000，至少 0x3000

    CH_STRIDE = 0x1000000      # 两通道相距 16MB（0x60000000 / 0x61000000）
    R_CTRL = 0x02              # bit7 与 bit1 分两步置起（`borh #0x8` → `borl #0x2`）
    R_STATUS = 0x100           # 状态字节（只读）
    # 状态字段在 **bit2–3**：固件用 `lsl2 #12` + `lsr2 #14` 取出来（= (b<<28)>>30），
    # 再 `cmp #3`；等于 3 才走「就绪」那条路（_s46j 逐值实测：0x0C–0x0F / 0xFF 命中）。
    STATUS_FIELD_SHIFT = 2
    STATUS_FIELD_MASK = 0x0C
    STATUS_READY = 0x03        # 字段值

    def __init__(self, machine=None, index=0, status=0x00):
        self.index = index
        self.status = status & 0xFF
        self.base = self.BASE + index * self.CH_STRIDE
        super().__init__(machine)

    # ---------------------------------------------------------------- 生命周期
    def reset(self):
        self.regs = bytearray(self.SIZE)
        self.status = getattr(self, "status", 0)
        self.writes = 0            # 本通道收到的写笔数（观测口）
        self.reads = 0
        self.ready_polls = 0       # 读 +0x100 的次数

    # ---------------------------------------------------------------- 总线接口
    def write(self, off, size, val):
        if not 0 <= off < self.SIZE:
            return
        if size > 1:
            for i in range(size):
                self.write(off + i, 1, (val >> (8 * (size - 1 - i))) & 0xFF)
            return
        self.writes += 1
        self.regs[off] = val & 0xFF

    def read(self, off, size):
        if not 0 <= off < self.SIZE:
            return 0
        if size > 1:
            v = 0
            for i in range(size):
                v = (v << 8) | (self.read(off + i, 1) & 0xFF)
            return v
        self.reads += 1
        if off == self.R_STATUS:
            self.ready_polls += 1
            # 整个字节都是器件状态（固件从不写它），直接由 `status` 驱动
            return self.status
        return self.regs[off]

    # ---------------------------------------------------------------- 观察口
    def ctrl(self):
        """`+0x02` 的当前存值（两次 RMW 叠完应当是 0x82）。"""
        return self.regs[self.R_CTRL]

    def state(self):
        return {"ch%d" % self.index: "ctrl=0x%02X 状态=0x%02X 写%d 读%d"
                % (self.ctrl(), self.status, self.writes, self.reads)}

    def info(self):
        return "ch%d @0x%08X: ctrl=0x%02X status=0x%02X 写 %d / 读 %d" % (
            self.index, self.base, self.ctrl(), self.status,
            self.writes, self.reads)

    def set_status(self, byte=None, field=None):
        """改状态字节（反事实实验用）。

        `byte` 直接给整字节；`field` 只给 bit2–3 那个字段（0–3，3 = 就绪）。
        """
        if byte is not None:
            self.status = byte & 0xFF
        if field is not None:
            self.status = (self.status & ~self.STATUS_FIELD_MASK) | (
                (field & 0x03) << self.STATUS_FIELD_SHIFT)

    @property
    def field(self):
        """`+0x100` 的 bit2–3 字段值（3 = 就绪）。"""
        return (self.status & self.STATUS_FIELD_MASK) >> self.STATUS_FIELD_SHIFT


def attach(machine, bases=(0x60000000, 0x61000000), status=0x00):
    """把一个或多个通道挂到机器上；返回实例列表（`machine.dual60`）。"""
    devs = []
    for i, base in enumerate(bases):
        dev = DualCtrl60(machine, index=i, status=status)
        dev.base = base                       # 允许调用方给非默认基址
        machine.bus.map_io(base, dev.SIZE, dev, "%s%d" % (dev.name, i))
        machine.devices.append(dev)
        devs.append(dev)
    return devs
