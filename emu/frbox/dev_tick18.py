# -*- coding: utf-8 -*-
"""INT 0x18 —— D300 B 段内核的系统节拍（V54 实证）。

V50 起整机一直「没有 tick」，V52/V53 全靠 `btick.tick()` / `tick_v52()`
**反事实地改内核数据结构**才能往下推。V54 换了个问法：固件自己的 tick
处理是谁叫的？答案在 ROM 里，整条链条可静态复核：

    B 段 TBR=0x000EFC00，vec 0x18 的槽 = 0xEFC00+(255-0x18)*4 = 0x000EFF9C
      0x000EFF9C = 0x0004046E        （全 ROM 唯一一处把 0x04046E 当 32 位数据）

    0x04046E  stm1 {r9..r15} / stm0 {r0..r7} / st dr4 / st dr5 / st dr1
    0x040478  ldi:8   #0x1A, r13
    0x04047A  dmovh   #78            ; 直接页 0x4E ← 0x1A（中断应答）
    0x04047C  ldi:32  #0x040A44, r12 ; ★ 全 ROM 唯一一处引用 tick 处理
    0x040482  call    r12
    0x040484..0x04048C  恢复上下文
    0x04048E  reti

    0x040A44  st dr1 / st r14       ; 无 enter 的小函数序言（V52 从 0x040A48 起读）
    0x040A48  ldi:32 #0x68000110, r14
    0x040A4E..0x040A5C  64 位 tick 计数 +1（关中断临界区）
    0x040A60  ldi:32 #0x68000118, r7 ; 延时队列哨兵
    0x040A78  ld #20, r6 / add2 #-1 / st #20, r6  ; TCB+0x14 递减
    0x040A80..0x040AAC  到期摘链、清等待位、写 0x44 请求派发

内核早期初始化 `0x040450..0x040468`（**在批量 ICR 循环之前**）：

    0x040450  ldi:20 #0x440, r14
    0x040454  ldi:8  #0x12, r0
    0x040456  stb    #8, r0          ; ⇒ 0x448（vec 0x18 的 ICR）= 0x12
    0x040458  ldi:20 #0x80E8, r13 / 0x04045C dmovh #72  ; 直接页 0x48 = 0x80E8
    0x04045E  ldi:8  #0x1B, r13   / 0x040460 dmovh #78  ; 直接页 0x4E = 0x1B
    0x040462  ldi:32 #0x12090E, r13 / 0x040468 call r13  ; 其余 47 条 ICR

`0x12090E` 那段批量初始化**跳过 0x448**，boot 动态里 0x448 只被 `0x040456`
写过 1 次 —— 这条向量是内核单独留出来的高优先级系统节拍。

## 这个设备做什么 / 不做什么

只做一件事：**周期性把 vec 0x18 这根线拉高**。除此之外什么都不碰。
tick 计数递增、TCB+0x14 递减、延时队列出队、写 0x44 请求派发，
全部由固件 ISR 自己完成。这是 V39 `dispatch=True` 那一类「补硬件应答」，
不是 `btick` 那一类「替固件改内核数据结构」的反事实补全。

⚠️ `period` 是**模型参数**，不是逆向出来的真机频率。直接页 `0x48 = 0x80E8`
可能和分频/周期有关，但没有实证，不写死（KI#191 的真机时基仍未证）。
默认不启用，必须显式 `machine.enable_auto_tick18()` 或 `attach()`，
以免把推断当成实测（与 icu.bind 的纪律一致）。

## 与 V21 `dev_irq2b` 的关系

复用 `Irq2BClock` 的合并/待决/ICR 检查逻辑，只是把向量固定到 0x18，
并给它一个独立的名字与属性（`machine.tick18_clock`）。V21 那条 vec 0x2B
是另一根线，实验里正好当阴性对照：同样 391 次周期中断，tick 计数全 0。
"""

from .dev_irq2b import Irq2BClock

#: 实证常量（探针与自检都从这里取，不各写一份）
VECTOR = 0x18
TBR = 0x000EFC00
VEC_SLOT = TBR + (255 - VECTOR) * 4      # 0x000EFF9C
ISR = 0x04046E
CALL_LDI = 0x04047C
CALL = 0x040482
TICK_HANDLER = 0x040A44
ICR_ADDR = 0x448
ICR_LEVEL = 0x12
ICR_INIT_PC = 0x040456
DIRECT_48 = 0x48
DIRECT_4E = 0x4E
TICK_COUNTER = 0x68000110
DELAY_SENTINEL = 0x68000118
DEFAULT_PERIOD = 1024


class Tick18Clock(Irq2BClock):
    """周期性 raise INT 0x18；同一向量已有待决请求时合并。"""

    name = "tick18clk"

    def __init__(self, machine=None, period=DEFAULT_PERIOD, vector=VECTOR):
        super().__init__(machine, period=period, vector=vector)

    def state(self):
        st = super().state()
        st["向量"] = "0x%02X" % self.vector
        st["ISR"] = "0x%06X" % ISR
        return st


def attach(machine, period=DEFAULT_PERIOD, vector=VECTOR):
    """挂到机器时基上。默认不启用，必须显式调用。"""
    dev = Tick18Clock(machine, period=period, vector=vector)
    machine.devices.append(dev)
    machine.tick18_clock = dev
    if machine.trace is not None:
        dev.trace = machine.trace
    return dev


def detach(machine):
    dev = getattr(machine, "tick18_clock", None)
    if dev is None:
        return None
    if dev in machine.devices:
        machine.devices.remove(dev)
    machine.tick18_clock = None
    return dev
