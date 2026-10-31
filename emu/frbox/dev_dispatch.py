# -*- coding: utf-8 -*-
"""「请求调度」寄存器：直接页 0x44。

REALOS 在需要换任务时会往这个地址写一个字节 1：

    0x000FECEC  ldi:8  #1, r13
    0x000FECF0  dmovb  @0x44      ; R13 → @0x44（普通内存写，不是 int 指令）
    0x000FECF2  ld     @r15+, rp
    0x000FECF4  ret

跑 30 万步里它被写了 1016 次，而**固件里从头到尾没有任何一处读它** ——
只写不读，那只能是硬件寄存器：写 1 = 向中断控制器挂一条请求。

请求哪条向量？证据链如下：

* 内核区里唯一一个没被解释的向量入口是 `vec 0x3F → 0x000FE776`，而它
  的头几条指令正是调度器该有的样子：
      andccr #0xEF      ; 先关中断（调度过程不许被外设 ISR 打断）
      orccr  #0x20      ; 切到系统栈
      stm1   r8-r14
      ldi:32 #0x459F0, r12
      xchb   @r12, r13  ; 原子读走并清空"待派发"字节
      beq    返回       ; 为空 → 不用调度
      ...
      ldi:32 #0x45980, r7   ; 当前 TCB
* 它的 ICR（0x440 + (0x3F-0x10)）= 0x1E，是所有启用向量里**级别号最大**的，
  也就是优先级最低 —— 调度器本就该最低，好让所有外设 ISR 都能抢占它。
* 内核进临界区用 `stilm #0x1E`：在「号越小优先级越高、受理条件 level < ILM」
  这套约定下，ILM=0x1E 恰好只把 0x1E 这一级（也就是调度器自己）挡在门外，
  派发因此被自然地推迟到 `reti` 回到任务上下文（ILM=0x1F）之后。

⚠️ 这是推断出来的绑定，不是手册确认的。三条证据互相咬合，并且（见 V11 报告）
   打开它之后真正的任务切换第一次发生了 —— 但寄存器名、以及"是不是还有别的
   触发源"都还没定。见 KNOWN_ISSUES 第 42 条。
"""

from .device import Device

DISPATCH_REG = 0x44      # 直接页里的「请求调度」寄存器
DISPATCH_VEC = 0x3F      # 它请求的那条向量（内核调度器 0x000FE776）


class DispatchRequest(Device):
    """写 1 就挂一条调度中断；读恒为 0（真机上是只写寄存器）。"""

    name = "dispatch"

    def __init__(self, machine=None, vec=DISPATCH_VEC):
        self.vec = vec & 0xFF
        self.requests = 0
        self.last_level = None
        super().__init__(machine)

    def reset(self):
        self.requests = 0
        self.last_level = None

    # ---- 总线 ----
    def read(self, off, size):
        return 0

    def write(self, off, size, val):
        if not (val & 0xFF):
            return                       # 写 0 不算请求
        self.requests += 1
        m = self.machine
        lvl = None
        if m is not None:
            lvl = m.irq_level(self.vec) or 0x1F   # 级别取固件自己写进 ICR 的值
            m.raise_irq(self.vec, lvl)
        self.last_level = lvl
        self.dbg(f"0x{off:02X} ← 0x{val & 0xFF:02X} → 请求 vec 0x{self.vec:02X} "
                 f"(level 0x{lvl:02X})，累计 {self.requests} 次")

    # ---- 调试 ----
    def state(self):
        out = {"请求次数": self.requests, "向量": "0x%02X" % self.vec}
        if self.last_level is not None:
            out["级别"] = "0x%02X" % self.last_level
        return out


def attach(machine, vec=DISPATCH_VEC):
    dev = DispatchRequest(machine, vec)
    machine.dp.attach(DISPATCH_REG, DISPATCH_REG + 1, dev, base=DISPATCH_REG)
    machine.dispatch = dev
    return dev
