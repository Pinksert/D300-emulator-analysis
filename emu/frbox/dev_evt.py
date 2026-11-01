# -*- coding: utf-8 -*-
"""「完成位 + 中断」型事件源：给没有外设的那一半硬件补一个最小模型。

## 为什么需要它

任务 5 的循环（`0x0A67C2` 一带）是这样的：

    0x0A67DC  把回调 0x0A6938 写进槽 0x3F074
    0x0A67E6  dmovb #2  → 0x8C      ; 配置
    0x0A67EE  borl #1 / #2 → @0x8F
    0x0A67F6  borh #1 → @0x82       ; 启动
    0x0A6810  int #0x40 (slp_tsk)   ; 睡，等中断

而 vec 0x23 的 ISR（`0x080D8A`）只做一件事：取槽 `0x3F074`，非零就 `call @r0`。
槽里那两个回调（`0x0A6938` / `0x0A687A`）分别对应两种情形：

    0x0A6938  直接 wup_tsk(5)                      ← 「有事，起来」
    0x0A687A  先 btstl #8,@0x80，置位了才 wup_tsk(5) ← 「数据就绪」

**关键**：中断来了还不够，状态位必须同时被硬件置起来，否则回调走「没数据」那一支，
任务被叫醒又立刻睡回去（V12 里 `wup_tsk(5)` 只能叫醒一次，就是这个原因）。

## 两种模式

- `period=N`：每 N 个周期置一次状态位并打中断（**默认**）。对应「转换器/传感器在连续工作」。
- `period=0`：看到 `start` 位的上升沿后等 `delay` 个周期再置位 + 打中断。对应「一次操作一次完成」。

⚠️ 这是**模型**不是手册：状态位（0x80/bit3）、向量（0x23）、周期长度都是我们从固件代码里
读出来的「必要条件」，但真实器件多久完成一次、有没有连续模式，没有硬件佐证。
默认关闭（`Machine.enable_events()` / `--events`），以免影响 V2–V12 的基线。
"""

from .device import Device

START_OFF = 0x8E
START_MASK = 0x08
STATUS_OFF = 0x80
STATUS_MASK = 0x08
VECTOR = 0x23


class CompletionEvent(Device):
    """周期性（或按启动位触发）地置状态位 + 打中断。"""

    name = "event"

    def __init__(self, machine=None, period=2048, delay=256,
                 start=(START_OFF, START_MASK), status=(STATUS_OFF, STATUS_MASK),
                 vector=VECTOR):
        self.period = period            # >0 = 周期性；0 = 按启动位触发
        self.delay = delay
        self.start = start
        self.status = status
        self.vector = vector
        self.count = 0                  # 周期计数
        self.busy = 0                   # 触发模式的倒计时
        self.armed = False
        self.fired = 0
        self.skipped = 0                # 计数到了但状态位已经是 1（不重复打）
        super().__init__(machine)

    def reset(self):
        self.count = 0
        self.busy = 0
        self.armed = False
        self.fired = 0
        self.skipped = 0

    # ---- 内部 ----
    def _fire(self):
        m = self.machine
        if m is None:
            return
        to, tm = self.status
        m.bus.wr(to, 1, m.bus.rd(to, 1) | tm)
        self.fired += 1
        m.raise_irq(self.vector)
        self.dbg("置完成位 0x%02X/0x%02X，挂 vec 0x%02X（第 %d 次）"
                 % (to, tm, self.vector, self.fired))

    def tick(self, cycles):
        if self.period > 0:
            self.count += cycles
            if self.count >= self.period:
                self.count = 0
                self._fire()
            return
        # 触发模式：看启动位的上升沿
        m = self.machine
        if m is None:
            return
        so, sm = self.start
        on = bool(m.bus.rd(so, 1) & sm)
        if on and not self.armed:
            self.armed = True
            self.busy = self.delay
        elif not on:
            self.armed = False
        if self.busy:
            self.busy -= cycles
            if self.busy <= 0:
                self.busy = 0
                self.armed = False
                m.bus.wr(so, 1, m.bus.rd(so, 1) & ~sm & 0xFF)
                self._fire()

    def state(self):
        out = {"已触发": self.fired, "跳过（状态位未取走）": self.skipped}
        if self.period > 0:
            out["周期"] = self.period
            out["计数"] = self.count
        elif self.busy:
            out["进行中"] = self.busy
        return out


def attach(machine, **kw):
    dev = CompletionEvent(machine, **kw)
    machine.devices.append(dev)
    machine.event = dev
    return dev
