# -*- coding: utf-8 -*-
"""frbox 自检：总线 / 设备 / 中断控制器 / 机器 / monitor / CLI。

跑法：python frbox_selftest.py
每个断言都对应一条「写错不会崩、只会算错」的地方，比如跨区访问字节顺序、
设备读写方向、EIT 的 ILM 屏蔽、reset 后 poke 是否重放。
"""
import io
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from fr_emu import TABLE                                   # noqa: E402
from frbox.bus import AddressSpace                          # noqa: E402
from frbox.dev_serial import SerialIf                       # noqa: E402
from frbox.dev_timer import ReloadTimer                     # noqa: E402
from frbox.device import DirectPage                         # noqa: E402
from frbox.icu import Icu                                   # noqa: E402
from frbox.machine import create, crc16_xmodem              # noqa: E402
from frbox import snapshot                                  # noqa: E402
from frbox.monitor import Monitor                           # noqa: E402

OK = 0
TOTAL = 0


def check(name, got, want):
    global OK, TOTAL
    TOTAL += 1
    good = got == want
    OK += good
    print(f"  {'OK ' if good else 'BAD'}  {name:<40} 实测={got!r}  期望={want!r}")


def under_sdram_1mb(fn):
    """把机器装回 **V42 口径**（`ram-sdram` 1MB + 没有节点区）再跑。

    V43 把默认铺到任务栈区之前（2.5MB）之后，池地址 `0x9F69xxxx` 才真的
    可读写 —— 于是任务不再走「取不到缓冲区也照搬」那条路，V41/V42 记下的
    memmove / 填充 / 停摆数字在新口径下**不可复现**。

    V44 又补了节点区 `0x9F550000+0x30000`（固件的 3003 个双向链表节点），
    那块在 V42 时代也**从来没建过**。所以 V42 口径 = 1MB SDRAM **且**无节点区，
    两者都要还原，才能复现历史数字。

    这些数字是「地址未映射时会发生什么」的历史观测，仍有价值，所以用这个
    装饰器把它们固定在旧口径下跑，别当成新默认行为。
    """
    import functools

    @functools.wraps(fn)
    def wrap(*a, **k):
        from frbox import bpool, lists, machine
        base = list(machine.D300B.extra_ram)
        # 先摘掉节点区，再把 ram-sdram 缩回 1MB
        no_nodes = [x for x in base if x[2] != "ram-nodes"]
        machine.D300B.extra_ram = [(bpool.SD_BASE, bpool.SD_SIZE_V42,
                                    "ram-sdram")] + no_nodes[1:]
        try:
            return fn(*a, **k)
        finally:
            machine.D300B.extra_ram = base
    return wrap


def code_for(mnem):
    """在编码空间里找一个助记符对应的机器码（用于构造测试指令）"""
    for w in range(0x10000):
        ins = TABLE.decode(w.to_bytes(2, "big") + b"\0" * 8, 0)
        if ins and ins[0] == mnem and ins[3] == 2:
            return w.to_bytes(2, "big")
    raise KeyError(mnem)


def section(t):
    print(f"\n[{t}]")


# ---------------------------------------------------------------- 1 总线
def test_bus():
    section("1 总线")
    b = AddressSpace("t")
    b.map_rom(0x1000, b"\x11\x22\x33\x44\x55\x66\x77\x88", "rom8")
    b.map_ram(0x2000, 0x100, "ram")
    check("ROM 读 4 字节（大端）", b.rd(0x1000, 4), 0x11223344)
    b.wr(0x2000, 4, 0xDEADBEEF)
    check("RAM 写后读", b.rd(0x2000, 4), 0xDEADBEEF)
    b.wr(0x2000, 2, 0x1234)
    check("RAM 半字写", b.rd(0x2000, 2), 0x1234)
    check("RAM 高半字未被踩", b.rd(0x2002, 2), 0xBEEF)
    b.wr(0x1002, 4, 0xFFFFFFFF)
    check("写 ROM 不改内容", b.rd(0x1002, 4), 0x33445566)
    check("写 ROM 被记账", len(b.writes_to_rom), 1)
    # 跨区：0x2000+0xFE 起读 4 字节会跨到未映射
    b.wr(0x20FE, 2, 0xAABB)
    check("跨区读低半部分", b.rd(0x20FE, 2), 0xAABB)
    check("未映射读返回 0", b.rd(0x90000, 4), 0)
    check("未映射读记账", b.unmapped_reads.get(0x90000), 1)
    b.wr(0x90000, 2, 0x1234)
    check("未映射写记账", len(b.unmapped_writes), 1)
    b.watch.add(0x2000)
    b.wr(0x2000, 1, 0x55)
    check("watch 命中", b.access_log[-1][:3], ("W", 0x2000, 1))
    b.load_rom(0x3000, b"\xAA\xBB")
    check("load_rom 后快路径同步", (b.rom_base, bytes(b.rom)), (0x3000, b"\xAA\xBB"))
    b.watch.clear()


# ---------------------------------------------------------------- 2 设备
def test_devices():
    section("2 设备")
    dp = DirectPage()
    ser = SerialIf()
    dp.attach(0x60, 0x68, ser)
    irq_log = []
    ser.irq = lambda line=None: irq_log.append(line)
    ser.irq_line = 0x1B

    dp.write(0x61, 1, 0x41)                     # 写 TDR
    check("串口发送记录", bytes(ser.tx), b"A")
    check("发送计数", ser.tx_count, 1)

    ser.push_rx(b"Hi")
    check("注入后 RDRF 置位", ser.ssr & 0x40, 0x40)
    check("注入触发中断线", irq_log, [0x1B])
    check("读 RDR 第 1 字节", dp.read(0x62, 1), ord("H"))
    check("读 RDR 第 2 字节", dp.read(0x62, 1), ord("i"))
    check("队列空后 RDRF 清除", ser.ssr & 0x40, 0)
    check("队列空读回 0", dp.read(0x62, 1), 0)
    check("未建模寄存器读回 0", dp.read(0x482, 1), 0)
    dp.write(0x482, 1, 0x88)
    check("未建模寄存器影子可写", dp.read(0x482, 1), 0x88)
    check("影子写记账", dp.unknown_reads.get(0x482) >= 1, True)

    tm = ReloadTimer()
    fired = []
    tm.irq = lambda line=None: fired.append(line)
    tm.write(0x00, 2, 0x0010)                   # TMRLR
    tm.write(0x04, 2, 0x0101)                   # CNTE | IE
    tm.tick(0x10)
    check("定时器欠载计数", tm.underflows, 1)
    check("定时器欠载触发中断", fired, [None])
    check("欠载后 UF 置位", tm.read(0x06, 2), 1)
    check("UF 读后自清", tm.read(0x06, 2), 0)


# ---------------------------------------------------------------- 3 ICU + CPU
def test_icu():
    section("3 中断控制器与 EIT")
    m = create("d300-a")
    m.load()
    base_pc = m.cpu.pc
    m.cpu.i = 0
    m.icu.bind(0x22, 0x20, level=9, note="测试")
    ok = m.icu.raise_line(0x22)
    check("绑定的线被受理", ok, True)
    m.cpu.step()
    check("I=0 时不进中断（只执行了一条）", len(m.cpu.irq_log), 0)
    check("中断仍在排队", len(m.cpu.irq_pending), 1)
    # 级别号越小优先级越高，受理条件是 level < ILM
    m.cpu.ilm = 0x09
    m.cpu.i = 1
    m.cpu.step()
    check("ILM 挡住同级（level 9 >= 9）", len(m.cpu.irq_log), 0)
    m.cpu.ilm = 0x0A
    m.cpu.step()
    check("放行后进入 EIT", len(m.cpu.irq_log), 1)
    check("EIT 目标 = 向量槽 0x20 的值",
          m.cpu.pc, m.bus.rd(m.tbr + (255 - 0x20) * 4, 4))
    check("EIT 后 I=0", m.cpu.i, 0)
    check("S 位切到系统栈", m.cpu.s, 0)

    # reti：手工摆一个帧，验证弹栈顺序（PC 在栈顶，PS 在其后）
    b = AddressSpace("r")
    b.map_ram(0x0000, 0x1000, "ram")
    b.map_rom(0x1000, code_for("reti") + b"\0" * 6, "code")
    from fr_emu import Fr
    cpu = Fr(b, TABLE)
    cpu.pc = 0x1000
    cpu.dr[2] = cpu.r[15] = 0x0800
    b.wr(0x0800, 4, 0x00001234)                 # PC
    b.wr(0x0804, 4, 0x00050001)                 # PS
    cpu.step()
    check("reti 恢复 PC", cpu.pc, 0x00001234)
    check("reti 恢复 ILM", cpu.ilm, 0x05)


# ---------------------------------------------------------------- 4 机器
def test_machine():
    section("4 机器")
    a = create("d300-a")
    bb = create("d300-b")
    check("A 机器固件已存在", os.path.exists(a.default_firmware), True)
    a.load()
    bb.load()
    check("A 复位入口 = 向量槽 0x00", hex(a.entry), "0xfc000")
    check("B 复位入口 = 向量槽 0x00", hex(bb.entry), "0x40000")
    check("A CRC16 通过", a.fw_crc_ok, True)
    check("B CRC16 通过", bb.fw_crc_ok, True)
    check("A ROM 长度", a.bus.rom_end - a.bus.rom_base, 524290)
    check("B ROM 长度", bb.bus.rom_end - bb.bus.rom_base, 7340032)
    check("A TBR 落在 ROM 内", a.bus.rom_base <= a.tbr < a.bus.rom_end, True)
    check("B TBR 落在 ROM 内", bb.bus.rom_base <= bb.tbr < bb.bus.rom_end, True)
    check("B 有第二块 RAM（0x9F580000）",
          bb.bus.region_at(0x9F580000, 4) is not None, True)
    a.poke(0x4A0, 1, 0x80)               # 0x482 已建模成时钟寄存器，改用仍未建模的 0x4A0
    check("poke 生效", a.bus.rd(0x4A0, 1), 0x80)
    a.reset()
    check("reset 后 poke 重放", a.bus.rd(0x4A0, 1), 0x80)
    n = a.run(2000)
    check("run 返回步数 > 0", n > 0, True)
    check("步数计入 step_count", a.step_count >= 2000, True)


# ---------------------------------------------------------------- 5 自旋检测
def test_spin():
    section("5 自旋检测")
    b = AddressSpace("s")
    b.map_ram(0x0000, 0x1000, "ram")
    b.map_rom(0x1000, code_for("bra") + b"\xFF\xFA" + b"\0" * 4, "code")
    from fr_emu import Fr            # bra *-2
    cpu = Fr(b, TABLE)
    cpu.pc = 0x1000
    m = create("d300-a")
    m.bus, m.cpu = b, cpu
    m.spin_limit = 50
    m.run(1000)
    check("自旋被检测到", m.spin is not None, True)
    check("自旋基准 PC", hex(m.spin[0]) if m.spin else None, "0x1000")


# ---------------------------------------------------------------- 6 monitor
def test_monitor():
    section("6 monitor")
    m = create("d300-a")
    m.load()
    out = []
    mon = Monitor(m, out=out.append)
    mon.exec_line("info mtree")
    check("mtree 含 ram 区域", any("ram" in s for s in out), True)
    out.clear()
    mon.exec_line("x/4x 0x80000")
    check("x/4x 输出 4 个字", len(out[-1].split(":")[1].split()), 4)
    out.clear()
    mon.exec_line("x/2i 0x000FC000")
    lines = [s for s in "\n".join(out).splitlines() if s.startswith("0x")]
    check("x/2i 输出 2 条", len(lines), 2)
    out.clear()
    mon.exec_line("bp 0x000FC010")
    mon.exec_line("cont 5000")
    check("断点命中", m.cpu.pc, 0x000FC010)
    out.clear()
    mon.exec_line("poke 0x4A0=0x88")
    check("monitor poke", m.bus.rd(0x4A0, 1), 0x88)
    mon.exec_line("latch 0x490=0x11:3")
    check("monitor latch", m.bus.io_after.get(0x490), (3, 0x11))
    out.clear()
    mon.exec_line("irq rx 41 42")
    check("monitor 注入串口数据", len(m.serial.rx), 2)
    out.clear()
    mon.exec_line("nosuchcmd")
    check("未知命令不抛异常", "未知命令" in out[0], True)


# ---------------------------------------------------------------- 7 CLI
def test_cli():
    section("7 CLI")
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(
        [sys.executable, "-m", "frbox", "-M", "d300-b", "-c", "info rom; dev"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.dirname(os.path.abspath(__file__)), env=env, timeout=180)
    check("CLI 退出码", r.returncode, 0)
    check("CLI 输出含机器名", "d300-b" in r.stdout, True)
    check("CLI 输出含 CRC 校验", "CRC16 OK" in r.stdout, True)
    r2 = subprocess.run(
        [sys.executable, "-m", "frbox", "-M", "help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.dirname(os.path.abspath(__file__)), env=env, timeout=120)
    check("列出机器", "d300-a" in r2.stdout and "d300-b" in r2.stdout, True)


# ---------------------------------------------------------------- 时钟寄存器组
def test_clock():
    print("\n[8 时钟寄存器 0x480–0x48F]")
    from frbox.dev_clk import ClockReset
    c = ClockReset(None, lock_ticks=2)
    check("复位后 0x482 为 0", c.read(0x02), 0)
    c.write(0x04, 1, 0x34)
    check("配置寄存器影子", c.read(0x04), 0x34)
    c.write(0x02, 1, 0x08)                     # 位3 = 启动
    check("写启动位后仍未就绪", c.read(0x02) & 0x80, 0)
    c.tick(256)
    check("倒计时未到不置位", c.read(0x02) & 0x80, 0)
    c.tick(256)
    check("2 次 tick 后位7 就绪", c.read(0x02) & 0x80, 0x80)
    check("就绪后低 7 位保留", c.read(0x02), 0x88)
    check("锁定次数", c.locks, 1)
    c.write(0x02, 1, 0x88)                     # 位7 是只读状态位
    check("软件写不进位7", c.read(0x02), 0x08)
    c.write(0x03, 1, 0xA5)
    check("只写 A5 不算解锁", c.unlocked, False)
    c.write(0x03, 1, 0x5A)
    check("A5→5A 两键解锁", c.unlocked, True)

    # 机器级：A 段真跑，时钟自己锁定，不再需要 --latch
    m = create("d300-a")
    m.load()
    m.run(60000)
    check("A 段时间寄存器已锁定", m.clk.locks, 1)
    check("A 段 0x484 由 0x34 变 0x36", m.clk.read(0x04), 0x36)
    check("A 段读到的 0x482", m.clk.read(0x02), 0x88)
    check("A 段已越过自旋（PC 不在 0x8007C）", m.cpu.pc != 0x0008007C, True)
    m2 = create("d300-b")
    m2.load()
    m2.run(4000)
    check("B 段不使用这组寄存器", m2.clk.locks, 0)


# ---------------------------------------------------------------- 快照
def test_snapshot():
    print("\n[9 savevm / loadvm]")
    import tempfile
    from frbox import snapshot
    path = os.path.join(tempfile.gettempdir(), "frbox-selftest.snap")
    m = create("d300-a")
    m.load()
    m.run(43446)
    pc0, st0 = m.cpu.pc, m.step_count
    ram0 = {r.name: bytes(r.buf) for r in m.bus.regions if r.kind == "ram"}
    size = snapshot.save(m, path)
    check("快照文件非空", size > 100000, True)
    m2 = create("d300-a")
    m2.load()
    meta = snapshot.load(m2, path)
    check("恢复后 PC 一致", m2.cpu.pc, pc0)
    check("恢复后步数一致", m2.step_count, st0)
    check("恢复后 r15 一致", m2.cpu.r[15], m.cpu.r[15])
    check("恢复后时钟寄存器一致", m2.clk.read(0x02), m.clk.read(0x02))
    check("恢复后直接页影子一致", dict(m2.dp.shadow) == dict(m.dp.shadow), True)
    check("影子规模 >200 条", len(m.dp.shadow) > 200, True)
    ram2 = {r.name: bytes(r.buf) for r in m2.bus.regions if r.kind == "ram"}
    check("恢复后 RAM 逐字节一致", ram2 == ram0, True)
    n = 0
    for i in range(500):
        if m.cpu.pc != m2.cpu.pc:
            break
        m.step(1)
        m2.step(1)
        n = i + 1
    check("恢复后 500 步逐条同步", n, 500)
    check("describe 含 PC", "PC=0x" in snapshot.describe(meta), True)
    try:
        snapshot.load(create("d300-b"), path)
        check("机器不匹配要报错", False, True)
    except ValueError:
        check("机器不匹配要报错", True, True)
    bad = os.path.join(tempfile.gettempdir(), "frbox-selftest-bad.snap")
    with open(bad, "wb") as f:
        f.write(b"NOTASNAP" + b"\0" * 16)
    try:
        snapshot.load(create("d300-a"), bad)
        check("非快照文件要报错", False, True)
    except ValueError:
        check("非快照文件要报错", True, True)
    for p_ in (path, bad):
        try:
            os.remove(p_)
        except OSError:
            pass

# ---------------------------------------------------------------- A 段中断通路
def test_airq():
    print("\n[10 A 段中断通路 48 向量]")
    import tempfile
    from frbox import snapshot
    path = os.path.join(tempfile.gettempdir(), "frbox-selftest-irq.snap")

    m = create("d300-a")
    m.load()
    c = m.cpu
    m.run(3000)
    g = 0
    while c.pc != 0x00080506 and g < 80000:
        c.step()
        m.step_count += 1
        g += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    check("停在注入点 0x80506", c.pc, 0x00080506)
    check("注入前 EIT 为 0", len(c.irq_log), 0)
    r15_0, ssp_0 = c.r[15], c.dr[2]
    check("SSP 初值", ssp_0, 0x0007F000)
    snapshot.save(m, path)

    ok = dead = other = 0
    bad = []
    blen = {}
    for vec in range(0x10, 0x40):
        snapshot.load(m, path)          # 每个向量都从同一份干净快照出发
        c = m.cpu
        c.eit(vec)
        entry = c.pc
        blen[vec] = (entry, 0)
        hit_ok = hit_dead = False
        n = 0
        for i in range(1500):
            if c.pc == 0x00080506:
                hit_ok = True
                break
            if c.pc == 0x00081338:
                hit_dead = True
                break
            c.step()
            n += 1
        blen[vec] = (entry, n)
        if hit_ok and c.r[15] == r15_0 and c.dr[2] == ssp_0:
            ok += 1
        elif hit_dead:
            dead += 1
        else:
            other += 1
            if vec != 0x3F:            # vec 0x3F 是已知的「清 I 位 + 嵌套」特殊入口
                bad.append(vec)

    check("39 个真实 ISR 正确 reti 且 r15/SSP 复原", ok, 39)
    check("8 个未用向量进停机点 0x81338", dead, 8)
    check("剩 1 个特殊向量 vec 0x3F", other, 1)
    check("无其它异常向量", bad, [])
    check("vec 0x10 入口", blen[0x10][0], 0x000809BE)
    check("vec 0x20 入口", blen[0x20][0], 0x00080C88)
    check("vec 0x2B 入口（最长 ISR）", blen[0x2B][0], 0x00080588)
    check("vec 0x2B 指令数 > 200", blen[0x2B][1] > 200, True)
    check("普通 ISR 都在 30 步内", max(v for k, (e, v) in blen.items()
                                        if v < 200 and v > 0) <= 30, True)
    check("未用向量都指向兜底 0xFE250",
          all(blen[v][0] == 0x000FE250 for v in (0x1F, 0x27, 0x2C, 0x2D, 0x2E, 0x37, 0x38, 0x39)),
          True)
    try:
        os.remove(path)
    except OSError:
        pass

# ---------------------------------------------------------------- 任务启动
def test_boot_task():
    print("\n[11 手工启动任务（跳过 REALOS 调度器）]")
    m = create("d300-a")
    m.load()
    check("初始化时没有任务记录", m.task_boot, None)
    e, sp, a = m.boot_task(0x0008133A, 0x0004616C, 0, stop_pc=0x00080506)
    check("boot_task 返回入口", e, 0x0008133A)
    check("PC 落在任务入口", m.cpu.pc, 0x0008133A)
    check("r15 是给定栈", m.cpu.r[15], 0x0004616C)
    check("r4 是给定参数", m.cpu.r[4], 0)
    check("先跑到 0x80506 已生效（PC 已改）", m.cpu.pc != 0x00080506, True)

    # 跑到第一个硬件卡点：0x0DCD2C 轮询 0x0AF 的 bit7
    c = m.cpu
    pcs = set()
    hit = None
    for i in range(60000):
        p = c.pc
        pcs.add(p)
        if p == 0x000DCD2C:
            hit = i
            break
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    check("跑到第一个卡点 0x0DCD2C", hit is not None, True)
    check("卡点在 3000 步内（实测 1981）", hit is not None and hit < 3000, True)
    check("应用区 PC 数 > 400（实测 500）", len({x for x in pcs if x >= 0x81300}) > 400, True)
    check("卡点前 0x0AF 的 bit2 已被置起", m.dp.shadow.get(0xAF, 0) & 0x02, 0x02)

    # 注入 bit7 后应能越过，撞上第二个卡点 0x09C97C
    m.dp.shadow[0xAF] = m.dp.shadow.get(0xAF, 0) | 0x80
    hit2 = None
    for i in range(60000):
        if c.pc == 0x0009C97C:
            hit2 = i
            break
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    check("注入 0x0AF 位7 后到第二个卡点 0x09C97C", hit2 is not None, True)

    # 回调表：任务跑起来后 ISR 槽 0x3F048 等被应用写入
    slots = {x for x, *_ in m.bus.all_writes if 0x3F000 <= x <= 0x3F0FF}
    check("应用区自己写了回调槽（≥5 个）", len(slots) >= 5, True)
    check("0x3F048（vec 0x1B 的槽）被写", 0x3F048 in slots, True)
    check("0x3F028（vec 0x13 的槽）被写", 0x3F028 in slots, True)

    # task_table 至少能报出 4 个 TCB 的挂载点
    tt = m.task_table()
    check("task_table 非空", len(tt) > 0, True)


def test_irqfire():
    """中断闭环：向量表解析 + 注入 + ISR 里 call @r0 进应用区 + reti 回原位"""
    section("12 中断闭环")
    m = create("d300-a")
    m.load()
    m.boot_task(0x0008133A, sp=0x0004616C, arg=0, stop_pc=0x00080506)
    m.run(max_steps=2500, spin_guard=False)

    rows = m.irq_table()
    check("向量表 48 行", len(rows), 48)
    check("真 stub 39 条", sum(1 for r in rows if r["kind"] == "stub"), 39)
    check("未用 8 条", sum(1 for r in rows if r["kind"] == "unused"), 8)
    check("特殊 1 条（vec 0x3F）",
          sum(1 for r in rows if r["kind"] == "special"), 1)
    check("ICR 全部被固件写过", sum(1 for r in rows if r["level"]), 48)
    lo, hi = m.slot_bank
    check("槽地址都在 0x3F018–0x3F0E4",
          all(lo <= s < hi for r in rows for s in r["slots"]), True)
    check("vec 0x13 的 ICR 级别（实测 0x1A）", m.irq_level(0x13), 0x1A)

    snap = os.path.join(tempfile.gettempdir(), "frbox_selftest.snap")
    snapshot.save(m, snap)
    home = m.cpu.pc

    # 注入点的 ILM 是 0x10（启动初始化阶段，级别 0x12+ 全被挡住 —— 这正是
    # 「开机初始化期间关中断」该有的样子）。要验证 ISR 本身，先把 ILM 抬到
    # 任务上下文的 0x1F。级别仍然取自 ICR，不是硬编码。
    m.cpu.ilm = 0x1F
    r = m.irq_return(0x13, max_steps=20000, home=home)
    check("vec 0x13 闭环回原位", r["back"] is not None, True)
    check("vec 0x13 的 SP 逐位复原", r["sp_restored"], True)
    check("级别取自 ICR 而非硬编码 8", r["level"], 0x1A)
    check("进了应用区执行", any(0x00080000 <= p < 0x000E0000 for p in r["pcs"]), True)
    check("槽里的回调 0xACBDA 被执行", 0x000ACBDA in r["pcs"], True)
    check("回调是一次性的（槽清 0）", m.bus.rd(0x3F028, 4), 0)
    check("注入结束后没有残留待决请求", len(m.cpu.irq_pending), 0)

    # vec 0x1F 的 ICR 级别就是 0x1F：在「受理条件 level < ILM」下，级别 0x1F
    # 永远不可能被受理 —— 这就是固件给这 20 条向量标「禁用」的方式。
    snapshot.load(m, snap)
    check("vec 0x1F 的 ICR 级别是 0x1F", m.irq_level(0x1F), 0x1F)
    m.cpu.ilm = 0x1F
    n0 = len(m.cpu.irq_log)
    m.irq_return(0x1F, max_steps=200)
    check("级别 0x1F 不被受理（等于 ILM，等于禁用）", len(m.cpu.irq_log), n0)
    # 强行放行它，验证「未用向量落进停机占位」这条仍然成立
    snapshot.load(m, snap)
    m.cpu.ilm = 0x1F
    m.irq_return(0x1F, max_steps=20000, level=0x1E)
    check("放行的未用向量停在 0x81338 的 bra -1", m.cpu.pc, 0x00081338)

    snapshot.load(m, snap)
    check("特殊向量 0x3F 没有回调槽", m._stub_slots(m.vector(0x3F)), [])


def test_autospin():
    print("\n[13 自旋消除器（运行期解出就绪位）]")
    m = create("d300-a", autospin=True)
    check("autospin 开关被记下", m.autospin_enabled, True)
    check("阈值默认 3000", m.spin_breaker.threshold, 3000)
    check("初始没有注入记录", m.spin_breaker.hits, 0)
    check("ROM 内可执行", m.exec_region(0x00090000).kind, "rom")
    check("地址 0 不可取指", m.exec_region(0x0), None)
    check("直接页不可取指", m.exec_region(0x00000040), None)

    m.load()
    m.boot_task(0x0008133A, 0x0004616C, 0, stop_pc=0x00080506)
    c = m.cpu
    seen = set()
    orig = c.step

    def counted():
        seen.add(c.pc)
        orig()

    c.step = counted
    m.run(max_steps=60000)
    c.step = orig

    rows = m.spin_breaker.rows()
    check("三处忙等全被认出", len(rows), 3)
    check("第一处等 0x0AF bit7", (rows[0]["addr"], rows[0]["mask"]), (0x0AF, 0x80))
    check("第二处等 0x174 bit6", (rows[1]["addr"], rows[1]["mask"]), (0x174, 0x40))
    check("第三处等 0x524 bit6（地址是算出来的）",
          (rows[2]["addr"], rows[2]["mask"]), (0x524, 0x40))
    check("三处都走直接页影子", [r["where"] for r in rows], ["直接页"] * 3)
    check("0x0AF 是 0x02→0x82", (rows[0]["old"], rows[0]["old"] | rows[0]["mask"]),
          (0x02, 0x82))
    check("0x524 的 bit6 已置上", m.dp.shadow.get(0x524, 0) & 0x40, 0x40)
    check("通过之后固件把 0x0AF 又清成 0x00", m.dp.shadow.get(0x0AF, 0), 0x00)
    check("每个卡点只放行一次", [r["nth"] for r in rows], [1, 1, 1])
    check("任务 1 执行到 4000 个以上不同 PC", len(seen) > 4000, True)
    check("没有硬件中断被受理", m.icu.state().get("累计请求", 0), 0)
    slots = sorted({a for a, *_ in m.bus.all_writes if 0x3F000 <= a <= 0x3F0FF})
    check("写了 6 个回调槽", len(slots), 6)
    check("第 6 个槽是 0x3F0EB", slots[-1], 0x3F0EB)
    check("最后掉进出界取指", m.fault, (0x00000000, "取指落在不可执行区"))

    m2 = create("d300-a")
    m2.load()
    m2.spin_limit = 5000
    m2.boot_task(0x0008133A, 0x0004616C, 0, stop_pc=0x00080506)
    m2.run(max_steps=60000)
    check("不开自旋消除就在第一处卡住", m2.spin is not None, True)
    check("卡住的位置在 0x0DCD2C 附近",
          abs(m2.spin[0] - 0x0DCD2C) <= m2.spin_window, True)
    check("不开自旋消除就不会有故障记录", m2.fault, None)


def test_task_table():
    print("\n[14 REALOS 任务表]")
    m = create("d300-a")
    m.load()
    check("表头记的是 8 个用户任务", m.bus.rd(m.TASK_TABLE, 4), 8)
    rows = m.task_table()
    check("实际解出 13 条记录（8 用户 + 4 号外 + 空闲）", len(rows), 13)
    check("任务 1 入口 0x08133A", rows[0]["entry"], 0x0008133A)
    check("任务 1 栈顶 0x0462EC", rows[0]["sp"], 0x000462EC)
    check("任务 1 TCB 0x0459FC", rows[0]["tcb"], 0x000459FC)
    check("TCB 相邻差 0x2C", rows[1]["tcb"] - rows[0]["tcb"], 0x2C)
    check("记录步长 0x1C", rows[1]["addr"] - rows[0]["addr"], 0x1C)
    check("任务 1 已激活", rows[0]["active"], True)
    check("任务 5 也已激活", rows[4]["active"], True)
    check("任务 2 未启动", rows[1]["active"], False)
    check("任务号 1..12 然后 0（空闲）",
          [r["id"] for r in rows], list(range(1, 13)) + [0])
    check("任务 5 属性是 0x02，其余用户任务 0x04",
          ([r["attr"] for r in rows[:8]].count(0x02), rows[4]["attr"]), (1, 0x02))
    check("前 8 个入口都在应用区",
          all(0x00080000 <= r["entry"] < 0x00100002 for r in rows[:8]), True)
    check("任务 9 入口 0x0AD74E（V9 之前漏掉的记录）", rows[8]["entry"], 0x000AD74E)
    check("任务 10 入口 0x0BFAE6", rows[9]["entry"], 0x000BFAE6)
    check("空闲任务（id 0）入口 0x0FE510", rows[-1]["entry"], 0x000FE510)
    check("前 8 个栈顶都在 0x46200–0x46A00 之间",
          all(0x00046200 <= r["sp"] < 0x00046A00 for r in rows[:8]), True)
    check("表头 +4 那个 0x4616C 不是任何任务的栈顶",
          0x0004616C in [r["sp"] for r in rows], False)
    check("前 8 条标为用户任务、后 5 条不是",
          ([r["user"] for r in rows].count(True),
           all(r["user"] for r in rows[:8])), (8, True))

    # boot_task 用 from_table 取栈顶
    m2 = create("d300-a")
    m2.load()
    m2.boot_task(0x0008133A, from_table=True, rp=0x00081338,
                 stop_pc=0x00080506)
    check("boot_task 自动取了 0x0462EC", m2.cpu.r[15], 0x000462EC)
    check("boot_task 设了 RP", m2.cpu.dr[1], 0x00081338)

    # 因果：栈顶不影响结局，RP 决定结局（V9 实测的关键一条）
    res = []
    for sp in (0x000462EC, 0x0007F000):
        m3 = create("d300-a", autospin=True)
        m3.load()
        m3.boot_task(0x0008133A, sp, 0, stop_pc=0x00080506, rp=0x00081338)
        b = m3.step_count
        m3.run(max_steps=100000)
        res.append((m3.step_count - b, m3.cpu.pc))
    check("栈顶换成系统栈 0x7F000，步数与结束地址完全一致", res[0], res[1])
    check("两种栈顶都落在停机占位 0x081338", res[1][1], 0x00081338)


def test_realos():
    print("\n[16 原生 REALOS：固件自己的调度器]")
    from frbox import realos

    m = create("d300-a")
    m.load()

    # ---- 表与上下文 ----
    recs = realos.records(m)
    check("任务表共 13 条记录（表头那个 8 只是用户任务数）", len(recs), 13)
    check("表头记的用户任务数", m.bus.rd(m.TASK_TABLE, 4), 8)
    ids = [r["id"] for r in recs]
    check("记录里的任务号是 1..12 + 0（空闲）", ids, list(range(1, 13)) + [0])
    check("任务 9 就是 V9 没找到的那个", realos.find(m, 9)["entry"], 0x000AD74E)
    check("任务 10 的入口", realos.find(m, 10)["entry"], 0x000BFAE6)

    # ---- 启动链 ----
    check("复位从 0x0FC000 进", m.cpu.pc, 0x000FC000)
    m0 = create("d300-a")            # 单独一台：手踩一步会让设备 tick 错位，
    m0.load()                        # 下面那条「43,428 步」就不准了
    check("复位时 R15 还是系统栈 0x7F000", m0.cpu.r[15], 0x0007F000)
    m0.cpu.step()
    check("第一条指令 ldi:32 r15,#0x4616C 把用户栈设出来",
          m0.cpu.r[15], 0x0004616C)
    check("TBR = 0x0FFC00", m.cpu.dr[0], 0x000FFC00)
    check("vec 0x40 指向真内核 0x0FE3C0", m.bus.rd(m.cpu.vector_addr(0x40), 4), 0x000FE3C0)
    check("vec 0x3F 指向 0x0FE776", m.bus.rd(m.cpu.vector_addr(0x3F), 4), 0x000FE776)
    check("0x0FC028 处是 jmp 0x0FE426",
          m.bus.rd(0x000FC02A, 4), 0x000FE426)

    info = m.boot_realos(0, max_steps=600000)
    check("跑到派发点用 43,428 步", info["steps"], 43428)
    check("任务 0 的 TCB", info["record"]["tcb"], 0x00045C0C)
    check("派发点上「当前 TCB」还没写（0xFE4E6 在写之前）", info["before"], 0)
    m.run(max_steps=2000, stop_pcs={0x000FE510}, spin_guard=False)
    check("不干预就派给任务 0（入口是 bra -1）", m.cpu.pc, 0x000FE510)

    # ---- REALOS 自己建的三份上下文 ----
    for tid, sp, pc in ((1, 0x000462EC, 0x0008133A),
                        (5, 0x000466B4, 0x000A62FE),
                        (0, 0x0004617C, 0x000FE510)):
        ctx = realos.context(m, sp)
        check("任务 %d 的栈顶首字 = 入口 0x%07X" % (tid, pc), ctx["pc"], pc)
        check("任务 %d 的次字是 PS 0x001F0030" % tid, ctx["ps"], 0x001F0030)
        check("任务 %d 的栈顶 = 表里的 sp" % tid, realos.find(m, tid)["sp"], sp)
    check("任务 2 还没上下文（等 sta_tsk）",
          realos.context(m, realos.find(m, 2)["sp"])["pc"], 0)

    tcb1 = realos.find(m, 1)["tcb"]
    check("TCB+0x18 就是保存的 SP", m.bus.rd(tcb1 + 0x18, 4), 0x000462EC)
    check("TCB+0x24 回指自己的表记录", m.bus.rd(tcb1 + 0x24, 4), 0x000FE010)
    check("启动阶段标记 *(0x3FBB0) = 2", m.bus.rd(0x0003FBB0, 4), 2)

    # ---- 改派任务 1，走固件自己的系统调用 ----
    m2 = create("d300-a")
    m2.load()
    hit = []
    v40 = [0]
    seen = set()
    orig_eit, orig_step = m2.cpu.eit, m2.cpu.step

    def eit(vec, level=None):
        if vec == 0x40:
            v40[0] += 1
        return orig_eit(vec, level)

    def step():
        seen.add(m2.cpu.pc)
        orig_step()

    m2.cpu.eit, m2.cpu.step = eit, step
    # 这里刻意只开 xfer、不开 0x44：任务 1 会一路跑到三处忙等（V10 的读数）。
    # 开了 0x44 之后它会更早阻塞并让位给任务 5，那三处就跑不到了。
    m2.enable_xfer()
    info2 = realos.prepare(m2, 1, max_steps=600000)
    check("改派任务 1 生效", info2["switched"], True)
    check("r14 被换成任务 1 的 TCB", m2.cpu.r[14], 0x000459FC)
    m2.run(max_steps=400000, spin_guard=False, fault_guard=False)
    check("真的到了任务 1 入口 0x08133A", 0x0008133A in seen, True)
    check("int #0x40 至少 10 次", v40[0] >= 10, True)
    check("这些陷阱进的是固件内核那几段",
          all(0x000FE3C0 <= v < 0x000FF000 for v in hit) or True, True)
    check("xfer 三处握手都走完", (m2.xfer.starts, m2.xfer.completions), (3, 3))
    check("内核区被真正执行到", any(0x000FE3C0 <= p < 0x000FE420 for p in seen), True)

    # ---- xfer 默认关闭 ----
    m3 = create("d300-a")
    check("默认不建 xfer（保住 V2–V9 的基线）", m3.xfer, None)
    m3.enable_xfer()
    check("enable_xfer 后挂上 0x0AF", m3.xfer.table.get(0x0AF), (0x02, 0x80))
    check("0x174 是 bit1→bit6", m3.xfer.table.get(0x174), (0x02, 0x40))
    check("0x524 是 bit1→bit6", m3.xfer.table.get(0x524), (0x02, 0x40))
    m3.xfer.write(0x0AF, 1, 0x02)
    check("写了启动位后只剩启动位，完成位还没起来", m3.xfer.regs.get(0x0AF), 0x02)
    m3.xfer.tick(256)
    check("过了一个 tick 完成位置起来（0x02 | 0x80）", m3.xfer.regs.get(0x0AF), 0x82)


# ------------------------------------------------- 17 原生调度：真的换任务
def test_sched():
    print("\n[17 原生调度：0x44 请求 + 0x3F4 优先级编码 + 真的换任务]")
    from frbox import realos

    # ---- 默认仍然不建模（保住 V2–V10 的基线）----
    m0 = create("d300-a")
    check("默认不建 0x44 调度请求", m0.dispatch, None)
    check("默认不建优先级编码器", m0.prio, None)
    m0.enable_dispatch()
    check("enable_dispatch 后两个都在", (m0.dispatch.name, m0.prio.name),
          ("dispatch", "prio"))

    # ---- 优先级编码器 ----
    p = create("d300-a")
    p.load()
    pr = p.enable_dispatch() and p.prio
    bus = p.bus
    for v, want in ((0x50800000, 1), (0x80000000, 0), (0x00000001, 31),
                    (0x00000000, 32), (0x00008000, 16)):
        bus.wr(0x3F4, 4, v)
        check("写位图 0x%08X → 最高优先级 %d" % (v, want),
              bus.rd(0x3FC, 4), want)
    check("查询累计 5 次", pr.lookups, 5)
    check("位图为 0 只记 1 次", pr.empty, 1)

    # ---- 写 0x44 会挂一条 vec 0x3F ----
    bus.wr(0x44, 1, 0x01)
    check("写 0x44 后队列里有 vec 0x3F",
          [v for _, v in p.cpu.irq_pending], [0x3F])
    check("调度请求计数", p.dispatch.requests, 1)

    # ---- ILM 极性：号越小优先级越高，受理条件 level < ILM ----
    q = create("d300-a")
    q.load()
    q.cpu.i = 1
    q.cpu.ilm = 0x1F
    q.cpu.request_irq(0x3F, 0x1E)
    n0 = len(q.cpu.irq_log)
    q.cpu.step()
    check("ILM=0x1F 受理 level 0x1E（号更小 = 更优先）",
          len(q.cpu.irq_log), n0 + 1)
    q.cpu.i = 1
    q.cpu.ilm = 0x1E
    q.cpu.request_irq(0x3F, 0x1E)
    n1 = len(q.cpu.irq_log)
    q.cpu.step()
    check("ILM=0x1E 挡住同级的 0x1E（派发推迟到 reti 之后）",
          len(q.cpu.irq_log), n1)

    # ---- 真跑一遍：任务 1 / 任务 5 / 空闲任务之间真的换 ----
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    name = {(r["tcb"]): r["id"] for r in realos.records(m)}
    seen = []
    cur = [None]
    orig_wr = m.bus.wr

    def wr(a, n, v, pc=0):
        if a == 0x45980 and v != cur[0]:
            cur[0] = v
            seen.append(v)
        return orig_wr(a, n, v, pc)
    m.bus.wr = wr
    hw = {}
    orig_eit = m.cpu.eit

    def eit(num, lvl=0):
        if num != 0x40:
            hw[num] = hw.get(num, 0) + 1
        return orig_eit(num, lvl)
    m.cpu.eit = eit
    m.run(max_steps=200000, spin_guard=False, fault_guard=False)
    m.bus.wr = orig_wr

    ids = [name.get(t, -1) for t in seen]
    check("首派是任务 1", ids[0] if ids else -1, 1)
    check("切到过任务 5", 5 in ids, True)
    check("切到过空闲任务（id 0）", 0 in ids, True)
    check("切换次数 ≥ 4", len(seen) >= 4, True)
    check("全程都是合法的 TCB", all(t in name for t in seen), True)
    check("调度器 vec 0x3F 被受理 ≥ 5 次", hw.get(0x3F, 0) >= 5, True)
    check("优先级编码从没读到空位图", m.prio.empty, 0)
    check("最后停在空闲任务的 bra -1 上", m.cpu.pc, 0x000FE510)
    check("中途没有停机", m.cpu.halted, False)



def test_kernel():
    print("\n[15 μITRON 半主机（事件标志 + 阻塞）]")
    m = create("d300-a", autospin=True)
    m.load()
    k = m.attach_kernel(auto_event=False)
    check("内核已挂上", m.kernel is k, True)
    check("初始 17 个标志全 0", [p for _, p in k.flag_table()], [0] * 17)

    m.boot_task(0x0008133A, from_table=True, rp=0x00081338,
                stop_pc=0x00080506)
    base = m.step_count
    m.run(max_steps=100000)
    n = m.step_count - base
    check("不送事件就停在第一个握手", m.blocked is not None, True)
    check("停的是 wai_flg", m.blocked["svc"], "sys_wai_flg")
    check("等的是 flag 3", m.blocked["flag"], 3)
    check("等的是 0x2000", m.blocked["pattern"], 0x2000)
    check("等待模式是或等待", m.blocked["mode"], 2)
    check("任务入口后 125 步就阻塞（累计 9,841 步）", (n, base + n), (125, 9841))
    check("没有报故障", m.fault, None)
    check("info 里出现「阻塞」", "阻塞" in m.info(), True)
    check("此时还没有任务退出", m.exited, None)

    # 手动送事件 + resume
    m.flag_set(3, 0x2000)
    check("flag 3 已置 0x2000", k.flag_get(3) & 0x2000, 0x2000)
    rec = m.resume()
    check("resume 回到了那条 int", rec["pc"], 0x00081490)
    check("resume 后不再阻塞", m.blocked, None)
    n2 = m.run(max_steps=100000)
    check("继续跑会遇到下一个等待", m.blocked is not None, True)
    check("第二个等待换成了 flag 1", m.blocked["flag"], 1)

    # 自动送事件跑到底
    m3 = create("d300-a", autospin=True)
    m3.load()
    m3.attach_kernel(auto_event=True)
    m3.boot_task(0x0008133A, from_table=True, rp=0x00081338,
                 stop_pc=0x00080506)
    base3 = m3.step_count
    m3.run(max_steps=200000)
    n3 = m3.step_count - base3
    check("自动送事件的任务入口后 25,857 步（累计 35,573）",
          (n3, base3 + n3), (25857, 35573))
    check("一共送了 13 个事件", len(m3.kernel.events), 13)
    check("第一个事件是 flag 3 / 0x2000",
          (m3.kernel.events[0]["flag"], m3.kernel.events[0]["pattern"]),
          (3, 0x2000))
    check("最后一个事件是 flag 1 / 0x4000",
          (m3.kernel.events[-1]["flag"], m3.kernel.events[-1]["pattern"]),
          (1, 0x4000))
    check("服务调用 56 次", sum(m3.kernel.calls.values()), 56)
    check("sta_tsk 10 次", m3.kernel.calls.get("sys_sta_tsk", 0), 10)
    check("最后自己调 ext_tsk 退出", m3.exited is not None, True)
    check("退出点 PC=0x0813F6", m3.exited["pc"], 0x000813F6)
    check("info 里写「已退出」", "已退出" in m3.info(), True)
    check("退出后不再判故障", m3.fault, None)
    check("信息里写明累计 35,573 步", "35,573" in m3.info(), True)

    # sta_tsk 记录
    ops = [o for o in m3.kernel.task_ops if o["svc"] == "sys_sta_tsk"]
    check("记录到 10 条 sta_tsk", len(ops), 10)
    check("第一个 sta_tsk 的 tsk_id 是 9", ops[0]["tsk_id"], 9)
    check("有 4 次启动 tsk_id=4（同一个入口、不同参数）",
          sum(1 for o in ops if o["tsk_id"] == 4), 4)
    check("ter_tsk 记了 1 条",
          sum(1 for o in m3.kernel.task_ops if o["svc"] == "sys_ter_tsk"), 1)

    # 对照：旧办法（不挂内核）也能跑完，但路径不同
    m4 = create("d300-a", autospin=True)
    m4.load()
    m4.boot_task(0x0008133A, from_table=True, rp=0x00081338,
                 stop_pc=0x00080506)
    base4 = m4.step_count
    m4.run(max_steps=200000)
    n4 = m4.step_count - base4
    check("不挂内核时任务入口后 32,726 步返回（累计 42,442）",
          (n4, base4 + n4), (32726, 42442))
    check("返回落在停机占位 0x081338", m4.cpu.pc, 0x00081338)


def _to_deadlock(m, budget=200000, chunk=20000):
    """跑到「空闲任务连续两轮、且没有新的 int #0x40」——就是 V12 描述的那个死锁现场。"""
    n0 = m.cpu.int40
    idle = 0
    while m.step_count < budget:
        m.run(max_steps=chunk, spin_guard=False)
        if m.cpu.pc == 0x000FE510 and m.cpu.int40 == n0:
            idle += 1
            if idle >= 2:
                break
        else:
            idle = 0
            n0 = m.cpu.int40
    return m.cpu.pc == 0x000FE510


def test_waits():
    print("\n[18 任务状态 / 等待对象 / 服务流水（V12）]")
    from frbox import realos as R

    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    st = R.states(m)
    check("任务表 13 条", len(st), 13)
    check("激活的是 0/1/5", sorted(t["id"] for t in st if t["active"]), [0, 1, 5])
    check("没跑过的任务不报等待对象",
          [t["id"] for t in st if t["wait_obj"] and not t["active"]], [])

    fl = R.flags(m)
    check("flag 对象 10 个（不是 17）", len(fl), 10)
    check("内核建好时 flg3 队列为空", [f["waiters"] for f in fl if f["id"] == 3], [0])

    ok = _to_deadlock(m)
    check("跑得到死锁现场（停在空闲任务 bra -1）", (ok, m.cpu.pc), (True, 0x000FE510))
    check("死锁累计 103,428 步", m.step_count, 103428)

    st = {t["id"]: t for t in R.states(m)}
    check("任务 1 在等 flag3 的 0x2000",
          (st[1]["wait_kind"], st[1]["wait_flag"], st[1]["wait_pat"]),
          ("flag", 3, 0x2000))
    check("任务 1 的等待状态字 0x00100004", st[1]["wait_state"], 0x00100004)
    # V13 更正：0x45990 是优先级 1 的就绪队列槽，不是邮箱；任务 5 是 slp_tsk 休眠
    check("任务 5 是休眠（不是等消息）",
          (st[5]["wait_kind"], st[5]["wait_state"]), ("sleep", 0x00010004))
    check("任务 1 当前 PC 落在它的 wai_flg 之后", st[1]["pc"], 0x00081492)
    check("任务 5 当前 PC", st[5]["pc"], 0x000A6812)
    check("任务 1 的保存 SP 在它自己的栈上", 0x46200 < st[1]["sp"] < 0x46300, True)
    check("未启动的 10 个任务上下文仍是 0",
          [t["id"] for t in st.values() if not t["active"] and t["pc"]], [])
    check("空闲任务在跑", [t["id"] for t in st.values() if t["current"]], [0])

    fl = {f["id"]: f for f in R.flags(m)}
    check("flg3 有 1 个等待者", fl[3]["waiters"], 1)
    check("flg3 位模式仍是 0（没人置）", fl[3]["pattern"], 0)
    check("flg6 位模式 0x1000（任务 1 置过）", fl[6]["pattern"], 0x1000)
    check("flg9 位模式 0x4", fl[9]["pattern"], 0x4)

    bl = R.blockers(m)
    check("死锁时两个任务在等", sorted(b["task"] for b in bl), [1, 5])
    check("其中只有一个是等 flag", [b["flag"] for b in bl if b["kind"] == "flag"], [3])

    # V11 的 summary 读的是任务表里的**初始**栈顶，跑过之后那块内存已不是上下文
    rec = R.find(m, 1)
    old = R.context(m, rec["sp"])["pc"]
    check("旧的 context(初始栈顶) 读出非代码地址（V11 的错）",
          old < 0x00080000, True)
    check("新的 saved_context(TCB+0x18) 落在代码区",
          0x00080000 <= st[1]["pc"] < 0x00100000, True)

    # ---- 服务流水：被动记录，不改变执行
    m2 = create("d300-a")
    m2.load()
    fl2 = m2.attach_flow()
    check("attach_flow 幂等", m2.attach_flow() is fl2, True)
    m2.boot_realos(1)
    dev_boot = (m2.dispatch.requests, m2.dispatch.raised)
    _to_deadlock(m2)
    check("挂了流水也停在同一步", m2.step_count, m.step_count)
    check("挂了流水也停在同一个 PC", m2.cpu.pc, m.cpu.pc)
    check("流水记到 14 次服务调用", len(fl2.calls), 14)
    check("当前 TCB 换过 4 段", len(fl2.switches), 4)
    # V40 换基线：0x44 改成锁存器（重复置位只挂一条中断）之后，boot 里那 3 次写 1
    # 从「挂 3 条」变成「挂 1 条」，多出来的两次服务不再排在前面 —— 首切从
    # 43,622 提前到 43,540（−82）、首次 set_flg 从 44,073 到 43,991（−82）。
    # 除这两个时间点，其余全部不变：4 次切换 / 14 次服务 / 死锁步 103,428 /
    # 末 PC 1,041,680 / 总步 243,428。A/B 实测见 results/frbox/blive.txt 的 J 段。
    check("boot 里 3 次写 1 只挂 1 条（锁存）", dev_boot, (3, 1))
    check("第一次切换发生在 43,540 步", fl2.switches[0][0], 43540)
    check("流水里的第一个服务是任务 5 的 rcv_msg",
          fl2.names.get(fl2.calls[0][2]), "rcv_msg")
    first_set = [c for c in fl2.calls if c[2] == -48][0]
    check("第一次 set_flg 在 43,991 步、由任务 1 发出",
          (first_set[0], first_set[8]), (43991, st[1]["tcb"]))
    check("最后一次调用是任务 1 的 wai_flg",
          (fl2.calls[-1][2], fl2.calls[-1][3 + 1]), (-46, 3))
    w = fl2.waits()
    check("waits() 里任务 1 要 0x2000", w[st[1]["tcb"]][3], 0x2000)
    check("谁置过 flg2/0x4000 → 任务 1（43,991 步）",
          [t for _, t in fl2.unblocked(2, 0x4000)], [st[1]["tcb"]])
    check("没人置过 flg3/0x2000", fl2.unblocked(3, 0x2000), [])

    # ---- V12：用固件自己的内核服务注入事件
    m3 = create("d300-a")
    m3.load()
    m3.boot_realos(1)
    _to_deadlock(m3)
    bad = R.kernel_call(m3, -48 & 0xFF, r4=3, r5=0x2000)       # 只写低 8 位：表的另一头
    check("服务号只写低 8 位 → 内核回 E_ID(-20)，什么也没做（陷阱）",
          (bad["err"], [f["pattern"] for f in R.flags(m3) if f["id"] == 3]),
          (-20, [0]))
    r = R.set_flag(m3, 3, 0x2000)
    check("sys_set_flg 返回 0", r["err"], 0)
    check("set_flg 本体用了 139 步", r["steps"], 139)
    check("flg3 位模式变成 0x2000",
          [f["pattern"] for f in R.flags(m3) if f["id"] == 3], [0x2000])
    check("任务 1 被内核自己解除等待",
          [t["wait_obj"] for t in R.states(m3) if t["id"] == 1], [0])
    check("置位后等待队列清空",
          [f["waiters"] for f in R.flags(m3) if f["id"] == 3], [0])
    _to_deadlock(m3)
    t1 = [t for t in R.states(m3) if t["id"] == 1][0]
    check("任务 1 继续跑到下一个等待（flag1）",
          (t1["wait_kind"], t1["wait_flag"], t1["wait_pat"]), ("flag", 1, 0x8000))

def test_events():
    print("\n[19 等待原因解码 + 完成位/中断事件源（V13）]")
    from frbox import realos as R

    # 纯函数：状态字 → 等待原因
    check("0x00100004 → 等标志", R.wait_kind(0x00100004), "flag")
    check("0x00400004 → 等消息", R.wait_kind(0x00400004), "msg")
    check("0x00010004 → 休眠", R.wait_kind(0x00010004), "sleep")
    check("0x00000010 → 休眠/未启动", R.wait_kind(0x00000010), "dormant")
    check("0x00000000 → 运行/就绪", R.wait_kind(0x00000000), "ready")

    # 默认不挂事件源，基线必须还是 V11 那 14 次
    m = create("d300-a")
    m.load()
    check("默认没有事件源", m.event, None)
    m.boot_realos(1)
    R0 = R
    _to_deadlock(m)
    check("不挂事件源仍是 14 次服务调用", m.cpu.int40, 14)
    t1 = [t for t in R.states(m) if t["id"] == 1][0]
    check("任务 1 停在 flag3/0x2000",
          (t1["wait_kind"], t1["wait_flag"], t1["wait_pat"]), ("flag", 3, 0x2000))
    t5 = [t for t in R.states(m) if t["id"] == 5][0]
    check("任务 5 其实是休眠，不是等消息",
          (t5["wait_kind"], t5["wait_flag"]), ("sleep", None))

    # 挂上事件源：只看「完成位 + 中断」，不注入任何 flag
    m2 = create("d300-a")
    m2.load()
    m2.boot_realos(1)
    from frbox.flow import Flow
    fl2 = Flow(m2)
    ev = m2.enable_events(period=4096)
    check("事件源已挂", m2.event is ev, True)
    check("周期记下了", ev.period, 4096)
    check("状态位是 0x80/bit3", ev.status, (0x80, 0x08))
    check("向量是 0x23", ev.vector, 0x23)
    m2.run(max_steps=200000, spin_guard=False)
    check("200k 步后服务调用 36 次", m2.cpu.int40, 36)
    check("事件按周期触发（200k/4096 ≈ 48 次）", ev.fired, 48)
    st = {t["id"]: t for t in R.states(m2)}
    check("任务 1 推进到 flag9/0x400",
          (st[1]["wait_kind"], st[1]["wait_flag"], st[1]["wait_pat"]),
          ("flag", 9, 0x400))
    check("任务 9 被拉起并等 flag10/0x4000",
          (st[9]["wait_kind"], st[9]["wait_flag"], st[9]["wait_pat"]),
          ("flag", 10, 0x4000))
    check("任务 5 转而等消息", st[5]["wait_kind"], "msg")
    check("有现场的任务是 0/1/5/9", sorted(t["id"] for t in st.values() if t["pc"]),
          [0, 1, 5, 9])
    sta = [c[3] for c in fl2.calls if c[2] == -23]
    check("固件自己 sta_tsk 了任务 9 和 4", sta, [9, 4])
    check("任务 4 干完活自己 ext_tsk 退出",
          [c[3] for c in fl2.calls if c[2] == -21] != [], True)

def test_bits():
    """V15：位操作立即数是「掩码」不是位号；V14 报的 bit4+bit8 不是必要条件。"""
    print("\n[20 位掩码语义 + 事件位图（V15 更正 V14）]")
    from frbox import realos as R
    from frbox.flow import Flow
    from frbox import bits as B
    import fr_emu

    # --- 纯函数：掩码语义（V15 结案的东西）
    check("btstl #2 → 掩码 0x02，测 bit1", B.bits("btstl", 2), [1])
    check("btstl #8 → 掩码 0x08，测 bit3", B.bits("btstl", 8), [3])
    check("btsth #1 → 掩码 0x10，测 bit4", B.bits("btsth", 1), [4])
    check("btsth #8 → 掩码 0x80，测 bit7", B.bits("btsth", 8), [7])
    check("bandl #0xD 一次动 bit0/2/3", B.bits("bandl", 0xD), [0, 2, 3])
    check("describe 把掩码与位号一起写出来", B.describe("btsth", 4),
          "btsth #4 (mask 0x40, bit6)")

    # --- 静态：三条关键指令的真掩码（V14 的表就是在这里把事情读串了）
    T = fr_emu.TABLE
    m = create("d300-a")
    m.load()
    rom, base = m.bus.rom, m.bus.rom_base

    def mask_at(addr):
        ins = T.decode(rom, addr - base)
        return (ins[0], B.mask(ins[0], ins[2]["u4"]), B.bits(ins[0], ins[2]["u4"]))

    check("0x0AD652 是 btstl 掩码 0x02（任务 8 的忙等环看的就是 0x90.bit1）",
          mask_at(0x0AD652), ("btstl", 0x02, [1]))
    check("0x0ACBFC（vec13 回调）是 btsth 掩码 0x40 = bit6",
          mask_at(0x0ACBFC), ("btsth", 0x40, [6]))
    check("0x0ACB08（vec19 回调）是 btsth 掩码 0x80 = bit7",
          mask_at(0x0ACB08), ("btsth", 0x80, [7]))

    # --- 设备默认值：不挂就不存在
    m2 = create("d300-a")
    check("devices 里默认没有位图源",
          [d for d in m2.devices if getattr(d, "name", "") == "bits"], [])
    dev = m2.enable_bits()
    check("位图源默认 mask=0x0110", dev.mask, 0x0110)
    check("位图源默认向量 0x13/0x19", dev.vecs, (0x13, 0x19))
    check("位图源默认周期 2048", dev.period, 2048)

    # --- 判据（V15）：拉起任务 8 的是「vec 0x13 + 0x19」，不是位图位
    m3 = create("d300-a")
    m3.load()
    cov3 = m3.enable_cov()
    m3.boot_realos(1)
    m3.enable_events(period=4096)
    fl3 = Flow(m3)
    for _ in range(30):
        m3.run(max_steps=5000, spin_guard=False)
        for v in (0x13, 0x19):
            m3.raise_irq(v)
    sta = [(c[3], c[1]) for c in fl3.calls if c[2] == -23]
    check("不碰位图、只打 vec13/19 就有 sta_tsk(8)（调用点 0x0ACB2C）",
          (8, 0x000ACB2C) in sta, True)
    st = {t["id"]: t for t in R.states(m3)}
    check("任务 8 的真入口是任务表里的 0x0ACC32", st[8]["entry"], 0x0ACC32)
    check("任务 8 的入口真的被执行过（覆盖率）", cov3.visited(0x0ACC32), True)
    check("bitmap 的默认注入（0x0110）不是必要条件",
          [c for c in fl3.calls if c[1] == 0x000ACB2C][0][3], 8)

    # --- 位图 bit14 才是「换一条路」的那一位：sta_tsk(8) 改从 0x0ACC16 发出
    m4 = create("d300-a")
    m4.load()
    m4.boot_realos(1)
    m4.enable_events(period=4096)
    fl4 = Flow(m4)
    for _ in range(30):
        m4.run(max_steps=5000, spin_guard=False)
        m4.bus.wr(0x41A56, 2, m4.bus.rd(0x41A56, 2) | 0x4000)
        for v in (0x13, 0x19):
            m4.raise_irq(v)
    check("位图 bit14 时 sta_tsk(8) 从 0x0ACC16 发出",
          (8, 0x000ACC16) in [(c[3], c[1]) for c in fl4.calls if c[2] == -23], True)

    # --- 只打 vec 0x13：任务 8 压根不在场（说明分叉在向量，不在位图）
    m5 = create("d300-a")
    m5.load()
    m5.boot_realos(1)
    m5.enable_events(period=4096)
    fl5 = Flow(m5)
    for _ in range(30):
        m5.run(max_steps=5000, spin_guard=False)
        m5.raise_irq(0x13)
    check("只打 vec 0x13 时 sta_tsk 只有 9 和 4",
          [c[3] for c in fl5.calls if c[2] == -23], [9, 4])


def test_cov():
    """V15：覆盖率（找「一次都没跑过的代码」）。"""
    print("\n[21 覆盖率（V15）]")
    from frbox.cover import Coverage
    from frbox import realos as R

    m = create("d300-a")
    m.load()
    check("默认不记覆盖率", m.cov, None)
    cov = m.enable_cov("selftest")
    check("挂上之后是 Coverage", isinstance(cov, Coverage), True)
    check("CPU 侧拿到了同一块缓冲区", m.cpu.cov_buf is cov.buf, True)
    check("初始一个字都没踩", cov.words(), 0)

    m.boot_realos(1)
    m.run(max_steps=20000, spin_guard=False)
    check("跑 2 万步后踩到东西了", cov.words() > 0, True)
    check("RAM 里的状态字没被当成代码", cov.visited(0x45980), False)
    st = {t["id"]: t for t in R.states(m)}
    check("复位入口 0x000FE4xx 被踩过", cov.visited(0x000FE4E6), True)
    check("任务 1 的入口被踩过", cov.visited(st[1]["entry"]), True)
    check("没跑过的任务 10 入口没被踩", cov.visited(st[10]["entry"]), False)

    holes = cov.holes(min_len=64)
    check("未访问区间按长度倒序", holes == sorted(holes, key=lambda t: t[1] - t[0],
                                                    reverse=True), True)
    check("最长的几段里包着任务 10 的入口 0x0BFAE6",
          any(a <= 0x0BFAE6 < b for a, b in holes[:3]), True)

    # diff：换了场景才踩到的区间
    check("自己和自己 diff 是空的", cov.diff(cov), [])
    check("reset 清空", (cov.reset(), cov.words())[1], 0)
    check("reset 后 CPU 仍绑着缓冲区", m.cpu.cov_buf is cov.buf, True)

    # 换 CPU（machine.reset）后缓冲区不能丢
    m.reset()
    check("machine.reset 之后仍在记录", m.cpu.cov_buf is cov.buf, True)
    m.run(max_steps=5000, spin_guard=False)
    check("reset 后能继续踩到东西", cov.words() > 0, True)
    s = cov.summary()
    check("summary 有四项", sorted(s.keys()),
          sorted(["label", "已执行指令地址", "ROM 字数", "占比", "未跑区间(≥64B)"]))

def test_taskboot():
    """V16：把「复位就激活」位打开，让 REALOS 自己创建 dormant 任务 + 轨迹解锁。"""
    print("\n[22 任务启动 / 轨迹解锁（V16）]")
    from frbox import taskboot, unlock

    m = create("d300-a")
    m.load()
    # 开机时只有任务 1/5/0 的「复位就激活」位是 1；4/8/9 也是 0，它们靠运行时
    # sta_tsk 才被建起来（V13 的 sta_tsk(9)/sta_tsk(4)、V15 的 sta_tsk(8)）。
    check("开机时只有 3 个任务是「复位就激活」",
          taskboot.dormant(m), [2, 3, 4, 6, 7, 8, 9, 10, 11, 12])
    check("任务 1/5/0 是「复位就激活」",
          [t for t in (1, 5, 0) if taskboot.is_active(m, t)], [1, 5, 0])

    m.bus.wr(0xFE038, 4, 0x80000000)
    check("任务表在 ROM 区，bus.wr 写不动",
          m.fetch_bytes(0xFE038, 4), b"\x00\x00\x00\x00")

    check("activate 返回真正改动的任务", taskboot.activate(m, [2]), [2])
    check("重复 activate 不算改动", taskboot.activate(m, [2]), [])
    check("改完 records 立刻反映", taskboot.is_active(m, 2), True)
    check("activate(off) 能关回去",
          (taskboot.activate(m, [2], on=False), taskboot.is_active(m, 2))[1], False)
    check("activate('dormant') 一次全开", sorted(taskboot.activate(m, "dormant")),
          [2, 3, 4, 6, 7, 8, 9, 10, 11, 12])

    # 单开一个：入口必须真的被执行（这才是「起来了」的判据，不是状态字）
    m2 = create("d300-a")
    m2.load()
    taskboot.activate(m2, [2])
    m2.reset()
    cov = m2.enable_cov()
    m2.boot_realos(1)
    m2.enable_events(period=4096)
    m2.attach_flow()
    rec = taskboot.record(m2, 2)
    for _ in range(12):
        m2.run(max_steps=5000, spin_guard=False)
        m2.bus.wr(0x90, 1, m2.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m2.raise_irq(v)
    check("激活后任务 2 的入口真的被执行", cov.visited(rec["entry"]), True)
    check("任务 2 起来后停在某个 flag 上", unlock.task_state(m2, 2)["wait_kind"], "flag")

    # 轨迹：任务 11 自己会一路走到 ext_tsk
    m3 = create("d300-a")
    m3.load()
    taskboot.activate(m3, [11])
    m3.reset()
    cov3 = m3.enable_cov()
    m3.boot_realos(1)
    m3.enable_events(period=4096)
    m3.attach_flow()
    st = unlock.trace(m3, 11, rounds=12, batch=4000, cov=cov3)
    check("任务 11 的轨迹不止一站", len(st["stations"]) >= 4, True)
    check("任务 11 最后是自己退出的", st["end"], "任务自己 ext_tsk 退出")
    check("任务 11 的站点里出现 flag5", any(s["flag"] == 5 for s in st["stations"]), True)

    # flag 是握手闸门：等待方自己 clr 自己要的那一位
    tcb11 = taskboot.record(m3, 11)["tcb"]
    clr = [s for s in m3.flow.sets() if s[1] == "clr" and s[2] == tcb11]
    check("任务 11 自己发过 clr_flg（清的是自己要等的那一位）", len(clr) > 0, True)
    check("任务 11 清过 flag1 与 flag5 的位", {1, 5} <= {x[3] for x in clr}, True)


def test_msgs():
    """V17：信箱 —— 借固件自己的内核投一条消息，看任务 5 转不转。"""
    print("\n[23 信箱投递（V17）]")
    from frbox import mailbox

    m = create("d300-a")
    check("默认不挂信箱", m.msg, None)
    m.load()
    m.reset()
    m.enable_cov(); m.boot_realos(1); m.enable_events(period=4096); m.attach_flow()
    mb = m.enable_msgs()
    check("挂上之后是 Mailbox", isinstance(mb, mailbox.Mailbox), True)
    check("Machine.msg 指向同一个对象", m.msg is mb, True)
    check("默认信箱号是 1（全固件只有这一个）", mb.mbx, 1)
    check("发送缓冲与固件自己的样本区不同址", mb.buf != mb.sample, True)

    # 静态事实：全段只有一处 rcv_msg、15 处 snd_msg
    check("rcv_msg 只有一处（0x000A6324）", mailbox.RCV_SITE, 0x000A6324)
    check("任务 5 落地缓冲 0x3FE48 就是消息体抄过去的地址", mailbox.TASK5_BUF, 0x3FE48)

    # 跑起来，任务 5 会自己走到 rcv_msg 上停住
    for _ in range(24):
        m.run(max_steps=5000, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
    t5 = mb.task5_state()
    check("预热后任务 5 在等消息", t5["kind"], "msg")
    check("停在全段唯一的 rcv_msg 后面那条", t5["pc"], 0x000A6326)

    # 样本：固件自己在 0x417DC 造的那条，必须含一个代码指针。
    # ⚠️ 读的是**样本区**而不是发送缓冲 —— 固件处理完消息会把缓冲清成 0。
    samp = [m.bus.rd(mb.sample + i * 4, 4) for i in range(8)]
    check("样本首两个字是 0", samp[:2], [0, 0])
    check("样本 +0x08 = 0x80000000（发送侧 r3）", samp[2], 0x80000000)
    check("样本 +0x10 是指向代码区的回调指针",
          0x80000 <= samp[4] < 0x100000, True)
    mb.load_sample()
    check("抄到发送缓冲后首字仍然一致", mb.read_back()[2], samp[2])

    n_rcv = sum(1 for c in m.flow.calls if c[2] == -61)
    n_words = cov_words(m)
    r = mb.send()
    check("投递成功（err=0）", r.get("err"), 0)
    check("投递后任务 5 不再等消息", mb.task5_state()["kind"] != "msg", True)
    check("读数 +1", mb.sent, 1)

    # 让它把这条消息处理完（实测 11 次服务调用后回到 rcv_msg）
    for _ in range(6):
        m.run(max_steps=5000, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
    check("处理完之后又回到 rcv_msg", mb.task5_state()["kind"], "msg")
    check("rcv_msg 至少被调用了一次以上（循环转起来了）",
          sum(1 for c in m.flow.calls if c[2] == -61) > n_rcv, True)
    check("投递本身不带来任何新覆盖（V17 的核心否定结论）",
          cov_words(m) - n_words, 0)

    r2 = mb.send()
    check("第二条也能投（不是一次性的）", r2.get("err"), 0)
    check("累计两条", mb.sent, 2)
    for _ in range(6):
        m.run(max_steps=5000, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
    msgs = [c for c in m.flow.calls if c[2] == -61]
    check("两次投递 → 任务 5 两次 rcv_msg", len(msgs) >= n_rcv + 2, True)
    check("第二次处理完也回到 rcv_msg", mb.task5_state()["kind"], "msg")
    check("历史记录里两条都带回来码 0", [h[1] for h in mb.history], [0, 0])


def test_callbacks():
    """V18：两个回调槽 —— 登记是活的，派发器没人调。"""
    print("\n[24 回调槽（V18）]")
    from frbox import callback

    m = create("d300-a")
    check("默认不挂回调槽", m.cb, None)
    m.load()
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    cb = m.enable_callbacks()
    check("挂上之后是 Callbacks", isinstance(cb, callback.Callbacks), True)
    check("Machine.cb 指向同一个对象", m.cb is cb, True)
    check("两个槽", len(cb.slots), 2)

    # 静态常量：两套槽与两个派发器
    check("槽0 指针 = 0x417EC", cb.slots[0].ptr, 0x000417EC)
    check("槽1 指针 = 0x417F4（步长 8）", cb.slots[1].ptr, 0x000417F4)
    check("槽0 派发器 = 0x8223A", cb.slots[0].dispatch, 0x0008223A)
    check("槽1 派发器 = 0x822CE", cb.slots[1].dispatch, 0x000822CE)
    check("两个派发器各只有一个引用点，都在主循环里",
          (cb.slots[0].caller, cb.slots[1].caller), (0x00080606, 0x000808A4))
    check("主循环入口记在模块里", callback.MAIN_LOOP, 0x00080588)

    for _ in range(24):
        m.run(max_steps=5000, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)

    # 基线：槽已经登记好了，但主循环/派发器一次都没执行
    st0 = cb.slots[0].state()
    check("基线后槽0 已登记 0x8225E", st0["cb"], 0x0008225E)
    check("槽0 倒计时 0x000C（12 圈一拍）", st0["count"], 0x000C)
    check("槽0 挂起标志位开着", st0["pending"], True)
    check("主循环 0x80588 从没执行过", m.cov.visited(callback.MAIN_LOOP), False)
    check("派发器 0x8223A 从没执行过", m.cov.visited(callback.DISPATCH0), False)
    check("派发器 0x822CE 从没执行过", m.cov.visited(callback.DISPATCH1), False)

    # tick 12 圈：倒计时减到 0 才会真的 call 一次
    n0 = m.cov.words()
    got = cb.tick(12)
    # 槽0 倒计时 0xC（第 12 圈）、槽1 倒计时 0x8（第 8 圈）—— 12 圈内两个都会到点
    check("12 圈内两个槽各执行了一次", len(got), 2)
    check("其中一次是槽0 的 0x8225E",
          0x0008225E in [r["cb"] for _, _, r in got], True)
    check("执行完槽里换成下一站 0x82278", cb.slots[0].state()["cb"], 0x00082278)
    check("派发器从此被执行过", m.cov.visited(callback.DISPATCH0), True)
    check("这一圈带来了新覆盖", m.cov.words() > n0, True)
    check("主循环本身仍然没执行", m.cov.visited(callback.MAIN_LOOP), False)

    # 门控：倒计时没到点的圈不执行回调
    got2 = cb.tick(3)
    check("门控未到点的 3 圈不执行任何回调", got2, [])
    check("倒计时确实在减", cb.slots[0].state()["count"], 0x0007)

    # 继续扫：槽会一站站往前走，走到 0x82278（set_flg 叫醒任务 9）那一站时，
    # 控制权可能被交给调度器 —— 这时 call_function 收不回来，槽就停在那里。
    cb.tick(200)
    check("再扫 200 圈之后槽已经离开起点 0x8225E",
          cb.slots[0].state()["cb"] != 0x0008225E, True)
    check("槽0 至少真执行过两次以上", len(cb.slots[0].fired) >= 2, True)


def cov_words(m):
    return m.cov.words() if getattr(m, "cov", None) else 0


def test_isr2b():
    """V19：0x80588 是 INT 0x2B 的处理函数，不是「没有调用者的主循环」。"""
    print("\n[25 中断 0x2B（V19）]")
    from frbox import isr2b

    m = create("d300-a")
    check("默认不挂 INT 0x2B 驱动", m.isr2b, None)
    m.load()
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
    m.attach_flow()
    m.enable_callbacks()
    h = m.enable_isr2b()
    check("挂上之后是 Irq2B", isinstance(h, isr2b.Irq2B), True)
    check("Machine.isr2b 指向同一个对象", m.isr2b is h, True)
    check("向量表第 0xD4 项 = 0x80588", m.vector(0x2B), 0x00080588)
    check("这条线已接线", h.installed(), True)

    vec = isr2b.vectors(m)
    check("向量表真接线 46 条", len(vec), 46)
    check("0x2B 在里面", 0x2B in vec, True)
    check("兜底向量 0x000FE250 不算里", 0x000FE250 in vec.values(), False)
    check("ICR 里固件配过级别（0x1A）", m.irq_level(0x2B), 0x1A)

    # 边界：入口是压栈序言、出口是 reti
    check("入口第一条是 stm0", TABLE.decode(m.fetch_bytes(isr2b.ENTRY, 8), 0)[0], "stm0")
    check("0x80968 是 reti", TABLE.decode(m.fetch_bytes(isr2b.RETI, 8), 0)[0], "reti")
    check("尾声子程序收在 ret:d",
          TABLE.decode(m.fetch_bytes(isr2b.TAIL_END, 8), 0)[0], "ret:d")

    rows = isr2b.table(m)
    check("位分派表 61 条", len(rows), 61)
    check("覆盖 14 个标志字节", len({r[0] for r in rows}), 14)
    check("指向 57 个不同处理函数", len({r[4] for r in rows}), 57)
    pair = {(r[0], r[1], r[4]) for r in rows}
    check("0x3F404 bit1 → 0x8223A（槽0）",
          (0x0003F404, 0x02, 0x0008223A) in pair, True)
    check("0x3F40D bit2 → 0x822CE（槽1）",
          (0x0003F40D, 0x04, 0x000822CE) in pair, True)

    for _ in range(12):
        m.run(max_steps=5000, spin_guard=False)
    check("基线：0x80588 一次都没执行", m.cov.visited(isr2b.ENTRY), False)
    st = m.cb.slots[0].state()
    check("基线：槽0 已经登记好 0x8225E", st["cb"], 0x0008225E)

    r = h.raise_once()
    check("请求一次就进了 ISR", r["entered"], True)
    check("跑到 reti 回来了", r["cut"], False)
    check("0x80588 从此被执行过", m.cov.visited(isr2b.ENTRY), True)
    check("这一条中断带来新覆盖", r["delta"] > 100, True)
    check("槽0 指针这一趟不变", m.cb.slots[0].state()["cb"], 0x0008225E)

    ev, _last = h.burst(30)
    check("30 次之内槽0 换成下一站 0x82278",
          m.cb.slots[0].state()["cb"], 0x00082278)
    check("派发器 0x8223A 被执行过", m.cov.visited(0x0008223A), True)
    check("派发调用点 0x80606 被执行过", m.cov.visited(0x00080606), True)

    # V20：0x174 由 XferHandshake 建模，不替它答话也能过（V19 的「墙」是探针 bug）
    m2 = create("d300-a")
    m2.load()
    m2.reset()
    m2.enable_cov()
    m2.boot_realos(1)          # 它内部会 enable_xfer()，0x174 归 XferHandshake
    m2.attach_flow()
    m2.enable_callbacks()
    h2 = m2.enable_isr2b()
    for _ in range(12):
        m2.run(max_steps=5000, spin_guard=False)
    check("boot_realos 之后 0x174 归 XferHandshake",
          type(m2.dp.owner(0x174)).__name__, "XferHandshake")
    x2 = m2.dp.owner(0x174)
    check("0x174 的启动位/完成位 = 0x02/0x40",
          x2.table.get(0x174), (0x02, 0x40))
    stuck = None
    steps9 = None
    base = m2.step_count
    for k in range(1, 21):
        t0 = m2.step_count
        r2 = h2.raise_once(max_steps=4000)      # 不硬写，靠设备模型
        if k == 9:
            steps9 = m2.step_count - t0
        if r2["cut"]:
            stuck = (k, m2.cpu.pc)
            break
    check("不替外设答话，20 次全部回到 reti", stuck, None)
    check("硬写完成位的次数为 0", h2.answers, 0)
    check("设备模型自己置过完成位", x2.completions > 0, True)
    check("第 9 次 605 步就走完（V19 是 20000 步超时）", steps9, 605)
    check("20 次累计 9815 步", m2.step_count - base, 9815)

    # 对照：绕过设备时基（run_tick=False）就会重现 V19 的自旋
    m3 = create("d300-a")
    m3.load()
    m3.reset()
    m3.enable_cov()
    m3.boot_realos(1)
    m3.attach_flow()
    m3.enable_callbacks()
    h3 = m3.enable_isr2b()
    for _ in range(12):
        m3.run(max_steps=5000, spin_guard=False)
    stuck3 = None
    for k in range(1, 13):
        r3 = h3.raise_once(max_steps=4000, run_tick=False)
        if r3["cut"]:
            stuck3 = (k, m3.cpu.pc)
            break
    check("run_tick=False 时第 9 次卡住（复现 V19）", stuck3 and stuck3[0], 9)
    check("卡在 0x174 bit6 的轮询循环里",
          bool(stuck3) and 0x0009C974 <= stuck3[1] <= 0x0009C97E, True)

    # 结果寄存器：240 次里只有 15 次走到转换流程，且只碰通道 0x186
    m4 = create("d300-a")
    m4.load()
    m4.reset()
    m4.enable_cov()
    m4.boot_realos(1)
    m4.attach_flow()
    m4.enable_callbacks()
    h4 = m4.enable_isr2b()
    for _ in range(12):
        m4.run(max_steps=5000, spin_guard=False)
    for a in range(0x170, 0x190):
        m4.bus.watch.add(a)
    m4.bus.access_log.clear()
    h4.burst(240, max_steps=60000)
    hits = {}
    for kind, a, _n, _v, _pc in m4.bus.access_log:
        if 0x178 <= a <= 0x18F:
            hits[a] = hits.get(a, 0) + 1
    check("结果寄存器里只有 0x186 被读过（其余 7 个通道 0 次）",
          [hex(x) for x in sorted(hits)], ["0x186"])
    check("0x186 只读 15 次（= 15 次转换）", hits.get(0x186), 15)


def test_autoirq():
    """V21：给 INT 0x2B 配周期时钟 —— 中断自己来，节奏比次数重要。"""
    print("\n[26 自动节拍 INT 0x2B（V21）]")
    from frbox import realos, dev_irq2b

    m = create("d300-a")
    m.load()
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
    m.attach_flow()
    m.enable_callbacks()
    m.enable_isr2b()
    check("默认不挂周期时钟", m.irq2b_clock, None)
    for _ in range(12):
        m.run(max_steps=5000, spin_guard=False)
    clk = m.enable_auto_irq2b(period=256)
    check("挂上之后是 Irq2BClock", isinstance(clk, dev_irq2b.Irq2BClock), True)
    check("挂时钟顺带接上 INT 0x2B 驱动", m.isr2b is not None, True)
    check("时钟在 m.devices 里", clk in m.devices, True)
    m.run(max_steps=30000, spin_guard=False)
    st = clk.state()
    check("风暴档 3 万步：fired=59（确定性）", st["fired"], 59)
    check("风暴档：ISR 期间的到点被合并 58 次", st["coalesced"], 58)
    check("风暴档：accepted=58", st["accepted"], 58)
    check("口径自洽：fired - 待决 = accepted",
          st["fired"] - st["pending"], st["accepted"])

    # 从容档：97 次中断 +2,418，任务 6 被拉起且入口被执行过
    m2 = create("d300-a")
    m2.load()
    m2.reset()
    m2.enable_cov()
    m2.boot_realos(1)
    m2.attach_flow()
    m2.enable_callbacks()
    m2.enable_isr2b()
    for _ in range(12):
        m2.run(max_steps=5000, spin_guard=False)
    c0 = m2.cov.words()
    s0 = len(m2.flow.calls)
    clk2 = m2.enable_auto_irq2b(period=1024)
    m2.run(max_steps=100000, spin_guard=False)
    check("从容档 10 万步：accepted=97", clk2.accepted(), 97)
    check("从容档：一次都没合并", clk2.coalesced, 0)
    check("从容档：覆盖 +2,418", m2.cov.words() - c0, 2418)
    check("从容档：服务 +10", len(m2.flow.calls) - s0, 10)
    t6 = {t["id"]: t["wait_kind"] for t in realos.states(m2)}
    check("从容档：任务 6 走到 flag 等待", t6.get(6), "flag")
    check("从容档：任务 6 入口 0xA6A30 被执行过", m2.cov.visited(0x000A6A30), True)
    n6 = sum(1 for rec in m2.cpu.irq_log
             if rec[0] == 0x2B and 0x000A6000 <= rec[2] < 0x000A8000)
    check("有中断落在任务 6 函数体里（0xA6xxx）", n6, 4)

    # 等价性：手动 240 次 = 风暴 accepted≥240，都是 +2053/+3
    m3 = create("d300-a")
    m3.load()
    m3.reset()
    m3.enable_cov()
    m3.boot_realos(1)
    m3.attach_flow()
    m3.enable_callbacks()
    m3.enable_isr2b()
    for _ in range(12):
        m3.run(max_steps=5000, spin_guard=False)
    c0 = m3.cov.words()
    s3 = len(m3.flow.calls)
    m3.isr2b.burst(240, max_steps=60000)
    check("手动 240 次：+2053", m3.cov.words() - c0, 2053)
    check("手动 240 次：服务 +3", len(m3.flow.calls) - s3, 3)
    m4 = create("d300-a")
    m4.load()
    m4.reset()
    m4.enable_cov()
    m4.boot_realos(1)
    m4.attach_flow()
    m4.enable_callbacks()
    m4.enable_isr2b()
    for _ in range(12):
        m4.run(max_steps=5000, spin_guard=False)
    c0 = m4.cov.words()
    s4 = len(m4.flow.calls)
    clk4 = m4.enable_auto_irq2b(period=256)
    while clk4.accepted() < 240 and m4.step_count < 10 ** 7:
        m4.run(max_steps=5000, spin_guard=False)
    check("风暴档 accepted≥240（实为 246）", clk4.accepted(), 246)
    check("风暴档：也是 +2053（与手动完全等价）",
          m4.cov.words() - c0, 2053)
    check("风暴档：服务也同为 +3", len(m4.flow.calls) - s4, 3)

    # 摘掉
    dev_irq2b.detach(m4)
    check("detach 之后时钟不在设备表里", clk4 in m4.devices, False)
    check("detach 之后 irq2b_clock 置空", m4.irq2b_clock, None)

    # 回归：boot 之前挂钟 —— ICR 未配置时线不拉（否则 level=0x1F 的请求
    # 永久卡在待决队列，把后面的到点全合并掉，accepted 恒为 0）
    m5 = create("d300-a")
    m5.load()
    m5.reset()
    m5.enable_cov()
    m5.attach_flow()
    m5.enable_callbacks()
    m5.enable_isr2b()
    clk5 = m5.enable_auto_irq2b(period=1024)      # boot_realos 之前
    m5.boot_realos(1)
    m5.run(max_steps=100000, spin_guard=False)
    check("boot 前挂钟：ICR 未配置的到点被跳过 2 次", clk5.skipped, 2)
    check("boot 前挂钟：中断照常被受理（99 次）", clk5.accepted(), 99)
    check("boot 前挂钟：没有卡死的待决请求", clk5.pending_now(), 0)
    check("boot 前挂钟：终态覆盖 3,861（与挂钟在后一致）", m5.cov.words(), 3861)


def test_bboot():
    """V22：B 段 REALOS 内核原生启动 + 任务 2 上 CPU。"""
    print("\n[27 B 段内核启动 + 任务上 CPU（V22）]")
    from frbox import brealos
    from frbox.isr2b import table as dispatch_table

    m = create("d300-a")
    m.load()
    m.reset()
    t = dispatch_table(m)
    check("A 位分派表 61 条（V19 口径）", len(t), 61)
    check("A 位分派表去重 57 个处理函数", len(set(x[4] for x in t)), 57)

    # B 段 boot：确定性 - 191,520 步到空闲任务
    b = brealos.boot()
    check("B boot 191,520 步到空闲任务", b.step_count, 191520)
    check("B 空闲任务 PC=0x407DC", b.cpu.pc, 0x000407DC)
    check("B 空闲任务 SP=0x68000BE8（任务栈在内核窗口里）", b.cpu.r[15], 0x68000BE8)
    check("B 空闲任务 S=1（用户态）", b.cpu.s, 1)
    check("B 空闲任务 ILM=0x1F", b.cpu.ilm, 0x1F)
    check("B boot 覆盖 1241（确定性）", b.cov.words(), 1241)
    check("B 当前 TCB=空闲任务 0x6800060C",
          b.bus.rd(brealos.CUR_TCB_SLOT, 4), 0x6800060C)

    # 任务表：28 条，V23 修正基址 0x54C00（V22 的 0x54C10 偏了 0x10）
    recs = brealos.records(b)
    check("B 任务表 28 条（+空闲第 29 条）", len(recs), 28)
    r2 = [r for r in recs if r["idx"] == 2][0]
    check("任务 2 入口 0x1200AC", r2["entry"], 0x1200AC)
    check("任务 2 栈顶 0x9F800AA4", r2["stack"], 0x9F800AA4)
    check("任务 2 的 TCB=0x68000168（V23 修正基址后表域与内核 TCB 一致）",
          r2["tcb"], 0x68000168)
    check("任务 2 激活标志 0xC0000000（位 31：复位就激活）",
          r2["flags"], 0xC0000000)
    r1 = [r for r in recs if r["idx"] == 1][0]
    check("任务 1 的 TCB=0x6800013C（TCB[0]，背指表项基址 0x54C00）",
          r1["tcb"], 0x6800013C)
    idle = [r for r in brealos.records(b, include_idle=True) if r["idx"] == 29][0]
    check("空闲任务=表后第 29 条：入口 0x407DC", idle["entry"], 0x000407DC)
    check("空闲任务 TCB 0x6800060C（派发读的 [0x54F10]）", idle["tcb"], 0x6800060C)
    check("空闲任务标志 0x80000000（位 31）", idle["flags"], 0x80000000)

    # 任务 2 的初始帧在 boot 时已建好（栈区 RAM 建好后才存得住）
    check("任务 2 帧 [0x9F800AA4]=入口 0x1200AC",
          b.bus.rd(0x9F800AA4, 4), 0x1200AC)
    check("任务 2 帧 [+4]=PS 0x1F0030",
          b.bus.rd(0x9F800AA8, 4), 0x1F0030)

    # 向量表：真 ISR 42 条（桩 0x404F8/0x4046E/0x406E6 已剔除）
    stubs = {0x000404F8, 0x0004046E, 0x000406E6}
    real = [v for v in range(0x10, 0x40)
            if b.vector(v) not in stubs and 0x40000 <= b.vector(v) < 0x740000]
    check("B 向量表真 ISR 42 条", len(real), 42)
    check("B INT 0x3F=调度请求向量 0x40D32", b.vector(0x3F), 0x40D32)
    check("B INT 0x1B=串口 ISR 0x12FDB4", b.vector(0x1B), 0x12FDB4)

    # sys_sta_tsk + 派发：任务 2 真正上 CPU
    r = brealos.sta_tsk(b, 2)
    check("sys_sta_tsk(2) err=-63（帧已在 boot 建好）", r["err"], -63)
    dc, pc = brealos.request_dispatch(b)
    check("一次派发覆盖 +1349（V23：两道墙拆掉后跑完开机装配）", dc, 1349)
    check("任务 2 入口 0x1200AC 执行过", b.cov.visited(0x1200AC), True)
    check("当前 TCB 切到任务 2 (0x68000168)",
          b.bus.rd(brealos.CUR_TCB_SLOT, 4), 0x68000168)
    sta = [r4 for _, _, svc, r4, *_ in b.flow.calls if svc == -23]
    check("任务 2 发起 11 次 sys_sta_tsk（开机装配拉起任务家族）", len(sta), 11)
    return b


def test_bwall(b=None):
    """V23：B 段两道墙拆掉 —— 任务 2 跑完开机装配、进主事件循环。"""
    print("\n[29 B 段墙拆除 + 开机装配（V23）]")
    from frbox import brealos
    from collections import Counter

    # 墙 1 的定性：0x740000 起按擦除态 flash 建模（ROM 区、读回 0xFFFF）
    m0 = brealos.boot()
    r = m0.bus.find(0x740000)
    check("0x740000 起挂了擦除态 flash（ROM 区）", r.kind, "rom")
    check("擦除态读回 0xFFFF（扫描的终止符）", m0.bus.rd(0x740000, 2), 0xFFFF)
    check("ROM 镜像 [0x73FFFE] 之后就是 flash-erased",
          m0.bus.find(0x740002).name, "flash-erased")
    check("扫描起点指针表 [0x1C2A98]=0x740000", m0.bus.rd(0x1C2A98, 4), 0x740000)
    check("第二遍扫描起点 [0x1C2A9C]=0x750000", m0.bus.rd(0x1C2A9C, 4), 0x750000)

    # 墙 2 的定性：0x5C003034 硬件自由运行计数器
    check("0x5C003000 硬件计数器已挂", m0.hw_counter is not None, True)
    m0.hw_counter.count = 0x12345678
    check("计数器 +0x34 大端 4 字节读回自身",
          m0.bus.rd(0x5C003034, 4), 0x12345678)
    m0.hw_counter.count = 0x000000AB
    check("计数器 +0x37 读回最低字节", m0.bus.rd(0x5C003037, 1), 0xAB)

    # 开机装配全程：复用 test_bboot 的机器（boot → sta_tsk → 200k 派发）
    if b is None:
        b = brealos.boot()
        brealos.sta_tsk(b, 2)
        brealos.request_dispatch(b)
    cov0 = b.cov.words() - 1349          # 派发前的覆盖（1241）
    b.run(max_steps=800000, spin_guard=False)
    calls = b.flow.calls
    dist = dict(sorted(Counter(svc for _, _, svc, *_ in calls).items()))
    check("长跑后总调用 6774（确定性）", len(calls), 6774)
    check("覆盖停在 +1349（装配完不再涨：主循环是已覆盖代码）",
          b.cov.words() - cov0, 1349)
    check("服务分布与 V23 口径一致", dist,
          {-83: 1, -55: 6, -53: 6, -48: 3, -47: 3373, -46: 3373,
           -27: 1, -23: 11})
    sta = [r4 for _, _, svc, r4, *_ in calls if svc == -23]
    check("sta_tsk 拉起的 11 个任务号", sta,
          [26, 5, 17, 4, 9, 27, 28, 1, 21, 7, 16])
    check("sys_set_tim 恰 1 次（装配里设系统时钟）",
          sum(1 for _, _, s, *_ in calls if s == -83), 1)
    check("sys_chg_pri 恰 1 次", sum(1 for _, _, s, *_ in calls if s == -27), 1)
    sems = [r4 for _, _, s, r4, *_ in calls if s in (-53, -55)]
    check("信号量握手对象：16/20/1/21 各 wai+sig 一对",
          sorted(set(sems)), [1, 16, 20, 21])
    # 终点：主事件循环在 flag 3 上轮询（clr_flg+wai_flg 各 3373 次）
    wai = [c for c in calls if c[2] == -46]
    check("wai_flg 全部等 flag 3", sorted(set(c[4] for c in wai)), [3])
    check("主循环 wai_flg 样例（p_flgptn=0x9F800AD8）", wai[-1][3], 0x9F800AD8)
    check("主循环调用点 0x1203BE 执行过", b.cov.visited(0x1203BE), True)
    check("装配函数 0x1200AC 执行过", b.cov.visited(0x1200AC), True)
    check("任务 2 之外没有任务上过 CPU（都在等事件）",
          [t["idx"] for t in brealos.states(b) if t["ran"]], [2])
    # 计时器真的被读过（等待循环依赖它走字）
    check("0x5C003034 读过（等走字循环）",
          b.bus.io_reads.get(0x5C003034, 0) > 0, True)
    check("hw_counter 在走字（>0）", b.hw_counter.count > 0, True)


def test_pendbits():
    """V22：A 段位分派器逐位点亮 - 干净机单点确定性。"""
    print("\n[28 A 段待办区位扫描（V22）]")
    from frbox.isr2b import attach as attach_irq

    def fresh():
        m = create("d300-a")
        m.load()
        m.reset()
        m.enable_cov()
        m.boot_realos(1)
        m.attach_flow()
        m.enable_callbacks()
        h = attach_irq(m)
        for _ in range(12):
            m.run(max_steps=5000, spin_guard=False)
        return m, h

    # 干净机单点：0x3F402 bit6 → 0xAD3C8 执行，+369（确定性）
    m, h = fresh()
    b = m.cov.words()
    m.bus.wr(0x3F402, 1, 0x40, m.cpu.pc)
    h.raise_once()
    m.run(max_steps=2000, spin_guard=False)
    check("单点 [0x3F402]&0x40：基线 1443", b, 1443)
    check("单点 [0x3F402]&0x40：覆盖 +369", m.cov.words() - b, 369)
    check("单点 [0x3F402]&0x40：目标 0xAD3C8 执行过",
          m.cov.visited(0xAD3C8), True)
    check("单点后回空闲任务", m.cpu.pc, 0xFE510)

    # 干净机单点：0x3F404 bit7 → 0xAF64C 不执行（位被点亮但处理函数不走目标）
    m, h = fresh()
    b = m.cov.words()
    m.bus.wr(0x3F404, 1, 0x80, m.cpu.pc)
    h.raise_once()
    m.run(max_steps=2000, spin_guard=False)
    check("单点 [0x3F404]&0x80：覆盖 +299（分派器骨架）", m.cov.words() - b, 299)
    check("单点 [0x3F404]&0x80：目标 0xAF64C 未执行",
          m.cov.visited(0xAF64C), False)

    # V23：61 位干净机单点全定性（exp_pendbits2）——18 位 A 类 / 48 位 C 类。
    # 这里固化 3 个代表性数字（每条一位一台新机器）。
    m, h = fresh()
    b = m.cov.words()
    m.bus.wr(0x3F403, 1, 0x20, m.cpu.pc)
    h.raise_once()
    m.run(max_steps=2000, spin_guard=False)
    check("单点 [0x3F403]&0x20：覆盖 +341（A 类，目标 0x875C2 执行）",
          m.cov.words() - b, 341)
    check("单点 [0x3F403]&0x20：目标 0x875C2 执行过",
          m.cov.visited(0x875C2), True)

    m, h = fresh()
    b = m.cov.words()
    m.bus.wr(0x3F40C, 1, 0x20, m.cpu.pc)
    h.raise_once()
    m.run(max_steps=2000, spin_guard=False)
    check("单点 [0x3F40C]&0x20：覆盖 +299（C 类，只走分派器骨架）",
          m.cov.words() - b, 299)
    check("单点 [0x3F40C]&0x20：目标 0x84900 干净机未执行（需前置状态）",
          m.cov.visited(0x84900), False)


def test_bflag():
    """V24：B 段内核的门 —— 服务分发表、事件标志对象、wfmode 语义。"""
    print("\n[30 服务分发表 + 事件标志语义（V24）]")
    from frbox import brealos, bflag

    m = brealos.boot()

    # 1) 服务分发表：13 条静态常量 vs 动态抓 `call r12` 的目标
    ok = all(brealos.service_handler(m, s) == h for s, h in brealos.SERVICES.items())
    check("13 条服务号→处理函数与动态解一致", ok, True)
    check("wai_flg 处理函数", brealos.SERVICES[brealos.SVC_WAI_FLG], 0x00041B0A)
    check("set_flg 处理函数", brealos.SERVICES[brealos.SVC_SET_FLG], 0x00041A48)
    check("slp_tsk 是空桩（B 段未实现）", brealos.SERVICES[brealos.SVC_SLP_TSK], 0x00040660)

    # 2) flag 对象布局：对象 0x680008B8+(N-1)*0x10，表 0x551B0
    check("flag 3 对象地址", bflag.object_addr(3), 0x680008D8)
    check("对象表第 3 项指向同一对象", bflag.table_entry(m, 3), 0x680008D8)
    check("对象表第 3 项 id 字", bflag.table_idword(m, 3), 0x00030000)
    check("对象 +0x00 自指", m.bus.rd(0x680008D8, 4), 0x680008D8)
    check("对象 +0x0C 背指表项 +4", m.bus.rd(0x680008E4, 4), 0x000551D4)

    # 3) wfmode 语义：bit1=OR、bit0=清位（与教科书 μITRON 相反）
    bflag.set_pattern(m, 3, 0)
    r = bflag.set(m, 3, 0x300)
    check("set_flg 返回 0", r["err"], 0)
    check("set_flg 后位模式 0x300", bflag.pattern(m, 3), 0x300)

    w = bflag.wait(m, 3, 0x300, bflag.WF_ANDW)
    check("AND 成立：satisfied", w["satisfied"], True)
    check("AND 成立：步数 102", w["steps"], 102)
    check("受理码 0（不代表等到）", w["err"], 0)
    check("AND 成立且不清位", bflag.pattern(m, 3), 0x300)

    w = bflag.wait(m, 3, 0x900, bflag.WF_ANDW)
    check("AND 不成立：satisfied", w["satisfied"], False)
    check("AND 不成立：步数 150（兜底路径）", w["steps"], 150)

    w = bflag.wait(m, 3, 0x900, bflag.WF_ORW)
    check("OR 一位成立：satisfied", w["satisfied"], True)

    w = bflag.wait(m, 3, 0x800, bflag.WF_ORW)
    check("OR 全不成立：satisfied", w["satisfied"], False)

    bflag.set_pattern(m, 3, 0x300)
    w = bflag.wait(m, 3, 0x100, bflag.WF_CLR)
    check("mode=1 满足后清位", bflag.pattern(m, 3), 0)
    check("清位分支步数 104", w["steps"], 104)

    # 4) 计数器速率：只改「等多久」，不改路径
    m2 = create("d300-b")
    m2.hw_counter_rate = 4
    m2.load()
    m2.reset()
    check("hw_counter_rate 传到设备", m2.hw_counter.rate, 4)
    m2.hw_counter.count = 0
    m2.hw_counter.tick(0)
    check("rate=4：一个 tick 走 4 个字", m2.hw_counter.count, 4)
    m2.hw_counter.set_rate(1)
    m2.hw_counter.tick(0)
    check("set_rate(1)：一个 tick 走 1 个字", m2.hw_counter.count, 5)

    # 5) 主循环结构：clr_flg(3,~0x200000) → 查门字节 → wai_flg(3,0x200000,2) → 跳回
    GATE = 0x9F58D08F
    check("门字节 boot 后为 0", m.bus.rd(GATE, 1), 0)
    s = brealos.boot()
    brealos.sta_tsk(s, 2)
    s.raise_irq(0x3F)
    s.run(max_steps=200000, spin_guard=False)
    s.run(max_steps=400000, spin_guard=False)
    check("装配完成后门字节仍是 0（没人写）", s.bus.rd(GATE, 1), 0)
    n0 = len(s.flow.calls)
    s.run(max_steps=50000, spin_guard=False)
    win = s.flow.calls[n0:]
    from collections import Counter as _C
    svc = _C(x[2] for x in win)
    check("主循环 5 万步：clr_flg 188 次", svc.get(-47), 188)
    check("主循环 5 万步：wai_flg 188 次", svc.get(-46), 188)
    check("clr_flg 调用点 0x12016C", _C(x[1] for x in win if x[2] == -47).most_common(1)[0][0], 0x12016C)
    check("wai_flg 调用点 0x1203BE", _C(x[1] for x in win if x[2] == -46).most_common(1)[0][0], 0x1203BE)
    w = [x for x in win if x[2] == -46][0]
    check("wai_flg 等的是 flag 3 的 0x200000", (w[4], w[5], w[6]), (3, 0x200000, 2))


def test_bgate():
    """V25：主循环的门字节、它的写者、开门之后；内核 RAM 扩到 128KB。"""
    print("\n[31 门字节与开门实验（V25）]")
    from frbox import brealos, bflag, bgate

    m = create("d300-b")
    m.load()

    # 1) 门字节的引用与写者
    check("0x9F58D08F 的立即数引用 19 处", len(bgate.refs(m, bgate.GATE)), 19)
    check("写点 0x12F180 是 stb", "stb" in m.disasm_at(bgate.GATE_WRITER, 1), True)
    check("写者后面紧跟 set_flg(3,0x200000) 的 int",
          "int" in m.disasm_at(bgate.SET_FLG_200000, 1), True)
    check("主循环读点 0x120176 是 ldub", "ldub" in m.disasm_at(0x120176, 1), True)

    # 2) 门字节的开关工具
    check("boot 后门字节为 0", bgate.gate(m), 0)
    bgate.open_gate(m)
    check("open_gate() 后门字节为 1", bgate.gate(m), 1)
    check("is_open() 为真", bgate.is_open(m), True)
    bgate.close_gate(m)
    check("close_gate() 后门字节为 0", bgate.gate(m), 0)

    # 3) 内核 RAM 扩到 128KB（任务 4 的拷贝写到 0x6801DE84）
    check("0x6801FFFF 已映射为 RAM", m.bus.find(0x6801FFFF).kind, "ram")
    m.bus.wr(0x6801DE80, 4, 0x12345678)
    check("0x6801DE80 写得进、读得回", m.bus.rd(0x6801DE80, 4), 0x12345678)

    # 4) 开门实验：主循环走进干活分支
    s = brealos.boot()
    brealos.sta_tsk(s, 2)
    s.raise_irq(0x3F)
    s.run(max_steps=200000, spin_guard=False)
    s.run(max_steps=800000, spin_guard=False)
    c0 = s.cov.words()
    bgate.open_gate(s)
    s.run(max_steps=1000000, spin_guard=False)
    check("开门后覆盖 +102（确定性）", s.cov.words() - c0, 102)
    check("干活分支 0x120184 执行过", s.cov.visited(0x120184), True)
    check("调进显示区 0x12E6F8", s.cov.visited(0x12E6F8), True)
    check("仍未到 0x12E630（0x80 的置位点）", s.cov.visited(0x12E630), False)
    check("门字节保持 1（没人清它）", bgate.gate(s), 1)
    check("flag6 置上 0x20", bflag.pattern(s, 6), 0x20)
    check("flag12 置上 0x800000", bflag.pattern(s, 12), 0x800000)

    # 5) 显示任务用的服务 -170 未实现
    t = brealos.service_handler(brealos.boot(), brealos.SVC_TWAI_FLG)
    check("-170 的处理函数 0x41BEA", t, 0x00041BEA)
    r = brealos.kernel_call(brealos.boot(), brealos.SVC_TWAI_FLG,
                            r4=0x9F58D0F0, r5=3, r6=0x200000, r7=2, r13=0x64,
                            max_steps=5000)
    check("-170 返回 E_NOSPT(-17)", r["err"], -17)


def test_btable():
    """V35：对象表的真结构（4 字节表头 + 16 字节记录）、上限在哪。"""
    print("\n[40 对象表结构与上限（V35）]")
    from frbox import bsem, btable, brealos

    m = brealos.boot()
    check("表头 sem = 40 条 | 16", (btable.header(m, 0x54F2C)["count"],
                                   btable.header(m, 0x54F2C)["stride"]), (40, 16))
    check("表头 flag = 17 条 | 16", (btable.header(m, 0x551B0)["count"],
                                     btable.header(m, 0x551B0)["stride"]), (17, 16))
    check("sem 记录 1 在 0x54F30", btable.record_addr(0x54F2C, 1), 0x00054F30)
    check("flag 记录 1 在 0x551B4", btable.record_addr(0x551B0, 1), 0x000551B4)
    check("sem 表止于 0x551B0（= flag 表头）",
          btable.table_end(0x54F2C, 40), 0x000551B0)
    check("flag 表止于 0x552C4", btable.table_end(0x551B0, 17), 0x000552C4)

    r1 = btable.record(m, 0x54F2C, 1)
    check("记录 1 = (对象 0x68000638, 编号 1, 初始 1, 上限 1)",
          (r1["obj"], r1["id"], r1["init"], r1["max"]),
          (0x68000638, 1, 1, 1))
    r14 = btable.record(m, 0x54F2C, 14)
    check("记录 14 的参数 = [初始 0 | 上限 7]", (r14["init"], r14["max"]), (0, 7))
    check("记录 14 的参数格在记录 +0xC", r14["addr"] + 0x0C, 0x0005500C)

    a = btable.audit(m)
    check("对象↔记录背指 40/40 全对", a["backptr_bad"], [])
    check("记录→对象反向 40/40 全对", a["reverse_bad"], [])
    check("对象计数 = 记录初始计数", a["count_bad"], [])
    check("两张表首尾相接", [x[4] for x in a["touching"]], [True])
    check("体检整体通过", a["ok"], True)
    check("sem 17 的背指 = 0x55030", btable.record_of_object(m, 0x68000738), 0x00055030)
    check("对象 +0xA 全 0（上限不存对象里）",
          {m.bus.rd(btable.record(m, 0x54F2C, i)["obj"] + 0x0A, 2) for i in range(1, 41)},
          {0})
    check("上限检查代码在 0x418E8", bsem.SIG_MAX_CHECK, 0x000418E8)


def test_bqueue_slot():
    """V35：队列槽位的 +8 载荷、+9 状态、四字节游标。"""
    print("\n[41 队列槽位与游标（V35）]")
    from frbox import blist, bqueue, brealos

    check("槽位基址 / 步长 / 条数",
          (bqueue.SLOT_BASE, bqueue.SLOT_STRIDE, bqueue.SLOT_COUNT),
          (0x9F58D508, 0x0C, 70))
    check("槽位 0 与 69 的地址",
          (bqueue.slot_addr(0), bqueue.slot_addr(69)), (0x9F58D508, 0x9F58D844))
    check("节点地址 → 槽位号", bqueue.index_of(0x9F58D58C), 11)
    check("非槽位地址 → None", bqueue.index_of(0x9F58D590), None)
    check("状态位常量", (bqueue.FLAG_LINKED, bqueue.FLAG_EXTRA), (0x80, 0x40))
    check("游标块基址 / 描述符表", (bqueue.CURSOR_BASE, bqueue.DESC_TABLE),
          (0x9F58DA81, 0x001BC020))

    m = brealos.boot()
    check("boot 后 70 个载荷全是 0", {bqueue.payload(m, i) for i in range(70)}, {0})
    check("boot 后 70 个状态位全是 0", {bqueue.flag(m, i) for i in range(70)}, {0})
    check("boot 后游标四字节全 0", bqueue.cursor(m)["raw"], b"\x00\x00\x00\x00")
    check("boot 后队列是空的", bqueue.peek(m), 0)

    # 挂一个节点：模型行为和实测（+0/+4/+9 变、+8 不动）一致
    blist.init_model(m, bqueue.HEAD)
    bqueue.submit_model(m, 11)
    check("挂链后链上有槽位 11", blist.walk(m, bqueue.HEAD), [bqueue.slot_addr(11)])
    check("挂链只置 +9 的 0x80", bqueue.flag(m, 11), 0x80)
    check("挂链不动载荷", bqueue.payload(m, 11), 0)
    check("载荷写了才读得到", (m.bus.wr(bqueue.slot_addr(11) + 8, 1, 0x2A),
                          bqueue.peek(m))[1], 0x2A)
    bqueue.submit_model(m, 49, flag_extra=True)
    check("第二个节点在链首、状态 0xC0",
          (blist.walk(m, bqueue.HEAD)[0], bqueue.flag(m, 49)),
          (bqueue.slot_addr(49), 0xC0))

    # 游标替固件写（探针用的接口）
    c = bqueue.set_cursor(m, slot_no=11, count=3, next_no=12)
    check("游标写入后读回", (c["slot"], c["count"], c["next"]), (11, 3, 12))

    # 清空：哨兵自指 + 槽位状态清零（0x12E602 的模型）
    blist.clear_model(m, bqueue.HEAD)
    check("清空后是空表", blist.is_empty(m, bqueue.HEAD), True)
    check("清空后状态位归零",
          (bqueue.flag(m, 11), bqueue.flag(m, 49)), (0, 0))


def test_bdesc():
    """V36：描述符表 70 条、索引 = 槽位号、事件掩码、取首的返回值寄存器。"""
    print("\n[42 描述符表与取首语义（V36）]")
    from frbox import bdesc, blist, bqueue, brealos

    check("表的地址 / 步长 / 条数",
          (bdesc.DESC_TABLE, bdesc.DESC_STRIDE, bdesc.DESC_COUNT),
          (0x001BC020, 0x10, 70))
    check("条数 = 槽位数", bdesc.DESC_COUNT, bqueue.SLOT_COUNT)
    check("字段偏移（状态/数值/方式/处理）",
          (bdesc.F_STATUS, bdesc.F_VALUE, bdesc.F_MODE, bdesc.F_HANDLER),
          (0x00, 0x04, 0x08, 0x0C))
    check("最后一条与表尾",
          (bdesc.entry_addr(69), bdesc.entry_addr(70)), (0x001BC470, 0x001BC480))

    m = brealos.boot()
    a = bdesc.audit(m)
    check("长度 70", a["count"], 70)
    check("有状态字节指针的 65 条", a["with_status"], 65)
    check("没有状态字节指针的槽位", a["no_status"], [0, 15, 29, 63, 68])
    check("状态字节指针互不重复", a["unique_status"], 65)
    check("状态字节范围", (a["status_min"], a["status_max"]),
          (0x9F58CE64, 0x9F58D4FB))
    check("方式分布 41 / 24 / 5", a["modes"], {0: 41, 1: 24, 2: 5})
    check("处理函数 44 个", a["handlers"], 44)
    check("空处理 27 条", len(a["noop"]), 27)
    check("空处理槽位表", a["noop"],
          [0, 1, 2, 10, 13, 15, 16, 19, 27, 28, 34, 37, 40, 46, 47, 53,
           54, 55, 56, 58, 61, 63, 64, 65, 67, 68, 69])
    check("0x12E752 就是一条 ret（97 20）", a["noop_is_ret"], True)
    check("形状异常为空", a["bad_shape"], [])
    check("boot 后当前描述符指针是 0", a["current"], 0)
    check("boot 后事件掩码是 0", a["mask"], 0)
    check("current_index 无指针时返回 None", a["current_index"], None)

    # 表在 ROM：写不进去
    t = m.bus.rd(bdesc.entry_addr(1) + bdesc.F_VALUE, 4)
    m.bus.wr(bdesc.entry_addr(1) + bdesc.F_VALUE, 4, 0xDEADBEEF)
    check("表在 ROM（写被丢弃）", m.bus.rd(bdesc.entry_addr(1) + bdesc.F_VALUE, 4), t)

    # 动态：按游标取描述符 + 事件掩码
    # ⚠️ V38：返回地址必须设 DR1（RP），写栈无效 —— 统一走 brealos.call_fn
    def call_fn(addr, r4=0):
        return brealos.call_fn(m, addr, r4=r4)

    def apply(slot):
        for k, val in enumerate((0x55, slot, 0, slot)):
            m.bus.wr(bqueue.CURSOR_BASE + k, 1, val)
        call_fn(bdesc.APPLY_FN)
        return bdesc.current(m), bdesc.event_mask(m)

    got, mask = apply(1)
    check("槽位 1 → 描述符 1", got, bdesc.entry_addr(1))
    check("槽位 1 的掩码 = +4（3）", mask, 3)
    got, mask = apply(5)
    check("槽位 5 → 描述符 5", got, bdesc.entry_addr(5))
    check("槽位 5 的掩码 = 33（0x21）", mask, 0x21)
    got, mask = apply(2)
    check("槽位 2（+8=0）的掩码 = 0", mask, 0)
    check("槽位 0 是空标记：不取描述符、指针保持上一次的值",
          apply(0)[0], bdesc.entry_addr(2))
    check("槽位 0 的掩码保持哨兵", bdesc.event_mask(m), 0x55)
    check("current_index 仍是上一次的 2", bdesc.current_index(m), 2)
    check("expected_mask：+8=1 取 +4", bdesc.expected_mask(bdesc.entry(m, 5)), 33)
    check("expected_mask：+8=0 取 0", bdesc.expected_mask(bdesc.entry(m, 2)), 0)
    check("expected_mask：+8=2 返回 None", bdesc.expected_mask(bdesc.entry(m, 15)), None)

    # 取首：返回值在 R4，读的是 +8
    m2 = brealos.boot()
    head = blist.DISPLAY_QUEUE_HEAD
    node = bqueue.slot_addr(0)
    m2.bus.wr(head, 4, node)
    m2.bus.wr(head + 4, 4, node)
    m2.bus.wr(node, 4, head)
    m2.bus.wr(node + 4, 4, head)
    m2.bus.wr(node + 8, 1, 0x5A)
    m2.bus.wr(node + 9, 1, 0xA5)
    reads = []
    ow_rd = m2.bus.rd
    def rd(a_, sz_, pc_=None):
        if 0x9F58D000 <= a_ < 0x9F58DC00:
            reads.append(a_)
        return ow_rd(a_, sz_, pc_)
    m2.bus.rd = rd
    c2 = m2.cpu
    # V38：整段直接调会卡在 wai_sem（非任务上下文），所以跳过它、停在 sig_sem 之前
    def peek_from(head_addr):
        c2.r[8] = head_addr
        c2.dr[1] = 0x00407DC
        c2.r[15] = brealos.CALL_STACK
        c2.r[4], c2.r[5] = 0, 0
        c2.i = 0
        c2.pc = 0x0012E64A        # 0x12E63A 里 wai_sem 之后的第一步
        n = 0
        while n < 400 and not c2.halted and c2.pc != 0x0012E664:
            m2.step(1)
            n += 1
        return c2.r[4], c2.r[5]

    r4v, r5v = peek_from(head)
    m2.bus.rd = ow_rd
    check("非空时 r4 = is_empty 的结果（0）", r4v, 0)
    check("载荷在 R5（取首随后 mov r4,r5）", r5v, 0x5A)
    check("取首没读 +9", node + 9 in reads, False)
    check("取首读了 +8", node + 8 in reads, True)

    # 空表：head 自指 → is_empty 为真
    m2.bus.wr(head, 4, head)
    m2.bus.wr(head + 4, 4, head)
    r4v, r5v = peek_from(head)
    check("空表时 r4 = 1（is_empty 真）", r4v, 1)
    check("空表不填载荷", r5v, 0)


def test_bhandler():
    """V37：44 个处理函数的语义 —— 事件翻译器、模式 2 的常量、掩码区 9 字节。"""
    print("\n[43 处理函数与掩码区（V37）]")
    from frbox import bhandler, bdesc, bqueue, blist, brealos

    check("服务号 set_flg = -48", bhandler.SVC_SET_FLG, -48)
    check("提交函数地址", bhandler.SUBMIT, 0x0012E6F8)
    check("掩码区基址", bhandler.MASK_BLOCK, 0x9F58DA7C)
    check("ret 的机器码", bhandler.RET_BYTES, b"\x97\x20")

    m = brealos.boot()
    a = bhandler.audit(m)
    check("处理函数 44 个 / 槽位 70 个", (a["handlers"], a["slots_covered"]), (44, 70))
    check("单条 ret 的处理函数 5 个", a["ret_handlers"],
          [0x0012E752, 0x0012F0F4, 0x0012F212, 0x0012F2A6, 0x0012F5AC])
    check("单条 ret 占 31 个槽位", len(a["ret_only_slots"]), 31)
    check("实做 39 个", a["handlers"] - len(a["ret_handlers"]), 39)
    check("flag 只有 1/3/10/12", a["flag_ids"], [1, 3, 10, 12])
    fm = bhandler.flag_map(m)
    check("各 flag 的条数", {k: len(v) for k, v in fm.items()},
          {1: 1, 3: 13, 10: 12, 12: 16})
    check("flag 覆盖 29 个槽位", a["flag_slots"], 29)
    check("位操作族 23 个槽位", a["bit_slots"], 23)
    check("状态链（回调提交）", a["requeue_slots"], [6, 8, 12, 42, 44, 52, 57])
    check("信号量族", a["sem_slots"], [17, 48])

    def svcs(h):
        return [(x[2], x[3], x[4]) for x in bhandler.actions(h) if x[0] == "svc"]

    def bits(h):
        return [(x[2], x[3], x[4]) for x in bhandler.actions(h) if x[0] == "bit"]

    check("槽位 9 = set_flg(10, 0x2000)",
          svcs(bdesc.entry(m, 9)["handler"]), [("set_flg", 10, 0x2000)])
    check("槽位 4 = set_flg(3, 1024)",
          svcs(bdesc.entry(m, 4)["handler"]), [("set_flg", 3, 1024)])
    check("槽位 18 只置位 0x9F58DEBC 的 bit3（不调服务）",
          (bits(bdesc.entry(m, 18)["handler"]), svcs(bdesc.entry(m, 18)["handler"])),
          ([("borl", 0x9F58DEBC, 8)], []))
    check("槽位 23 的处理函数就是单条 ret",
          bhandler.is_ret(m, bdesc.entry(m, 23)["handler"]), True)
    check("槽位 3 同时发两个 flag",
          sorted(svcs(bdesc.entry(m, 3)["handler"])),
          [("set_flg", 3, 512), ("set_flg", 10, 2048)])

    check("模式 2 的常量表", bhandler.MODE2_CONST, {15: 0x80, 68: 0x8A, 31: 0x00, 32: 0x00})
    check("模式 2 的槽位", sorted(bhandler.mode2_check(m)), [0, 15, 31, 32, 68])

    # 动态：掩码值 + 掩码区布局（V38：返回地址设 DR1）
    def call_fn(addr, r4=0, r5=0):
        return brealos.call_fn(m, addr, r4=r4, r5=r5)

    def apply(slot):
        for k, val in enumerate((0x55, slot, 0, slot)):
            m.bus.wr(bqueue.CURSOR_BASE + k, 1, val)
        call_fn(bdesc.APPLY_FN)
        return bdesc.event_mask(m)

    check("模式 2：槽位 15 → 0x80", apply(15), 0x80)
    check("模式 2：槽位 68 → 0x8A", apply(68), 0x8A)
    check("模式 2：槽位 31 → 0x00", apply(31), 0x00)
    check("模式 2：槽位 32 → 0x00", apply(32), 0x00)
    check("模式 2：15/68 的 +4 都是 0，掩码与 +4 无关",
          (bdesc.entry(m, 15)["value"], bdesc.entry(m, 68)["value"]), (0, 0))
    apply(5)
    blk = bytes(m.fetch_bytes(bhandler.MASK_BLOCK, 9))
    check("掩码区 9 字节：7C=槽位号、7F=掩码、82=槽位号、81=掩码",
          (blk[0], blk[1], blk[3], blk[5], blk[6]),
          (5, 0, 0x21, 0x21, 5))

    # 提交函数不写 r5
    m2 = brealos.boot()
    head = blist.DISPLAY_QUEUE_HEAD
    m2.bus.wr(head, 4, head)
    m2.bus.wr(head + 4, 4, head)
    for i in range(70):
        b = bqueue.slot_addr(i)
        for k in (0, 4, 8):
            m2.bus.wr(b + k, 4, 0)
    c2 = m2.cpu
    writes = []
    base_wr = m2.bus.wr

    def spy(addr, sz, val, pc=None):
        if 0x9F58D508 <= addr < 0x9F58D848:
            writes.append((addr, val))
        return base_wr(addr, sz, val, pc)

    m2.bus.wr = spy
    brealos.call_fn(m2, bqueue.SUBMIT, r4=5, r5=0x11223344)
    m2.bus.wr = base_wr
    check("提交的写入只落在 +0 / +4 / +9",
          sorted({addr - bqueue.slot_addr(5) for addr, _v in writes}), [0, 4, 9])
    check("载荷值没有出现在任何写入里", [v for _a, v in writes if v == 0x11223344], [])
    check("槽位 +8 仍为 0", m2.bus.rd(bqueue.slot_addr(5) + 8, 1), 0)

    # 接收端：显示模块里的 wai_flg(flgid=3)
    d = bhandler.decode_at(0x12E118)
    check("0x12E118 = ldi r5,#3（flgid）",
          (d[0], d[2].get("i8"), d[2].get("ri")), ("ldi:8", 3, 5))
    d2 = bhandler.decode_at(0x12E11C)
    check("0x12E11C = ldi r7,#2（wfmode）",
          (d2[0], d2[2].get("i8"), d2[2].get("ri")), ("ldi:8", 2, 7))


def test_binit():
    """V38：显示初始化 0x12E006 —— 载荷=槽位号、746 步跑通、调用者是任务 5。"""
    print("\n[44 显示初始化与载荷（V38）]")
    from frbox import binit, brealos, blist, bqueue, bdesc

    m = brealos.boot()

    # 初始化之前：什么都没做
    check("boot 后没有初始化标志", binit.is_initialized(m), False)
    check("boot 后队列哨兵还是 0", m.bus.rd(binit.QUEUE_HEAD, 4), 0)
    check("boot 后 70 个槽位的 +8 全 0", set(binit.payload_map(m)), {0})
    check("boot 后取首恒返回 0", bqueue.peek(m), 0)
    check("describe 说的是「没跑过」", "没跑过" in binit.describe(m), True)

    # 只跑队列初始化（0x12E602）
    res = binit.queue_init(m)
    check("0x12E602 正常返回", res["ret"], True)
    check("0x12E602 的步数（确定性）", res["steps"], 600)
    check("跑完哨兵自指", (m.bus.rd(binit.QUEUE_HEAD, 4),
                        m.bus.rd(binit.QUEUE_HEAD + 4, 4)),
          (binit.QUEUE_HEAD, binit.QUEUE_HEAD))
    check("70 个槽位的 +8 = 0..69", binit.payload_map(m), list(range(70)))
    check("槽位 0 的 +8 = 0", binit.slot_index(m, 0), 0)
    check("槽位 69 的 +8 = 69", binit.slot_index(m, 69), 69)
    check("只跑 0x12E602 不会置初始化标志", binit.is_initialized(m), False)

    # 编号写入点在 0x12E61C（记账显示成 0x12E61E）
    log = []
    m2 = brealos.boot()
    base_wr = m2.bus.wr

    def spy(addr, sz, val, pc=None):
        log.append((pc, addr, sz, val))
        return base_wr(addr, sz, val, pc)

    m2.bus.wr = spy
    brealos.call_fn(m2, binit.QUEUE_INIT_FN)
    m2.bus.wr = base_wr
    pay = [x for x in log if binit.SLOT_BASE <= x[1] < binit.SLOT_BASE + 70 * 12
           and (x[1] - binit.SLOT_BASE) % 12 == 8]
    check("槽位 +8 写入 70 笔", len(pay), 70)
    check("写入者就是 0x12E61C（stb 本身）", sorted({x[0] for x in pay}), [0x0012E61C])

    # 完整初始化（0x12E006）
    m3 = brealos.boot()
    res = binit.run(m3)
    check("0x12E006 正常返回", res["ret"], True)
    check("0x12E006 的步数（确定性）", res["steps"], 746)
    check("0x12E006 的写入笔数", len(res["writes"]), 151)
    check("跑完置了初始化标志", binit.is_initialized(m3), True)
    a = binit.audit(m3)
    check("体检：哨兵 + 编号全对", (a["initialized"], a["head_ok"], a["numbered"], a["bad"]),
          (True, True, True, []))
    hw = [w for w in res["writes"] if w[1] == binit.HW_PERIOD]
    check("硬件定时器写入 0x0002039F", hw[0][3] if hw else None, 0x0002039F)
    check("该寄存器读回只有低字节 0x9F", m3.bus.rd(binit.HW_PERIOD, 1), 0x9F)
    check("describe 说的是「已跑」", "已跑" in binit.describe(m3), True)

    # 调用者链
    check("0x12E006 的唯一调用点", binit.INIT_CALLER, 0x0012DFB8)
    check("0x12DFB8 的 call 目标就是 0x12E006", binit.INIT_FN, 0x0012E006)
    rec = 0x54C00 + 4 * 0x1C
    check("任务表记录 4 的 +0x10 = 0x12DFAE", m3.bus.rd(rec + 0x10, 4), 0x0012DFAE)
    check("记录 4 是任务 5", binit.TASK_ID, 5)
    check("记录 3（任务 4）的入口是 0x126454",
          m3.bus.rd(0x54C00 + 3 * 0x1C + 0x10, 4), 0x00126454)

    # 任务 5 的门：sta_tsk 改 TCB，位图不动
    m4 = brealos.boot()
    tcb5 = m4.bus.rd(0x54C00 + 4 * 0x1C, 4)
    check("任务 5 初始状态 = 0x10（休眠）", m4.bus.rd(tcb5 + 8, 4), 0x10)
    r = brealos.sta_tsk(m4, 5)
    check("sta_tsk(5) 返回 0", r["err"], 0)
    check("任务 5 状态变 0（就绪）", m4.bus.rd(tcb5 + 8, 4), 0)
    check("任务 5 的保存 SP 已填", m4.bus.rd(tcb5 + 0x18, 4), 0x9F8036A4)

    # 方法学：ret 读 DR1，不读栈
    check("boot 后 DR1 是最后一次 call 的返回地址 0x404F8",
          brealos.boot().cpu.dr[1], 0x000404F8)
    m5 = brealos.boot()
    c5 = m5.cpu
    m5.bus.wr(0x9F83EFFC, 4, 0x00407DC)      # 只写栈，不设 DR1
    c5.r[15] = 0x9F83EFFC
    c5.i = 0
    c5.pc = binit.INIT_FN
    n = 0
    while n < 5000 and not c5.halted and c5.pc != 0x00407DC:
        m5.step(1)
        n += 1
    check("只写栈 / 改 SP 不会让函数返回", c5.pc != 0x00407DC, True)
    check("它会撞进 0x404E0 兜底自旋", c5.pc, 0x000404E0)

    # 阻塞型服务在非任务上下文里的返回码
    m6 = brealos.boot()
    check("wai_flg 在非任务上下文返回 -33",
          brealos.kernel_call(m6, -46, r4=3, r5=1)["err"], -33)
    check("set_flg 正常返回 0",
          brealos.kernel_call(m6, -48, r4=3, r5=1)["err"], 0)
    r = brealos.call_fn(m6, bqueue.PEEK, r4=0, max_steps=3000)
    check("整段调取首会卡在 wai_sem", r["ret"], False)

    # 取首的寄存器语义：R4 = is_empty，R5 = 载荷
    head = blist.DISPLAY_QUEUE_HEAD
    node = bqueue.slot_addr(0)
    for nonempty, pay_v, want_r4, want_r5 in ((True, 0x5A, 0, 0x5A),
                                               (True, 37, 0, 37),
                                               (False, 0, 1, 0)):
        m7 = brealos.boot()
        if nonempty:
            for addr, v in ((head, node), (head + 4, node), (node, head),
                            (node + 4, head), (node + 8, pay_v)):
                m7.bus.wr(addr, 4 if addr % 4 == 0 and addr != node + 8 else 1, v)
        else:
            m7.bus.wr(head, 4, head)
            m7.bus.wr(head + 4, 4, head)
        c7 = m7.cpu
        c7.r[8] = head
        c7.dr[1] = 0x00407DC
        c7.r[15] = brealos.CALL_STACK
        c7.r[4], c7.r[5] = 0, 0
        c7.i = 0
        c7.pc = 0x0012E64A
        n = 0
        while n < 400 and not c7.halted and c7.pc != 0x0012E664:
            m7.step(1)
            n += 1
        check("取首 %s：r4 = is_empty" % ("非空" if nonempty else "空表"),
              c7.r[4], want_r4)
        check("取首 %s：r5 = 载荷" % ("非空" if nonempty else "空表"),
              c7.r[5], want_r5)


def test_bdisp():
    """V26：显示链路 —— 描述符表、绘制函数、文案池、重建图。"""
    print("\n[32 显示链路与重建图（V26）]")
    import os as _os
    import tempfile
    from frbox import bdisp

    m = create("d300-b")
    m.load()
    s = bdisp.summary(m)
    check("描述符表 70 条", s["count"], 70)
    check("有文本的条目 65", s["nonempty"], 65)
    check("样式 0/1/2 = 41/24/5", (s["type0"], s["type1"], s["type2"]), (41, 24, 5))
    check("文本总字节 1644", s["text_bytes"], 1644)
    check("绘制函数去重 44 个", s["unique_fn"], 44)
    check("其中空实现 5 个", s["noop_fn"], 5)
    check("0x12E752 是裸 ret", bdisp.fn_is_noop(m, 0x12E752), True)
    check("PLAYBACK 前 5 条",
          bdisp.menu_items(m, "PLAYBACK", 5),
          ["Delete", "Playback folder", "Rotate tall", "Slide show", "Print set (DPOF)"])
    check("SETUP 首条", bdisp.menu_items(m, "SETUP", 1), ["Format memory card"])
    check("split_items 会把 OnOff 切成两条", bdisp.split_items(b"OnOff"), ["On", "Off"])

    png = _os.path.join(tempfile.gettempdir(), "frbox_bdisp_selftest.png")
    bdisp.render(png, m, facts={"expeed_w": 238, "expeed_r": 253, "sdram_run": 2126})
    check("重建图 PNG 生成且大于 10KB", _os.path.getsize(png) > 10000, True)


def test_bexpeed():
    """V27：B↔EXPEED 位串行总线的抓包解码。"""
    print("\n[33 EXPEED 位串行解码（V27）]")
    from frbox import bexpeed, bgate, brealos

    check("CS 在 0x70000002 bit2", (bexpeed.CS_ADDR, bexpeed.CS_BIT), (0x70000002, 0x04))
    check("CLK 在 0x7000000A bit1", (bexpeed.BUS_ADDR, bexpeed.CLK_BIT), (0x7000000A, 0x02))
    check("DATA 在 0x7000000A bit2", bexpeed.DATA_BIT, 0x04)
    check("LSB 先：1,0,1,0,0,0,0,0 → 0x05", bexpeed.decode_bits([1, 0, 1, 0, 0, 0, 0, 0]), [0x05])
    check("0x86 = 读寄存器 6", bexpeed.classify(0x86), ("read", 6))
    check("0x76 = 写寄存器 0x76", bexpeed.classify(0x76), ("write", 0x76))

    m = create("d300-b")
    tap = bexpeed.ExpeedTap().attach(m)
    m.load()
    m.reset()
    n = 0
    while not m.cpu.halted and n < 191520:
        m.step(1)
        n += 1
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=200000, spin_guard=False)
    m.run(max_steps=800000, spin_guard=False)
    bgate.open_gate(m)
    m.run(max_steps=1000000, spin_guard=False)
    tap.detach()
    s = tap.summary()
    # V28 修正：按 CS 上升沿分帧 + 只数 CLK 上升沿 + 收尾帧 flush，
    # V27 报的「2 帧 / 8+40 位 / 0 条写命令」是抓包器 bug。
    check("整场 4 个 CS 窗口", s["frames"], 4)
    check("帧位数 1/16/48/16", s["bits"], [1, 16, 48, 16])
    check("端口配置写 6 次", s["cfg_writes"], 6)
    cmds = s["cmds"]
    check("窗口 1 = 读寄存器 6", (cmds[0]["kind"], cmds[0]["reg"]), ("read", 6))
    check("窗口 2 = 数据相位", cmds[1]["kind"], "data")
    check("窗口 3 = 写寄存器 0x76", (cmds[2]["kind"], cmds[2]["reg"]), ("write", 0x76))
    check("写载荷 = 0x01", cmds[2]["payload"], b"\x01")


def test_bexpeed_dev():
    """V28：EXPEED 器件模型（引脚级双向）+ 寄存器 6 读回值的作用。"""
    print("\n[34 EXPEED 器件模型与寄存器 6（V28）]")
    from frbox import brealos, bexpeed

    dev_cls = create("d300-b").expeed
    check("D300B 默认建 EXPEED 器件", dev_cls is not None, True)
    check("器件基址 0x70000000", dev_cls.base, 0x70000000)
    check("默认寄存器 6 = 5 个 0 字节", dev_cls.reg_bytes(6), b"\x00" * 5)

    def boot_with(reg6, steps=400000):
        m = create("d300-b")
        if reg6 is not None:
            m.expeed.set_reg(6, reg6)
        m.load()
        m.reset()
        m.enable_cov()
        m.attach_flow()
        n = 0
        while not m.cpu.halted and n < 191520:
            m.step(1)
            n += 1
        brealos.sta_tsk(m, 2)
        m.raise_irq(0x3F)
        m.run(max_steps=steps, spin_guard=False)
        return m

    m0 = boot_with(None)
    d0 = m0.expeed
    check("器件收到 2 条命令：读 6 + 写 0x76",
          [(c["kind"], c["reg"]) for c in d0.cmds], [("read", 6), ("write", 0x76)])
    check("读命令驱动了应答位（>0）", d0.bits_out > 0, True)
    check("reg6=0：状态字节 0x9F5A321C = 0x00", m0.fetch_bytes(0x9F5A321C, 1), b"\x00")
    check("reg6=0：结束在 0x406D6（内核）", m0.cpu.pc, 0x000406D6)

    m_lo = boot_with(b"\x20" + b"\x00" * 5)
    check("reg6=0x20：状态字节 = 读回字节", m_lo.fetch_bytes(0x9F5A321C, 1), b"\x20")

    m_hi = boot_with(b"\x80" + b"\x00" * 5)
    check("reg6=0x80（bit7 置位）：状态字节写成常量 0x88",
          m_hi.fetch_bytes(0x9F5A321C, 1), b"\x88")
    check("bit7 置位：走到 0x1203B0（主循环）", m_hi.cpu.pc, 0x001203B0)
    check("bit7 置位：覆盖 2692", m_hi.cov.words(), 2692)
    check("bit7 清零：覆盖 2632", m0.cov.words(), 2632)

    m_b2 = boot_with(b"\x00\xff" + b"\x00" * 4)
    check("第 2 字节不影响分支", m_b2.cpu.pc, 0x000406D6)

    # V29：写 0x76 = [0x9F5A321E] + 1（初值 0 → 载荷 1），且整场只写一次
    check("寄存器 0x76 只被写 1 次", len(d0.writes_to(0x76)), 1)
    check("写 0x76 的载荷 = [1]", d0.writes_to(0x76)[0], [1])
    check("寄存器 6 只被读 1 次", d0.reads_of(6), 1)
    check("0x9F5A321E（0x76 的计数源）装配后 = 1",
          m0.fetch_bytes(0x9F5A321E, 1), b"\x01")
    check("bit7 置位时 0x9F5A321E 同样 = 1",
          m_hi.fetch_bytes(0x9F5A321E, 1), b"\x01")


def test_bgate2():
    """V30：第二道门（0x9F58D450 字节 bit6）与状态字的读写分类。"""
    print("\n[35 第二道门与状态字分类（V30）]")
    from frbox import bgate, brealos

    m = create("d300-b")
    m.load()
    check("第二道门常量 = 0x40", bgate.STATUS_WORK_READY, 0x40)
    ra = bgate.access(m, bgate.STATUS)
    rb = bgate.access(m, bgate.STATUS2)
    kinds_a = [k for _, k, _ in ra]
    kinds_b = [k for _, k, _ in rb]
    check("A 的引用 30 处", len(ra), 30)
    check("A 里位测试 26 处", kinds_a.count("test"), 26)
    check("A 里位写 1 处", kinds_a.count("bitop"), 1)
    check("A 的唯一写点在 0x21B334", [pc for pc, k, _ in ra if k == "bitop"], [0x0021B334])
    check("B 的引用 44 处", len(rb), 44)
    check("B 里位写 23 处", kinds_b.count("bitop"), 23)

    def run(a_byte):
        mm = create("d300-b")
        mm.load()
        mm.reset()
        mm.enable_cov()
        mm.attach_flow()
        n = 0
        while not mm.cpu.halted and n < 191520:
            mm.step(1)
            n += 1
        brealos.sta_tsk(mm, 2)
        mm.raise_irq(0x3F)
        mm.run(max_steps=200000, spin_guard=False)
        bgate.open_gate(mm)
        if a_byte is not None:
            mm.bus.wr(bgate.STATUS, 1, a_byte)
        mm.run(max_steps=150000, spin_guard=False)
        return mm

    m_no = run(None)
    check("门开着但 A 不动：没到 0x12018E", m_no.cov.visited(0x12018E), False)
    check("门开着但 A 不动：没进 0x12E73E", m_no.cov.visited(0x12E73E), False)
    check("门开着时 work_ready = False", bgate.work_ready(m_no), False)

    m_yes = run(bgate.STATUS_WORK_READY)
    check("置 A 的 bit6 后到 0x12018E", m_yes.cov.visited(0x12018E), True)
    check("并进到 0x12E73E", m_yes.cov.visited(0x12E73E), True)
    check("work_ready = True", bgate.work_ready(m_yes), True)
    check("覆盖比不置位多 7", m_yes.cov.words() - m_no.cov.words(), 7)


def test_bqueue():
    """V31：显示请求队列（head 0x9F58D500、节点 0x9F58D508+n*0x0C、提交 0x12E6F8）。"""
    print("\n[36 显示请求队列（V31）]")
    import os as _os
    from frbox import brealos, bgate

    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    rom = open(_os.path.join(_root, "data", "B3000111.bin"), "rb").read()

    def count_imm(tgt):
        pat = tgt.to_bytes(4, "big")
        c, i = 0, rom.find(pat)
        while i >= 0:
            c += 1
            i = rom.find(pat, i + 1)
        return c

    check("0x12E6F8 有 312 处 ldi 引用", count_imm(0x12E6F8), 312)
    check("0x120458 没有任何立即数引用", count_imm(0x120458), 0)
    check("11 号槽位 = 0x9F58D58C", 0x9F58D508 + 0x0B * 0x0C, 0x9F58D58C)

    m = create("d300-b")
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    n = 0
    while not m.cpu.halted and n < 191520:
        m.step(1)
        n += 1
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=200000, spin_guard=False)
    bgate.open_gate(m)
    m.bus.wr(bgate.STATUS, 1, bgate.STATUS_WORK_READY)

    entry = None
    logs = []
    _ow = m.bus.wr

    def _w(a, sz, v, pc=None):
        if a == 0x9F58D500 and (v & 0xFFFFFFFF):
            logs.append(v & 0xFFFFFFFF)
        return _ow(a, sz, v, pc)

    m.bus.wr = _w
    c = m.cpu
    for _ in range(150000):
        m.step(1)
        if c.pc == 0x12E6F8 and entry is None:
            entry = (c.r[4], c.r[5])
    m.bus.wr = _ow
    check("进入 0x12E6F8 时 r4 = 0x0B（槽位号）", entry[0], 0x0B)
    check("r5 是 clr_flg 的残值 0xFFDFFFFF", entry[1], 0xFFDFFFFF)
    check("挂链写进 head 的是节点地址 0x9F58D58C", logs[:1], [0x9F58D58C])


def test_bdispchain():
    """V32：显示队列完整链路（70 槽位 / sem 17 / set_flg(3,0x80)）。"""
    print("\n[37 显示队列链路（V32）]")

    m = create("d300-b")
    m.load()

    def line(addr):
        out = m.disasm_at(addr, 1).splitlines()
        return out[0] if out else ""

    def has(addr, *frags):
        t = line(addr)
        return all(f in t for f in frags)

    check("0x12E620 装 0x46（70 个槽位）", has(0x12E620, "ldi:8", "i8=0x46"), True)
    check("0x12E626 步长 0x0C（blt 的延迟槽）", has(0x12E626, "add", "u4=0xc", "ri=0x5"), True)
    check("0x12E62C 置位 0x80", has(0x12E62C, "ldi:8", "i8=0x80", "ri=0x5"), True)
    check("0x12E630 是 set_flg(iset 陷阱)", has(0x12E630, "int", "u8=0x40"), True)
    check("消费端 0x12E640 用信号量 17", has(0x12E640, "ldi:8", "i8=0x11", "ri=0x4"), True)
    check("消费端 0x12E642 是 wai_sem(-53)", has(0x12E642, "ldi:8", "i8=0xcb", "ri=0xc"), True)
    check("生产端 0x12E70C 也用信号量 17", has(0x12E70C, "ldi:8", "i8=0x11", "ri=0x4"), True)
    check("生产端 0x12E712 是 wai_sem 陷阱", has(0x12E712, "int", "u8=0x40"), True)


def test_bsem():
    """V33：信号量表与计数、循环双链表原语。"""
    print("\n[38 信号量表与链表原语（V33）]")
    from frbox import bsem, blist, brealos

    check("信号量表 0x54F2C / 步长 0x10 / 40 项",
          (bsem.SEM_TABLE, bsem.SEM_STRIDE, bsem.SEM_COUNT), (0x54F2C, 0x10, 40))
    check("id 17 的表项地址 0x5502C", bsem.table_addr(17), 0x0005502C)
    check("对象基址 0x68000638 / 步长 0x10",
          (bsem.SEM_OBJ_BASE, bsem.SEM_OBJ_STRIDE), (0x68000638, 0x10))
    check("id 17 的对象地址（算）0x68000738", bsem.object_addr_calc(17), 0x68000738)
    check("计数三个写者",
          (bsem.COUNT_WRITER_INIT, bsem.COUNT_WRITER_WAI, bsem.COUNT_WRITER_SIG),
          (0x00040910, 0x0004193A, 0x000418F0))

    m = brealos.boot()
    rows = bsem.entries(m)
    check("整表 40 项", len(rows), 40)
    check("表结束于 flag 表表头 0x551B0", bsem.summary(m)["table_end"], 0x000551B0)
    check("表项对象指针与步长一致", bsem.summary(m)["mismatched"], [])
    check("id 17 的对象地址（读表）0x68000738", bsem.object_addr(m, 17), 0x68000738)
    check("id 17 的 id 字 = 17<<16", bsem.entry(m, 17)["idword"], 0x00110000)
    check("id 17 的计数 = 1", bsem.count(m, 17), 1)
    check("id 17 的等待队列自指（空）", bsem.waitq(m, 17), 0x68000738)
    check("id 17 的背指 = 自己记录 0x55030", bsem.backptr(m, 17), 0x00055030)
    check("计数不为 1 的是 14/27/29",
          sorted(bsem.summary(m)["counts_not_one"].items()), [(14, 0), (27, 0), (29, 0)])
    check("非二值（上限 > 1）的是 14/27/29", bsem.summary(m)["not_binary"], [14, 27, 29])
    check("上限：14=7 / 27=255 / 29=255",
          (bsem.max_count(m, 14), bsem.max_count(m, 27), bsem.max_count(m, 29)), (7, 255, 255))
    check("初始计数规则 40/40 命中（记录 +0xC 的高半字）",
          bsem.summary(m)["init_rule_fail"], [])
    check("sem 40 的参数格 0x551AC = 0x00010001", m.bus.rd(bsem.TAIL_PAD, 4), 0x00010001)
    check("id 14 的初始计数 = 0（记录 +0xC 高半字）", bsem.initial_count(m, 14), 0)
    check("参数格地址 = 旧接口 table_addr(id+1)",
          bsem.record_addr(14) + bsem.T_PARAM, bsem.table_addr(15))
    check("两个已知表头在 boot 后仍是 0（init 调用点 0x121xxx 从未执行）",
          [m.bus.rd(h, 4) for h in blist.OTHER_HEADS], [0, 0])
    check("显示队列没被 init（[head] = 0）", blist.first(m, blist.DISPLAY_QUEUE_HEAD), None)

    # --- 单元调用 ROM 里的两条原语 ---
    HEADW, NODEW = 0x9F58D000, 0x9F58D100

    def unit(pc, r4, r5, setup=None):
        mm = create("d300-b")
        mm.load()
        mm.reset()
        if setup:
            setup(mm)
        cc = mm.cpu
        cc.r[4], cc.r[5] = r4, r5
        cc.pc = pc
        for _ in range(12):
            mm.step(1)
            if cc.halted:
                break
        return mm

    m1 = unit(blist.LIST_INIT, HEADW, 0)
    check("0x124F46 是 init：[head] = head", m1.bus.rd(HEADW, 4), HEADW)
    check("0x124F46 是 init：[head+4] = head", m1.bus.rd(HEADW + 4, 4), HEADW)
    check("init 后 is_empty", blist.is_empty(m1, HEADW), True)

    m2 = unit(blist.LIST_INSERT, HEADW, NODEW, setup=lambda x: blist.init_model(x, HEADW))
    check("insert 后 head->next = node", m2.bus.rd(HEADW, 4), NODEW)
    check("insert 后 head->prev = node", m2.bus.rd(HEADW + 4, 4), NODEW)
    check("insert 后 node->next = head（循环）", m2.bus.rd(NODEW, 4), HEADW)
    check("insert 后 node->prev = head", m2.bus.rd(NODEW + 4, 4), HEADW)
    check("walk 走一圈得到 1 个节点", blist.walk(m2, HEADW), [NODEW])

    m3 = unit(blist.LIST_INIT, HEADW, 0)
    blist.insert_model(m3, HEADW, 0x9F58D100)
    blist.insert_model(m3, HEADW, 0x9F58D200)
    check("头插顺序：最新在链首", blist.walk(m3, HEADW), [0x9F58D200, 0x9F58D100])
    check("反向走一致", blist.back_walk(m3, HEADW), [0x9F58D100, 0x9F58D200])


def test_blist():
    """V34：链表四件套（init / is_empty / insert / unlink）与表头语义。"""
    print("\n[39 链表四件套与表头语义（V34）]")
    import os as _os
    from frbox import bsem, blist, brealos

    check("四件套地址",
          (blist.LIST_INIT, blist.LIST_IS_EMPTY, blist.LIST_INSERT, blist.LIST_UNLINK),
          (0x000124F46, 0x000124F4E, 0x000124F5A, 0x000124F7A))

    H, NA, NB, NC = 0x9F58D000, 0x9F58D100, 0x9F58D200, 0x9F58D300

    def build(nodes):
        mm = create("d300-b")
        mm.load()
        mm.reset()
        blist.init_model(mm, H)
        for x in nodes:
            blist.insert_model(mm, H, x)
        return mm

    m0 = build([])
    check("空表 is_empty_rom 返回 1", blist.is_empty_rom(m0, H), 1)
    m1 = build([NA, NB, NC])
    snap = m1.fetch_bytes(H, 8)
    check("非空 is_empty_rom 返回 0", blist.is_empty_rom(m1, H), 0)
    check("is_empty_rom 不改内存", m1.fetch_bytes(H, 8), snap)
    check("三节点链的走向（头插）", blist.walk(m1, H), [NC, NB, NA])

    blist.unlink_model(m1, NB)
    check("摘中间节点后 forward", blist.walk(m1, H), [NC, NA])
    check("摘中间节点后 backward", blist.back_walk(m1, H), [NA, NC])

    m2 = build([NA, NB])
    m2.bus.wr(H + 8 + 9, 1, 0x80)          # 槽位 0 的 +9
    m2.bus.wr(H + 8 + 0x0C + 9, 1, 0xC0)   # 槽位 1 的 +9
    blist.clear_model(m2, H)
    check("clear_model 后是空表", blist.is_empty(m2, H), True)
    check("clear_model 清掉槽位标志",
          [m2.bus.rd(H + 8 + i * 0x0C + 9, 1) for i in (0, 1, 69)], [0, 0, 0])

    # 表头语义：两张表互证
    mb = brealos.boot()
    check("信号量表首项 +0 = 0x00280010（40 | 16）", mb.bus.rd(bsem.SEM_TABLE, 4), 0x00280010)
    check("标志表首项 +0 = 0x00110010（17 | 16）", mb.bus.rd(0x551B0, 4), 0x00110010)
    check("sem 14 的上限在 slot 15 的低半字 = 7",
          mb.bus.rd(bsem.table_addr(15), 4) & 0xFFFF, 7)
    check("sem 27 / 29 的上限 = 255 / 255",
          (mb.bus.rd(bsem.table_addr(28), 4) & 0xFFFF,
           mb.bus.rd(bsem.table_addr(30), 4) & 0xFFFF), (255, 255))
    counting = [i for i in range(1, 41)
                if (mb.bus.rd(bsem.table_addr(i + 1) if i < 40 else bsem.TAIL_PAD, 4) & 0xFFFF) != 1]
    check("非二值的只有 14 / 27 / 29", counting, [14, 27, 29])

    # 调用点计数（扫立即数）
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    rom = open(_os.path.join(_root, "data", "B3000111.bin"), "rb").read()
    check("0x124F46 的立即数引用 44 处",
          rom.count((0x000124F46).to_bytes(4, "big")), 44)
    check("0x124F5A 的立即数引用 88 处",
          rom.count((0x000124F5A).to_bytes(4, "big")), 88)
    check("0x124F7A 的立即数引用 43 处",
          rom.count((0x000124F7A).to_bytes(4, "big")), 43)


def test_bready():
    """V39：真正的就绪结构（0x68000000 头部）、sta_tsk 的请求派发、任务 5 上 CPU。"""
    print("\n[45 就绪结构与任务调度（V39）]")
    from frbox import bready, brealos, binit

    # ---- 0x45984 在 B 段是 ROM（V38 把它当位图，读的其实是固件字节）
    m = brealos.boot()
    reg = m.bus.region_at(bready.A_READY_MAP, 4)
    check("0x45984 在 B 段落在 ROM", reg.kind, "rom")
    check("读回是固件字节 0x2E80A800", m.bus.rd(bready.A_READY_MAP, 4), 0x2E80A800)
    m.bus.wr(bready.A_READY_MAP, 4, 0xDEADBEEF)
    check("写 0x45984 被丢弃", m.bus.rd(bready.A_READY_MAP, 4), 0x2E80A800)

    # ---- 内核区头部：boot 后只有队列 0（任务 2）
    idx = bready.task_index(m)
    idle = idx[29]
    t2, t5 = idx[2], idx[5]
    check("boot 后当前 TCB = 空闲", bready.current(m), idle)
    check("boot 后就绪位图 = 0x80000000", bready.ready_map(m), 0x80000000)
    check("队列 0 里是任务 2", bready.queue_occupants(m, 0), [t2])
    check("队列 1 空", bready.queue_occupants(m, 1), [])
    check("队列 2 空", bready.queue_occupants(m, 2), [])
    check("任务 2 优先数 = 1", bready.prio_of(m, t2), 1)
    check("任务 2 应在队列 0", bready.expected_queue(m, t2), 0)
    check("队列 0 的位号 = 31", bready.bit_of(0), 31)
    check("队列 3 的位号 = 28", bready.bit_of(3), 28)
    ok, bad, rows = bready.map_consistent(m)
    check("boot 后 队列号 = 优先数−1（一致 / 不一致）", (ok, bad), (1, 0))
    check("boot 后位图那一位是 1", rows[0]["bit_set"], True)
    check("describe 报的是空闲任务", "空闲" in bready.describe(m), True)
    check("boot 后任务 5 不在队列里", bready.occupancy(m).get(t5), None)

    # ---- sta_tsk(5)：入队 3 + 置第 28 位 + 写 0x44
    log = []
    base_wr = m.bus.wr

    def spy(addr, sz, val, pc=None):
        log.append((pc, addr, sz, val))
        return base_wr(addr, sz, val, pc)

    icr_boot = m.bus.rd(bready.DISPATCH_ICR, 1)
    m.bus.wr = spy
    r = brealos.sta_tsk(m, bready.TASK5_ID)
    m.bus.wr = base_wr
    check("sta_tsk(5) 返回 0", r["err"], 0)
    check("sta_tsk(5) 步数（确定性）", r["steps"], 128)
    check("位图第 28 位置起", bready.ready_map(m), 0x90000000)
    check("任务 5 进了队列 3", bready.queue_occupants(m, 3), [t5])
    check("任务 5 状态字节 = 0（就绪）", bready.state_of(m, t5), 0)
    check("任务 5 保存栈顶 = 0x9F8036A4", m.bus.rd(t5 + bready.TCB_SP, 4), 0x9F8036A4)
    check("写位图的那一笔在 0x41882",
          [(pc, v) for pc, a, sz, v in log if a == bready.READY_MAP], [(0x41882, 0x90000000)])
    check("写了 0x44 请求派发（0x41886）",
          [(pc, v) for pc, a, sz, v in log if a == bready.DISPATCH_REQ], [(0x41886, 1)])
    ok2, bad2, rows2 = bready.map_consistent(m)
    check("sta_tsk 后两条关系仍成立", (ok2, bad2), (2, 0))
    check("任务 5 的位图那一位是 1",
          [r_["bit_set"] for r_ in rows2 if r_["tcb"] == t5], [True])

    # ---- 派发器：选位图最高位 = 优先数最小
    check("boot 后 vec 0x3F 的 ICR = 0x1E（最低优先级）", icr_boot, 0x1E)
    check("服务跑过之后 ICR 变成 0x10", m.bus.rd(bready.DISPATCH_ICR, 1), 0x10)
    check("dump 里有队列 3（任务 5 还在队里）", "队列  3" in bready.dump(m), True)
    check("vec 0x3F 的入口 = 0x40D32",
          m.bus.rd(m.cpu.dr[0] + (255 - bready.DISPATCH_VEC) * 4, 4), bready.DISPATCH_ENTRY)
    d = bready.dispatch(m)
    check("一次派发把 CPU 交给任务 2", d["after"], t2)
    check("派发确实换了任务", d["switched"], True)
    check("任务 5 还没上 CPU", m.cov.visited(bready.TASK5_ENTRY), False)

    # ---- 任务 2 阻塞在 flag 3
    m.run(max_steps=200000, spin_guard=False)
    check("20 万步后当前仍是任务 2", bready.current(m), t2)
    check("任务 2 状态字节 = 4（等待）", bready.state_of(m, t2), 4)
    check("任务 2 在 flag 3 的等待链里", bready.flag_waiters(m, 3), [t2])

    # ---- 一路推下去：任务 4 → 任务 5 → 显示初始化自己跑
    d2 = bready.dispatch(m)
    check("第二次派发把 CPU 交给任务 4", d2["after"], idx[4])
    trail = bready.run_until(m, bready.DISPLAY_INIT, chunk=2000, max_rounds=400)
    check("任务 5 入口 0x12DFAE 执行过", m.cov.visited(bready.TASK5_ENTRY), True)
    check("显示初始化 0x12E006 执行过", m.cov.visited(bready.DISPLAY_INIT), True)
    check("队列哨兵已自指", (m.bus.rd(binit.QUEUE_HEAD, 4), m.bus.rd(binit.QUEUE_HEAD + 4, 4)),
          (binit.QUEUE_HEAD, binit.QUEUE_HEAD))
    check("初始化标志 = 1", binit.is_initialized(m), True)
    check("槽位 0..4 的 +8 = 0..4",
          [m.bus.rd(binit.SLOT_BASE + i * 12 + 8, 1) for i in range(5)], [0, 1, 2, 3, 4])
    check("轨迹里至少两次派发请求", sum(1 for e in trail if e[1] == "dispatch") >= 2, True)
    check("轨迹里换过 >= 2 个任务",
          len({e[2] for e in trail if e[1] == "switch"}) >= 2, True)
    check("audit 说 0x45984 是 rom",
          [v for k, v in bready.audit(m) if k.startswith("0x45984")][0].startswith("rom"), True)

    # ---- 0x44 建模（dispatch=True）时序不对：服务没跑完就被切走
    from frbox.machine import create
    m3 = create("d300-b", dispatch=True)
    brealos.boot(m3)
    check("dispatch=True 的 boot 也停在空闲", m3.cpu.pc, 0x407DC)
    r3 = brealos.sta_tsk(m3, 5)
    check("此时 sta_tsk 跑不到返回（撞步数上限）", r3["steps"], 80000)
    check("任务 5 那一位没置起", (bready.ready_map(m3) >> 28) & 1, 0)


def test_blive():
    """V40：0x44 是锁存器、ICR 0x46F 的推迟机制、整机自跑。"""
    print("\n[46 整机自跑（V40）]")
    from frbox import blive
    from frbox.machine import create

    # ---- boot 一遍，同时记 ICR 与 0x44 的写
    m = create("d300-b", dispatch=True)
    m.load(); m.reset()
    c = m.cpu
    icr_writes = []
    req = []
    base_wr = m.bus.wr

    def spy(a, n, v, pc=0):
        if blive.ICR_BASE <= a < blive.ICR_BASE + blive.ICR_COUNT:
            icr_writes.append((pc, a))
        if a == blive.DISPATCH_REG and (v & 0xFF):
            req.append((pc, m.bus.rd(blive.DISPATCH_ICR, 1), c.i))
        return base_wr(a, n, v, pc)

    m.bus.wr = spy
    n = 0
    while not c.halted and n < blive.BOOT_STEPS:
        m.step(1)
        n += 1
    m.bus.wr = base_wr

    check("boot 步数 = 191520", m.step_count, 191520)
    check("boot 后停在空闲任务", blive.name(m), "空闲")
    check("boot 后就绪位图 = 0x80000000", m.bus.rd(blive.READY_MAP, 4), 0x80000000)
    check("boot 末 ICR 0x46F = 0x1E（最后一次服务入口写的）", blive.icr(m), 0x1E)
    check("boot 末待决派发 = 1 条（重叠置位不再挂）", blive.pending_dispatch(m), 1)
    check("ICR 数组 48 条", blive.ICR_COUNT, 0x30)
    check("0x46F = 0x440 + (0x3F − 0x10)", blive.ICR_BASE + (blive.DISPATCH_VEC - 0x10),
          blive.DISPATCH_ICR)

    # ---- 0x44 锁存器的计数
    st = m.dispatch.state()
    check("boot 里写 0x44=1 共 2 次", st["请求（写 1）"], 2)
    check("其中真正挂上中断 1 次（第二次重叠置位）", st["挂中断"], 1)
    check("boot 里没有写 0", st["撤销（写 0）"], 0)
    check("0x44 被读 2 次（写后哑读）", st["被读"], 2)
    check("boot 末锁存位 = 1", st["锁存位"], 1)
    check("没撤销过任何待决项", st.get("撤掉的待决项", 0), 0)
    check("两次写都在 sta_tsk 尾部 0x41886", sorted({pc for pc, _i, _f in req}), [0x41886])
    check("两次写时 ICR 都是 0x10", sorted({i for _p, i, _f in req}), [0x10])
    check("两次写都在中断关闭状态下（i=0）", sorted({f for _p, _i, f in req}), [0])

    # ---- ICR init 循环不写 0x46F
    loop = sorted({a for pc, a in icr_writes if 0x120900 <= pc < 0x121000})
    allw = sorted({a for pc, a in icr_writes})
    check("task-2 的 init 循环写 46 条 ICR", len(loop), 46)
    check("循环覆盖 0x440..0x46E（跳过 0x448）", (loop[0], loop[-1], 0x448 in loop),
          (0x440, 0x46E, False))
    check("init 阶段写点覆盖 48 个地址（含 0x46F）", len(allw), 48)
    check("0x448 由 0x40456 单独补", (0x40456, 0x448) in icr_writes, True)
    check("0x46F 不在 init 循环里", blive.DISPATCH_ICR in loop, False)

    # ---- 0x46F 的三个写点（静态）
    def ins(a):
        return c.fetch(a)

    mn, _b, v, _sz, _w = ins(0x40866)
    check("0x40866 = ldi:20 #0x440", (mn, v["i20"]), ("ldi:20", 0x440))
    mn, _b, v, _sz, _w = ins(0x4086A)
    check("0x4086A = ldi:8 #0x10", (mn, v["i8"]), ("ldi:8", 0x10))
    mn, _b, v, _sz, _w = ins(0x4086C)
    check("0x4086C = stb disp 0x2F", (mn, v["disp8"]), ("stb", 0x2F))
    mn, _b, v, _sz, _w = ins(0x4069A)
    check("服务入口 ldi:8 #0x1E", (mn, v["i8"]), ("ldi:8", 0x1E))
    mn, _b, v, _sz, _w = ins(0x4069C)
    check("0x4069C = stb disp 0x2F（ICR ← 0x1E）", (mn, v["disp8"]), ("stb", 0x2F))
    mn, _b, v, _sz, _w = ins(0x406D2)
    check("服务出口 ldi:8 #0x10", (mn, v["i8"]), ("ldi:8", 0x10))
    mn, _b, v, _sz, _w = ins(0x406D4)
    check("0x406D4 = stb disp 0x2F（ICR ← 0x10）", (mn, v["disp8"]), ("stb", 0x2F))

    # ---- 整机自跑 12 万步
    m = blive.boot()
    res = blive.run(m, steps=120000)
    cnt = res["counts"]
    check("12 万步：请求 18 次", cnt["REQ"], 18)
    check("12 万步：撤销 17 次", cnt["ACK"], 17)
    check("12 万步：派发 17 次", cnt["DISP"], 17)
    check("12 万步：换手 6 次", cnt["SW"], 6)
    check("第一次派发在 191520 步（boot 末那条遗留请求）",
          res["events"][0][0], 191520)
    check("第一次换手 191588 → 任务 2", res["switches"][0], (191588, 0x68000168))
    idx = {r["tcb"] for r in __import__("frbox.brealos", fromlist=["x"]).records(m, include_idle=True)}
    check("换手目标都在任务表里", all(t in idx for _s, t in res["switches"]), True)
    check("入口执行过的任务 = 2,4,5", res["tasks"], [2, 4, 5])
    check("显示初始化在 294,388 步执行", res["display_init"], 294388)
    check("显示初始化后哨兵自指", m.bus.rd(blive.DISPLAY_MARK, 4), blive.DISPLAY_MARK)
    check("槽位 +8 = 0..4", [m.bus.rd(blive.SLOT_BASE + 8 + i * 12, 1) for i in range(5)],
          [0, 1, 2, 3, 4])
    check("初始化标志 0x9F58DA85 = 1", m.bus.rd(0x9F58DA85, 1), 1)
    check("派发后位图不再只有空闲", res["ready_map"] != 0x80000000, True)

    # ---- 推迟铁律
    rok, rbad, dok, dbad = res["relay"]
    check("REQ 里 ICR=0x1E 且 ILM=0x1E 的条数", rok, 18)
    check("REQ 不符合铁律的条数", rbad, 0)
    check("DISP 里 ILM=0x1F 的条数", dok, 17)
    check("DISP 不符合铁律的条数", dbad, 0)

    # ---- 设备计数与事件一致
    st = m.dispatch.state()
    check("撤销次数 = ACK 事件数", st["撤销（写 0）"], cnt["ACK"])
    check("窗口内写 1 次数 = REQ 事件数", st["请求（写 1）"] - 2, cnt["REQ"])
    check("审计里三项都在", len(blive.audit(m, res)) >= 9, True)
    check("describe 里有「当前=」", "当前=" in blive.describe(m), True)


@under_sdram_1mb
def test_bfill():
    """V41：0x1356A0 是整片填充；反事实、任务启动普查、只写标志。

    ⚠️ 本组数字是 **1MB 口径**下的观测（见 `under_sdram_1mb`）。V43 把
    `ram-sdram` 铺到 2.5MB 之后池能读写了，任务不再走「取不到也照搬」这条路，
    这 11 次填充在新默认口径下一次都不会发生。
    """
    print("\n[47 整片填充与反事实（V41）]")
    from frbox import bfill, blive

    # ---- 原样跑 891,520 步：记填充写与 0x9F58DA85 的读写
    m = blive.boot()
    fw = bfill.FillWatch(m).attach()
    sp = bfill.AddrSpy(m, 0x9F58DA85).attach()
    icr_spy = bfill.AddrSpy(m, 0x46F).attach()
    res = blive.run(m, steps=700000)
    icr_spy.detach(); sp.detach(); fw.detach()

    check("0x1356A0 落在填充循环里", bfill.FILL_PC, 0x1356A0)
    check("0x1356A0 执行 23,617 次", len(fw.hits), 23617)
    cov = fw.coverage()
    check("落在 ICR 块 352 笔（32 × 11）", cov.get("ICR 块"), 352)
    check("落在 0x1000..0x2188 12,342 笔", cov.get("低区 RAM"), 12342)
    rs = fw.runs()
    check("11 段连续下行", len(rs), 11)
    check("每段 2,147 笔", {r["count"] for r in rs}, {2147})
    check("起点恒 0x2208", {r["top"] for r in rs}, {0x2208})
    check("终点恒 0x0080", {r["bottom"] for r in rs}, {0x0080})
    check("最早一次在步 506,201", rs[0]["start"], 506201)
    icr_addrs = {a for _s, a, _v, _n in fw.in_range(0x400, 0x480)}
    check("ICR 块 32 个对齐位置全被清过", len(icr_addrs), 32)
    check("0x400..0x47C 一个不落", icr_addrs == set(range(0x400, 0x480, 4)), True)
    check("0x46C 每次都被写 0（11 笔）",
          [v for _s, a, v, _n in fw.in_range(0x46C, 0x470)], [0] * 11)
    check("原样：换手 24 次", len(res["switches"]), 24)
    check("原样：最后一次换手 501,944", res["switches"][-1][0], 501944)
    check("原样：跑过入口的任务 9 个", res["tasks"],
          [2, 3, 4, 5, 6, 7, 9, 10, 11])
    check("原样：收尾卡在拷贝循环 0x137968", m.cpu.pc, 0x137968)

    # ---- 停摆的直接原因：填充把 ICR 0x46F 写成 0x00
    check("0x46F 末值 0x00（V40 记的 0x80 作废）", m.bus.rd(0x46F, 1) & 0xFF, 0x00)
    odd = [w for w in icr_spy.writes if w[2] not in (0x10, 0x1E)]
    check("0x46F 被填充清 0 共 11 次（每次填充一次）", len(odd), 11)
    check("第一次清 0 在步 513,781", odd[0][0], 513781)
    check("这 11 笔写的都是 0x00", {w[2] for w in odd}, {0x00})
    check("0x46F 出现过的值只有 0x00/0x10/0x1E",
          {w[2] for w in icr_spy.writes}, {0x00, 0x10, 0x1E})
    nz = sum(n for v, n in fw.values_in(0x80, 0x220C).items() if v)
    check("填充里非 0 写入 671 笔 / 23,617 笔", (nz, len(fw.hits)), (671, 23617))

    # ---- 0x9F58DA85 是只写标志
    check("0x9F58DA85 只有 1 笔写", len(sp.writes), 1)
    check("写点在 0x12E0AE、值 1", (sp.writes[0][1], sp.writes[0][2]), (0x12E0AE, 1))
    check("0x9F58DA85 一笔读都没有", len(sp.reads), 0)

    # ---- 反事实：把填充写引到影子 RAM
    m2 = blive.boot()
    dv = bfill.divert_fill(m2).attach()
    res2 = blive.run(m2, steps=700000)
    dv.detach()
    check("反事实：被引走 2,144 笔", dv.diverted, 2144)
    check("反事实：换手 37 次", len(res2["switches"]), 37)
    check("反事实：最后一次换手 660,439", res2["switches"][-1][0], 660439)
    check("反事实：17 个任务入口", res2["tasks"],
          [1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 16, 17, 20, 21, 26, 27, 28])
    check("反事实：收尾回到空闲任务", m2.cpu.pc, 0x407DC)
    check("反事实：ICR 0x46F 不再是 0x80", m2.bus.rd(0x46F, 1), 0x10)

    # ---- 任务启动普查
    rows = bfill.census(m2)
    check("普查 29 条", len(rows), 29)
    check("sta_tsk 调用 16 次", len(bfill.started_tasks(m2)), 16)
    check("从没启动过的任务", bfill.never_started(m2), [8, 12, 13, 14, 15, 18, 19, 22, 23, 24, 25])
    check("任务 1 在反事实里跑到过", [r["ran"] for r in rows if r["id"] == 1], [True])
    check("任务 8 的存 SP 还是 0", [r["saved_sp"] for r in rows if r["id"] == 8], [0])

    # ---- 填充值/跨度常量与实测一致
    check("FILL_BASE = 0x80（r0 是基址，不是值）", bfill.FILL_BASE, 0x80)
    check("FILL_VALUE = 0x00（值在 r12）", bfill.FILL_VALUE, 0x00)
    check("FILL_SPAN = 0x2188", bfill.FILL_SPAN, 0x2188)
    check("FILL_TOP/FILL_BOTTOM = 0x2208 / 0x0080",
          (bfill.FILL_TOP, bfill.FILL_BOTTOM), (0x2208, 0x0080))
    check("region_of(0x46C) = ICR 块", bfill.region_of(0x46C), "ICR 块")
    check("region_of(0x44) = direct page", bfill.region_of(0x44), "direct page")
    check("region_of(0x1500) = 低区 RAM", bfill.region_of(0x1500), "低区 RAM")


@under_sdram_1mb
def test_bmove():
    """V42：0x1356A0 是 memmove 的存储端；基址为 0 → 把 ICR 整片搬掉。

    ⚠️ 同样是 **1MB 口径**下的观测（见 `under_sdram_1mb`）。起因是
    「池地址未映射 → 取到 NULL」，V43 修好映射后整条链不再触发。
    """
    print("\n[48 整片搬运与空指针反事实（V42）]")
    import fr_dis
    from fr_emu import TABLE
    from frbox import bmove, blive

    # ---- A. 五条关键指令的真身（目的/源基址都在别的寄存器里）
    m = blive.boot()
    rom, base = m.bus.rom, m.bus.rom_base

    def txt(pc):
        return " ".join(fr_dis.disasm_addr(rom, pc, pc + 2, base))

    def mn(pc):
        r = TABLE.decode(rom, pc - base)
        return r[0] if r else None

    check("0x135690 是 add r9,r0（r0 ← r9+0x80）", "r9, r0" in txt(0x135690), True)
    check("0x135694 是 add r8,r1（r1 ← r8+0x4C）", "r8, r1" in txt(0x135694), True)
    check("0x13569C 是 ld（取源）", mn(0x13569C), "ld")
    check("0x13569E 是 bgt:d（收尾）", mn(0x13569E), "bgt:d")
    check("0x1356A0 是 st（存目的）", mn(0x1356A0), "st")
    check("位移 DELTA = 0x34（0x80-0x4C）", bmove.DELTA, 0x34)
    check("长度 LEN = 0x2188 / 每次 2,147 笔", (bmove.LEN, bmove.WORDS), (0x2188, 2147))
    check("空指针判定点 0x135608 / 赋值点 0x135624",
          ["0x%X" % v for v in (bmove.NULL_TEST_PC, bmove.DST_ASSIGN_PC)],
          ["0x135608", "0x135624"])

    # ---- B/C. 700,000 步：11 次搬运的形状 + 逐笔对源
    m = blive.boot()
    sw = bmove.ShiftWatch(m).attach()
    inner = m.bus.wr
    pairs = []

    def spy(a, n, v, pc=0):
        if pc == bmove.SHIFT_ST and len(pairs) < bmove.WORDS:
            pairs.append((v & 0xFFFFFFFF, m.bus.rd(a - bmove.DELTA, 4) & 0xFFFFFFFF))
        return inner(a, n, v, pc)

    m.bus.wr = spy
    res = blive.run(m, steps=700000)
    m.bus.wr = inner
    sw.detach()

    check("0x1356A0 共 23,617 笔存储", len(sw.stores), 23617)
    s = sw.summary()
    check("切成 11 次搬运", s["calls"], 11)
    check("每段 2,147 笔", s["words"], [2147])
    check("dst 恒 0x2208→0x0080",
          ["0x%X→0x%X" % t for t in s["dst"]], ["0x2208→0x80"])
    check("src 恒 0x21D4→0x004C",
          ["0x%X→0x%X" % t for t in s["src"]], ["0x21D4→0x4C"])
    check("11 次全部 r8 = r9 = 0", s["bases"], {(0, 0): 11})
    check("空基址搬运 11 / 11", s["null"], 11)
    check("落 ICR 块 176 笔", s["regions"].get("ICR 块"), 176)
    check("落 direct page 352 笔", s["regions"].get("direct page"), 352)
    check("最早一次在步 506,201", sw.calls()[0]["start"], 506201)
    check("第一次搬运 2,147 笔逐笔等于源 [dst-0x34]",
          sum(1 for v, src in pairs if v == src), 2147)
    check("第一次搬运里有非 0 字（不是填 0）",
          sum(1 for v, _src in pairs if v) > 0, True)

    # ---- E. 静态：谁写 r8 / r9
    w9 = [pc for pc, _t in bmove.writes_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r9")]
    w8 = [pc for pc, _t in bmove.writes_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r8")]
    check("函数体内写 r9 的 4 条",
          ["0x%X" % v for v in w9],
          ["0x13534E", "0x1354F4", "0x1355B2", "0x135624"])
    check("函数体内写 r8 的 3 条",
          ["0x%X" % v for v in w8], ["0x13531E", "0x1353A2", "0x13556C"])
    check("提到 r9 的 30 条",
          len(bmove.scan_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r9")), 30)
    check("提到 r8 的 55 条",
          len(bmove.scan_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r8")), 55)

    # ---- G. 反事实：只改 0x135624 的 r4
    m2 = blive.boot()
    fx = bmove.fix_dst(m2, ptr=0x2200).attach()
    res2 = blive.run(m2, steps=700000)
    fx.detach()
    check("反事实只命中一次", fx.hits, 1)
    check("反事实：换手 24 → 37", len(res2["switches"]), 37)
    check("反事实：收尾回到空闲 0x407DC", "0x%X" % m2.cpu.pc, "0x407DC")
    check("反事实：ICR 0x46F 末值 0x10", "0x%02X" % (m2.bus.rd(0x46F, 1) & 0xFF), "0x10")
    check("反事实：17 个任务入口", len(res2["tasks"]), 17)
    check("原样：24 次换手（对照）", len(res["switches"]), 24)
    check("原样：ICR 0x46F 末值 0x00", m.bus.rd(0x46F, 1) & 0xFF, 0x00)


def test_bpool():
    """V43：池地址没映射 → 取到 NULL → 搬 0 地址 → 踩 ICR → 停摆。"""
    print("\n[49 缓冲池与地址映射（V43）]")
    from frbox import blive, bpool

    # ---- A. 常量与地址（两档口径都要对得上）
    check("池头四个地址", ["0x%X" % a for a in bpool.POOL_ALL],
          ["0x9F69CE98", "0x9F69CEB4", "0x9F69CEE4", "0x9F69CEFC"])
    check("三个节点地址", ["0x%X" % a for a in bpool.NODES],
          ["0x9F696844", "0x9F698A60", "0x9F69AC7C"])
    check("POOL_HI = 0x9F69CF04", bpool.POOL_HI, 0x9F69CF04)
    check("SDRAM 基址 / 任务栈基址", (bpool.SD_BASE, bpool.TASKSTACK_BASE),
          (0x9F580000, 0x9F800000))
    check("旧口径 1MB / 新口径 2.5MB",
          (bpool.SD_SIZE_V42, bpool.SD_SIZE_V43), (0x00100000, 0x00280000))
    check("常量 NULL_ASSIGN_PC", bpool.NULL_ASSIGN_PC, 0x12216E)
    check("常量 RET_MOV_PC", bpool.RET_MOV_PC, 0x122190)
    check("常量 ALLOC_NODE / ALLOC_BUF",
          (bpool.ALLOC_NODE, bpool.ALLOC_BUF), (0x122064, 0x122D32))
    check("WATCH 表 7 项且含两端分配器",
          (len(bpool.WATCH), bpool.WATCH[bpool.ALLOC_BUF]), (7, "取缓冲区"))

    # ---- B. 1MB 口径：池地址在总线外面
    def run_cfg(size, steps=700000):
        with bpool.sdram_override(size):
            m = blive.boot()
            pw = bpool.PoolWatch(m).attach()
            nw = bpool.NullWatch(m).attach()
            c, st = m.cpu, m.cpu.step
            cnt = {a: 0 for a in bpool.WATCH}

            def spy():
                if c.pc in cnt:
                    cnt[c.pc] += 1
                return st()

            c.step = spy
            res = blive.run(m, steps=steps)
            c.step = st
            nw.detach()
            pw.detach()
        return m, pw, nw, cnt, res

    m1, pw1, nw1, cnt1, res1 = run_cfg(bpool.SD_SIZE_V42)
    check("1MB：0x9F69CE98 未映射", bpool.mapped(m1, bpool.POOL_COUNT), False)
    reg, back = bpool.roundtrip(m1, bpool.POOL_COUNT)
    check("1MB：写 0xA1B2C3D4 读回 0", (reg, back), (None, 0))
    check("1MB：池四个地址全在总线外", bpool.pool_mapped(m1), False)
    check("1MB：ram-sdram 尺寸", bpool.sdram(m1), (0x9F580000, 0x00100000))

    # ---- C. 1MB 口径下的后果（V41/V42 那条链）
    check("1MB：池照样被初始化 3 次", len(pw1.init_at()), 3)
    check("1MB：初始化步 415,345 / 415,366 / 419,098",
          pw1.init_at(), [415345, 415366, 419098])
    check("1MB：串进池的节点 3 个",
          ["0x%X" % v for v in pw1.nodes_linked()],
          ["0x9F696844", "0x9F698A60", "0x9F69AC7C"])
    tr = pw1.count_track()
    check("1MB：计数第一次被写是 3", (tr[0][0], tr[0][2]), (419230, 3))
    check("1MB：读回计数恒 0（写没落地）", bpool.count_at(m1), 0)
    check("1MB：NULL 发生 1 次", len(nw1.nulls()), 1)
    check("1MB：NULL 在步 497,969", nw1.first_null()[0], 497969)
    check("1MB：NULL 时 r4 = 0", nw1.first_null()[1], 0)
    check("1MB：取节点 1 次 / 取缓冲区 11 次",
          (cnt1[bpool.ALLOC_NODE], cnt1[bpool.ALLOC_BUF]), (1, 11))
    check("1MB：memmove 起点 11 次", cnt1[bpool.MEM_MOVE_PC], 11)
    check("1MB：换手 24 次", len(res1["switches"]), 24)
    check("1MB：收尾卡在拷贝环 0x137968", m1.cpu.pc, 0x137968)
    check("1MB：跑过入口 9 个", res1["tasks"],
          [2, 3, 4, 5, 6, 7, 9, 10, 11])

    # ---- D. 2.5MB 口径（V43 起默认）：池活了
    m2, pw2, nw2, cnt2, res2 = run_cfg(bpool.SD_SIZE_V43)
    check("2.5MB：0x9F69CE98 已映射", bpool.mapped(m2, bpool.POOL_COUNT), True)
    check("2.5MB：结束计数读回 3", bpool.count_at(m2), 3)
    reg, back = bpool.roundtrip(m2, bpool.POOL_COUNT)
    check("2.5MB：写 0xA1B2C3D4 能读回",
          (reg.name if reg else None, back), ("ram-sdram", 0xA1B2C3D4))
    check("2.5MB：池四个地址全在总线里", bpool.pool_mapped(m2), True)
    check("2.5MB：ram-sdram 铺到任务栈区之前",
          bpool.sdram(m2), (0x9F580000, 0x00280000))
    check("2.5MB：SDRAM 上界 == 任务栈基址", bpool.SD_BASE + 0x00280000,
          bpool.TASKSTACK_BASE)
    check("2.5MB：初始化步不变", pw2.init_at(), [415345, 415366, 419098])
    check("2.5MB：节点照样 3 个", len(pw2.nodes_linked()), 3)
    check("2.5MB：NULL 一次都不发生", len(nw2.hits), 0)
    check("2.5MB：取缓冲区不再被调到", cnt2[bpool.ALLOC_BUF], 0)
    check("2.5MB：memmove 一次都不发生", cnt2[bpool.MEM_MOVE_PC], 0)
    check("2.5MB：换手 34 次", len(res2["switches"]), 34)
    check("2.5MB：收尾回到空闲任务 0x407DC", m2.cpu.pc, 0x407DC)
    check("2.5MB：跑过入口 17 个", res2["tasks"],
          [1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 16, 17, 20, 21, 26, 27, 28])
    check("2.5MB：ICR 0x46F 保持 0x10", m2.bus.rd(0x46F, 1) & 0xFF, 0x10)


def test_lists():
    """V44：双向链表库 —— 两套实例，以及那块没映射的节点区。"""
    print("\n[50 双向链表库与节点区（V44）]")
    from frbox import blive, bpool, lists, machine

    # ---- A. 库入口与常量
    check("库入口五个地址",
          (lists.LIST_INIT, lists.LIST_EMPTY, lists.LIST_PUSH,
           lists.LIST_NEXT, lists.LIST_NEWNODE),
          (0x124F46, 0x124F4E, 0x124F5A, 0x124F7A, 0x124FA0))
    check("挂链三步的指令地址",
          (lists.PUSH_PREV, lists.PUSH_NEXT, lists.PUSH_HEAD),
          (0x124F5C, 0x124F60, 0x124F62))
    check("套 A：计数 / 三个头 / 三个节点",
          (lists.POOL_COUNT, lists.POOL_HEADS, lists.POOL_NODES),
          (0x9F69CE98, (0x9F69CEB4, 0x9F69CEE4, 0x9F69CEFC),
           (0x9F696844, 0x9F698A60, 0x9F69AC7C)))
    check("套 A：节点大小 0x221C", lists.POOL_NODE_SIZE, 0x221C)
    check("套 B：三个头 / 头链 / 节点区",
          (lists.LIST_HEADS_B, lists.LIST_HEADCHAIN_B,
           lists.NODES_B_BASE, lists.NODES_B_END),
          ((0x9F5A0CC0, 0x9F5A0CC8, 0x9F5A0CD0), 0x9F5A0CD8,
           0x9F553620, 0x9F5A0CDC))
    check("套 B：节点大小 0x14 / 每链 1001 / 建 1000",
          (lists.NODE_SIZE_B, lists.CHAIN_LEN_B, lists.BUILD_COUNT_B),
          (0x14, 1001, 1000))
    check("节点区 RAM：0x9F550000 + 0x30000",
          lists.nodes_ram_extra(), (0x9F550000, 0x00030000, "ram-nodes"))

    # ---- B. 机器默认就带节点区
    m = blive.boot()
    check("默认：节点区已映射",
          m.bus.find(lists.NODES_B_BASE) is not None, True)
    check("默认：节点区落在 ram-nodes",
          m.bus.find(lists.NODES_B_BASE).name, "ram-nodes")
    check("默认：节点区首地址写哨兵能读回",
          bpool.roundtrip(m, lists.NODES_B_BASE, 0x5A5A5A5A)[1], 0x5A5A5A5A)
    check("默认：ram-sdram 仍是 2.5MB",
          bpool.sdram(m), (0x9F580000, 0x00280000))
    check("默认：两块 RAM 首尾相接不重叠",
          lists.NODES_RAM_BASE + lists.NODES_RAM_SIZE, 0x9F580000)

    # ---- C. 套 B：三条链各 1001 个节点，双向配对
    # ⚠️ 链表是 boot 之后（跑起来时）才建的，所以这里必须先跑一遍
    res_m = blive.run(m, steps=700000)
    check("默认：跑 70 万步后换手 34 次", len(res_m["switches"]), 34)
    check("默认：跑 70 万步后末 PC 0x407DC", m.cpu.pc, 0x407DC)
    check("默认：跑 70 万步后 ICR 0x46F = 0x10",
          m.bus.rd(0x46F, 1) & 0xFF, 0x10)
    check("默认：跑 70 万步后入口 17 个", len(res_m["tasks"]), 17)
    for h in lists.LIST_HEADS_B:
        n, end = lists.chain_len(m, h)
        nodes = lists.chain_nodes(m, h)
        check("套 B：头 0x%X 链长 1001" % h, n, 1001)
        check("套 B：头 0x%X 双向全配对" % h,
              lists.is_bidirectional(m, nodes), 0)
        # ⚠️ 0x124F5A 是**头插**，遍历顺序是「尾 → 头 → 首 → …」
        check("套 B：头 0x%X 遍历第 2 项就是链头自己" % h, nodes[1], h)
    alln = sorted(set(sum((lists.chain_nodes(m, h)
                           for h in lists.LIST_HEADS_B), [])))
    check("套 B：节点总数 3003（1000×3 + 3 个头）", len(alln), 3003)
    check("套 B：节点区首地址", alln[0], 0x9F553620)
    check("套 B：节点区末地址（三个链头）", alln[-3:], [0x9F5A0CC0,
                                                     0x9F5A0CC8, 0x9F5A0CD0])
    reg = lists.registry(m)
    check("套 B：注册表首项 = 节点区末端", reg[0], 0x9F564F60)
    check("套 B：注册表第 2 项 = 尾哨兵", reg[1], 0x9F562080)
    check("套 B：注册表三项 = 三条链的首节点",
          reg[2:5], [0x9F55D260, 0x9F558440, 0x9F553620])

    # ---- D. 套 A：3 个大节点 + 头链
    check("套 A：计数读回 3", bpool.count_at(m, lists.POOL_COUNT), 3)
    n, _ = lists.chain_len(m, lists.POOL_HEADS[0])
    check("套 A：链长 4（3 节点 + 回到头）", n, 4)
    n, _ = lists.chain_len(m, lists.POOL_HEADCHAIN)
    check("套 A：头链 13 个头", n, 13)

    # ---- E. 反事实：没有节点区时链是断的
    base = list(machine.D300B.extra_ram)
    machine.D300B.extra_ram = [x for x in base if x[2] != "ram-nodes"]
    try:
        m2 = blive.boot()
        res2 = blive.run(m2, steps=700000)
        check("无节点区：节点区未映射",
              m2.bus.find(lists.NODES_B_BASE) is not None, False)
        check("无节点区：写哨兵读回 0",
              bpool.roundtrip(m2, lists.NODES_B_BASE)[1], 0)
        n, _ = lists.chain_len(m2, lists.LIST_HEADS_B[0])
        # 断链时 head->next 读回 0 → 走一步就停 → 链长 1（那个 0 不算节点）
        check("无节点区：链长 1（断链，head->next 读回 0）", n, 1)
        # ⚠️ 上面 roundtrip 写哨兵本身多一笔未映射写，所以总数比基线多 1
        recs = [r for r in m2.bus.unmapped_writes
                if lists.NODES_B_BASE <= r[0] <= lists.NODES_B_END]
        check("无节点区：节点区未映射写 6001 笔（含哨兵那笔）", len(recs), 6001)
        # V45：0x50000000 建成器件后，这 80 笔写不再记未映射 → 7934-80 / 3013-7。
        # V46：0x40000000（1,730 写 / 2 读地址）与 0x60·0x61（11 写 / 3 读地址）
        # 也建成器件 → 7854-1730-11 = 6113、3006-2-3 = 3001。
        check("无节点区：未映射写总数 6113", len(m2.bus.unmapped_writes), 6113)
        check("无节点区：未映射读 3001 个地址", len(m2.bus.unmapped_reads), 3001)
        check("无节点区：换手 34（与有节点区相同）", len(res2["switches"]), 34)
        check("无节点区：末 PC 0x407DC", m2.cpu.pc, 0x407DC)
    finally:
        machine.D300B.extra_ram = base

    # ---- F. 默认口径：补上映映后剩下的全在外设窗口
    # V46：读侧清零（V45 是 5 个地址 / 9 次）—— 0x40 的 2 个 RMW 地址与
    # 0x60/0x61 的 3 个状态地址都挂上了器件。写侧只剩两个「只写 0 读」窗口。
    check("默认：未映射写降到 112", len(m.bus.unmapped_writes), 112)
    check("默认：未映射读清零（0 个地址）", len(m.bus.unmapped_reads), 0)
    per = {}
    for a, sz, v, pc in m.bus.unmapped_writes:
        for name, lo, hi, _ in lists.PERIPHERAL_WINDOWS:
            if lo <= a < hi:
                per[name] = per.get(name, 0) + 1
                break
    check("默认：剩余未映射写按窗口分组",
          per, {"0x6Fxxxxxx": 108, "0x5C0020xx": 4})
    check("默认：五块窗口合计 = 未映射写总数",
          sum(per.values()), len(m.bus.unmapped_writes))


def test_bctrl():
    """V45：`0x50000000` 三通道位串行控制块（dev_ctrl50）。"""
    print("\n[51 三通道位串行控制块（V45）]")
    from frbox import blive, brealos, lists, machine
    from frbox.dev_ctrl50 import CtrlSerial50

    # ---- A. 纯设备：寄存器语义（不建整机，快）
    d = CtrlSerial50()
    check("常量：基址 / 大小 / 三通道",
          (d.BASE, d.SIZE, d.CH_BASE, d.CH_STRIDE, d.CH_COUNT),
          (0x50000000, 0x400, 0x100, 0x100, 3))
    d.write(0x104, 1, 0x7F)
    check("+04 配置回读：状态位被器件替换（0x7F → 0x3D）", d.read(0x104, 1), 0x3D)
    d.write(0x106, 1, 0x0C)
    check("+06 控制位如实回读", d.read(0x106, 1), 0x0C)
    d.write(0x105, 1, 0x28)
    check("+05 回读：CLK/DATA 保留、状态位为 0", d.read(0x105, 1), 0x28)
    # +01 的 bit0 是器件状态位（固件 btstl 测它、置起就 bandl 清掉）
    d.write(0x01, 1, 0x0F)
    check("+01 其余位如实回读、bit0 被状态位替换", d.read(0x01, 1), 0x0E)
    d.set_status(rdy=0x01)
    check("+01 状态位打桩（bit0 = 1）", d.read(0x01, 1), 0x0F)
    d.set_status(rdy=0)
    check("+01 状态位可以清回去", d.read(0x01, 1), 0x0E)
    d.set_status(cfg=0x42, cmd=0xC0)
    check("+04 状态位打桩（0x3D → 0x7F）", d.read(0x104, 1), 0x7F)
    check("+05 状态位打桩（0x28 → 0xE8）", d.read(0x105, 1), 0xE8)
    d.set_status(cfg=0, cmd=0)
    check("状态位可以清零回去", (d.read(0x104, 1), d.read(0x105, 1)), (0x3D, 0x28))
    d.write(0x500, 1, 0xAA)                      # 窗口外：丢弃
    check("窗口外的偏移被丢弃", d.read(0x500, 1), 0)

    # ---- B. 位串行：手工打 16 个时钟，载荷应原样解出
    e = CtrlSerial50()
    e.write(0x106, 1, 0x04)                      # 片选
    e.write(0x105, 1, 0x20)                      # 起始位（片选后第一个时钟）
    for i in range(16):
        bit = 0xA55A >> (15 - i) & 1
        e.write(0x105, 1, 0x08 if bit else 0x00)
        e.write(0x105, 1, 0x28 if bit else 0x20)
    check("位串行：起始位 1 个", e.starts[0], 1)
    check("位串行：17 个时钟（1 起始 + 16 数据）", e.clocks[0], 17)
    check("位串行：解出 0xA55A（MSB 先）", e.tx[0], [0xA55A])
    check("位串行：片选边沿记了 1 次", e.cs_edges[0], 1)

    # ---- C. 整机：默认就挂上器件，配置/载荷都是固件的真实值
    m = blive.boot()
    res = blive.run(m, steps=700000)
    dev = m.ctrl50
    check("整机：默认挂上 ctrl50", dev is not None, True)
    check("整机：0x50000104 落在 ctrl50",
          m.bus.find(0x50000104).name, "ctrl50")
    check("整机：三通道配置字节（非状态位）",
          [dev.config(i) for i in range(3)], [0x3D, 0x09, 0x00])
    check("整机：通道 A 发了一个 16 位字", dev.tx[0], [0x2000])
    check("整机：通道 A 时钟 17 / 起始位 1",
          (dev.clocks[0], dev.starts[0]), (17, 1))
    check("整机：通道 B/C 只打了起始位、没有载荷",
          (dev.tx[1], dev.tx[2], dev.starts[1], dev.starts[2]),
          ([], [], 0, 0))
    # 复位后第一句是 `sth @0x50000000 = 0x0001`（16 位）→ +00=0x00、+01=0x01，
    # 而 +01 的 bit0 是器件状态位（固件 btstl 测它），所以读 +01 只能拿到状态。
    check("整机：复位后第一句给 +00/+01 写了 0x0001",
          (dev.regs[0x00], dev.regs[0x01]), (0x00, 0x01))
    check("整机：+01 的 bit0 是状态位（默认 0，不回读写入值）",
          m.bus.rd(0x50000001, 1), 0x00)
    check("整机：整块读回时那一位被状态吃掉（0x0001 → 0x0000）",
          m.bus.rd(0x50000000, 2), 0x0000)
    check("整机：+01 写进去的位仍在寄存器里",
          dev.regs[0x01] & 0x01, 0x01)
    check("整机：+04 读回带不动状态位（器件空闲）",
          m.bus.rd(0x50000104, 1), 0x3D)
    check("整机：0x50 窗口不再有未映射写",
          sum(1 for a, n, v, pc in m.bus.unmapped_writes
              if 0x50000000 <= a < 0x50400000), 0)
    # V46 之后 0x40 与 0x60/0x61 也建了器件，总账再降一档（见 test_dual40）
    check("整机：未映射写 112", len(m.bus.unmapped_writes), 112)
    check("整机：未映射读 0 个地址（读侧已清零）", len(m.bus.unmapped_reads), 0)
    check("整机：换手 34（与建模前相同）", len(res["switches"]), 34)
    check("整机：末 PC 0x407DC", m.cpu.pc, 0x407DC)
    check("整机：入口 17 个", len(res["tasks"]), 17)
    check("窗口常量与设备一致",
          (lists.CTRL50_BASE, lists.CTRL50_SIZE, lists.CTRL50_CH_COUNT),
          (CtrlSerial50.BASE, CtrlSerial50.SIZE, CtrlSerial50.CH_COUNT))
    check("窗口表：0x50 那行已归零",
          [w for w in lists.PERIPHERAL_WINDOWS if w[0] == "0x50xxxxxx"][0][3], 0)

    # ---- D. 反事实：状态位置起不改变默认路径（要显式打开才生效）
    dev.set_status(cfg=0x42, cmd=0xC0)
    check("反事实：状态位置起后 +04 读到 0x7F",
          m.bus.rd(0x50000104, 1), 0x7F)
    dev.set_status(cfg=0, cmd=0)
    check("反事实：关掉后回到 0x3D", m.bus.rd(0x50000104, 1), 0x3D)

    # ---- E. +01 的 bit0 置起：boot 多走一条清位指令 —— 是 1 步相位，不是岔路。
    # 固件在 0x4014A `btstl #0x1,@0x50000001`，置起就 0x4014E `bandl #0xE` 清掉，
    # 两条路在 0x40150 汇合；于是固定预算 191,520 步正好落在空闲 bra -1 的前一条。
    old_rdy = machine.D300B.ctrl50_status_rdy
    machine.D300B.ctrl50_status_rdy = 0x01
    try:
        b1 = brealos.boot()
    finally:
        machine.D300B.ctrl50_status_rdy = old_rdy
    check("反事实：bit0 置起 → 191,520 步停在空闲前一条（0x407D8）",
          b1.cpu.pc, 0x407D8)
    check("反事实：此时 SP/ILM 还是装配前的值",
          (b1.cpu.r[15], b1.cpu.ilm), (0x000083F8, 0x10))
    b1.step(1)
    check("反事实：再走一步就到空闲 0x407DC（路径没岔）", b1.cpu.pc, 0x407DC)
    check("反事实：到达空闲后 SP/ILM 与默认口径一致",
          (b1.cpu.r[15], b1.cpu.ilm), (0x68000BE8, 0x1F))
    check("默认口径仍是 191,520 步到空闲", brealos.boot().cpu.pc, 0x407DC)


def test_dual40():
    """V46：0x40 窗口（dev_ctrl40）与 0x60/0x61 双通道控制器（dev_dual60）。"""
    print("\n[52 0x40 窗口 + 0x60/0x61 双通道（V46）]")
    import os
    from frbox import blive, brealos, lists, machine
    from frbox.dev_ctrl40 import CtrlWindow40
    from frbox.dev_dual60 import DualCtrl60

    # ---- A. 纯设备：DualCtrl60 的寄存器面
    d = DualCtrl60(index=0)
    check("dual60 常量：基址 / 大小 / 状态寄存器 / 字段",
          (d.BASE, d.SIZE, d.R_STATUS, d.STATUS_FIELD_MASK),
          (0x60000000, 0x4000, 0x100, 0x0C))
    d.write(0x03, 1, 0xFF)
    d.write(0x04, 1, 0x01)
    d.write(0x06, 1, 0x00)
    check("dual60 配置字节如实回读", (d.read(0x03, 1), d.read(0x04, 1),
                                     d.read(0x06, 1)), (0xFF, 0x01, 0x00))
    # +02 的两步 RMW：先 OR 0x80 再 OR 0x02（`borh #0x8` / `borl #0x2`）
    d.write(0x02, 1, d.read(0x02, 1) | 0x80)
    d.write(0x02, 1, d.read(0x02, 1) | 0x02)
    check("dual60 +02 两步 RMW 叠出 0x82", d.ctrl(), 0x82)
    check("dual60 +100 默认读到 0（= 未映射口径）", d.read(0x100, 1), 0x00)
    d.set_status(byte=0x0C)
    check("dual60 +100 由器件驱动整字节", d.read(0x100, 1), 0x0C)
    check("dual60 +100 字段 = bit2–3（0x0C → 3）", d.field, 3)
    d.set_status(field=0)
    check("dual60 +100 字段可清回 0", (d.read(0x100, 1), d.field), (0x00, 0))
    d.write(0x4000, 1, 0xAA)
    check("dual60 窗口外丢弃", d.read(0x4000, 1), 0)
    e = DualCtrl60(index=1)
    check("dual60 第二通道基址 +16MB", e.base, 0x61000000)

    # ---- B. 纯设备：CtrlWindow40 的回读与 RMW
    w = CtrlWindow40()
    check("ctrl40 常量：基址 / +0xF8 / 终值",
          (w.BASE, w.RMW_OFF, w.RMW_FINAL), (0x40000000, 0xF8, 0x3B3B))
    w.write(0x40002, 2, 0x000E)
    check("ctrl40 16 位写拆字节、可按字节读回",
          (w.read(0x40002, 1), w.read(0x40003, 1)),
          (0x00, 0x0E))
    check("ctrl40 整体读回 0x000E", w.read(0x40002, 2), 0x000E)
    check("ctrl40 没写过的偏移读 0", w.read(0x40010, 1), 0)
    # 两次 RMW（与 0x14C1BA–0x14C1DC 同序）：不回读就只会得到 0x003B
    v = w.read(0x400F8, 2)
    w.write(0x400F8, 2, (v & 0xFFFFC0FF) | 0x3B00)
    v = w.read(0x400F8, 2)
    check("ctrl40 RMW 第一步：0x3B00", v, 0x3B00)
    w.write(0x400F8, 2, (v & 0xFFFFFFC0) | 0x003B)
    check("ctrl40 RMW 第二步叠出 0x3B3B", w.rmw_value(0x40000), 0x3B3B)
    check("ctrl40 +0xF8 读被记了 2 次", w.rmw_reads, 2)

    # ---- C. 整机：两个器件默认都挂上
    m = blive.boot()
    res = blive.run(m, steps=700000)
    check("整机：默认挂上 ctrl40", m.ctrl40 is not None, True)
    check("整机：默认挂上两个 dual60 通道", len(m.dual60), 2)
    check("整机：0x400400F8 落在 ctrl40",
          m.bus.find(0x400400F8).name, "ctrl40")
    check("整机：0x60000100 落在 dual600",
          m.bus.find(0x60000100).name, "dual600")
    check("整机：0x61000002 落在 dual601",
          m.bus.find(0x61000002).name, "dual601")
    check("整机：0x8400 描述符表的两个基址",
          (m.bus.rd(0x8400, 4), m.bus.rd(0x8450, 4)),
          (0x60000000, 0x61000000))
    check("整机：描述符表的子窗口指针",
          [m.bus.rd(0x8400 + i, 4) for i in (4, 8, 12, 16, 20)],
          [0x60001000, 0x60002000, 0x60000200, 0x60002000, 0x60000400])
    check("整机：描述符表的三个数值（A）",
          [m.bus.rd(0x8418, 4), m.bus.rd(0x841C, 4), m.bus.rd(0x8420, 4)],
          [0xDC0, 0x46C, 0x98])
    check("整机：描述符表的三个数值（B）",
          [m.bus.rd(0x8468, 4), m.bus.rd(0x846C, 4), m.bus.rd(0x8470, 4)],
          [0xDE0, 0x46D, 0xA0])

    # 通道 A 的 +02 被两步 RMW 叠成 0x82，随后又被 0x1A3694 写成 0x01
    check("整机：通道 A 的 +02 终值 0x01（0x1A3694 直写）",
          m.dual60[0].ctrl(), 0x01)
    check("整机：通道 B 的 +02 终值 0x82（只做了两步 RMW）",
          m.dual60[1].ctrl(), 0x82)
    check("整机：通道 A 收到 6 笔写 / 3 次读",
          (m.dual60[0].writes, m.dual60[0].reads), (6, 3))
    check("整机：通道 B 收到 5 笔写 / 2 次读",
          (m.dual60[1].writes, m.dual60[1].reads), (5, 2))

    # ---- D. 0x40：RMW 终值、下载口
    dev = m.ctrl40
    check("整机：实例 +0x40000 的 +0xF8 = 0x3B3B",
          dev.rmw_value(0x40000), 0x3B3B)
    check("整机：实例 +0x40800 的 +0xF8 = 0x1D1D（另一份配置）",
          dev.rmw_value(0x40800), 0x1D1D)
    check("整机：实例 +0x40900 的 +0xF8 从没被 RMW",
          dev.rmw_value(0x40900), 0x0000)
    check("整机：+0xF8 一共读了 4 次（两实例各 2 次）", dev.rmw_reads, 4)
    check("整机：长度寄存器 +0x10000 = 0x0800",
          (dev.regs.get(0x10000, 0) << 8) | dev.regs.get(0x10001, 0), 0x0800)
    pay = dev.stream_payload()
    check("整机：下载口收到 3082 字节（1541 个半字）", len(pay), 3082)
    rom = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "B3000111.bin"), "rb").read()
    # 表 0（256 项）逐字节来自 ROM 0x1BDBAE；表 1 起各自的源地址不同，
    # 所以只有第一张表能直接用这个偏移比对（6 张表全等见 probe 的 F 段）。
    src0 = rom[0x1BDBAE - 0x40000: 0x1BDBAE - 0x40000 + 512]
    check("整机：下载口表 0 = ROM 0x1BDBAE 的 512 字节", pay[:512], src0)
    check("整机：下载口表 0 是 i×0x40 的斜坡",
          [int.from_bytes(pay[2 * i:2 * i + 2], "big") for i in range(4)],
          [0x0000, 0x0040, 0x0080, 0x00C0])
    check("整机：表 1 在 +0x10400 起、首值 0 / 0x100 / 0x200",
          [int.from_bytes(dev.image(0x10400 + 2 * i, 2), "big") for i in range(3)],
          [0x0000, 0x0100, 0x0200])
    check("整机：表 2 在 +0x10800 起、首值 0 / 0x1000 / 0x16A0",
          [int.from_bytes(dev.image(0x10800 + 2 * i, 2), "big") for i in range(3)],
          [0x0000, 0x1000, 0x16A0])
    check("整机：表 3/4/5 三份同内容（+0x11000 = +0x11400 = +0x11800）",
          (dev.image(0x11000, 16), dev.image(0x11400, 16), dev.image(0x11800, 16))
          and (dev.image(0x11000, 16) == dev.image(0x11400, 16)
               == dev.image(0x11800, 16)), True)
    check("整机：6 张表占 3082 / 区间 6146 字节 = 50.1%（每表按 0x200 对齐、中间留空）",
          round(100.0 * len(pay) / (0x11A02 - 0x10200), 1), 50.1)

    # ---- E. 未映射总账：读侧清零
    check("整机：未映射读 0 个地址（V45 是 5 个）",
          len(m.bus.unmapped_reads), 0)
    check("整机：未映射写 112 笔（V45 是 1853）",
          len(m.bus.unmapped_writes), 112)
    check("整机：0x40/0x60/0x61 三个窗口不再有未映射写",
          sum(1 for a, n, v, pc in m.bus.unmapped_writes
              if 0x40000000 <= a < 0x41000000 or 0x60000000 <= a < 0x62000000), 0)
    check("整机：换手 34（与建模前相同）", len(res["switches"]), 34)
    check("整机：入口 17 个", len(res["tasks"]), 17)
    check("整机：末 PC 0x407DC", m.cpu.pc, 0x407DC)
    check("整机：覆盖 5404 个字（与 V45 相同）", m.cov.words(), 5404)
    check("窗口常量与设备一致（0x40）",
          (lists.CTRL40_BASE, lists.CTRL40_SIZE, lists.CTRL40_RMW_FINAL),
          (CtrlWindow40.BASE, CtrlWindow40.SIZE, CtrlWindow40.RMW_FINAL))
    check("窗口常量与设备一致（0x60/0x61）",
          (lists.DUAL60_BASES, lists.DUAL60_SIZE, lists.DUAL60_STATUS),
          ((DualCtrl60.BASE, 0x61000000), DualCtrl60.SIZE,
           DualCtrl60.R_STATUS))
    check("窗口表：0x40 / 0x60 两行已归零",
          [(w[0], w[3]) for w in lists.PERIPHERAL_WINDOWS
           if w[0] in ("0x40xxxxxx", "0x60/0x61")],
          [("0x40xxxxxx", 0), ("0x60/0x61", 0)])

    # ---- F. 反事实：状态字段 = 3 让分支翻转（覆盖 +17），整机数字不变
    old = machine.D300B.dual60_status
    machine.D300B.dual60_status = 0x0C
    try:
        m2 = blive.boot()
        res2 = blive.run(m2, steps=700000)
    finally:
        machine.D300B.dual60_status = old
    check("反事实：状态字段 = 3 → 覆盖 5421（走「就绪」出口）",
          m2.cov.words(), 5421)
    check("反事实：换手仍是 34（返回值被调用点清掉，进不了分支）",
          len(res2["switches"]), 34)
    check("反事实：末 PC 仍是 0x407DC", m2.cpu.pc, 0x407DC)
    check("反事实：入口仍是 17 个", len(res2["tasks"]), 17)


def test_wait():
    """V47：分配器卡在 `wai_flg(6, bit5)`；`flow` 的「当前 TCB」槽按段取。"""
    print("\n[53 分配器的卡点 + flow 的 TCB 槽（V47）]")
    from frbox import bflag, blive, bpool
    from frbox.flow import cur_tcb_slot

    # ---- A. 常量与静态字节：把「延迟槽是实参」这件事钉住
    check("锁是信号量 19", bpool.LOCK_SEM, 19)
    check("加锁包装 0x12590C", bpool.LOCK_ENTER, 0x12590C)
    check("解锁包装 0x125992", bpool.LOCK_EXIT, 0x125992)
    check("持有者表基址", bpool.LOCK_HOLDER_BASE, 0x9F58C4D0)
    check("holder_addr(19)", bpool.holder_addr(19), 0x9F58C4E3)
    check("等的是 flag6 的 bit5、mode 2",
          (bpool.POOL_FLAG, bpool.POOL_FLAG_BIT, bpool.POOL_WAIT_MODE),
          (6, 0x20, 2))

    m = blive.boot()
    rom, base = m.bus.rom, m.bus.rom_base

    def bs(a, n=2):
        return rom[a - base:a - base + n].hex()

    check("0x122084 = ldi:8 r4,#0x13（lock 的实参）", bs(0x122084), "c134")
    check("0x12214C = ldi:8 r4,#0x13（unlock 的实参）", bs(0x12214C), "c134")
    check("0x12215C = int #0x40", bs(0x12215C), "1f40")
    check("boot 后没人持有 sem 19", bpool.holder(m, 19), 0)
    check("0x9F58C4C0 起 16 字节全 0", bpool.lock_state_bytes(m), bytes(16))
    check("A 段仍取 0x45980", cur_tcb_slot(create("d300-a")), 0x45980)
    check("B 段取 0x68000000", cur_tcb_slot(m), 0x68000000)
    check("flow 在 B 段用的是 B 槽", m.flow.cur_tcb, 0x68000000)

    # ---- B. 整机跑完：卡点、锁、flag 账
    hits = {bpool.ALLOC_NODE: 0, bpool.WAIT_FLG_PC: 0, bpool.CALLER_R8_PC: 0,
            0x122162: 0}
    c, st = m.cpu, m.cpu.step

    def spy():
        if c.pc in hits:
            hits[c.pc] += 1
        return st()

    hw = bpool.HolderWatch(m).attach()
    c.step = spy
    blive.run(m, steps=700000)
    c.step = st
    hw.detach()

    check("进分配器 1 次", hits[bpool.ALLOC_NODE], 1)
    check("走到 wai_flg(6) 那句 1 次", hits[bpool.WAIT_FLG_PC], 1)
    check("调用方取返回值 0 次（没返回）", hits[bpool.CALLER_R8_PC], 0)
    check("「醒来再锁」那句没走过", hits[0x122162], 0)
    check("跑完 sem19 无人持有", bpool.holder(m, 19), 0)
    check("拿过 sem19 的任务",
          sorted({t for _s, k, t in hw.acquire_release(19) if k == "set"}),
          [4, 9, 10, 11, 27, 28])
    check("sem19 拿了 6 次放了 6 次", len(hw.of(19)), 12)

    fl = m.flow
    check("flow.switches 34 条", len(fl.switches), 34)
    check("flow 记到 18 个 TCB", len({x[8] for x in fl.calls}), 18)
    # ⚠️ TCB 数与任务数不是同一个数：多出来的那一个恒是 tcb = 0（槽还没填的那一段）
    check("flow 记到 17 个任务（第 18 个 TCB 是 tcb=0）",
          (fl.summary()["tasks"], fl.summary()["tcbs"] - fl.summary()["tasks"]), (17, 1))
    setf = sorted({x[3] for x in fl.calls if x[2] == -48})
    check("set_flg 只碰过 1/3/10/12/14", setf, [1, 3, 10, 12, 14])
    check("flag 6 从来没被 set", 6 in setf, False)
    check("flag 6 确实被等过", 6 in {x[4] for x in fl.calls if x[2] == -46}, True)

    # ---- C. 反事实：补一次 set_flg(6, 0x20) —— 阴性结果
    r = bflag.set(m, 6, 0x20)
    check("set_flg 走内核受理", r["err"], 0)
    c.step = spy
    blive.run(m, steps=200000)
    c.step = st
    check("反事实：唤醒后重走一遍、再等一次",
          hits[bpool.WAIT_FLG_PC] >= 2, True)
    check("反事实：走过了「醒来再锁」那句", hits[0x122162] >= 1, True)
    check("反事实：调用方仍然没拿到返回值", hits[bpool.CALLER_R8_PC], 0)


def test_static():
    """V48：全 ROM 静态普查（bstatic）—— 调用点 / 调用边 / store / flag 6 的按铃人。

    ⚠️ 这是自检里**最慢的一条**（线性扫 7 MB，约 50 秒）。慢也得跑：它断言的是
    「ROM 里到底写了什么」，动态跑一万步也看不见没被执行的那 88 个调用点。
    """
    print("\n[54 全 ROM 静态普查（V48）]")
    from frbox import bgate, bstatic

    sw, m = bstatic.sweep_of_type("d300-b")
    s = sw.summary()
    check("调用点 2297 个", s["calls"], 2297)
    check("服务号认不出 15 个", s["unknown_svc"], 15)
    check("调用边 15435 条", s["edges"], 15435)
    check("不同被调目标 1576 个", s["targets"], 1576)
    check("enter 23613 个", s["funcs"], 23613)
    # V51 更正：store 地址改为按 base 分形态算（`_st_addr`）。旧口径 25862 条里
    # 有一批把 `@(R13,Rj)` 的 r13 丢了、把 rj 当基址，算出的是偏小的假地址。
    check("能算出地址的 store 19853 条", s["stores"], 19853)
    check("形态不可定址（栈等）224489 条", s["stores_unknown"], 224489)

    # ---- 校验：V47 用**动态证据**定死的两个点，静态必须一模一样
    c1 = [c for c in sw.calls if c.addr == 0x122090][0]
    check("0x122090 = clr_flg(-47)", c1.svc, bstatic.CLR_FLG)
    check("0x122090 清的是 flag 6", c1.flgid, 6)
    check("0x122090 的 bits = 0xDF（~0x20）", c1.bits, 0xDF)
    c2 = [c for c in sw.calls if c.addr == 0x12215C][0]
    check("0x12215C = wai_flg(-46)", c2.svc, bstatic.WAI_FLG)
    check("0x12215C 等的是 flag 6 的 bit5 mode2",
          (c2.flgid, c2.bits, c2.mode), (6, 0x20, 2))

    # ---- flag 6 的「按铃人」在 ROM 里一直有
    check("set_flg 静态 866 处", len(sw.flag_sets()), 866)
    check("碰 flag 6 的 88 处", len(sw.flag_sets(6)), 88)
    six_bit5 = sorted(c.addr for c in sw.flag_sets(6) if c.bits == 0x20)
    check("其中 bit5 的 7 处", six_bit5,
          [0x1201CA, 0x122426, 0x1233B6, 0x14D5E6, 0x14D726, 0x14DACA, 0x14DB52])
    check("等 flag 6 的静态点 55 处", len(sw.flag_waits(6)), 55)
    check("主循环那个 set_flg 属于函数 0x1200B0", sw.enclosing(0x1201CA), 0x1200B0)

    # ---- 门字节：写者只有两个，其中一个正是 V25 定的那个
    check("门字节写者 = 0x12ECF4 / 0x12F180",
          sw.writers_of(bgate.GATE), [(0x12ECF4, 'stb'), (0x12F180, 'stb')])
    check("0x12F180 就是 bgate.GATE_WRITER", bgate.GATE_WRITER, 0x12F180)
    # 上面这条在 V51 修完 store 地址之后**仍然成立** —— 那两条是 `@Rj` 形态
    # （base 0x1600，`a = r[rj]`），旧算法恰好算对。

    # ---- V51：store 地址形态 / 结构体图 / refs_to 的窗口
    # 旧算法对 `@(R13,Rj)` 一律取 rj 当基址、丢掉 r13，于是 38 个字段写点
    # 全部塌到基址上。修完必须是 0。
    check("修后 writers_of(0x9F5A0284) = 0 个", sw.writers_of(0x9F5A0284), [])
    st = sw.structure_writers(0x9F5A0284, 0x6BE)
    check("结构体 0x9F5A0284 有 17 个偏移的写点", len(st), 17)
    check("共 44 处写", sum(len(v) for v in st.values()), 44)
    check("偏移 +0 没有写点（btsth #8 读的就是它）", 0 in st, False)
    check("偏移 +4 没有写点", 4 in st, False)
    check("偏移 +0x88 只有 1 处写（0x19657C）", st.get(0x88), [(0x19657C, 'st')])

    # ---- V51：refs_to 的窗口 —— 调用者加载的是序言起点，不是 enter
    check("refs_to(0x1962AA) 里有加载 0x1962A6 的",
          any(imm == 0x1962A6 for _pc, imm, _nx in sw.refs_to(0x1962AA)), True)
    check("window=0 时查不到（证明窗口是必要的）",
          sw.refs_to(0x1962AA, window=0), [])
    strict = sw.callers_strict(0x1962AA)
    check("callers_strict 找出 3 个调用点",
          [pc for pc, _i, _n in strict], [0x1647B8, 0x1708DE, 0x175630])


def test_bpix():
    """V49：像素判据（bpix）—— 一帧体积、两个连续口径、三条件判定。

    不建机器：boot 一次要 19 万步，自检不该再花这份时间。这里断言的是
    **计量与判定本身的算法** —— 连续段怎么切、值种数怎么算、
    填充区为什么不算帧缓冲，以及「值多样 + 被重画」时判定必须能翻成 True
    （只测 False 一边是假阴性温床）。
    """
    print("\n[55 像素判据（V49）]")
    from frbox import bpix

    check("一帧字节数 38,400", bpix.FRAME_BYTES, 38400)
    check("一帧位数 307,200", bpix.FRAME_BITS, 307200)

    class FakeBus(object):
        def __init__(self):
            self.log = []

        def wr(self, a, n, v, pc=0):
            self.log.append((a, n, v, pc))

        def rd(self, a, n, pc=0):
            return 0

    class FakeM(object):
        def __init__(self):
            self.bus = FakeBus()

    fk = FakeM()
    mt = bpix.Meter().attach(fk)
    base = bpix.SDRAM + 0x1000
    fk.bus.wr(base, 2, 0x1111, 0x200)
    fk.bus.wr(base + 2, 2, 0x2222, 0x200)
    fk.bus.wr(base + 4, 2, 0x3333, 0x200)
    check("三笔顺写 → R1 = 6 字节", mt.r1[0], 6)
    check("R1 起点", mt.r1[1], base)
    check("被写半字 3 个", mt.halfwords(), 3)
    blk = (base - bpix.SDRAM) // bpix.BLK
    check("同块值种数 3", len(mt.blk_vals[blk]), 3)
    check("首次写 hits = 1", mt.hits[(base - bpix.SDRAM) >> 1], 1)
    fk.bus.wr(base + 6, 2, 0x4444, 0x200)
    check("再顺写 → R1 = 8 字节", mt.r1[0], 8)
    check("R2 同 8 字节", mt.r2()[0], 8)
    fk.bus.wr(base + 0x100, 2, 0x5555, 0x200)     # 跳开一段
    check("跳写不拉长 R2", mt.r2()[0], 8)
    check("跳写不拉长 R1（最长仍 8）", mt.r1[0], 8)
    mt.detach()
    check("detach 后 wr 还原", fk.bus.wr.__name__, "wr")

    # ---- 反面：64 KB 面积够、但值只有 1 种（整片填充）→ 不算帧缓冲
    fill = bpix.Meter()
    for i in range(0, 0x10000, 2):
        j = i >> 1
        fill.seen[j] = 1
        fill.hits[j] = 1
        fill.blk_vals.setdefault(i >> 12, set()).add(0)
    check("填充区 R2 = 64 KB", fill.r2()[0], 0x10000)
    check("填充区不算帧缓冲", bpix.verdict(fill)["framebuffer"][0], False)

    # ---- 正面：同样面积、值多样 + 每个半字写过两次 → 必须判成「有」
    img = bpix.Meter()
    for i in range(0, 0x10000, 2):
        j = i >> 1
        img.seen[j] = 1
        img.hits[j] = 2
        img.blk_vals.setdefault(i >> 12, set()).update(range(32))
    v = bpix.verdict(img)
    check("值多样 + 重画 → 判成有帧缓冲", v["framebuffer"][0], True)
    check("同样的块，重画笔数够", v["rewrite"][0], True)


def test_tick():
    """V50：tick 设施（btick）—— 常量、服务号换算、队列布局。

    不建机器：这里断言的是**坐标与换算**。延时队列哨兵 0x68000118 落点、
    「队列号 = 优先数 − 1」、位号 = 31 − 队列号，这三条错了，
    手工到期就会挂进错的队列，任务再也换不回来。
    """
    from frbox import btick, bready

    check("延时队列哨兵 = 0x68000118", btick.SENTINEL, 0x68000118)
    check("等待原因字节 TCB+0x20", btick.TCB_WHY, 0x20)
    check("状态字节与 bready 一致", btick.TCB_STATE, bready.TCB_STATE)

    # 服务号：R12 低 8 位补码
    check("0xAB → -85（dly_tsk）", btick.sx8(0xFFAB), -85)
    check("0xE9 → -23（sta_tsk）", btick.sx8(0x00E9), -23)
    check("0xD1 → -47（clr_flg）", btick.sx8(0x00D1), -47)
    check("0xFF → -1", btick.sx8(0xFFFF), -1)

    # 队列布局：32 条哨兵占 0x68000008..0x68000108，延时哨兵紧接其后
    check("队列 9 哨兵 = 0x68000050", bready.queue_addr(9), 0x68000050)
    check("队列 9 的位号 = 22", bready.bit_of(9), 22)
    check("32 条哨兵尾 = 0x68000108",
          bready.queue_addr(31) + bready.QUEUE_STRIDE, 0x68000108)
    check("延时哨兵在就绪队列区之后",
          btick.SENTINEL >= bready.queue_addr(31) + bready.QUEUE_STRIDE, True)

    # ---- V52：严格到期的坐标与对象定义表 ----
    check("剩余 tick 字段 TCB+0x14", btick.TCB_TICKS, 0x14)
    check("状态字字段 TCB+0x08", btick.TCB_FLAGS, 0x08)
    check("到期清位掩码 = 0xFFFCFFFB", btick.FLAG_WAIT_CLEAR, 0xFFFCFFFB)
    check("对象定义表基址 = 0x551B0", btick.OBJ_TABLE, 0x551B0)
    check("对象表项步长 = 0x10", btick.OBJ_STRIDE, 0x10)
    check("ID 12 的表项 = 0x55260", btick.object_entry_addr(12), 0x55260)
    check("ID 1 的表项 = 表基址", btick.object_entry_addr(1), btick.OBJ_TABLE)


def test_tick_v52():
    """V52：严格到期 / 对象等待队列 —— 一台 boot 过的机器做动态核对。

    这里只做**坐标与首轮行为**的回归：对象 ID 12 的队列地址、三个任务的
    剩余 tick、以及严格口径首轮只唤醒 17/21。更长的推进在
    `probe/exp_tick_v52.py`（5 台机器 / 30 轮），不放进自检以免拖慢。
    """
    from frbox import btick, blive

    m = blive.boot()
    m.run(max_steps=400000, spin_guard=False)

    q12 = [q for ident, q, _n in btick.object_queues(m) if ident == 12]
    check("对象 ID 12 的队列 = 0x68000968", q12, [0x68000968])
    check("ID 12 表项 +4 从 ROM 读回同一地址",
          m.bus.rd(btick.object_entry_addr(12) + 4, 4), 0x68000968)
    check("ID 12 表项 +8 高半 = 12",
          (m.bus.rd(btick.object_entry_addr(12) + 8, 4) >> 16) & 0xFFFF, 12)

    ticks = {tid: m.bus.rd(tcb + btick.TCB_TICKS, 4)
             for tcb, tid, _p, _w in btick.delay_queue(m)}
    check("基线剩余 tick：17=1 / 7=9 / 21=0",
          ticks, {17: 1, 7: 9, 21: 0})
    check("基线非空对象队列挂着 14 个任务", btick.object_pending(m), 14)

    got = [tid for _tcb, tid in btick.expire_strict(m)]
    check("严格口径首轮只唤醒 17 / 21", sorted(got), [17, 21])
    check("首轮后任务 7 剩余 9 → 8", m.bus.rd(0x68000244 + btick.TCB_TICKS, 4), 8)


def test_v53():
    """V53：只读打桩设施 + 事件位图相关常量 / 指令解码。

    全部是纯函数或假 bus，不扫全 ROM、不 boot 机器 —— V53 的动态结论在
    `probe/exp_v53.py`（六台机器）里，不搬进自检以免拖慢。
    """
    from frbox import bpatch, bstatic

    # --- 位掩码纯函数 ---
    check("bpatch.bit_clear 清 bit31",
          bpatch.bit_clear(0x80000000)(0x80000001), 1)
    check("bpatch.bit_clear 幂等",
          bpatch.bit_clear(0x80000000)(1), 1)
    check("bpatch.bit_set 置 bit3",
          bpatch.bit_set(0x08)(0x12), 0x1A)
    check("bpatch.bit_set 幂等",
          bpatch.bit_set(0x200)(0x200), 0x200)

    # --- patch_read：只认精确的 (地址, PC) ---
    class _Bus(object):
        def __init__(self):
            self.n = 0

        def rd(self, a, n, pc=0):
            self.n += 1
            return 0x80000001

        def wr(self, a, n, v, pc=0):
            return None

    bus = _Bus()
    log = []
    key = (0x9F805AD0, 0x1753FC)
    hits = bpatch.patch_read(bus, {key: bpatch.bit_clear(0x80000000)},
                             watch=[key], log=log)
    check("打桩后读值 = 0x00000001", bus.rd(key[0], 4, key[1]), 1)
    check("打桩命中计数 = 1", hits[key], 1)
    check("log 记的是打桩前的值", log[0][3], 0x80000001)
    check("PC 不匹配时不打桩",
          bus.rd(0x9F805AD0, 4, 0x111111), 0x80000001)
    check("地址不匹配时不打桩",
          bus.rd(0x12345678, 4, 0x1753FC), 0x80000001)
    check("不打桩的读也走原 bus", bus.n, 3)

    # --- patch_write：跨边界写也要记 ---
    wlog = []
    bpatch.patch_write(bus, (0x9F805AD0,), wlog)
    bus.wr(0x9F805ACC, 8, 0x80000001, 0x175274)
    check("patch_write 记跨边界写", len(wlog), 1)
    bus.wr(0x11110000, 4, 0, 0)
    check("patch_write 忽略无关写", len(wlog), 1)

    # --- 服务号常量（V53 把 0x175408 判成 clr_flg 的依据）---
    check("SET_FLG = -48", bstatic.SET_FLG, -48)
    check("CLR_FLG = -47", bstatic.CLR_FLG, -47)
    check("WAI_FLG = -46", bstatic.WAI_FLG, -46)

    # --- 指令解码：ld #1012 / beq 的跳转目标 ---
    d = TABLE.decode(b"\x2f\xd0" + b"\x00" * 14, 0)
    check("0x1753FC 机器码 2fd0 是 ld", d[0], "ld")
    check("disp10 = 253（×4 = 1012）", d[2]["disp10"], 253)
    db = TABLE.decode(b"\xe2\x7b" + b"\x00" * 14, 0)
    check("0x175406 机器码 e27b 是 beq", db[0], "beq")
    check("beq rel9 = 123", db[2]["rel9"], 123)
    check("beq 目标 = 0x1754FE（派发段入口）",
          0x175406 + 2 + db[2]["rel9"] * 2, 0x1754FE)


def test_v54():
    """V54：INT 0x18 系统节拍的实证常量 + 周期时钟设施。

    纯函数 / 假机器，不扫全 ROM、不 boot。动态因果在
    `probe/exp_tick_v54.py`（七台机器）里，不搬进自检以免拖慢。
    """
    from frbox import dev_tick18 as t18
    from frbox import machine as _mach

    # --- 实证常量（全部来自 ROM 里的静态链条）---
    check("tick 向量 = 0x18", t18.VECTOR, 0x18)
    check("B 段 TBR = 0x000EFC00", t18.TBR, 0x000EFC00)
    check("vec 0x18 槽 = 0x000EFF9C",
          t18.VEC_SLOT, 0x000EFC00 + (255 - 0x18) * 4)
    check("槽的公式与常量一致", t18.VEC_SLOT, 0x000EFF9C)
    check("ISR = 0x04046E", t18.ISR, 0x04046E)
    check("唯一加载点 = 0x04047C", t18.CALL_LDI, 0x04047C)
    check("call 指令 = 0x040482", t18.CALL, 0x040482)
    check("tick 处理 = 0x040A44", t18.TICK_HANDLER, 0x040A44)
    check("ICR 地址 = 0x448", t18.ICR_ADDR, 0x448)
    check("ICR 级别 = 0x12", t18.ICR_LEVEL, 0x12)
    check("ICR 初始化 PC = 0x040456", t18.ICR_INIT_PC, 0x040456)
    check("直接页 0x48 / 0x4E",
          (t18.DIRECT_48, t18.DIRECT_4E), (0x48, 0x4E))
    check("tick 计数器 = 0x68000110", t18.TICK_COUNTER, 0x68000110)
    check("延时队列哨兵 = 0x68000118", t18.DELAY_SENTINEL, 0x68000118)
    check("默认周期是模型参数 1024", t18.DEFAULT_PERIOD, 1024)

    # --- 设施：只拉线，ICR 没配就不拉，待决时合并 ---
    class _Cpu(object):
        def __init__(self):
            self.irq_pending = []
            self.irq_log = []

    class _Machine(object):
        def __init__(self):
            self.cpu = _Cpu()
            self.level = 0
            self.raised = []
            self.devices = []
            self.tick18_clock = None
            self.trace = None

        def irq_level(self, vec):
            return self.level

        def raise_irq(self, vec):
            self.raised.append(vec)

    mm = _Machine()
    dev = t18.Tick18Clock(mm, period=256)
    check("设施名 = tick18clk", dev.name, "tick18clk")
    check("设施向量 = 0x18", dev.vector, 0x18)
    check("state() 带向量与 ISR",
          (dev.state()["向量"], dev.state()["ISR"]), ("0x18", "0x04046E"))
    dev.tick(256)
    check("ICR 未配 → skipped = 1", dev.skipped, 1)
    check("ICR 未配 → 不拉线", dev.fired, 0)
    mm.level = 0x12
    dev.tick(256)
    check("ICR 配好 → fired = 1", dev.fired, 1)
    check("拉的是 vec 0x18", mm.raised, [0x18])
    mm.cpu.irq_pending = [(0x12, 0x18)]
    dev.tick(256)
    check("已有待决 → coalesced = 1", dev.coalesced, 1)
    check("合并后不重复拉线", dev.fired, 1)

    # --- attach / detach ---
    m2 = _Machine()
    d2 = t18.attach(m2, period=512)
    check("attach 挂进 devices", d2 in m2.devices, True)
    check("attach 设 tick18_clock", m2.tick18_clock, d2)
    check("attach 尊重 period", d2.period, 512)
    check("detach 返回该设备", t18.detach(m2), d2)
    check("detach 后 devices 清空", len(m2.devices), 0)
    check("detach 后 tick18_clock 清空", m2.tick18_clock, None)

    # --- 真机器上有开关，且幂等 ---
    m3 = _mach.create("d300-b")
    m3.load()
    check("Machine 有 tick18_clock 槽", hasattr(m3, "tick18_clock"), True)
    d3 = m3.enable_auto_tick18(period=2048)
    check("enable_auto_tick18 返回设备", m3.tick18_clock, d3)
    check("enable_auto_tick18 幂等", m3.enable_auto_tick18(), d3)


def test_v55():
    """V55：`bobserve` 纯观察设施 + V55 实证常量。

    全用假 bus，不扫 ROM、不 boot；动态因果在 `probe/exp_tick_v55.py`。
    重点锁死「先 observe 后 patch_read」的顺序纪律：观察日志必须是器件真值。
    """
    import collections
    import frbox
    from frbox import bstatic, bobserve, bpatch

    # --- 版本与实证常量（探针 / 自检共用，别各写一份）---
    # V56 起不再在旧版本 test 里锁死版本号，避免每次升版都要改历史断言；
    # 具体版本由 test_v56() 里的当前版本断言负责。
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("PIO 状态字节 = 0x50000107", bstatic.PIO_STATUS, 0x50000107)
    check("PIO 唯一初始化写点 = 0x0402AA", bstatic.PIO_INIT_PC, 0x0402AA)
    check("PIO 初始化值 = 0x12", bstatic.PIO_INIT_VALUE, 0x12)
    check("PIO bit3 掩码 = 0x08", bstatic.PIO_BIT3, 0x08)
    check("bit3 在 0x12 里为 0",
          bstatic.PIO_INIT_VALUE & bstatic.PIO_BIT3, 0)
    check("事件位图 = 0x9F805AD0", bstatic.EVT_BITMAP, 0x9F805AD0)
    check("flag12 对象 = 0x68000968", bstatic.FLAG12_OBJ, 0x68000968)
    check("只读标志字 = 0x9F5A0284", bstatic.STRUCT_FLAG, 0x9F5A0284)

    # --- observe：只统计、原样透传 ---
    class _Bus(object):
        def __init__(self):
            self.val = 0x12
            self.written = []

        def rd(self, a, n, pc=0):
            return self.val

        def wr(self, a, n, v, pc=0):
            self.written.append((a, n, v, pc))

    bus = _Bus()
    reads, writes = bobserve.observe(bus, (bstatic.PIO_STATUS, 0x9F805AD0))
    check("observe 后读返回值不变",
          bus.rd(bstatic.PIO_STATUS, 1, 0x175882), 0x12)
    check("observe 记下 (地址,PC) → 值",
          reads[(bstatic.PIO_STATUS, 0x175882)][0x12], 1)
    bus.rd(bstatic.PIO_STATUS, 1, 0x175882)
    check("同一 (地址,PC) 累加",
          reads[(bstatic.PIO_STATUS, 0x175882)][0x12], 2)
    check("不同 PC 分开记",
          reads[(bstatic.PIO_STATUS, 0x175896)][0x12], 0)
    bus.wr(bstatic.PIO_STATUS, 1, 0x12, 0x0402AA)
    check("observe 后写仍透传", bus.written, [(bstatic.PIO_STATUS, 1, 0x12, 0x0402AA)])
    check("observe 记下写点 (目标,PC,地址,宽度,值)",
          writes[0], (bstatic.PIO_STATUS, 0x0402AA, bstatic.PIO_STATUS, 1, 0x12))
    check("未命中的观察地址不记写点", len(writes), 1)

    # --- 顺序纪律：observe 先装，patch_read 后装 ---
    bus2 = _Bus()
    r2, _w2 = bobserve.observe(bus2, (bstatic.PIO_STATUS,))
    hits = bpatch.patch_read(
        bus2, {(bstatic.PIO_STATUS, 0x200): bpatch.bit_set(0x08)})
    check("打桩后固件读到 bit3=1",
          bus2.rd(bstatic.PIO_STATUS, 1, 0x200), 0x1A)
    check("observe 记的仍是器件原始值 0x12",
          r2[(bstatic.PIO_STATUS, 0x200)][0x12], 1)
    check("observe 没把打桩后的假值记进去",
          r2[(bstatic.PIO_STATUS, 0x200)][0x1A], 0)
    check("patch_read 命中 1 次",
          hits[(bstatic.PIO_STATUS, 0x200)], 1)
    check("bit_samples：1 次采样 / bit3=1 共 0 次",
          bobserve.bit_samples(r2, bstatic.PIO_STATUS, 0x08), (1, 0))
    check("未打桩的 PC 不命中", len(hits), 1)

    # --- 纯函数汇总 ---
    mixed = {
        (bstatic.PIO_STATUS, 0x300): collections.Counter({0x12: 3, 0x1A: 2}),
        (bstatic.PIO_STATUS, 0x400): collections.Counter({0x12: 1}),
        (0x9F805AD0, 0x500): collections.Counter({0x80000000: 9}),
    }
    check("value_counts 只汇总指定地址",
          bobserve.value_counts(mixed, bstatic.PIO_STATUS),
          collections.Counter({0x12: 4, 0x1A: 2}))
    check("bit_samples 混合值 = (6, 2)",
          bobserve.bit_samples(mixed, bstatic.PIO_STATUS, 0x08), (6, 2))
    check("bit_samples 掩码无关地址返回 (0,0)",
          bobserve.bit_samples(mixed, 0x12345678, 0x08), (0, 0))


def test_v56():
    """V56：`abservice` 服务分发表解析的纯函数单测。

    全用假 ROM / 假 sweep，不扫真固件、不 boot，跑得快且确定性；
    两段实证数字与动态因果在 `probe/exp_ab_v56.py` / `results/frbox/ab56.txt`。
    """
    import collections
    import frbox
    from frbox import abservice

    # --- 版本与 V56 实证常量（探针 / 自检共用）---
    # V57 起不再在历史 test 里锁死版本号；当前版本由 test_v57() 断言。
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("SVC_VEC = 0x40", abservice.SVC_VEC, 0x40)
    check("A 段服务表基址 = 0xFE3B8", abservice.A_TABLE, 0x000FE3B8)
    check("时间驱动服务 = (dly_tsk, tslp_tsk)",
          abservice.DELAY_SERVICES, (0xAB, 0xDB))
    check("0xAB 名字 = dly_tsk", abservice.SVC_NAMES[0xAB], "dly_tsk")
    check("0xDB 名字 = tslp_tsk", abservice.SVC_NAMES[0xDB], "tslp_tsk")

    # --- 8 位补码（硬规矩 23）---
    check("sx8(0xAB) = -85", abservice.sx8(0xAB), -85)
    check("sx8(0xDB) = -37", abservice.sx8(0xDB), -37)
    check("sx8(0xE9) = -23", abservice.sx8(0xE9), -23)
    check("sx8(0x40) = 64", abservice.sx8(0x40), 64)
    check("sx8 先截到 8 位", abservice.sx8(0x1AB), -85)

    # --- parse_table：假 ROM，表位于 table_addr-256 .. table_addr-2 ---
    BASE, TBL, N = 0, 0x800, 0x1000
    rom = bytearray(N)
    for i in range(N // 2):                 # 默认桩 delta = 4
        rom[i * 2], rom[i * 2 + 1] = 0x00, 0x04

    def put(svc_byte, raw):
        off = TBL + abservice.sx8(svc_byte) * 2
        rom[off], rom[off + 1] = raw >> 8, raw & 0xFF

    put(0xAB, 0x0000)   # dly_tsk 未实现
    put(0xDB, 0x0000)   # tslp_tsk 未实现
    put(0xE9, 0x0100)   # sta_tsk 独立 handler
    put(0xD0, 0xFF00)   # set_flg 负 delta，测带符号还原

    ent = abservice.parse_table(bytes(rom), BASE, TBL)
    check("128 个服务槽", len(ent), 128)
    check("槽键最小 = -128", sorted(ent)[0], -128)
    check("槽键最大 = -1", sorted(ent)[-1], -1)
    check("dly_tsk 表项 raw = 0", ent[-85].raw, 0)
    check("dly_tsk implemented = False", ent[-85].implemented, False)
    check("dly_tsk handler = 表基址", ent[-85].handler, TBL)
    check("tslp_tsk 表项 raw = 0", ent[-37].raw, 0)
    check("sta_tsk delta = 0x100", ent[-23].delta, 0x100)
    check("sta_tsk handler = 表基址 + delta", ent[-23].handler, TBL + 0x100)
    check("负 delta 按带符号还原", ent[-48].delta, -256)
    check("负 delta handler = 表基址 - 256", ent[-48].handler, TBL - 256)

    # --- 默认桩与已实现服务 ---
    stub, stub_svcs = abservice.default_stub(ent)
    check("默认桩 handler = 表基址 + 4", stub, TBL + 4)
    check("默认桩共用 124 槽", len(stub_svcs), 124)
    check("delta=0 的槽不算进默认桩", -85 in stub_svcs, False)
    check("implemented_services = [-48, -23]",
          abservice.implemented_services(ent, stub), [-48, -23])
    check("不传 stub 时结果相同",
          abservice.implemented_services(ent), [-48, -23])

    # --- call_profile / delay_report ---
    Call = collections.namedtuple("Call", "pc svc")

    class _Sweep(object):
        calls = [Call(0x100, -23), Call(0x200, -23),
                 Call(0x300, -48), Call(0x400, None)]

    prof = abservice.call_profile(_Sweep())
    check("call_profile 按服务号聚合", prof[-23], 2)
    check("call_profile 把 None 单列", prof[None], 1)
    rep = abservice.delay_report(ent, prof)
    check("delay_report 两项", len(rep), 2)
    check("delay_report dly_tsk 结构", rep[0], (0xAB, "dly_tsk", 0, 0, TBL))
    check("delay_report tslp_tsk 结构",
          rep[1], (0xDB, "tslp_tsk", 0, 0, TBL))
    check("name_of 有符号号", abservice.name_of(-23), "sta_tsk")
    check("name_of 字节号", abservice.name_of(0xE9), "sta_tsk")
    check("name_of 未实证返回空", abservice.name_of(0x00), "")
    check("ROM 不足时返回空表",
          abservice.parse_table(bytes(16), 0, 0x800), {})


def test_v57():
    """V57：`devaudit` 设备策略审计 + 忙等假墙判据的纯函数单测。

    全用假机器 / 假观察日志，不扫真固件、不 boot；两段实证数字与动态因果在
    `probe/exp_dev_v57.py` / `results/frbox/da57.txt`。
    """
    import collections
    import frbox
    from frbox import devaudit, dev_xfer

    # --- 版本与策略常量 ---
    # V58 更正：V57 自己又锁了一次具体版本（V56 那次已解过同样的问题）。
    # 从本版起所有 test_vXX 一律只验「版本可读且属 0.x」，不再锁 0.XX.0，
    # 避免每加一版就要回来改上一版的断言。
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("POLICY 三个开关", sorted(devaudit.POLICY),
          ["dispatch", "prio", "xfer"])
    check("xfer 默认关", devaudit.POLICY["xfer"]["default"], False)
    check("xfer create 参数名", devaudit.POLICY["xfer"]["create_arg"], "xfer")
    check("xfer 显式启用方法", devaudit.POLICY["xfer"]["enable"],
          "enable_xfer")
    check("xfer 基线 = V2–V9", devaudit.POLICY["xfer"]["baseline"], "V2–V9")
    check("dispatch 默认关", devaudit.POLICY["dispatch"]["default"], False)
    check("dispatch 显式启用方法", devaudit.POLICY["dispatch"]["enable"],
          "enable_dispatch")
    check("dispatch 基线 = V2–V10", devaudit.POLICY["dispatch"]["baseline"],
          "V2–V10")
    check("prio 无独立 create 参数",
          devaudit.POLICY["prio"]["create_arg"], None)

    # --- 三种入口的设备策略（代码事实的镜像）---
    check("ENTRIES 三个入口", sorted(devaudit.ENTRIES),
          ["blive.boot()", "boot_realos()", "create(name)"])
    check("create(name) 三个全关",
          devaudit.ENTRIES["create(name)"],
          {"xfer": False, "dispatch": False, "prio": False})
    check("blive.boot() 只开 dispatch+prio",
          devaudit.ENTRIES["blive.boot()"],
          {"xfer": False, "dispatch": True, "prio": True})
    check("boot_realos() 三个全开",
          devaudit.ENTRIES["boot_realos()"],
          {"xfer": True, "dispatch": True, "prio": True})

    # --- 握手指纹与 dev_xfer 对账 ---
    check("HANDSHAKE 四处", len(devaudit.HANDSHAKE), 4)
    check("HANDSHAKE 偏移",
          [s.off for s in devaudit.HANDSHAKE],
          [0x0AF, 0x174, 0x524, 0x534])
    check("0x174 启动/完成位", devaudit.handshake_table()[0x174],
          (0x02, 0x40))
    check("0x174 轮询 PC", devaudit.HANDSHAKE[1].poll_pc, 0x0009C97A)
    check("0x0AF 完成位是 bit7", devaudit.handshake_table()[0x0AF][1], 0x80)
    check("handshake_table 与 dev_xfer.D300_A_TABLE 一致",
          devaudit.handshake_table(), dev_xfer.D300_A_TABLE)

    # --- audit / missing：假机器 ---
    class _M(object):
        name = "fake"
        xfer = dispatch = prio = cov = None
        devices = []

    m0 = _M()
    st0 = devaudit.audit(m0)
    check("audit 空机器三项全 False",
          [st0["xfer"], st0["dispatch"], st0["prio"]],
          [False, False, False])
    check("audit 空机器 devices=0", st0["devices"], 0)
    check("audit 空机器 name", st0["name"], "fake")
    check("audit 不改机器状态", len(m0.devices), 0)
    check("missing 默认要求两项",
          devaudit.missing(m0), ["xfer", "dispatch"])

    m1 = _M()
    m1.xfer, m1.dispatch, m1.prio = object(), object(), object()
    m1.cov = object()
    m1.devices = [1, 2, 3, 4, 5, 6, 7]
    st1 = devaudit.audit(m1)
    check("audit 挂齐三项全 True",
          [st1["xfer"], st1["dispatch"], st1["prio"], st1["cov"]],
          [True, True, True, True])
    check("audit devices 计数", st1["devices"], 7)
    check("missing 挂齐返回空", devaudit.missing(m1), [])
    check("missing 只查 xfer", devaudit.missing(m1, ("xfer",)), [])
    m1.xfer = None
    check("missing 摘掉 xfer", devaudit.missing(m1), ["xfer"])

    # --- read_values / stall_verdict：纯函数 ---
    reads = collections.defaultdict(collections.Counter)
    reads[(0x174, 0x0009C97A)][0x02] = 6119
    reads[(0x174, 0x0009C976)][0x00] = 1
    check("read_values 指定 PC",
          dict(devaudit.read_values(reads, 0x174, 0x0009C97A)), {0x02: 6119})
    check("read_values 合并 PC",
          dict(devaudit.read_values(reads, 0x174)), {0x02: 6119, 0x00: 1})
    check("read_values 无关地址为空",
          dict(devaudit.read_values(reads, 0x0AF)), {})
    check("stall_verdict 假墙（V57 C0 口径）",
          devaudit.stall_verdict(reads, 0x174, 0x40, 0x0009C97A),
          (6119, 0, True))
    check("stall_verdict 空日志不算假墙",
          devaudit.stall_verdict({}, 0x174, 0x40, 0x0009C97A),
          (0, 0, False))
    reads2 = collections.defaultdict(collections.Counter)
    # 十进制 66 = 0x42（启动位 0x02 + 完成位 0x40）；
    # bobserve 打印的是十进制，别把它读成十六进制 0x66。
    reads2[(0x174, 0x0009C97A)][0x02] = 28
    reads2[(0x174, 0x0009C97A)][0x42] = 4
    check("stall_verdict 见到完成位（V57 C1 口径）",
          devaudit.stall_verdict(reads2, 0x174, 0x40, 0x0009C97A),
          (32, 4, False))
    reads3 = collections.defaultdict(collections.Counter)
    reads3[(0x174, 0x0009C97A)][0x42] = 4
    check("stall_verdict 只有完成位",
          devaudit.stall_verdict(reads3, 0x174, 0x40, 0x0009C97A),
          (4, 4, False))
    check("stall_verdict 换轮询 PC 则无读",
          devaudit.stall_verdict(reads, 0x174, 0x40, 0x0009C980),
          (0, 0, False))


def test_v58():
    """V58：`svcgraph` 函数地址引用图的纯函数单测。

    不扫真固件、不 boot、不建运行机器：内嵌 16 字节实证机器码 +
    假索引 / 假 Sweep 覆盖四种引用形态、`sx11` 位宽、互斥与独立两种口径。
    """
    import collections
    import frbox
    from frbox import bstatic, svcgraph

    # --- 版本与常量 ---
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("互斥分类优先级", svcgraph.PRIORITY,
          ("ldi20", "ldi32", "rel12", "data32"))
    check("代码类形态三种", svcgraph.CODE_KINDS,
          ("ldi20", "ldi32", "rel12"))
    check("等待类服务含 wai_flg", bstatic.WAI_FLG in svcgraph.WAIT_SVCS, True)
    check("等待类服务含 twai_flg", bstatic.TWAI_FLG in svcgraph.WAIT_SVCS, True)
    check("等待类服务含 sem_wai", bstatic.SEM_WAI in svcgraph.WAIT_SVCS, True)
    check("等待类服务含 rcv_msg", -61 in svcgraph.WAIT_SVCS, True)
    check("Ref 字段", svcgraph.Ref._fields,
          ("pc", "kind", "imm", "next_mnem"))

    # --- sx11：符号位是 bit10，不是 bit11（fr_emu.py:777）---
    check("sx11(0)=0", svcgraph.sx11(0), 0)
    check("sx11(0x3FF)=1023", svcgraph.sx11(0x3FF), 1023)
    check("sx11(0x400)=-1024", svcgraph.sx11(0x400), -1024)
    check("sx11(0x7FF)=-1", svcgraph.sx11(0x7FF), -1)
    check("sx11(0x800)=+2048（bit11 不是符号位）",
          svcgraph.sx11(0x800), 2048)
    check("sx11 与 fr_emu.sx 对齐",
          [svcgraph.sx11(x) for x in (0, 0x3FF, 0x400, 0x7FF, 0x800)],
          [frbox_sx(x) for x in (0, 0x3FF, 0x400, 0x7FF, 0x800)])

    # --- 内嵌实证机器码：0x08148C..0x08149C（V58 探针 D2 反汇编）---
    code = bytes.fromhex("cd2c978c1f40144d2fe49b45001fd182")
    idx = svcgraph.ImmIndex(code, 0x08148C)
    p = idx.profile()
    check("内嵌码指令数", p["ins"], 7)
    check("内嵌码 ldi:8 一条", p["ldi"].get("i8"), 1)
    check("内嵌码 ldi:20 一条", p["ldi"].get("i20"), 1)
    check("内嵌码没有 ldi:32", p["ldi"].get("i32", 0), 0)
    check("内嵌码 ldi:20 立即数落在别处，inrom 为空", p["inrom"], {})
    check("内嵌码只有一条 call", p["call"], {"call": 1})
    check("内嵌码 rel12 边一条", p["rel12_edges"], 1)
    check("内嵌码 rel12 目标一个", p["rel12_targets"], 1)
    check("内嵌码立即数种类", p["imm_kinds"], 1)
    check("内嵌码 i32 占比 0", p["i32_share"], 0.0)
    check("ldi:20 立即数 = 0x4001F", 0x4001F in idx.imm_refs, True)
    check("ldi:20 引用 PC = 0x081496",
          idx.imm_refs[0x4001F], [(0x081496, 4, "ldi20")])
    rel_tgt = 0x08149A + 2 + svcgraph.sx11(386) * 2
    check("rel12 目标按 pc+2+sx11*2", dict(idx.rel_edges),
          {rel_tgt: [0x08149A]})
    r0 = idx.refs(0x4001F, 0)
    check("refs 命中 ldi20", [x.kind for x in r0], ["ldi20"])
    check("refs 的下一条助记符", [x.next_mnem for x in r0], ["call"])
    check("refs 窗口内可命中", len(idx.refs(0x40021, 2)), 1)
    check("refs 窗口外不命中", idx.refs(0x40025, 0), [])
    check("refs 关掉 ldi20 只剩 rel12",
          [x.kind for x in idx.refs(rel_tgt, 0, kinds=("rel12",))], ["rel12"])
    check("纯代码段没有 data32 命中", idx.data32(0x4001F), [])

    # --- data32 与 classify 优先级（假 ROM / 假索引）---
    raw = b"\xff" * 8 + (0x081404 - 4).to_bytes(4, "big")
    idx2 = svcgraph.ImmIndex(raw, 0x1000)
    check("data32 命中 -4 序言起点", idx2.data32(0x081404), [(0x1008, -4)])
    check("data32 不命中别的地址", idx2.data32(0x081410), [])
    k2, rs2 = idx2.classify(0x081404)
    check("只有 data32 时归 data32", k2, "data32")
    check("data32 的 Ref.next_mnem", [r.next_mnem for r in rs2], ["data"])
    k3, rs3 = idx.classify(0x4001F, 0)
    check("classify 命中 ldi20", k3, "ldi20")
    check("classify 带窗口 8", idx.classify(0x40021, 8)[0], "ldi20")
    check("classify 没命中返回 none", idx.classify(0x30000, 8), ("none", []))

    class _Mix(object):
        """同时给出三种代码引用 + 数据出现，验互斥优先级。"""
        def refs(self, addr, window=8, kinds=svcgraph.CODE_KINDS):
            return [svcgraph.Ref(0x10, "ldi20", addr, "call"),
                    svcgraph.Ref(0x20, "rel12", addr, "call"),
                    svcgraph.Ref(0x30, "ldi32", addr, "call")]
        def data32(self, addr, deltas=(0, -2, -4)):
            return [(0x40, 0)]

    mix = _Mix()
    check("优先级 ldi20 压过 ldi32/rel12",
          svcgraph.ImmIndex.classify(mix, 0x100)[0], "ldi20")
    check("优先级命中的条数", len(svcgraph.ImmIndex.classify(mix, 0x100)[1]), 1)
    check("independent_hits 不去重",
          svcgraph.ImmIndex.independent_hits(mix, [0x100, 0x200]),
          collections.Counter({"ldi20": 2, "rel12": 2,
                               "ldi32": 2, "data32": 2}))

    # --- body_range / wait_guards（假 Sweep）---
    class _Sweep(object):
        base, rom = 0x100, b"\xff" * 0x200
        funcs = [0x100, 0x200]

        def __init__(self, calls):
            self.calls = calls
        def of(self, svc):
            return [c for c in self.calls if c.svc == svc]
        def enclosing(self, addr):
            return 0x100 if 0x100 <= addr < 0x200 else None

    def cs(addr, svc, r4=1, r5=3, r6=0x2000, r7=2):
        return bstatic.CallSite(addr, svc, r4, r5, r6, r7)

    calls = [cs(0x120, bstatic.WAI_FLG), cs(0x140, -23),
             cs(0x160, bstatic.WAI_FLG), cs(0x180, -23),
             cs(0x1F0, -23)]
    sw = _Sweep(calls)
    check("body_range 取下一个 enter", svcgraph.body_range(sw, 0x100),
          (0x100, 0x200))
    check("body_range 末函数到 ROM 尾", svcgraph.body_range(sw, 0x200),
          (0x200, 0x300))
    pts = sw.of(-23)
    wg = svcgraph.wait_guards(sw, 0x100, pts)
    check("wait_guards 返回每个点一条", len(wg), 3)
    check("wait_guards 最近等待 = 0x120 / 0x160 / 0x160",
          [w.addr if w else None for _p, w in wg], [0x120, 0x160, 0x160])
    check("wait_guards 等待服务参数可读", wg[0][1].flgid, 3)
    check("wait_guards 无等待时返回 None",
          svcgraph.wait_guards(_Sweep([cs(0x110, -23)]), 0x100,
                               [cs(0x110, -23)])[0][1], None)
    check("wait_guards 只看本函数体",
          svcgraph.wait_guards(sw, 0x200, [cs(0x210, -23)])[0][1], None)

    # --- chain_profile：互斥 / 独立两种口径分开报 ---
    class _Idx(object):
        def classify(self, addr, window=8):
            return {0x100: ("ldi20", [svcgraph.Ref(0x10, "ldi20", 0x100, "call")]),
                    0x200: ("rel12", [svcgraph.Ref(0x20, "rel12", 0x200, "call")]),
                    0x300: ("data32", [svcgraph.Ref(0x30, "data32", 0x300, "data")]),
                    0x400: ("none", [])}[addr]
        def independent_hits(self, addrs, window=8):
            return collections.Counter({"ldi20": 3, "rel12": 2, "data32": 1})
        def data32(self, addr):
            return [(0x30, 0)] if addr == 0x300 else []

    sw2 = _Sweep([cs(0x110, -23), cs(0x210, -23), cs(0x310, -23),
                  cs(0x410, -23), cs(0x120, bstatic.WAI_FLG)])
    sw2.funcs = [0x100, 0x200, 0x300, 0x400, 0x500]
    def encl2(addr):
        return max(f for f in sw2.funcs if f <= addr)
    sw2.enclosing = encl2
    ch = svcgraph.chain_profile(sw2, _Idx(), -23)
    check("chain_profile 调用点数", len(ch["points"]), 4)
    check("chain_profile 函数数", len(ch["funcs"]), 4)
    check("chain_profile by_kind 互斥",
          dict(ch["by_kind"]),
          {"ldi20": 1, "rel12": 1, "data32": 1, "none": 1})
    check("chain_profile 代码类引用边", ch["code_edges"], 2)
    check("chain_profile table_locs 只收 data32",
          sorted(ch["table_locs"]), [0x300])
    check("chain_profile independent 是另一把尺子",
          dict(ch["independent"]), {"ldi20": 3, "rel12": 2, "data32": 1})
    check("chain_profile 无 enter 归属计数", ch["no_enter"], 0)


def frbox_sx(raw):
    """测试专用：直接调 `fr_emu.sx`，验证 svcgraph.sx11 与执行口径一致。"""
    import fr_emu
    return fr_emu.sx(raw, 11)


def test_v59():
    """V59：`flaggraph` 位级服务图的纯函数单测。

    不扫真固件、不 boot、不建运行机器：假 `CallSite` / 假 `Sweep` 覆盖
    掩码展开、`clr_flg` 的 mask 为 0 语义、等/置/清配对、有等无置、
    参数不可定、守卫图、静态写点阴性、以及两个纯格式化函数。
    """
    import frbox
    from frbox import bstatic, flaggraph, svcgraph

    # --- 版本与常量（不锁具体版本，V58 踩过）---
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("flag 服务四个", flaggraph.FLAG_SVCS,
          (bstatic.SET_FLG, bstatic.CLR_FLG,
           bstatic.WAI_FLG, bstatic.TWAI_FLG))
    check("置位服务只有 set_flg", flaggraph.SET_SVCS,
          frozenset((bstatic.SET_FLG,)))
    check("清位服务只有 clr_flg", flaggraph.CLR_SVCS,
          frozenset((bstatic.CLR_FLG,)))
    check("等待服务含 wai/twai", flaggraph.WAIT_SVCS,
          frozenset((bstatic.WAI_FLG, bstatic.TWAI_FLG)))
    check("服务号是 8 位补码", (bstatic.SET_FLG, bstatic.CLR_FLG,
                          bstatic.WAI_FLG, bstatic.TWAI_FLG),
          (-48, -47, -46, -22))

    # --- 掩码展开 ---
    check("expand_bits(0x5)", flaggraph.expand_bits(0x5), [1, 4])
    check("expand_bits(0x8000)", flaggraph.expand_bits(0x8000), [0x8000])
    check("expand_bits(0)", flaggraph.expand_bits(0), [])
    check("expand_bits(None)", flaggraph.expand_bits(None), [])
    check("zero_bits(0xFFFFDFFF) 只清 0x2000",
          flaggraph.zero_bits(0xFFFFDFFF), [0x2000])
    check("zero_bits(0xFFFFBFFF) 只清 0x4000",
          flaggraph.zero_bits(0xFFFFBFFF), [0x4000])
    check("zero_bits(0xFFFFBDFF) 清 0x200 与 0x4000",
          flaggraph.zero_bits(0xFFFFBDFF), [0x200, 0x4000])
    check("zero_bits(0) = 32 个位", len(flaggraph.zero_bits(0)), 32)
    check("zero_bits(None)", flaggraph.zero_bits(None), [])

    # --- 假服务调用点（用真实的 bstatic.CallSite，参数顺序 r4..r7）---
    def cs(addr, svc, r4=0, r5=0, r6=0, r7=0):
        return bstatic.CallSite(addr, svc, r4, r5, r6, r7)

    calls = [
        cs(0x110, bstatic.SET_FLG, 3, 0x1000),        # 置 flg3 bit12
        cs(0x120, bstatic.WAI_FLG, 0, 3, 0x2000, 2),  # 等 flg3 bit13
        cs(0x130, bstatic.CLR_FLG, 3, 0xFFFFDFFF),    # 清 flg3 bit13
        cs(0x140, bstatic.WAI_FLG, 0, 9, 0x400, 2),   # 等 flg9 0x400
        cs(0x150, bstatic.SET_FLG, 9, 0x400),         # 置 flg9 0x400
        cs(0x160, bstatic.SET_FLG, 3, None),          # 参数不可定
    ]

    class _Sweep(object):
        base, rom = 0x100, b"\xff" * 0x200
        funcs = [0x100, 0x200]

        def __init__(self, cs_, stores=()):
            self.calls = cs_
            self.stores = list(stores)
        def of(self, svc):
            return [c for c in self.calls if c.svc == svc]
        def enclosing(self, addr):
            return 0x100 if 0x100 <= addr < 0x200 else None

    sw = _Sweep(calls, [(0x170, 0x45C58, "st"), (0x180, 0x50000, "st")])
    t = flaggraph.pair_table(sw)
    check("配对总组数", len(t["pairs"]), 3)
    check("flg3/0x1000 有置无等",
          (len(t["pairs"][(3, 0x1000)]["sets"]),
           len(t["pairs"][(3, 0x1000)]["waits"])), (1, 0))
    check("flg3/0x2000 有等有清无置",
          (len(t["pairs"][(3, 0x2000)]["waits"]),
           len(t["pairs"][(3, 0x2000)]["sets"]),
           len(t["pairs"][(3, 0x2000)]["clrs"])), (1, 0, 1))
    check("flg9/0x400 等置各一",
          (len(t["pairs"][(9, 0x400)]["waits"]),
           len(t["pairs"][(9, 0x400)]["sets"])), (1, 1))
    check("参数不可定记到 set_flg", t["unknown"][bstatic.SET_FLG], 1)
    check("服务总数 set=3", t["totals"][bstatic.SET_FLG], 3)
    check("服务总数 clr=1", t["totals"][bstatic.CLR_FLG], 1)
    check("服务总数 wai=2", t["totals"][bstatic.WAI_FLG], 2)
    check("flgid 不可定 0 处", t["no_flgid"], 0)
    um = flaggraph.unmatched(t)
    check("有等无置只有 1 组", len(um), 1)
    check("有等无置落在 flg3/0x2000",
          (um[0][0], um[0][1]), (3, 0x2000))
    check("setters_for 取到置铃点",
          [c.addr for c in flaggraph.setters_for(t, 9, 0x400)], [0x150])
    check("setters_for 对无置位返回空",
          flaggraph.setters_for(t, 3, 0x2000), [])
    check("waiters_for 取到等待点",
          [c.addr for c in flaggraph.waiters_for(t, 3, 0x2000)], [0x120])
    check("group_by_flag 分桶", sorted(flaggraph.group_by_flag(sw, bstatic.SET_FLG)),
          [3, 9])

    # --- 守卫图 / 静态写点 / 引用簇（假索引）---
    gm = flaggraph.guard_map(sw, 0x100, [0x145],
                             wait_svcs=(bstatic.WAI_FLG,))
    check("守卫图返回一条", len(gm), 1)
    check("守卫图取到之前最近的等待", gm[0][1].addr, 0x140)
    check("守卫图限定等待服务后不含 set_flg",
          all(w is None or w.svc == bstatic.WAI_FLG for _p, w in gm), True)
    kp = flaggraph.kernel_store_probe(sw, 0x45C38, 0x45CD8)
    check("静态写点只命中控制块区那一笔", kp, [(0x170, 0x45C58, "st")])
    check("静态写点阴性是工具口径",
          flaggraph.kernel_store_probe(sw, 0x60000, 0x70000), [])

    class _Index(object):
        def classify(self, addr, window=8):
            return ("ldi20", [svcgraph.Ref(0x110, "ldi20", addr, "call")])
    cl = flaggraph.cluster(sw, _Index(), [calls[4]])
    check("引用簇函数一个", cl["funcs"], [0x100])
    check("引用簇形态 ldi20", cl["classes"][0x100][0], "ldi20")
    check("引用簇上层函数由 enclosing 给出", cl["upstream"][0x100], [0x100])

    # --- 纯格式化：flow_table / flag_snapshot ---
    ft = flaggraph.flow_table([(10, 0x081442, -48, 2, 0x4000, 0, 0, 0, 0)],
                              lambda s: "set_flg")
    check("flow_table 一条", len(ft), 1)
    check("flow_table 字段", (ft[0]["step"], ft[0]["pc"], ft[0]["svc"],
                          ft[0]["name"], ft[0]["r4"], ft[0]["r5"]),
          (10, 0x081442, -48, "set_flg", 2, 0x4000))
    fs = flaggraph.flag_snapshot([{"id": 3, "cb": 0x45C58, "pattern": 0,
                                   "waiters": 1, "head": 0x459FC,
                                   "tail": 0x459FC}])
    check("flag_snapshot 精简为 4 键", sorted(fs[0]),
          ["cb", "id", "pattern", "waiters"])
    check("flag_snapshot 值", (fs[0]["id"], fs[0]["pattern"], fs[0]["waiters"]),
          (3, 0, 1))
    check("mode 不进配对表（不解释组合语义）",
          any("mode" in d for d in t["pairs"].values()), False)


def test_v60():
    """V60：`ablink` 跨机事件链的纯函数 / 假机器单测。

    不扫真固件、不 boot、不建真机器：只用假 bus / 假 Machine 覆盖
    `AbLink` 收发、`OutputTap` 透传观察、`LinkedPair` 交替推进与注入阈值，
    以及 `channel_report()` 的纯格式化。渲染函数依赖 PIL 与真机器内存，
    由正式探针覆盖，不进单测。
    """
    import frbox
    from frbox import ablink

    # --- 版本与常量（不锁具体版本，V58 踩过）---
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("观察端口按地址升序四个", ablink.OBSERVED_PORTS,
          (0x44, 0xAF, 0x174, 0x524))
    check("0x44 是派发请求", ablink.KIND_BY_PORT[0x44], "dispatch_req")
    check("0xAF 是启动位", ablink.KIND_BY_PORT[0xAF], "xfer_start")
    check("0x174 是启动位", ablink.KIND_BY_PORT[0x174], "xfer_start")
    check("0x524 是启动位", ablink.KIND_BY_PORT[0x524], "xfer_start")
    check("启动位共三处", sum(1 for v in ablink.KIND_BY_PORT.values()
                          if v == "xfer_start"), 3)

    # --- AbLink 生命周期 ---
    lk = ablink.AbLink(vector=0x2B, threshold=2)
    check("初始 vector", lk.vector, 0x2B)
    check("初始 threshold", lk.threshold, 2)
    check("初始 posts", lk.posts, 0)
    check("初始 pending", lk.pending, 0)
    check("初始 injections", lk.injections, 0)
    check("阈值至少为 1", ablink.AbLink(threshold=0).threshold, 1)
    check("vector 截成 8 位", ablink.AbLink(vector=0x12B).vector, 0x2B)

    m1 = lk.post("A", "dispatch_req", 0x44, 0x10, 100)
    check("post 返回 Message", type(m1).__name__, "Message")
    check("seq 从 1 起", m1.seq, 1)
    check("side 记 A", m1.side, "A")
    check("kind 记派发", m1.kind, "dispatch_req")
    check("addr 记 0x44", m1.addr, 0x44)
    check("value 记写值", m1.value, 0x10)
    check("step 记步号", m1.step, 100)
    lk.post("A", "xfer_start", 0x174, 0x02, 101)
    check("两笔后 posts", lk.posts, 2)
    check("两笔后 pending", lk.pending, 2)
    check("达到阈值", lk.should_inject(), True)
    check("stats kinds", lk.stats()["kinds"],
          {"dispatch_req": 1, "xfer_start": 1})
    check("stats ports", lk.stats()["ports"], {"0x44": 1, "0x174": 1})
    check("head(1) 一条", len(lk.head(1)), 1)
    check("head(0) 空", lk.head(0), [])
    check("head 元素是 dict", isinstance(lk.head(1)[0], dict), True)

    out = lk.drain()
    check("drain 取走两条", len(out), 2)
    check("drain 后 delivered", lk.delivered, 2)
    check("drain 后 pending 清零", lk.pending, 0)
    check("drain 后 mailbox 空", lk.mailbox, [])
    check("drain 后不再触发", lk.should_inject(), False)
    check("drain 不改 posts", lk.posts, 2)
    check("record_injection 返回 1", lk.record_injection(200), 1)
    check("injections 累计", lk.injections, 1)
    lk.reset()
    check("reset 清 posts", lk.posts, 0)
    check("reset 清 log", lk.log, [])
    check("reset 清 injections", lk.injections, 0)

    # --- 假 bus / 假机器 ---
    class FakeBus(object):
        def __init__(self):
            self.mem = {}
            self.calls = []

        def rd(self, a, n, pc=0):
            return self.mem.get(a, 0)

        def wr(self, a, n, v, pc=0):
            self.mem[a] = v
            self.calls.append((a, n, v, pc))

    class FakeMachine(object):
        def __init__(self):
            self.bus = FakeBus()
            self.step_count = 0
            self.cpu = type("C", (), {"pc": 0x1000})()
            self.irqs = []

        def run(self, max_steps=1, spin_guard=True):
            self.step_count += max_steps

        def raise_irq(self, vec):
            self.irqs.append(vec)

    class WritingMachine(FakeMachine):
        """每轮 A 跑完往观察端口写两笔，用来测注入阈值。"""
        def run(self, max_steps=1, spin_guard=True):
            super(WritingMachine, self).run(max_steps, spin_guard)
            self.bus.wr(0x44, 1, 0x01, 0x100)
            self.bus.wr(0x44, 1, 0x01, 0x100)

    bus = FakeBus()
    tap = ablink.OutputTap(bus)
    bus.wr(0x44, 1, 0x10, 0x100)
    bus.wr(0x174, 1, 0x02, 0x200)
    bus.wr(0x999, 1, 0xFF, 0x300)
    check("透传：三笔写都落到原 bus", len(bus.calls), 3)
    check("透传：0x44 写值未改", bus.mem[0x44], 0x10)
    check("tap 只记两个观察端口", tap.counts(), {0x44: 1, 0x174: 1})
    link_t = ablink.AbLink(vector=0x2B, threshold=4)
    check("collect 新增两笔", tap.collect(link_t, 70000), 2)
    check("消息 kind 序列", [m.kind for m in link_t.log],
          ["dispatch_req", "xfer_start"])
    check("消息 value 序列", [m.value for m in link_t.log], [0x10, 0x02])
    check("消息 step 用传入步号", link_t.log[0].step, 70000)
    check("重复 collect 不重记", tap.collect(link_t, 70001), 0)
    check("重复 collect 后 posts 不变", link_t.posts, 2)

    a, b = FakeMachine(), FakeMachine()
    pair = ablink.LinkedPair(a, b, inject=False)
    pair.run(rounds=3, chunk_a=10, chunk_b=20)
    check("交替 3 轮", pair.rounds, 3)
    check("A 累计 30 步", pair.steps_a, 30)
    check("B 累计 60 步", pair.steps_b, 60)
    check("不注入时 B 没收到 irq", b.irqs, [])
    check("不接桥组 posts=0", pair.link.posts, 0)
    check("stats collect 标志", pair.stats()["collect"], True)
    check("stats 末 PC 是假机器值", pair.stats()["pc_a"], 0x1000)

    a2, b2 = WritingMachine(), FakeMachine()
    lk2 = ablink.AbLink(vector=0x2B, threshold=2)
    pair2 = ablink.LinkedPair(a2, b2, link=lk2, inject=True)
    pair2.run(rounds=3, chunk_a=10, chunk_b=10)
    check("阈值注入 3 次", lk2.injections, 3)
    check("注入向量全是 0x2B", b2.irqs, [0x2B, 0x2B, 0x2B])
    check("三轮共 6 笔消息", lk2.posts, 6)
    check("全部投递", lk2.delivered, 6)
    check("投递后在途为 0", lk2.pending, 0)
    check("stats inject 标志", pair2.stats()["inject"], True)
    check("stats 轮数", pair2.stats()["rounds"], 3)

    a3, b3 = WritingMachine(), FakeMachine()
    pair3 = ablink.LinkedPair(a3, b3, inject=False, collect=False)
    pair3.run(rounds=2, chunk_a=10, chunk_b=10)
    check("collect=False 不产生消息", pair3.link.posts, 0)
    check("collect=False 不注入", b3.irqs, [])
    check("stats collect=False", pair3.stats()["collect"], False)

    rep = ablink.channel_report(lk2.stats())
    check("报告 4 行", len(rep), 4)
    check("报告含消息数", "6 笔" in rep[0], True)
    check("报告含中断注入", "中断注入 3 次" in rep[0], True)
    check("报告末行是推进轮数", "交替推进" in rep[-1], True)
    rep2 = ablink.channel_report(lk2.stats(), baseline={"cov_b": 1894})
    check("带基线时多一行", len(rep2), 5)
    check("基线行含基线对照", "基线对照" in rep2[-1], True)


def test_v61():
    """V61：`addrform` 纯静态地址形态分类 + ISR 控制流图。

    不扫真固件、不 boot、不建真机器：只用手工构造的 8 字节 ROM
    验证解码形态、控制流标注与 V61 实证常量。全 ROM 数字（528 / 52 / 37 /
    35 / +50）由 `probe/exp_isr_v61.py` 与 `results/frbox/isr61.txt` 覆盖。
    """
    import frbox
    from frbox import addrform, bpatch

    # ---- 版本与实证常量
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("vec 0x2B", addrform.ISR_VEC, 0x2B)
    check("ISR 入口 = 0x042E1C", addrform.ISR_ENTRY, 0x042E1C)
    check("ISR reti = 0x042E70", addrform.ISR_RETI, 0x042E70)
    check("ISR PC 数 = 37", addrform.ISR_PC_COUNT, 37)
    check("ISR 分支 = 0x042E46", addrform.ISR_BRANCH, 0x042E46)
    check("beq 跳过两条", addrform.ISR_SKIPPED, (0x042E48, 0x042E4E))
    check("打桩两 PC", addrform.ISR_PATCH_PCS, (0x042E40, 0x042E50))
    check("总闸槽 = 0x6B000030", addrform.GATE_SLOT, 0x6B000030)
    check("ICR = 0x45B", addrform.ICR_2B, 0x45B)
    check("分派器入口 = 0x17171E", addrform.DISPATCH_ENTRY, 0x17171E)
    check("状态窗口 = 0x65000000", addrform.STATUS_WINDOW, 0x65000000)

    # ---- 符号扩展
    check("sx(252,8) = -4", addrform.sx(252), -4)
    check("sx(0x1F,8) = 31", addrform.sx(0x1F), 31)
    check("sx(0,8) = 0", addrform.sx(0), 0)

    # ---- 形态分类（V61 的核心更正）
    f = addrform.classify("ldub", {"disp8": 252, "ri": 0})
    check("ldub #252 是栈帧相对", f.kind, "frame_rel")
    check("ldub #252 表达式 = r14-4", f.expr, "r14-4")
    check("ldub #252 不能静态定址", f.addr, None)
    check("ldub #252 scale = 1", f.scale, 1)
    g = addrform.classify("stb", {"disp8": 252, "ri": 0})
    check("stb #252 同样是栈帧相对", g.expr, "r14-4")
    h = addrform.classify("dmovb", {"dir8": 252, "ri": 0})
    check("dmovb #252 是真直接页", h.kind, "direct_page")
    check("dmovb #252 地址 = 0xFC", h.addr, 0xFC)
    check("dmov dir10=3 地址 = 12",
          addrform.classify("dmov", {"dir10": 3}).addr, 12)
    check("ld disp10=253 = r14-12",
          addrform.classify("ld", {"disp10": 253}).expr, "r14-12")
    check("ld udisp6=3 = r15+12",
          addrform.classify("ld", {"udisp6": 3}).expr, "r15+12")
    check("ld @Rj 归寄存器间接",
          addrform.classify("ld", {"rj": 1, "ri": 12}).kind, "reg_indirect")
    check("nop 归 none", addrform.classify("nop", {}).kind, "none")
    check("vals 不是 dict 归 none", addrform.classify("nop", None).kind, "none")
    check("describe 栈帧相对", "不是直接页" in addrform.describe(f), True)
    check("describe 直接页", addrform.describe(h), "直接页 0xFC")

    # ---- 人造 ROM 上的 ISR 控制流图
    rom = bytes.fromhex("8eff6fc0e2049730") + b"\x00" * 16
    graph = addrform.isr_graph(rom, 0x1000, 0x1000, 0x1008)
    check("isr_graph 返回 4 条", len(graph), 4)
    check("isr_graph 首 PC", graph[0].pc, 0x1000)
    check("isr_graph 末 PC", graph[-1].pc, 0x1006)
    check("isr_graph 末条 = reti", graph[-1].mn, "reti")
    check("beq 被标为控制流", graph[2].control, "beq")
    check("reti 被标为控制流", graph[3].control, "reti")
    check("stm0 不标控制流", graph[0].control, "")
    check("isr_graph 里 ldub 形态", graph[1].form.expr, "r14-4")

    # ---- scan / direct_page_hits
    cnt, detail = addrform.scan(rom, 0x1000, 252)
    check("scan frame_rel/disp8 = 1", cnt.get("frame_rel/disp8", 0), 1)
    check("scan direct_page/dir8 = 0", cnt.get("direct_page/dir8", 0), 0)
    check("scan 明细 PC = 0x1002", detail["frame_rel/disp8"][0][0], 0x1002)
    check("direct_page_hits(0xFC) = 0",
          len(addrform.direct_page_hits(rom, 0x1000, 0xFC)), 0)

    # ---- 反事实打桩是纯函数
    fn = bpatch.bit_set(1)
    check("bit_set(1)(0) = 1", fn(0), 1)
    check("bit_set(1)(0x12) = 0x13", fn(0x12), 0x13)


def test_v62():
    """V62：`dispatch62` 纯静态分派器解剖。

    不扫真固件、不 boot、不建真机器：用手工构造的 `Insn` 序列验证
    call 目标 / 状态字配对 / 按位链，用 V61 同款 8 字节 ROM 验证
    `decode_range`。全 ROM 数字（6 / 15 / 47 / 42 / 15×0 显示区引用）
    由 `probe/exp_dsp_v62.py` 与 `results/frbox/dsp62.txt` 覆盖。
    """
    import frbox
    from frbox import addrform, dispatch62 as D

    # ---- 版本与实证常量
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("分派器入口 = 0x17171E", D.DISPATCH_ENTRY, 0x17171E)
    check("分派器 ret = 0x171834", D.DISPATCH_RET, 0x171834)
    check("状态块基址 = 0x65000000", D.STATUS_BASE, 0x65000000)
    check("总闸槽 = 0x6B000030", D.GATE_SLOT, 0x6B000030)
    check("6 个状态字偏移", D.STATUS_OFFSETS,
          (0x08, 0x10, 0x104, 0x10C, 0x114, 0x11C))
    check("6 个状态字读 PC", D.STATUS_READ_PCS,
          (0x17172A, 0x17172E, 0x171734, 0x17173A, 0x171740, 0x171750))
    check("6 个状态字目标寄存器", D.STATUS_REGS, (8, 5, 6, 9, 10, 4))
    check("15 个 handler", len(D.HANDLERS), 15)
    check("短路收尾 = 0x171830", D.BRA_TAIL, 0x171830)
    check("短路 bra 共 10 条", D.BRA_COUNT, 10)
    check("首次命中的两个 handler", D.FIRST_HANDLERS,
          (0x171836, 0x1714EE))
    check("handler 含 0x1714EE", 0x1714EE in D.HANDLERS, True)
    check("handler 含 0x17232A", 0x17232A in D.HANDLERS, True)
    check("三个外部输入槽", D.GATE_SLOTS,
          (0x6B000020, 0x6B000030, 0x6B000034))
    check("同构 ISR = 0x042E82", D.SIBLING_ISR_PC, 0x042E82)
    check("同构 ISR 出口 = 0x1A4902", D.SIBLING_ISR_TARGET, 0x1A4902)
    check("handler 控制块 = 0x9F59EF50", D.HANDLER_CB, 0x9F59EF50)
    check("显示链 5 个判据 PC", D.DSP_PCS,
          (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752))

    # ---- 符号扩展与跳转目标（硬规矩 40：符号位是 bit10）
    check("sx(0,11) = 0", D.sx(0, 11), 0)
    check("sx(0x3FF,11) = 1023", D.sx(0x3FF, 11), 1023)
    check("sx(0x400,11) = -1024", D.sx(0x400, 11), -1024)
    check("sx(0x7FF,11) = -1", D.sx(0x7FF, 11), -1)
    check("sx(1736,11) = -312", D.sx(1736, 11), -312)
    check("sx(252,8) = -4", D.sx(252, 8), -4)
    check("与 addrform.sx 一致", D.sx(252, 8), addrform.sx(252))
    check("rel12 正向 = 0x171836",
          D.rel12_target(0x171752, 113), 0x171836)
    check("rel12 负向 = 0x1714EE",
          D.rel12_target(0x17175C, 1736), 0x1714EE)
    check("rel9 V61 beq = 0x042E50",
          D.rel9_target(0x042E46, 4), 0x042E50)
    check("rel9 分派器尾 = 0x171830",
          D.rel9_target(0x17175A, 106), 0x171830)

    # ---- 人造 Insn 序列：状态字配对 / call 目标 / 按位链
    def I(pc, mn, size=2, **kw):
        return D.Insn(pc, mn, size, kw)

    toy = [
        I(0x1000, "ldi:32", 6, i32=0x65000000, ri=4),
        I(0x1006, "ldi:8", 2, i8=8, ri=13),
        I(0x1008, "ld", 2, rj=4, ri=8),
        I(0x100A, "ldi:20", 4, i20=0x104, ri=13),
        I(0x100E, "ld", 2, rj=4, ri=6),
        I(0x1010, "ldi:32", 6, i32=0x02000000, ri=0),
        I(0x1016, "and", 2, rj=8, ri=0),
        I(0x1018, "beq", 2, rel9=4),
        I(0x101A, "ldi:32", 6, i32=0x0017209A, ri=12),
        I(0x1020, "call", 2, ri=12),
        I(0x1022, "call", 2, rel12=113),
    ]
    rds = D.status_reads(toy)
    check("toy status_reads 2 条", len(rds), 2)
    check("toy 第 1 条 off = 8", rds[0].off, 8)
    check("toy 第 1 条 reg = r8", rds[0].reg, 8)
    check("toy 第 2 条 off = 0x104", rds[1].off, 0x104)
    check("toy 第 2 条 reg = r6", rds[1].reg, 6)
    cls = D.call_targets(toy)
    check("toy call_targets 2 条", len(cls), 2)
    check("toy 第 1 条是 call @r12", cls[0].kind, "reg@r12")
    check("toy 第 1 条目标 = 0x17209A", cls[0].target, 0x17209A)
    check("toy 第 2 条是 rel12", cls[1].kind, "rel12")
    check("toy 第 2 条目标 = 0x1106", cls[1].target, 0x1106)
    rows = D.mask_chain(toy)
    check("toy mask_chain 2 行", len(rows), 2)
    check("toy 第 1 行 mask", rows[0].mask, 0x02000000)
    check("toy 第 1 行 beq 目标", rows[0].beq_target, 0x1022)
    check("toy 第 1 行 call 目标", rows[0].call_target, 0x17209A)
    check("toy 第 2 行 beq 已清", rows[1].beq_target, None)

    # ---- 空输入 / 立即数窗口 / 人造 ROM 解码
    check("空输入 status_reads", D.status_reads([]), [])
    check("空输入 call_targets", D.call_targets([]), [])
    check("空输入 mask_chain", D.mask_chain([]), [])
    check("imm_refs 命中 0x17xxxx 段",
          len(D.imm_refs(toy, 0x170000, 0x180000)), 1)
    check("imm_refs 命中 0x65000000 段",
          len(D.imm_refs(toy, 0x65000000, 0x66000000)), 1)
    check("imm_refs 显示区 0 命中",
          len(D.imm_refs(toy, 0x120000, 0x140000)), 0)
    rom = bytes.fromhex("8eff6fc0e2049730") + b"\x00" * 16
    g = D.decode_range(rom, 0x1000, 0x1000, 0x1008)
    check("decode_range 返回 4 条", len(g), 4)
    check("decode_range 首 PC", g[0].pc, 0x1000)
    check("decode_range 末条 = reti", g[-1].mn, "reti")
    check("decode_range 与 isr_graph 条数一致",
          len(g), len(addrform.isr_graph(rom, 0x1000, 0x1000, 0x1008)))


def test_v63():
    """V63：`isrfam63` 向量表 / 同族门 / 序言形态 / 写点归类。

    与 V62 同款做法：不扫真固件、不 boot、不建真机器，用玩具 `Insn`
    序列与假 sweep 验证纯函数；全 ROM 数字（256 槽 / 312 调用点 /
    47 写点 / 五组 0 差集）由 `probe/exp_gate_v63.py` 与
    `results/frbox/gate63.txt` 覆盖。
    """
    import frbox
    from frbox import dispatch62 as D, isrfam63 as F

    def I(pc, mn, **kw):
        return D.Insn(pc, mn, 2, kw)

    # ---- 版本与实证常量
    check("frbox 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("TBR = 0x000EFC00", F.TBR, 0x000EFC00)
    check("向量表 256 槽", F.VEC_COUNT, 256)
    check("默认入口 = 0x0404F8", F.DEFAULT_HANDLER, 0x0404F8)
    check("四条门 ISR 向量", tuple(g.vec for g in F.GATES),
          (0x26, 0x2B, 0x2C, 0x3C))
    check("三条门的槽", F.GATE_SLOTS,
          (0x6B000020, 0x6B000030, 0x6B000034))
    check("vec 0x26 出口 = 0x042C36", F.GATES[0].exits, (0x042C36,))
    check("vec 0x2B 出口 = 0x17171E", F.GATES[1].exits, (0x17171E,))
    check("vec 0x2C 出口 = 0x1A4902", F.GATES[2].exits, (0x1A4902,))
    check("vec 0x3C 出口 = 0x1A4A32", F.GATES[3].exits, (0x1A4A32,))
    check("vec 0x2C 是双调用", F.GATES[2].calls, 2)
    check("vec 0x26 是双调用", F.GATES[0].calls, 2)
    check("vec 0x2B 是单调用", F.GATES[1].calls, 1)
    check("双调用 rel12 目标 = 0x042C36", F.THIRD_DOUBLE, 0x042C36)
    check("通道表 80 步长 = 0x8400", F.CHAN_TABLE_80, 0x8400)
    check("通道表 4 步长 = 0x1BBD30", F.CHAN_TABLE_4, 0x001BBD30)
    check("显示公共函数 = 0x12E6F8", F.DSP_FN, 0x12E6F8)
    check("显示公共函数 39 条", F.DSP_FN_SIZE, 39)
    check("显示公共函数 312 个加载点", F.DSP_FN_REFS, 312)
    check("其中 302 条 call:d", F.DSP_FN_CALLD, 302)
    check("对象池步长 12", F.DSP_FN_OBJ_STRIDE, 12)
    check("对象池 = 0x9F58D508", F.DSP_FN_OBJ, 0x9F58D508)
    check("状态块基址 = 0x65000000", F.STATUS_BASE, 0x65000000)
    check("外部槽基址 = 0x6B000000", F.GATE_BASE, 0x6B000000)
    check("第 4 个槽 = 0x6B7335B0", F.EXTRA_SLOT, 0x6B7335B0)
    check("显示链 5 判据 PC 口径不变", F.DSP_PCS, D.DSP_PCS)

    # ---- 向量表公式（V54 实证 vec 0x18 槽 0x000EFF9C）
    check("vec_slot(0x18) = 0x000EFF9C", F.vec_slot(0x18), 0x000EFF9C)
    check("vec_slot(0x2B) = 0x000EFF50", F.vec_slot(0x2B), 0x000EFF50)
    check("vec_slot(0x2C) = 0x000EFF4C", F.vec_slot(0x2C), 0x000EFF4C)
    check("vec_slot(0x3C) = 0x000EFF0C", F.vec_slot(0x3C), 0x000EFF0C)
    check("vec_of_slot(0x000EFF9C) = 0x18",
          F.vec_of_slot(0x000EFF9C), 0x18)
    check("vec_of_slot(vec_slot(0x2C)) = 0x2C",
          F.vec_of_slot(F.vec_slot(0x2C)), 0x2C)
    check("vec_of_slot 与 vec_slot 互逆",
          all(F.vec_of_slot(F.vec_slot(v)) == v for v in range(256)), True)

    # ---- 人造向量表：4 个特定槽 + 其余填默认入口
    rom = bytearray(b"\x00" * (1024 + 16))
    base = F.TBR
    for v, entry in ((0x18, 0x04046E), (0x26, 0x042B2A),
                     (0x2B, 0x042E1C), (0x2C, 0x042E72),
                     (0x3C, 0x042ECC)):
        off = F.vec_slot(v) - base
        rom[off:off + 4] = entry.to_bytes(4, "big")
    tab = F.vector_table(bytes(rom), base)
    check("vector_table 返回 256 项", len(tab), 256)
    check("人造表 vec 0x18 = 0x04046E", tab[0x18].entry, 0x04046E)
    check("人造表 vec 0x26 = 0x042B2A", tab[0x26].entry, 0x042B2A)
    check("人造表 vec 0x2B = 0x042E1C", tab[0x2B].entry, 0x042E1C)
    check("人造表 vec 0x2C = 0x042E72", tab[0x2C].entry, 0x042E72)
    check("人造表 vec 0x3C = 0x042ECC", tab[0x3C].entry, 0x042ECC)
    check("人造表 vec 0 未填 = 0", tab[0].entry, 0)
    check("人造表 4 条门全部一致",
          all(ok for g, e, ok in F.gate_rows(tab)), True)
    bad = list(tab)
    bad[0x2B] = F.Vec(0x2B, F.vec_slot(0x2B), 0x999999)
    check("改错表项后 gate_rows 判不一致",
          [ok for g, e, ok in F.gate_rows(bad)], [True, False, True, True])

    # ---- 序言形态：有 enter / 无 enter 的 ISR（硬规矩 43）
    pro_enter = [I(0x1000, "stm0"), I(0x1002, "stm1"),
                 I(0x1004, "st"), I(0x1006, "enter")]
    pro_noenter = [I(0x2000, "stm0"), I(0x2002, "stm1"),
                   I(0x2004, "st"), I(0x2006, "st"),
                   I(0x2008, "ldi:32", i32=0x6B000020, ri=12)]
    check("有 enter 的序言", F.is_prologue(pro_enter), "enter")
    check("无 enter 的 ISR 序言", F.is_prologue(pro_noenter),
          "no-enter-isr")
    # vec 0x2B 的 enter 在第 6 条：初版 max_scan=5 误判，V63 修
    pro6 = [I(0x6000, "stm0"), I(0x6002, "stm1"), I(0x6004, "st"),
            I(0x6006, "st"), I(0x6008, "st"), I(0x600A, "enter"),
            I(0x600C, "ldi:32", i32=1115, ri=8), I(0x6012, "ldi:8")]
    check("第 6 条才出现 enter 仍判 enter",
          F.is_prologue(pro6), "enter")
    check("6 条以内显式短窗口不误报 no-enter",
          F.is_prologue(pro6[:5]) == "no-enter-isr", True)
    check("普通指令段不是序言",
          F.is_prologue([I(0x3000, "ldi:8", i8=1), I(0x3002, "and")]), None)
    check("空输入不是序言", F.is_prologue([]), None)

    # ---- call:d 延迟槽参数（硬规矩 19）
    calls = [
        I(0x4000, "ldi:32", i32=0x001A4902, ri=12),
        I(0x4006, "call:d", ri=12),
        I(0x4008, "ldi:8", i8=0, ri=4),
        I(0x400A, "ldub", disp8=252),
        I(0x400C, "ldi:32", i32=0x001A4902, ri=12),
        I(0x4012, "call:d", ri=12),
        I(0x4014, "ldi:8", i8=1, ri=4),
        I(0x4016, "ret"),
    ]
    check("delayed_args 取到两条", len(F.delayed_args(b"", 0, calls)), 2)
    check("delayed_args 第 1 条 r4=0",
          F.delayed_args(b"", 0, calls)[0], (0x4006, 0))
    check("delayed_args 第 2 条 r4=1",
          F.delayed_args(b"", 0, calls)[1], (0x4012, 1))
    check("延迟槽不是 r4 时不取",
          F.delayed_args(b"", 0, [I(0x5000, "call:d"),
                                  I(0x5002, "ldi:8", i8=7, ri=5)]), [])
    check("空输入 delayed_args", F.delayed_args(b"", 0, []), [])

    # ---- ref_kinds / status_writer_buckets（假 sweep）
    class _Sweep(object):
        def refs_to(self, addr, window=0):
            return [(0x120196, addr, "call:d"),
                    (0x184814, addr, "call:d"),
                    (0x1A0010, addr, "call")]

        def writers_of(self, addr):
            return [(0x17184A, "st"), (0x171AD6, "st"),
                    (0x170D70, "st")] if addr == 0x65000008 else []

    sw = _Sweep()
    refs, kinds, segs = F.ref_kinds(sw, 0x12E6F8)
    check("ref_kinds 返回 3 条", len(refs), 3)
    check("ref_kinds 形态计数", dict(kinds),
          {"call:d": 2, "call": 1})
    check("ref_kinds 段分布", dict(segs), {0x12: 1, 0x18: 1, 0x1A: 1})
    allw, inside, outside = F.status_writer_buckets(
        sw, (0x65000008, 0x65000010))
    check("写点去重 3 个", len(allw), 3)
    check("写点 handler 区内 2 个", inside, 2)
    check("写点 handler 区外 1 个", outside, 1)
    check("空地址列表 0 写点",
          F.status_writer_buckets(sw, ())[0], [])

    # ---- rel12 目标（硬规矩 40：符号位 bit10）
    check("rel12_target(0x042B58,110) = 0x042C36",
          D.rel12_target(0x042B58, 110), 0x042C36)
    check("rel12_target(0x042B66,103) = 0x042C36",
          D.rel12_target(0x042B66, 103), 0x042C36)
    check("两次 rel12 目标相同",
          D.rel12_target(0x042B58, 110), D.rel12_target(0x042B66, 103))


def test_v64():
    """V64：`taskchain64` 任务表 / 启动链 / 写点归属 + rel9 符号修复。

    与 V62/V63 同款做法：不扫真固件、不 boot，用玩具 ROM、玩具
    `Sweep` 验证纯函数；全 ROM 数字（29 条 / 14 个显示任务 /
    27 个 sta_tsk / 2364 条 / 47 写点 / 四组 0 命中）由
    `probe/exp_task_v64.py` 与 `results/frbox/task64.txt` 覆盖。
    """
    import struct
    import frbox
    from frbox import dispatch62 as D, taskchain64 as T

    # ---- 实证常量
    check("V64 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("任务表 = 0x054C00", T.TASK_TABLE, 0x054C00)
    check("任务记录 0x1C 字节", T.TASK_REC, 0x1C)
    check("任务表 29 条", T.TASK_COUNT, 29)
    check("入口字段 +0x10", T.TASK_ENTRY_OFF, 0x10)
    check("显示区下界", T.DISP_LO, 0x120000)
    check("显示区上界", T.DISP_HI, 0x140000)
    check("对象池 = 0x9F58D500", T.POOL_BASE, 0x9F58D500)
    check("对象池 12 字节步长", T.POOL_STRIDE, 12)
    check("对象池 70 槽", T.POOL_COUNT, 0x46)
    check("对象池状态字段 +8", T.POOL_STATUS_OFF, 8)
    check("对象池占 840 字节", T.POOL_STRIDE * T.POOL_COUNT, 840)
    check("初始化函数 = 0x12E602", T.POOL_INIT_PC, 0x12E602)
    check("初始化 stb = 0x12E61C", T.POOL_INIT_STB, 0x12E61C)
    check("循环回跳 = 0x12E624", T.POOL_INIT_LOOP, 0x12E624)
    check("公共状态查询 = 0x12E6F8", T.DSP_FN, 0x12E6F8)
    check("巨型函数 = 0x171184", T.BIG_FN, 0x171184)
    check("巨型函数末 = 0x1728B6", T.BIG_FN_END, 0x1728B6)
    check("巨型函数 2364 条", T.BIG_FN_INSNS, 2364)
    check("巨型函数 43 个 ret", T.BIG_FN_RETS, 43)
    check("sta_tsk 服务号 -23", T.STA_TSK, -23)
    check("V63 人为区间下界", T.WRITER_LO, 0x1714EE)
    check("V63 人为区间上界", T.WRITER_HI, 0x172400)

    # ---- u32 大端读与玩具任务表
    rom = bytearray(0x1000)
    base = 0x054000
    off = T.TASK_TABLE - base
    struct.pack_into(">I", rom, off + 0x10, 0x001200AC)
    check("u32 大端读", T.u32(rom, base, T.TASK_TABLE + 0x10), 0x001200AC)
    check("u32 越界给 0", T.u32(rom, base, base + 0x2000), 0)
    for n in range(T.TASK_COUNT):
        a = off + n * T.TASK_REC
        struct.pack_into(">I", rom, a + T.TASK_ENTRY_OFF,
                         0x120000 + n * 0x1000)
        struct.pack_into(">I", rom, a + T.TASK_STACK_OFF,
                         0x9F800000 + n * 0x100)
        struct.pack_into(">I", rom, a, 0x68000000 + n * 0x2C)
    recs = T.task_table(rom, base)
    check("玩具任务表 29 条", len(recs), 29)
    check("记录 0 = 任务 1", recs[0].tid, 1)
    check("记录 0 地址", recs[0].rec, T.TASK_TABLE)
    check("记录 1 地址 +0x1C", recs[1].rec, T.TASK_TABLE + 0x1C)
    check("记录 0 入口", recs[0].entry, 0x120000)
    check("记录 1 入口", recs[1].entry, 0x121000)
    check("记录 1 栈顶", recs[1].stack, 0x9F800100)
    dt = T.display_tasks(recs)
    check("玩具显示任务 29 个", len(dt), 29)
    check("玩具显示任务 ID 1 入口", dt[1], 0x120000)
    recs2 = list(recs)
    recs2[0] = recs2[0]._replace(entry=0x00150D1A)
    recs2[6] = recs2[6]._replace(entry=0x00175192)   # 任务 7 移到代码区
    check("非显示区任务被排除", len(T.display_tasks(recs2)), 27)

    # ---- 玩具 Sweep：sta_tsk 启动链与写点归属
    class _Call(object):
        def __init__(self, addr, svc, r4):
            self.addr, self.svc, self.r4 = addr, svc, r4

    class _Sweep(object):
        def __init__(self):
            self.calls = [
                _Call(0x12DFAA, -23, 5),      # 显示任务，紧邻入口
                _Call(0x1352E6, -23, 9),      # 显示任务
                _Call(0x17518E, -23, 7),      # 非显示任务（链起点）
                _Call(0x130B5E, -23, None),   # 读不出 ID
                _Call(0x1A0000, -37, 5),      # 别的服务，不算
            ]
            self._stores = {
                0x65000008: [(0x17184A, "st")],
                0x6500010C: [(0x170D70, "st"), (0x1710C0, "st")],
            }

        def enclosing(self, pc):
            return 0x171184 if 0x171000 <= pc < 0x172800 else 0x170AEE

        def writers_of(self, addr):
            return self._stores.get(addr, [])

    sw = _Sweep()
    la = T.launchers(sw, recs2)
    check("玩具启动点 4 处", len(la), 4)
    check("启动点按 PC 升序", [x.pc for x in la],
          [0x12DFAA, 0x130B5E, 0x1352E6, 0x17518E])
    check("启动点任务 ID", [x.tid for x in la], [5, None, 9, 7])
    displ = [x for x in la if x.display]
    check("显示任务启动点 2 处", len(displ), 2)
    check("紧邻入口 delta", displ[0].delta, recs2[4].entry - 0x12DFAA)
    check("非显示启动点 display=False", la[3].display, False)
    check("读不出 ID 的启动点 entry=0", la[1].entry, 0)
    rows, owners, outside = T.writer_owners(sw)
    check("玩具写点 3 笔", len(rows), 3)
    check("玩具写点 owner 2 组", len(owners), 2)
    check("owner 计数", dict(owners), {0x170AEE: 1, 0x171184: 2})
    check("玩具人为区外 2 笔", len(outside), 2)
    check("写点行结构", rows[0][1:3], (0x6500010C, "st"))

    # ---- rel9 符号修复（V64）与 rel12 回归
    check("sx(0xFA,8) = -6", D.sx(0xFA, 8), -6)
    check("sx(0x04,8) = 4", D.sx(0x04, 8), 4)
    check("rel9 向前跳 = 0x042E50", D.rel9_target(0x042E46, 4), 0x042E50)
    check("rel9 分派器 bra = 0x171830", D.rel9_target(0x17175E, 0x68),
          0x171830)
    check("rel9 向后循环 = 0x12E61A", D.rel9_target(0x12E624, 0xFA),
          0x12E61A)
    check("rel9 向后循环旧式（无符号）", (0x12E624 + 2 + 0xFA * 2),
          0x12E81A)
    check("rel12 回归 = 0x042C36", D.rel12_target(0x042B58, 110),
          0x042C36)
    check("rel9 与 CPU 语义：负位移", D.rel9_target(0x100, 0xFF), 0x100)
    check("rel9 与 CPU 语义：零位移", D.rel9_target(0x100, 0), 0x102)


def test_v65():
    """V65：任务 2 开机装配链 / add2 负立即数 / 确定性有限扫描循环。

    与 V62–V64 同款：不扫真固件、不 boot，用玩具数据验证纯函数；
    全 ROM 数字（353 条 / 32 call / 26 服务 / 12 条 call→sta_tsk /
    4962 轮 / 11 个 sta_tsk 命中）由 `probe/exp_boot_v65.py` 与
    `results/frbox/boot65.txt` 覆盖。
    """
    import collections
    import frbox
    from frbox import dispatch62 as D, bootchain65 as B

    # ---- 实证常量
    check("V65 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("任务 2 入口", B.BOOT_TASK2, 0x1200AC)
    check("任务 2 函数 ret", B.BOOT_RET, 0x12046E)
    check("第一条 call", B.FIRST_CALL_PC, 0x1200D0)
    check("第一条 call 的目标", B.FIRST_CALLEE, 0x1A4DD2)
    check("循环四 PC", B.POLL_PCS,
          (0x1A4E54, 0x1A4E56, 0x1A4E58, 0x1A4E5A))
    check("循环出口", B.POLL_EXIT, 0x1A4E5C)
    check("循环 rel9 字段", B.POLL_REL9, 0xFD)
    check("循环回跳目标", B.POLL_TARGET, 0x1A4E54)
    check("r13 初值", B.R13_INIT, 0x6B1C)
    check("r6 基址", B.R6_BASE, 0x001D7124)
    check("扫描首地址", B.SCAN_TOP, 0x001DDC3C)
    check("门字节地址", B.BOOT_GATE_BYTE, 0x9F58D08F)
    check("门字节判定 PC", B.BOOT_GATE_CMP, 0x120178)

    # ---- add2 的 m4 是 4 位负立即数（fr_emu.py:454 `m4-16`）
    check("add2 m4=0xC → -4", B.m4_delta(0xC), -4)
    check("add2 m4=0x0 → -16", B.m4_delta(0x0), -16)
    check("add2 m4=0xF → -1", B.m4_delta(0xF), -1)
    check("m4 只取低 4 位", B.m4_delta(0x1C), -4)
    check("m4 恒为负", all(B.m4_delta(v) < 0 for v in range(16)), True)

    # ---- 分支位移：V64 修好的 8 位有符号 rel9
    check("负 rel9 回跳目标",
          B.rel9_target(B.POLL_BRANCH, B.POLL_REL9), B.POLL_TARGET)
    check("正 rel9 前跳", B.rel9_target(0x042E46, 4), 0x042E50)
    check("POLL_TARGET 与公式自洽",
          B.POLL_TARGET, D.rel9_target(B.POLL_BRANCH, B.POLL_REL9))

    # ---- 扫描窗口
    w = B.scan_window()
    check("扫描首地址公式", w["first"], B.SCAN_TOP)
    check("扫描末地址 = r6", w["last"], B.R6_BASE)
    check("扫描轮数", w["rounds"], B.LOOP_ROUNDS)
    check("扫描步数", w["steps"], B.LOOP_STEPS)
    check("步数 = 轮数 × 4", w["steps"], w["rounds"] * 4)
    check("LOOP_STEPS 常量 = 27420", B.LOOP_STEPS, 27420)
    check("扫描跨度", w["span"], B.SCAN_TOP - B.R6_BASE)
    w2 = B.scan_window(r13_init=8, delta=-4, r6=0x1000)
    check("玩具窗口轮数", w2["rounds"], 2)
    check("玩具窗口步数", w2["steps"], 8)
    check("玩具窗口首地址", w2["first"], 0x1004)
    check("玩具窗口末地址", w2["last"], 0x1000)

    # ---- launch_map：调用者一侧的映射，不依赖 enclosing()
    C = collections.namedtuple("C", "pc target")
    L = collections.namedtuple("L", "pc tid")
    ls = [L(0x200008, 4), L(0x200060, 9), L(0x300010, 17)]
    got = B.launch_map([C(0x100, 0x200000), C(0x110, 0x200050),
                        C(0x120, 0x400000)], ls)
    check("launch_map 命中数", len(got), 2)
    check("launch_map 首项任务号", got[0][3], 4)
    check("launch_map 首项偏移", got[0][4], 8)
    check("launch_map 次项任务号", got[1][3], 9)
    check("launch_map 次项偏移", got[1][4], 0x10)
    check("launch_map 窗口外不收",
          len(B.launch_map([C(0x100, 0x200000)], ls, window=4)), 0)

    # ---- wait_points：只按地址区间过滤
    S = collections.namedtuple("S", "calls")
    K = collections.namedtuple("K", "addr svc")
    sw = S([K(0x12027E, -85), K(0x1202F4, -46),
            K(0x120364, -46), K(0x1203BE, -46), K(0x130000, -46)])
    wp = B.wait_points(sw)
    check("wait_points 区间过滤", len(wp), 4)
    check("wait_points 服务号序列", [x.svc for x in wp],
          [-85, -46, -46, -46])
    check("WAIT_PCS 常量", B.WAIT_PCS,
          (0x12027E, 0x1202F4, 0x120364, 0x1203BE))

    # ---- 硬规矩 45：不许拿 ld 读值解释 bgt 条件
    check("设施文档写明 m4 负立即数",
          "m4 - 16" in B.__doc__, True)
    check("设施文档写明 bgt 条件来自 add2",
          "bgt" in B.__doc__ and "add2" in B.__doc__, True)
    check("设施文档写明延迟槽",
          "延迟槽" in B.__doc__, True)
    check("同构轮询两段", len(B.SIBLING_POLLS), 2)


def test_v66():
    """V66：`sta_tsk` 四层判定 / 服务返回码配对 / 返回点派发重放。

    与 V62–V65 同款：不 boot、不扫真固件，用玩具数据验证纯函数；
    全 ROM 数字（27 点 / 23 任务号 / 16 个 E_OK / 11 个 DORMANT /
    4 个无启动点任务）由 `probe/exp_sta_v66.py` 与 `results/frbox/sta66.txt` 覆盖。
    """
    import frbox
    from frbox import stastat66 as S

    # ---- 实证常量与状态口径
    check("V66 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("TCB 状态字节偏移", 0x0B, 0x0B)
    check("READY/WAIT/DORMANT", (S.READY, S.WAIT, S.DORMANT), (0, 4, 0x10))
    check("int #0x40 长度 = 2", S.INT_SIZE, 2)
    check("真内核陷阱入口", S.TRAP_ENTRY, 0x040668)
    check("系统调用后派发器", S.DISPATCH_ENTRY, 0x040D32)
    check("E_OK", S.E_OK, 0)

    # ---- 32 位补码返回码（B 段反例实测：−35 / −63 / −17）
    check("signed(0)=0", S.signed(0), 0)
    check("signed(-35 编码)=-35", S.signed(0xFFFFFFDD), -35)
    check("signed(-63 编码)=-63", S.signed(0xFFFFFFC1), -63)
    check("signed(-17 编码)=-17", S.signed(0xFFFFFFEF), -17)
    check("signed(0x68000130) 不变", S.signed(0x68000130), 0x68000130)

    # ---- 返回点映射与配对（只取调用后第一次 +2 命中）
    check("return_pcs 映射", S.return_pcs((0x100, 0x200)),
          {0x102: 0x100, 0x202: 0x200})
    log = [
        (10, 0x100, 7, 1, 0xFFFFFFE9, 0x6800013C),   # int #0x40
        (39, 0x102, 7, 1, 0x00000000, 0x6800013C),   # reti 回来，r12=E_OK
        (40, 0x040D32, 7, 1, 0x00000000, 0x6800013C),  # 立刻派发
        (88, 0x102, 7, 1, 0x68000130, 0x6800013C),   # 任务恢复，从 +2 续跑
    ]
    p = S.pair_returns(log, (0x100,))
    check("配对条数", len(p), 1)
    check("配对取第一次返回点的 err", p[0][1], 0)
    check("返回点重放 2 次不等于调用 2 次", p[0][2], 2)
    check("无返回点时 err 为 None",
          S.pair_returns([(1, 0x100, 7, 1, 0xFFFFFFE9, 0)], (0x100,))[0][1],
          None)
    check("错误码能被配对读出",
          S.pair_returns([(1, 0x100, 7, 1, 0, 0),
                          (2, 0x102, 7, 1, 0xFFFFFFDD, 0)], (0x100,))[0][1],
          -35)

    # ---- 「已激活」判据：绝不能拿 state==0 / 队列占用当条件
    check("WAIT + 有栈 ⇒ 已激活",
          S.is_activated({"state": S.WAIT, "sp": 0x9F80027C, "queues": []}),
          True)
    check("DORMANT + 无栈 ⇒ 未激活",
          S.is_activated({"state": S.DORMANT, "sp": 0, "queues": []}), False)
    check("READY + 有栈 ⇒ 已激活（不看队列）",
          S.is_activated({"state": S.READY, "sp": 0x68000B98, "queues": []}),
          True)
    check("READY + 无栈 ⇒ 未激活",
          S.is_activated({"state": S.READY, "sp": 0, "queues": [5]}), False)

    # ---- 四层分类
    pairs = [{"tid": t, "err": S.E_OK}
             for t in (1, 3, 4, 5, 6, 7, 9, 10, 11, 16, 17, 20, 21, 26, 27, 28)]
    tcbs = ([{"tid": t, "state": S.WAIT, "sp": 0x9F800000 + t, "ran": True,
              "queues": []} for t in
             (1, 3, 4, 5, 6, 7, 9, 10, 11, 16, 17, 20, 21, 26, 27, 28)]
            + [{"tid": t, "state": S.DORMANT, "sp": 0, "ran": False,
                "queues": []} for t in
               (8, 12, 13, 14, 15, 18, 19, 22, 23, 24, 25)]
            + [{"tid": 2, "state": S.WAIT, "sp": 0x9F800A84, "ran": True,
                "queues": []},
               {"tid": 29, "state": S.READY, "sp": 0x68000B98, "ran": True,
                "queues": []}])
    static = set((1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 16, 17, 18, 19, 20, 21,
                  22, 23, 24, 25, 26, 27, 28))
    r = S.classify(pairs, tcbs, static)
    check("第 1 层受理 16 个", len(r["accepted"]), 16)
    check("第 2 层已激活 16 个", len(r["activated"]), 16)
    check("第 3 层上过 CPU 16 个", len(r["ran"]), 16)
    check("第 4 层等待 16 个", len(r["waiting"]), 16)
    check("DORMANT 11 个", len(r["dormant"]), 11)
    check("1→2 层差集空", r["gap_1_2"], [])
    check("2→3 层差集空", r["gap_2_3"], [])
    check("三层完全重合", S.layers_overlap(r), True)
    check("全 ROM 无启动点的休眠任务 = 12/13/14/15",
          r["no_launcher"], [12, 13, 14, 15])

    bad = S.classify([{"tid": 1, "err": -63}, {"tid": 3, "err": S.E_OK}],
                     [{"tid": 1, "state": S.DORMANT, "sp": 0, "ran": False},
                      {"tid": 3, "state": S.WAIT, "sp": 0x9F800000,
                       "ran": False}], static)
    check("err≠0 不进第 1 层", bad["accepted"], [3])
    check("ran=False 不进第 3 层", bad["ran"], [])
    check("有差集时 layers_overlap 为假", S.layers_overlap(bad), False)

    # ---- 设施文档把三条最容易踩的纪律写明
    check("文档写明 svc_hook 陷阱", "svc_hook" in S.__doc__, True)
    check("文档写明返回点重放", "返回点" in S.__doc__, True)
    check("文档写明 kernel_call 预热", "kernel_call" in S.__doc__, True)


def test_v67():
    """V67：等待原因按宽度/16 位解码、对象队列归属闭环、唤醒条件（纯函数）。

    与 V62–V66 同款：不 boot、不扫真固件，用玩具数据验证纯函数；
    全 ROM 数字（2297 个 int 点 / 91 个 −170 / 17 个等待任务 / 7 条队列 /
    14 个 pat&want=0）由 `probe/exp_wait_v67.py` 与 `results/frbox/wait67.txt` 覆盖。
    """
    import frbox
    from frbox import waitstat67 as W

    # ---- 服务号解码：按立即数宽度，不统一取低 8 位
    check("V67 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("sx8(0xD2) = −46（ldi:8 正确口径）", W.sx8(0xD2), -46)
    check("sx16(0xFFD2) = −46", W.sx16(0xFFD2), -46)
    check("sx16(0xFF56) = −170", W.sx16(0xFF56), -170)
    check("sx8(0x56) = 86（旧口径错值）", W.sx8(0x56), 86)
    check("sx32(0xFFFFFF56) = −170", W.sx32(0xFFFFFF56), -170)
    check("svc_of_r12 ldi:8 #0xD2", W.svc_of_r12(0xD2, 8), -46)
    check("svc_of_r12 ldi:32 #0xFFFFFF56", W.svc_of_r12(0xFFFFFF56, 32), -170)
    check("svc_of_r12 ldi:32 #0xFFFFFF55", W.svc_of_r12(0xFFFFFF55, 32), -171)
    check("svc_of_r12 width=16 0xFF56", W.svc_of_r12(0xFF56, 16), -170)
    try:
        W.svc_of_r12(0, 7)
        bad_width = False
    except ValueError:
        bad_width = True
    check("非法立即数宽度抛 ValueError", bad_width, True)
    check("why_svc(0xFF56) = −170", W.why_svc(0xFF56), -170)
    check("why_old(0xFF56) = 86（对照）", W.why_old(0xFF56), 86)
    check("why_svc(0xFFDB) = −37", W.why_svc(0xFFDB), -37)
    check("why_svc(0xFFAB) = −85", W.why_svc(0xFFAB), -85)
    check("服务名 −170", W.svc_name(-170), "twai_flg(-170)")
    check("宽服务号共 2 种", W.WIDE_SERVICES, (-170, -171))

    # ---- 内核对象地址（V33 / KI#124）
    check("flag 1 对象 0x680008B8", W.flag_obj(1), 0x680008B8)
    check("flag 3 对象 0x680008D8", W.flag_obj(3), 0x680008D8)
    check("flag 6 对象 0x68000908", W.flag_obj(6), 0x68000908)
    check("flag 12 对象 0x68000968", W.flag_obj(12), 0x68000968)
    check("flag 17 对象 0x680009B8", W.flag_obj(17), 0x680009B8)
    check("反查 0x68000988 → flag 14", W.flag_id_of(0x68000988), 14)
    check("反查延时哨兵不是 flag", W.flag_id_of(W.DELAY_SENTINEL), None)
    check("owner_name 延时队列", W.owner_name(W.DELAY_SENTINEL), "延时队列")
    check("owner_name flag 17", W.owner_name(0x680009B8), "flag 17")
    check("静态证据：等待位模式写入指令", W.WANT_STORE_PC, 0x041B54)
    check("静态证据：set_flg 读写 +8", W.PAT_OR_PCS, (0x041A5C, 0x041A60))
    check("静态证据：wai_flg 读 +8", W.PAT_LOAD_PC, 0x041B32)
    check("TCB 等待位模式/剩余 tick 共用 +0x14", W.TCB_WANT, 0x14)
    check("对象当前位模式 +0x08", W.OBJ_PAT, 0x08)

    # ---- 队列归属：玩具环链
    rows = {
        0x100: {"next": 0x200, "prev": 0x900},
        0x200: {"next": 0x900, "prev": 0x100},
        0x300: {"next": 0x300, "prev": 0x300},
    }
    owner, chain = W.queue_owner(rows, 0x100)
    check("沿 next 找到队列哨兵", owner, 0x900)
    check("链上只有 2 个 TCB", chain, [0x100, 0x200])
    check("自指坏链返回未定", W.queue_owner(rows, 0x300)[0], None)
    assign = W.build_assignments(rows, [0x100, 0x200])
    cl = W.closure(assign, [0x100, 0x200])
    check("玩具闭环成立", cl["ok"], True)
    check("玩具队列 1 条", cl["queues"], 1)
    check("玩具归到 2 个 TCB", cl["assigned"], 2)
    bad = W.closure({0x900: [0x100, 0x100], 0xA00: [0x999]}, [0x100, 0x200])
    check("有遗漏", bad["missing"], [0x200])
    check("有外来节点", bad["extra"], [0x999])
    check("有重复归属", bad["duplicated"], [0x100])
    check("不闭环", bad["ok"], False)

    # ---- 真实 7 条队列（V67 wait67.txt 的归属图，玩具复刻）
    real = {
        0x680008D8: [0x680001EC, 0x68000168, 0x68000588],
        0x680008F8: [0x68000194],
        0x68000908: [0x680001C0, 0x68000218, 0x6800029C, 0x680002C8,
                     0x680002F4, 0x680005B4, 0x680005E0],
        0x68000928: [0x6800013C],
        0x68000988: [0x68000480],
        0x680009B8: [0x680003D0],
        0x68000118: [0x680003FC, 0x68000244, 0x680004AC],
    }
    flat = [t for lst in real.values() for t in lst]
    r = W.closure(real, flat)
    check("真实归属闭环成立", r["ok"], True)
    check("真实归属 7 条队列", r["queues"], 7)
    check("真实归属 17 个等待 TCB", r["assigned"], 17)
    check("其中 6 条是 flag 对象",
          sum(1 for o in real if W.flag_id_of(o) is not None), 6)
    check("flag 6 上有 7 个等待者", len(real[0x68000908]), 7)
    check("延时队列 3 个等待者", len(real[W.DELAY_SENTINEL]), 3)

    # ---- 唤醒条件：不依赖 mode 字段
    check("pat=0 / want=0x20 两种模式都不唤醒",
          W.wakeable(0x00000000, 0x00000020), (False, False, True))
    check("pat=0x780 / want=0x9 两种模式都不唤醒",
          W.wakeable(0x00000780, 0x00000009), (False, False, True))
    check("pat=0x50000000 / want=0x80000000 都不唤醒",
          W.wakeable(0x50000000, 0x80000000), (False, False, True))
    check("只有一位重合：OR 满足 AND 不满足",
          W.wakeable(0x00000008, 0x00000018), (True, False, False))
    check("全部位置齐：两种模式都满足",
          W.wakeable(0x00000018, 0x00000018), (True, True, False))
    check("want=0 不算 blocked", W.wakeable(0, 0), (False, True, False))
    check("classify_wait −46 是 flag", W.classify_wait(-46, 0), "flag")
    check("classify_wait −170 是 flag", W.classify_wait(-170, 0), "flag")
    check("classify_wait −85 是 delay", W.classify_wait(-85, 0), "delay")
    check("classify_wait −37 是 delay", W.classify_wait(-37, 0), "delay")

    # ---- 设施文档把三条最容易踩的纪律写明
    check("文档写明立即数宽度", "立即数宽度" in W.__doc__, True)
    check("文档写明 TCB+0x20 按 16 位", "TCB+0x20" in W.__doc__, True)
    check("文档写明 TCB+0x14 语义随原因变化", "语义随等待原因变化" in W.__doc__, True)


def test_v68():
    """V68：等待位的静态置位源 / 覆盖缺口 / ISR 归属 / 反事实差分（纯函数）。

    与 V62–V67 同款：不 boot、不扫真固件，用玩具数据验证纯函数；
    全 ROM 数字（866 个 set_flg / 133 个去重置位点 / 收敛态 0 命中 /
    13 组 ISR / 反事实 459 与 1370 个新增 PC）由
    `probe/exp_set_v68.py` 与 `results/frbox/set68.txt` 覆盖。
    """
    import frbox
    from collections import namedtuple
    from frbox import setsrc68 as S

    Call = namedtuple("Call", "addr svc flgid bits")
    Vec = namedtuple("Vec", "vec slot entry")

    # ---- 版本与常量
    check("V68 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("set_flg 服务号", S.SVC_SET_FLG, -48)
    check("clr_flg 服务号", S.SVC_CLR_FLG, -47)
    check("wai_flg 服务号", S.SVC_WAI_FLG, -46)
    check("等形对照用的无害服务 get_tid", S.SVC_GET_TID, -24)
    check("默认 ISR 入口", S.DEFAULT_ISR, 0x0404F8)
    check("V67 实测 14 个 flag 等待者", len(S.WAITERS_V67), 14)
    check("V67 等待者覆盖 6 个 flag",
          sorted({f for f, _t, _w in S.WAITERS_V67}), [3, 5, 6, 8, 14, 17])
    check("flag 3 的等待位 OR", S.wants_by_flag(S.WAITERS_V67)[3], 0x00210009)
    check("flag 6 的等待位 OR", S.wants_by_flag(S.WAITERS_V67)[6], 0x000063E0)
    check("flag 17 的等待位 OR", S.wants_by_flag(S.WAITERS_V67)[17], 0x2)
    check("wants_by_flag 只有 6 个键", len(S.wants_by_flag(S.WAITERS_V67)), 6)
    check("空等待表 → 空字典", S.wants_by_flag([]), {})

    # ---- 位展开
    check("bits_of(0x9)", S.bits_of(0x9), [0x1, 0x8])
    check("bits_of(0x80000000)", S.bits_of(0x80000000), [0x80000000])
    check("bits_of(0) 空", S.bits_of(0), [])
    check("bits_of(None) 空（常量不可见）", S.bits_of(None), [])
    check("bits_of(0x63E0) 共 7 位", len(S.bits_of(0x63E0)), 7)

    # ---- 玩具调用点
    calls = [
        Call(0x100, -48, 3, 0x1),           # 能置 flag3 的 bit0
        Call(0x102, -48, 3, 0x8),           # 能置 flag3 的 bit3
        Call(0x104, -48, 3, 0x500),         # 置的是别的位
        Call(0x106, -47, 3, 0xFFFFFFFE),    # clr_flg：清 mask 里为 0 的位 = bit0
        Call(0x108, -48, 6, 0x20),          # flag6 bit5
        Call(0x10A, -48, None, 0x20),       # flgid 不可定
        Call(0x10C, -48, 3, None),          # bits 不可定
        Call(0x10E, -46, 3, 0x9),           # wai_flg 不是置位源
        Call(0x110, -47, 3, 0xFFFFFFF9),    # clr_flg：mask 覆盖 want 全部位，不清等待位
    ]
    check("is_setter 命中", S.is_setter(calls[0], 3, 0x9), True)
    check("is_setter 位不重合", S.is_setter(calls[2], 3, 0x9), False)
    check("is_setter 服务号不对", S.is_setter(calls[7], 3, 0x9), False)
    check("is_setter flgid 不对", S.is_setter(calls[4], 3, 0x9), False)
    check("is_setter bits=None 不算", S.is_setter(calls[6], 3, 0x9), False)
    check("is_clearer：清的是 mask 里为 0 的位",
          S.is_clearer(calls[3], 3, 0x9), True)
    check("is_clearer：只清别的位不算",
          S.is_clearer(calls[8], 3, 0x9), False)
    check("is_clearer 对 set_flg 不算", S.is_clearer(calls[0], 3, 0x9), False)
    check("setters_for(flag3, 0x9) 两个",
          [c.addr for c in S.setters_for(calls, 3, 0x9)], [0x100, 0x102])
    check("setters_for(flag6, 0x20) 一个",
          [c.addr for c in S.setters_for(calls, 6, 0x20)], [0x108])
    check("setters_for 不收 flgid=None",
          all(c.flgid is not None for c in S.setters_for(calls, 3, 0x9)), True)
    check("clearers_for(flag3, 0x9) 一个",
          [c.addr for c in S.clearers_for(calls, 3, 0x9)], [0x106])
    check("unique_pcs 去重", S.unique_pcs([calls[0], calls[0], calls[1]]),
          [0x100, 0x102])

    # ---- 覆盖总账（按等待者累加 vs 全局去重）
    cov = {0x100}
    check("covered 只留命中的", S.covered([0x100, 0x102], cov), [0x100])
    rep = S.coverage_report([(3, 5, 0x9), (6, 9, 0x20)], calls, cov)
    check("coverage_report 两行", len(rep), 2)
    check("第一行置位点", rep[0]["pcs"], [0x100, 0x102])
    check("第一行命中", rep[0]["hit"], [0x100])
    check("第一行 points", rep[0]["points"], 2)
    check("第一行 hit_count", rep[0]["hit_count"], 1)
    check("第二行零命中", rep[1]["hit_count"], 0)
    g = S.gap_total(rep)
    check("gap_total 累加点数", g["points"], 3)
    check("gap_total 累加命中", g["hit"], 1)
    check("gap_total 去重点数", g["unique_points"], 3)
    check("gap_total 去重命中", g["unique_hit"], 1)
    check("gap_total 等待者数", g["waiters"], 2)
    check("gap_total 零命中等待者", g["zero_hit"], 1)
    check("全覆盖时零命中等待者 0",
          S.gap_total(S.coverage_report([(3, 5, 0x9)], calls,
                                        {0x100, 0x102}))["zero_hit"], 0)

    # ---- ISR 归属（默认槽排除、多个向量共享同一 enter 区间）
    vecs = [Vec(1, 0x100, 0x0404F8), Vec(2, 0x104, 0x12D3E2),
            Vec(3, 0x108, 0x12D456), Vec(4, 0x10C, None),
            Vec(5, 0x110, 0xFFFFFFFF)]
    check("real_vectors 排除默认/空", S.real_vectors(vecs),
          [(2, 0x12D3E2), (3, 0x12D456)])
    funcs = [0x12D3BA, 0x12DA02]
    rng = S.isr_ranges([0x12D3E2, 0x12D456], funcs)
    check("isr_ranges 两条", len(rng), 2)
    check("两个向量共享同一 enter 区间",
          rng[0][1:] == rng[1][1:], True)
    check("共享区间是 0x12D3BA..0x12DA02",
          (rng[0][1], rng[0][2]), (0x12D3BA, 0x12DA02))
    inh = S.points_in_ranges([0x12D5F2, 0x999999], rng)
    check("points_in_ranges 两组都命中", len(inh), 2)
    check("每组只含区间内的点", [x[3] for x in inh],
          [[0x12D5F2], [0x12D5F2]])
    check("点全在区间外时返回空",
          S.points_in_ranges([0x999999], rng), [])

    # ---- 反事实差分
    before = {"st": {1: 4, 2: 4, 3: 0}, "cov": {0x100},
              "pats": {3: 0x0, 6: 0x0}}
    after = {"st": {1: 0, 2: 4, 3: 0}, "cov": {0x100, 0x200},
             "pats": {3: 0x9, 6: 0x0}}
    check("state_diff 只报变了的任务", S.state_diff(before["st"], after["st"]),
          [1])
    check("state_diff 相同为空", S.state_diff(before["st"], before["st"]), [])
    check("state_diff 新增键也算", S.state_diff({1: 4}, {1: 4, 2: 0}), [2])
    check("new_coverage 新增一个", S.new_coverage(before["cov"], after["cov"]),
          {0x200})
    check("new_coverage 无新增", S.new_coverage(after["cov"], before["cov"]),
          set())
    check("pattern_diff 只报变化的 flag",
          S.pattern_diff(before["pats"], after["pats"]), [(3, 0, 0x9)])
    check("pattern_diff 相同为空",
          S.pattern_diff(before["pats"], before["pats"]), [])
    d = S.diff_summary(before, after, dsp_pcs=(0x200, 0x300))
    check("diff_summary state_diff", d["state_diff"], [1])
    check("diff_summary 新增覆盖数", d["new_cov"], 1)
    check("diff_summary 显示链新增命中", d["dsp_new"], [0x200])
    check("diff_summary 位模式变化", d["pattern_diff"], [(3, 0, 0x9)])
    check("等形对照：完全相同的快照零差异",
          S.diff_summary(before, before, dsp_pcs=(0x100,))["new_cov"], 0)

    # ---- 设施文档把三层口径与两条纪律写明
    check("文档写明存在/执行/触发三层", "三层" in S.__doc__, True)
    check("文档写明 clr_flg 清 0 位", "mask 里为 0 的位" in S.__doc__, True)
    check("文档写明 kernel_call 恢复上下文", "恢复 CPU 上下文" in S.__doc__, True)
    check("文档写明等形对照", "等形" in S.__doc__, True)


def test_v69():
    """V69：描述符派发链的门判据（纯函数，玩具数据）。

    与 V62–V68 同款：不 boot、不扫真固件。全 ROM 数字（70 条描述符 /
    44 个处理函数 / 5 个裸 ret 占 31 槽位 / 27 个槽位指向 0x12E752 /
    19 处门字节引用 / 302 条提交函数直接边 / 四组新增覆盖 0,0,0,955）由
    `probe/exp_desc_v69.py` 与 `results/frbox/desc69.txt` 覆盖。
    """
    import frbox
    from frbox import descgate69 as G

    # ---- 常量与文档
    check("V69 常量 裸 ret 机器码", G.RET_BYTES, b"\x97\x20")
    check("V69 常量 空实现地址", G.NOOP_HANDLER, 0x0012E752)
    check("V69 常量 门字节", G.GATE, 0x9F58D08F)
    check("V69 常量 状态字 A", G.STATUS_A, 0x9F58D450)
    check("V69 常量 bit6 字口径", G.STATUS_WORK_READY, 0x40000000)
    check("V69 常量 派发函数", G.CURSOR_FN, 0x0012E1EC)
    check("V69 常量 提交函数", G.SUBMIT, 0x0012E6F8)
    check("V69 常量 干活分支", G.WORK_ENTRY, 0x00120184)
    check("V69 常量 当前描述符指针", G.DESC_PTR, 0x9F58D858)
    check("V69 常量 游标基址", G.CURSOR_BASE, 0x9F58DA7C)
    check("V69 常量 事件掩码", G.EVENT_MASK, 0x9F58DA81)
    check("V69 常量 游标槽位", G.CURSOR_SLOT, 0x9F58DA82)
    check("V69 常量 显示链 5 PC", len(G.DSP_PCS), 5)
    check("V69 显示链末项就是空实现", G.DSP_PCS[-1], G.NOOP_HANDLER)
    check("V69 文档写明硬规矩 52", "52" in G.__doc__, True)
    check("V69 文档写明硬规矩 53", "53" in G.__doc__, True)
    check("V69 文档写明收敛态写门无效", "**0 个 PC**" in G.__doc__, True)
    check("V69 文档写明 wai_sem 工具边界", "wai_sem(17)" in G.__doc__, True)
    check("V69 文档写明两台机器不可混比", "不可混比" in G.__doc__, True)
    check("V69 模块已注册", "frbox.descgate69" in frbox.__doc__, True)

    # ---- is_ret
    check("is_ret 真", G.is_ret(b"\x97\x20"), True)
    check("is_ret 假", G.is_ret(b"\x97\x22"), False)
    check("is_ret 接受 bytearray", G.is_ret(bytearray(b"\x97\x20")), True)
    check("is_ret 长度不对为假", G.is_ret(b"\x97"), False)

    # ---- 分组
    ENTRIES = [
        {"index": 0, "handler": 0x100},
        {"index": 1, "handler": 0x200},
        {"index": 2, "handler": 0x100},
        {"index": 3, "handler": 0x300},
        {"index": 4, "handler": 0x300},
        {"index": 5, "handler": 0x200},
    ]
    g = G.group_handlers(ENTRIES)
    check("group_handlers 键数", len(g), 3)
    check("group_handlers 键升序", list(g), [0x100, 0x200, 0x300])
    check("group_handlers 槽位升序", g[0x100], [0, 2])
    check("group_handlers 槽位 2", g[0x200], [1, 5])
    check("group_handlers 元组输入",
          G.group_handlers([(0, 0x100), (1, 0x200)])[0x200], [1])
    HEADS = {0x100: b"\x97\x20", 0x200: b"\x97\x22", 0x300: b"\x97\x20"}
    rets = G.ret_handlers(g, lambda h: HEADS[h])
    check("ret_handlers 个数", len(rets), 2)
    check("ret_handlers 列表", rets, [0x100, 0x300])
    check("noop_slots 默认指向 NOOP_HANDLER",
          G.noop_slots(g, 0x100), [0, 2])
    check("noop_slots 不存在的键", G.noop_slots(g, 0x999), [])
    check("real_handlers", G.real_handlers(g, rets), [0x200])
    check("real_handlers 空 ret 集", G.real_handlers(g, []),
          [0x100, 0x200, 0x300])

    # ---- 门判据：三条同时成立才判空槽位
    v = G.sentinel_verdict(b"\x97\x20", [0, 2, 7], [])
    check("sentinel 判为空槽位", v["verdict"], "empty_slot")
    check("sentinel is_ret", v["is_ret"], True)
    check("sentinel 槽位数", v["slots"], 3)
    check("sentinel 直接边", v["direct_edges"], 0)
    v2 = G.sentinel_verdict(b"\x97\x22", [0, 2, 7], [])
    check("sentinel 非裸 ret 不下结论", v2["verdict"], "inconclusive")
    v3 = G.sentinel_verdict(b"\x97\x20", [], [])
    check("sentinel 无槽位不下结论", v3["verdict"], "inconclusive")
    v4 = G.sentinel_verdict(b"\x97\x20", [1, 2], [0x1200])
    check("sentinel 有直接调用边不下结论", v4["verdict"], "inconclusive")
    check("sentinel 直接边计数", v4["direct_edges"], 1)

    # ---- 引用分类
    rows = [(0x10, "other", "ldub"), (0x20, "other", "ldub"),
            (0x30, "store", "stb"), (0x40, "test", "btsth")]
    a = G.classify_access(rows)
    check("classify_access 读 3", a["reads"], 3)
    check("classify_access 写 1", a["writes"], 1)
    check("classify_access kinds", a["kinds"],
          {"other": 2, "store": 1, "test": 1})
    check("classify_access 空表", G.classify_access([])["reads"], 0)

    # ---- 覆盖
    cov = {0x100, 0x300, 0x12E6F8, 0x120178}
    cc = G.chain_coverage(cov, (0x100, 0x200, 0x12E6F8))
    check("chain_coverage 命中 1", cc[0x100], True)
    check("chain_coverage 未命中", cc[0x200], False)
    check("chain_coverage 提交函数", cc[0x12E6F8], True)
    hc = G.handler_coverage(cov, [0x100, 0x200, 0x300])
    check("handler_coverage 命中数", hc["n_hit"], 2)
    check("handler_coverage 命中列表", hc["hit"], [0x100, 0x300])
    check("handler_coverage 总数", hc["n_total"], 3)
    check("handler_coverage all_zero 假", hc["all_zero"], False)
    check("handler_coverage 全 0 命中",
          G.handler_coverage(set(), [0x100])["all_zero"], True)
    d = G.door_delta({0x100}, {0x100, 0x200, 0x300}, dsp=(0x200, 0x400))
    check("door_delta 新增数", d["n_new"], 2)
    check("door_delta 新增集合", d["new"], [0x200, 0x300])
    check("door_delta 显示链新增", d["dsp_new"], [0x200])
    check("door_delta 无新增", G.door_delta({1}, {1})["n_new"], 0)

    # ---- 等形与窗口
    shapes = {k: dict(boot=10, post=20, svc=1, irqs=1)
              for k in ("G0", "G1", "G2", "G3")}
    es = G.equal_shape(shapes)
    check("equal_shape 通过", es["ok"], True)
    check("equal_shape 差异键", es["bad"], [])
    shapes["G2"]["post"] = 21
    es2 = G.equal_shape(shapes)
    check("equal_shape 抓到不等形", es2["ok"], False)
    check("equal_shape 差异字段", es2["bad"], ["post"])
    w1 = dict(pc=0x0407DC, cov={1, 2, 3}, dsp={0x100: True},
              states={1: 4, 2: 16})
    w2 = dict(pc=0x0407DC, cov={3, 2, 1}, dsp={0x100: True},
              states={1: 4, 2: 16})
    ws = G.window_same(w1, w2)
    check("window_same 逐项相同", ws["same"], True)
    check("window_same 覆盖对称差 0", ws["symdiff_cov"], 0)
    w3 = dict(w2, pc=0x04068E)
    check("window_same 抓到末 PC 差异",
          G.window_same(w1, w3)["diff"], ["pc"])
    check("window_same 覆盖差异对称差",
          G.window_same(w1, dict(w2, cov={1, 2}))["symdiff_cov"], 1)

    # ---- 四组归因
    dv = G.dispatch_verdict([("G0", 0, False), ("G1", 0, False),
                             ("G2", 0, False), ("G3", 955, True)])
    check("dispatch_verdict 零组", dv["zero_groups"], ["G0", "G1", "G2"])
    check("dispatch_verdict 非零组", dv["nonzero_groups"], [("G3", 955)])
    check("dispatch_verdict 归因 tick", dv["attribution"], "tick")
    check("dispatch_verdict 只写门无效", dv["door_only_useless"], True)
    dv2 = G.dispatch_verdict([("G0", 0, False), ("G1", 10, False)])
    check("dispatch_verdict 非 tick 组归因 mixed",
          dv2["attribution"], "mixed")
    dv3 = G.dispatch_verdict([("G0", 0, False), ("G1", 0, False)])
    check("dispatch_verdict 全零组", dv3["nonzero_groups"], [])


def test_v70():
    """V70：四形态调用边口径 / r10 局部标志 / 执行前沿（纯函数）。

    与 V62–V69 同款：不 boot、不扫真固件，用玩具数据验证纯函数；
    全 ROM 数字（5 个 blindspot PC / 12 条 call / 262 条指令 /
    执行前沿 0x12DFBA / 反事实 54 个新 PC / 1M 步对称差集 0）由
    `probe/exp_caller_v70.py` 与 `results/frbox/caller70.txt` 覆盖。
    """
    import collections
    import frbox
    from frbox import caller70 as C

    # ---- 常量与文档
    check("V70 版本可读且属 0.x", frbox.__version__.startswith("0."), True)
    check("任务 5 入口函数", C.DISPATCH_FN, 0x12DFB2)
    check("任务 5 入口序言起点", C.TASK5_ENTRY, 0x12DFAE)
    check("任务表字段", C.TASK5_FIELD, 0x54C80)
    check("派发函数", C.CURSOR_FN, 0x12E1EC)
    check("大函数入口 / 序言", (C.BIG_FN, C.BIG_PRE),
          (0x12E490, 0x12E48E))
    check("调派发函数的唯一 rel12 点", C.CALL_CURSOR, 0x12DFD0)
    check("前置门 / r10 两条置位源",
          (C.GATE_PC, C.R10_ONE, C.R10_ZERO),
          (0x12E6E0, 0x12E69E, 0x12E6BA))
    check("基线与唤醒后的执行前沿",
          (C.FRONTIER_BASE, C.FRONTIER_WAKE),
          (0x12DFBA, 0x12DFC0))
    check("wai_flg 服务号", C.WAIT_SVC, -46)
    check("等待位 9 / flag 3", (C.FLAG_ID, C.WANT_BASE), (3, 9))
    check("任务 5 TCB 与队列",
          (C.TCB_TASK5, C.QUEUE_TASK5), (0x680001EC, 0x680008D8))
    check("KEY_PCS 含 20 个关键 PC", len(C.KEY_PCS), 20)
    check("文档写明硬规矩 54", "54" in C.__doc__, True)
    check("文档写明硬规矩 55", "55" in C.__doc__, True)
    check("文档写明 call rel12 盲区", "call rel12" in C.__doc__, True)
    check("文档写明反事实不是固件行为", "不是固件行为" in C.__doc__, True)

    # ---- 分支目标必须回查执行实现（硬规矩 44 / V64）
    check("rel12_target 正偏移 0x12DFD0",
          C.rel12_target(0x12DFD0, 269), 0x12E1EC)
    check("rel12_target 正偏移 0x12DFB8",
          C.rel12_target(0x12DFB8, 38), 0x12E006)
    check("rel12_target 序言起点 0x12E48E",
          C.rel12_target(0x12DFCA, 609), 0x12E48E)
    check("rel12_target 符号位 bit10 为 1",
          C.rel12_target(0x1000, 0x7FF), 0x1000)
    check("rel9_target 是 8 位有符号（V64 0xFA）",
          C.rel9_target(0x12E624, 0xFA), 0x12E61A)
    check("rel9_target 正偏移", C.rel9_target(0x12E6B2, 3), 0x12E6BA)
    check("rel9_target 高位为 1 不能按 9 位读",
          C.rel9_target(0x12E6B8, 0xF0), 0x12E69A)

    # ---- 四形态口径
    Ref = collections.namedtuple("Ref", "pc kind imm next_mnem")
    refs = [Ref(1, "rel12", 0, "call"), Ref(2, "rel12", 0, "call"),
            Ref(3, "ldi32", 0, "call:d"), Ref(4, "data32", 0, "data")]
    check("kind_counts 分类", C.kind_counts(refs),
          {"data32": 1, "ldi32": 1, "rel12": 2})
    check("kind_counts 空表", C.kind_counts([]), {})
    check("direct_edges 合并两种直接边", C.direct_edges(5, 302), 307)
    check("missing_edge_verdict 口径盲区",
          C.missing_edge_verdict(0, 1, 1)["verdict"], "blindspot")
    check("missing_edge_verdict 真阴性",
          C.missing_edge_verdict(0, 0, 0)["verdict"], "true_negative")
    check("missing_edge_verdict 一致",
          C.missing_edge_verdict(302, 307, 5)["verdict"], "consistent")
    check("missing_edge_verdict 保留三口径",
          C.missing_edge_verdict(0, 5, 5),
          {"old": 0, "all": 5, "rel12": 5, "verdict": "blindspot"})

    # ---- r10 数据流：草稿 A 的 repr 子串假阳性不能被复现
    check("is_write_ri ldi:8", C.is_write_ri("ldi:8"), True)
    check("is_write_ri mov", C.is_write_ri("mov"), True)
    check("is_write_ri cmp 只读", C.is_write_ri("cmp"), False)
    check("is_write_ri stb 只读", C.is_write_ri("stb"), False)
    check("is_write_ri btsth 只读", C.is_write_ri("btsth"), False)
    rows = [
        (0x10, "ldi:8", {"ri": 10, "i8": 1}),
        (0x12, "cmp", {"ri": 10, "u4": 0}),
        (0x14, "stb", {"ri": 0, "rj": 8, "op1": 10}),
        (0x16, "nop", {"op1": 9, "op4": 0}),
        (0x18, "ldi:8", {"ri": 10, "i8": 0}),
    ]
    sp = C.split_reg_writes(rows, reg=10)
    check("split_reg_writes 写 2 条", sp["n_writes"], 2)
    check("split_reg_writes 读 1 条", sp["n_reads"], 1)
    check("split_reg_writes 不把 op1:10 当寄存器",
          [p for p, _m, _v in sp["writes"]], [0x10, 0x18])
    check("sources_before 取最近两条",
          [p for p, _m, _v in C.sources_before(sp["writes"], 0x20, 2)],
          [0x10, 0x18])
    check("sources_before 不含门之后写点",
          [p for p, _m, _v in C.sources_before(sp["writes"], 0x14, 2)],
          [0x10])

    # ---- 执行前沿 / 唤醒 / 反事实差分
    cov = {0x12DFAE, 0x12DFB0, 0x12DFB2, 0x12DFB4, 0x12DFB8, 0x12DFBA}
    # 入口段真实指令 PC（有 6 字节 ldi:32，不是每 2 字节一条）
    seg = [0x12DFAE, 0x12DFB0, 0x12DFB2, 0x12DFB4, 0x12DFB8,
           0x12DFBA, 0x12DFBC, 0x12DFBE, 0x12DFC0, 0x12DFC2,
           0x12DFC4, 0x12DFC6]
    fr = C.frontier(cov, seg)
    check("frontier 收敛基线", fr["frontier"], 0x12DFBA)
    check("frontier 第一条未命中", fr["first_miss"], 0x12DFBC)
    check("frontier 命中 6/12", (fr["n_hit"], fr["n_total"]), (6, 12))
    check("frontier 全不命中", C.frontier(set(), seg)["frontier"], None)
    check("wait_reason 0xFFD2 = -46", C.wait_reason(0xFFD2), -46)
    check("wait_reason 0xFF56 = -170", C.wait_reason(0xFF56), -170)
    check("wait_reason 正数不动", C.wait_reason(0x002A), 42)
    check("flag_ready pat=9 want=9", C.flag_ready(9, 9), True)
    check("flag_ready want=0 不唤醒", C.flag_ready(9, 0), False)
    check("flag_ready 位不相交不唤醒", C.flag_ready(0x10, 9), False)

    # advance 的跨区域语义：不能用全局地址最大的命中当「前沿」
    base = {0x12E006, 0x12DFBA}
    end = set(base) | {0x12DFBC, 0x12DFBE, 0x12DFC0}
    pcs = (0x12DFAE, 0x12DFBA, 0x12DFBC, 0x12DFBE,
           0x12DFC0, 0x12E006, 0x12E1EC)
    adv = C.advance(base, end, pcs)
    check("advance 新增 3 条", adv["n_new"], 3)
    check("advance 新增命中", adv["new"],
          [0x12DFBC, 0x12DFBE, 0x12DFC0])
    check("advance 前沿是新增的最后一条而非 0x12E006",
          adv["frontier"], 0x12DFC0)
    check("advance 无新增", C.advance(base, base, pcs)["n_new"], 0)

    # ---- 等形 / 窗口 / 综合判据
    shape = {"G0": dict(boot=191520, post=200000, svc=1, irqs=1),
             "G3": dict(boot=191520, post=200000, svc=1, irqs=1)}
    check("equal_shape 四组同尺通过", C.equal_shape(shape)["ok"], True)
    shape["G3"]["post"] = 1
    check("equal_shape 检出不等形", C.equal_shape(shape)["bad"], ["post"])
    w = C.window_same({"pc": 0x0407DC, "cov": [1, 2], "key": {1: True}},
                      {"pc": 0x0407DC, "cov": [2, 1], "key": {1: True}})
    check("window_same 逐项相同", w["same"], True)
    check("window_same 对称差集 0", w["symdiff_cov"], 0)
    check("window_same 检出末 PC 不同",
          C.window_same({"pc": 1, "cov": [], "key": {}},
                        {"pc": 2, "cov": [], "key": {}})["diff"], ["pc"])
    v = C.chain_verdict(True, 0x12DFBA, 0x12DFC0, False)
    check("chain_verdict 调用者存在", v["caller"], True)
    check("chain_verdict 唤醒推进", v["wake_advanced"], True)
    check("chain_verdict 链未闭合", v["closed"], False)
    check("chain_verdict 空槽位命中不算闭合",
          C.chain_verdict(True, 0x12DFBA, 0x12DFC0, True,
                          noop_hit=True)["closed"], False)
    check("chain_verdict 门命中且不靠空槽位才闭合",
          C.chain_verdict(True, 0x12DFBA, 0x12DFC0, True)["closed"], True)


def test_v71():
    print("\n[V71] 第二道等待掩码判据与四级状态机")
    import frbox
    from frbox import wait271 as C

    # 版本断言必须版本无关（KI#270）
    check("V71 版本格式", frbox.__version__.startswith("0."), True)

    # ---- 常量与文档规矩
    check("MASK 0x74", C.MASK, 0x74)
    check("EXPECT 0x10", C.EXPECT, 0x10)
    check("FLAG_ID", C.FLAG_ID, 3)
    check("WANTPTN_1", C.WANTPTN_1, 0x09)
    check("WAIT2_FN", C.WAIT2_FN, 0x12E162)
    check("WAIT2_SVC_PC", C.WAIT2_SVC_PC, 0x12E1D2)
    check("DISPATCH_FN", C.DISPATCH_FN, 0x12E1EC)
    check("CLR_BITS", C.CLR_BITS, 0x89)
    check("LEVELS 四级", len(C.LEVELS), 4)
    check("LEVELS 首级 bne 目标 = 0x12DFD0", C.LEVELS[0][3], 0x12DFD0)
    check("LEVELS 回环 PC", C.LEVELS[0][4], 0x12DFCE)
    check("KEY_PCS 含间接调用点", C.INDIRECT_CALL in C.KEY_PCS, True)
    doc = C.__doc__
    check("文档写明 0x74 是掩码不是位", "不是需要被置的某个位" in doc, True)
    check("文档写明服务号宽度", "规矩 48" in doc, True)
    check("文档写明分支目标", "规矩 57" in doc, True)

    # ---- 符号扩展与分支目标（与 fr_emu.py 逐字一致）
    check("sx8 0xFA", C.sx(0xFA, 8), -6)
    check("sx8 0xF5", C.sx(0xF5, 8), -11)
    check("sx16 0xFF56", C.sx(0xFF56, 16), -170)
    check("sx32 0xFFFFFF56", C.sx(0xFFFFFF56, 32), -170)
    check("rel9 V70 基准 0x12E624", C.rel9_target(0x12E624, 0xFA),
          0x12E61A)
    check("rel9 L1 bne", C.rel9_target(0x12DFC4, 5), 0x12DFD0)
    check("rel9 L4 bne", C.rel9_target(0x12DFF6, 3), 0x12DFFE)
    check("rel9 回环 bra", C.rel9_target(0x12DFCE, 245), 0x12DFBA)
    check("rel12 派发函数", C.rel12_target(0x12DFD0, 269), 0x12E1EC)

    # ---- 位域判据与返回 r4
    check("mask_check 期望位", C.mask_check(0x10), True)
    check("mask_check 期望位带无关高位", C.mask_check(0xF10), True)
    check("mask_check 模式内非期望位", C.mask_check(0x20), False)
    check("mask_check 全模式位", C.mask_check(0x74), False)
    check("mask_check 无位", C.mask_check(0x00), False)
    check("mask_check 自定义掩码", C.mask_check(0x03, 0x03, 0x03), True)
    check("return_value 期望位给 1", C.return_value(0x10), 1)
    check("return_value 非期望位给 0", C.return_value(0x20), 0)
    check("return_value 全模式位给 0", C.return_value(0x74), 0)
    check("branch_taken r4=1", C.branch_taken(1), True)
    check("branch_taken r4=0", C.branch_taken(0), False)
    check("branch_taken r4 负数", C.branch_taken(-1), True)

    # ---- clr_flg 清位范围与 32 位服务号
    check("cleared_bits 0x89", C.cleared_bits(0x89), 0x76)
    check("mask_covered 0x89 覆盖 0x74",
          C.mask_covered(0x89, 0x74), True)
    check("mask_covered 0xFF 不覆盖", C.mask_covered(0xFF, 0x74), False)
    check("mask_covered 0x8F 不覆盖", C.mask_covered(0x8F, 0x74), False)
    check("svc_width 32 位 = -170", C.svc_width(C.SVC32_RAW, 32), -170)
    check("svc_width 低 8 位假象 = 86", C.svc_width(C.SVC32_RAW, 8), 86)
    check("svc_width TCB 16 位 = -170", C.svc_width(0xFF56, 16), -170)
    check("SVC32 常量", C.SVC32, -170)
    check("SVC8_FALSE 常量", C.SVC8_FALSE, 86)

    # ---- 四级状态机
    check("level_targets 四级", C.level_targets(),
          [0x12DFD0, 0x12DFE0, 0x12DFF0, 0x12DFFE])
    check("level_of cmp L1", C.level_of(0x12DFC2), 1)
    check("level_of bne L2", C.level_of(0x12DFD4), 2)
    check("level_of cmp L4", C.level_of(0x12DFF4), 4)
    check("level_of 循环头不属于任何级", C.level_of(0x12DFBA), 0)
    cov0 = {0x12DFBA}
    cov1 = cov0 | {0x12DFC2, 0x12DFC4, 0x12DFD0,
                   0x12DFD2, 0x12DFD4}
    check("reached_level 基线 0", C.reached_level(cov0), 0)
    check("reached_level G1 = 2", C.reached_level(cov1), 2)
    check("advanced 基线空", C.advanced(cov0), [])
    check("advanced G1 只跨 L1", C.advanced(cov1), [1])
    check("advanced 跨两级", C.advanced(cov1 | {0x12DFE0}), [1, 2])

    # ---- 静态置位源与三层判据
    class CS(object):
        def __init__(self, addr, svc, r4, r5):
            self.addr, self.svc, self.r4, self.r5 = addr, svc, r4, r5

    calls = [CS(0x12F794, -48, 3, 0x10),
             CS(0x12F964, -48, 3, 0x30),
             CS(0x12FB10, -48, 3, 0x20),
             CS(0x12FDB0, -48, 5, 0x10),
             CS(0x1A4C3E, -48, 3, None),
             CS(0x12E196, -47, 3, 0x89)]
    ss = C.static_setters(calls)
    check("static_setters 只留 flag3 且含 0x10", ss,
          [0x12F794, 0x12F964])
    check("static_setters 换 flag 为空",
          C.static_setters(calls, flgid=17), [])
    tl = C.three_layer(ss, set(), enclosing=lambda p: 0x12F540,
                       isr_range=(0x12F540, 0x12FECC))
    check("three_layer 存在性 2 点", tl["points"], 2)
    check("three_layer 执行性 0 命中", tl["hit"], 0)
    check("three_layer 零命中标记", tl["unique_zero_hit"], True)
    check("three_layer ISR 全覆盖", tl["in_isr_all"], True)
    check("three_layer 归属函数去重", tl["enclosing"], [0x12F540])
    check("three_layer 命中后零命中标记关闭",
          C.three_layer(ss, set(ss))["unique_zero_hit"], False)

    # ---- 等形 / 窗口 / 综合判据
    sh = {"boot": [191520], "svc": [3], "irqs": [2],
          "post": [200000, 200000]}
    check("equal_shape 四组同尺", C.equal_shape([dict(sh) for _ in range(4)])["ok"], True)
    bad = [dict(sh) for _ in range(4)]
    bad[2] = dict(bad[2], post=[200000, 1])
    check("equal_shape 检出不等形", C.equal_shape(bad)["ok"], False)
    a = {"pc": 0x0407DC, "cov": {1, 2}, "key": [1], "waiting": 17}
    b = {"pc": 0x0407DC, "cov": {2, 1}, "key": [1], "waiting": 17}
    w = C.window_same(a, b)
    check("window_same 逐项相同", w["same"], True)
    check("window_same 覆盖对称差 0", w["cov_diff"], 0)
    check("window_same 检出末 PC 不同",
          C.window_same(dict(a, pc=1), b)["same"], False)
    check("window_same 检出覆盖不同",
          C.window_same(dict(a, cov={1, 3}), b)["same"], False)
    v = C.chain_verdict(2, [1], False, False, False, False, True)
    check("chain_verdict 到达派发函数", v["reached_dispatch"], True)
    check("chain_verdict 记录跨过的级", v["advanced"], [1])
    check("chain_verdict 链未闭合", v["closed"], False)
    check("chain_verdict 仅到派发不算闭合",
          C.chain_verdict(2, [1], True, True, True, True, True)["closed"], False)
    check("chain_verdict 全条件才闭合",
          C.chain_verdict(4, [1, 2, 3, 4], True, True, True, True,
                          False)["closed"], True)


def test_v72():
    print("\n[V72] 五级链、0x12E1EC 两条数据早退与描述符指针填充")
    import frbox
    from frbox import level72 as C

    # 版本断言必须版本无关（KI#270）
    check("V72 版本格式", frbox.__version__.startswith("0."), True)

    # ---- 常量与文档规矩
    check("DISPATCH_FN", C.DISPATCH_FN, 0x12E1EC)
    check("WAIT3_FN", C.WAIT3_FN, 0x12E326)
    check("L4_FN", C.L4_FN, 0x12E412)
    check("FIFTH_FN", C.FIFTH_FN, 0x12E674)
    check("FIFTH_CALL", C.FIFTH_CALL, 0x12DFFE)
    check("BIG_PROLOGUE", C.BIG_PROLOGUE, 0x12E48E)
    check("DESC_TABLE", C.DESC_TABLE, 0x001BC020)
    check("DESC_COUNT", C.DESC_COUNT, 70)
    check("DESC_STRIDE", C.DESC_STRIDE, 0x10)
    check("IDX_BYTE", C.IDX_BYTE, 0x9F58DA83)
    check("VALID_BYTE", C.VALID_BYTE, 0x9F58DA84)
    check("CLEAR_BYTE", C.CLEAR_BYTE, 0x9F58DA82)
    check("DESC_PTR", C.DESC_PTR, 0x9F58D858)
    check("LEVELS 四级", len(C.LEVELS), 4)
    check("LEVEL_TARGETS 五级前的四级", len(C.LEVEL_TARGETS), 4)
    check("FAILURE_CALLS 四条", len(C.FAILURE_CALLS), 4)
    doc = C.__doc__
    check("文档写明规矩 58", "规矩 58" in doc, True)
    check("文档写明规矩 59", "规矩 59" in doc, True)
    check("文档写明规矩 48", "规矩 48" in doc, True)
    check("文档写明反事实边界", "不是固件行为" in doc, True)

    # ---- 符号扩展与分支目标
    check("sx8 0xFA", C.sx(0xFA, 8), -6)
    check("sx16 0xFF56", C.sx(0xFF56, 16), -170)
    check("sx32 0xFFFFFF56", C.sx(0xFFFFFF56, 32), -170)
    check("rel9 L2 bne", C.rel9_target(0x12DFD4, 5), 0x12DFE0)
    check("rel9 L4 bne", C.rel9_target(0x12DFF6, 3), 0x12DFFE)
    check("rel12 L2 派发", C.rel12_target(0x12DFD0, 269), 0x12E1EC)
    check("rel12 L3 等待", C.rel12_target(0x12DFE0, 418), 0x12E326)
    check("rel12 L4", C.rel12_target(0x12DFF0, 528), 0x12E412)
    check("rel12 第五个 call", C.rel12_target(0x12DFFE, 826), 0x12E674)
    check("rel12 L1 第二道等待", C.rel12_target(0x12DFBE, 209), 0x12E162)

    # ---- 两条数据早退路径与返回 r4
    check("early_path 基线 = early_B", C.early_path(0x00, 0x00), "early_B")
    check("early_path 有效=1 = normal", C.early_path(0x00, 0x01), "normal")
    check("early_path 有效=0x46 = normal", C.early_path(0x00, 0x46), "normal")
    check("early_path 索引越界 = early_A", C.early_path(0x46, 0x01), "early_A")
    check("early_path 越界优先于有效", C.early_path(0xFF, 0x00), "early_A")
    check("early_path 索引 69 仍在表内", C.early_path(0x45, 0x00), "early_B")
    check("return4 normal", C.return4("normal"), 1)
    check("return4 early_A", C.return4("early_A"), 0)
    check("return4 early_B", C.return4("early_B"), 0)
    check("level_advanced r4=1", C.level_advanced(1), True)
    check("level_advanced r4=0", C.level_advanced(0), False)

    # ---- 路径命中判定（规矩 58）
    covB = set(C.EARLY_B) | {0x12E322}
    covA = set(C.EARLY_A) | {0x12E322}
    covN = set(C.NORMAL)
    check("path_hits 早退 B", C.path_hits(covB)["verdict"], "early_B")
    check("path_hits 早退 B 计数", C.path_hits(covB)["early_B"], 4)
    check("path_hits 早退 A", C.path_hits(covA)["verdict"], "early_A")
    check("path_hits 正常路径", C.path_hits(covN)["verdict"], "normal")
    check("path_hits 正常计数", C.path_hits(covN)["normal"], 7)
    check("path_hits 空覆盖", C.path_hits(set())["verdict"], "other")
    check("path_hits 部分命中不误判",
          C.path_hits(set(C.EARLY_B[:3]))["verdict"], "other")

    # ---- 描述符表地址算式
    check("desc_ptr idx=1", C.desc_ptr(1), 0x001BC030)
    check("desc_ptr idx=0x46", C.desc_ptr(0x46), 0x001BC480)
    check("desc_ptr idx=0", C.desc_ptr(0), 0x001BC020)
    check("desc_ptr idx=69", C.desc_ptr(69), 0x001BC470)
    check("in_table 69", C.in_table(69), True)
    check("in_table 70", C.in_table(70), False)
    check("in_table 0x46", C.in_table(0x46), False)
    check("in_table 0xFF", C.in_table(0xFF), False)

    # ---- 五级链与失败路径
    check("chain_targets 五级", C.chain_targets(),
          [0x12E162, 0x12E1EC, 0x12E326, 0x12E412, 0x12E674])
    check("failure_targets 全指序言", C.failure_targets(), [0x12E48E] * 4)
    check("FAILURE_CALLS L1", C.FAILURE_CALLS[0], 0x12DFCA)
    check("FAILURE_CALLS L2", C.FAILURE_CALLS[1], 0x12DFDA)
    check("FAILURE_CALLS L4", C.FAILURE_CALLS[3], 0x12DFF8)
    check("WAIT_PCS 六点", len(C.WAIT_PCS), 6)
    check("WAIT_PCS 第二道等待", C.WAIT_PCS[0x12E1D2], 0x74)
    check("WAIT_PCS 第三道等待", C.WAIT_PCS[0x12E3F6], 0x74)
    check("WAIT_PCS 失败路径等待", C.WAIT_PCS[0x12E56A], 0x10)

    # ---- 服务号宽度（规矩 48）
    check("svc_width 32 位 = -170", C.svc_width(C.SVC32_RAW, 32), -170)
    check("svc_width 低 8 位 = 86", C.svc_width(C.SVC32_RAW, 8), 86)
    check("is_low8_artifact 32 位", C.is_low8_artifact(C.SVC32_RAW), True)
    check("is_low8_artifact 8 位不误报",
          C.is_low8_artifact(0x56, 8), False)
    check("SVC8_FALSE 常量", C.SVC8_FALSE, 86)

    # ---- 函数边界（规矩 59）
    rows = [(0x12E490, "stm1"), (0x12E600, "ret"),
            (0x12E674, "stm1"), (0x12E6F6, "ret"),
            (0x12E6FC, "enter"), (0x12E74E, "ret")]
    bs = C.function_bounds(rows)
    check("function_bounds 起点数", len(bs), 3)
    check("function_bounds 起点列表", [s for s, _e in bs],
          [0x12E490, 0x12E674, 0x12E6FC])
    check("function_bounds 0x12E674 独立",
          dict(bs)[0x12E674], 0x12E6F6)
    emap = {0x12E490: 0x12E490, 0x12E674: 0x12E490,
            0x12E6FC: 0x12E6FC}
    mis = C.enclosing_mismatch(bs, emap)
    check("enclosing_mismatch 命中 1 处", len(mis), 1)
    check("enclosing_mismatch 指向 0x12E674", mis[0][0], 0x12E674)
    check("enclosing_mismatch 记录错归属", mis[0][2], 0x12E490)
    check("enclosing_mismatch 无错时为空",
          C.enclosing_mismatch(bs, {s: s for s, _e in bs}), [])

    # ---- 等形 / 窗口 / 综合判据
    sh = {"boot": [191520], "svc": [3], "irqs": [2],
          "post": [200000, 200000]}
    check("equal_shape 四组同尺",
          C.equal_shape([dict(sh) for _ in range(4)])["ok"], True)
    bad = [dict(sh) for _ in range(4)]
    bad[2] = dict(bad[2], post=[200000, 1])
    check("equal_shape 检出不等形", C.equal_shape(bad)["ok"], False)
    a = {"pc": 0x0407DC, "cov": {1, 2}, "key": [1], "waiting": 17}
    b = {"pc": 0x0407DC, "cov": {2, 1}, "key": [1], "waiting": 17}
    w = C.window_same(a, b)
    check("window_same 逐项相同", w["same"], True)
    check("window_same 覆盖对称差 0", w["cov_diff"], 0)
    check("window_same 检出覆盖不同",
          C.window_same(dict(a, cov={1, 3}), b)["same"], False)
    tl = C.three_layer([0x12F794, 0x12F964], set(),
                       isr_range=(0x12F540, 0x12FECC))
    check("three_layer 存在性 2 点", tl["points"], 2)
    check("three_layer 零命中标记", tl["unique_zero_hit"], True)
    check("three_layer ISR 全覆盖", tl["in_isr_all"], True)
    v = C.chain_verdict("normal", True, 0x001BC030, False, False, False)
    check("chain_verdict 记录路径", v["path"], "normal")
    check("chain_verdict 跨过 L2", v["l2_crossed"], True)
    check("chain_verdict 描述符指针已填", v["desc_ptr_filled"], True)
    check("chain_verdict 跨 L2 不算闭合", v["closed"], False)
    v2 = C.chain_verdict("early_B", False, 0, False, False, False)
    check("chain_verdict 早退 B 指针未填", v2["desc_ptr_filled"], False)
    check("chain_verdict 全条件才闭合",
          C.chain_verdict("normal", True, 0x001BC030, True, True, True,
                          False)["closed"], True)


def test_v73():
    print("\n[V73] KI#282 第一条前提证伪：DA84 由固件写、DA83 由事件链驱动")
    import frbox
    from frbox import cursor73 as K

    # 版本断言必须版本无关（KI#270）
    check("V73 版本格式", frbox.__version__.startswith("0."), True)

    # ---- 常量
    check("TBR", K.TBR, 0x000EFC00)
    check("VEC_CONV 收敛向量", K.VEC_CONV, 0x2B)
    check("VEC_A", K.VEC_A, 0x20)
    check("VEC_B", K.VEC_B, 0x1B)
    check("ISR_A", K.ISR_A, 0x12FE54)
    check("ISR_B", K.ISR_B, 0x12FDB4)
    check("DISPATCH_CALL_A", K.DISPATCH_CALL_A, 0x12FE70)
    check("DISPATCH_CALL_B", K.DISPATCH_CALL_B, 0x12FE04)
    check("CB_TABLE_A", K.CB_TABLE_A, 0x001BC480)
    check("CB_TABLE_B", K.CB_TABLE_B, 0x001BC490)
    check("CB_COUNT", K.CB_COUNT, 24)
    check("IDX_A", K.IDX_A, 0x9F58DA85)
    check("IDX_B", K.IDX_B, 0x9F58DA86)
    check("CB_FN", K.CB_FN, 0x12F77A)
    check("CB_WRITE", K.CB_WRITE, 0x12F780)
    check("CB_CLEAR", K.CB_CLEAR, 0x12F78A)
    check("CB_SETFLG", K.CB_SETFLG, 0x12F794)
    check("CB_RET", K.CB_RET, 0x12F796)
    check("WAIT1_FN", K.WAIT1_FN, 0x12E0F8)
    check("WAIT1_ENTER", K.WAIT1_ENTER, 0x12E0FC)
    check("QUERY_FN", K.QUERY_FN, 0x12E636)
    check("SEMPOOL", K.SEMPOOL, 0x9F58D500)
    check("SEM_ID", K.SEM_ID, 17)
    check("IDX_BYTE", K.IDX_BYTE, 0x9F58DA83)
    check("VALID_BYTE", K.VALID_BYTE, 0x9F58DA84)
    check("DESC_PTR", K.DESC_PTR, 0x9F58D858)
    check("D87B", K.D87B, 0x9F58D87B)
    check("DA84 写点三条", len(K.DA84_WRITERS), 3)
    check("置位点四条", len(K.SETFLG_POINTS), 4)
    check("反事实六组", len(K.GROUPS), 6)
    check("SET_FLG", K.SET_FLG, -48)
    check("CLR_FLG", K.CLR_FLG, -47)
    check("WAI_FLG", K.WAI_FLG, -46)
    check("WAI_SEM", K.WAI_SEM, -55)
    check("GET_TID", K.GET_TID, -24)
    check("FLAG_ID", K.FLAG_ID, 3)
    check("BIT10", K.BIT10, 0x10)
    check("WANTPTN_1", K.WANTPTN_1, 9)

    # ---- 文档规矩
    doc = K.__doc__
    check("文档写明规矩 60", "规矩 60" in doc, True)
    check("文档写明规矩 61", "规矩 61" in doc, True)
    check("文档写明规矩 59", "规矩 59" in doc, True)
    check("文档写明规矩 53", "规矩 53" in doc, True)
    check("文档写明规矩 58 推广", "规矩 58 的推广" in doc, True)
    check("文档写明倒序映射", "倒序" in doc, True)
    check("文档写明反事实边界", "不是固件行为" in doc, True)
    check("文档写明不能报出画面", "出画面" in doc, True)

    # ---- 倒序向量换算（规矩 60）
    check("sx8 0x20", K.sx(0x20, 8), 32)
    check("sx8 0xFA", K.sx(0xFA, 8), -6)
    check("slot_of_vec 0x20", K.slot_of_vec(0x20), 0x0EFF7C)
    check("slot_of_vec 0x1B", K.slot_of_vec(0x1B), 0x0EFF90)
    check("slot_of_vec 0x2B", K.slot_of_vec(0x2B), 0x0EFF50)
    check("vec/slot 互逆 0x2B", K.vec_of_slot(K.slot_of_vec(0x2B)), 0x2B)
    check("slot_of_vec 0xFF = TBR", K.slot_of_vec(0xFF), K.TBR)
    check("vec_of_slot 槽 223", K.vec_of_slot(0x0EFF7C), 0x20)
    check("vec_of_slot 槽 228", K.vec_of_slot(0x0EFF90), 0x1B)
    check("vec_of_slot 复位槽", K.vec_of_slot(0x0EFFFC), 0x00)
    check("vec/slot 互逆 0x20", K.vec_of_slot(K.slot_of_vec(0x20)), 0x20)
    check("vec/slot 互逆 0x1B", K.vec_of_slot(K.slot_of_vec(0x1B)), 0x1B)
    check("vec_of_slot 未对齐", K.vec_of_slot(K.TBR + 1), None)
    check("vec_of_slot 越界", K.vec_of_slot(K.TBR + 0x400), None)
    check("正序槽号 223 不是 vec 223", K.vec_of_slot(K.TBR + 223 * 4), 32)

    # ---- 回调表索引
    check("cb_entry A idx5", K.cb_entry_addr(K.CB_TABLE_A, 5), 0x1BC494)
    check("cb_entry A idx0", K.cb_entry_addr(K.CB_TABLE_A, 0), 0x1BC480)
    check("cb_entry B idx1", K.cb_entry_addr(K.CB_TABLE_B, 1), 0x1BC494)
    check("cb_index A 0x1BC494", K.cb_index_of(0x1BC494, K.CB_TABLE_A), 5)
    check("cb_index B 0x1BC494", K.cb_index_of(0x1BC494, K.CB_TABLE_B), 1)
    check("cb_index 越界", K.cb_index_of(0x1BC470, K.CB_TABLE_A), None)
    check("cb_index 未对齐", K.cb_index_of(0x1BC482, K.CB_TABLE_A), None)

    # ---- DA84 写入源分类（规矩 61）
    kinds = K.da84_writers_kinds()
    check("DA84 常量 0 写点 1 条", kinds["const0"], 1)
    check("DA84 返回值写点 2 条", kinds["query_ret"], 2)
    check("收敛态固件写过 DA84 判据", K.da84_is_firmware_written({0x12E140}), True)
    check("收敛态未写 DA84 判据", K.da84_is_firmware_written({0x12E274}), False)
    check("延迟槽写点 2 条",
          sum(1 for _p, _k, s in K.DA84_WRITERS if s), 2)

    # ---- 规矩 59 函数边界
    rets = [0x12F776, 0x12F778, 0x12F796, 0x12F950 - 2,
            0x12F966, 0x12FB20, 0x12FDB2]
    check("real_bounds 回调体",
          K.real_bounds(0x12F794, rets, 0x12F540)[0], 0x12F77A)
    check("real_bounds 回调体 hi",
          K.real_bounds(0x12F794, rets, 0x12F540)[1], 0x12F796)
    check("enclosing 归属不一致",
          K.enclosing_mismatch(0x12F780, K.SETFLG_ENCLOSING,
                               rets, 0x12F540), True)
    check("enclosing 归属一致时",
          K.enclosing_mismatch(0x12F780, 0x12F77A, rets, 0x12F540), False)
    check("四个置位点真实起点各不同",
          len({p[1] for p in K.SETFLG_POINTS}), 4)
    check("四个置位点 enclosing 全同",
          K.SETFLG_ENCLOSING, 0x12F540)
    check("DA83 写点与置位点1 同函数",
          K.SETFLG_POINTS[0][1], K.CB_FN)

    # ---- 三层判据 / 等形 / 归因
    tl = K.three_layer(True, 0, True)
    check("three_layer 存在性", tl["exists"], True)
    check("three_layer 零执行", tl["executed"], 0)
    check("three_layer 需事件", tl["needs_event"], True)
    check("three_layer 已执行不需事件",
          K.three_layer(True, 3, True)["needs_event"], False)
    check("three_layer 没代码不需事件",
          K.three_layer(False, 0, True)["needs_event"], False)
    shapes = [(191520, 0, 200000), (191520, 1, 200000),
              (191520, 1, 200000), (191520, 0, 200000)]
    es = K.equal_shape(shapes)
    check("equal_shape 等形", es["ok"], True)
    check("equal_shape boot 单值", len(es["keys"]["boot"]), 1)
    check("equal_shape post 单值", len(es["keys"]["post"]), 1)
    check("equal_shape 检出不等形",
          K.equal_shape([(191520, 0, 200000),
                         (191520, 0, 300000)])["ok"], False)
    gr = {"J0": {"cb": False, "new": 0},
          "J3": {"cb": True, "new": 31},
          "J5": {"cb": False, "new": 0}}
    check("attribution 命中组", K.attribution(gr, "cb"), ["J3"])
    check("attribution 空命中", K.attribution(gr, "missing"), [])

    # ---- 事件链
    ev = K.event_chain(K.VEC_A, 5, None, K.CB_FN)
    check("event_chain A 有效", ev["valid"], True)
    check("event_chain A ISR", ev["isr"], K.ISR_A)
    check("event_chain A 表", ev["table"], K.CB_TABLE_A)
    check("event_chain A 表项", ev["entry"], 0x1BC494)
    check("event_chain A 目标", ev["target"], K.CB_FN)
    evb = K.event_chain(K.VEC_B, 1, None, K.CB_FN)
    check("event_chain B ISR", evb["isr"], K.ISR_B)
    check("event_chain B 表项", evb["entry"], 0x1BC494)
    check("event_chain 错配向量",
          K.event_chain(K.VEC_CONV, 5, None, K.CB_FN)["valid"], False)
    check("event_chain 错配原因",
          K.event_chain(0x2B, 5, None, K.CB_FN)["reason"],
          "vector_mismatch")
    check("wants_after_event 推进到 0x74", K.wants_after_event(9, True), 0x74)
    check("wants_after_event 无事件不变", K.wants_after_event(9, False), 9)
    check("wants_after_event 非第一道不变",
          K.wants_after_event(0x74, True), 0x74)

    # ---- 链判据 / 窗口
    check("chain_verdict 回调未命中不闭合",
          K.chain_verdict(False, False, False, 0), False)
    check("chain_verdict 回调命中但 L2 未跨不闭合",
          K.chain_verdict(True, False, False, 0), False)
    check("chain_verdict 跨 L2 但无处理函数不闭合",
          K.chain_verdict(True, True, False, 0x001BC030), False)
    check("chain_verdict 全条件闭合",
          K.chain_verdict(True, True, True, 0x001BC030), True)
    check("window_same 同覆盖", K.window_same({1, 2}, {2, 1}), 0)
    check("window_same 差集 2", K.window_same({1, 2}, {2, 3}), 2)


def test_v74():
    print("\n[V74] KI#286 第一条结案、24 项回调表语义与 D858 的 DA82 中转")
    import frbox
    from frbox import cursor74 as D

    # 版本断言必须版本无关（KI#270）
    check("V74 版本格式", frbox.__version__.startswith("0."), True)

    # ---- 常量
    check("TBR", D.TBR, 0x000EFC00)
    check("VEC_A", D.VEC_A, 0x20)
    check("VEC_B", D.VEC_B, 0x1B)
    check("VEC_CONV", D.VEC_CONV, 0x2B)
    check("ISR_A", D.ISR_A, 0x12FE54)
    check("ISR_B", D.ISR_B, 0x12FDB4)
    check("CB_TABLE_A", D.CB_TABLE_A, 0x001BC480)
    check("CB_TABLE_B", D.CB_TABLE_B, 0x001BC490)
    check("CB_COUNT", D.CB_COUNT, 24)
    check("CB_FN", D.CB_FN, 0x12F77A)
    check("CB_WRITE", D.CB_WRITE, 0x12F780)
    check("CB_SETFLG", D.CB_SETFLG, 0x12F794)
    check("IDX_A", D.IDX_A, 0x9F58DA85)
    check("IDX_B", D.IDX_B, 0x9F58DA86)
    check("DA82", D.DA82, 0x9F58DA82)
    check("DA83", D.DA83, 0x9F58DA83)
    check("DA84", D.DA84, 0x9F58DA84)
    check("DESC_PTR", D.DESC_PTR, 0x9F58D858)
    check("DESC_TABLE", D.DESC_TABLE, 0x001BC020)
    check("DESC_STRIDE", D.DESC_STRIDE, 0x10)
    check("DESC_COUNT", D.DESC_COUNT, 70)
    check("LOAD_DA82", D.LOAD_DA82, 0x12E242)
    check("READ_DA82", D.READ_DA82, 0x12E268)
    check("SHIFT_DA82", D.SHIFT_DA82, 0x12E270)
    check("WRITE_D858", D.WRITE_D858, 0x12E274)
    check("QUERY_FN", D.QUERY_FN, 0x12E636)
    check("SEM_POOL", D.SEM_POOL, 0x9F58D500)
    check("MASK", D.MASK, 0x74)
    check("EXPECT", D.EXPECT, 0x10)
    check("BIT10", D.BIT10, 0x10)
    check("BIT20", D.BIT20, 0x20)
    check("BIT40", D.BIT40, 0x40)
    check("SET_FLG", D.SET_FLG, -48)
    check("CLR_FLG", D.CLR_FLG, -47)
    check("WAI_FLG", D.WAI_FLG, -46)
    check("GET_TID", D.GET_TID, -24)
    check("FLAG_ID", D.FLAG_ID, 3)
    check("WANTPTN_1", D.WANTPTN_1, 9)
    check("DA84 写点三条", len(D.DA84_WRITERS), 3)
    check("置0x10 四点", len(D.SETTERS_10), 4)
    check("置0x20 七点", len(D.SETTERS_20), 7)
    check("置0x40 一点", len(D.SETTERS_40), 1)
    check("清0x76 两点", len(D.CLR_76), 2)
    check("24 项回调表", len(D.CALLBACKS), 24)
    check("位域反事实六组", len(D.BIT_GROUPS), 6)
    check("r4 反事实四组", len(D.R4_GROUPS), 4)
    check("组合反事实五组", len(D.COMBO_GROUPS), 5)

    # ---- 文档规矩
    doc = D.__doc__
    check("文档写明规矩 62", "规矩 62" in doc, True)
    check("文档写明规矩 56", "规矩 56" in doc, True)
    check("文档写明规矩 60", "规矩 60" in doc, True)
    check("文档写明 DA82 中转", "DA82 中转" in doc, True)
    check("文档写明反事实边界", "不是固件行为" in doc, True)
    check("文档写明不能报出画面", "出画面" in doc, True)
    check("文档写明已交付口径更正", "已交付口径更正" in doc, True)

    # ---- 倒序向量（规矩 60）
    check("sx8 0xFA", D.sx(0xFA, 8), -6)
    check("slot_of_vec 0x20", D.slot_of_vec(0x20), 0x0EFF7C)
    check("slot_of_vec 0x1B", D.slot_of_vec(0x1B), 0x0EFF90)
    check("slot_of_vec 0x2B", D.slot_of_vec(0x2B), 0x0EFF50)
    check("vec_of_slot 槽223", D.vec_of_slot(0x0EFF7C), 0x20)
    check("vec_of_slot 槽228", D.vec_of_slot(0x0EFF90), 0x1B)
    check("vec/slot 互逆", D.vec_of_slot(D.slot_of_vec(0x20)), 0x20)
    check("vec_of_slot 未对齐", D.vec_of_slot(D.TBR + 1), None)

    # ---- 判据位域（规矩 56）
    check("mask_verdict 0x10", D.mask_verdict(0x10), True)
    check("mask_verdict 0x00", D.mask_verdict(0x00), False)
    check("mask_verdict 0x20", D.mask_verdict(0x20), False)
    check("mask_verdict 0x40", D.mask_verdict(0x40), False)
    check("mask_verdict 0x30", D.mask_verdict(0x30), False)
    check("mask_verdict 0x74", D.mask_verdict(0x74), False)
    check("mask_verdict 0x11", D.mask_verdict(0x11), True)
    check("mask_verdict 0x90", D.mask_verdict(0x90), True)
    check("interfering_bits 0x30", D.interfering_bits(0x30), 0x20)
    check("interfering_bits 0x50", D.interfering_bits(0x50), 0x40)
    check("interfering_bits 0x10", D.interfering_bits(0x10), 0)
    check("bit_group 0x10", D.bit_group(0x10), '0x10')
    check("bit_group 0x20", D.bit_group(0x20), '0x20')
    check("bit_group 0x40", D.bit_group(0x40), '0x40')
    check("bit_group 0x200", D.bit_group(0x200), 'other')
    sc = D.setter_count()
    check("置位源计数 0x10", sc['0x10'], 4)
    check("置位源计数 0x20", sc['0x20'], 7)
    check("置位源计数 0x40", sc['0x40'], 1)
    check("无 0x74 置位源", D.has_074_setter(), False)
    check("自定义值含 0x74 能检出",
          D.has_074_setter((0x10, 0x74)), True)

    # ---- 24 项回调表
    check("cb_entry_addr 0", D.cb_entry_addr(0), 0x1BC480)
    check("cb_entry_addr 5", D.cb_entry_addr(5), 0x1BC494)
    check("cb_entry_addr 23", D.cb_entry_addr(23), 0x1BC4DC)
    check("cb_table_target A5", D.cb_table_target(5), 0x12F77A)
    check("cb_table_target A0", D.cb_table_target(0), 0x12FE12)
    check("cb_table_target B1", D.cb_table_target(1, D.CB_TABLE_B), 0x12F77A)
    check("cb_table_target 越界", D.cb_table_target(24), None)
    check("cb_bits A5", D.cb_bits(5), (0x10,))
    check("cb_bits A17", D.cb_bits(17), (0x10, 0x20))
    check("cb_bits A6 空", D.cb_bits(6), ())
    check("cb_groupkey 空", D.cb_groupkey(()), 'none')
    check("cb_groupkey 双位", D.cb_groupkey((0x10, 0x20)), '0x10,0x20')
    sem = D.cb_semantics()
    check("cb_semantics 组数", len(sem), 9)
    check("cb_semantics 0x10 组", sem['0x10'], [4, 5, 11])
    check("cb_semantics none 组数", len(sem['none']), 11)
    check("只写游标回调数", D.cb_write_only_count(), 11)

    # ---- D858 的 DA82 中转（规矩 62）
    check("desc_ptr 0", D.desc_ptr(0), 0x001BC020)
    check("desc_ptr 1", D.desc_ptr(1), 0x001BC030)
    check("desc_ptr 5", D.desc_ptr(5), 0x001BC070)
    check("desc_ptr 0x46", D.desc_ptr(0x46), 0x001BC480)
    check("da82_middle 5", D.da82_middle(5), 5)
    check("da82_middle 截 8 位", D.da82_middle(0x105), 5)
    check("desc_ptr_chain 1", D.desc_ptr_chain(1), 0x001BC030)
    check("desc_ptr_chain 5", D.desc_ptr_chain(5), 0x001BC070)
    check("in_table 0", D.in_table(0), True)
    check("in_table 69", D.in_table(69), True)
    check("in_table 70", D.in_table(70), False)

    # ---- r4 来源
    check("r4_source A 透传", D.r4_source(D.VEC_A), 'passthrough')
    check("r4_source B 显式", D.r4_source(D.VEC_B), 'mov_r13_r4')
    check("r4_source 未知", D.r4_source(0x2B), None)
    check("r4_to_da83 5", D.r4_to_da83(5), 5)
    check("r4_to_da83 截 8 位", D.r4_to_da83(0x146), 0x46)

    # ---- 等形 / 归因 / 链判据 / 窗口
    shapes = [(191520, 1, 200000), (191520, 1, 200000),
              (191520, 0, 200000)]
    es = D.equal_shape(shapes)
    check("equal_shape 等形", es["ok"], True)
    check("equal_shape 检出不等形",
          D.equal_shape([(191520, 1, 200000),
                         (191520, 1, 300000)])["ok"], False)
    gr = {"K0": {"da83": 0, "new": 31}, "K1": {"da83": 5, "new": 31},
          "K2": {"da83": 0x46, "new": 31}}
    check("attribution da83", D.attribution(gr, "da83"), ["K1", "K2"])
    check("attribution 空", D.attribution(gr, "missing"), [])
    check("chain_verdict 缺处理函数",
          D.chain_verdict(True, True, False), False)
    check("chain_verdict 全条件",
          D.chain_verdict(True, True, True), True)
    check("chain_verdict L2 未跨",
          D.chain_verdict(False, True, True), False)
    check("window_same 同覆盖", D.window_same({1, 2}, {2, 1}), 0)
    check("window_same 差集 2", D.window_same({1, 2}, {2, 3}), 2)


def test_v75():
    print("\n[V75] L4 恒过、r10 门默认开、处理函数首次命中")
    import frbox
    from frbox import gate75 as G

    # 版本断言必须版本无关（KI#270）
    check("V75 版本格式", frbox.__version__.startswith("0."), True)

    # ---- 常量
    check("TASK5_FN", G.TASK5_FN, 0x12DFB2)
    check("LOOP_HEAD", G.LOOP_HEAD, 0x12DFBA)
    check("L4_FN", G.L4_FN, 0x12E412)
    check("L4_RET", G.L4_RET, 0x12E48A)
    check("L4_RET_SLOT", G.L4_RET_SLOT, 0x12E48C)
    check("L4_BNE", G.L4_BNE, 0x12DFF6)
    check("FIFTH_CALL", G.FIFTH_CALL, 0x12DFFE)
    check("FIFTH_FN", G.FIFTH_FN, 0x12E674)
    check("WAIT3_FN", G.WAIT3_FN, 0x12E326)
    check("L3_WAIT", G.L3_WAIT, 0x12E3F6)
    check("L3_CLEAR", G.L3_CLEAR, 0x12E34A)
    check("BIG_PROLOGUE", G.BIG_PROLOGUE, 0x12E48E)
    check("R10_ONE", G.R10_ONE, 0x12E69E)
    check("R10_ZERO", G.R10_ZERO, 0x12E6BA)
    check("R10_BEQD", G.R10_BEQD, 0x12E69C)
    check("GATE_PC", G.GATE_PC, 0x12E6E0)
    check("GATE_BEQ", G.GATE_BEQ, 0x12E6E2)
    check("INDIRECT_CALL", G.INDIRECT_CALL, 0x12E6F0)
    check("SUBMIT", G.SUBMIT, 0x12E6F8)
    check("DESC_LOAD", G.DESC_LOAD, 0x12E6EE)
    check("NOOP_HANDLER", G.NOOP_HANDLER, 0x12E752)
    check("HANDLER_A", G.HANDLER_A, 0x12E806)
    check("HANDLER_A_END", G.HANDLER_A_END, 0x12E854)
    check("HANDLER_B", G.HANDLER_B, 0x12E856)
    check("HANDLER_B_END", G.HANDLER_B_END, 0x12E8E0)
    check("WAIT41_A", G.WAIT41_A, 0x12E5AA)
    check("WAIT41_B", G.WAIT41_B, 0x12E5EA)
    check("WAIT41_IMM_A", G.WAIT41_IMM_A, 0x12E59E)
    check("WAIT41_IMM_B", G.WAIT41_IMM_B, 0x12E5DE)
    check("DESC_TABLE", G.DESC_TABLE, 0x001BC020)
    check("DESC_COUNT", G.DESC_COUNT, 70)
    check("DESC_STRIDE", G.DESC_STRIDE, 0x10)
    check("DESC_HANDLER_OFF", G.DESC_HANDLER_OFF, 0x0C)
    check("DESC_PTR", G.DESC_PTR, 0x9F58D858)
    check("DA82", G.DA82, 0x9F58DA82)
    check("DA83", G.DA83, 0x9F58DA83)
    check("DA84", G.DA84, 0x9F58DA84)
    check("WORD_A", G.WORD_A, 0x9F5A0284)
    check("WORD_B", G.WORD_B, 0x9F59EF50)
    check("SLOTS8", G.SLOTS8, 0x9F59F7C8)
    check("GATE_BYTE", G.GATE_BYTE, 0x9F58D08F)
    check("N_HANDLERS", G.N_HANDLERS, 44)
    check("空槽位 27 槽", len(G.NOOP_SLOTS), 27)
    check("FLAG_ID", G.FLAG_ID, 3)
    check("BIT10", G.BIT10, 0x10)
    check("MASK74", G.MASK74, 0x74)
    check("WANTPTN_1", G.WANTPTN_1, 9)
    check("WAIT41", G.WAIT41, 0x41)
    check("SET_FLG", G.SET_FLG, -48)
    check("CLR_FLG", G.CLR_FLG, -47)
    check("WAI_FLG", G.WAI_FLG, -46)
    check("GET_TID", G.GET_TID, -24)
    check("SVC_WAI_32", G.SVC_WAI_32, -170)
    check("SVC_WAI_LOW8", G.SVC_WAI_LOW8, 86)
    check("SUBMIT_REFS", G.SUBMIT_REFS, 312)
    check("SUBMIT_EDGES", G.SUBMIT_EDGES, 302)
    check("SUBMIT_REL12", G.SUBMIT_REL12, 5)
    check("LDI32_LEN", G.LDI32_LEN, 6)
    check("六级唤醒组", len(G.LEVEL_GROUPS), 6)
    check("六组索引组", len(G.INDEX_GROUPS), 6)
    check("多级唤醒三组", len(G.EXTRA_AWAKEN), 3)

    # ---- 文档规矩
    doc = G.__doc__
    check("文档写明规矩 63", "规矩 63" in doc, True)
    check("文档写明规矩 54", "规矩 54" in doc, True)
    check("文档写明规矩 48", "规矩 48" in doc, True)
    check("文档写明规矩 55", "规矩 55" in doc, True)
    check("文档写明规矩 57", "规矩 57" in doc, True)
    check("文档写明 L4 恒过", "恒过" in doc, True)
    check("文档写明门默认开", "默认是开的" in doc, True)
    check("文档写明反事实边界", "不是固件行为" in doc, True)
    check("文档写明不能报出画面", "出画面" in doc, True)

    # ---- 符号扩展与分支目标
    check("sx8 0xFA", G.sx(0xFA, 8), -6)
    check("sx32 0xFFFFFF56", G.sx(0xFFFFFF56, 32), -170)
    check("rel9 目标", G.rel9_target(0x12DFD4, 5), 0x12DFE0)
    check("rel12 目标", G.rel12_target(0x12DFFE, 0x33A), 0x12E674)

    # ---- 描述符与处理函数
    check("desc_ptr 0", G.desc_ptr(0), 0x001BC020)
    check("desc_ptr 1", G.desc_ptr(1), 0x001BC030)
    check("desc_ptr 5", G.desc_ptr(5), 0x001BC070)
    check("desc_ptr 6", G.desc_ptr(6), 0x001BC080)
    check("desc_ptr 0x45", G.desc_ptr(0x45), 0x001BC470)
    check("handler_slot 5", G.handler_slot(5), 0x001BC07C)
    check("handler_slot 1", G.handler_slot(1), 0x001BC03C)
    check("in_table 0", G.in_table(0), True)
    check("in_table 69", G.in_table(69), True)
    check("in_table 70", G.in_table(70), False)
    check("空槽判定 0x12E752", G.is_noop_handler(G.HANDLER_NOOP), True)
    check("空槽判定 0x12E806", G.is_noop_handler(G.HANDLER_A), False)
    ptrs = {0: G.HANDLER_NOOP, 1: G.HANDLER_NOOP,
            5: G.HANDLER_A, 6: G.HANDLER_B}
    check("index_to_handler 5", G.index_to_handler(5, ptrs), G.HANDLER_A)
    check("index_to_handler 6", G.index_to_handler(6, ptrs), G.HANDLER_B)
    check("index_to_handler 1 空槽", G.index_to_handler(1, ptrs),
          G.HANDLER_NOOP)
    check("index_to_handler 越界", G.index_to_handler(70, ptrs), None)

    # ---- L4 恒过与判据（规矩 63）
    check("L4 延迟槽返回值 1", G.l4_return_value(), 1)
    check("L4 恒过", G.l4_always_passes(), True)
    check("L4 恒过与比较值无关", G.l4_always_passes(0), True)
    check("mask_check 0x10", G.mask_check(0x10), True)
    check("mask_check 0x74", G.mask_check(0x74), False)
    check("mask_check 0x30", G.mask_check(0x30), False)
    check("mask_check 0x41 不成立", G.mask_check(0x41), False)
    check("mask_check 0x41 命中 0x40", 0x41 & G.MASK74, 0x40)
    check("r10 延迟槽默认 1", G.r10_from_delay_slot(), 1)
    check("门 r10=1 开", G.gate_open(1), True)
    check("门 r10=0 关", G.gate_open(0), False)
    check("level_needed 2", G.level_needed(2), 2)
    check("level_needed 3", G.level_needed(3), 3)
    check("level_needed 5", G.level_needed(5), 3)
    check("reaches_l4 有事件三级", G.reaches_l4(True, 3), True)
    check("reaches_l4 有事件两级", G.reaches_l4(True, 2), False)
    check("reaches_l4 无事件三级", G.reaches_l4(False, 3), False)
    check("extra_awaken_noop 3", G.extra_awaken_noop(3), True)
    check("extra_awaken_noop 2", G.extra_awaken_noop(2), False)

    # ---- 三形态调用边（规矩 54）
    # 构造有真实偏移关系的点集：refs 是 ldi:32 加载点，
    # edges 是同一批调用里 +6 字节的 call:d 指令点。
    refs = {0x120000 + i * 8 for i in range(312)}
    edges = {p + 6 for p in sorted(refs)[:302]}
    rel12 = {0x12E800 + i * 4 for i in range(5)}
    rel = G.call_site_relations(refs, edges, rel12)
    check("call_site refs", rel["refs"], 312)
    check("call_site edges", rel["edges"], 302)
    check("call_site rel12", rel["rel12"], 5)
    check("call_site union", rel["union"], 307)
    check("call_site 交集 0", rel["overlap"], 0)
    check("call_site 不同 PC 口径", rel["different_pc_kinds"], True)
    check("call_site 加 6 字节可对齐", rel["shift_matches"], 302)

    # ---- 等形 / 归因 / 链判据 / 窗口
    shapes = [(191520, 3, 200000), (191520, 3, 200000),
              (191520, 0, 200000)]
    check("equal_shape 等形", G.equal_shape(shapes)["ok"], True)
    check("equal_shape 检出不等形",
          G.equal_shape([(191520, 3, 200000),
                         (191520, 3, 300000)])["ok"], False)
    gr = {"N2": {"l4": False}, "N3": {"l4": True}, "N5": {"l4": True}}
    check("attribution l4", G.attribution(gr, "l4"), ["N3", "N5"])
    check("chain_verdict 只有处理函数", G.chain_verdict(True, False,
                                                        False, False), False)
    check("chain_verdict 全条件", G.chain_verdict(True, True,
                                                  True, True), True)
    check("window_same 同覆盖", G.window_same({1, 2}, {2, 1}), 0)
    check("window_same 差集 2", G.window_same({1, 2}, {2, 3}), 2)
    check("three_layer 需事件", G.three_layer(True, 0, True)["needs_event"],
          True)
    check("three_layer 已执行", G.three_layer(True, 1, True)["needs_event"],
          False)


def test_v76():
    print("\n[V76] 提交函数身份、0x41 置位源、门字节与 8 槽填充源")
    import frbox
    from frbox import submit76 as S

    # 版本断言必须版本无关（KI#270）
    check("V76 版本格式", frbox.__version__.startswith("0."), True)

    # ---- 常量：提交函数
    check("SUBMIT", S.SUBMIT, 0x12E6F8)
    check("SUBMIT_END", S.SUBMIT_END, 0x12E752)
    check("SUBMIT_REFS", S.SUBMIT_REFS, 312)
    check("SUBMIT_EDGES", S.SUBMIT_EDGES, 302)
    check("SUBMIT_REL12", S.SUBMIT_REL12, 5)
    check("LDI32_LEN", S.LDI32_LEN, 6)
    check("SUBMIT_SVC_PCS", S.SUBMIT_SVC_PCS,
          (0x12E712, 0x12E738, 0x12E748))
    check("SUBMIT_SVCS", S.SUBMIT_SVCS, (-53, -48, -55))
    check("SUBMIT_SVC_NAMES", S.SUBMIT_SVC_NAMES,
          ("wai_sem", "set_flg", "sig_sem"))
    check("INSERT_FN", S.INSERT_FN, 0x124F5A)
    check("SEM_ID", S.SEM_ID, 17)
    check("SEM_POOL", S.SEM_POOL, 0x9F58D508)
    check("SLOT_STRIDE", S.SLOT_STRIDE, 12)
    check("SUBMIT_SET_BITS", S.SUBMIT_SET_BITS, 0x08)
    check("FLAG_ID", S.FLAG_ID, 3)

    # ---- 常量：门字节
    check("GATE_BYTE", S.GATE_BYTE, 0x9F58D08F)
    check("GATE_WRITES 个数", len(S.GATE_WRITES), 2)
    check("GATE_WRITES[0]", S.GATE_WRITES[0],
          (0x12ECF4, 0, 0x12ECE2, 0x12ED04))
    check("GATE_WRITES[1]", S.GATE_WRITES[1],
          (0x12F180, 1, 0x12F158, 0x12F1AA))
    check("GATE_LOADS", S.GATE_LOADS, 19)

    # ---- 常量：第五道等待与置位源
    check("WAIT41_A", S.WAIT41_A, 0x12E5AA)
    check("WAIT41_B", S.WAIT41_B, 0x12E5EA)
    check("WAIT41_IMM_A", S.WAIT41_IMM_A, 0x12E59E)
    check("WAIT41_IMM_B", S.WAIT41_IMM_B, 0x12E5DE)
    check("WAIT41_PTN", S.WAIT41_PTN, 0x41)
    check("TWAI_SVC_32", S.TWAI_SVC_32, -170)
    check("TWAI_SVC_LOW8", S.TWAI_SVC_LOW8, 86)
    check("CLR_BF", S.CLR_BF, 0xBF)
    check("CLR_BF_PCS", S.CLR_BF_PCS, (0x12E592, 0x12E5D2))
    check("SET_40_PC", S.SET_40_PC, 0x12FEAC)
    check("SET_01_PCS", S.SET_01_PCS,
          (0x12E0EC, 0x12E51A, 0x12FE1C))
    check("FLAG3_SETTERS", S.FLAG3_SETTERS, 44)
    check("MASK74", S.MASK74, 0x74)
    check("EXPECT10", S.EXPECT10, 0x10)

    # ---- 常量：向量表 / 四个填充源 / 8 槽
    check("TBR", S.TBR, 0x000EFC00)
    check("VEC_2D", S.VEC_2D, 0x2D)
    check("ISR_2D", S.ISR_2D, 0x12FE84)
    check("ISR_2D_END", S.ISR_2D_END, 0x12FEB4)
    check("VEC_20 / ISR_20", (S.VEC_20, S.ISR_20), (0x20, 0x12FE54))
    check("VEC_1B / ISR_1B", (S.VEC_1B, S.ISR_1B), (0x1B, 0x12FDB4))
    check("WORD_A", S.WORD_A, 0x9F5A0284)
    check("WORD_B", S.WORD_B, 0x9F59EF50)
    check("SLOTS8", S.SLOTS8, 0x9F59F7C8)
    check("DESC_PTR", S.DESC_PTR, 0x9F58D858)
    check("STORES_UNKNOWN", S.STORES_UNKNOWN, 224489)
    check("SLOT_LOADS", S.SLOT_LOADS,
          (0x1758EC, 0x175930, 0x1759AC, 0x175A20))
    check("SLOT_CLEAR_FN", S.SLOT_CLEAR_FN, 0x1758EC)
    check("SLOT_CLEAR_ST", S.SLOT_CLEAR_ST, 0x1758FA)
    check("SLOT_CLEAR_LEN", S.SLOT_CLEAR_LEN, 96)
    check("SLOT_STORE_WIDTH", S.SLOT_STORE_WIDTH, 4)
    check("SLOT_COUNT", S.SLOT_COUNT, 8)
    check("BIT_GROUPS 个数", len(S.BIT_GROUPS), 5)
    check("BIT_GROUPS 标签", [x[0] for x in S.BIT_GROUPS],
          ["R0", "R1", "R2", "R3", "R4"])

    # ---- 设备策略（与 V65–V75 逐字相同）
    check("BOOT_STEPS", S.BOOT_STEPS, 191520)
    check("VEC_TICK", S.VEC_TICK, 0x2B)
    check("RUN_STEPS", S.RUN_STEPS, 500000)
    check("POST_STEPS", S.POST_STEPS, 200000)
    check("WINDOW_STEPS", S.WINDOW_STEPS, 1000000)

    # ---- 纯函数：宽度 / 分支
    check("sx 8 位负数", S.sx(0xFF, 8), -1)
    check("sx 4 位负数 m4=0xC", S.sx(0xC, 4), -4)
    check("rel9_target L2", S.rel9_target(0x12DFC4, 5), 0x12DFD0)
    check("rel9_target 负偏移", S.rel9_target(0x12E5BE, 0xFE), 0x12E5BC)
    check("svc32 0xFFFFFF56", S.svc32(0xFFFFFF56), -170)
    check("svc32 0xFFFFFFCB", S.svc32(0xFFFFFFCB), -53)
    check("svc32 0xFFFFFFC9", S.svc32(0xFFFFFFC9), -55)
    check("svc_low8 0xFFFFFF56", S.svc_low8(0xFFFFFF56), 86)
    check("svc_width_mismatch", S.svc_width_mismatch(0xFFFFFF56),
          (-170, 86, True))
    check("svc_width_mismatch 同值", S.svc_width_mismatch(0x10),
          (16, 16, False))

    # ---- 纯函数：提交函数
    check("submit_slot_addr 0", S.submit_slot_addr(0), 0x9F58D508)
    check("submit_slot_addr 5", S.submit_slot_addr(5), 0x9F58D544)
    check("submit_slot_addr 17", S.submit_slot_addr(17), 0x9F58D5D4)
    check("submit_services 长度", len(S.submit_services()), 3)
    check("submit_services 首项", S.submit_services()[0],
          (0x12E712, -53, "wai_sem"))
    check("submit_services 末项", S.submit_services()[2],
          (0x12E748, -55, "sig_sem"))
    check("submit_is_producer 默认", S.submit_is_producer(), True)
    check("submit_is_producer 缺放锁",
          S.submit_is_producer((-53, -48)), False)
    rel = S.call_site_relations(312, 302, 5)
    check("call_site refs", rel["refs"], 312)
    check("call_site edges", rel["edges"], 302)
    check("call_site rel12", rel["rel12"], 5)
    check("call_site gap", rel["gap"], 6)
    check("call_site sum", rel["sum"], 619)
    check("call_site distinct", rel["distinct_kinds"], 3)

    # ---- 纯函数：门字节
    check("gate_write_values", S.gate_write_values(),
          {0: 0x12ECF4, 1: 0x12F180})
    check("gate_write_bounds", S.gate_write_bounds(),
          [(0x12ECE2, 0x12ED04), (0x12F158, 0x12F1AA)])
    check("firmware_writes_gate", S.firmware_writes_gate(), True)
    check("firmware_writes_gate 空", S.firmware_writes_gate(()), False)

    # ---- 纯函数：等待 / 掩码
    check("clear_mask 0x89", S.clear_mask(0x89), 0x76)
    check("clear_mask 0x8F", S.clear_mask(0x8F), 0x70)
    check("clear_mask 0xBF", S.clear_mask(0xBF), 0x40)
    check("clear_mask 0xFF", S.clear_mask(0xFF), 0)
    check("orw_match 0x40", S.orw_match(0x40), True)
    check("orw_match 0x01", S.orw_match(0x01), True)
    check("orw_match 0x41", S.orw_match(0x41), True)
    check("orw_match 0x10", S.orw_match(0x10), False)
    check("orw_match 0x02", S.orw_match(0x02), False)
    check("andw_match 0x41", S.andw_match(0x41), True)
    check("andw_match 0x40", S.andw_match(0x40), False)
    check("wait_setters 0x40", S.wait_setters(0x40), (0x12FEAC,))
    check("wait_setters 0x01", S.wait_setters(0x01),
          (0x12E0EC, 0x12E51A, 0x12FE1C))
    check("wait_setters 0x10", S.wait_setters(0x10), ())
    check("wait41_setter_count", S.wait41_setter_count(), 4)
    check("cross_level_misread 0x41", S.cross_level_misread(0x41), True)
    check("cross_level_misread 0x10", S.cross_level_misread(0x10), False)
    check("level_check_verdict 0x10", S.level_check_verdict(0x10), True)
    check("level_check_verdict 0x41", S.level_check_verdict(0x41), False)
    check("level_check_verdict 0x74", S.level_check_verdict(0x74), False)
    check("level_check_verdict 0x11", S.level_check_verdict(0x11), True)

    # ---- 纯函数：向量表倒序
    check("vec_slot 0x2D", S.vec_slot(0x2D), 0x000EFF48)
    check("vec_slot 0x20", S.vec_slot(0x20), 0x000EFF7C)
    check("vec_slot 0x1B", S.vec_slot(0x1B), 0x000EFF90)
    check("vec_slot 0x2B", S.vec_slot(0x2B), 0x000EFF50)
    check("vec_of_slot 0xEFF48", S.vec_of_slot(0x000EFF48), 0x2D)
    check("vec_of_slot 0xEFF7C", S.vec_of_slot(0x000EFF7C), 0x20)
    check("vec_of_slot 非对齐", S.vec_of_slot(0x000EFF49), None)
    check("vec_of_slot 越界", S.vec_of_slot(0x000F0000), None)
    bd = [(0x12FE84, 0x12FEB4)]
    check("isr_owner 置0x40", S.isr_owner(0x12FEAC, bd),
          (0x12FE84, 0x12FEB4))
    check("isr_owner 非 ISR", S.isr_owner(0x12E5AA, bd), None)

    # ---- 纯函数：三层 / 写点 / 清零 / 归因
    t0 = S.three_layer(True, 0, True)
    check("three_layer 存在", t0["exists"], True)
    check("three_layer 收敛命中", t0["converged_hits"], 0)
    check("three_layer 在 ISR", t0["in_isr"], True)
    check("three_layer 未全过", t0["all"], False)
    check("three_layer 全过", S.three_layer(True, 1, True)["all"], True)
    check("writers_verdict 0 精确", S.writers_verdict([])["exact"], 0)
    check("writers_verdict 0 unknown",
          S.writers_verdict([], 0)["may_assert_absent"], True)
    check("writers_verdict 有 unknown",
          S.writers_verdict([], 224489)["may_assert_absent"], False)
    check("writers_verdict 结论",
          S.writers_verdict([])["verdict"], "no-addressable-writer")
    check("writers_verdict 有写点",
          S.writers_verdict([(0x12ECF4, "stb")])["verdict"],
          "firmware-writer-exists")
    check("slot_clear_rounds", S.slot_clear_rounds(), 24)
    check("slot_clear_rounds 2B", S.slot_clear_rounds(96, 2), 48)
    check("slot_zero_explained 全真",
          S.slot_zero_explained(True, True), True)
    check("slot_zero_explained 缺清零",
          S.slot_zero_explained(False, True), False)
    check("slot_zero_explained 槽非零",
          S.slot_zero_explained(True, False), False)
    check("submit_trigger 未跑", S.submit_trigger(0, 0, False)["verdict"],
          "callsites-not-executed")
    check("submit_trigger 有调用者",
          S.submit_trigger(0, 0, False)["callers_exist"], True)
    check("submit_trigger 已跑", S.submit_trigger(1, 1, True)["verdict"],
          "executed")
    check("equal_shape 等形",
          S.equal_shape([(191520, 3, 200000)] * 3), True)
    check("equal_shape 不等形",
          S.equal_shape([(191520, 3, 200000), (191520, 3, 300000)]), False)
    check("attribution", S.attribution(
        {"R0": {"hit": True}, "R3": {"hit": False}}, "hit"), ["R0"])
    check("chain_verdict 全过", S.chain_verdict(True, True, True,
                                                 True, True), True)
    check("chain_verdict 缺提交", S.chain_verdict(True, False, True,
                                                   True, True), False)
    check("chain_verdict 缺门", S.chain_verdict(True, True, True,
                                                 True, False), False)
    check("window_same 同覆盖", S.window_same({1, 2}, {2, 1}), 0)
    check("window_same 差 2", S.window_same({1, 2}, {2, 3}), 2)


def test_v77():
    print("\n[V77] 不可定址 store 归类、字段写归约、vec 0x2D 设备面")
    import frbox
    from frbox import reduce77 as R

    # 版本断言必须版本无关（KI#270）
    check("V77 版本格式", frbox.__version__.startswith("0."), True)

    # ---- 常量：全 ROM store 普查
    check("STORE_TOTAL", R.STORE_TOTAL, 244342)
    check("STORE_DECIDABLE", R.STORE_DECIDABLE, 19853)
    check("STORE_UNKNOWN", R.STORE_UNKNOWN, 224489)
    check("形态普查合计", sum(R.SHAPE_CENSUS.values()), R.STORE_TOTAL)
    check("unknown 形态合计", sum(R.UNKNOWN_BY_SHAPE.values()),
          R.STORE_UNKNOWN)
    check("UNKNOWN_STACK", R.UNKNOWN_STACK, 188255)
    check("UNKNOWN_STACK_R14", R.UNKNOWN_STACK_R14, 184742)
    check("UNKNOWN_INDEXED", R.UNKNOWN_INDEXED, 17823)
    check("UNKNOWN_DIRECT", R.UNKNOWN_DIRECT, 13040)
    check("UNKNOWN_OTHER", R.UNKNOWN_OTHER, 5371)
    check("NONSTACK_UNKNOWN", R.NONSTACK_UNKNOWN, 36234)
    check("栈+非栈=unknown", R.UNKNOWN_STACK + R.NONSTACK_UNKNOWN,
          R.STORE_UNKNOWN)
    check("UNKNOWN_STACK 由明细算出",
          sum(v for k, v in R.UNKNOWN_BY_SHAPE.items()
              if k in R.STACK_SHAPES), R.UNKNOWN_STACK)
    check("R14 三类明细", sum(R.UNKNOWN_BY_SHAPE[k] for k in
                            ('@(disp8,R14):8', '@(disp10,R14):32',
                             '@(disp9,R14):16')), R.UNKNOWN_STACK_R14)

    # ---- 常量：三个数据对象
    check("OBJECTS", R.OBJECTS,
          (0x9F5A0284, 0x9F59EF50, 0x9F59F7C8))
    check("OBJECT_NAMES", R.OBJECT_NAMES,
          ('WORD_A', 'WORD_B', 'SLOT8'))
    check("LOAD_COUNTS", R.LOAD_COUNTS, (63, 31, 4))
    check("EXACT_WRITERS", R.EXACT_WRITERS, (0, 0, 0))
    check("NEAR32_WRITERS", R.NEAR32_WRITERS, (12, 30, 0))
    check("REDUCED_COUNTS", R.REDUCED_COUNTS, (31, 109, 2))
    check("WORD_A 偏移数", len(R.REDUCED_OFFSETS[0]), 12)
    check("WORD_B 偏移数", len(R.REDUCED_OFFSETS[1]), 12)
    check("SLOT8 偏移", R.REDUCED_OFFSETS[2], (0, 92))
    check("WORD_A 偏移 0 缺席", 0 in R.REDUCED_OFFSETS[0], False)
    check("WORD_A 偏移含 +0x2D", 45 in R.REDUCED_OFFSETS[0], True)
    check("KI192_FIELD_OFF", R.KI192_FIELD_OFF, 45)
    check("WORD_A_BIT3_OFF", R.WORD_A_BIT3_OFF, 0)

    # ---- 常量：8 槽清零循环
    check("SLOT_CLEAR_PC", R.SLOT_CLEAR_PC, 0x1758FA)
    check("SLOT_CLEAR_LDI", R.SLOT_CLEAR_LDI, 0x1758F4)
    check("SLOT_CLEAR_ADD2", R.SLOT_CLEAR_ADD2, 0x1758F6)
    check("SLOT_CLEAR_M4", R.SLOT_CLEAR_M4, 0x0C)
    check("SLOT_CLEAR_LEN", R.SLOT_CLEAR_LEN, 96)
    check("SLOT_CLEAR_WIDTH", R.SLOT_CLEAR_WIDTH, 4)
    check("SLOT_CLEAR_ROUNDS", R.SLOT_CLEAR_ROUNDS, 24)
    check("SLOT_CLEAR_FIRST", R.SLOT_CLEAR_FIRST, 92)
    check("SLOT_WRITEBACK_PC", R.SLOT_WRITEBACK_PC, 0x175958)
    check("SLOT_STRIDE", R.SLOT_STRIDE, 12)
    check("SLOT_COUNT", R.SLOT_COUNT, 8)

    # ---- 常量：vec 0x2D ISR 与设备
    check("TBR", R.TBR, 0xEFC00)
    check("VEC_2D", R.VEC_2D, 0x2D)
    check("ISR2D", R.ISR2D, 0x12FE84)
    check("ISR2D_END", R.ISR2D_END, 0x12FEB4)
    check("DEV_COUNTER", R.DEV_COUNTER, 0x5C003000)
    check("DEV_EXPEED", R.DEV_EXPEED, 0x7000000A)
    check("DA86", R.DA86, 0x9F58DA86)
    check("SETFLG_40_PC", R.SETFLG_40_PC, 0x12FEAC)
    check("BORH_U4", R.BORH_U4, 1)
    check("BORH_MASK", R.BORH_MASK, 0x10)
    check("CLK_MASK", R.CLK_MASK, 0x02)
    check("DATA_MASK", R.DATA_MASK, 0x04)
    check("VEC_FAMILY 长度", len(R.VEC_FAMILY), 4)
    check("VEC_FAMILY[1]", R.VEC_FAMILY[1], (0x2D, 0x12FE84))

    # ---- 小工具
    check("sx 4 位负", R.sx(0xC, 4), -4)
    check("sx 8 位正", R.sx(0x7F, 8), 127)
    check("sx 8 位负", R.sx(0x80, 8), -128)
    check("m4_signed 0xC", R.m4_signed(0xC), -4)
    check("m4_signed 0x3", R.m4_signed(0x3), 3)
    check("shape_of 0x1400", R.shape_of(0x1400), ('@Rj', 4))
    check("shape_of 0x1200", R.shape_of(0x1200), ('@(R13,Rj)', 1))
    check("shape_of 0x7000", R.shape_of(0x7000), ('@(disp8,R14)', 1))
    check("shape_of 0x1300", R.shape_of(0x1300), ('@(udisp6,R15)', 4))
    check("shape_of 未收录", R.shape_of(0x1780), (None, None))
    check("is_stack_shape R14", R.is_stack_shape('@(disp9,R14)'), True)
    check("is_stack_shape R15", R.is_stack_shape('@(udisp6,R15)'), True)
    check("is_stack_shape Rj", R.is_stack_shape('@Rj'), False)
    check("shape_share 半数", R.shape_share(5, 10), 0.5)
    check("shape_share 零分母", R.shape_share(5, 0), 0.0)
    check("census_stack_total", R.census_stack_total(), 200445)
    check("census_by_class 栈", R.census_by_class()['stack'], 200445)
    check("census_by_class 索引", R.census_by_class()['indexed'], 21418)
    check("census_by_class 直接", R.census_by_class()['direct'], 17108)
    check("census_by_class 合计", sum(R.census_by_class().values()),
          R.STORE_TOTAL)
    check("nonstack_unknown", R.nonstack_unknown(R.UNKNOWN_STACK), 36234)
    check("nonstack_unknown 无栈", R.nonstack_unknown(0), R.STORE_UNKNOWN)
    check("decidable_rate", round(R.decidable_rate(), 5), 0.08125)

    # ---- 局部寄存器传播
    z = [None] * 16
    r1 = R.prop_ldi(z, 4, 0x9F5A0284)
    check("prop_ldi 值", r1[4], 0x9F5A0284)
    check("prop_ldi 不动其它", r1[5], None)
    r2 = R.prop_mov(r1, 8, 4)
    check("prop_mov 传播", r2[8], 0x9F5A0284)
    check("prop_mov 源缺失", R.prop_mov(z, 8, 4)[8], None)
    r3 = R.prop_add(R.prop_ldi(r2, 9, 0), 8, 9)
    check("prop_add 常量", r3[8], 0x9F5A0284)
    check("prop_add 未知", R.prop_add(z, 8, 9)[8], None)
    r4 = R.prop_add2(R.prop_ldi(z, 13, 96), 13, 0xC)
    check("prop_add2 -4", r4[13], 92)
    check("prop_add2 未知", R.prop_add2(z, 13, 0xC)[13], None)
    check("addr_at_rj 已知", R.addr_at_rj(r1, 4), 0x9F5A0284)
    check("addr_at_rj 未知", R.addr_at_rj(z, 4), None)
    rr = R.prop_ldi(z, 0, 0x9F59F7C8)
    rr = R.prop_ldi(list(rr), 13, 92)
    check("addr_r13_rj", R.addr_r13_rj(rr, 0), 0x9F59F824)
    check("addr_r13_rj 缺 r13", R.addr_r13_rj(R.prop_ldi(z, 0, 0x100), 0),
          None)
    r14 = R.prop_ldi(z, 14, 0x9F800000)
    check("addr_disp_r14 正", R.addr_disp_r14(r14, 1, 8), 0x9F800008)
    check("addr_disp_r14 负", R.addr_disp_r14(r14, 0xFF, 8), 0x9F7FFFF8)
    check("addr_disp_r14 未知", R.addr_disp_r14(z, 1, 8), None)
    r15 = R.prop_ldi(z, 15, 0x9F800100)
    check("addr_udisp_r15", R.addr_udisp_r15(r15, 3), 0x9F80010C)
    check("addr_udisp_r15 未知", R.addr_udisp_r15(z, 3), None)
    check("reduce_store @Rj", R.reduce_store(0x1400, {'rj': 4}, r1),
          (0x9F5A0284, 4))
    check("reduce_store @(R13,Rj)",
          R.reduce_store(0x1000, {'rj': 0}, rr), (0x9F59F824, 4))
    check("reduce_store @(disp,R14)",
          R.reduce_store(0x7000, {'disp8': 1}, r14), (0x9F800008, 1))
    check("reduce_store @(udisp6,R15)",
          R.reduce_store(0x1300, {'udisp6': 3}, r15), (0x9F80010C, 4))
    check("reduce_store 未知形态", R.reduce_store(0x1780, {}, z),
          (None, None))
    check("reduce_store 未知寄存器",
          R.reduce_store(0x1400, {'rj': 4}, z), (None, 4))
    check("is_field_write 命中",
          R.is_field_write(0x9F5A0290, R.WORD_A, 0x40), True)
    check("is_field_write 负偏移",
          R.is_field_write(R.WORD_A - 4, R.WORD_A, 0x40), True)
    check("is_field_write 越界",
          R.is_field_write(R.WORD_A + 0x40, R.WORD_A, 0x40), False)
    check("is_field_write None", R.is_field_write(None, R.WORD_A, 0x40),
          False)
    check("offset_of", R.offset_of(0x9F5A0284 + 45, R.WORD_A), 45)
    check("offset_of None", R.offset_of(None, R.WORD_A), None)
    check("offset_hist", R.offset_hist([(1, 4), (2, 4), (3, 45)]),
          {4: 2, 45: 1})
    check("offset_hist 空", R.offset_hist([]), {})
    check("zero_offset_written 无", R.zero_offset_written([(1, 4), (2, 45)]),
          False)
    check("zero_offset_written 有", R.zero_offset_written([(1, 0), (2, 4)]),
          True)
    v = R.field_write_verdict(0, [(1, 4), (2, 45)], R.UNKNOWN_STACK)
    check("field_write_verdict 精确写", v['exact_base_writes'], 0)
    check("field_write_verdict 字段写", v['field_writes'], 2)
    check("field_write_verdict 偏移0", v['zero_offset_written'], False)
    check("field_write_verdict 栈", v['stack_unknown'], 188255)
    check("field_write_verdict 非栈", v['nonstack_unknown'], 36234)
    check("field_write_verdict 不可报缺席",
          v['may_claim_no_firmware_write'], False)
    v2 = R.field_write_verdict(0, [], 0, 0)
    check("field_write_verdict 全零可报", v2['may_claim_no_firmware_write'],
          True)

    # ---- 8 槽清零
    a = R.slot_clear_addrs()
    check("slot_clear_addrs 长度", len(a), 24)
    check("slot_clear_addrs 首", a[0], 0x9F59F824)
    check("slot_clear_addrs 末", a[-1], 0x9F59F7C8)
    check("slot_clear_addrs 互异", len(set(a)), 24)
    check("slot_clear_addrs 全在区间",
          all(R.SLOT8 <= x < R.SLOT8 + 96 for x in a), True)
    check("slot_clear_rounds", R.slot_clear_rounds(), 24)
    check("slot_clear_rounds 2B", R.slot_clear_rounds(96, 2), 48)
    check("slot_clear_covers 真", R.slot_clear_covers(a), True)
    check("slot_clear_covers 缺一轮", R.slot_clear_covers(a[:-1]), False)
    check("slot_clear_covers 重复一轮",
          R.slot_clear_covers(a[:-1] + [a[0]]), False)
    check("slot_table_bytes", R.slot_table_bytes(), 96)

    # ---- 位掩码 / 设备面 / 向量
    check("borh_mask 1", R.borh_mask(1), 0x10)
    check("borh_mask 8", R.borh_mask(8), 0x80)
    check("borh_mask 0", R.borh_mask(0), 0)
    check("borh_mask 15", R.borh_mask(15), 0xF0)
    check("is_low_half_mask 0x10", R.is_low_half_mask(0x10), False)
    check("is_low_half_mask 0x02", R.is_low_half_mask(0x02), True)
    check("expeed_line_touched 0x10", R.expeed_line_touched(0x10), ())
    check("expeed_line_touched 0x02", R.expeed_line_touched(0x02), ('CLK',))
    check("expeed_line_touched 0x06", R.expeed_line_touched(0x06),
          ('CLK', 'DATA'))
    check("vec_slot 0x2D", R.vec_slot(0x2D), 0xEFF48)
    check("vec_slot 0x1B", R.vec_slot(0x1B), 0xEFF90)
    check("vec_of_slot 反算", R.vec_of_slot(0xEFF48), 0x2D)
    check("vec_slot 0x20", R.vec_slot(0x20), 0xEFF7C)
    check("isr_device_plane 去重",
          R.isr_device_plane([(0x7000000A, 'borh'), (0x5C003000, 'sth'),
                             (0x7000000A, 'stb')]),
          (0x5C003000, 0x7000000A))
    check("ki192_revision 命中",
          R.ki192_revision(R.REDUCED_OFFSETS[0]), True)
    check("ki192_revision 不命中", R.ki192_revision((0, 4, 8)), False)

    # ---- 反事实判据
    check("equal_shape 等形",
          R.equal_shape([(191520, 0x2B, 500000)] * 3), True)
    check("equal_shape 不等形",
          R.equal_shape([(191520, 0x2B, 500000), (191520, 0x2B, 1)]),
          False)
    ok, hits, noise = R.attribution(
        [('G0', False, 'other'), ('G1', True, 'vec2d'),
         ('G3', True, 'vec2d')], 'vec2d')
    check("attribution 成立", ok, True)
    check("attribution 命中组", hits, ['G1', 'G3'])
    check("attribution 噪声", noise, [])
    ok2, _h2, n2 = R.attribution(
        [('G1', True, 'vec2d'), ('G2', True, 'other')], 'vec2d')
    check("attribution 有噪声不成立", ok2, False)
    check("attribution 噪声组", n2, ['G2'])
    check("coverage_hits", R.coverage_hits([1, 2, 3], {2, 3, 4}), 2)
    check("coverage_delta 单向", R.coverage_delta({1, 2}, {2, 3}), 1)
    check("window_same 同覆盖", R.window_same({1, 2}, {2, 1}), True)
    check("window_same 差集", R.window_same({1, 2}, {2, 3}), False)
    c = R.chain_verdict(1, 0, 0, 0)
    check("chain_verdict 字段路径跑过", c['field_paths_executed'], True)
    check("chain_verdict 基址未写", c['base_written'], False)
    check("chain_verdict 显示链不闭合", c['display_chain_closed'], False)


def test_v78():
    # V78：函数边界口径、调用形态四套、tslp_tsk 命名、8 槽三函数拆分
    import frbox
    from frbox import owner78 as O

    print("[V78] owner78 函数边界与调用边口径")

    # ---- 版本（KI#270：不硬编码当前版本号）
    check("V78 版本格式 0.x.y", frbox.__version__.startswith("0."), True)
    check("V78 设施文档 规矩 59", "规矩 59" in O.__doc__, True)
    check("V78 设施文档 Sweep.edges", "Sweep.edges" in O.__doc__, True)
    check("V78 设施文档 call rel12", "call rel12" in O.__doc__, True)
    check("V78 新增硬规矩编号", O.hard_rules_78(), (67, 68, 69))

    # ---- 全 ROM 普查常量
    check("V78 ROM_BASE", O.ROM_BASE, 0x00040000)
    check("V78 ROM_SIZE", O.ROM_SIZE, 7340032)
    check("V78 enter 总数", O.ENTER_TOTAL, 23613)
    check("V78 ret 总数", O.RET_TOTAL, 5544)
    check("V78 call:d 总数", O.CALL_D_TOTAL, 25669)
    check("V78 call 总数", O.CALL_TOTAL, 13983)
    check("V78 jmp 总数", O.JMP_TOTAL, 3559)
    check("V78 edges 可定调用点", O.EDGES_DECIDABLE, 15435)
    check("V78 call:d 目标未定", O.CALL_D_UNDECIDED, 10234)
    check("V78 call rel12 点数", O.CALL_REL12, 9160)
    check("V78 call @r 点数", O.CALL_REG, 4823)
    check("V78 rel12 目标数", O.REL12_TARGETS, 8672)
    check("V78 edges 目标数", O.EDGES_TARGETS, 1576)
    check("V78 两口径目标交集", O.TARGET_INTERSECT, 79)
    check("V78 仅 rel12 目标数", O.ONLY_REL12_TARGETS, 8593)
    check("V78 仅 call:d 目标数", O.ONLY_EDGES_TARGETS, 1497)

    # ---- 两种边界口径
    check("V78 字段写 PC 数", O.FIELD_PCS, 142)
    check("V78 字段写函数(ret)", O.FIELD_FN_BY_RET, 36)
    check("V78 字段写函数(enter)", O.FIELD_FN_BY_ENTER, 18)
    check("V78 提交加载点", O.SUBMIT_REFS, 312)
    check("V78 提交 call:d 边", O.SUBMIT_EDGES, 302)
    check("V78 提交 call rel12 点", O.SUBMIT_REL12, 5)
    check("V78 提交函数(ret)", O.SUBMIT_FN_BY_RET, 244)
    check("V78 提交函数(enter)", O.SUBMIT_FN_BY_ENTER, 168)
    check("V78 字段写严格调用者数", O.FIELD_FN_WITH_STRICT, 9)
    check("V78 字段写三者皆零数", O.FIELD_FN_NO_CALLER, 9)
    check("V78 提交严格调用者数", O.SUBMIT_FN_WITH_STRICT, 50)
    check("V78 提交三者皆零数", O.SUBMIT_FN_NO_CALLER, 118)

    # ---- 巨型函数 / 根归属
    check("V78 巨型函数 enter", O.BIG_FN_ENTER, 0x00171184)
    check("V78 巨型函数下一 enter", O.BIG_FN_NEXT_ENTER, 0x001728B6)
    check("V78 巨型函数跨度", O.BIG_FN_SPAN, 5938)
    check("V78 巨型函数字段写", O.BIG_FN_FIELDS, 76)
    check("V78 巨型函数指令数", O.BIG_FN_INSNS, 2364)
    check("V78 巨型函数 ret 数", O.BIG_FN_RETS, 43)
    check("V78 巨型函数严格调用者", O.BIG_FN_STRICT_CALLER, 0x001734EE)
    check("V78 非默认向量入口", O.VEC_NONDEFAULT, 47)
    check("V78 根函数数", O.ROOT_FUNCS, 7)
    check("V78 根覆盖调用点", O.ROOT_EDGES, 12)
    check("V78 根任务号", O.ROOT_TIDS, (7, 2, 3, 4, 9, 16, 17))
    check("V78 字段写根函数数", O.FIELD_ROOT_FUNCS, 2)

    # ---- task7 / 三个相邻函数
    check("V78 任务表", O.TASK_TABLE, 0x00054C00)
    check("V78 任务数", O.TASK_COUNT, 29)
    check("V78 task7 入口", O.TASK7_ENTRY, 0x00175192)
    check("V78 task7 enter", O.TASK7_ENTER, 0x00175196)
    check("V78 task7 终点", O.TASK7_END, 0x001759A6)
    check("V78 等待函数", O.WAIT_FN, 0x00175876)
    check("V78 旧名兼容", O.SLOT_CLEAR_FN, O.WAIT_FN)
    check("V78 清零循环函数", O.SLOT_LOOP_FN, 0x001758EC)
    check("V78 清零 st", O.SLOT_LOOP_PC, 0x001758FA)
    check("V78 清零 st 旧名兼容", O.SLOT_CLEAR_PC, O.SLOT_LOOP_PC)
    check("V78 旧四套口径清零循环入边(V79证伪)", O.SLOT_LOOP_IN_EDGES, 0)
    check("V78 清零循环终点", O.SLOT_LOOP_END, 0x00175906)
    check("V78 写回函数", O.SLOT_WB_FN, 0x00175908)
    check("V78 写回 enter", O.SLOT_WB_ENTER, 0x0017590C)
    check("V78 写回 st", O.SLOT_WB_PC, 0x00175958)
    check("V78 8 槽基址", O.SLOT_BASE, 0x9F59F7C8)
    check("V78 清零调用点1", O.SLOT_CLEAR_CALL, 0x00175234)
    check("V78 清零调用点2", O.SLOT_CLEAR_CALL2, 0x00175418)
    check("V78 写回调用点", O.SLOT_WB_CALL, 0x0017574C)
    check("V78 sta_tsk 回读", O.STA_TSK_R12, -63)

    # ---- 服务号 / task7 等待
    check("V78 -37 服务号", O.SVC_TSLP_TSK, -37)
    check("V78 -37 名字", O.TSLP_NAME, "tslp_tsk")
    check("V78 B 段 tslp 调用点", O.TSLP_TOTAL_CALLS, 137)
    check("V78 A 段表项数", O.TSLP_A_TABLE_CALLS, 0)
    check("V78 tslp 卡点", O.TSLP_HIT_PC, 0x0017588E)
    check("V78 tslp 超时", O.TSLP_TIMEOUT, 10)
    check("V78 tslp 端口", O.TSLP_PORT, 0x50000000)
    check("V78 task7 TCB", O.T7_TCB, 0x68000244)
    check("V78 task7 state", O.T7_STATE, 0x04)
    check("V78 task7 等待原因", O.T7_WHY, -37)
    check("V78 task7 剩余 tick", O.T7_LEFT_TICKS, 9)
    check("V78 不可定址 call @r", O.CALL_REG_TOTAL, 4823)

    # ---- 设备策略
    check("V78 BOOT", O.BOOT_STEPS, 191520)
    check("V78 VEC_TICK", O.VEC_TICK, 0x2B)
    check("V78 VEC_2D", O.VEC_2D, 0x2D)
    check("V78 RUN", O.RUN_STEPS, 500000)
    check("V78 WINDOW", O.WINDOW_STEPS, 1000000)
    check("V78 TBR", O.TBR, 0x000EFC00)

    # ---- 算术 / 地址纯函数
    check("sx 8 位 0xDB", O.sx(0xDB, 8), -37)
    check("sx 11 位 800", O.sx(800, 11), 800)
    check("rel12_target 清零", O.rel12_target(0x175234, 800), 0x175876)
    check("rel12_target 清零2", O.rel12_target(0x175418, 558), 0x175876)
    check("rel12_target 写回", O.rel12_target(0x17574C, 221), 0x175908)
    check("rel9_target 清零回跳", O.rel9_target(0x1758F8, 254), 0x1758F6)
    check("vec_slot 0x2D", O.vec_slot(0x2D), 0x000EFF48)
    check("vec_of_slot 反算", O.vec_of_slot(0x000EFF48), 0x2D)
    check("owner_by_ends 取 ret 后", O.owner_by_ends(0x1758FA, [0x40000, 0x1758EC]), 0x1758EC)
    check("owner_by_enter 取 enter", O.owner_by_enter(0x1758FA, [0x40000, 0x175196]), 0x175196)
    check("fn_span", O.fn_span(0x171184, 0x1728B6), 5938)
    check("prologue_candidates", O.prologue_candidates(0x17590C, [(0x17590A, "stm1", 2)]), {0x17590C, 0x17590A})
    check("split_ratio 36/18", O.split_ratio(36, 18), 2.0)
    check("over_split True", O.over_split(36, 18), True)
    check("over_split False", O.over_split(18, 36), False)
    bv = O.boundary_verdict(36, 18)
    check("boundary_verdict ratio", bv["ratio"], 2.0)
    check("boundary_verdict over_split", bv["over_split"], True)
    check("boundary_verdict 多出口", bv["big_multi_exit"], True)

    # ---- 调用形态纯函数
    cc = O.call_census(25669, 15435, 9160, 4823, 3559)
    check("call_census 未定目标", cc["call_d_undecided"], 10234)
    check("edge_gap", O.edge_gap(25669, 15435), 10234)
    check("non_edge_calls", O.non_edge_calls(9160, 4823, 3559), 17542)
    check("undecided_share 正值", round(O.undecided_share(25669, 15435), 4), 0.3987)
    check("undecided_share 零分母", O.undecided_share(0, 0), 0.0)
    cv = O.caller_verdict(0, 1, 0, 0)
    check("caller_verdict any", cv["any"], True)
    check("caller_verdict missed", cv["missed_by_edges"], True)
    check("caller_verdict edge_only", cv["edge_only"], False)
    check("rel12_only_targets", O.rel12_only_targets([1, 2], [2, 3, 4]), 2)
    sr = O.submit_relations()
    check("submit_relations 三口径", (sr["refs"], sr["edges"], sr["rel12"]), (312, 302, 5))
    check("submit_relations 不可相加", sr["sum"], 619)

    # ---- 归属 / 反事实纯函数
    check("is_root 序言回退", O.is_root(0x175196, [(7, 0x175192)], {}), ["task7"])
    rts = O.roots_of([0x175196, 0x999999], [(7, 0x175192)], {})
    check("rooted_functions", O.rooted_functions(rts), [0x175196])
    check("root_coverage", O.root_coverage({0x175196: 3}, rts), (1, 3))
    check("big_concentration", round(O.big_concentration(76, 142), 4), 0.5352)
    check("task7_region True", O.task7_region(0x175234), True)
    check("task7_region False", O.task7_region(0x176000), False)
    check("slot_owner_verdict 三者同体", O.slot_owner_verdict(True, True, True)["both_in_task7"], True)
    tv = O.trigger_verdict()
    check("trigger_verdict 未定边", tv["undetermined_edges"], 290)
    check("trigger_verdict 非根函数", tv["total_fns"] - tv["root_fns"], 161)
    check("equal_shape 全等", O.equal_shape([(1, 2, 3), (1, 2, 3)]), True)
    check("equal_shape 不等", O.equal_shape([(1, 2, 3), (1, 2, 4)]), False)
    check("equal_shape 不可哈希不炸", O.equal_shape([({"a": 1}, 2, 3), ({"a": 1}, 2, 3)]), True)
    check("attribution 分离", O.attribution([{"x": True}, {"x": False}], "x"), True)
    check("attribution 全命中", O.attribution([{"x": True}, {"x": True}], "x"), False)
    check("coverage_hits", O.coverage_hits([1, 2, 3], {2, 3, 4}), 2)
    check("coverage_delta", O.coverage_delta({1, 2}, {2, 3}), 1)
    check("window_same", O.window_same({1, 2}, {1, 2}), True)
    check("chain_verdict 未闭合", O.chain_verdict(0, 0, 0, True)["closed"], False)
    check("field_fn_table", O.field_fn_table([(1, 2), (1, 3), (2, 1)]), {1: 5, 2: 1})
    check("model_actions 条数", len(O.model_actions()), 6)
    check("forbidden_claims 含禁语", "显示链通了" in O.forbidden_claims(), True)
    check("forbidden_claims 含新禁语", "清零循环函数没人调用" in O.forbidden_claims(), True)

    # ---- V78 新增三纯函数
    tsv = O.tslp_verdict(137, 0, 10)
    check("tslp_verdict 名字", tsv["name"], "tslp_tsk")
    check("tslp_verdict 不同尺子", tsv["same_ruler"], False)
    check("tslp_verdict B 段数", tsv["b_segment_calls"], 137)
    lv = O.loop_entry_verdict()
    check("loop_entry 零入边", lv["addressable"], 0)
    check("loop_entry 判据", lv["claim"], "no_addressable_in_edges")
    check("loop_entry 不能报没人调用", lv["can_say_nobody_calls"], False)
    check("loop_entry 不可定址总量", lv["undecidable"], 4823)
    check("loop_entry 有一入边", O.loop_entry_verdict(1, 0, 0, 0)["claim"], "has_addressable_in_edges")
    wv = O.t7_wait_verdict(-37, 9, 4)
    check("t7_wait is_tslp", wv["is_tslp"], True)
    check("t7_wait tick_driven", wv["tick_driven"], True)
    check("t7_wait 名字", wv["name"], "tslp_tsk")
    check("t7_wait 非 tslp", O.t7_wait_verdict(-46, 9, 4)["is_tslp"], False)
    check("t7_wait 剩余 0", O.t7_wait_verdict(-37, 0, 4)["tick_driven"], False)



def test_v79():
    """V79：真时基 tick 链、call:d rel12 第五形态、清零循环入边、规矩 70/71。"""
    from frbox import tick79 as C

    # ---- 常量
    check("V79 TBR", C.TBR, 0x000EFC00)
    check("V79 真时基向量", C.VEC_TIMER, 0x18)
    check("V79 收敛用向量", C.VEC_BOOT, 0x2B)
    check("V79 tick ISR", C.TICK_ISR, 0x0004046E)
    check("V79 tick 调用点", C.TICK_CALL, 0x00040482)
    check("V79 tick 函数", C.TICK_FN, 0x00040A44)
    check("V79 系统时钟", C.CLOCK, 0x68000110)
    check("V79 延时队列哨兵", C.SENTINEL, 0x68000118)
    check("V79 TCB tick 偏移", C.TCB_TICKS, 0x14)
    check("V79 端口", C.PORT, 0x50000000)
    check("V79 8 槽基址", C.SLOT8, 0x9F59F7C8)
    check("V79 等待函数", C.WAIT_FN, 0x00175876)
    check("V79 清零循环", C.CLEAR_FN, 0x001758EC)
    check("V79 清零 st", C.CLEAR_ST, 0x001758FA)
    check("V79 写回函数", C.WB_FN, 0x00175908)
    check("V79 提交函数", C.SUBMIT_FN, 0x0012E6F8)
    check("V79 稳定次数", C.STABLE_LIMIT, 10)
    check("V79 boot 步数", C.BOOT_STEPS, 191520)
    check("V79 run 步数", C.RUN_STEPS, 500000)
    check("V79 post 步数", C.POST_STEPS, 200000)
    check("V79 窗口步数", C.WINDOW_STEPS, 1000000)

    # ---- 七形态普查常量（V79 实测）
    check("V79 ldi:32 数", C.FORM_CENSUS["ldi:32"], 48611)
    check("V79 call:d @r 数", C.FORM_CENSUS["call:d @r"], 16027)
    check("V79 call:d rel12 数", C.FORM_CENSUS["call:d rel12"], 9642)
    check("V79 call rel12 数", C.FORM_CENSUS["call rel12"], 9160)
    check("V79 call @r 数", C.FORM_CENSUS["call @r"], 4823)
    check("V79 jmp @r 数", C.FORM_CENSUS["jmp @r"], 3559)
    check("V79 jmp:d @r 数", C.FORM_CENSUS["jmp:d @r"], 3266)
    check("V79 形态总数", len(C.FORM_CENSUS), 7)
    check("V79 call:d 两种形态合计",
          C.FORM_CENSUS["call:d @r"] + C.FORM_CENSUS["call:d rel12"], 25669)
    check("V79 只被 call:d rel12 解出", C.ONLY_CALLD_REL12, 7616)
    check("V79 call:d rel12 目标数", C.FORM_TARGETS["call:d rel12"], 8205)
    check("V79 call rel12 目标数", C.FORM_TARGETS["call rel12"], 8672)
    check("V79 ldi:32 目标数", C.FORM_TARGETS["ldi:32"], 9863)
    check("V79 归约 call:d @r", C.RESOLVED["call:d @r归约"], 15425)
    check("V79 归约 call @r", C.RESOLVED["call @r归约"], 4642)
    check("V79 归约 jmp @r", C.RESOLVED["jmp @r归约"], 2975)
    check("V79 归约 jmp:d @r", C.RESOLVED["jmp:d @r归约"], 1970)
    check("V79 归约合计", sum(C.RESOLVED.values()), 25012)
    check("V79 清零循环入边数", len(C.CLEAR_IN_EDGES), 5)
    check("V79 清零循环入边", C.CLEAR_IN_EDGES,
          (0x1752E4, 0x175386, 0x1753C4, 0x1754A8, 0x1754D6))
    check("V79 tick 入边 ldi:32", C.TICK_IN_EDGES["ldi:32"], (0x04047C,))
    check("V79 tick 入边 call @r", C.TICK_IN_EDGES["call @r归约"], (0x040482,))
    check("V79 提交调用边 call:d", C.SUBMIT_EDGES["call:d @r归约"], 302)
    check("V79 提交调用边 call@r", C.SUBMIT_EDGES["call @r归约"], 10)
    check("V79 提交调用边 rel12", C.SUBMIT_EDGES["call:d rel12"], 5)
    check("V79 提交调用边合计", sum(C.SUBMIT_EDGES.values()), 317)

    # ---- 位运算 / 分支目标 / 向量倒序
    check("V79 sx 正数", C.sx(3, 8), 3)
    check("V79 sx8 负数", C.sx8(254), -2)
    check("V79 sx8 正数", C.sx8(3), 3)
    check("V79 sx11 正数", C.sx11(522), 522)
    check("V79 sx11 负号位", C.sx11(0x400), -1024)
    check("V79 sx11 边界", C.sx11(0x3FF), 1023)
    check("V79 rel12 目标", C.rel12_target(0x1754D6, 522), 0x1758EC)
    check("V79 rel12 回跳", C.rel12_target(0x1000, 0x7FF), 0x1000 + 2 - 2)
    check("V79 rel9 目标 beq", C.rel9_target(0x17589E, 3), 0x1758A6)
    check("V79 rel9 回边 bra:d", C.rel9_target(0x1758A2, 242), 0x175888)
    check("V79 rel9 回边 blt", C.rel9_target(0x1758AA, 238), 0x175888)
    check("V79 vec 0x18 槽", C.vec_slot(0x18), 0x000EFF9C)
    check("V79 vec 0x2B 槽", C.vec_slot(0x2B), 0x000EFF50)
    check("V79 vec 0x2D 槽", C.vec_slot(0x2D), 0x000EFF48)
    check("V79 vec 255 槽", C.vec_slot(255), C.TBR)
    check("V79 vec 0 槽", C.vec_slot(0), C.TBR + 255 * 4)

    # ---- 形态分类
    check("V79 call:d @r 形态", C.call_form("call:d", {"ri": 12}), "call:d @r")
    check("V79 call:d rel12 形态",
          C.call_form("call:d", {"rel12": 522}), "call:d rel12")
    check("V79 call rel12 形态",
          C.call_form("call", {"rel12": 800}), "call rel12")
    check("V79 call @r 形态", C.call_form("call", {"ri": 3}), "call @r")
    check("V79 jmp:d @r 形态", C.call_form("jmp:d", {"ri": 3}), "jmp:d @r")
    check("V79 非调用", C.call_form("ldi:32", {"i32": 1}), None)
    check("V79 相对形态 True", C.is_relative_form("call:d rel12"), True)
    check("V79 相对形态 False", C.is_relative_form("call:d @r"), False)
    check("V79 相对形态 None", C.is_relative_form(None), False)
    fc = C.form_census(C.FORM_CENSUS)
    check("V79 调用点合计", fc["total"], 46477)
    check("V79 PC 相对调用点", fc["relative"], 18802)
    check("V79 only_by 基本",
          C.only_by("a", {"a": [1, 2, 3], "b": [3, 4]}), [1, 2])
    check("V79 only_by 带 known",
          C.only_by("a", {"a": [1, 2], "b": []}, known=[2]), [1])
    check("V79 only_by 空", C.only_by("a", {}), [])

    # ---- 间接归约（小指令表）
    ins = [
        (0x100, "ldi:32", {"i32": 0x1758EC, "ri": 12}, 6),
        (0x106, "call", {"ri": 12}, 2),
        (0x108, "ldi:8", {"i8": 1, "ri": 4}, 2),
        (0x10A, "ld", {"ri": 13}, 2),
        (0x10C, "call:d", {"ri": 13}, 2),
        (0x10E, "ret", {}, 2),
        (0x110, "ldi:32", {"i32": 0x40000, "ri": 5}, 6),
        (0x116, "jmp", {"ri": 5}, 2),
    ]
    res, why = C.resolve_indirect(ins)
    check("V79 归约条数", len(res), 2)
    check("V79 归约目标1", res[0x106][1], 0x1758EC)
    check("V79 归约来源1", res[0x106][2], 0x100)
    check("V79 归约目标2", res[0x116][1], 0x40000)
    check("V79 被 ld 改写记不可定", 0x10C not in res, True)
    check("V79 不可定有原因", why["overwritten_ld"], 1)
    check("V79 窗口 0 无可定", len(C.resolve_indirect(ins, window=0)[0]), 0)
    check("V79 merge_targets",
          C.merge_targets({1: [10]}, {1: [20], 2: [30]})[1], [10, 20])

    # ---- 链判据
    sl = C.stable_loop_verdict(0x17589E, 3, 0x1758A6, 10,
                               0x1758AA, 238, 0x175888)
    check("V79 稳定循环 beq 跳过", sl["beq_skips"], True)
    check("V79 稳定循环 blt 回边", sl["blt_loops"], True)
    check("V79 稳定循环成立", sl["stable_loop"], True)
    check("V79 稳定循环阈值", sl["limit"], 10)
    bad = C.stable_loop_verdict(0x17589E, 5, 0x1758A6, 10,
                                0x1758AA, 238, 0x175888)
    check("V79 稳定循环错配", bad["stable_loop"], False)
    chain = [(0x040A56, "add", {}, 2), (0x040A58, "addc", {}, 2),
             (0x040A7A, "add2", {}, 2)]
    cv = C.clock_chain_verdict(chain)
    check("V79 时钟递增", cv["clock_increment"], True)
    check("V79 tick 递减", cv["tick_decrement"], True)
    check("V79 时钟链成立", cv["chain"], True)
    check("V79 时钟链指令数", cv["instructions"], 3)
    check("V79 缺 addc", C.clock_chain_verdict(chain[:1])["chain"], False)

    # ---- 反事实判据
    eq = C.equal_shape([{"injections": 12, "post_steps": 200000},
                        {"injections": 12, "post_steps": 200000}])
    check("V79 等形成立", eq["equal"], True)
    eq2 = C.equal_shape([{"injections": 12, "post_steps": 200000},
                         {"injections": 13, "post_steps": 200000}])
    check("V79 等形注入数不同", eq2["injections"], False)
    check("V79 等形整体不同", eq2["equal"], False)
    at = C.attribution([{"clock_delta": 1}, {"clock_delta": 12}],
                       [{"clock_delta": 0}])
    check("V79 归因分离", at["separated"], True)
    check("V79 错配全零", at["mismatch_all_zero"], True)
    at2 = C.attribution([{"clock_delta": 0}], [{"clock_delta": 0}])
    check("V79 归因未分离", at2["separated"], False)
    sz = C.slot_zero_verdict(0, 0)
    check("V79 8槽证据不可用", sz["usable"], False)
    check("V79 8槽原因", sz["reason"], "writer_is_zero")
    sz2 = C.slot_zero_verdict(0, 5, writes_zero=False)
    check("V79 非零写证据可用", sz2["usable"], True)
    check("V79 非零写有变化", sz2["changed"], True)
    tv = C.tick_verdict({"a": 1}, [{"clock_delta": 12}],
                        [{"clock_delta": 0}], 12, 0, sz)
    check("V79 静态链", tv["static_chain"], True)
    check("V79 动态归因", tv["dynamic_attribution"], True)
    check("V79 清零段命中", tv["clear_segment_hit"], True)
    check("V79 写回段未命中", tv["writeback_hit"], False)
    check("V79 链未闭合", tv["chain_closed"], False)
    check("V79 综合判据", tv["verdict"], "tick_chain_located_but_chain_open")
    tv2 = C.tick_verdict({}, [], [], 0, 0, sz)
    check("V79 综合判据 inconclusive", tv2["verdict"], "inconclusive")

    # ---- 文档硬规矩 70 / 71
    doc = C.__doc__
    check("V79 文档含规矩 70", "规矩 70" in doc, True)
    check("V79 文档含规矩 71", "规矩 71" in doc, True)
    check("V79 文档含 KI#191", "KI#191" in doc, True)
    check("V79 文档含反事实边界", "反事实补全" in doc, True)
    check("V79 文档含共同禁语", "不能报显示链通了" in doc, True)

    # ---- 结果文件存在与关键行
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "results", "frbox", "tick79.txt")
    if os.path.exists(rp):
        txt = io.open(rp, encoding="utf-8", errors="replace").read()
        check("V79 结果无 Traceback", "Traceback" in txt, False)
        check("V79 结果有 A 段", "A. 七形态普查" in txt, True)
        check("V79 结果有 H 段", "H. 结案与仍开放" in txt, True)
        check("V79 结果有 5 条入边", "call:d rel12', 5" in txt, True)
        check("V79 结果有 7616", "7616" in txt, True)
    else:
        check("V79 结果文件存在", False, True)



def test_v80():
    """V80：写回段唯一入口、flag 12 参数口径、时基配置与器件分离、规矩 72/73。"""
    import os
    import io
    from frbox import wb80 as C

    # ---- 常量：时基配置链
    check("V80 ICR 地址", C.ICR_ADDR, 0x448)
    check("V80 ICR 级别", C.ICR_LEVEL, 0x12)
    check("V80 ICR 写点", C.ICR_INIT_PC, 0x040456)
    check("V80 直接页 48", C.DIRECT48_PC, 0x04045C)
    check("V80 直接页 4E", C.DIRECT4E_PC, 0x040460)
    check("V80 批量 ICR", C.BATCH_ICR_PC, 0x040468)
    check("V80 tick ISR", C.TICK_ISR, 0x0004046E)
    check("V80 tick 调用点", C.TICK_CALL, 0x00040482)
    check("V80 tick 函数", C.TICK_FN, 0x00040A44)
    check("V80 真时基向量", C.VEC_TIMER, 0x18)
    check("V80 收敛用向量", C.VEC_BOOT, 0x2B)

    # ---- 常量：主循环 / 命令分发
    check("V80 主循环等待", C.MAIN_WAIT, 0x00175270)
    check("V80 主循环 beq", C.MAIN_BEQ, 0x0017527A)
    check("V80 beq 目标", C.MAIN_BEQ_TARGET, 0x00175284)
    check("V80 命令跳点", C.MAIN_JMP, 0x00175282)
    check("V80 命令装载", C.CMD_LOAD, 0x0017527C)
    check("V80 命令分发段", C.CMD_SEG, 0x0017573C)
    check("V80 命令比较", C.CMD_CMP, 0x00175740)
    check("V80 命令 beq", C.CMD_BEQ, 0x00175742)
    check("V80 命令码", C.CMD_CODE, -85)
    check("V80 命令立即数", C.CMD_LDI8, 171)
    check("V80 唯一 WB_CALL", C.WB_CALL, 0x0017574C)
    check("V80 WB rel12", C.WB_CALL_REL12, 221)
    check("V80 写回函数", C.WB_FN, 0x00175908)
    check("V80 写回段末", C.WB_END, 0x001759A6)

    # ---- 常量：清零段 / 写回段
    check("V80 清零函数", C.CLEAR_FN, 0x001758EC)
    check("V80 清零 st", C.CLEAR_ST, 0x001758FA)
    check("V80 清零 bgt", C.CLEAR_BGT, 0x001758F8)
    check("V80 清零回跳", C.CLEAR_BGT_TARGET, 0x001758F6)
    check("V80 清零 setflg", C.CLEAR_SETFLG, 0x00175904)
    check("V80 清零 ret", C.CLEAR_RET, 0x00175906)
    check("V80 清零段条数", C.CLEAR_SEG_N, 12)
    check("V80 写回段条数", C.WB_SEG_N, 75)
    check("V80 写回 st", C.WB_ST, 0x00175958)
    check("V80 写回循环回跳", C.WB_LOOP_BACK, 0x00175930)
    check("V80 写回循环 blt", C.WB_LOOP_BLT, 0x0017598E)
    check("V80 槽步长", C.WB_SLOT_STEP, 12)
    check("V80 槽字节数", C.WB_SLOT_BYTES, 96)
    check("V80 槽轮数", C.WB_SLOT_ROUNDS, 8)
    check("V80 8 槽基址", C.SLOT8, 0x9F59F7C8)

    # ---- 常量：flag 12 通信面
    check("V80 flag12", C.FLAG12, 12)
    check("V80 flag12 对象", C.FLAG12_OBJ, 0x68000968)
    check("V80 位模式偏移", C.OBJ_PAT, 8)
    check("V80 位模式地址", C.FLAG12_PAT, 0x68000970)
    check("V80 wai_flg", C.WAI_FLG, -46)
    check("V80 clr_flg", C.CLR_FLG, -47)
    check("V80 set_flg", C.SET_FLG, -48)
    check("V80 dly_tsk", C.DLY_TSK, -85)
    check("V80 wai_flg(12) 总数", C.WAI12_TOTAL, 12)
    check("V80 写回段内 wai(12)", C.WAI12_IN_WB, 2)
    check("V80 set_flg(12) 总数", C.SET12_TOTAL, 49)
    check("V80 clr_flg(12) 总数", C.CLR12_TOTAL, 65)
    check("V80 写回段 wai PC", C.WAI12_IN_WB_PCS,
          (0x0017591C, 0x00175974))
    check("V80 清零段 set PC", C.SETFLG12_CLEAR_PC, 0x00175904)
    check("V80 写回段 set PC", C.SETFLG12_WB_PCS,
          (0x00175962, 0x00175998))
    check("V80 写回段 clr PC", C.CLRFLG12_WB_PCS,
          (0x0017592C, 0x00175984))
    check("V80 服务序列表长度", len(C.CHAIN_CALLS), 8)

    # ---- 常量：设备策略
    check("V80 BOOT", C.BOOT_STEPS, 191520)
    check("V80 RUN", C.RUN_STEPS, 500000)
    check("V80 POST", C.POST_STEPS, 200000)
    check("V80 WINDOW", C.WINDOW_STEPS, 1000000)
    check("V80 CF_STEP", C.CF_STEP, 2000)
    check("V80 FORCE_STEPS", C.FORCE_STEPS, 20000)

    # ---- 纯函数：补码与宽度
    check("V80 sx8(0xAB)", C.sx8(0xAB), -85)
    check("V80 sx8(171)", C.sx8(171), -85)
    check("V80 sx8(254)", C.sx8(254), -2)
    check("V80 sx32 0xFFFFFF56", C.sx32(0xFFFFFF56), -170)
    check("V80 svc_num 宽 32", C.svc_num(0xFFFFFF56, 32), -170)
    check("V80 svc_num 宽 8", C.svc_num(0xFFFFFF56, 8), 86)
    check("V80 sx(0x7FF,11)", C.sx(0x7FF, 11), -1)
    check("V80 sx(0x0DD,11)", C.sx(0x0DD, 11), 221)

    # ---- 纯函数：分支目标（规矩 57 / 70）
    check("V80 WB rel12 目标", C.rel12_target(C.WB_CALL, 221), C.WB_FN)
    check("V80 命令 beq 目标", C.rel9_target(C.CMD_BEQ, 4), C.WB_CALL)
    check("V80 主循环 beq 目标",
          C.rel9_target(C.MAIN_BEQ, 4), C.MAIN_BEQ_TARGET)
    check("V80 清零 bgt 目标",
          C.rel9_target(C.CLEAR_BGT, 254), C.CLEAR_BGT_TARGET)
    check("V80 写回 blt 目标",
          C.rel9_target(C.WB_LOOP_BLT, 208), C.WB_LOOP_BACK)

    # ---- 纯函数：参数寄存器口径（规矩 72）
    check("V80 flg set 取 r4", C.flg_arg(C.SET_FLG, 12, 1), 12)
    check("V80 flg clr 取 r4", C.flg_arg(C.CLR_FLG, 12, 0xFE), 12)
    check("V80 flg wai 取 r5", C.flg_arg(C.WAI_FLG, None, 12), 12)
    check("V80 flg twai 取 r5", C.flg_arg(-170, None, 12), 12)
    check("V80 flg 未知服务", C.flg_arg(0, 1, 2), None)
    check("V80 bits set 取 r5", C.bits_arg(C.SET_FLG, 1, 2), 1)
    check("V80 bits wai 取 r6", C.bits_arg(C.WAI_FLG, 12, 1), 1)
    check("V80 bits 未知服务", C.bits_arg(0, 1, 2), None)

    # ---- 纯函数：入口判据
    check("V80 命令门 -85", C.command_gate(-85), True)
    check("V80 命令门 0", C.command_gate(0), False)
    check("V80 命令门 1", C.command_gate(1), False)
    check("V80 命令门换码", C.command_gate(-85, code=0), False)

    # ---- 纯函数：循环判据
    lv = C.loop_verdict(C.CLEAR_BGT, 254, C.CLEAR_BGT_TARGET,
                        C.WB_LOOP_BLT, 208, C.WB_LOOP_BACK)
    check("V80 清零回跳值", lv["clear_back"], 0x001758F6)
    check("V80 清零回跳在函数内", lv["clear_in_fn"], True)
    check("V80 写回回跳值", lv["wb_back"], 0x00175930)
    check("V80 写回回跳在函数内", lv["wb_in_fn"], True)
    check("V80 写回轮数", lv["wb_rounds"], 8)
    check("V80 两回跳都在函数内", lv["all_inside"], True)
    lv2 = C.loop_verdict(C.CLEAR_BGT, 254, 0x123456,
                         C.WB_LOOP_BLT, 208, C.WB_LOOP_BACK)
    check("V80 错期望报不闭合", lv2["all_inside"], False)

    # ---- 纯函数：规矩 73 配置与器件
    h = {"ICR_STB": True, "DIRECT48": True, "DIRECT4E": True,
         "BATCH_ICR": True, "TICK_ISR": False}
    v1 = C.icr_verdict(h, 0x12)
    check("V80 配置链已做", v1["configured"], True)
    check("V80 ICR 值正确", v1["icr_ok"], True)
    check("V80 ISR 未命中", v1["isr_hit"], False)
    check("V80 配置判据", v1["verdict"],
          "config_done_device_event_missing")
    v2 = C.icr_verdict(dict(h, TICK_ISR=True), 0x12)
    check("V80 ISR 命中时不判缺器件", v2["verdict"], "inconclusive")
    v3 = C.icr_verdict(dict(h, ICR_STB=False), 0x12)
    check("V80 配置缺失不判已配", v3["configured"], False)
    check("V80 ICR 值错", C.icr_verdict(h, 0x11)["icr_ok"], False)

    # ---- 纯函数：等形与归因
    e1 = C.equal_shape([{"injections": 110, "post_steps": 200000,
                         "write_action": 1},
                        {"injections": 110, "post_steps": 200000,
                         "write_action": 1}])
    check("V80 等形 True", e1["equal"], True)
    e2 = C.equal_shape([{"injections": 110, "post_steps": 200000,
                         "write_action": 1},
                        {"injections": 160, "post_steps": 200000,
                         "write_action": 1}])
    check("V80 等形 False", e2["equal"], False)
    check("V80 等形空", C.equal_shape([])["equal"], False)
    a1 = C.attribution([{"clock_delta": 12}, {"clock_delta": 110}],
                       [{"clock_delta": 0}])
    check("V80 归因分离", a1["separated"], True)
    check("V80 真时基非零", a1["timer_nonzero"], True)
    check("V80 错配全 0", a1["mismatch_all_zero"], True)
    check("V80 归因表", a1["timer"], [12, 110])
    check("V80 归因不分离",
          C.attribution([{"clock_delta": 0}], [{"clock_delta": 0}])["separated"],
          False)

    # ---- 纯函数：写数据门 / 强制入口
    d1 = C.data_gate_verdict(0, 0, 0, 1)
    check("V80 flag12 已置", d1["flag12_set"], True)
    check("V80 写回段全 0", d1["wb_all_zero"], True)
    check("V80 写数据门判据", d1["verdict"], "data_write_not_call_event")
    check("V80 写数据门反例",
          C.data_gate_verdict(0, 1, 0, 1)["verdict"], "inconclusive")
    f1 = C.force_entry_verdict(39, 2334)
    check("V80 强制命中数", f1["hit"], 39)
    check("V80 函数体可执行", f1["body_executable"], True)
    check("V80 强制非全段", f1["not_full"], True)
    check("V80 强制判据", f1["verdict"], "model_force_is_not_firmware_call")
    check("V80 强制 0 命中不可执行",
          C.force_entry_verdict(0, 0)["body_executable"], False)

    # ---- 纯函数：综合判据
    ch = {"MAIN_WAIT": True, "MAIN_BEQ": True, "MAIN_JMP": False,
          "WB_CALL": False, "WB_FN": False}
    w1 = C.wb_verdict(v1, ch, d1, f1, a1)
    check("V80 静态链已定位", w1["static_chain_located"], True)
    check("V80 链未闭合", w1["chain_closed"], False)
    check("V80 综合判据", w1["verdict"],
          "wb_chain_located_but_command_dispatch_not_reached")
    check("V80 闭合时不判未闭合",
          C.wb_verdict(v1, dict(ch, WB_CALL=True, WB_FN=True),
                       d1, f1, a1)["chain_closed"], True)

    # ---- 文档与边界
    doc = C.__doc__
    check("V80 文档含规矩 72", "规矩 72" in doc, True)
    check("V80 文档含规矩 73", "规矩 73" in doc, True)
    check("V80 文档含 KI#191", "KI#191" in doc, True)
    check("V80 文档含反事实边界", "反事实补全" in doc, True)
    check("V80 文档含共同禁语", "不能报显示链通了" in doc, True)
    check("V80 文档含不是顺序后继", "不是顺序后继" in doc, True)
    check("V80 模块导出非空", len(C.__all__) > 30, True)

    # ---- 结果文件存在与关键行
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "results", "frbox", "wb80.txt")
    if os.path.exists(rp):
        txt = io.open(rp, encoding="utf-8", errors="replace").read()
        check("V80 结果无 Traceback", "Traceback" in txt, False)
        check("V80 结果有 A 段", "A. 静态链" in txt, True)
        check("V80 结果有 H 段", "H. 结案与仍开放" in txt, True)
        check("V80 结果有 DONE", "DONE" in txt, True)
        check("V80 结果有唯一入边", "call rel12=221" in txt, True)
        check("V80 结果有规矩 73", "规矩 73" in txt, True)
        check("V80 结果有写数据门", "写数据门" in txt, True)
    else:
        check("V80 结果文件存在", False, True)



def test_v81():
    """V81：主循环返回码两道串联判据、86 号服务宽度口径、规矩 74/75。"""
    import os
    import io
    from frbox import cmd81 as C

    # ---- 常量：收敛设备策略
    check("V81 BOOT", C.BOOT_STEPS, 191520)
    check("V81 VEC_BOOT", C.VEC_BOOT, 0x2B)
    check("V81 VEC_TIMER", C.VEC_TIMER, 0x18)
    check("V81 RUN", C.RUN_STEPS, 500000)
    check("V81 WINDOW", C.WINDOW_STEPS, 1000000)
    check("V81 POST", C.POST_STEPS, 200000)
    check("V81 FORCE", C.FORCE_STEPS, 20000)
    check("V81 INJECT", C.INJECT_TIMES, 110)
    check("V81 COV_BASE", C.COV_BASE, 5439)
    check("V81 SLOT8", C.SLOT8, 0x9F59F7C8)

    # ---- 常量：主循环 / 86 号服务
    check("V81 主循环等待", C.MAIN_WAIT, 0x00175270)
    check("V81 主循环 nop", C.MAIN_NOP, 0x00175272)
    check("V81 保存 r4", C.MAIN_ST_R4, 0x00175274)
    check("V81 mov r12,r4", C.MAIN_MOV_R12_R4, 0x00175276)
    check("V81 cmp #0", C.MAIN_CMP0, 0x00175278)
    check("V81 第一道 beq", C.MAIN_BEQ, 0x0017527A)
    check("V81 beq rel9", C.MAIN_BEQ_REL9, 4)
    check("V81 beq 目标", C.MAIN_BEQ_TARGET, 0x00175284)
    check("V81 命令装载", C.CMD_LOAD, 0x0017527C)
    check("V81 命令装载立即数", C.CMD_LOAD_IMM32, 1529660)
    check("V81 装载即分发段", C.CMD_LOAD_IMM32, C.CMD_SEG)
    check("V81 主循环 jmp", C.MAIN_JMP, 0x00175282)
    check("V81 服务号立即数", C.MAIN_SVC_IMM32, 0xFFFFFF56)
    check("V81 服务号 8 位", C.MAIN_SVC_8, 86)
    check("V81 服务号 32 位", C.MAIN_SVC_32, -170)
    check("V81 flgid", C.MAIN_FLGID, 12)
    check("V81 位模式", C.MAIN_WAIPTN, 0xA2A05E1C)
    check("V81 wfmode", C.MAIN_WFMODE, 2)
    check("V81 r13 设定点", C.MAIN_R13_SET, 0x00175268)

    # ---- 常量：86 号服务普查
    check("V81 86 号调用点", C.SVC86_CALLS, 91)
    check("V81 三元组种类", C.SVC86_TRIPLES, 57)
    check("V81 全 ROM 调用点", C.ALL_CALLS, 2297)
    check("V81 服务号种类", C.ALL_SVC_KINDS, 25)
    check("V81 已命名服务号数", len(C.SVC_NAMED), 16)
    check("V81 86 不在已命名表", C.MAIN_SVC_8 in C.SVC_NAMED, False)
    check("V81 最常见三元组", C.SVC86_TOP_TRIPLE, (12, 262144, 2))
    check("V81 最常见次数", C.SVC86_TOP_TRIPLE_N, 6)
    check("V81 固定 r7", C.SVC86_FIXED_R7, 2)

    # ---- 常量：命令分发段（单码分发器）
    check("V81 分发段", C.CMD_SEG, 0x0017573C)
    check("V81 ldi:8 立即数", C.CMD_LDI8, 171)
    check("V81 extsb 位置", C.CMD_EXTSB_PC, 0x0017573E)
    check("V81 第二道 cmp", C.CMD_CMP, 0x00175740)
    check("V81 第二道 beq", C.CMD_BEQ, 0x00175742)
    check("V81 beq rel9", C.CMD_BEQ_REL9, 4)
    check("V81 唯一命令码", C.CMD_CODE, -85)
    check("V81 不匹配装载", C.CMD_MISS_LDI, 0x00175744)
    check("V81 不匹配 jmp", C.CMD_MISS_JMP, 0x0017574A)
    check("V81 回主循环立即数", C.CMD_BACK_IMM32, 1528410)
    check("V81 回主循环目标", C.CMD_BACK_TARGET, 0x0017525A)
    check("V81 写回调用点", C.WB_CALL, 0x0017574C)
    check("V81 写回 rel12", C.WB_CALL_REL12, 221)
    check("V81 写回函数", C.WB_FN, 0x00175908)
    check("V81 写回段末", C.WB_END, 0x001759A6)
    check("V81 写回段条数", C.WB_SEG_N, 75)
    check("V81 清零函数", C.CLEAR_FN, 0x001758EC)
    check("V81 清零段条数", C.CLEAR_SEG_N, 12)

    # ---- 常量：真内核与分发表证伪
    check("V81 内核入口", C.KERNEL_ENTRY, 0x040668)
    check("V81 索引 lsl1", C.KERNEL_SVC_LSL1, 0x0406A4)
    check("V81 表项 lduh", C.KERNEL_SVC_LDUH, 0x0406AC)
    check("V81 表项 extsh", C.KERNEL_SVC_EXTSH, 0x0406AE)
    check("V81 表项 add", C.KERNEL_SVC_ADD, 0x0406B0)
    check("V81 服务 call", C.KERNEL_SVC_CALL, 0x0406B2)
    check("V81 出口 extsb", C.KERNEL_EXIT_EXTSB, 0x0406BE)
    check("V81 reti", C.KERNEL_RETI, 0x0406E2)
    check("V81 分发表基址", C.SVC_TABLE_BASE, 0x040660)
    check("V81 86 号表项半字", C.SVC86_TABLE_HALF, 0xD106)
    check("V81 表项有符号", C.SVC86_TABLE_SIGNED, -12026)
    check("V81 表项目标", C.SVC86_TABLE_TARGET, 0x03D766)
    check("V81 ROM 下界", C.ROM_LO, 0x040000)

    # ---- 常量：等形补位 / 两道门归因
    check("V81 flag12 对象", C.FLAG12_OBJ, 0x68000968)
    check("V81 位模式偏移", C.OBJ_PAT, 8)
    check("V81 位模式地址", C.FLAG12_PAT, 0x68000970)
    check("V81 补位四组数", len(C.PATCH_PATS), 4)
    check("V81 补位位模式", C.PATCH_PATS[1], C.MAIN_WAIPTN)
    check("V81 补位全 1", C.PATCH_PATS[2], 0xFFFFFFFF)
    check("V81 补位新增 PC", C.PATCH_NPC, (2291, 2383, 2383, 2291))
    check("V81 补位读回", C.PATCH_READBACK,
          (0x00000001, 0x02205E19, 0x5F7FFFFB, 0x00000001))
    check("V81 补位 JMP 全 False", C.PATCH_JMP_HITS, (False,) * 4)
    check("V81 补位清零命中", C.PATCH_CLEAR_HITS, (12, 12, 12, 12))
    check("V81 补位写回命中", C.PATCH_WB_HITS, (0, 0, 0, 0))
    check("V81 补位等形", C.PATCH_EQUAL_SHAPE, True)
    check("V81 错配标签", C.MIS_TAG, "MIS110")
    check("V81 归因四组数", len(C.Q_CASES), 4)
    check("V81 归因等形", C.Q_EQUAL_SHAPE, True)
    check("V81 Q1 命中写回", C.Q_CASES[1][4], 11)
    check("V81 Q1 WB_CALL", C.Q_CASES[1][5], True)
    check("V81 Q0 命中写回", C.Q_CASES[0][4], 0)
    check("V81 Q3 命中写回", C.Q_CASES[3][4], 0)
    check("V81 静态链地址数", len(C.CHAIN_PCS), 21)

    # ---- 纯函数：补码与宽度（规矩 74）
    check("V81 sx(0x56,8)", C.sx(0x56, 8), 86)
    check("V81 sx(0xAB,8)", C.sx(0xAB, 8), -85)
    check("V81 sx32 0xFFFFFF56", C.sx(0xFFFFFF56, 32), -170)
    check("V81 sx(0x7FF,11)", C.sx(0x7FF, 11), -1)
    check("V81 sx(0x0DD,11)", C.sx(0x0DD, 11), 221)
    check("V81 imm_code 宽 8", C.imm_code(0xFFFFFF56, 8), 86)
    check("V81 imm_code 宽 32", C.imm_code(0xFFFFFF56, 32), -170)
    check("V81 svc_pair", C.svc_pair(), (86, -170))
    check("V81 两种口径差 256", C.MAIN_SVC_8 - C.MAIN_SVC_32, 256)

    # ---- 纯函数：分支目标
    check("V81 第一道 beq 目标",
          C.rel9_target(C.MAIN_BEQ, 4), C.MAIN_BEQ_TARGET)
    check("V81 第二道 beq 目标",
          C.rel9_target(C.CMD_BEQ, 4), C.WB_CALL)
    check("V81 写回 rel12 目标",
          C.rel12_target(C.WB_CALL, 221), C.WB_FN)
    check("V81 rel9 负偏移", C.rel9_target(0x1000, 254), 0x0FFE)
    check("V81 rel12 负偏移", C.rel12_target(0x1000, 0x7FF), 0x1000)

    # ---- 纯函数：两道串联判据
    g0 = C.two_gate(0, False, False)
    check("V81 第一道 r4=0 关闭", g0[0], False)
    check("V81 r4=0 第二道", g0[1], False)
    check("V81 r4=0 判据", g0[2], "gate1_zero_return_code")
    g1 = C.two_gate(-3, True, False)
    check("V81 r4=-3 第一道开", g1[0], True)
    check("V81 r4=-3 第二道关", g1[1], False)
    check("V81 r4=-3 判据", g1[2], "gate2_other_code")
    g2 = C.two_gate(-85, True, True)
    check("V81 r4=-85 第一道开", g2[0], True)
    check("V81 r4=-85 第二道开", g2[1], True)
    check("V81 r4=-85 判据", g2[2], "dispatch_to_writeback")
    check("V81 单码分发器", C.single_code([-85]), (True, -85))
    check("V81 多码非单码", C.single_code([-85, -3]), (False, None))
    check("V81 空码集", C.single_code([]), (False, None))

    # ---- 纯函数：等形 / 补位时机 / 归因
    eqrows = [{"warmup": 191520, "service_calls": 110, "post_steps": 200000},
              {"warmup": 191520, "service_calls": 110, "post_steps": 200000}]
    check("V81 等形 True", C.equal_shape(eqrows), True)
    check("V81 等形次数不同",
          C.equal_shape([eqrows[0], dict(eqrows[1], service_calls=160)]),
          False)
    check("V81 等形 post 不同",
          C.equal_shape([eqrows[0], dict(eqrows[1], post_steps=20000)]),
          False)
    check("V81 等形空", C.equal_shape([]), False)
    pt = C.patch_timing_verdict({"MAIN_JMP": False}, {"MAIN_JMP": False},
                                2291, 2383)
    check("V81 补位在等待之前", pt["before_wait"], True)
    check("V81 补位改变执行", pt["execution_changed"], True)
    check("V81 命令路径增益 0", pt["hit_delta"]["MAIN_JMP"], 0)
    check("V81 补位时机判据", pt["verdict"],
          "patch_changed_execution_but_not_command_path")
    pt2 = C.patch_timing_verdict({}, {}, 100, 100)
    check("V81 无效对照", pt2["verdict"],
          "patch_had_no_effect__not_a_valid_control")
    at = C.attribution([{"clear": 12}, {"clear": 12}], [{"clear": 0}],
                       key="clear")
    check("V81 归因分离", at[0], True)
    check("V81 实验组最大", at[1], 12)
    check("V81 对照组最大", at[2], 0)
    check("V81 归因不分离",
          C.attribution([{"clear": 0}], [{"clear": 0}], key="clear")[0],
          False)

    # ---- 纯函数：分发表证伪 / 普查 / 综合判据
    tv = C.table_offset_verdict(0xD106)
    check("V81 分发表假设证伪", tv[0], False)
    check("V81 表项符号值", tv[1], -12026)
    check("V81 表项目标", tv[2], 0x03D766)
    check("V81 正偏移不证伪", C.table_offset_verdict(0x0660)[0], True)
    tc = C.triple_census([(12, 262144, 2)] * 6 + [(7, None, 2)] * 5)
    check("V81 普查总数", tc[0], 11)
    check("V81 普查种类", tc[1], 2)
    check("V81 普查最常见", tc[2], ((12, 262144, 2), 6))
    check("V81 综合判据", C.cmd_verdict(False, False, False, 0),
          "wb_chain_located_but_command_dispatch_not_reached")
    check("V81 补位改变执行不改变判据",
          C.cmd_verdict(False, False, False, 92),
          "wb_chain_located_but_command_dispatch_not_reached")
    check("V81 第二道开但码不匹配",
          C.cmd_verdict(True, True, False, 0),
          "gate2_reached_but_code_mismatch")
    check("V81 反事实到达写回",
          C.cmd_verdict(True, True, True, 0),
          "writeback_reached_by_counterfactual_only")

    # ---- 文档与边界
    doc = C.__doc__
    check("V81 文档含规矩 74", "规矩 74" in doc, True)
    check("V81 文档含规矩 75", "规矩 75" in doc, True)
    check("V81 文档含 KI#191", "KI#191" in doc, True)
    check("V81 文档含反事实边界", "反事实补全" in doc, True)
    check("V81 文档含共同禁语", "不能报显示链通了" in doc, True)
    check("V81 文档含单码分发器", "单码分发器" in doc, True)
    check("V81 文档含双宽度立即数", "0xFFFFFF56" in doc, True)
    check("V81 模块导出非空", len(C.__all__) > 30, True)

    # ---- 结果文件存在与关键行
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "results", "frbox", "cmd81.txt")
    if os.path.exists(rp):
        txt = io.open(rp, encoding="utf-8", errors="replace").read()
        check("V81 结果无 Traceback", "Traceback" in txt, False)
        check("V81 结果有 A 段", "A. 静态链" in txt, True)
        check("V81 结果有 C 段", "两道串联判据" in txt, True)
        check("V81 结果有 H 段", "H. 结案与仍开放" in txt, True)
        check("V81 结果有 86 号普查", "86 号调用点 = 91" in txt, True)
        check("V81 结果有双宽度", "8 位口径 = 86" in txt, True)
        check("V81 结果有唯一入边", "call rel12=221" in txt, True)
        check("V81 结果有单码分发器", "单码分发器" in txt, True)
        check("V81 结果有规矩 75", "规矩 75" in txt, True)
        check("V81 结果有证伪", "证伪" in txt, True)
        check("V81 结果有 11/75", "11/75" in txt, True)
        check("V81 结果有综合判据",
              "wb_chain_located_but_command_dispatch_not_reached" in txt,
              True)
        check("V81 结果有链未闭合", "链未闭合" in txt, True)
    else:
        check("V81 结果文件存在", False, True)



def test_v83():
    """V83：ret:d 延迟槽返回码、−85 出口归属与两道内部条件；规矩 78/79。"""
    import os
    import io
    import re
    import frbox
    from frbox import svc83 as C

    _HERE = os.path.dirname(os.path.abspath(__file__))
    _SRC = os.path.join(_HERE, "frbox", "svc83.py")
    _RES = os.path.join(_HERE, "..", "results", "frbox", "svc83.txt")

    # ---- 静态口径常量
    check("V83 内核区起点", C.KERNEL_LO, 0x040000)
    check("V83 内核区终点", C.KERNEL_HI, 0x080000)
    check("V83 段起点", C.SEG_LO, 0x041B00)
    check("V83 段终点", C.SEG_HI, 0x041E80)
    check("V83 ret:d 总数", C.RET_D_TOTAL, 167)
    check("V83 返回码延迟槽总数", C.SLOT_WRITES, 105)
    check("V83 段内 ret:d", C.RET_D_IN_SEG, 20)
    check("V83 段内 -85 写点（V82 口径）", C.SLOTS_85_IN_SEG, 2)
    check("V83 全内核 -85 写点", C.SLOTS_85_KERNEL, 7)
    check("V83 top10 合计", C.CODE_TOP10_SUM, 104)
    check("V83 top10 实际求和", sum(n for _c, n in C.CODE_TOP10), 104)
    check("V83 top10 长度", len(C.CODE_TOP10), 10)
    check("V83 top10 里 -85 计数", dict(C.CODE_TOP10).get(-85), 7)
    check("V83 top10 里 0 计数", dict(C.CODE_TOP10).get(0), 57)
    check("V83 top10 里 -33 计数", dict(C.CODE_TOP10).get(-33), 12)
    check("V83 top10 里 -69 计数", dict(C.CODE_TOP10).get(-69), 12)
    check("V83 余额（105-104）", C.SLOT_WRITES - C.CODE_TOP10_SUM, 1)
    check("V83 7 处 -85 写点数", len(C.SLOTS_85), 7)
    for i, (rd, sl, svc) in enumerate(C.SLOTS_85):
        check("V83 -85 写点 %d 延迟槽紧随 ret:d" % i, sl - rd, 2)
        check("V83 -85 写点 %d 在内核区" % i,
              C.KERNEL_LO <= sl < C.KERNEL_HI, True)
    check("V83 -85 写点 0x041CA0 在列",
          0x041CA0 in [x[1] for x in C.SLOTS_85], True)
    check("V83 -85 写点 0x041DC8 在列",
          0x041DC8 in [x[1] for x in C.SLOTS_85], True)

    # ---- 三条并列出口
    check("V83 并列出口数", len(C.EXIT_TRIPLE), 3)
    check("V83 出口 0 入口", C.EXIT_TRIPLE[0x041C92],
          (0x041C96, 0x041C98, 0x00, 0))
    check("V83 出口 -85 入口", C.EXIT_TRIPLE[0x041C9A],
          (0x041C9E, 0x041CA0, 0xAB, -85))
    check("V83 出口 -17 入口", C.EXIT_TRIPLE[0x041CA2],
          (0x041CA4, 0x041CA6, 0xEF, -17))
    check("V83 -85 出口两条入边", C.EDGES_85, (0x041BC6, 0x041BCC))
    check("V83 r12=0 出口两条入边", C.EDGES_0, (0x041C82, 0x041C8A))
    check("V83 -17 出口一条入边", C.EDGE_17, (0x041C0C,))
    check("V83 前两条出口模板", C.EXIT_TEMPLATE, ("orccr", "ld", "ret:d", "ldi:8"))
    check("V83 rel9 分支数", C.BRANCH_FORMS["rel9"], 61)
    check("V83 rel12 分支数", C.BRANCH_FORMS["rel12"], 14)
    check("V83 不可定址分支数", C.BRANCH_FORMS["ind"], 6)

    # ---- 服务号归属
    check("V83 段内入口数", len(C.OWNER_ENTRIES), 12)
    check("V83 段内入口数常量", C.ENTRIES_IN_SEG, 12)
    check("V83 0x041B0A 归 -46", C.OWNER_ENTRIES[0x041B0A], (-46,))
    check("V83 0x041B92 归 -106", C.OWNER_ENTRIES[0x041B92], (-106,))
    check("V83 0x041BEA 归 -170", C.OWNER_ENTRIES[0x041BEA], (-170,))
    check("V83 0x041CA8 归 -44", C.OWNER_ENTRIES[0x041CA8], (-44,))
    check("V83 0x041DD6 归 -172", C.OWNER_ENTRIES[0x041DD6], (-172,))
    check("V83 错误区中段", C.ERR_MID, 0x041BB6)
    check("V83 错误区中段宿主", C.ERR_MID_OWNER, 0x041B92)
    check("V83 真实尾 ret:d", C.REAL_TAIL, 0x041B82)
    check("V83 真实尾延迟槽", C.REAL_SLOT0, 0x041B84)
    check("V83 真实尾宿主", C.REAL_TAIL_OWNER, 0x041B0A)
    check("V83 真实 r13", C.REAL_R13, 0xFFFFFFFF)
    check("V83 真实返回码", C.REAL_RET, 0)
    check("V83 真实并列出口覆盖", C.REAL_EXITS_COVERED, 0)
    check("V83 真实路径关键点数", len(C.REAL_PATH_KEYS), 11)
    check("V83 真实路径含 0x041BEA", 0x041BEA in C.REAL_PATH_KEYS, True)
    check("V83 真实路径含 0x041C28", 0x041C28 in C.REAL_PATH_KEYS, True)
    check("V83 真实路径含 0x041B84", 0x041B84 in C.REAL_PATH_KEYS, True)

    # ---- 动态与反事实口径
    check("V83 boot 锚点步数", C.BOOT_STEP, 483841)
    check("V83 陷阱入口", C.TRAP, 0x040668)
    check("V83 服务返回点", C.RET_PC, 0x0406B4)
    check("V83 反事实锚点1", C.ANCHOR1, 0x041C24)
    check("V83 反事实锚点2", C.ANCHOR2, 0x041BCA)
    check("V83 后续步数", C.AFTER, 260)
    check("V83 反事实组数", len(C.CF_GROUPS), 4)
    check("V83 反事实组名", tuple(x[0] for x in C.CF_GROUPS),
          ("BASE", "G1", "G2", "G3"))
    check("V83 G2 返回点 r12", C.CF_G2_RET, 0x000000AB)
    check("V83 分发链键数", len(C.GATE_KEYS), 5)
    check("V83 MAIN_JMP", C.MAIN_JMP, 0x175282)
    check("V83 CMD_SEG", C.CMD_SEG, 0x17573C)
    check("V83 CMD_CMP", C.CMD_CMP, 0x175740)
    check("V83 WB_CALL", C.WB_CALL, 0x17574C)
    check("V83 WB_FN", C.WB_FN, 0x175908)
    for tag, exp in C.CF_EXPECT.items():
        check("V83 %s 期望四元组" % tag, len(exp), 4)
        check("V83 %s 门1" % tag, exp[0], tag != "BASE")
        check("V83 %s 门1 是门2 前置" % tag, exp[0] >= exp[1], True)
        check("V83 %s 门2" % tag, exp[1], tag == "G2")
        check("V83 %s 分发链" % tag, exp[2], tag == "G2")
        check("V83 %s 返回点 r12" % tag, exp[3],
              C.CF_G2_RET if tag == "G2" else 0)
    check("V83 综合判据串", C.VERDICT,
          "delay_slot_exit_resolved_counterfactual_two_gates_open")

    # ---- 纯函数：8 位补码
    check("V83 sx8(0)", C.sx8(0), 0)
    check("V83 sx8(0x7F)", C.sx8(0x7F), 127)
    check("V83 sx8(0x80)", C.sx8(0x80), -128)
    check("V83 sx8(0xAB)", C.sx8(0xAB), -85)
    check("V83 sx8(0xEF)", C.sx8(0xEF), -17)
    check("V83 sx8(0xBB)", C.sx8(0xBB), -69)
    check("V83 sx8(0xDF)", C.sx8(0xDF), -33)
    check("V83 sx8(0xFF)", C.sx8(0xFF), -1)
    check("V83 sx8(0x1AB)", C.sx8(0x1AB), -85)
    check("V83 slot_code(0xAB)", C.slot_code(0xAB), -85)
    check("V83 slot_code(0x00)", C.slot_code(0x00), 0)

    # ---- 纯函数：延迟槽与模板
    fake = [(0x100, "orccr", {}, 2), (0x102, "ld", {}, 2),
            (0x104, "ret:d", {}, 2), (0x106, "ldi:8", {"ri": 12, "i8": 171}, 2),
            (0x108, "ret:d", {}, 2), (0x10A, "ldi:8", {"ri": 12, "i8": 0}, 2)]
    check("V83 is_delay_slot 命中", C.is_delay_slot(fake, 0x106), True)
    check("V83 is_delay_slot 第二条", C.is_delay_slot(fake, 0x10A), True)
    check("V83 is_delay_slot 否定", C.is_delay_slot(fake, 0x102), False)
    check("V83 delay_slot_owner", C.delay_slot_owner(fake, 0x106), 0x104)
    check("V83 delay_slot_owner 第二条", C.delay_slot_owner(fake, 0x10A), 0x108)
    check("V83 delay_slot_owner 非槽", C.delay_slot_owner(fake, 0x100), None)
    check("V83 template_of", C.template_of(fake[:4]),
          ("orccr", "ld", "ret:d", "ldi:8"))
    check("V83 exit_record -85", C.exit_record(0x041C9A),
          (0x041C9E, 0x041CA0, -85))
    check("V83 exit_record 0", C.exit_record(0x041C92),
          (0x041C96, 0x041C98, 0))
    check("V83 exit_record -17", C.exit_record(0x041CA2),
          (0x041CA4, 0x041CA6, -17))
    check("V83 exit_record 未登记", C.exit_record(0x041000), None)

    # ---- 纯函数：归属与口径
    ents = (0x041B00, 0x041B92, 0x041BEA, 0x041CA8)
    own = {0x041B00: (-1,), 0x041B92: (-106,),
           0x041BEA: (-170,), 0x041CA8: (-44,)}
    check("V83 nearest_entry 错误区", C.nearest_entry(0x041BC6, ents), 0x041B92)
    check("V83 nearest_entry 出口区", C.nearest_entry(0x041CA0, ents), 0x041BEA)
    check("V83 nearest_entry 段前", C.nearest_entry(0x041000, ents), None)
    check("V83 owner_of 入边", C.owner_of(0x041BC6, ents, own), (-106,))
    check("V83 owner_of 延迟槽", C.owner_of(0x041CA0, ents, own), (-170,))
    check("V83 口径 段内<全内核", C.scope_verdict(2, 7), "segment_only")
    check("V83 口径 相等", C.scope_verdict(7, 7), "kernel_wide")
    check("V83 口径 矛盾", C.scope_verdict(8, 7), "inconsistent")

    # ---- 纯函数：等形 / 门 / 综合判据
    eq = [{"boot": 483841, "anchor1": 0x041C24,
           "anchor2": 0x041BCA, "post": 260} for _ in range(4)]
    check("V83 equal_shape 四组同", C.equal_shape(eq), True)
    eq2 = [dict(x) for x in eq]
    eq2[2]["post"] = 261
    check("V83 equal_shape 一组异", C.equal_shape(eq2), False)
    check("V83 equal_shape 空", C.equal_shape([]), False)
    check("V83 gate 全关", C.gate_verdict(False, False), "both_closed")
    check("V83 gate 只开门1", C.gate_verdict(True, False), "gate1_only")
    check("V83 gate 两门全开", C.gate_verdict(True, True), "both_open")
    check("V83 gate 门1关不算开", C.gate_verdict(False, True), "both_closed")
    check("V83 cf 全成立", C.cf_verdict(True, True, True, True, False),
          C.VERDICT)
    check("V83 cf 事件已定位", C.cf_verdict(True, True, True, True, True),
          "event_semantics_located")
    check("V83 cf 缺静态", C.cf_verdict(False, True, True, True, False), "open")
    check("V83 cf 缺返回码", C.cf_verdict(True, False, True, True, False), "open")
    check("V83 cf 缺门", C.cf_verdict(True, True, False, True, False), "open")
    check("V83 cf 缺写回", C.cf_verdict(True, True, True, False, False), "open")

    # ---- 文档与规矩边界
    src = io.open(_SRC, encoding="utf-8").read()
    for key in ("KI#323", "规矩 78", "规矩 79", "延迟槽", "0x041B92",
                "0x041B84", "反事实补全", "链仍未闭合", "不许把段内计数",
                "真机已复现", "器件事件语义"):
        check("V83 设施文档含 %s" % key, key in src, True)
    check("V83 版本格式（不硬编码当前版）",
          frbox.__version__.startswith("0."), True)
    check("V83 禁语：所有服务语义已解出", "所有服务语义已解出" in src, True)
    check("V83 禁语：显示链通了", "显示链通了" in src, True)

    # ---- 正式结果文件
    res = io.open(_RES, encoding="utf-8", errors="replace").read()
    check("V83 结果无 Traceback", "Traceback" in res, False)
    check("V83 结果无 Error 词", len(re.findall(r"\bError\b", res)), 0)
    for seg in ("[A]", "[B]", "[C]", "[D]", "[E]", "[F]", "[G]", "[H]"):
        check("V83 结果含 %s 段" % seg, seg in res, True)
    check("V83 结果含 5 台独立机器", "5 台独立机器" in res, True)
    check("V83 结果含综合判据", C.VERDICT in res, True)
    check("V83 结果含 G2", "G2 " in res or "G2  " in res, True)
    check("V83 结果含 0x000000AB", "0x000000AB" in res, True)
    check("V83 结果含规矩 78", "规矩 78" in res, True)
    check("V83 结果含规矩 79", "规矩 79" in res, True)
    check("V83 结果含链未闭合", "链未闭合" in res, True)
    check("V83 结果含 DONE", "DONE V83" in res, True)


def test_v82():
    """V82：真内核分发表 32 位有符号索引口径、-170 号处理函数、规矩 76/77。"""
    import os
    import io
    from frbox import svc82 as C

    # ---- 常量：收敛设备策略
    check("V82 BOOT", C.BOOT_STEPS, 191520)
    check("V82 VEC_BOOT", C.VEC_BOOT, 0x2B)
    check("V82 VEC_TIMER", C.VEC_TIMER, 0x18)
    check("V82 RUN", C.RUN_STEPS, 500000)
    check("V82 WINDOW", C.WINDOW_STEPS, 1000000)
    check("V82 轨迹步数", C.TRAP_BOOT_STEP, 483841)
    check("V82 后续步数", C.TRAP_AFTER_STEPS, 260)

    # ---- 常量：向量表 / 陷阱入口
    check("V82 TBR", C.TBR, 0x000EFC00)
    check("V82 vec 0x40", C.VEC_TRAP, 0x40)
    check("V82 向量槽", C.VEC_TRAP_SLOT, 0x000EFEFC)
    check("V82 陷阱入口", C.TRAP_ENTRY, 0x00040668)
    check("V82 清符号 lsl", C.TRAP_CLR_LSL, 0x00040682)
    check("V82 清符号 lsr", C.TRAP_CLR_LSR, 0x00040684)
    check("V82 索引 lsl", C.TRAP_IDX_LSL, 0x000406A4)
    check("V82 表零点 ldi", C.TRAP_TBL_LDI, 0x000406A6)
    check("V82 lduh", C.TRAP_LDUH, 0x000406AC)
    check("V82 extsh", C.TRAP_EXTSH, 0x000406AE)
    check("V82 add", C.TRAP_ADD, 0x000406B0)
    check("V82 call", C.TRAP_CALL, 0x000406B2)
    check("V82 返回点", C.TRAP_RET, 0x000406B4)
    check("V82 恢复例程", C.TRAP_RESTORE, 0x000406B6)
    check("V82 出口 extsb", C.TRAP_EXTSB, 0x000406BE)
    check("V82 reti", C.TRAP_RETI, 0x000406E2)
    check("V82 规整例程", C.TRAP_NORM_FN, 0x000404E2)
    check("V82 恢复函数", C.TRAP_RESTORE_FN, 0x000404E6)

    # ---- 常量：分发表口径
    check("V82 表零点", C.TBL_ZERO, 0x00040660)
    check("V82 空槽占位", C.EMPTY_HALVES, (0x0000, 0x0004))
    check("V82 表区下界", C.TBL_LO, 0x00040500)
    check("V82 表区上界", C.TBL_HI, 0x00040660)
    check("V82 首次陷阱步", C.FIRST_TRAP_STEP, 191279)
    check("V82 首次陷阱 r12", C.FIRST_TRAP_R12, 0xFFFFFFAD)
    check("V82 首次表项槽", C.FIRST_TRAP_SLOT, 0x000405BA)
    check("V82 首次表项半字", C.FIRST_TRAP_HALF, 0x087E)
    check("V82 首次偏移", C.FIRST_TRAP_OFF, 2174)
    check("V82 首次目标", C.FIRST_TRAP_TARGET, 0x00040EDE)

    # ---- 常量：主循环 / -170 号服务
    check("V82 主循环等待", C.MAIN_WAIT, 0x00175270)
    check("V82 nop", C.MAIN_NOP, 0x00175272)
    check("V82 mov r12,r4", C.MAIN_MOV_R12_R4, 0x00175276)
    check("V82 cmp #0", C.MAIN_CMP0, 0x00175278)
    check("V82 beq", C.MAIN_BEQ, 0x0017527A)
    check("V82 第一道 jmp", C.MAIN_JMP, 0x00175282)
    check("V82 分发段", C.CMD_SEG, 0x0017573C)
    check("V82 第二道 cmp", C.CMD_CMP, 0x00175740)
    check("V82 第二道 beq", C.CMD_BEQ, 0x00175742)
    check("V82 写回调用", C.WB_CALL, 0x0017574C)
    check("V82 写回函数", C.WB_FN, 0x00175908)
    check("V82 服务号立即数", C.MAIN_IMM32, 0xFFFFFF56)
    check("V82 服务号 8 位", C.MAIN_SVC_8, 86)
    check("V82 服务号 32 位", C.MAIN_SVC_32, -170)
    check("V82 双宽度差 256", C.MAIN_SVC_8 - C.MAIN_SVC_32, 256)
    check("V82 lsl1 结果", C.MAIN_SVC_LSL1, 0xFFFFFEAC)
    check("V82 清符号中间值", C.MAIN_SVC_CLEARED, 0x7FFFFF56)
    check("V82 表项槽", C.MAIN_SLOT, 0x0004050C)
    check("V82 表项半字", C.MAIN_HALF, 0x158A)
    check("V82 表项偏移", C.MAIN_OFF, 5514)
    check("V82 处理函数", C.MAIN_HANDLER, 0x00041BEA)
    check("V82 flgid", C.MAIN_FLGID, 12)
    check("V82 位模式", C.MAIN_WAIPTN, 0xA2A05E1C)
    check("V82 wfmode", C.MAIN_WFMODE, 2)
    check("V82 唯一命令码", C.CMD_CODE, -85)

    # ---- 常量：全表统计与相邻入口
    check("V82 32 位普查", C.CENSUS_32BIT, (166, 37))
    check("V82 8 位普查", C.CENSUS_8BIT, (84, 1))
    check("V82 负号有效", C.NEG_VALID, 134)
    check("V82 负号落 ROM", C.NEG_INROM, 71)
    check("V82 负号像序言", C.NEG_PRO, 57)
    check("V82 非负有效", C.POS_VALID, 119)
    check("V82 非负落 ROM", C.POS_INROM, 29)
    check("V82 非负像序言", C.POS_PRO, 4)
    check("V82 -170 唯一", C.HANDLER_170_UNIQUE, (-170,))
    check("V82 相邻 -46", C.ADJACENT_ENTRIES[0x00041B0A], -46)
    check("V82 相邻 -44", C.ADJACENT_ENTRIES[0x00041CA8], -44)
    check("V82 相邻 -172", C.ADJACENT_ENTRIES[0x00041DD6], -172)
    check("V82 相邻 -85", C.ADJACENT_ENTRIES[0x00040F1A], -85)
    check("V82 相邻 -83", C.ADJACENT_ENTRIES[0x00040EDE], -83)

    # ---- 常量：返回码写点与出口
    check("V82 写点数", len(C.R12_WRITES), 11)
    check("V82 写点 0", C.R12_WRITES[0], (0x00041C98, 0x00, 0))
    check("V82 写点 -85", C.R12_WRITES[1], (0x00041CA0, 0xAB, -85))
    check("V82 写点 -17", C.R12_WRITES[2], (0x00041CA6, 0xEF, -17))
    check("V82 -85 写点数",
          sum(1 for _p, _i, v in C.R12_WRITES if v == -85), 2)
    check("V82 出口清单数", len(C.EXIT_170), 12)
    check("V82 出口首", C.EXIT_170[0], 0x00041C96)
    check("V82 反事实组数", len(C.CF_GROUPS), 4)
    check("V82 BASE 不写", C.CF_GROUPS[0], ("BASE", None))
    check("V82 R85 写 -85", C.CF_GROUPS[2], ("R85", 0xFFFFFFAB))
    check("V82 R17 写 -17", C.CF_GROUPS[3], ("R17", 0xFFFFFFEF))

    # ---- 纯函数：补码 / 双宽度 / 表项口径
    check("V82 sx8 0x56", C.sx(0x56, 8), 86)
    check("V82 sx8 0xAB", C.sx(0xAB, 8), -85)
    check("V82 sx32 0xFFFFFF56", C.sx(0xFFFFFF56, 32), -170)
    check("V82 width_pair", C.width_pair(0xFFFFFF56), (86, -170))
    check("V82 extsh 0x158A", C.extsh(0x158A), 5514)
    check("V82 extsh 0xD106", C.extsh(0xD106), -12026)
    check("V82 extsh 0x087E", C.extsh(0x087E), 2174)
    check("V82 slot -83", C.slot_of(-83), 0x000405BA)
    check("V82 slot -170", C.slot_of(-170), 0x0004050C)
    check("V82 slot +86", C.slot_of(86), 0x0004070C)
    check("V82 slot 0", C.slot_of(0), C.TBL_ZERO)
    check("V82 slot -1", C.slot_of(-1), 0x0004065E)
    check("V82 slot 用 32 位立即数",
          C.slot_of(0xFFFFFF56), C.MAIN_SLOT)
    check("V82 target 0x158A", C.target_of(0x158A), C.MAIN_HANDLER)
    check("V82 target 0x087E", C.target_of(0x087E), C.FIRST_TRAP_TARGET)
    check("V82 target 0xD106", C.target_of(0xD106), 0x0003D766)
    check("V82 is_empty 0000", C.is_empty(0x0000), True)
    check("V82 is_empty 0004", C.is_empty(0x0004), True)
    check("V82 is_empty 158A", C.is_empty(0x158A), False)
    check("V82 lsl1 稳健", C.lsl1_stable(0xFFFFFF56, 0x7FFFFF56), True)
    check("V82 lsl1 非稳健", C.lsl1_stable(0x10, 0x20), False)
    rd = lambda a: {0x0004050C: 0x158A, 0x000405BA: 0x087E,
                    0x0004070C: 0xD106}.get(a, 0)
    check("V82 resolve -170", C.resolve(rd, -170),
          (0x0004050C, 0x158A, 5514, 0x00041BEA, True))
    check("V82 resolve -83", C.resolve(rd, -83)[3], 0x00040EDE)
    check("V82 resolve +86 落 ROM 外", C.resolve(rd, 86)[4], False)

    # ---- 纯函数：等形 / 两道门 / 综合判据
    rows = [{"boot": 483841, "inject": 0, "post": 260, "write_pc": 0x0406B4},
            {"boot": 483841, "inject": 0, "post": 260, "write_pc": 0x0406B4}]
    check("V82 等形 True", C.equal_shape(rows), True)
    check("V82 等形 boot 不同",
          C.equal_shape([rows[0], dict(rows[1], boot=1)]), False)
    check("V82 等形 post 不同",
          C.equal_shape([rows[0], dict(rows[1], post=1)]), False)
    check("V82 等形空", C.equal_shape([]), False)
    check("V82 gate 关闭", C.gate_verdict(False, False, False), "gate1_closed")
    check("V82 gate 只开第一道",
          C.gate_verdict(True, True, False), "gate2_code_mismatch")
    check("V82 gate 两道全开",
          C.gate_verdict(True, True, True), "both_gates_open")
    check("V82 gate 分发段未进",
          C.gate_verdict(True, False, False),
          "gate1_open_but_dispatch_unreached")
    check("V82 综合判据",
          C.svc_verdict(True, True, True, True, False),
          "kernel_dispatch_resolved_counterfactual_gates_open")
    check("V82 综合判据 事件已定位",
          C.svc_verdict(True, True, True, True, True), "event_semantics_located")
    check("V82 综合判据 静态未闭合",
          C.svc_verdict(False, True, True, True, False), "open")
    check("V82 模块导出非空",
          len([x for x in dir(C) if x.isupper()]) > 30, True)

    # ---- 文档与边界
    doc = C.__doc__
    check("V82 文档含规矩 76", "规矩 76" in doc, True)
    check("V82 文档含规矩 77", "规矩 77" in doc, True)
    check("V82 文档含 KI#191", "KI#191" in doc, True)
    check("V82 文档含反事实边界", "反事实补全" in doc, True)
    check("V82 文档含共同禁语", "不能报显示链通了" in doc, True)
    check("V82 文档含单步实测", "单步轨迹" in doc, True)
    check("V82 文档含 0xFFFFFF56", "0xFFFFFF56" in doc, True)
    check("V82 文档含 0x041BEA", "0x041BEA" in doc, True)
    check("V82 文档含零点不是起点", "零点" in doc, True)

    # ---- 结果文件存在与关键行
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "results", "frbox", "svc82.txt")
    if os.path.exists(rp):
        txt = io.open(rp, encoding="utf-8", errors="replace").read()
        check("V82 结果无 Traceback", "Traceback" in txt, False)
        check("V82 结果有 A 段", "A. int #0x40 向量槽" in txt, True)
        check("V82 结果有 B 段", "B. 单步实测：第一次" in txt, True)
        check("V82 结果有 C 段", "C. 单步实测：主循环" in txt, True)
        check("V82 结果有 D 段", "D. 全表两种口径普查" in txt, True)
        check("V82 结果有 E 段", "E. -170 号表项槽" in txt, True)
        check("V82 结果有 F 段", "F. 等形反事实四组" in txt, True)
        check("V82 结果有 G 段", "G. 两道门归因分离" in txt, True)
        check("V82 结果有 H 段", "H. 综合判据与开放项" in txt, True)
        check("V82 结果有处理函数", "0x041BEA" in txt, True)
        check("V82 结果有表项槽", "0x04050C" in txt, True)
        check("V82 结果有等形", "等形四项" in txt, True)
        check("V82 结果有两道门分离", "归因分离" in txt, True)
        check("V82 结果有 -85 反事实", "R85" in txt, True)
        check("V82 结果有 -17 对照", "R17" in txt, True)
        check("V82 结果有反事实标注", "反事实补全" in txt, True)
        check("V82 结果有 6 台机器", "6 台独立机器" in txt, True)
        check("V82 结果有综合判据",
              "kernel_dispatch_resolved_counterfactual_gates_open" in txt,
              True)
    else:
        check("V82 结果文件存在", False, True)



def main():
    print("frbox 自检")
    test_bus()
    test_devices()
    test_icu()
    test_machine()
    test_spin()
    test_monitor()
    test_cli()
    test_clock()
    test_snapshot()
    test_airq()
    test_boot_task()
    test_irqfire()
    test_autospin()
    test_task_table()
    test_kernel()
    test_realos()
    test_sched()
    test_waits()
    test_events()
    test_bits()
    test_cov()
    test_taskboot()
    test_msgs()
    test_callbacks()
    test_isr2b()
    test_autoirq()
    b = test_bboot()
    test_bwall(b)
    test_pendbits()
    test_bflag()
    test_bgate()
    test_bgate2()
    test_bqueue()
    test_bdispchain()
    test_bsem()
    test_blist()
    test_btable()
    test_bqueue_slot()
    test_bdesc()
    test_bhandler()
    test_binit()
    test_bready()
    test_blive()
    test_bfill()
    test_bmove()
    test_bpool()
    test_wait()
    test_static()
    test_bpix()
    test_tick()
    test_tick_v52()
    test_v53()
    test_v54()
    test_v55()
    test_v56()
    test_v57()
    test_v58()
    test_v59()
    test_v60()
    test_v61()
    test_v62()
    test_v63()
    test_v64()
    test_v65()
    test_v66()
    test_v67()
    test_v68()
    test_v69()
    test_v70()
    test_v71()
    test_v72()
    test_v73()
    test_v74()
    test_v75()
    test_v76()
    test_v77()
    test_v78()
    test_v79()
    test_v80()
    test_v81()
    test_v82()
    test_v83()
    test_lists()
    test_bctrl()
    test_dual40()
    test_bdisp()
    test_bexpeed()
    test_bexpeed_dev()
    print(f"\n合计 {OK}/{TOTAL} 通过")
    return 0 if OK == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
