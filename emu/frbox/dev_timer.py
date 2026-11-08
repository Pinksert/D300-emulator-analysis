# -*- coding: utf-8 -*-
"""重载定时器（Reload Timer）模型 —— 直接页 0x10–0x17。

⚠️ **V54 更正：这个设备不是 D300 的时基，固件根本不认识它。**
boot 191,520 步 + 后续 400,000 步（合计 591,520 步）里，直接页 `0x10–0x17`
**一次读、一次写都没有**（`results/frbox/tick18.txt` 的 C 段）。它只是「按 FR
系列常见重载定时器通用结构摆的」占位模型，保留下来是为了老探针可复现，
不要拿它的 csr / reload / 下溢次数解释整机为什么没有 tick。

V54 已把真实的内核系统节拍**实证定位到 INT 0x18**，见 `dev_tick18.py`：

    向量表槽 0x000EFF9C = 0x04046E（ISR）
    → 0x04047C ldi:32 #0x040A44, r12（全 ROM 唯一引用）
    → 0x040482 call r12 → 内核 tick 处理（0x68000110 计数 +1 / 延时队列递减）

固件初始化的是直接页 `0x48 = 0x80E8` / `0x4E = 0x1B`（`0x04045C` / `0x040460`），
不是本设备的 `0x10–0x17`；真机频率与器件身份仍未证（KI#191）。

机器配置里默认不绑定向量（irq_line=None），需要时在 monitor 里
`irq bind 0x0A 0x09` 之类显式接上，避免把推断当成实测。

  0x10 TMRLR  重载值（16 位，写）
  0x12 TMR    计数器（16 位，读写）
  0x14 TMCSR  控制：bit0 CNTE 计数使能 / bit1 MOD 1=重载 0=单次
                     bit2 TRG 单次触发 / bit8 IE 中断使能
  0x16 ST     状态：bit0 UF 下溢（读后自清）
"""

from .device import Device


class ReloadTimer(Device):
    name = "rtc"

    TMRLR = 0x00
    TMR = 0x02
    TMCSR = 0x04
    ST = 0x06

    CNTE = 0x0001
    MOD = 0x0002
    TRG = 0x0004
    IE = 0x0100
    UF = 0x0001

    def __init__(self, machine=None, irq_line=None):
        self.irq_line = irq_line
        self.reload = 0
        self.counter = 0
        self.csr = 0
        self.status = 0
        self.underflows = 0
        self.armed = False
        super().__init__(machine)

    def reset(self):
        self.reload = 0
        self.counter = 0
        self.csr = 0
        self.status = 0
        self.underflows = 0
        self.armed = False

    # ---- 时钟 ----
    def tick(self, cycles):
        if not (self.csr & self.CNTE):
            return
        if not self.armed and (self.csr & self.MOD):
            self.armed = True
            self.counter = self.reload
        left = cycles
        while left > 0:
            if self.counter == 0:
                if self.reload == 0 and not self.armed:
                    return
                self._underflow()
                if not (self.csr & self.MOD):
                    self.csr &= ~self.CNTE
                    return
            step = min(left, max(1, self.counter))
            self.counter -= step
            left -= step

    def _underflow(self):
        self.underflows += 1
        self.status |= self.UF
        self.counter = self.reload
        self.dbg(f"下溢 #{self.underflows}（重载 {self.reload}）")
        if self.csr & self.IE:
            self.raise_irq(self.irq_line)

    # ---- 总线接口 ----
    def read(self, off, size):
        if off == self.TMRLR:
            return self.reload & 0xFFFF
        if off == self.TMR:
            return self.counter & 0xFFFF
        if off == self.TMCSR:
            return self.csr & 0xFFFF
        if off == self.ST:
            v = self.status
            self.status = 0                     # 读后自清
            return v
        self.dbg(f"读未映射寄存器 0x{0x10 + off:02X}")
        return 0

    def write(self, off, size, val):
        if off == self.TMRLR:
            self.reload = val & 0xFFFF
            self.dbg(f"TMRLR ← {self.reload}")
            return
        if off == self.TMR:
            self.counter = val & 0xFFFF
            return
        if off == self.TMCSR:
            self.csr = val & 0xFFFF
            self.armed = False
            if self.csr & self.TRG:
                self.armed = True
                self.counter = self.reload
            self.dbg(f"TMCSR ← 0x{self.csr:04X}")
            return
        if off == self.ST:
            self.status &= ~(val & 0xFFFF)
            return
        self.dbg(f"写未映射寄存器 0x{0x10 + off:02X} ← 0x{val:X}")

    def state(self):
        return {
            "重载": self.reload,
            "计数": self.counter,
            "CSR": f"0x{self.csr:04X}",
            "下溢": self.underflows,
        }
