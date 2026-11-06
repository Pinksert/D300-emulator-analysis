# -*- coding: utf-8 -*-
"""V46 探针：外设读侧收官 —— 0x40 窗口与 0x60/0x61 双通道控制器（KI#179 / #180）。

V45 之后全机还剩 5 个地址 9 次未映射读：
    * 0x60/0x61 窗口 3 个地址 5 次（唯一还"像"能改控制流的一笔）
    * 0x40 窗口 2 个地址 4 次（RMW 的读回）
这一版把两块都建成器件，于是**整机跑下来再没有一笔读落在没建模的地址上**；
未映射写从 1853 降到 112（只剩 0x6F 的 108 与 0x5C0020xx 的 4）。

分段：
    A  建器件前后的总账（未映射写/读、换手、入口、末 PC、覆盖）
    B  0x60/0x61：逐条访问 + 两通道寄存器快照 + 0x8400 描述符表
    C  0x60/0x61 的状态字段是哪几位：256 个值逐个直接调 0x1A36DA
    D  0x40 窗口：按 PC 归组的寄存器实例（偏移 + 值 + 时序参数）
    E  0x40 的 +0xF8：两次读-改-写为什么必须能读回
    F  0x40 的下载口：3082 字节与 ROM 0x1BDBAE 逐字节比对
    G  反事实：状态字段 = 3 会让分支翻转，整机数字却不变（阴性结果）
    H  剩下的 112 笔未映射写在哪

跑法（在 emu/ 下）：
    python probe/exp_dual40.py > ../results/frbox/dual40.txt
"""
import os
import sys
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8')

from collections import Counter, defaultdict

import fr_dis
from frbox import blive, brealos, machine

STEPS = 700000
BASE_EXTRA = list(machine.D300B.extra_ram)
DFLT = dict(ctrl40=0x40000000, dual60=(0x60000000, 0x61000000), status=0)

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_REPO = os.path.dirname(_EMU)
ROM = open(os.path.join(_REPO, "data", "B3000111.bin"), "rb").read()
BASE = 0x40000


def set_scope(ctrl40=None, dual60=None, status=None):
    if ctrl40 is not None:
        machine.D300B.ctrl40_base = ctrl40
    if dual60 is not None:
        machine.D300B.dual60_bases = dual60
    if status is not None:
        machine.D300B.dual60_status = status


def restore():
    machine.D300B.ctrl40_base = DFLT["ctrl40"]
    machine.D300B.dual60_bases = DFLT["dual60"]
    machine.D300B.dual60_status = DFLT["status"]
    machine.D300B.extra_ram = list(BASE_EXTRA)


def run(ctrl40=None, dual60=None, status=None, steps=STEPS, hook=None):
    """建新机 → boot → 跑；不改的参数沿用默认口径，跑完还原。"""
    set_scope(ctrl40, dual60, status)
    try:
        m = blive.boot()
        if hook:
            hook(m)
        res = blive.run(m, steps=steps)
        return m, res
    finally:
        restore()


def line(tag, m, res):
    st = m.bus.stats()
    print("  %-16s 换手 %3d  入口 %2d  末PC 0x%08X  ICR 0x%02X  覆盖 %4d"
          "  未映射写 %4d  读 %d 地址 %2d 次"
          % (tag, len(res["switches"]), len(res["tasks"]), m.cpu.pc,
             m.bus.rd(blive.DISPATCH_ICR, 1) & 0xFF, m.cov.words(),
             st["unmapped_write_times"], st["unmapped_read_addr"],
             st["unmapped_read_times"]))
    return st


# --------------------------------------------------------------------- A
def sec_a():
    print("=" * 78)
    print("A. 建器件前后的总账（boot + 700,000 步）")
    print("=" * 78)
    m0, r0 = run(ctrl40=0, dual60=())
    s0 = line("V45 口径", m0, r0)
    m1, r1 = run()
    s1 = line("V46 默认", m1, r1)
    print()
    print("  ⇒ 未映射写 %d → %d（-%d）；未映射读 %d 地址/%d 次 → %d 地址/%d 次"
          % (s0["unmapped_write_times"], s1["unmapped_write_times"],
             s0["unmapped_write_times"] - s1["unmapped_write_times"],
             s0["unmapped_read_addr"], s0["unmapped_read_times"],
             s1["unmapped_read_addr"], s1["unmapped_read_times"]))
    print("  ⇒ 换手 / 入口 / 末 PC / ICR / 覆盖 五项全部相同 —— 建模不改整机行为")
    print()
    print("  两块新器件的观察口：")
    d = m1.ctrl40
    print("    ctrl40 ：写 %d 字节日志 / 读 %d 次（+0xF8 占 %d 次）"
          % (d.writes, d.reads, d.rmw_reads))
    print("             下载口 %d 笔（%d 字节），最大偏移 +0x%X"
          % (d.stream_writes, len(d.stream_payload()), d.stream_last))
    for i, dev in enumerate(m1.dual60):
        print("    dual60 ch%d @0x%08X：ctrl=0x%02X 状态字节=0x%02X（字段 %d）"
              " 写 %d 读 %d" % (i, dev.base, dev.ctrl(), dev.status,
                                dev.field, dev.writes, dev.reads))
    return m1, r1


# --------------------------------------------------------------------- B
def sec_b():
    print()
    print("=" * 78)
    print("B. 0x60/0x61：逐条访问（boot + 700,000 步；+02 的两次读-改-写叠成 0x82）")
    print("=" * 78)
    m = blive.boot()
    log = []
    orig_rd, orig_wr = m.bus.rd, m.bus.wr
    LO, HI = 0x60000000, 0x62000000

    def rd(a, n, pc=0):
        v = orig_rd(a, n, pc)
        if LO <= a < HI:
            log.append((m.step_count, "R", a, n, v, pc))
        return v

    def wr(a, n, v, pc=0):
        if LO <= a < HI:
            log.append((m.step_count, "W", a, n, v, pc))
        return orig_wr(a, n, v, pc)

    m.bus.rd, m.bus.wr = rd, wr
    try:
        done = 0
        while done < STEPS and not m.cpu.halted:
            k = min(20000, STEPS - done)
            m.run(max_steps=k, spin_guard=False)
            done += k
    finally:
        m.bus.rd, m.bus.wr = orig_rd, orig_wr
        restore()

    for st, kd, a, n, v, pc in log:
        print("  步 %6d  %s  0x%08X  %d字节 = 0x%02X   @PC 0x%X"
              % (st, kd, a, n, v, pc))
    print("  共 %d 条：写 %d / 读 %d" % (len(log),
          sum(1 for r in log if r[1] == "W"), sum(1 for r in log if r[1] == "R")))
    print()

    print("  --- 通道 A 初始化（0x1A36BC–0x1A36D8）---")
    print(m.disasm_at(0x1A36BC, 15))
    print()
    print("  --- 通道 A 查状态（0x1A36DA–0x1A3704）---")
    print(m.disasm_at(0x1A36DA, 17))
    print()
    print("  --- 0x8400 / 0x8450 两张 0x50 字节描述符表（任务 2 在开机装配里填）---")
    for base, tag in ((0x8400, "通道 A"), (0x8450, "通道 B")):
        w = [m.bus.rd(base + i, 4) for i in range(0, 0x28, 4)]
        print("  0x%X %s：%s" % (base, tag, " ".join("%08X" % v for v in w)))
    print("  子窗口指针：+0=基址、+4/+8=+0x1000/+0x2000、+0xC=+0x200、+0x10/+0x14")
    print("  三个数值：+0x18=0xDC0/0xDE0、+0x1C=0x46C/0x46D、+0x20=0x98/0xA0")


# --------------------------------------------------------------------- C
def sec_c():
    print()
    print("=" * 78)
    print("C. 状态字段是哪几位：256 个状态值逐个直接调 0x1A36DA")
    print("=" * 78)
    set_scope()
    try:
        m = blive.boot()
        m.run(max_steps=292100 - m.step_count, spin_guard=False)
        base = {}
        for st in range(256):
            for d in m.dual60:
                d.set_status(byte=st)
            sig = []
            for idx in (0, 1):
                r = brealos.call_fn(m, 0x1A36DA, r4=idx, ret=0x404E0)
                sig.append((r["steps"], m.cpu.r[4]))
            base[st] = tuple(sig)
        ready = sorted(st for st, v in base.items() if v != base[0])
        print("  状态 0x00 时：步数/返回值 = %s（未就绪）" % (base[0],))
        print("  与 0x00 行为不同的状态值共 %d 个：" % len(ready))
        for i in range(0, len(ready), 8):
            print("    " + " ".join("0x%02X" % v for v in ready[i:i + 8]))
        print("  ⇒ 命中的都是 **bit2–3 = 3**（0x0C–0x0F、0x1C–0x1F、…、0xFC–0xFF）")
        print("  ⇒ 那两支 `lsl2 #12` / `lsr2 #14` 是 28/30 位位移（u4+16），")
        print("     合起来就是 (b<<28)>>30 —— **取 bit2–3**，再 `cmp #3`。")
        print("     一开始按 bit1 判是错的（见 KI#181）。")
        for st in (0x00, 0x04, 0x0C):
            for d in m.dual60:
                d.set_status(byte=st)
            outs = []
            for idx in (0, 1):
                r = brealos.call_fn(m, 0x1A36DA, r4=idx, ret=0x404E0)
                outs.append("idx=%d → %d 步 r4=0x%X" % (idx, r["steps"], m.cpu.r[4]))
            print("    状态 0x%02X：%s" % (st, "；".join(outs)))
        print()
        print("  调用点（8 处）都长这个样子：")
        print(m.disasm_at(0x15CB7C, 4))
        print("  ↑ `ldi:8 r4, 0` 把返回值清掉了 —— 所以「读到什么」进不了分支（G 段）。")
    finally:
        restore()


# --------------------------------------------------------------------- D
def sec_d():
    print()
    print("=" * 78)
    print("D. 0x40 窗口：按写 PC 归组的寄存器实例")
    print("=" * 78)
    m = blive.boot()
    rec = defaultdict(list)
    orig_wr = m.bus.wr
    LO, HI = 0x40000000, 0x41000000

    def wr(a, n, v, pc=0):
        if LO <= a < HI:
            rec[pc].append((a - 0x40000000, n, v))
        return orig_wr(a, n, v, pc)

    m.bus.wr = wr
    try:
        done = 0
        while done < STEPS and not m.cpu.halted:
            k = min(20000, STEPS - done)
            m.run(max_steps=k, spin_guard=False)
            done += k
    finally:
        m.bus.wr = orig_wr
        restore()

    print("  写过 0x40 窗口的 PC 共 %d 个，写 %d 笔"
          % (len(rec), sum(len(v) for v in rec.values())))
    print()
    for pc in sorted(rec, key=lambda p: -len(rec[p])):
        rs = rec[pc]
        offs = sorted({o for o, _n, _v in rs})
        if len(rs) > 20:
            print("  @PC 0x%-7X %4d 笔  offset +0x%05X..+0x%05X（%d 个不同偏移）"
                  % (pc, len(rs), min(offs), max(offs), len(offs)))
            for o, v in [(o, v) for o, n, v in rs[:4]]:
                print("       +0x%05X ← 0x%X" % (o, v))
            print("       …")
        else:
            print("  @PC 0x%-7X %4d 笔：%s" % (pc, len(rs), " ".join(
                "+%X=0x%X" % (o, v) for o, _n, v in rs)))
    print()
    print("  --- 三段寄存器块的形状（按偏移归组，不按「实例」切 —— 它们互相嵌套）---")
    print("    +0x002 / +0x010..+0x026：单值参数（0x0E、0x80、0x280、0x1E0、0x140）")
    print("    +0x0C0..+0x0EE：一段 16 个半字全写 0xFFFF（0x400D0..+0x400EE 与")
    print("                   +0x408D0..+0x408EE 各一份）")
    print("    +0x100..+0x11E：另一组单值参数（0x8D、0x10、0x808、0x1E0、0x280）")
    print("    +0x400 / +0x40C / +0xC00：14 个 32 位系数（0x7008080 0x7FF8080")
    print("                   0x74D55FF 0x7952B15 0x71DFF6B 0x7DA8080 …）")
    print("    +0xD004 / +0x1C0000：孤立的两个门控（0x8000 / 0x0）")
    print()
    print("  --- 0x137968 的 16 位拷贝环（1541 笔写，源 ROM 0x1BDBAE）---")
    print(m.disasm_at(0x137956, 10))


# --------------------------------------------------------------------- E
def sec_e():
    print()
    print("=" * 78)
    print("E. 0x40 的 +0xF8：两次读-改-写为什么必须能读回")
    print("=" * 78)
    m = blive.boot()
    print(m.disasm_at(0x14C1B6, 13))
    print()
    print("  第一次：r1 = [base+0xF8] → 清 bit8–13 → 置 0x3B00 → 写回")
    print("  第二次：r1 = [base+0xF8] → 清 bit0–5  → 置 0x003B → 写回")
    print("  ⇒ 回读正确时终值 0x3B3B；V44 口径（读恒 0）会把第二次写成 **0x003B**")
    print()
    m2, _ = run()
    d = m2.ctrl40
    for inst in (0x40000, 0x40800, 0x40900):
        print("  实例 +0x%05X 的 +0xF8 = 0x%04X" % (inst, d.rmw_value(inst)))
    print("  （+0x40000 与 +0x40800 两个实例拿到的是不同的值 —— 说明它们不是同一份配置）")


# --------------------------------------------------------------------- F
def sec_f():
    print()
    print("=" * 78)
    print("F. 0x40 的下载口：6 张表、1541 项，逐字节都能在 ROM 里找到")
    print("=" * 78)
    m = blive.boot()
    rec = []
    orig_wr = m.bus.wr

    def wr(a, n, v, pc=0):
        if pc == 0x137968 and 0x40000000 <= a < 0x41000000:
            rec.append((a - 0x40000000, v, m.cpu.r[4]))
        return orig_wr(a, n, v, pc)

    m.bus.wr = wr
    try:
        done = 0
        while done < STEPS and not m.cpu.halted:
            k = min(20000, STEPS - done)
            m.run(max_steps=k, spin_guard=False)
            done += k
    finally:
        m.bus.wr = orig_wr
        restore()

    print("  拷贝环 @PC 0x137968：%d 笔写，源地址 %d 处不连续"
          % (len(rec), sum(1 for k in range(1, len(rec))
                           if rec[k][2] != rec[k - 1][2] + 2) + 1))
    blocks, cur = [], [rec[0]]
    for x, p in zip(rec[1:], rec):
        if x[2] != p[2] + 2:
            blocks.append(cur)
            cur = []
        cur.append(x)
    blocks.append(cur)

    print()
    print("  表  目标偏移            项数   源（ROM）           与 ROM 一致   首 4 值")
    for i, b in enumerate(blocks):
        d0, d1 = b[0][0], b[-1][0]
        s0 = b[0][2]
        vals = [v for _d, v, _s in b]
        ok = all(int.from_bytes(ROM[s0 - BASE + 2 * k:s0 - BASE + 2 * k + 2], "big")
                 == vals[k] for k in range(len(vals)))
        print("   %d  +0x%05X..+0x%05X  %3d   0x%X       %s        %s"
              % (i, d0, d1, len(b), s0, "是" if ok else "★否",
                 " ".join("0x%X" % v for v in vals[:4])))
    print()
    print("  ⇒ 6 张表**全部**逐字节等于 ROM 对应段（0x1BD5A8–0x1BDDAC 这一片）；")
    print("     表 3/4/5 是同一条曲线写了三份到三个基址（+0x11000/+0x11400/+0x11800）。")
    print("  ⇒ 形状（逐项相邻差）：表 0 = 纯斜坡（差恒 0x40，到 0x3FC0）；")
    print("     表 1 = 斜坡起步（前 43 项差 0x100）→ 弯曲 → 第 64 项起饱和在 0x3FFF；")
    print("     表 2 = 增量递减（凹，4096→1696→1296→1104…，末 0xFFF0）；")
    print("     表 3/4/5 = 增量递增（凸，起步差 0/0/16/0/16…，末 0xFFE0）。")
    print("  ⇒ 窗口 +0x10200..+0x11A01 共 6146 字节，实际写进 3082 字节（50%）——")
    print("     不是整窗填满，是 6 段表各占 0x200。")
    print("  ⇒ `+0x10000 ← 0x0800` 只是记下的值，含义不明，不解释。")


def sec_g():
    print()
    print("=" * 78)
    print("G. 反事实：状态字段 = 3 让分支翻转，整机数字却不变（阴性结果）")
    print("=" * 78)
    print("  三档：状态字节 0x00（字段 0）/ 0x0C（字段 3）/ 0xFF（字段 3 + 其余位全 1）")
    for st in (0x00, 0x0C, 0xFF):
        m, r = run(status=st)
        line("状态 0x%02X" % st, m, r)
    print()
    print("  ⇒ 覆盖从 **5404 → 5421**（+17 个指令字）= 「就绪」那条出口真的被走过；")
    print("     但换手 / 入口 / 末 PC 一个都没变。")
    print("  ⇒ 原因：8 个调用点都紧接着 `ldi:8 r4, 0`（C 段），把返回值清掉了。")
    print("     **这一位对整机控制流是中性**，V44 的「读恒 0」在这个窗口上是无害的。")
    print("  ⇒ 那为什么还要建模？因为它决定了函数走哪条出口、返回什么；")
    print("     不建的话这 5 次读永远是「未映射」，审计表里就永远有个说不清的洞。")


# --------------------------------------------------------------------- H
def sec_h():
    print()
    print("=" * 78)
    print("H. 剩下的 112 笔未映射写在哪")
    print("=" * 78)
    m, _ = run()
    addrs = Counter()
    for a, s, v, pc in m.bus.unmapped_writes:
        addrs[a] += 1
    hi = Counter()
    for a, c in addrs.items():
        hi[a >> 24] += c
    for k in sorted(hi):
        print("  0x%02X000000：%d 笔" % (k, hi[k]))
    print()
    print("  --- 0x6F 的最大PC（108 笔里的主要来源）---")
    pcs = Counter(pc for a, s, v, pc in m.bus.unmapped_writes if a >> 24 == 0x6F)
    for pc, c in pcs.most_common(6):
        print("    @PC 0x%-7X %d 笔" % (pc, c))
    print("  --- 0x5C0020xx 的 4 笔 ---")
    for a, s, v, pc in m.bus.unmapped_writes:
        if a >> 16 == 0x5C00:
            print("    0x%08X ← 0x%X（%d 字节）@PC 0x%X" % (a, v, s, pc))
    print()
    print("  ⇒ 两个窗口都是**只写、0 读**，铺 RAM 的反事实也证明没人读")
    print("     （V44 的 E 段）；证据不足以说清它们的寄存器布局，故意不建模。")


def main():
    sec_a()
    sec_b()
    sec_c()
    sec_d()
    sec_e()
    sec_f()
    sec_g()
    sec_h()


main()
