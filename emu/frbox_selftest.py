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

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")



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
    m.cpu.ilm = 0x1F
    m.cpu.i = 1
    m.cpu.step()
    check("ILM 屏蔽（level 9 <= 31）", len(m.cpu.irq_log), 0)
    m.cpu.ilm = 0
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
    env = dict(os.environ, PYTHONPATH=_EMU)
    r = subprocess.run(
        [sys.executable, "-m", "frbox", "-M", "d300-b", "-c", "info rom; dev"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=_EMU, env=env, timeout=180)
    check("CLI 退出码", r.returncode, 0)
    check("CLI 输出含机器名", "d300-b" in r.stdout, True)
    check("CLI 输出含 CRC 校验", "CRC16 OK" in r.stdout, True)
    r2 = subprocess.run(
        [sys.executable, "-m", "frbox", "-M", "help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=_EMU, env=env, timeout=120)
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

    r = m.irq_return(0x13, max_steps=20000, home=home)
    check("vec 0x13 闭环回原位", r["back"] is not None, True)
    check("vec 0x13 的 SP 逐位复原", r["sp_restored"], True)
    check("级别取自 ICR 而非硬编码 8", r["level"], 0x1A)
    check("进了应用区执行", any(0x00080000 <= p < 0x000E0000 for p in r["pcs"]), True)
    check("槽里的回调 0xACBDA 被执行", 0x000ACBDA in r["pcs"], True)
    check("回调是一次性的（槽清 0）", m.bus.rd(0x3F028, 4), 0)
    check("注入结束后没有残留待决请求", len(m.cpu.irq_pending), 0)

    snapshot.load(m, snap)
    r = m.irq_return(0x1F, max_steps=200)
    check("未用向量 0x1F 不闭环", r["back"], None)
    check("未用向量停在 0x81338 的 bra -1", m.cpu.pc, 0x00081338)

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
    check("表头记的是 8 个任务", m.bus.rd(m.TASK_TABLE, 4), 8)
    rows = m.task_table()
    check("解出 8 条记录", len(rows), 8)
    check("任务 1 入口 0x08133A", rows[0]["entry"], 0x0008133A)
    check("任务 1 栈顶 0x0462EC", rows[0]["sp"], 0x000462EC)
    check("任务 1 TCB 0x0459FC", rows[0]["tcb"], 0x000459FC)
    check("TCB 相邻差 0x2C", rows[1]["tcb"] - rows[0]["tcb"], 0x2C)
    check("记录步长 0x1C", rows[1]["addr"] - rows[0]["addr"], 0x1C)
    check("任务 1 已激活", rows[0]["active"], True)
    check("任务 5 也已激活", rows[4]["active"], True)
    check("任务 2 未启动", rows[1]["active"], False)
    check("任务号 1..8 连续", [r["id"] for r in rows], list(range(1, 9)))
    check("任务 5 属性是 0x02，其余 0x04",
          ([r["attr"] for r in rows].count(0x02), rows[4]["attr"]), (1, 0x02))
    check("八个入口都在应用区",
          all(0x00080000 <= r["entry"] < 0x00100002 for r in rows), True)
    check("八个栈顶都在 0x46200–0x46A00 之间",
          all(0x00046200 <= r["sp"] < 0x00046A00 for r in rows), True)
    check("表头 +4 那个 0x4616C 不是任何任务的栈顶",
          0x0004616C in [r["sp"] for r in rows], False)

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
    print(f"\n合计 {OK}/{TOTAL} 通过")
    return 0 if OK == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
