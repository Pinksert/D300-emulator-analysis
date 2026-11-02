# -*- coding: utf-8 -*-
"""事件位图源：把「哪路外设有事件」写到软件位图里，再打中断。

## 为什么不是「再多一个状态位」

V13 的 `dev_evt` 只模拟了一件事：某个器件干完活 → 把状态位置 1 → 打 vec 0x23。
它能把任务 5 的循环推起来，却推不动任务 1 / 任务 9。原因在 V14 找到：
**回调真正看的不止一个位，而是一张位图。**

`0x00041A56` 是一个 16 位字，固件自己就有一处把「外设状态 → 位图位」的转发：

    0x0AD666  ldi  r0, #0x90
    0x0AD66A  btstl #8, r0        ; 状态寄存器 0x90 的 bit8 起来了？
    0x0AD66E  borl  #8, r4        ; → 位图 0x41A56 置 bit8
    0x0AD672  borh  #8, r4        ; → 连 0x41A57 的高位一起置

全段扫描一共 13 处碰这个字：5 处置位、6 处判定、2 处整字写。而五个回调槽的
判定各不相同（见下表，出处在 `probe/exp_slot_profile.py`）：

    槽 0x3F028 vec 0x13 ← 位图 bit4
    槽 0x3F040 vec 0x19 ← 位图 bit8
    槽 0x3F048 vec 0x1B ← 直接页 0x60 的 bit8 / bit1 / bit2
    槽 0x3F074 vec 0x23 ← 直接页 0x80 的 bit3（dev_evt 覆盖）
    槽 0x3F078 vec 0x24 ← 位图 bit9

于是「只喂 bit4」或「只喂 bit8」都不够：vec 0x19 的回调 `0x0ACAFE` 里
`ref_tsk` 和 `sta_tsk(8)` 是**同一个分支里连着**的两句，只有 bit8 置着才走到。

## 这个模型能报什么、不能报什么

能报：给到位图位 + 中断之后，固件自己把任务 8 建起来（`ref_tsk` → `sta_tsk`），
      并且任务 8 真的跑起来 —— 这一步的每一步都是固件代码干的，我们只按了「有事件」。
不能报：真实器件是哪一个、位图位的物理来源、一个事件该持续多久。
      位图位是**软件语义**，不是硬件寄存器；我们只证明了「这几个位是必要条件」。

默认关闭（`--bits` / `Machine.enable_bits()`），以免影响 V2–V13 的基线。
"""

from .device import Device

BITMAP = 0x00041A56


class BitEvent(Device):
    """周期性地把若干位图位置 1，然后打一串中断。

    `mask`  要置的位（16 位掩码），0 表示不碰位图
    `vecs`  要挂的中断向量
    `period` 每隔多少个周期触发一次
    """

    name = "bits"

    def __init__(self, machine=None, mask=0x0110, vecs=(0x13, 0x19),
                 period=2048, addr=BITMAP):
        self.mask = mask
        self.vecs = tuple(vecs)
        self.period = period
        self.addr = addr
        self.count = 0
        self.fired = 0
        super().__init__(machine)

    def reset(self):
        self.count = 0
        self.fired = 0

    def _fire(self):
        m = self.machine
        if m is None:
            return
        if self.mask:
            m.bus.wr(self.addr, 2, m.bus.rd(self.addr, 2) | self.mask)
        for v in self.vecs:
            m.raise_irq(v)
        self.fired += 1
        self.dbg("位图 0x%05X |= 0x%04X，挂 %s（第 %d 次）"
                 % (self.addr, self.mask,
                    ",".join("0x%02X" % v for v in self.vecs), self.fired))

    def tick(self, cycles):
        if self.period <= 0:
            return
        self.count += cycles
        if self.count >= self.period:
            self.count = 0
            self._fire()

    def state(self):
        return {"已触发": self.fired, "周期": self.period,
                "掩码": "0x%04X" % self.mask,
                "向量": ",".join("0x%02X" % v for v in self.vecs)}


def attach(machine, **kw):
    dev = BitEvent(machine, **kw)
    machine.devices.append(dev)
    machine.bits = getattr(machine, "bits", None) or dev
    return dev
