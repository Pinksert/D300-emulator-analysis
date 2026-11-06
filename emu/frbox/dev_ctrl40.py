# -*- coding: utf-8 -*-
"""dev_ctrl40.py —— `0x4000_0000` 窗口：寄存器面 + 批量下载口（V46）。

## 这块地址是什么（证据）

V45 之后 `0x40xxxxxx` 还剩 1650 笔写、4 次读，是**最大的一块未建模窗口**。
V44 把它叫「只写数据汇」，V46 逐笔拆开之后要把这个说法改精确：它**不是**一整块
数据汇，而是「若干寄存器实例 + 一个批量下载窗口」的组合（`results/frbox/dual40.txt`（A–H 八段））。

按 PC 归组（1650 笔写 / 54 个 PC）后只有三类：

1. **寄存器实例**（写点 54 个 PC 里 51 个都在这一层，值全是参数）：

       偏移（相对 0x40000000）   谁写的      内容                          实写值
       +0x40002 / +0x40010..    0x14C0xx   单值参数                      0x0E 0x80 0x280 0x1E0 0x140
       +0x40014 / +0x40016      0x14C0xx   「640 / 480」这一对             0x280 / 0x1E0
       +0x40100..+0x4011E       0x14C0xx   第二组单值参数                 0x8D 0x10 0x808
       +0x400D0..+0x400EE       0x14C1A0   16 个半字全 0xFFFF（掩码）      0xFFFF ×16
       +0x408D0..+0x408EE       0x14C218   同上（另一份）                 0xFFFF ×16
       +0x40400 / +0x4040C      0x14CF18   14 个 32 位系数                0x7008080 0x7FF8080 …
       +0x40C00 / +0x40C0C      0x14CF16   同一组系数（另一份）            同上
       +0x40D004 / +0x40D006    0x12727E   门控（基址 0x4000D000）        0x8000 / 0x0
       +0x1C0000 / +0x1C0006    0x1A4F7E   门控（基址 0x401C0000）        0x8000 / 0x0

   单值参数里 `0x0280 = 640`、`0x01E0 = 480`、`0x0140 = 320` —— 与 D300 那块
   640×480 的 VGA 液晶一致（只作线索，**不下结论说它就是显示控制器**）；
   `0x280/0x1E0/0x140` 以三件套的形式在两个实例里各重复一遍。
   那 14 个 32 位系数（`0x7008080` / `0x7FF8080` / `0x74D55FF` …）有共同的高位
   形状（末 8 位恒 `0x80`），像定点系数表，**但不猜它算什么**。
2. **一个必须可读的 16 位寄存器** `+0xF8`（两个实例各一份）。固件做的是两次
   **读-改-写**：

       0x14C1BA  lduh  r1, @(base+0xF8)     ; 读
       0x14C1BC  ldi:32 r2, 0xFFFFC0FF
       0x14C1C2  and   r1, r2               ; 清 bit8–13
       0x14C1C8  or    r1, 0x3B00           ; 置 bit8,9,11,12,13
       0x14C1CA  sth   @(base+0xF8), r1     ; 写回
       0x14C1D0  lduh  r1, @(base+0xF8)     ; 再读（要读到自己刚写的值）
       0x14C1D6  and   r1, 0xFFFFFFC0       ; 清低 6 位
       0x14C1DA  or    r1, 0x3B             ; 置 bit0,1,3,4,5
       0x14C1DC  sth   @(base+0xF8), r1     ; 写回 → 终值 0x3B3B

   ⇒ **不回读就错**（V44 口径下第二次读到 0，终值会写成 0x3B 而不是 0x3B3B）；
3. **批量下载窗口** `0x40010000` / `0x40010200`：先往 `+0x10000` 写 `0x0800`，
   然后 `0x137968` 的 16 位拷贝环把 **6 张 16 位表**顺着 `+0x10200` 起打进去
   （共 1541 项 / 3082 字节，占 6 KB 窗口的一半）：

       表  目标偏移            项数   源（ROM）              首值                    末值
       0   +0x10200         256    0x1BDBAE            0, 0x40, 0x80 …          0x3FC0
       1   +0x10400         257    0x1BD9AC            0, 0x100, 0x200 …        0x3FFF
       2   +0x10800         257    0x1BD7AA            0, 0x1000, 0x16A0 …      0xFFF0
       3   +0x11000         257    0x1BD5A8            0, 0, 0, 0x10 …           0xFFE0
       4   +0x11400         257    0x1BD5A8            （与表 3 同源同内容）    0xFFE0
       5   +0x11800         257    0x1BD5A8            （与表 3 同源同内容）    0xFFE0

   每张表的每一项都**逐字节等于 ROM 里那一段**（6/6 段全等）⇒ 这是把固件自带的
   查表搬到硬件，不是运行时算出来的。形状（逐项相邻差可核）：
     * 表 0：**纯线性斜坡** —— 差恒 `0x40`，0 … `0x3FC0`（256 项，正好铺满 14 位）；
     * 表 1：**先斜坡后弯曲、末段饱和** —— 前 43 项差恒 `0x100`，之后差值变小，
       第 64 项起一直是 `0x3FFF`（14 位满量程就是它的饱和值）；
     * 表 2：**增量递减**（凹）—— 差 `4096 → 1696 → 1296 → 1104 → 960 …`，末值 `0xFFF0`；
     * 表 3/4/5：**增量递增**（凸）—— 起步差 `0 / 0 / 16 / 0 / 16 …` 逐渐变大，末值 `0xFFE0`；
       同一条曲线写了三份到三个不同基址 —— 像三条同曲线通道。

## 模型做什么、不做什么

做的：
  * 整个窗口建成**可回读的寄存器面**（写进去的值能读回来）—— 这是「RMW 需要
    回读」这一条证据支持的最小行为，不是「认定它是 RAM」；
  * `+0xF8` 的两次 RMW 于是能叠出 `0x3B3B`（可核：`dev.rmw_value(inst)`）；
  * 把批量下载口的字节按偏移**留档**（`dev.image(0x10200, n)` /
    `dev.stream_payload()`），这样「固件往这块硬件送了什么」可以离线比对，
    而不只是「写丢了 1541 笔」。实测留档 3082 字节，与 ROM 里 6 段表逐字节相同。

不做的：
  * 时序参数的含义（640/480/320 只是数值，**没有手册**，不解释成宽高 ——
    虽然它和 640×480 面板吻合，但「吻合」不是证据）；
  * 那 6 张表**是什么曲线**（线性/弯曲是可算的事实，用途只能到「曲线表」为止）；
  * 写 `+0x10000` 的 0x0800 是不是长度/起始地址（只是记下值）；
  * 这块硬件的身份 —— 只说「寄存器实例 + 下载口」，**不猜功能**。
"""

from .device import Device


class CtrlWindow40(Device):
    """`0x40000000` 窗口：稀疏寄存器面 + 批量下载口留档。"""

    name = "ctrl40"

    BASE = 0x40000000
    SIZE = 0x1000000           # 0x40000000–0x40FFFFFF（实测最高偏移 0x40908）

    RMW_OFF = 0xF8             # 每实例 +0xF8（16 位，位域由固件 RMW 改）
    RMW_HI_MASK = 0xFFFFC0FF
    RMW_LO_MASK = 0xFFFFFFC0
    RMW_HI_VAL = 0x3B00
    RMW_LO_VAL = 0x003B
    RMW_FINAL = 0x3B3B

    STREAM_LO = 0x10200        # 批量下载口
    STREAM_HI = 0x12000
    STREAM_LEN_REG = 0x10000   # 先写 0x0800 的那个寄存器

    def __init__(self, machine=None, size=None):
        self.size = size or self.SIZE
        super().__init__(machine)

    # ---------------------------------------------------------------- 生命周期
    def reset(self):
        self.regs = {}                     # 稀疏：只记写过的偏移
        self.writes = 0
        self.reads = 0
        self.rmw_reads = 0                 # 读 +0xF8 的次数
        self.stream_writes = 0             # 落在下载口里的写笔数
        self.stream_last = 0               # 下载口最后一笔的偏移

    # ---------------------------------------------------------------- 总线接口
    def write(self, off, size, val):
        if not 0 <= off < self.size:
            return
        if size > 1:
            for i in range(size):
                self.write(off + i, 1, (val >> (8 * (size - 1 - i))) & 0xFF)
            return
        self.writes += 1
        if self.STREAM_LO <= off < self.STREAM_HI:
            self.stream_writes += 1
            self.stream_last = off
        self.regs[off] = val & 0xFF

    def read(self, off, size):
        if not 0 <= off < self.size:
            return 0
        if size > 1:
            v = 0
            for i in range(size):
                v = (v << 8) | (self.read(off + i, 1) & 0xFF)
            return v
        self.reads += 1
        if off % 0x100 == self.RMW_OFF:        # 落在某个实例的 +0xF8 上
            self.rmw_reads += 1
        return self.regs.get(off, 0)

    # ---------------------------------------------------------------- 观察口
    def image(self, off, n):
        """下载口（或任意偏移）上固件写进去的字节，按大端拼成整数列表。"""
        return bytes(self.regs.get(off + i, 0) for i in range(n))

    def rmw_value(self, inst_base):
        """某实例 `+0xF8` 当前存的 16 位值（两次 RMW 叠完应当是 0x3B3B）。"""
        o = inst_base + self.RMW_OFF
        return (self.regs.get(o, 0) << 8) | self.regs.get(o + 1, 0)

    def stream_payload(self):
        """把下载口收到的字节按首次写入顺序还原（只返回非零区间）。"""
        offs = sorted(o for o in self.regs if self.STREAM_LO <= o < self.STREAM_HI)
        return bytes(self.regs[o] for o in offs)

    def state(self):
        return {"写": self.writes, "读": self.reads,
                "下载口": "%d 笔 / 最大偏移 +0x%X"
                          % (self.stream_writes, self.stream_last),
                "寄存器": "%d 个偏移" % len(self.regs)}

    def info(self):
        return "ctrl40: 写 %d / 读 %d（+0xF8 %d 次）, 下载口 %d 笔, 寄存器 %d 个偏移" % (
            self.writes, self.reads, self.rmw_reads,
            self.stream_writes, len(self.regs))


def attach(machine, base=CtrlWindow40.BASE, size=None):
    """把窗口挂到机器上（`machine._build()` 里调用）。"""
    dev = CtrlWindow40(machine, size=size)
    machine.bus.map_io(base, dev.size, dev, dev.name)
    machine.devices.append(dev)
    return dev
