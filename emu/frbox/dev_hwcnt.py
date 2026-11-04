# -*- coding: utf-8 -*-
"""dev_hwcnt.py —— 0x5C003000 窗口的自由运行计数器（V23 实测定性）。

B 段任务 2（入口 0x1200AC，开机装配）跑过 0x125A00 一带的函数时，
在 0x125A0A–0x125A18 落进「连读两次 [0x5C003034]，差值小于容差就继续等」
的循环 —— 经典的「等计数器走字」时间安全读法。我们的总线未映射读
恒 0，两次读永远相等，循环出不去（V23 之前 B 段的第二道墙）。

证据链（全部静态+动态实测，见 docs/03-frbox.md V23 段）：

  * 固件里 0x5C003000 立即数共 33 处，全落在任务 2 的代码区
    0x120xxx–0x12Exxx —— 是任务 2 专用的一组外设寄存器。
  * 0x120C8E 的初始化序列：`ldi:32 r4,#0x5C003000` 后往
    [base+0x30] 写半字 0x813（周期/比较值）、[base+0x38] 写 0xFFFFFFFF
    （控制/预分频域）。
  * +0x34 夹在两者之间，被「读两次等变化」地读 —— 定性为当前计数值。

建模边界（写报告必须带上）：

  * 只保证「会走字」这一语义：计数器每个机器 tick（256 步）+1。
    0x813 对应的真实硬件输入频率未标定，不猜。
  * 其余偏移走影子存储（读了 0、写了记档），不做任何语义解释。
  * 这是「替硬件走字」，不是硬件真的在计时；固件拿它做精确延时时，
    我们的时长比例不代表真实时长。
"""

from .device import Device


class FreeRunCounter(Device):
    """0x5C003000 起的硬件定时器：+0x34 是自由运行计数器。"""

    name = "hw-counter"

    def __init__(self, machine=None, base=0x5C003000):
        self.base = base
        self.count = 0
        self.shadow = {}          # 未建模寄存器的影子存储
        super().__init__(machine)

    def reset(self):
        self.count = 0
        self.shadow.clear()

    def tick(self, cycles):
        self.count = (self.count + 1) & 0xFFFFFFFF

    def read(self, off, size):
        # +0x34..+0x37：32 位计数器（大端字节序拼起来）。
        # 注意：普通 IO 区的总线是整块读（一次传 off+size），不是逐字节。
        if 0x34 <= off and off + size <= 0x38:
            b = self.count.to_bytes(4, "big")
            i = off - 0x34
            return int.from_bytes(b[i:i + size], "big")
        return self.shadow.get(off, 0) & 0xFF

    def write(self, off, size, val):
        self.shadow[off] = val & 0xFF

    def state(self):
        return {"count": hex(self.count)}
