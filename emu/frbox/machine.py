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
from .autospin import SpinBreaker
from .device import DirectPage
from .dev_clk import attach as attach_clk
from .dev_serial import SerialIf
from .dev_timer import ReloadTimer
from .dev_xfer import attach as attach_xfer
from .dev_dispatch import attach as attach_dispatch
from .dev_prio import attach as attach_prio
from .icu import Icu
from .trace import Trace

def _fw_dir():
    """固件目录：环境变量 FRBOX_FW_DIR → 仓库 data/ → 仓库同级 D300Update/。

    这个顺序是刻意定的：仓库里带 `data/A3000111.bin` 时 clone 下来就能跑，
    不用任何设置；想用别处的解包产物时再指 `FRBOX_FW_DIR`。
    """
    env = os.environ.get("FRBOX_FW_DIR")
    if env and os.path.isdir(env):
        return env
    # 往上找三层就够了：无论 frbox 落在 emu/frbox 还是直接在工作区根下，
    # 只要某一层同时没有 data/ 和 D300Update/，就继续往上一层。
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        up2 = os.path.dirname(here)
        if up2 == here:
            break
        for cand in (os.path.join(up2, "data"),           # 仓库自带的 data/
                     os.path.join(up2, "D300Update")):    # 解包产物直接放仓库里
            if os.path.isdir(cand):
                return cand
        here = up2
    return os.path.join(up2, "data")


FW_DIR = _fw_dir()

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
    # 额外 ROM：[(基址, 长度, 名字)]，全 0xFF（擦除态）填充 —— 给「镜像没提取到、
    # 但固件会去扫」的窗口留口子（V23：0x740000 起的标定/参数区）。load() 会
    # 清掉所有 ROM 区再装主镜像，所以这里挂的每条都要在 load() 之后重挂。
    extra_rom = []
    # 硬件自由运行计数器基址（0 = 不建）。V23：B 段 0x5C003000 窗口,
    # +0x34 计数器,任务 2 的「等走字」循环靠它出坑。
    hw_counter_base = 0
    # 计数器每个 tick 走几个字（V24：可标定速率）。rate=1 是 V23 的默认口径；
    # 改它只改「等多久」（延时长度），不改程序路径（实测见 dev_hwcnt.py）。
    hw_counter_rate = 1
    # B↔EXPEED 位串行从机（V28）：0 = 不建。建了之后 0x7000000A 的读
    # 会返回器件驱动的 DATA 位，而不是「未映射读回 0」。
    expeed_base = 0
    expeed_regs = None        # {寄存器号: bytes}，None = 全 0
    expeed_read_len = 5       # 应答字节数（无手册依据；取值不影响主机读到什么）
    known_windows = []
    # ---- 中断 ----
    # 48 个 ICR 紧接在直接页 0x440 起，vec 0x10 对应 0x440，一一对应（A/B 两段都实测到）
    icr_base = 0x00000440
    # 回调槽区：只有 A 段是「每个 ISR stub 里 ldi 写死自己的槽」，见 irq_table()
    slot_bank = None
    fallback_vec = None                  # 未使用向量的兜底目标

    def __init__(self, trace=None, serial_console=False, clk_lock_ticks=None,
                 autospin=False, autospin_threshold=3000, xfer=False,
                 dispatch=False):
        self.trace = trace or Trace()
        self.serial_console = serial_console
        self.autospin_enabled = bool(autospin)
        self.autospin_threshold = autospin_threshold
        if clk_lock_ticks is not None:
            self.clk_lock_ticks = clk_lock_ticks
        self.bus = AddressSpace(self.name)
        self.dp = DirectPage(self)
        self.serial = SerialIf(self, tx_sink=self._serial_tx)
        self.timer = ReloadTimer(self)
        self.icu = Icu(self)
        # xfer=False（默认）：0x0AF/0x174/0x524 走影子存储，三处忙等会一直等 ——
        # 这是 V2–V9 所有基线的前提，默认保留。
        # xfer=True：把这三处建模成「置启动位→轮询完成位」的寄存器，
        # 忙等会正常结束（原生 REALOS 跑任务时必需）。见 dev_xfer.py。
        self.xfer = attach_xfer(self) if xfer else None
        # dispatch=False（默认）：写直接页 0x44 只是落到影子内存，调度请求没人受理 ——
        # 这是 V2–V10 的前提，默认保留。
        # dispatch=True：把 0x44 建模成「请求调度」寄存器，写 1 就挂一条 vec 0x3F 的
        # 中断，固件自己的调度器才会被调用。见 dev_dispatch.py。
        self.dispatch = attach_dispatch(self) if dispatch else None
        # prio=False（默认）：0x3F4/0x3FC 走影子存储，调度器读回来的优先级恒为 0。
        # prio=True：建模成硬件优先级编码器（见 dev_prio.py）。
        self.prio = attach_prio(self) if dispatch else None
        self.devices = [self.dp, self.serial, self.timer]
        if self.xfer is not None:
            self.devices.append(self.xfer)
        if self.dispatch is not None:
            self.devices.append(self.dispatch)
        if self.prio is not None:
            self.devices.append(self.prio)
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
        self.fault = None                # (pc, 原因) 取指落到不可执行区
        self.blocked = None              # μITRON 阻塞记录（见 frbox/itron.py）
        self.exited = None               # 任务自己调 sys_ext_tsk 退出的记录
        self.kernel = None               # 挂上的 μITRON 半主机内核
        self.int_hook = None             # 被动观察 int #0x40 的钩子（见 attach_int_hook）
        self.flow = None                 # 服务调用流水（见 attach_flow / frbox.flow）
        self.event = None                # 完成位+中断事件源（见 enable_events / frbox.dev_evt）
        self.cov = None                  # 覆盖率（见 enable_cov / frbox.cover，V15）
        self.msg = None                  # 信箱源（见 enable_msgs / frbox.mailbox，V17）
        self.cb = None                   # 回调槽（见 enable_callbacks / frbox.callback，V18）
        self.isr2b = None                # INT 0x2B 驱动（见 enable_isr2b / frbox.isr2b，V19）
        self.irq2b_clock = None          # INT 0x2B 周期时钟（见 enable_auto_irq2b / frbox.dev_irq2b，V21）
        self.spin_breaker = SpinBreaker(self, threshold=autospin_threshold)
        for d in self.devices:
            d.trace = self.trace
        self._build()
        self.reset()

    # ------------------------------------------------------------------ 构建
    def _map_extra_rom(self):
        for base, size, name in self.extra_rom:
            self.bus.map_rom(base, b"\xFF" * size, name)

    def _build(self):
        self.bus.map_io(0x0000, 0x1000, self.dp, "direct-page")
        self.bus.map_ram(self.ram_base, self.ram_size, "ram")
        self.bus.load_rom(self.rom_base, b"\xFF" * self.rom_size)
        self._map_extra_rom()
        self.hw_counter = None
        if self.hw_counter_base:
            from .dev_hwcnt import FreeRunCounter
            self.hw_counter = FreeRunCounter(self, base=self.hw_counter_base,
                                            rate=self.hw_counter_rate)
            self.bus.map_io(self.hw_counter_base, 0x100, self.hw_counter,
                            self.hw_counter.name)
            self.devices.append(self.hw_counter)
        self.expeed = None
        if self.expeed_base:
            from .dev_expeed import ExpeedLink
            self.expeed = ExpeedLink(self, base=self.expeed_base,
                                     regs=self.expeed_regs,
                                     read_len=self.expeed_read_len)
            self.bus.map_io(self.expeed_base, 0x100, self.expeed,
                            self.expeed.name)
            self.devices.append(self.expeed)
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
        if self.kernel is not None:      # 后挂的生效；load()/reset() 之后要保住
            c.svc_hook = self.kernel
        if self.int_hook is not None:    # 被动观察钩子同样不能在换 CPU 后丢
            c.int_hook = self.int_hook
        if self.cov is not None:         # 覆盖率缓冲区换 CPU 后要重绑
            self.cov.bind(c)
        for d in self.devices:
            d.reset()
        self.icu.reset()
        self.step_count = 0
        self.spin = None
        self.fault = None
        self._spin_base = None
        self._spin_count = 0
        self.spin_breaker.reset()
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
        self._map_extra_rom()          # load_rom 会清掉全部 ROM 区,extra_rom 重挂
        if self.hw_counter is not None:
            # 设备在 __init__ 的 _build() 里就建好了；允许调用方在 create()
            # 之后改 hw_counter_rate 再 load()（V24 速率标定就用这条路）。
            self.hw_counter.set_rate(self.hw_counter_rate)
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

    # ------------------------------------------------------- μITRON 半主机
    def attach_flow(self, force=False):
        """挂服务调用流水记录器（frbox.flow），返回它本身。幂等。"""
        if self.flow is not None and not force:
            return self.flow
        from .flow import Flow
        self.flow = Flow(self)
        return self.flow

    def attach_int_hook(self, fn):
        """挂一个被动观察 `int #0x40` 的钩子（不拦截执行）。

        与 `attach_kernel` / `attach_itron` 的区别是**不改语义**：钩子看一眼就放行，
        `int` 照原样走真向量表。V12 用它记录「谁在第几步、用哪些参数调了哪个服务」，
        从而在不挂内核的情况下读出固件自己的任务在等什么。
        """
        self.int_hook = fn
        if self.cpu is not None:
            self.cpu.int_hook = fn
        return fn

    def attach_kernel(self, nflags=17, auto_event=False):
        """挂上会阻塞的 μITRON 半主机（事件标志 + 睡眠/退出）。

        与 attach_itron 的区别：attach_itron 查询的是固件里的服务表、
        所有服务都「立刻成功」；attach_kernel 会真的把任务停住。
        两者互斥，后挂的生效。
        """
        from .itron import ItronKernel
        self.kernel = ItronKernel(nflags=nflags, trace=self.trace, machine=self,
                                  auto_event=auto_event)
        self.cpu.svc_hook = self.kernel
        self.blocked = None
        return self.kernel

    def flag_set(self, fid, bits):
        """外部事件注入：等价于硬件把事件标志某几位置 1"""
        if self.kernel is None:
            self.attach_kernel()
        v = self.kernel.flag_set(fid, bits)
        return v

    def flag_clear(self, fid, bits=None):
        if self.kernel is None:
            self.attach_kernel()
        return self.kernel.flag_clr(fid, 0xFFFFFFFF if bits is None else bits)

    def resume(self):
        """从被阻塞的那条 int 指令重新执行（标志应已满足）"""
        if self.kernel is None or self.blocked is None:
            return None
        rec = self.kernel.resume(self.cpu)
        self.blocked = None
        return rec

    # ------------------------------------------------------------------ 运行
    def on_irq(self, vec, level):
        if self.cpu is not None:
            self.cpu.request_irq(vec, level)

    def vector(self, n):
        return self.bus.rd(self.tbr + (255 - (n & 0xFF)) * 4, 4)

    # ------------------------------------------------------------ 中断表
    def _stub_slots(self, addr, n=18):
        """扫一个 ISR stub 的前 n 条指令，取出它自己写死的回调槽地址。

        A 段的 39 个真 ISR 是同一个模板（V8 实测）：

            stm0 #0xFF / stm1 #0x0C     ; 存 r0–r7 + r12,r13
            st r4,@-r15 / st r5,@-r15 / st r1,@-r15
            ldi:20 0x0003F0xx, r4       ; ← 本 stub 的回调槽，编译器按 stub 顺序分配
            ld @r4, r0
            cmp r0,#0 ; beq 用默认
            call @r0                    ; 有回调就进应用区
            ldi:20 0x00081336, r12      ; 槽空则调纯 ret
            call @r12
            reti

        所以槽地址不是向量的线性函数，必须从代码里读出来。
        """
        if not self.slot_bank:
            return []
        lo, hi = self.slot_bank
        out = []
        data = self.fetch_bytes(addr, 6 * n)
        off = 0
        for _ in range(n):
            ins = TABLE.decode(data, off)
            if ins is None:
                break
            mnem, _base, vals, size, _w = ins
            if mnem.startswith("ldi"):
                for v in vals.values():
                    if isinstance(v, int) and lo <= v < hi and v % 4 == 0:
                        out.append(v)
            off += size
        return out

    def irq_table(self, vecs=None):
        """解析向量表：vec / ICR 原始值 / 级别 / ISR 目标 / 回调槽 / 当前回调。"""
        vecs = range(0x10, 0x40) if vecs is None else vecs
        rows = []
        for v in vecs:
            isr = self.vector(v)
            icr = self.bus.rd(self.icr_base + (v - 0x10), 1)
            slots = self._stub_slots(isr) if isr > 0x1000 else []
            cbs = [self.bus.rd(s, 4) for s in slots]
            if self.fallback_vec is not None and isr == self.fallback_vec:
                kind = "unused"
            elif slots:
                kind = "stub"
            else:
                kind = "special"
            rows.append({"vec": v, "icr": icr, "level": icr & 0x1F, "isr": isr,
                         "slots": slots, "callbacks": cbs, "kind": kind})
        return rows

    def irq_level(self, vec):
        """读固件自己写进 ICR 的级别（低 5 位）；0 = 还没配置。

        A 段实测：init 阶段把 48 个 ICR 全写了一遍，级别落在 0x12–0x1F，
        同时 stilm 到 0x10。按「数值大 = 优先级高」的模型，这批全部可受理；
        若按相反约定则一条都不该响 —— 固件自己的取值就是这条模型的旁证。
        """
        return self.bus.rd(self.icr_base + ((vec - 0x10) & 0x3F), 1) & 0x1F

    def raise_irq(self, vec, level=None):
        """按 ICR 里的级别请求一条中断；返回实际使用的级别。"""
        lvl = self.irq_level(vec) if level is None else (level & 0x1F)
        if not lvl:
            lvl = 0x1F                   # ICR 还没配置时给最高级，便于实验
        self.icu.raise_vec(vec, lvl)
        return lvl

    def irq_return(self, vec, max_steps=20000, home=None, level=None):
        """注入一条中断并跑到 reti 回原位，返回观测记录。

        判据（与 exp_allisr.py 的 48 向量验证一致）：回到注入时的 PC 且 r15 复原。
        """
        c = self.cpu
        home = c.pc if home is None else home
        sp0 = c.r[15]
        isr = self.vector(vec)
        lvl = self.raise_irq(vec, level)
        pcs = []
        back = None
        for i in range(max_steps):
            if c.halted:
                break
            c.step()
            self.step_count += 1
            pcs.append(c.pc)
            if c.pc == home and i > 0:
                back = i
                break
        return {"vec": vec, "level": lvl, "isr": isr, "home": home,
                "steps": len(pcs), "back": back, "sp_restored": c.r[15] == sp0,
                "pcs": pcs, "halted": c.halted}

    def exec_region(self, pc):
        """PC 落在哪个可执行区；None = 不可取指（ROM 之外、或落进 IO 直接页）。"""
        r = self.bus.find(pc)
        if r is None or r.kind not in ("rom", "ram"):
            return None
        return r

    def run(self, max_steps=100000, stop_pcs=(), spin_guard=True, fault_guard=True):
        c = self.cpu
        stop = set(stop_pcs)
        done = 0
        for _ in range(max_steps):
            if c.halted:
                break
            if self.blocked is not None:
                break
            if stop and c.pc in stop:
                break
            pc0 = c.pc
            if fault_guard and self.exec_region(pc0) is None:
                self.fault = (pc0, "取指落在不可执行区")
                if self.trace is not None:
                    self.trace.log("guest", f"故障：PC=0x{pc0:08X} 不是可执行地址")
                break
            if self.trace.has("in_asm"):
                self._log_asm(pc0)
            if spin_guard:
                if self._spin_base is None or abs(pc0 - self._spin_base) > self.spin_window:
                    self._spin_base, self._spin_count = pc0, 1
                else:
                    self._spin_count += 1
                    limit = (self.spin_breaker.threshold if self.autospin_enabled
                             else self.spin_limit)
                    if self._spin_count >= limit:
                        broke = (self.autospin_enabled
                                 and self.spin_breaker.break_spin(pc0))
                        if not broke:
                            self.spin = (self._spin_base, self._spin_count)
                            if self.trace is not None:
                                self.trace.log("exec", f"检测到自旋：0x{pc0:08X} 附近连续执行 "
                                                       f"{self._spin_count} 步未离开")
                            break
                        self._spin_base, self._spin_count = pc0, 1
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
    def enable_xfer(self):
        """打开 0x0AF/0x174/0x524 的「启动位→完成位」建模（见 dev_xfer.py）。

        默认关闭是为了保住 V2–V9 的基线：那三处忙等在真机上会结束，在我们原来的
        模型里永远不会 —— 「卡住」本身就是那些实验要观察的现象。
        """
        if self.xfer is not None:
            return self.xfer
        self.xfer = attach_xfer(self)
        self.xfer.trace = self.trace
        self.devices.append(self.xfer)
        return self.xfer

    def enable_events(self, **kw):
        """挂「完成位 + 中断」事件源（见 dev_evt.py）。默认关闭以保住旧基线。"""
        if self.event is not None:
            return self.event
        from .dev_evt import attach as attach_evt
        self.event = attach_evt(self, **kw)
        self.event.trace = self.trace
        return self.event

    def enable_bits(self, **kw):
        """挂「事件位图 + 中断」源（见 dev_bits.py，V14）。

        V13 的 `enable_events` 只管一个状态位（0x80/bit3）；这一路管的是软件位图
        `0x41A56` —— 五个回调槽里有三个靠它判定「有没有事件」。位图位要**成组**
        置（bit4+bit8）才推得动 vec 0x19 那条链，参见 probe/exp_evt_matrix.py。
        """
        from .dev_bits import attach as attach_bits
        dev = attach_bits(self, **kw)
        dev.trace = self.trace
        return dev

    def enable_msgs(self, **kw):
        """挂一个信箱源（见 mailbox.py，V17）。

        与 `enable_bits` / `enable_events` 的区别是：那两条都是**伪造状态位**再打中断，
        这一条是**借固件自己的内核**执行一次 `snd_msg` —— 消息挂链、等待方唤醒
        全由固件代码完成，我们只当一个「外部来了一条消息」的设备。

        A 段 1.11 只有一个信箱（id 1 ↔ 控制块 0x466E4），全段唯一的 `rcv_msg`
        在 0x0A6324（任务 5）。投递入口是 `Machine.msg.send()`。
        """
        from .mailbox import attach as attach_msg
        return attach_msg(self, **kw)

    def enable_callbacks(self):
        """挂上两个回调槽（见 callback.py，V18）。

        A 段有一对「登记—倒计时—派发」链：全局单槽存一个代码指针 + 一个倒计时，
        每 tick 计数减 1，减到 0 才 `call [槽]`。两套槽的派发器分别在 0x8223A /
        0x822CE，唯一的引用点都在 `0x80588` 那张位分派表里。

        ⚠️ V19 更正：`0x80588` **不是主循环**，它是 INT 0x2B 的处理函数
        （见 isr2b.py）。它静态上没有 `call` 引用是对的 —— 它不是被调用的，
        是被中断进来的。要让它跑，用 `Machine.enable_isr2b()`，而不是 `cb.tick()`：
        后者是「手动替它转一圈」，前者是「把那根中断线接上」。
        """
        from .callback import attach as attach_cb
        return attach_cb(self)

    def enable_isr2b(self):
        """接上 INT 0x2B（见 isr2b.py，V19）。

        `0x80588` = 向量表第 0xD4 项 → INT 0x2B。它是 A 段的位分派器：逐位测
        `0x3F402`–`0x3F40F` 这 14 个待办标志字节，命中就调对应处理函数。
        接上之后两个回调槽会自己往前走，不用再手动 tick。
        """
        from .isr2b import attach as attach_irq
        return attach_irq(self)

    def enable_auto_irq2b(self, period=1024):
        """给 INT 0x2B 配一个周期时钟（见 dev_irq2b.py，V21）。

        V19/V20 的 `--irq2b N` 是手动一次一次拉线；这一层把线挂到设备时基上，
        `m.run()` / `fast N` 里中断自己来 —— 包括在任务代码里、内核里被拉起来。
        周期是模型参数（真机频率未知，V20 已声明），实验里做频率扫描用。
        同一向量已有待决请求时时钟会合并，不会把 cpu.irq_pending 灌满。
        """
        if self.irq2b_clock is not None:
            return self.irq2b_clock
        if self.isr2b is None:
            self.enable_isr2b()
        from .dev_irq2b import attach as attach_clk
        dev = attach_clk(self, period=period)
        dev.trace = self.trace
        return dev

    def enable_cov(self, label=""):
        """打开覆盖率记录（见 cover.py，V15）。reset() 之后仍然有效。"""
        from .cover import attach as attach_cov
        cov = attach_cov(self, label)
        self.cov = cov
        return cov

    def call_function(self, entry, r4=0, r5=0, r6=0, r7=0, max_steps=100000,
                      ret_pc=None, restore=True):
        """像 `call` 那样把 `entry` 当普通子程序跑一遍，回来时把寄存器复原（V18）。

        跟 `realos.kernel_call` 是同一套思路，区别只有一个：

        * `kernel_call` 走 `eit(0x40)` → 陷阱入口 → 内核分发表（**系统调用**）；
        * `call_function` 直接设 RP 跳进去（**普通子程序**），不经过内核。

        FR 的返回地址放在专用寄存器 RP（dr[1]）里、不压栈（见 fr_emu 的 call 实现），
        所以这里只要把 RP 设成停机桩 `ret_pc`，跑完再复原全部寄存器即可。

        ⚠️ 三条边界：
          ① 这是「替固件调用一个函数」，不是「固件自己跑到这里」——
             调进来的函数看到的是当前任务的栈和寄存器现场，报告里不能写成
             「相机执行了这个回调」；
          ② `entry` 若是 `call r0` 这类间接调用的目标，调用方必须先确认指针非 0，
             否则会跳到 0 地址跑飞；
          ③ 默认 `restore=True` 复原现场，好让探针能连续调多个入口做对照。

        返回 `{"steps":…, "ret":…, "halted":…, "pc":…}`。
        """
        c = self.cpu
        safe = ret_pc if ret_pc is not None else 0x00081338   # `bra -1` 停机桩
        saved = (list(c.r), list(c.dr), c.ps, c.pc, c.delay, c.halted)
        c.r[4], c.r[5], c.r[6], c.r[7] = r4, r5, r6, r7
        c.dr[1] = safe
        c.pc = entry & 0xFFFFFFFF
        c.halted = False
        n = 0
        while n < max_steps and not c.halted:
            c.step()
            n += 1
            self.step_count += 1
            if c.pc == safe and n > 1:
                break
        out = {"steps": n, "ret": c.dr[1], "halted": c.halted,
               "pc": c.pc, "r0": c.r[0]}
        if restore:
            c.r[:], c.dr[:], c.ps, c.pc, c.delay, c.halted = saved
        return out

    def enable_dispatch(self):
        """打开直接页 0x44 的「请求调度」建模（见 dev_dispatch.py）。

        默认关闭同样是保住旧基线：不开它时，内核把任务挂进等待队列之后
        调度器永远不会被叫醒，任务就这么继续跑下去 —— V2–V10 观察到的
        「没有任务切换」正是这个现象。
        """
        if self.dispatch is not None:
            return self.dispatch
        self.dispatch = attach_dispatch(self)
        self.dispatch.trace = self.trace
        self.devices.append(self.dispatch)
        if self.prio is None:
            self.prio = attach_prio(self)
            self.prio.trace = self.trace
            self.devices.append(self.prio)
        return self.dispatch

    def boot_realos(self, task_id=1, max_steps=600000):
        """让固件自己的 REALOS 调度 `task_id` 号任务（V10）。

        与 `boot_task` 的区别是本质性的：`boot_task` 是**绕过调度器**手工造上下文，
        这里是把固件从复位一路跑到它自己的派发点，只在派发那一条指令前改一个寄存器。
        上下文、TCB、系统调用全部由固件完成。

        前提：不要挂 `attach_kernel()` / `attach_itron()` —— 那两条会截住 `int #0x40`，
        真内核就进不去了。
        """
        from .realos import prepare
        self.enable_xfer()
        self.enable_dispatch()
        if self.kernel is not None or self.itron is not None:
            self.trace.log("exec", "boot_realos：已挂 μITRON 钩子，先摘掉再跑真内核")
            self.kernel = None
            self.itron = None
            self.cpu.svc_hook = None
        return prepare(self, task_id, max_steps=max_steps)

    def boot_task(self, entry, sp=None, arg=0, stop_pc=None, max_steps=400000,
                  rp=None, from_table=False):
        """手工启动一个任务：跳过 REALOS 调度器，直接构造初始上下文跳进去。

        背景（V7）：A 段复位链第三跳 0x804FA 是纯 `ret`（1.02/1.03/1.10/1.11
        四版一致），main 的第二条 call 0x80520 也是纯 `ret` —— 内核对象建好、
        状态 0、从未 sta_tsk。所以正常启动一定在 main 返回后 reti 弹 PC=0。

        entry  任务函数入口（A 段任务 1 = 0x0008133A）
        sp     初始栈指针。V9 起推荐 ``sp=None`` + ``from_table=True``：栈顶从
               任务表里取（任务 1 = 0x000462EC）。注意 0x0004616C 不是任务栈顶，
               它是任务表头部那个基准字段。但实测栈顶初值**不影响结局**（任务的
               压栈/弹栈是平衡的，栈顶只决定那 4 个字节的落点），见 KNOWN_ISSUES 29。
        arg    传进 r4 的参数（任务函数第一句通常是 mov r4,r8）
        stop_pc  先跑到这个 PC 再跳（默认 None = 不铺垫；A 段建议 0x00080506）
        rp     初始 RP —— **这是决定任务能否正常返回的那个参数**。任务收尾是
               `ld @r15+,rp; ret`，返回地址就是进函数时压进栈的这个 RP。
               None 则保持当前值，而当前值通常是引导阶段最后一次 call 留下的
               （实测 0x00080500），于是任务「返回」到一段不相干的代码里。
               A 段推荐 0x00081338（固件自带的 `bra -1` 停机占位）。
        """
        c = self.cpu
        if stop_pc is not None:
            self.run(max_steps=max_steps, stop_pcs={stop_pc}, spin_guard=False)
        if from_table:
            for rec in self.task_table():
                if rec["entry"] == entry:
                    sp = rec["sp"]
                    break
        if sp is not None:
            c.r[15] = sp
        if rp is not None:
            c.dr[1] = rp & 0xFFFFFFFF
        c.pc = entry
        c.r[4] = arg
        c.r[5] = 0
        c.halted = False
        self.blocked = None
        self.exited = None
        self.task_boot = (entry, c.r[15], arg)
        if self.trace.has("guest"):
            self.trace.log("guest", f"boot_task entry=0x{entry:08X} "
                                    f"sp=0x{c.r[15]:08X} arg=0x{arg:X} "
                                    f"rp=0x{c.dr[1]:08X}")
        return self.task_boot

    # REALOS 任务表：头部 16 字节 + 8 条 0x1C 字节的记录
    TASK_TABLE = 0x000FE000
    TASK_REC = 0x1C
    TASK_REC0 = 0x000FE010
    TASK_COUNT = 8

    def task_table(self):
        """解析 A 段任务表（0xFE000 头部 + 0xFE010 起 13 条 × 0x1C）

        头部：
          +0x00 任务数（8）—— 这是**用户任务数**，不是记录总数
          +0x04 0x0004616C  ← 不是任何任务的栈顶，见 boot_task 的说明
          +0x08 0xFFFFFFFF
          +0x0C 0x000C001C

        记录（每 0x1C 字节，实际 13 条，见 realos.records()）：
          +0x00 TCB 指针（相邻两条差 0x2C，即 TCB 大小）
          +0x04 任务号 << 16 | 属性（0x01 / 0x02 / 0x03 / 0x04 / 0x06 / 0x09）
          +0x08 保留（全 0）
          +0x0C 状态：0x80000000 = 已激活，0 = 未启动
          +0x10 入口地址
          +0x14 栈顶
          +0x18 保留（全 0）

        ⚠️ V9 及之前这里只解 8 条（信了表头那个数），于是任务 9–12 和空闲任务 0
        全被漏掉 —— "tsk_id 9/10 在任务表里找不到" 那个悬案就是这么来的。V10 起
        改由 `realos.records()` 统一解析，`index` 仍从 1 开始以保住既有输出格式。
        """
        from .realos import records
        if not hasattr(self, "bus"):
            return []
        out = []
        for r in records(self):
            d = dict(r)
            d["index"] = r["index"] + 1
            out.append(d)
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
        if self.fault:
            pc, why = self.fault
            lines.append(f"故障      PC=0x{pc:08X}  {why}")
        if self.spin:
            base, cnt = self.spin
            lines.append(f"自旋      PC 在 0x{base:08X}±0x{self.spin_window:X} 内连续 {cnt:,} 步未离开")
        if self.spin_breaker.hits:
            lines.append(f"自旋消除  {self.spin_breaker.summary()}")
        if self.blocked is not None:
            from .itron import ItronKernel
            tag = "已退出" if self.exited is not None else "阻塞  "
            lines.append(f"{tag}    " + ItronKernel.describe(self.blocked)
                         + f"   @ PC=0x{self.blocked['pc']:08X}")
            if self.exited is None:
                lines.append("          用 flag set <id>=<bits> 注入外部事件，再 resume 继续")
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
    slot_bank = (0x0003F018, 0x0003F0E8)   # A 段回调槽区，实测 0x3F018–0x3F0E4
    fallback_vec = 0x000FE250              # 8 个未用向量 → 兜底桩 → 0x81338 bra -1


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
    extra_ram = [(0x9F580000, 0x00100000, "ram-sdram"),
                 # 0x68000xxx 窗口（V22 实测定性）：REALOS 的内核对象区 ——
                 # TCB 链表（0x6800013C 起步长 0x2C）+ 当前任务用户栈
                 # （空闲任务栈 0x68000B98）。派发序列读 [TCB+0x18]（下一任务
                 # 保存的用户栈指针）；未映射时读回 0 → USP=0 → 派发链塌掉。
                 # 按 64KB 建。V25 扩到 128KB：任务 4（入口 0x126454）在
                 # 0x1A4F9A 把 ROM 0x25F534 起的一段表拷到 0x68010000，
                 # 实测写到 0x6801DE84（14242 次 4 字节写）—— 64KB 时全被吞。
                 (0x68000000, 0x00020000, "ram-kernel"),
                 # 任务用户栈区（V22 实测定性）：任务表 28 条的栈顶全在
                 # 0x9F8002A4–0x9F8222A4。之前只给了 0x9F58xxxx 那 1MB，
                 # 任务栈的写入全被吞、读回全 0 —— sys_sta_tsk「成功」但帧
                 # 落不了地、派发读回 0 就跳 0。按 256KB 建，覆盖最高栈顶
                 # 0x9F8222A4 之下的全部区域（栈向下生长，不会越出上界）。
                 (0x9F800000, 0x00040000, "ram-taskstack")]
    # ROM 之外的擦除态 flash（V23 实测定性）：任务 2 的开机装配会扫
    # 0x740000（指针表 0x1C2A98[0]）和 0x750000（[1]）两块半字表 ——
    # 0xAA55=记录头、0xFFFF=终止符。我们的 7MB 镜像正好到 0x740000 截止
    # （尾部 FFFF 填充 + CRC），这两块是镜像没提取到的标定/参数区。按
    # 擦除态（全 0xFF）建模：扫描立刻命中终止符干净退出 —— 「无记录」
    # 也是合法的机器状态。
    extra_rom = [(0x740000, 0x00100000, "flash-erased")]
    # 0x5C003000 硬件定时器（V23）：+0x30 周期 0x813、+0x38 控制域
    # （0x120C8E 初始化写入）、+0x34 自由运行计数器（0x125A0A 的
    # 「读两次等走字」循环在等它）。
    hw_counter_base = 0x5C003000
    # B↔EXPEED 位串行（V28 建模）：0x70000002 = CS(bit2)、0x7000000A = CLK(bit1)
    # /DATA(bit2)。整场固件只碰这两个偏移（实测写入 8 + 242 次）。
    # 之前未映射 → 读回恒 0 → 主机采样到的数据位永远是 0。
    expeed_base = 0x70000000
    # 0x68000xxx 窗口（V22 实测定性）：boot 期间固件往 0x68000000–0x6800B984
    # 写了 3746 次；REALOS 把 TCB 链表和任务用户栈都放在这里 ——
    #   * TCB@0x6800060C：+0x00 自指链、+0x10 优先级、+0x18 保存的用户栈指针
    #     （0x4152A 写入 0x68000B98）、+0x24 任务表回指 0x54F10
    #   * 派发序列与 A 段 0xFE4E6 逐字相同：r14=[0x54F10]（下一任务 TCB）→
    #     [0x68000000]=r14 → r0=[TCB+0x18] → mov r0,USP → orccr #0x20 切用户栈 →
    #     r0=[USP]（任务入口）→ 压系统栈 → ldm 恢复 → retI 进任务。
    # 之前未映射时 [TCB+0x18] 读回 0 → USP=0 → 整条链塌了。
    # 按 64KB 建（覆盖全部观测写入 0x68000000–0x6800B984）。
    known_windows = [
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
