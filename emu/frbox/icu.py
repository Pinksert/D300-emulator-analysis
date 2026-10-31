# -*- coding: utf-8 -*-
"""中断控制器：把设备的中断线聚合成 CPU 的向量请求。

D300 两段固件的向量表已经实测出来了（1.11）：

    A 段 TBR=0x000FFC00  真接线 46 条
    B 段 TBR=0x000EFC00  真接线 48 条

但「哪条线对应哪个外设」只有两条有实证：
    IRQ 0x1B → 0x0012FDB4   串口（读直接页 0x60，非零则 set_flg(3,0x20)）
    IRQ 0x20 → 0x0012FE54   EXT9（索引 0x1BC480 函数指针表，末尾写 0x38=0xFB）

所以这里不做「猜全表」，而是做成可配置的绑定表：谁有实证就绑谁，没实证的
设备默认不占向量（bind=None），需要时在机器配置里显式指定并在报告里标明来源。
"""

from .device import Device


class Icu(Device):
    name = "icu"

    def __init__(self, machine=None):
        self.bindings = {}          # line -> (vector, level, 说明)
        self.pending = {}           # line -> level（保持电平）
        self.total_raised = 0
        self.log = []               # (line, vec, level)
        super().__init__(machine)

    def reset(self):
        self.pending.clear()
        self.total_raised = 0
        self.log.clear()

    # ---- 绑定 ----
    def bind(self, line, vector, level=8, note=""):
        self.bindings[line] = (vector & 0xFF, level & 0x1F, note)
        return self

    def bind_many(self, table):
        for line, (vec, lvl, note) in table.items():
            self.bind(line, vec, lvl, note)

    # ---- 中断线 ----
    def raise_vec(self, vec, level=0x1F):
        """直接按向量请求（不经过线绑定表）。

        级别应当来自固件写进 ICR 的值——A 段实测 48 个 ICR 全被写过，级别
        0x12–0x1F；硬编码 8 会被 ILM=0x10 掩掉，等于什么都没发生。
        """
        lv = level & 0x1F
        self.pending[vec] = lv
        self.total_raised += 1
        self.log.append((vec, vec, lv))
        if self.trace is not None:
            self.trace.log("int", f"icu: 向量 0x{vec:02X} 请求 (level 0x{lv:02X})")
        if self.machine is not None:
            self.machine.on_irq(vec, lv)
        return True

    def raise_line(self, line, level=None):
        vec, lvl, note = self.bindings.get(line, (None, 0, ""))
        if vec is None:
            if self.trace is not None:
                self.trace.log("int", f"icu: 线 {line} 无绑定，忽略（{note}）")
            return False
        lv = lvl if level is None else (level & 0x1F)
        self.pending[line] = lv
        self.total_raised += 1
        self.log.append((line, vec, lv))
        if self.trace is not None:
            self.trace.log("int", f"icu: 线 {line} → 向量 0x{vec:02X} (level {lv})")
        if self.machine is not None:
            self.machine.on_irq(vec, lv)
        return True

    def drop_line(self, line):
        self.pending.pop(line, None)

    # ---- 视图 ----
    def state(self):
        out = {}
        for line, (vec, lvl, _) in sorted(self.bindings.items()):
            out[f"line{line}"] = (f"→vec 0x{vec:02X}/L{lvl}" +
                                  (" 待决" if line in self.pending else ""))
        out["累计请求"] = self.total_raised
        return out

    def table(self):
        rows = []
        for line, (vec, lvl, note) in sorted(self.bindings.items()):
            rows.append((line, vec, lvl, note))
        return rows
