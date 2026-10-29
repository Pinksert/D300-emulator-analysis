# -*- coding: utf-8 -*-
"""设备基类 + 直接页（直接寻址 I/O）多路器。

FR 的低端 4KB（0x0000–0x0FFF）是「直接寻址 I/O」区，用 @r13+disp 之类的小位移
就能碰到，D300 的串口、定时器、外部中断控制都在这一带。这里用一个 DirectPage
设备把 0x0000–0x0FFF 整段占住，内部再按偏移分派给具体设备：

    dp.attach(0x10, 0x18, timer)      # 0x10–0x17 定时器
    dp.attach(0x60, 0x68, serial)     # 0x60–0x67 串口

好处是「没建模的偏移」不会变成未映射访问，而是单独记一份 unknown_reads ——
既能保持 io_reads 的统计口径，又能看出固件还摸过哪些我们没管的寄存器。
"""


class Device:
    name = "dev"

    def __init__(self, machine=None):
        self.machine = machine
        self.irq = None            # callable(line, level)：由 machine 注入
        self.trace = None          # Trace 对象，由 machine 注入
        self.reset()

    # ---- 生命周期 ----
    def reset(self):
        pass

    def tick(self, cycles):
        """推进 cycles 个 CPU 周期（machine 每 N 步调一次）"""
        pass

    # ---- 总线接口 ----
    def read(self, off, size):
        return 0

    def write(self, off, size, val):
        pass

    # ---- 调试视图 ----
    def state(self):
        return {}

    def info(self):
        st = self.state()
        if not st:
            return f"{self.name}"
        return f"{self.name}: " + "  ".join(f"{k}={v}" for k, v in st.items())

    # ---- 辅助 ----
    def raise_irq(self, line):
        if self.irq is not None:
            self.irq(line)

    def dbg(self, msg):
        if self.trace is not None:
            self.trace.log("dev", f"{self.name}: {msg}")


class DirectPage(Device):
    """0x0000–0x0FFF 的寄存器多路器"""
    name = "direct-page"

    def __init__(self, machine=None, lo=0x0000, hi=0x1000):
        self.lo = lo
        self.hi = hi
        self.children = []          # (lo, hi, dev, base)
        self.shadow = {}            # 未建模寄存器的影子存储（可被 --poke 打值）
        self.unknown_reads = {}     # off -> 次数
        self.unknown_writes = []    # (off, size, val)
        super().__init__(machine)

    def reset(self):
        self.shadow.clear()
        self.unknown_reads.clear()
        self.unknown_writes.clear()

    def attach(self, lo, hi, dev, base=0):
        for a, b, d, _ in self.children:
            if not (hi <= a or lo >= b):
                raise ValueError(f"直接页 0x{lo:03X}-0x{hi - 1:03X} 与 {d.name} 重叠")
        self.children.append((lo, hi, dev, base))
        self.children.sort(key=lambda x: x[0])
        return dev

    def _route(self, off):
        for lo, hi, dev, base in self.children:
            if lo <= off < hi:
                return dev, base + (off - lo)
        return None, off

    def read(self, off, size):
        v = 0
        for i in range(size):
            dev, o = self._route(off + i)
            if dev is None:
                self.unknown_reads[o] = self.unknown_reads.get(o, 0) + 1
                b = self.shadow.get(o, 0)
            else:
                b = dev.read(o, 1) & 0xFF
            v = (v << 8) | b
        return v

    def write(self, off, size, val):
        for i in range(size):
            dev, o = self._route(off + i)
            b = (val >> (8 * (size - 1 - i))) & 0xFF
            if dev is None:
                self.shadow[o] = b
                self.unknown_writes.append((o, 1, b))
            else:
                dev.write(o, 1, b)

    def state(self):
        out = {}
        for lo, hi, dev, _ in self.children:
            out[f"0x{lo:02X}"] = dev.name
        if self.unknown_reads:
            out["未建模读"] = f"{len(self.unknown_reads)} 个地址/{sum(self.unknown_reads.values())} 次"
        return out

    def owner(self, off):
        dev, _ = self._route(off)
        return dev
