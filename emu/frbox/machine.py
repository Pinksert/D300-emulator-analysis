# -*- coding: utf-8 -*-
"""机器模型：内存映射 + 复位向量 + 运行循环（QEMU MachineClass 的简化版）。

D300 不是「一个 CPU + 一份固件」，而是至少 A（IO 协处理）+ B（主控）两套独立
处理器子系统，各有自己的固件、自己的向量表、自己的 RAM。因此这里建**两台机器**
而不是一台：

    d300-a   ROM 0x00080000 起 512KB+2   RAM 0x00001000–0x0007FFFF   TBR 0x000FFC00
    d300-b   ROM 0x00040000 起 7MB       RAM 0x00001000–0x0003FFFF   TBR 0x000EFC00

这些数字不是猜的，来源都在固件自身：
  * ROM 基址与长度 = 镜像文件大小（A 524290 = 0x80002，B 7340032 = 0x700000）
  * TBR = 向量表所在（A 段槽 0x000FFFFC 读到复位入口 0x000FC000；B 段槽 0x000EFFFC 读到 0x00040000）
  * RAM 上界 = 固件真正写过的高地址（A 段写到 0x0007F81C 一带，B 段栈用了 0x0003F000）
"""

import os

from fr_emu import Fr, TABLE

from .bus import AddressSpace
from .device import DirectPage
from .dev_clk import attach as attach_clk
from .dev_serial import SerialIf
from .dev_timer import ReloadTimer
from .icu import Icu
from .trace import Trace

_PKG = os.path.dirname(os.path.abspath(__file__))       # emu/frbox
_EMU = os.path.dirname(_PKG)                            # emu/
_ROOT = os.path.dirname(_EMU)                           # 仓库根（含 data/）


def _pick_fw_dir():
    """固件目录解析顺序，先存在的先用：

    1. 环境变量 FRBOX_FW_DIR
    2. 仓库 data/（本仓库把 A3000111.bin / B3000111.bin 放在这里）
    3. 仓库同级 D300Update/（尼康官方更新包解压后的目录名）
    """
    env = os.environ.get("FRBOX_FW_DIR")
    if env:
        return env
    for cand in (os.path.join(_ROOT, "data"), os.path.join(_ROOT, "D300Update")):
        if os.path.isdir(cand):
            return cand
    return os.path.join(_ROOT, "data")


FW_DIR = _pick_fw_dir()

MACHINES = {}


def register(cls):
    MACHINES[cls.name] = cls
    return cls


def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Machine:
    name = "machine"
    desc = ""
    rom_base = 0
    rom_size = 0
    ram_base = 0x00001000
    ram_size = 0
    tbr = 0
    ssp = 0
    default_firmware = ""
    cycles_per_tick = 256
    clk_lock_ticks = 8                   # 时钟寄存器 0x482 位7 就绪所需 tick 数
    # 额外区域：[(基址, 长度, 名字)]，给「固件实际访问到、但我们一开始没建模」的窗口留口子
    extra_ram = []
    known_windows = []

    def __init__(self, trace=None, serial_console=False, clk_lock_ticks=None):
        self.trace = trace or Trace()
        self.serial_console = serial_console
        if clk_lock_ticks is not None:
            self.clk_lock_ticks = clk_lock_ticks
        self.bus = AddressSpace(self.name)
        self.dp = DirectPage(self)
        self.serial = SerialIf(self, tx_sink=self._serial_tx)
        self.timer = ReloadTimer(self)
        self.icu = Icu(self)
        self.devices = [self.dp, self.serial, self.timer]
        self.itron = None
        self.cpu = None
        self.step_count = 0
        self.fw_path = None
        self.fw_crc_ok = None
        self.fw_crc_read = 0
        self.fw_crc_calc = 0
        self.spin = None                 # (基准 PC, 步数) 自旋检测结果
        self._spin_base = None
        self._spin_count = 0
        self.spin_window = 0x100
        self.spin_limit = 200000
        self.pokes = []                  # [(addr, size, val)] 复位后写入（模拟「外设已就绪」）
        self.task_boot = None            # (entry, sp, arg) 手工启动任务留下的记录
        for d in self.devices:
            d.trace = self.trace
        self._build()
        self.reset()

    # ------------------------------------------------------------------ 构建
    def _build(self):
        self.bus.map_io(0x0000, 0x1000, self.dp, "direct-page")
        self.bus.map_ram(self.ram_base, self.ram_size, "ram")
        self.bus.load_rom(self.rom_base, b"\xFF" * self.rom_size)
        for base, size, name in self.extra_ram:
            self.bus.map_ram(base, size, name)
        self.dp.attach(0x10, 0x18, self.timer)
        self.dp.attach(0x60, 0x68, self.serial)
        # 0x480–0x48F：时钟/复位控制寄存器组，位7 由硬件置位（见 dev_clk.py 的注释）
        self.clk = attach_clk(self, lock_ticks=self.clk_lock_ticks)
        # 只有两条线有实证，其余留空——推断的绑定要显式加
        self.icu.bind(0x1B, 0x1B, 8, "串口，实测（ISR 0x0012FDB4）")
        self.icu.bind(0x20, 0x20, 8, "EXT9，实测（ISR 0x0012FE54）")
        self.serial.irq_line = 0x1B
        self.timer.irq_line = None

    # ------------------------------------------------------------------ 复位
    def reset(self):
        self.cpu = Fr(self.bus, TABLE)
        c = self.cpu
        c.dr[0] = self.tbr
        c.dr[2] = self.ssp
        c.r[15] = self.ssp
        c.s = 0
        c.i = 0
        c.ilm = 0x1F                 # 复位默认屏蔽全部中断，等固件自己放开
        if self.itron is not None:
            c.svc_hook = self.itron
        for d in self.devices:
            d.reset()
        self.icu.reset()
        self.step_count = 0
        self.spin = None
        self._spin_base = None
        self._spin_count = 0
        self.entry = self.vector(0x00)
        c.pc = self.entry
        for a, s, v in self.pokes:
            self.bus.wr(a, s, v)
        return c

    def poke(self, addr, size, val):
        """往总线写一个值：可打未建模寄存器的影子存储，也可打真实设备寄存器"""
        self.pokes.append((addr, size, val))
        self.bus.wr(addr, size, val)
        return self

    # ------------------------------------------------------------------ 固件
    def load(self, path=None):
        path = path or self.default_firmware
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"固件不存在：{path}")
        data = open(path, "rb").read()
        self.fw_path = path
        payload, crc = data[:-2], int.from_bytes(data[-2:], "big")
        calc = crc16_xmodem(payload)
        self.fw_crc_read, self.fw_crc_calc = crc, calc
        self.fw_crc_ok = crc == calc
        if not self.fw_crc_ok:
            self.trace.log("guest", f"CRC16 不符：文件 0x{crc:04X} / 计算 0x{calc:04X}")
        self.bus.load_rom(self.rom_base, data)
        if len(data) != self.rom_size + 2 and len(data) != self.rom_size:
            self.trace.log("guest", f"镜像大小 {len(data)} 与机器配置 {self.rom_size + 2} 不符")
        self.reset()
        return self

    def attach_itron(self, seg=None):
        """挂上表驱动 μITRON 层：INT #0x40 当成立即返回的服务调用"""
        from fr_itron import ItronStub
        seg = seg or ("A" if self.name.endswith("a") else "B")
        self.itron = ItronStub(seg=seg, verbose=False)
        self.cpu.svc_hook = self.itron
        return self.itron

    # ------------------------------------------------------------------ 运行
    def on_irq(self, vec, level):
        if self.cpu is not None:
            self.cpu.request_irq(vec, level)

    def vector(self, n):
        return self.bus.rd(self.tbr + (255 - (n & 0xFF)) * 4, 4)

    def run(self, max_steps=100000, stop_pcs=(), spin_guard=True):
        c = self.cpu
        stop = set(stop_pcs)
        done = 0
        for _ in range(max_steps):
            if c.halted:
                break
            if stop and c.pc in stop:
                break
            pc0 = c.pc
            if self.trace.has("in_asm"):
                self._log_asm(pc0)
            if spin_guard:
                if self._spin_base is None or abs(pc0 - self._spin_base) > self.spin_window:
                    self._spin_base, self._spin_count = pc0, 1
                else:
                    self._spin_count += 1
                    if self._spin_count >= self.spin_limit:
                        self.spin = (self._spin_base, self._spin_count)
                        if self.trace is not None:
                            self.trace.log("exec", f"检测到自旋：0x{pc0:08X} 附近连续执行 "
                                                   f"{self._spin_count} 步未离开")
                        break
            c.step()
            self.step_count += 1
            done += 1
            if self.cycles_per_tick and self.step_count % self.cycles_per_tick == 0:
                for d in self.devices:
                    d.tick(self.cycles_per_tick)
        return done

    def step(self, n=1):
        c = self.cpu
        for _ in range(n):
            if c.halted:
                break
            pc0 = c.pc
            if self.trace.has("in_asm"):
                self._log_asm(pc0)
            c.step()
            self.step_count += 1
            if self.cycles_per_tick and self.step_count % self.cycles_per_tick == 0:
                for d in self.devices:
                    d.tick(self.cycles_per_tick)

    # ------------------------------------------------------------ 任务启动
    def boot_task(self, entry, sp=None, arg=0, stop_pc=None, max_steps=400000):
        """手工启动一个任务：跳过 REALOS 调度器，直接构造初始上下文跳进去。

        背景（V7）：A 段复位链第三跳 0x804FA 是纯 `ret`（1.02/1.03/1.10/1.11
        四版一致），main 的第二条 call 0x80520 也是纯 `ret` —— 内核对象建好、
        状态 0、从未 sta_tsk。所以正常启动一定在 main 返回后 reti 弹 PC=0。

        entry  任务函数入口（A 段任务 1 = 0x0008133A）
        sp     初始栈指针；None 则沿用复位入口设的 0x0004616C
        arg    传进 r4 的参数（任务函数第一句通常是 mov r4,r8）
        stop_pc  先跑到这个 PC 再跳（默认 None = 不铺垫；A 段建议 0x00080506）
        """
        c = self.cpu
        if stop_pc is not None:
            self.run(max_steps=max_steps, stop_pcs={stop_pc}, spin_guard=False)
        c.pc = entry
        if sp is not None:
            c.r[15] = sp
        c.r[4] = arg
        c.r[5] = 0
        c.halted = False
        self.task_boot = (entry, c.r[15], arg)
        if self.trace.has("guest"):
            self.trace.log("guest", f"boot_task entry=0x{entry:08X} "
                                    f"sp=0x{c.r[15]:08X} arg=0x{arg:X}")
        return self.task_boot

    def task_table(self):
        """解析 A 段任务定义表（0xFE00C 起变长条目）

        已确认字段：条目里 0x00010000 形态的字 = 任务号(高 16 位)。
        """
        out = []
        if not hasattr(self, "bus"):
            return out
        pos = 0x000FE00C
        for _ in range(16):
            head = self.bus.rd(pos, 4)
            if head == 0 and len(out):
                break
            out.append(pos)
            pos += 0x10
            if pos > 0x000FE180:
                break
        return out

    # ------------------------------------------------------------------ 视图
    def fetch_bytes(self, addr, n):
        r = self.bus.find(addr)
        if r is not None and r.kind in ("rom", "ram") and addr + n <= r.end:
            o = addr - r.base
            return bytes((r.data if r.kind == "rom" else r.buf)[o:o + n])
        return self.bus.rd(addr, n).to_bytes(n, "big")

    def disasm_at(self, addr, n=1):
        """在 monitor 里用：反汇编 n 条"""
        data = self.fetch_bytes(addr, 16 * n)
        out, off, a = [], 0, addr
        for _ in range(n):
            ins = TABLE.decode(data, off)
            if ins is None:
                out.append(f"0x{a:08X}: <无法解码>")
                break
            mnem, _base, vals, size, w = ins
            ops = ", ".join(f"{k}={v if not isinstance(v, int) else hex(v)}"
                            for k, v in vals.items())
            out.append(f"0x{a:08X}: {w:0{size * 2}X}  {mnem} {ops}".rstrip())
            a += size
            off += size
        return "\n".join(out)

    def _log_asm(self, pc):
        ins = TABLE.decode(self.fetch_bytes(pc, 10), 0)
        if ins is None:
            self.trace.log("in_asm", f"0x{pc:08X}: <无法解码>")
            return
        mnem, _base, vals, size, w = ins
        ops = ", ".join(f"{k}={v if not isinstance(v, int) else hex(v)}" for k, v in vals.items())
        self.trace.log("in_asm", f"0x{pc:08X}: {w:0{size * 2}X}  {mnem} {ops}")

    def info(self):
        c = self.cpu
        lines = [
            f"机器      {self.name}  —— {self.desc}",
            f"固件      {self.fw_path or '（未加载）'}"
            + (f"   CRC16 {'OK' if self.fw_crc_ok else 'BAD'}"
               f"（文件 0x{self.fw_crc_read:04X} / 计算 0x{self.fw_crc_calc:04X}）"
               if self.fw_crc_ok is not None else ""),
            f"复位入口  0x{self.entry:08X}   （向量 0x00 槽 = TBR+0x3FC = 0x{self.tbr + 0x3FC:08X}）",
            f"当前 PC   0x{c.pc:08X}   已执行 {self.step_count:,} 步",
            f"TBR       0x{self.tbr:08X}    SSP 0x{c.dr[2]:08X}   ILM {c.ilm}   I={c.i}",
        ]
        if c.halted:
            lines.append(f"已停机    {c.halt_reason}")
        if self.spin:
            base, cnt = self.spin
            lines.append(f"自旋      PC 在 0x{base:08X}±0x{self.spin_window:X} 内连续 {cnt:,} 步未离开")
        return "\n".join(lines)

    def diagnose(self):
        """一份「为什么跑不下去」的报告：内存树 + 未映射审计 + 外设审计 + 设备状态"""
        out = [self.info(), "", self.bus.mtree(), "", self.bus.audit(), "", self.bus.io_audit()]
        if self.known_windows:
            out.append("")
            out.append("已知但未建模的窗口（看过访问痕迹，语义不明）：")
            for base, note in self.known_windows:
                out.append(f"  0x{base:08X}  {note}")
        unknown = self.dp.unknown_reads
        if unknown:
            top = sorted(unknown.items(), key=lambda x: -x[1])[:10]
            out.append("")
            out.append(f"直接页未建模寄存器：{len(unknown)} 个 / {sum(unknown.values())} 次读")
            out.append("  " + "  ".join(f"0x{a:03X}×{c}" for a, c in top))
        if self.dp.unknown_writes:
            wr = {}
            for off, size, v in self.dp.unknown_writes:
                wr[off] = wr.get(off, 0) + 1
            top = sorted(wr.items(), key=lambda x: -x[1])[:10]
            out.append(f"直接页未建模写：{len(self.dp.unknown_writes)} 次")
            out.append("  " + "  ".join(f"0x{a:03X}×{c}" for a, c in top))
        out.append("")
        for d in self.devices:
            out.append("  " + d.info())
        if self.itron is not None:
            out.append("")
            out.append("μITRON 服务调用：")
            out.append(self.itron.report(12))
        return "\n".join(out)

    def stats(self):
        s = dict(self.bus.stats())
        s.update({
            "steps": self.step_count,
            "pc": self.cpu.pc,
            "halted": self.cpu.halted,
            "irq_taken": len(self.cpu.irq_log),
            "serial_tx": self.serial.tx_count,
            "timer_underflow": self.timer.underflows,
            "dp_unknown_read_addr": len(self.dp.unknown_reads),
            "dp_unknown_read_times": sum(self.dp.unknown_reads.values()),
            "dp_unknown_write_times": len(self.dp.unknown_writes),
            "spin_pc": f"0x{self.spin[0]:08X}" if self.spin else "-",
        })
        return s

    def _serial_tx(self, data):
        if self.serial_console:
            self.trace.out.write(data.decode("latin-1"))
            self.trace.out.flush()


# ---------------------------------------------------------------------- 机器
@register
class D300A(Machine):
    name = "d300-a"
    desc = "D300 A 段（IO 协处理，512KB 固件）"
    rom_base = 0x00080000
    rom_size = 0x00080000
    ram_base = 0x00001000
    ram_size = 0x0007F000
    tbr = 0x000FFC00
    ssp = 0x0007F000
    default_firmware = os.path.join(FW_DIR, "A3000111.bin")
    cycles_per_tick = 256


@register
class D300B(Machine):
    name = "d300-b"
    desc = "D300 B 段（主控，7MB 固件）"
    rom_base = 0x00040000
    rom_size = 0x00700000
    ram_base = 0x00001000
    ram_size = 0x0003F000
    tbr = 0x000EFC00
    ssp = 0x0003F000
    default_firmware = os.path.join(FW_DIR, "B3000111.bin")
    cycles_per_tick = 256
    # 依据（全部来自本次 boot 实验的未映射访问审计）：
    #   * 写入 0x9F58C000 起 1307 次、0x9F5AA000–0x9F5AF000 五页共 5120 次（步长 4）
    #   * 早先静态分析里串口 ISR 读 0x9F58DA86、EXT9 ISR 读 0x9F58DA85
    # 说明 0x9F58xxxx 是主控真正的数据 RAM，而不是「未映射」。先按 1MB 给，
    # 上界取观测最高写入（0x9F5AFxxx）之上。
    extra_ram = [(0x9F580000, 0x00100000, "ram-sdram")]
    known_windows = [
        (0x68000000, "外设窗口：读到 0x68000004/0x68000174/0x68000618，写 3048 个地址（多为字节）"),
        (0x50000000, "外设窗口：复位代码向 0x50000000 写 1，随后读 0x50000001/0x50000008"),
        (0x4000D000, "外设窗口：写 0x4000D210/0x4000D240/0x4000D310…（0x0/0x8 交替，像通道使能）"),
        (0x40000000, "别名窗口：0x4002xxxx–0x401Fxxxx 之间有约 70 次散写，写进去不报错、读回不影响流程"),
        (0x9F800000, "零星 4 次写，未建模"),
    ]


def machine_names():
    return sorted(MACHINES)


def create(name, **kw):
    key = name.lower().replace("_", "-")
    if key not in MACHINES:
        raise KeyError(f"没有机器 {name}，可选：{', '.join(machine_names())}")
    return MACHINES[key](**kw)
