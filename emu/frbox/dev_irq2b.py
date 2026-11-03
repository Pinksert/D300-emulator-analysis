# -*- coding: utf-8 -*-
"""自动节拍的中断 0x2B（V21）。

## 这一层解决的问题

V19/V20 把 INT 0x2B 接上了，但要靠 CLI `--irq2b 240` / monitor `irq2b N`
**手动一次一次地请求** —— 等价于「外面有人反复拉这根线」。真机上这条线
背后应该有个按节拍来的器件（V20 只确认了它的软件签名像 A/D 转换，
身份未证）。V21 把最后一块补上：给这根线配一个**周期时钟**，
挂在 `Machine.step()` 的设备时基上，像 `dev_evt` / `dev_bits` 一样自己跳。

    Irq2BClock(period=1024)   每 1024 个 CPU 周期 raise 一次 vec 0x2B
                              （cycles_per_tick=256，即每 4 个 tick 一次）

之后 `m.run(N)` / `fast N` 里中断会自己来，不用任何人手动请求 ——
包括**在任务代码里、在内核里**被拉起来，这是手动 `raise_once()`
（每次都跑到 reti，中断被串行化）永远给不出的现场。

## 合并（coalesce）

CPU 的受理规则是 `lvl < ILM`，被 ILM 挡住的请求**留在 `cpu.irq_pending`
里反复重试**（见 fr_emu._accept_irq）。如果时钟不理会这一点继续排，
队列会被同一条向量的副本灌满 —— ILM 一放开就洪水一样连发一串，
这不是任何真实硬件的行为。所以本设备在挂请求之前先看一眼：
这条向量已经有待决请求就**跳过这次**，计进 `coalesced`。
效果等价于「电平触发的线已经拉高，再拉一次没有意义」。

## 口径提醒

`period` 是我们定的**模型参数**，不是逆向出来的真机频率 ——
V20 报告里「真机上这条中断多久来一次」仍然没有答案，这里做的是
频率扫描：看覆盖/服务数对频率敏不敏感（对照 V15「事件越多≠覆盖越广」、
V17「投 N 条 +0」、V20「换输入≠换路径」这一族结论）。

计数四个口径，别混：
  fired      时钟真的挂到 ICU 的次数
  coalesced  因为已有待决请求而跳过的次数
  skipped    ICR 还没被固件配置（level=0）而跳过的次数 —— 这时 raise_irq
             会把级别强制成 0x1F，而 0x1F < ILM(0x1F) 永不成立，请求会
             永久卡在待决队列里把后面的到点全合并掉。所以「线没配好
             就不拉」，等固件写完 ICR 再开始。
  accepted   CPU 真正受理进 ISR 的次数（= cpu.irq_log 里 vec=0x2B 的条数）
"""

from .device import Device

VECTOR = 0x2B                    # INT 0x2B（A 段位分派器，见 isr2b.py）
DEFAULT_PERIOD = 1024            # CPU 周期；256 = 每 tick 一次


class Irq2BClock(Device):
    """周期性 raise INT 0x2B；同一向量已有待决请求时合并。"""

    name = "irq2bclk"

    def __init__(self, machine=None, period=DEFAULT_PERIOD, vector=VECTOR):
        self.period = max(1, int(period))     # CPU 周期
        self.vector = vector & 0xFF
        self.count = 0                        # 周期累加器
        self.fired = 0                        # 挂到 ICU 的次数
        self.coalesced = 0                    # 已有待决而跳过的次数
        super().__init__(machine)

    def reset(self):
        self.count = 0
        self.fired = 0
        self.coalesced = 0
        self.skipped = 0

    # ---- 时基 ----
    def tick(self, cycles):
        self.count += cycles
        while self.count >= self.period:
            self.count -= self.period
            self._fire()

    def _fire(self):
        m = self.machine
        if m is None or m.cpu is None:
            return
        if not m.irq_level(self.vector):
            # ICR 还没被固件配置：raise_irq 会强制 level=0x1F，
            # 而 0x1F < ILM(0x1F) 永不成立 → 请求永久卡在待决队列，
            # 把后面的到点全部合并掉（V21 实测：fired 1 / coalesced 139 / accepted 0）。
            # 线没配好就不拉，等固件写完 ICR 再开始。
            self.skipped += 1
            self.dbg("ICR 未配置，线不拉（第 %d 次跳过）" % self.skipped)
            return
        c = m.cpu
        for _lvl, vec in getattr(c, "irq_pending", ()):
            if vec == self.vector:            # 已有待决：电平还拉着，不重复排
                self.coalesced += 1
                self.dbg("待决已有 vec 0x%02X，合并（第 %d 次）"
                         % (self.vector, self.coalesced))
                return
        self.fired += 1
        m.raise_irq(self.vector)              # 级别走固件写进 ICR 的 0x1A
        self.dbg("周期到，挂 vec 0x%02X（第 %d 次）" % (self.vector, self.fired))

    # ---- 视图 ----
    def accepted(self):
        """CPU 真正受理进 ISR 的次数（受理记在 cpu.irq_log）。"""
        m = self.machine
        if m is None or m.cpu is None:
            return 0
        return sum(1 for rec in m.cpu.irq_log if rec[0] == self.vector)

    def pending_now(self):
        m = self.machine
        if m is None or m.cpu is None:
            return 0
        return sum(1 for _l, v in m.cpu.irq_pending if v == self.vector)

    def state(self):
        return {"周期": self.period,
                "fired": self.fired,
                "coalesced": self.coalesced,
                "skipped": self.skipped,
                "accepted": self.accepted(),
                "pending": self.pending_now()}

    def summary(self):
        st = self.state()
        return ("周期 %d 周期：fired %d / coalesced %d / skipped %d / "
                "accepted %d / 待决 %d"
                % (st["周期"], st["fired"], st["coalesced"], st["skipped"],
                   st["accepted"], st["pending"]))


def attach(machine, period=DEFAULT_PERIOD, **kw):
    dev = Irq2BClock(machine, period=period, **kw)
    machine.devices.append(dev)
    machine.irq2b_clock = dev
    return dev


def detach(machine):
    dev = getattr(machine, "irq2b_clock", None)
    if dev is None:
        return None
    if dev in machine.devices:
        machine.devices.remove(dev)
    machine.irq2b_clock = None
    return dev
