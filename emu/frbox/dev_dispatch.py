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
    """请求锁存器（V40 改写）。

    写 1 = 置位并挂一条 vec 0x3F 中断；写 0 = 清位**并撤掉待决项**；
    读回锁存位（固件确实会写完立刻读一次：0x41886→0x41888、0x40EAC→0x40EAE，
    读回值丢弃，是写后哑读）。

    旧版本只置不清：写 0 什么都不做，于是一条已经被撤销的请求
    会在 CPU 的待决队列里活到下一次开中断，被当成真请求受理。
    """

    name = "dispatch"

    def __init__(self, machine=None, vec=DISPATCH_VEC):
        self.vec = vec & 0xFF
        self.requests = 0        # 写 1 的次数
        self.acks = 0            # 写 0 的次数
        self.reads = 0           # 被读的次数
        self.cancelled = 0       # 写 0 时真正撤掉的待决项
        self.raised = 0          # 真正挂上中断的次数（重叠置位不算）
        self.latch = 0           # 锁存位
        self.last_level = None
        super().__init__(machine)

    def reset(self):
        self.requests = 0
        self.acks = 0
        self.reads = 0
        self.cancelled = 0
        self.raised = 0
        self.latch = 0
        self.last_level = None

    # ---- 总线 ----
    def read(self, off, size):
        self.reads += 1
        return self.latch

    def write(self, off, size, val):
        v = val & 0xFF
        m = self.machine
        if not v:
            self.acks += 1
            if self.latch:
                self.latch = 0
                self.deassert()
            return
        self.requests += 1
        if self.latch:                   # 已经挂着了：重复置位不重复挂中断
            return
        self.latch = 1
        if m is not None:
            lvl = m.irq_level(self.vec) or 0x1F   # 级别取固件自己写进 ICR 的值
            m.raise_irq(self.vec, lvl)
            self.raised += 1
            self.last_level = lvl
            self.dbg(f"0x{off:02X} ← 0x{v:02X} → 请求 vec 0x{self.vec:02X} "
                     f"(level 0x{lvl:02X})，累计 {self.requests} 次")

    def deassert(self):
        """撤掉 CPU 待决队列与 ICU 里这条向量的请求。"""
        m = self.machine
        c = getattr(m, "cpu", None) if m is not None else None
        if c is None:
            return
        before = len(c.irq_pending)
        c.irq_pending[:] = [e for e in c.irq_pending if e[1] != self.vec]
        self.cancelled += before - len(c.irq_pending)
        if m.icu is not None:
            m.icu.pending.pop(self.vec, None)

    # ---- 调试 ----
    def state(self):
        out = {"请求（写 1）": self.requests, "挂中断": self.raised, "撤销（写 0）": self.acks,
               "被读": self.reads, "锁存位": self.latch, "向量": "0x%02X" % self.vec}
        if self.cancelled:
            out["撤掉的待决项"] = self.cancelled
        if self.last_level is not None:
            out["最后级别"] = "0x%02X" % self.last_level
        return out


def attach(machine, vec=DISPATCH_VEC):
    dev = DispatchRequest(machine, vec)
    machine.dp.attach(DISPATCH_REG, DISPATCH_REG + 1, dev, base=DISPATCH_REG)
    machine.dispatch = dev
    return dev
