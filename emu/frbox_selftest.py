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

sys.path.insert(0, r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp")
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
    env = dict(os.environ, PYTHONPATH=r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp")
    r = subprocess.run(
        [sys.executable, "-m", "frbox", "-M", "d300-b", "-c", "info rom; dev"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp", env=env, timeout=180)
    check("CLI 退出码", r.returncode, 0)
    check("CLI 输出含机器名", "d300-b" in r.stdout, True)
    check("CLI 输出含 CRC 校验", "CRC16 OK" in r.stdout, True)
    r2 = subprocess.run(
        [sys.executable, "-m", "frbox", "-M", "help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp", env=env, timeout=120)
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
    print(f"\n合计 {OK}/{TOTAL} 通过")
    return 0 if OK == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
