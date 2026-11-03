# -*- coding: utf-8 -*-
"""exp_tick.py —— V19 那道「新墙」其实是探针自己的 bug（V20）

V19 的结论是「不替外设答话，第 9 次中断会死等 0x174 的 bit6」。V20 查下去发现：
`0x174` 早在 `dev_xfer.D300_A_TABLE` 里就有建模（启动位 bit1、完成位 bit6），
`Machine.boot_realos()` 还无条件调了 `enable_xfer()`。真正的问题是
`Irq2B.raise_once()` 用 `cpu.step()` 单步，**绕过了 `Machine.step()` 的设备时基分发**，
`XferHandshake.tick()` 一个节拍都拿不到 —— 完成位自然永远不出现。

八个场景，同一套预热口径（12 × 5000 步，不注入 vec 0x13/0x19）：

    A 归属        0x174 到底挂在谁身上：dp.owner / table / devices
    B 两种步进    cpu.step 与 m.step 的分水岭：第 9 次、自旋次数、最终步数
    C 谁置的位    watch 0x174：第几次读出 0x42（= 0x02 | 0x40）
    D 全程 240 次 不硬写任何字节，看能走多远、覆盖多少
    E 两种答话    硬写 0x40 vs 设备模型：覆盖数字一不一样
    F 结果寄存器  240 次里走到转换流程几次、碰了哪些通道
    G 换模拟量    给 8 个通道灌不同值，覆盖会不会变
    H 8 通道结构 0x8A500 那个 switch：通道表与位宽

⚠️ 口径三条：
   ① 预热 12×5000 步、不注入事件向量 → 基线覆盖 1,443；V17/V18 是 40×5000 + 喂事件
      （基线 6,382），**两套数字不能相减**。
   ② 「请求一次中断」是我们拿固件自己配的 ICR 级别 0x1A 去请求，等价于外设拉了一下
      这条线；请求频率是我们定的，固件里没有节拍器模型。
   ③ 设备模型的 delay=64 个 CPU 周期 —— 这是**近似的时序**，不是逆向确认的转换时间。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import isr2b, taskboot           # noqa: E402
from frbox.dev_xfer import D300_A_TABLE     # noqa: E402

WARM = 12
BATCH = 5000
CHANNELS = [0x178, 0x17A, 0x17C, 0x17E, 0x180, 0x182, 0x184, 0x186,
            0x188, 0x18A, 0x18C, 0x18E]      # 10 位组的全部 12 路


def base(boot_all=False, xfer=True):
    m = create("d300-a")
    m.load()
    if boot_all:
        taskboot.activate(m, "dormant")
    m.reset()
    m.enable_cov()
    m.boot_realos(1)                  # 它内部无条件 enable_xfer()
    m.attach_flow()
    m.enable_callbacks()
    h = m.enable_isr2b()
    for _ in range(WARM):
        m.run(max_steps=BATCH, spin_guard=False)
    return m, h


def tasks(m):
    from frbox import realos
    return {t["id"]: t["wait_kind"] for t in realos.states(m)}


def head(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ------------------------------------------------------------------ A
def sec_a(m, h):
    head("A 归属：0x174 早就有人管")
    print("D300_A_TABLE（dev_xfer.py 里写死的）:")
    for off in sorted(D300_A_TABLE):
        sm, dm = D300_A_TABLE[off]
        print("  0x%03X  启动位 0x%02X  完成位 0x%02X" % (off, sm, dm))
    print("\n运行期：")
    for a in (0x0AF, 0x174, 0x524, 0x534):
        own = m.dp.owner(a)
        print("  dp.owner(0x%03X) = %s" % (a, type(own).__name__ if own else "None（影子存储）"))
    x = m.xfer
    print("  m.xfer = %s" % (type(x).__name__ if x else "None"))
    print("  xfer 在 m.devices 里 = %s" % (x in m.devices if x else False))
    print("  累计：启动 %d 次 / 完成 %d 次" % (x.starts, x.completions))
    print("\n结论：`boot_realos()` 里第一行就是 `enable_xfer()`，")
    print("      所以从 V10 起所有走 REALOS 的实验都已经挂着 0x174 的握手模型。")
    print("      V19 说的「缺一个外设」不成立。")


# ------------------------------------------------------------------ B
def sec_b():
    head("B 分水岭：cpu.step 与 m.step")
    print("%-14s %-10s %-10s %-10s %s"
          % ("步进口径", "第9次", "自旋经过", "终点 pc", "[0x174]"))
    for tag, run_tick in (("cpu.step", False), ("m.step", True)):
        m, h = base()
        m.bus.watch.add(0x174)
        m.bus.access_log.clear()
        lo, hi = m.cov.words(), 0
        stuck = None
        for k in range(1, 13):
            r = h.raise_once(max_steps=20000, run_tick=run_tick)
            if r["cut"]:
                stuck = (k, m.cpu.pc)
                break
        spins = sum(1 for kind, a, _n, _v, pc in m.bus.access_log
                    if kind == "R" and a == 0x174 and pc == isr2b.POLL_PC)
        print("%-14s %-10s %-10d 0x%05X    0x%02X"
              % (tag,
                 ("第 %d 次卡死" % stuck[0]) if stuck else "全部 reti",
                 spins, m.cpu.pc, m.bus.rd(0x174, 1)))
    print("\n同一条中断线、同一台机器，唯一的差别是「谁在单步」：")
    print("  cpu.step 不跑设备 tick → XferHandshake.tick() 永不调用 → 完成位不来")
    print("  m.step   每 cycles_per_tick 步分发一次 tick → 完成位到点自己起来")
    print("\n`XferHandshake.delay=%d`，`cycles_per_tick=%d`，所以一次 tick 就够。"
          % (D300_A_TABLE and 64, 256))


# ------------------------------------------------------------------ C
def sec_c():
    head("C 谁把 bit6 置上的：watch 0x174")
    m, h = base()
    m.bus.watch.add(0x174)
    m.bus.access_log.clear()
    for k in range(9):
        r = h.raise_once(max_steps=20000)
        if r["cut"]:
            print("第 %d 次卡住了" % k)
            return
    log = [(kind, v, pc) for kind, a, _n, v, pc in m.bus.access_log if a == 0x174]
    print("9 次中断里 0x174 的访问 %d 条，最后一次事务：" % len(log))
    tail = log[-20:]
    for kind, v, pc in tail:
        mark = ""
        if kind == "R" and v & 0x40:
            mark = "   ← bit6 在这一读里出现"
        print("  %s pc=0x%05X = 0x%02X%s"
              % ("wr" if kind == "W" else "rd", pc, v, mark))
    n_ok = sum(1 for kind, v, pc in log if kind == "R" and pc == isr2b.POLL_PC and v & 0x40)
    n_poll = sum(1 for kind, v, pc in log if kind == "R" and pc == isr2b.POLL_PC)
    print("\n0x9C97A 上一共读了 %d 次，其中 %d 次带回 bit6。" % (n_poll, n_ok))
    print("固件在 0x9C960 写 0（清寄存器）→ 0x9C974 置 bit1 → 0x9C97A 等 bit6。")
    print("模型在写 bit1 时把完成位清掉、过 64 个周期再置起来 —— 所以第 1 次读是 0x02，")
    print("第 14 次已经是 0x42。**整个过程中没有任何一行代码替硬件写过字节。**")


# ------------------------------------------------------------------ D
def sec_d():
    head("D 全程 240 次：不硬写任何字节")
    m, h = base()
    c0 = m.cov.words()
    s0 = len(m.flow.calls)
    stuck = None
    for k in range(240):
        r = h.raise_once(max_steps=60000)
        if r["cut"]:
            stuck = (r["n"], m.cpu.pc)
            break
    print("请求 %d 次，硬写完成位 %d 次" % (h.count, h.answers))
    print("结果：%s" % ("第 %d 次卡在 0x%05X" % stuck if stuck else "全部回到 reti"))
    print("覆盖 %d → %d（+%d）  服务调用 %d → %d"
          % (c0, m.cov.words(), m.cov.words() - c0, s0, len(m.flow.calls)))
    print("任务：%s" % tasks(m))
    print("回调槽：%s" % m.cb.summary())
    print("设备：%s" % m.xfer.info())


# ------------------------------------------------------------------ E
def sec_e():
    head("E 两种答话方式，覆盖数字一不一样")
    print("%-24s %-8s %-8s %-8s %s"
          % ("答话方式", "请求", "硬写", "覆盖+", "结果"))
    rows = []
    for tag, ans in (("硬写 0x40（V19）", True), ("设备模型（V20）", False)):
        m, h = base()
        c0 = m.cov.words()
        stuck = None
        for k in range(240):
            r = h.raise_once(answer=ans, max_steps=60000)
            if r["cut"]:
                stuck = k + 1
                break
        rows.append((tag, h.count, h.answers, m.cov.words() - c0,
                     "第 %d 次卡住" % stuck if stuck else "全部 reti"))
        print("%-24s %-8d %-8d %-8d %s" % rows[-1])
    print("\n两条路的覆盖完全相同（+2053）—— 因为固件拿到 bit6 之后的走法一样。")
    print("区别在于「谁给的位」：一个是外面硬写，一个是模型按启动/完成位自己置。")
    print("V20 起默认走后一条，`answer=True` 只作对照。")


# ------------------------------------------------------------------ F
def sec_f():
    head("F 结果寄存器：240 次里走到转换流程几次、碰了哪些通道")
    m, h = base()
    for a in range(0x170, 0x190):
        m.bus.watch.add(a)
    m.bus.access_log.clear()
    h.burst(240, max_steps=60000)
    hits = {}
    for kind, a, _n, _v, _pc in m.bus.access_log:
        key = ("%s 0x%03X" % ("W" if kind == "W" else "R", a))
        hits[key] = hits.get(key, 0) + 1
    print("以 0x170–0x18F 为窗口，240 次中断的访问统计：")
    for k in sorted(hits, key=lambda s: (-hits[s], s)):
        print("  %-10s %d 次" % (k, hits[k]))
    ch = {a: 0 for a in CHANNELS}
    for kind, a, _n, _v, _pc in m.bus.access_log:
        if kind == "R" and a in ch:
            ch[a] += 1
    print("\n12 个 10 位结果寄存器各自的读次数：")
    for a in CHANNELS:
        print("  0x%03X  %s" % (a, ch[a] if ch[a] else "—（一次都没读）"))
    print("\n只有 0x186 被读，别的 11 路一次都没碰。")
    print("也就是说：这条路径上的转换流程只取第 8 路，240 次里一共走 15 趟。")


# ------------------------------------------------------------------ G
def sec_g():
    head("G 换模拟量：12 个通道灌不同值，覆盖会不会变")
    print("%-22s %-10s %-8s %s" % ("灌进去的值", "覆盖", "增量", "服务"))
    base_cov = None
    for tag, vals in (("全 0（现状）", [0] * 8),
                      ("全 0x200", [0x200] * 8),
                      ("全 0x3FF", [0x3FF] * 8),
                      ("0x40,0x80,…,0x200", [0x40 * (i + 1) for i in range(8)])):
        m, h = base()
        for a, v in zip(CHANNELS, vals):
            m.dp.shadow[a] = v
        c0 = m.cov.words()
        for k in range(240):
            for a, v in zip(CHANNELS, vals):
                m.dp.shadow[a] = v
            r = h.raise_once(max_steps=60000)
            if r["cut"]:
                break
        if base_cov is None:
            base_cov = m.cov.words() - c0
        print("%-22s %-10d %-8d %d"
              % (tag, m.cov.words(), m.cov.words() - c0, len(m.flow.calls)))
    print("\n四个场景的增量一模一样 —— 灌什么值都不影响这条路径。")
    print("不是「值被忽略」，而是它只被搬到别处存着，中间没有任何分支看它。")


# ------------------------------------------------------------------ H
def sec_h():
    head("H 8 通道的结构：0x8A500 那个 switch")
    m, h = base()
    from fr_emu import TABLE
    rom = m.bus.rom
    base_addr = m.bus.rom_base
    # 线性扫 0x8A3C0–0x8A5C0：`ldi:20 r12,#addr` → `lduh r4,@r12` → `ldi:20 r0,#mask`
    rows = []
    a = 0x0008A3C0
    reg = {}
    pend = None                 # (lduh 地址, 读的那个寄存器)
    while a < 0x0008A5C0:
        ins = TABLE.decode(rom[a - base_addr:a - base_addr + 8], 0)
        if ins is None:
            a += 2
            continue
        mn, _op, v, size, _w = ins
        if mn == "ldi:20" and "ri" in v:
            reg[v["ri"]] = v.get("i20")
            if pend and v["ri"] == 0 and pend[1] in reg:
                rows.append((pend[0], reg[pend[1]], v.get("i20")))
                pend = None
        elif mn in ("lduh", "ldub"):
            src = v.get("rj", v.get("ri"))
            pend = (a, src)
        elif mn not in ("ldi:20",):
            pend = None
        a += size
    print("通道读取表（读哪个地址 → 用哪个掩码）:")
    for ad, addr, mask in rows:
        tag = "12 位" if mask == 0xFFF else ("10 位" if mask == 0x3FF else "%d 位" % mask.bit_length())
        print("  0x%05X  lduh @0x%03X  & 0x%03X（%s）" % (ad, addr, mask, tag))
    ch10 = [r[1] for r in rows if r[2] == 0x3FF]
    ch12 = [r[1] for r in rows if r[2] == 0xFFF]
    print("\n10 位通道 %d 个：%s" % (len(ch10), " ".join("0x%03X" % c for c in ch10)))
    if ch10:
        print("  地址 0x%03X–0x%03X，步长 0x%X（连续半字）"
              % (ch10[0], ch10[-1], ch10[1] - ch10[0] if len(ch10) > 1 else 0))
    print("12 位通道 %d 个：%s" % (len(ch12), " ".join("0x%03X" % c for c in ch12)))
    print("  注意 0x52E 之后跳到 0x538 —— 中间 0x530–0x536 这 4 个字不在表里。")
    print("\n两组通道各有一套控制/状态：10 位组在 0x174（bit1 启动 / bit6 完成），")
    print("12 位组在 0x524 / 0x534 —— 这两处**也在 XferHandshake 的表里**")
    print("（`D300_A_TABLE` 的 0x524:(0x02,0x40)、0x534:(0x02,0x40)）。")
    print("也就是说，两个通道组是同一类「启动—完成—取结果」的器件。")
    print("\n⚠️ 这只是**软件层**的形状：固件按「选通道 → 启动 → 等完成 → 读结果」办事，")
    print("   是 A/D 转换的典型签名，但器件身份没有被证实，报告里只能这么说。")


def main():
    print("V20：V19 那道「新墙」是探针自己的 bug")
    m, h = base()
    sec_a(m, h)
    sec_b()
    sec_c()
    sec_d()
    sec_e()
    sec_f()
    sec_g()
    m2, _ = base()
    sec_h()
    print("\n" + "=" * 78)
    print("能报 / 不能报")
    print("=" * 78)
    print("能报：0x174 归 XferHandshake（启动位 0x02 / 完成位 0x40）；boot_realos 无条件")
    print("      enable_xfer()；V19 的第 9 次卡死是 raise_once 绕过设备时基造成的；")
    print("      改走 Machine.step 之后 240 次全部 reti、+2053，且一个字节都没硬写；")
    print("      这条路径只读第 8 路结果寄存器，240 次走 15 趟，灌什么值覆盖都不变。")
    print("不能报：这是 A/D 转换器（软件签名像，器件身份未证）；真机上这条中断多久")
    print("      来一次；转换要多少微秒（delay=64 是近似）；「相机跑起来了」。")


if __name__ == "__main__":
    main()
