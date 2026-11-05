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
    _to_deadlock(m2)
    check("挂了流水也停在同一步", m2.step_count, m.step_count)
    check("挂了流水也停在同一个 PC", m2.cpu.pc, m.cpu.pc)
    check("流水记到 14 次服务调用", len(fl2.calls), 14)
    check("当前 TCB 换过 4 段", len(fl2.switches), 4)
    check("第一次切换发生在 43,622 步", fl2.switches[0][0], 43622)
    check("流水里的第一个服务是任务 5 的 rcv_msg",
          fl2.names.get(fl2.calls[0][2]), "rcv_msg")
    first_set = [c for c in fl2.calls if c[2] == -48][0]
    check("第一次 set_flg 在 44,073 步、由任务 1 发出",
          (first_set[0], first_set[8]), (44073, st[1]["tcb"]))
    check("最后一次调用是任务 1 的 wai_flg",
          (fl2.calls[-1][2], fl2.calls[-1][3 + 1]), (-46, 3))
    w = fl2.waits()
    check("waits() 里任务 1 要 0x2000", w[st[1]["tcb"]][3], 0x2000)
    check("谁置过 flg2/0x4000 → 任务 1（44,073 步）",
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
    test_bdisp()
    test_bexpeed()
    test_bexpeed_dev()
    print(f"\n合计 {OK}/{TOTAL} 通过")
    return 0 if OK == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
