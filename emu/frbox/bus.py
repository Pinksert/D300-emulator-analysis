# -*- coding: utf-8 -*-
"""地址空间：ROM / RAM / 外设三类区域，按地址分派。

对应 QEMU 的 AddressSpace + MemoryRegion。设计要点：

1. **区域优先，设备其次**：CPU 只认 rd/wr，不知道背后是内存还是寄存器。
2. **跨区访问按字节拆**：QEMU 里 4 字节读跨到设备会拆成多次设备访问，这里同样处理。
3. **未映射访问必须记账**：读返回 0、写丢弃，但地址和次数都留档 —— 固件到底碰了
   哪些还没建模的硬件，全靠这张表。
4. **兼容旧实验**：保留 load_rom / io_value / io_reads / io_writes / all_writes /
   watch / access_log 这些字段，早先的 exp_*.py 可以直接把 AddressSpace 当 Mem 用。
"""
import bisect
import sys

from . import _ROOT  # noqa: F401  （确保 fr_emu 可导入）
from fr_emu import M32


class BusError(Exception):
    pass


class Region:
    """一段连续地址。kind: rom / ram / io"""

    def __init__(self, name, base, size, kind):
        self.name = name
        self.base = base & M32
        self.size = size
        self.end = self.base + size
        self.kind = kind

    def __repr__(self):
        return f"<{self.kind} {self.name} 0x{self.base:08X}+0x{self.size:X}>"


class RomRegion(Region):
    def __init__(self, name, base, data):
        super().__init__(name, base, len(data), "rom")
        self.data = bytearray(data)

    @property
    def writable(self):
        return False


class RamRegion(Region):
    def __init__(self, name, base, size, fill=0x00):
        super().__init__(name, base, size, "ram")
        self.buf = bytearray([fill]) * size


class IoRegion(Region):
    def __init__(self, name, base, size, dev, dev_base=0):
        super().__init__(name, base, size, "io")
        self.dev = dev
        self.dev_base = dev_base


class AddressSpace:
    def __init__(self, name="bus"):
        self.name = name
        self.regions = []                 # 按 base 排序
        self._starts = []
        self._cache = None                # (base, end, region) 命中缓存
        # ---- 统计与审计 ----
        self.unmapped_reads = {}          # addr -> 次数
        self.unmapped_writes = []         # (addr, size, val, pc)
        self.io_reads = {}                # addr -> 次数（外设访问，含直接页）
        self.io_writes = []               # (addr, size, val, pc)
        self.writes_to_rom = []           # (addr, size, val, pc)
        self.all_writes = []              # (addr, size, val, pc) 只记 RAM
        self.watch = set()
        self.access_log = []              # (kind, addr, size, val, pc)
        self.io_value = {}                # addr -> 强制读值（测试打桩，优先于设备）
        self.io_after = {}                # addr -> (次数阈值, 值) 轮询逃生阀
        self.strict_unmapped = False      # True 则未映射访问直接抛异常
        self.mmio_end = 0x1000            # 仅作语义提示：直接页 IO 的上界
        # 兼容 fr_emu.Fr.fetch 的快路径（它直接读 m.rom / m.rom_base）
        self.rom = b""
        self.rom_base = 0
        self.rom_end = 0

    def _sync_rom(self):
        for r in self.regions:
            if r.kind == "rom":
                self.rom = r.data
                self.rom_base = r.base
                self.rom_end = r.end
                return
        self.rom, self.rom_base, self.rom_end = b"", 0, 0

    # ------------------------------------------------------------------ 映射
    def _insert(self, r):
        i = bisect.bisect_left(self._starts, r.base)
        if i < len(self.regions) and self.regions[i].base == r.base:
            raise BusError(f"地址 0x{r.base:08X} 已被 {self.regions[i].name} 占用")
        if i > 0 and self.regions[i - 1].end > r.base:
            raise BusError(f"与 {self.regions[i - 1].name} 重叠")
        if r.end > 0x100000000:
            raise BusError("区域越出 32 位地址空间")
        self.regions.insert(i, r)
        self._starts.insert(i, r.base)
        self._cache = None
        if r.kind == "rom":
            self._sync_rom()
        return r

    def map_rom(self, base, data, name="rom"):
        return self._insert(RomRegion(name, base, data))

    def map_ram(self, base, size, name="ram"):
        return self._insert(RamRegion(name, base, size))

    def map_io(self, base, size, dev, name="io", dev_base=0):
        return self._insert(IoRegion(name, base, size, dev, dev_base))

    def load_rom(self, base, data):
        """兼容旧接口：Mem.load_rom 的等价物（先抹掉旧 rom 再映射）"""
        self.regions = [r for r in self.regions if r.kind != "rom"]
        self._starts = [r.base for r in self.regions]
        self._cache = None
        self._sync_rom()
        return self.map_rom(base, data)

    # ------------------------------------------------------------------ 查找
    def find(self, a):
        c = self._cache
        if c is not None and c[0] <= a < c[1]:
            return c[2]
        i = bisect.bisect_right(self._starts, a) - 1
        if i >= 0:
            r = self.regions[i]
            if r.base <= a < r.end:
                self._cache = (r.base, r.end, r)
                return r
        return None

    def region_at(self, a, n=1):
        r = self.find(a)
        if r is not None and a + n <= r.end:
            return r
        return None

    # ------------------------------------------------------------------ 读
    def _maybe_watch(self, kind, a, n, v, pc):
        if a in self.watch:
            self.access_log.append((kind, a, n, v, pc))

    def _io_read(self, r, a, n, pc):
        off = a - r.base
        self.io_reads[a] = self.io_reads.get(a, 0) + 1
        if a in self.io_after and self.io_reads[a] >= self.io_after[a][0]:
            v = self.io_after[a][1]
        elif a in self.io_value:
            v = self.io_value[a]
        else:
            v = r.dev.read(r.dev_base + off, n)
        self._maybe_watch("R", a, n, v, pc)
        return v & M32

    def rd(self, a, n, pc=0):
        a &= M32
        r = self.find(a)
        if r is not None and a + n <= r.end:
            if r.kind == "rom":
                o = a - r.base
                v = int.from_bytes(r.data[o:o + n], "big")
            elif r.kind == "ram":
                o = a - r.base
                v = int.from_bytes(r.buf[o:o + n], "big")
            else:
                return self._io_read(r, a, n, pc)
            self._maybe_watch("R", a, n, v, pc)
            return v & M32
        if r is None:
            self.unmapped_reads[a] = self.unmapped_reads.get(a, 0) + 1
            if self.strict_unmapped:
                raise BusError(f"未映射读 0x{a:08X} ({n} 字节) @pc=0x{pc:08X}")
            self._maybe_watch("R", a, n, 0, pc)
            return 0
        # 跨区：逐字节拼（设备不会被拆成 1 字节访问以外的东西）
        v = 0
        for i in range(n):
            v = (v << 8) | (self.rd(a + i, 1, pc) & 0xFF)
        return v & M32

    # ------------------------------------------------------------------ 写
    def wr(self, a, n, v, pc=0):
        a &= M32
        v &= M32
        r = self.find(a)
        if r is not None and a + n <= r.end:
            if r.kind == "rom":
                self.writes_to_rom.append((a, n, v, pc))
                return
            if r.kind == "ram":
                o = a - r.base
                r.buf[o:o + n] = (v & ((1 << (8 * n)) - 1)).to_bytes(n, "big")
                self.all_writes.append((a, n, v, pc))
                self._maybe_watch("W", a, n, v, pc)
                return
            self.io_writes.append((a, n, v, pc))
            r.dev.write(r.dev_base + (a - r.base), n, v)
            self._maybe_watch("W", a, n, v, pc)
            return
        if r is None:
            self.unmapped_writes.append((a, n, v, pc))
            if self.strict_unmapped:
                raise BusError(f"未映射写 0x{a:08X} <- 0x{v:X} @pc=0x{pc:08X}")
            self._maybe_watch("W", a, n, v, pc)
            return
        for i in range(n):
            self.wr(a + i, 1, (v >> (8 * (n - 1 - i))) & 0xFF, pc)

    # ------------------------------------------------------------------ 视图
    def mtree(self):
        out = [f"地址空间 {self.name}（{len(self.regions)} 个区域）"]
        for r in self.regions:
            pad = " " * max(0, 34 - len(r.name))
            dev = f"  dev={r.dev.name}" if r.kind == "io" else ""
            out.append(f"  0x{r.base:08X}-0x{r.end - 1:08X}  {r.size:>9,}  {r.kind:<4}  {r.name}{pad}{dev}")
        if self.unmapped_reads:
            n = len(self.unmapped_reads)
            tot = sum(self.unmapped_reads.values())
            top = sorted(self.unmapped_reads.items(), key=lambda x: -x[1])[:5]
            out.append(f"  未映射读：{n} 个地址 / {tot} 次；最频繁 "
                       + "，".join(f"0x{a:08X}×{c}" for a, c in top))
        if self.unmapped_writes:
            n = len(self.unmapped_writes)
            tot = sum(1 for _ in self.unmapped_writes)
            addrs = {}
            for a, s, v, pc in self.unmapped_writes:
                addrs[a] = addrs.get(a, 0) + 1
            top = sorted(addrs.items(), key=lambda x: -x[1])[:5]
            out.append(f"  未映射写：{n} 个地址 / {tot} 次；最频繁 "
                       + "，".join(f"0x{a:08X}×{c}" for a, c in top))
        return "\n".join(out)

    def stats(self):
        return {
            "regions": len(self.regions),
            "unmapped_read_addr": len(self.unmapped_reads),
            "unmapped_read_times": sum(self.unmapped_reads.values()),
            "unmapped_write_addr": len({a for a, *_ in self.unmapped_writes}),
            "unmapped_write_times": len(self.unmapped_writes),
            "io_read_addr": len(self.io_reads),
            "io_read_times": sum(self.io_reads.values()),
            "io_write_times": len(self.io_writes),
            "ram_write_times": len(self.all_writes),
            "rom_write_times": len(self.writes_to_rom),
        }

    # ------------------------------------------------------------------ 审计
    @staticmethod
    def _by_page(items):
        """按 4KB 页汇总，返回 [(页基址, 次数, 地址数)] 降序"""
        pages = {}
        for a, c in items:
            p = a & 0xFFFFF000
            cnt, naddr = pages.get(p, (0, 0))
            pages[p] = (cnt + c, naddr + 1)
        return sorted(((p, c, n) for p, (c, n) in pages.items()), key=lambda x: -x[1])

    def audit(self, top=8):
        """未映射访问按 4KB 页汇总 —— 用来判断「固件在碰哪块还没建模的硬件」"""
        lines = []
        rd = list(self.unmapped_reads.items())
        if rd:
            lines.append(f"未映射读（{sum(c for _, c in rd)} 次 / {len(rd)} 个地址）：")
            for p, c, n in self._by_page(rd)[:top]:
                sample = sorted(a for a, _ in rd if a & 0xFFFFF000 == p)[:3]
                lines.append(f"  0x{p:08X} 页  {c:>7} 次  {n:>3} 个地址  例 "
                             + " ".join(f"0x{s:08X}" for s in sample))
        wr = {}
        for a, s, v, pc in self.unmapped_writes:
            wr[a] = wr.get(a, 0) + 1
        if wr:
            lines.append(f"未映射写（{len(self.unmapped_writes)} 次 / {len(wr)} 个地址）：")
            for p, c, n in self._by_page(list(wr.items()))[:top]:
                sample = sorted(a for a in wr if a & 0xFFFFF000 == p)[:3]
                lines.append(f"  0x{p:08X} 页  {c:>7} 次  {n:>3} 个地址  例 "
                             + " ".join(f"0x{s:08X}" for s in sample))
        return "\n".join(lines) if lines else "没有未映射访问：固件碰到的每个地址都在区域里。"

    def io_audit(self, top=10):
        if not self.io_reads:
            return "没有外设访问"
        rows = self._by_page(list(self.io_reads.items()))[:top]
        out = [f"外设访问：{sum(self.io_reads.values())} 次读 / {len(self.io_writes)} 次写，按页汇总"]
        for p, c, n in rows:
            tops = sorted(((a, k) for a, k in self.io_reads.items() if a & 0xFFFFF000 == p),
                          key=lambda x: -x[1])[:4]
            out.append(f"  0x{p:08X} 页  {c:>7} 次  {n:>3} 个地址  "
                       + " ".join(f"0x{a:03X}×{k}" for a, k in tops))
        return "\n".join(out)
