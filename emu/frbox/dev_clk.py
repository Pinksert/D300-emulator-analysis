# -*- coding: utf-8 -*-
"""时钟/复位控制寄存器组（A 段直接页 0x480–0x48F）。

## 为什么建这个设备

A 段 1.11 复位后第二跳的初始化子程序（0x00080044，由 0xFC000 的 `call @r13` 调进来）
干了这么一串：

    0x80044  ldi:8  #0x33, r0
    0x80046  ldi:20 #0x7004, r12        ; 先往 RAM 0x7004 写 0x33
    0x8004A  stb    r0, @r12
    0x8004C  ldi:8  #0x01, r0
    0x8004E  ldi:20 #0x486, r12
    0x80052  stb    r0, @r12            ; [0x486] = 0x01
    0x80054  ldi:8  #0xF0, r0
    0x80056  ldi:20 #0x487, r12
    0x8005A  stb    r0, @r12            ; [0x487] = 0xF0
    0x8005C  ldi:8  #0x34, r0
    0x8005E  ldi:20 #0x484, r12
    0x80062  stb    r0, @r12            ; [0x484] = 0x34
    0x80064  ldi:8  #0x08, r0
    0x80066  ldi:20 #0x482, r12
    0x8006A  stb    r0, @r12            ; [0x482] = 0x08  ← 位3 = 启动
    0x8006C  ldi:8  #0xA5, r0
    0x8006E  ldi:20 #0x483, r12
    0x80072  stb    r0, @r12            ; [0x483] = 0xA5  ┐ 两键写保护
    0x80074  ldi:8  #0x5A, r0
    0x80076  stb    r0, @r12            ; [0x483] = 0x5A  ┘
    0x80078  ldi:20 #0x482, r0
    0x8007C  btsth  #8, @r0             ; 测 [0x482] 的位7
    0x8007E  beq    0x8007C             ; 不置位就原地等
    0x80080  ldi:8  #0x36, r0
    0x80082  ldi:20 #0x484, r12
    0x80086  stb    r0, @r12            ; [0x484] = 0x36  ← 0x34→0x36 = 位1 置位，锁定确认

「先写 0x34 → 置启动位 → 两键解锁 A5/5A → 空转等位7 → 等到后改成 0x36」是教科书式的
**PLL/时钟发生器启动等待**：位3=启动，位7=就绪（只有硬件会置它），位1=切换完成。

全 A 段 ROM 里只有 6 条指令写这组寄存器（0x80052/0x8005A/0x80062/0x8006A/0x80072/0x80076）
外加一处 ISR（0x80F60：`bandh #7,@r0` 清低位 + 读 0x3F09C 计数）——**没有任何代码写过位7**，
所以位7 只能来自硬件。原先用 `--latch 0x482=0x88:2` 硬灌是作弊，这里补成真模型。

## 建模口径

* 寄存器影子按字节存，读回影子（未写过的位为 0）。
* 写 0x482 且位3=1 → 武装倒计时，倒计时到点后把位7 置 1（模拟 PLL 锁定时间）。
* 写 0x483：0xA5 → 记住；紧接 0x5A → 解锁（只记录，不改变锁定行为，
  因为固件是在启动之后才解锁、且实测解锁不影响等待）。
* 倒计时用「tick 次数」计时，tick 由 machine 每 cycles_per_tick 步调一次，
  所以 8 次 tick ≈ 2048 步 —— 和真机上 PLL 稳定等待的量级相符，也不再让
  自旋检测在 20 万步后误报。
"""
from .device import Device

LO = 0x480
SIZE = 0x10

OFF_START = 0x02       # 0x482：位3 启动 / 位7 就绪
OFF_KEY = 0x03         # 0x483：写保护两键
OFF_CFG = 0x04         # 0x484：0x34 → 0x36
KEY1, KEY2 = 0xA5, 0x5A


class ClockReset(Device):
    name = "clock-reset"

    def __init__(self, machine=None, lock_ticks=8):
        self.lock_ticks = lock_ticks
        super().__init__(machine)

    def reset(self):
        self.reg = bytearray(SIZE)
        self.armed = False
        self.count = 0
        self.key1_seen = False
        self.unlocked = False
        self.locks = 0
        self.log = []                       # [(off, val)] 只在调试时看

    # ---------------------------------------------------------------- 总线
    def read(self, off, size=1):
        if not (0 <= off < SIZE):
            return 0
        return self.reg[off]

    def write(self, off, size, val):
        if not (0 <= off < SIZE):
            return
        v = val & 0xFF
        self.log.append((off, v))
        if off == OFF_KEY:
            if v == KEY1:
                self.key1_seen = True
            elif v == KEY2 and self.key1_seen:
                self.unlocked = True
                self.key1_seen = False
            else:
                self.key1_seen = False
            self.reg[off] = v
            return
        if off == OFF_START:
            self.reg[off] = v & 0x7F        # 位7 是只读状态位，写入忽略
            if v & 0x08:
                self.armed = True
                self.count = self.lock_ticks
            return
        self.reg[off] = v

    # ---------------------------------------------------------------- 时间
    def tick(self, cycles):
        if not self.armed:
            return
        self.count -= 1
        if self.count <= 0:
            self.armed = False
            self.reg[OFF_START] |= 0x80
            self.locks += 1
            self.dbg(f"PLL 锁定，[0x482] = 0x{self.reg[OFF_START]:02X}")

    # ---------------------------------------------------------------- 视图
    def state(self):
        return {
            "0x482": f"0x{self.reg[OFF_START]:02X}",
            "0x483": f"0x{self.reg[OFF_KEY]:02X}",
            "0x484": f"0x{self.reg[OFF_CFG]:02X}",
            "锁定次数": self.locks,
            "已解锁": self.unlocked,
            "倒计时": self.count if self.armed else "-",
        }


def attach(machine, lock_ticks=8):
    """把时钟寄存器组挂到机器的直接页上，并加入 tick 列表"""
    from .device import DirectPage  # noqa: F401
    dev = ClockReset(machine, lock_ticks=lock_ticks)
    machine.dp.attach(LO, LO + SIZE, dev)
    machine.devices.append(dev)
    dev.trace = machine.trace
    dev.irq = None
    return dev
