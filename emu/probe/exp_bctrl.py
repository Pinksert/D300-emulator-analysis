# -*- coding: utf-8 -*-
"""V45 探针：0x5000_0000 三通道位串行控制块（KI#176）。

V44 收尾时还剩 1,933 笔未映射写，全在五个「外设窗口」里。这一版把最大的一块
（`0x50xxxxxx`，80 笔写 / 58 次读）从「未映射、读恒 0」变成一个**有寄存器的器件**，
并把 0x40 / 0x6F 两个窗口的性质改正过来（它们是只写数据汇，不是寄存器）。

分段：
    A  三个窗口的访问清单（0x50 已建模 ⇒ 走 io_writes；0x40/0x6F/0x60/0x5C 仍未映射）
    B  0x50 的位串行时序：时钟 / 起始位 / 片选 / 解出的 16 位载荷
    C  三个通道 + 全局寄存器的存值快照（boot 配置的是什么）
    D  反事实：默认 / 状态位打桩（4 档）/ 铺 RAM —— 整机控制流怎么变
    E  0x40 / 0x6F 是「只写数据汇」的证据
    F  全局 +01 的 bit0 是状态位（自检回归挖出来的，含 1 步相位的逐拍对照）

跑法（在 emu/ 下）：
    python probe/exp_bctrl.py > ../results/frbox/bctrl.txt
"""
import sys
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8')

from collections import Counter

import fr_dis
from frbox import blive, machine

STEPS = 700000
BASE_EXTRA = list(machine.D300B.extra_ram)

WINDOWS = [
    ("0x40xxxxxx", 0x40000000, 0x41000000),
    ("0x6Fxxxxxx", 0x6F000000, 0x70000000),
    ("0x60/0x61", 0x60000000, 0x62000000),
    ("0x5C0020xx", 0x5C002000, 0x5C002100),
]


def run(extra=None, status_cfg=None, status_cmd=None, hook=None, steps=STEPS,
        ctrl50=True):
    """建机 → 跑 → 还原口径。每次都是**新机器**（同一台不能跑第二遍）。

    `ctrl50=False` 时把设备摘掉（V45 之前的口径），用来和「铺 RAM」对照。
    """
    if extra is None:
        machine.D300B.extra_ram = list(BASE_EXTRA)
    else:
        machine.D300B.extra_ram = list(BASE_EXTRA) + list(extra)
    if status_cfg is not None:
        machine.D300B.ctrl50_status_cfg = status_cfg
    if status_cmd is not None:
        machine.D300B.ctrl50_status_cmd = status_cmd
    base_ctrl = machine.D300B.ctrl50_base
    if not ctrl50:
        machine.D300B.ctrl50_base = 0
    try:
        m = blive.boot()
        if hook:
            hook(m)
        res = blive.run(m, steps=steps)
        return m, res
    finally:
        machine.D300B.extra_ram = list(BASE_EXTRA)
        machine.D300B.ctrl50_status_cfg = 0
        machine.D300B.ctrl50_status_cmd = 0
        machine.D300B.ctrl50_base = base_ctrl


def snapshot(m, res):
    return {"sw": len(res["switches"]), "pc": m.cpu.pc,
            "icr": m.bus.rd(blive.DISPATCH_ICR, 1) & 0xFF,
            "tasks": list(res["tasks"]), "n": len(res["tasks"]),
            "uw": len(m.bus.unmapped_writes),
            "ur": len(m.bus.unmapped_reads),
            "urt": sum(m.bus.unmapped_reads.values())}


def line(tag, s):
    print("  %-26s 换手 %3d  入口 %2d  末PC 0x%08X  ICR 0x%02X  "
          "未映射写 %4d / 读 %2d 地址 %2d 次"
          % (tag, s["sw"], s["n"], s["pc"], s["icr"], s["uw"], s["ur"], s["urt"]))


# --------------------------------------------------------------------------- A
def sec_a():
    print("=" * 100)
    print("A. 窗口访问清单（0x50 已建模 ⇒ 走 io_writes，不再算未映射）")
    print("=" * 100)
    m, res = run()
    uw = m.bus.unmapped_writes
    print("  整机：步数 %d  未映射写 %d  未映射读 %d 地址 / %d 次"
          % (m.step_count, len(uw), len(m.bus.unmapped_reads),
             sum(m.bus.unmapped_reads.values())))
    print()
    for name, lo, hi in WINDOWS:
        recs = [r for r in uw if lo <= r[0] < hi]
        print("  %s  未映射写 %4d 笔" % (name, len(recs)))
        if recs:
            for pc, c in Counter(r[3] for r in recs).most_common(4):
                print("      写点 0x%08X × %d" % (pc, c))
            print("      地址范围 0x%08X..0x%08X"
                  % (min(r[0] for r in recs), max(r[0] for r in recs)))
    io50 = [w for w in m.bus.io_writes if 0x50000000 <= w[0] < 0x50000400]
    print()
    print("  0x50xxxxxx  已建模：写 %d 笔（都进了 ctrl50）" % len(io50))
    for pc, c in Counter(r[3] for r in io50).most_common(6):
        print("      写点 0x%08X × %d" % (pc, c))
    print("      读 %d 次 / %d 个地址"
          % (sum(k for a, k in m.bus.io_reads.items() if 0x50000000 <= a < 0x50000400),
             sum(1 for a in m.bus.io_reads if 0x50000000 <= a < 0x50000400)))
    return m, res


# --------------------------------------------------------------------------- B
def sec_b():
    print()
    print("=" * 100)
    print("B. 0x50 的位串行：时钟 / 起始位 / 片选 / 载荷")
    print("=" * 100)
    seq = []

    def hook(m):
        orig = m.bus.wr

        def wr(a, n, v, pc=0):
            if a == 0x50000105:
                seq.append((m.step_count, pc, v))
            return orig(a, n, v, pc)

        m.bus.wr = wr

    m, res = run(hook=hook)
    d = m.ctrl50
    print("  %s" % d.info())
    print("  每通道：时钟 %s / 起始位 %s / 片选边沿 %s / 载荷 %s"
          % (d.clocks, d.starts, d.cs_edges, d.tx))
    print()
    print("  0x50000105 的写入序列（通道 A，前 40 笔）：")
    print("    " + " ".join("%02X" % v for _s, _p, v in seq[:40]))
    print("  按值统计：%s"
          % ", ".join("0x%02X×%d" % (v, c) for v, c in
                      Counter(v for _s, _p, v in seq).most_common(6)))
    print()
    print("  位串行三步（每个时钟一圈）：")
    for pc, note in ((0x125830, "0x106 |= 0x04  片选/使能"),
                     (0x125838, "0x105 |= 0x20  起始位（片选后第一个时钟）"),
                     (0x12584C, "0x105 &= 0xD0  清 CLK/DATA，只留状态位"),
                     (0x12585A, "0x105 |= 0x08  该位为 1 时置 DATA"),
                     (0x125866, "0x105 |= 0x20  CLK 上升沿（从机在此采样）"),
                     (0x125868, "r4 >>= 1      换下一位（0x8000 走 16 圈）"),
                     (0x125840, "0x106 &= 0x0B  清片选")):
        print("    0x%08X  %s" % (pc, note))
    print()
    print("  循环头 0x125812 附近（实参 r6 = 要发的 16 位字）：")
    rom, base = m.bus.rom, m.bus.rom_base
    for t in fr_dis.disasm_addr(rom, 0x125812, 0x125842, base):
        print("    " + t)
    return m, res


# --------------------------------------------------------------------------- C
def sec_c(m):
    print()
    print("=" * 100)
    print("C. 寄存器快照（boot 配置成什么）")
    print("=" * 100)
    d = m.ctrl50
    names = {0x04: "+04 配置/状态", 0x05: "+05 串口", 0x06: "+06 片选",
             0x07: "+07 配置", 0x09: "+09 配置", 0x0A: "+0A 配置"}
    for ch in range(3):
        base = 0x100 + ch * 0x100
        cells = []
        for off in range(0x10):
            v = d.reg(ch, off)
            if v or (base + off) in [0x104, 0x105, 0x106]:
                cells.append("+%02X=%02X" % (off, v))
        print("  通道 %s（0x%03X）：%s" % ("ABC"[ch], base, "  ".join(cells)))
    print("  全局 0x00–0x0F：%s"
          % " ".join("%02X" % m.bus.rd(0x50000000 + i, 1) for i in range(16)))
    print()
    print("  boot 写配置的那段（0x40248 起，逐个 stb）：")
    rom, base = m.bus.rom, m.bus.rom_base
    for t in fr_dis.disasm_addr(rom, 0x40248, 0x40300, base):
        print("    " + t)


# --------------------------------------------------------------------------- D
def sec_d():
    print()
    print("=" * 100)
    print("D. 反事实：状态位 / 铺 RAM 对整机控制流的影响")
    print("=" * 100)
    print("  基准 = V45 新增的 ctrl50（读回配置、+05 只回状态位 0）")
    print()
    m0, r0 = run()
    s0 = snapshot(m0, r0)
    line("默认（状态位 0）", s0)
    print()

    for tag, kw in (("+04 bit1=1（cfg 0x02）", dict(status_cfg=0x02)),
                    ("+04 bit6=1（cfg 0x40）", dict(status_cfg=0x40)),
                    ("+04 bit1+6=1（cfg 0x42）", dict(status_cfg=0x42)),
                    ("+05 bit6/7=1（cmd 0xC0）", dict(status_cmd=0xC0)),
                    ("+04/+05 全置（0x42/0xC0）",
                     dict(status_cfg=0x42, status_cmd=0xC0))):
        m, r = run(**kw)
        s = snapshot(m, r)
        same = (s["sw"] == s0["sw"] and s["pc"] == s0["pc"]
                and s["tasks"] == s0["tasks"])
        line(tag, s)
        print("      → %s" % ("与基准完全一样" if same else
                              "★变了★ 换手 %d→%d 入口 %d→%d 末PC 0x%X→0x%X"
                              % (s0["sw"], s["sw"], s0["n"], s["n"],
                                 s0["pc"], s["pc"])))
    print()
    m, r = run(extra=[(0x50000000, 0x00000400, "ram-test-50")], ctrl50=False)
    s = snapshot(m, r)
    line("铺 RAM（摘掉设备）", s)
    print("      → %s" % ("与基准完全一样" if (s["sw"] == s0["sw"] and
                                            s["tasks"] == s0["tasks"] and
                                            s["pc"] == s0["pc"]) else
                          "★变了★ 换手 %d→%d 入口 %d→%d"
                          % (s0["sw"], s["sw"], s0["n"], s["n"])))
    print()
    print("  ⚠️ 踩坑记录：`m.bus.io_value[0x50000104] = 0x42` 这种「打桩」对")
    print("     **未映射**地址是无效的 —— io_value 只在 map_io 的读路径里生效，")
    print("     未映射读走的是 `unmapped_reads` 分支、直接返回 0。要打桩必须先")
    print("     把窗口映射成设备或 RAM（本段就是靠 ctrl50 的 status_cfg 做的）。")


# --------------------------------------------------------------------------- E
def sec_e(m):
    print()
    print("=" * 100)
    print("E. 0x40 / 0x6F 是「只写数据汇」的证据")
    print("=" * 100)
    uw = m.bus.unmapped_writes
    for name, lo, hi in (("0x40xxxxxx", 0x40000000, 0x41000000),
                         ("0x6Fxxxxxx", 0x6F000000, 0x70000000)):
        recs = [r for r in uw if lo <= r[0] < hi]
        reads = [a for a in m.bus.unmapped_reads if lo <= a < hi]
        widths = Counter(r[1] for r in recs)
        print("  %s：写 %d 笔、读 %d 个地址（%d 次）"
              % (name, len(recs), len(reads),
                 sum(m.bus.unmapped_reads.get(a, 0) for a in reads)))
        print("      宽度分布 %s" % dict(widths))
        segs = []
        if recs:
            addrs = sorted(r[0] for r in recs)
            start = prev = addrs[0]
            for a in addrs[1:]:
                if a != prev + widths.most_common(1)[0][0]:
                    segs.append((start, prev))
                    start = a
                prev = a
            segs.append((start, prev))
        if segs:
            print("      连续段（%d 段）：" % len(segs))
            for a0, a1 in segs[:8]:
                print("        0x%08X .. 0x%08X  (%d 字节)"
                      % (a0, a1, a1 - a0 + 1))
    print()
    print("  0x6F008080 起那 34 笔 4 字节写的内容（同一写点 0x12780E）：")
    recs = sorted(r for r in uw if 0x6F008080 <= r[0] < 0x6F0080C0)
    vals = [r[2] for r in recs]
    print("    %s" % " ".join("0x%02X" % v for v in vals))
    print("    差分 %s" % " ".join(str(vals[i + 1] - vals[i])
                                  for i in range(len(vals) - 1)))
    print("    → 单调递增、增量递减 ⇒ 8 位**单调曲线表**（gamma/对数类），")
    print("      不是「寄存器位模式」；且该窗口 0 笔读 ⇒ 只写。")
    print()
    print("  0x40xxxxxx 里最密的一段（0x137968 的 16 位拷贝环）：")
    recs = [r for r in uw if r[3] == 0x137968]
    print("    写点 0x137968 共 %d 笔，宽度 %s"
          % (len(recs), dict(Counter(r[1] for r in recs))))
    print("    写值前 16 个：%s" % " ".join("0x%04X" % r[2] for r in
                                          sorted(recs)[:16]))
    rom, base = m.bus.rom, m.bus.rom_base
    print("    写点附近（一步 2 字节的拷贝环）：")
    for t in fr_dis.disasm_addr(rom, 0x137950, 0x137980, base):
        print("      " + t)
    print()
    print("  反事实：把这两块铺成 RAM，控制流会不会变？（变了才说明有读者）")
    s0 = snapshot(*run())
    for tag, spec in (("0x40 铺 RAM", [(0x40000000, 0x00200000, "ram-t-40")]),
                      ("0x6F 铺 RAM", [(0x6F000000, 0x00010000, "ram-t-6f")]),
                      ("两块都铺", [(0x40000000, 0x00200000, "ram-t-40"),
                                    (0x6F000000, 0x00010000, "ram-t-6f")])):
        s = snapshot(*run(extra=spec))
        same = (s["sw"] == s0["sw"] and s["pc"] == s0["pc"]
                and s["tasks"] == s0["tasks"])
        line(tag, s)
        print("      → %s" % ("与基准完全一样（没人读它们）" if same else
                              "★变了★ 说明有读者，要单独查"))


# --------------------------------------------------------------------------- F
def sec_f():
    """全局区 +00/+01 的「写配置 / 读状态」混合语义（V45 收尾新增）。

    这一节是自检回归时挖出来的：+01 的 bit0 若如实回读写入值，boot 的终态就变了。
    """
    from frbox import brealos
    from frbox.dev_ctrl50 import CtrlSerial50

    print()
    print("=" * 100)
    print("F. 全局 +01 的 bit0 是状态位（一次「检测 + 清掉」，不是轮询）")
    print("=" * 100)
    m = machine.create("d300-b")
    m.load()
    m.reset()
    rom, base = m.bus.rom, m.bus.rom_base
    print("  复位后的头两句（写 +00 的就是它）：")
    for t in fr_dis.disasm_addr(rom, 0x40000, 0x4000E, base):
        print("    " + t)
    print("  boot 末尾那次「测位 + 清位」：")
    for t in fr_dis.disasm_addr(rom, 0x40144, 0x40152, base):
        print("    " + t)
    print("  → 0x4014A `btstl #0x1` 测 +01 的 bit0；0x4014C `beq` 跳过；")
    print("    0x4014E `bandl #0xE` 就地清 bit0；两条路在 0x40150 汇合。")

    print()
    print("  反事实（同一口径、只改这一位）：")
    old = machine.D300B.ctrl50_status_rdy
    try:
        for rdy, tag in ((0x00, "+01 bit0 = 0（默认，= V44 未映射）"),
                         (0x01, "+01 bit0 = 1（器件已置起）")):
            machine.D300B.ctrl50_status_rdy = rdy
            b = brealos.boot()
            c = b.cpu
            print("    %-30s 步 %d  PC 0x%X  SP 0x%X  S=%d  ILM 0x%02X  覆盖 %d"
                  % (tag, b.step_count, c.pc, c.r[15], c.s, c.ilm,
                     b.cov.words()))
            if rdy:
                b.step(1)
                print("    %-30s 再走一步 → PC 0x%X  SP 0x%X  ILM 0x%02X"
                      % ("", b.cpu.pc, b.cpu.r[15], b.cpu.ilm))
    finally:
        machine.D300B.ctrl50_status_rdy = old
    print("    ⇒ 置起只多执行一条 `bandl`，路径没岔；固定预算 191,520 步于是")
    print("      落在空闲 bra -1 的前一条（0x407D8），再一步同样到 0x407DC。")
    print("      **所以默认取 0**（与 V44 逐拍一致），要试才显式打开。")

    print()
    print("  逐拍对齐（判「相位差」而不是「岔路」的关键）：三台机同口径跑 ——")
    print("    A = 无设备（V44）   B = 有设备、+01 bit0=0（默认）   C = 有设备、bit0=1")
    print("    判据① B@k 与 A@k 逐拍相同  ⇒ 默认口径与 V44 完全等价")
    print("    判据② C@(k+1) 与 A@k 逐拍相同 ⇒ 置起只是把整条轨迹推后一步")

    def trace(has, rdy):
        machine.D300B.ctrl50_base = 0x50000000 if has else 0
        machine.D300B.ctrl50_status_rdy = rdy
        mk = machine.create("d300-b")
        mk.load()
        mk.reset()
        seq = []
        k = 0
        while not mk.cpu.halted and k < 191525:
            mk.step(1)
            k += 1
            if k >= 191511:
                seq.append((mk.cpu.pc, mk.cpu.r[15], mk.cpu.s, mk.cpu.ilm,
                            mk.bus.rd(blive.DISPATCH_ICR, 1) & 0xFF))
        return seq

    try:
        ta, tb, tc = trace(False, 0), trace(True, 0), trace(True, 1)
    finally:
        machine.D300B.ctrl50_base = 0x50000000
        machine.D300B.ctrl50_status_rdy = 0
    n = min(len(ta), len(tb), len(tc))
    m1 = sum(tb[i] == ta[i] for i in range(n))
    m2 = sum(tc[i + 1] == ta[i] for i in range(n - 1))

    def fmt(st):
        return ("PC 0x%05X SP 0x%08X S=%d ILM 0x%02X ICR 0x%02X"
                % (st[0], st[1], st[2], st[3], st[4]))

    print("        步      A（无设备）                                  "
          "B（默认）                                     C（bit0=1）")
    for i in range(n):
        print("    %6d  %s  %s  %s"
              % (191511 + i, fmt(ta[i]), fmt(tb[i]), fmt(tc[i])))
    print("    → ① B@k == A@k：%d/%d    ② C@(k+1) == A@k：%d/%d"
          % (m1, n, m2, n - 1))
    print("    ⇒ 默认口径与 V44 逐拍等价；置 1 只是整条轨迹推后一步 ——")
    print("      固定预算 191,520 步因此停在空闲 bra -1 的前一条（0x407D8），")
    print("      第 191,521 步到达 0x407DC，SP 0x68000BE8 / S=1 / ILM 0x1F 与 V44 相同。")

    print()
    print("  另一个易错点：+08 被 `orh` 做读改写（0x120CC0），必须可读才能叠位")
    m2 = machine.create("d300-b")
    m2.load()
    m2.reset()
    m2.enable_cov()
    n = 0
    while not m2.cpu.halted and n < 191520:
        m2.step(1)
        n += 1
    brealos.sta_tsk(m2, 2)
    m2.raise_irq(0x3F)
    m2.run(max_steps=400000, spin_guard=False)
    dev = m2.ctrl50
    rom2, base2 = m2.bus.rom, m2.bus.rom_base
    for t in fr_dis.disasm_addr(rom2, 0x120CBE, 0x120CC6, base2):
        print("    " + t)
    print("    +08 最终 = 0x%04X（0x2000 | 0x4000）⇒ 读回 0 会写成 0x4000，是错的"
          % ((dev.regs[0x08] << 8) | dev.regs[0x09]))
    print("    注：这笔读在 access_log 里记成 @PC 0x0 —— `orh` 的 RMW 读走")
    print("        fr_emu.py:503 那条路径，不带 pc 参数（记账噪声，不是固件读了 0 地址）。")


def main():
    m, res = sec_a()
    sec_b()
    sec_c(m)
    sec_d()
    sec_e(m)
    sec_f()
    print()
    print("=" * 100)
    print("结论")
    print("=" * 100)
    print("""  1. 0x5000_0000 是**三通道位串行控制块**（+0x100/+0x200/+0x300，各 16 字节），
     bit5 = CLK、bit3 = DATA、bit6-7 = 状态，+0x06 bit2 = 片选；
  2. 固件在通道 A 上用 16 个时钟发了一个 16 位载荷 **0x2000**（MSB 先）；
     通道 B/C 只做了 boot 配置（各 1 个时钟 = 起始位），没有传输；
  3. 建成器件后：未映射写 1,933 → **1,853**（0x50 那 80 笔全部落进器件），
     未映射读地址 12 → 5、次数 69 → 9，**整机控制流数字一个都没变**
     （换手 34 / 入口 17 / 末 PC 0x407DC / ICR 0x10）⇒ 这是「补对了」的判据；
  4. 0x6Fxxxxxx **一笔读都没有**、0x40xxxxxx 1730 笔写只有 **4 次读**
     （0x400400F8 / 0x400408F8 各 2 次），写进去的是 ROM 表（等差段 / 单调曲线）
     ⇒ 两者都是**只写数据汇**，V44 把它们叫「外设寄存器」不准确（KI#174-b 已改）；
     剩下真正没建模的只有 0x60/0x61（11 写 / 5 读）与 0x5C0020xx（4 写）。
  5. 全局 `+01` 的 bit0 是**状态位**（固件 btstl 测、置起就 bandl 清掉），默认 0
     与 V44 逐拍一致；置 1 只多一条指令 —— 是**1 步相位**不是岔路（KI#178）。
     另一处易错：`+08` 被 `orh` 读改写，必须可读才能叠位（读 0 会写成 0x4000）。""")


if __name__ == "__main__":
    main()
