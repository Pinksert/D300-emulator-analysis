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
from .icu import Icu
from .trace import Trace

# 固件目录：环境变量 → 仓库 data/ → 同级 D300Update/
_PKG = os.path.dirname(os.path.abspath(__file__))     # emu/frbox
_EMU = os.path.dirname(_PKG)                          # emu
_ROOT = os.path.dirname(_EMU)                         # 仓库根
FW_DIR = (os.environ.get("FRBOX_FW_DIR")
          or (os.path.join(_ROOT, "data") if os.path.isdir(os.path.join(_ROOT, "data"))
              else os.path.join(_ROOT, "D300Update")))

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
    # ---- 中断 ----
    # 48 个 ICR 紧接在直接页 0x440 起，vec 0x10 对应 0x440，一一对应（A/B 两段都实测到）
    icr_base = 0x00000440
    # 回调槽区：只有 A 段是「每个 ISR stub 里 ldi 写死自己的槽」，见 irq_table()
    slot_bank = None
    fallback_vec = None                  # 未使用向量的兜底目标

    def __init__(self, trace=None, serial_console=False, clk_lock_ticks=None,
                 autospin=False, autospin_threshold=3000):
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
        self.fault = None                # (pc, 原因) 取指落到不可执行区
        self.blocked = None              # μITRON 阻塞记录（见 frbox/itron.py）
        self.exited = None               # 任务自己调 sys_ext_tsk 退出的记录
        self.kernel = None               # 挂上的 μITRON 半主机内核
        self.spin_breaker = SpinBreaker(self, threshold=autospin_threshold)
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
        if self.kernel is not None:      # 后挂的生效；load()/reset() 之后要保住
            c.svc_hook = self.kernel
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
        """解析 A 段任务表（0xFE000 头部 + 0xFE010 起 8 条 × 0x1C）

        头部：
          +0x00 任务数（8）
          +0x04 0x0004616C  ← 不是任何任务的栈顶，见 boot_task 的说明
          +0x08 0xFFFFFFFF
          +0x0C 0x000C001C

        记录（每 0x1C 字节）：
          +0x00 TCB 指针（相邻两条差 0x2C，即 TCB 大小）
          +0x04 任务号 << 16 | 属性（0x0004 / 0x0002）
          +0x08 保留（全 0）
          +0x0C 状态：0x80000000 = 已激活，0 = 未启动
          +0x10 入口地址
          +0x14 栈顶
          +0x18 保留（全 0）
        """
        out = []
        if not hasattr(self, "bus"):
            return out
        n = self.bus.rd(self.TASK_TABLE, 4)
        if not (0 < n <= 64):
            n = self.TASK_COUNT
        for i in range(n):
            base = self.TASK_REC0 + i * self.TASK_REC
            tcb = self.bus.rd(base, 4)
            idf = self.bus.rd(base + 0x04, 4)
            state = self.bus.rd(base + 0x0C, 4)
            entry = self.bus.rd(base + 0x10, 4)
            sp = self.bus.rd(base + 0x14, 4)
            out.append({
                "index": i + 1,
                "addr": base,
                "tcb": tcb,
                "id": (idf >> 16) & 0xFFFF,
                "attr": idf & 0xFFFF,
                "state": state,
                "active": bool(state & 0x80000000),
                "entry": entry,
                "sp": sp,
            })
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
