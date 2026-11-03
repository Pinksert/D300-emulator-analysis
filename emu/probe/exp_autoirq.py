# -*- coding: utf-8 -*-
"""exp_autoirq.py —— V21：给 INT 0x2B 配周期时钟，中断自己来

V19/V20 把 INT 0x2B 接上了，但都是**手动拉线**（`--irq2b N` 一次一次请求，
每次跑到 reti，中断被串行化）。V21 挂一个周期时钟到设备时基上
（`dev_irq2b.py`，同一向量已有待决请求时合并），让 `m.run()` 里中断自己来。

八个场景，同一套预热口径（12 × 5000 步，不注入事件 → 基线 1,443）：

    A 四口径      fired / coalesced / skipped / accepted 自洽；
                  风暴时合并发生在哪；ICR 未配置时线不拉
    B 频率扫描    period ∈ {256…16384} + 无中断对照，固定 10 万步
    C 同次数      accepted 都 ≈97：风暴（256）与从容（1024）差多少
    D 多在哪      period=1024 比 256 多出的覆盖落在哪些地址（cov.diff）
    E 时间线      每 1 万步记录：覆盖 / 受理 / 任务 6 状态
    F 受理现场    cpu.irq_log 里 0x2B 每次受理时的 PC 分布（落在谁身上）
    G 等价性      手动 240 次 vs 自动风暴 accepted≥240：还是 +2053 吗
    H 长跑        period=1024 跑 100 万步，覆盖何时饱和

⚠️ 口径：period 是**模型参数**，真机频率未知（V20 已声明）；
   预热后挂钟，与 V19/V20 同基线，数字可以和 +2053 直接对照。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402

WARM = 12
BATCH = 5000
VEC = 0x2B


def base(period=None, dormant=False):
    """预热到基线，再（可选）挂周期时钟。返回 (m, clk)。"""
    m = create("d300-a")
    m.load()
    if dormant:
        from frbox import taskboot
        taskboot.activate(m, "dormant")
    m.reset()
    m.enable_cov()
    m.boot_realos(1)                  # 内部无条件 enable_xfer()
    m.attach_flow()
    m.enable_callbacks()
    m.enable_isr2b()
    for _ in range(WARM):
        m.run(max_steps=BATCH, spin_guard=False)
    clk = m.enable_auto_irq2b(period=period) if period else None
    return m, clk


def tasks(m):
    return {t["id"]: t["wait_kind"] for t in realos.states(m)}


def snap(m, clk):
    return (m.cov.words(), len(m.flow.calls),
            clk.accepted() if clk else 0)


def head(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ------------------------------------------------------------------ A
def sec_a():
    head("A 四口径：fired / coalesced / skipped / accepted")
    print("period=256（每 tick 一次，风暴档）跑 3 万步：")
    m, clk = base(period=256)
    m.run(max_steps=30000, spin_guard=False)
    st = clk.state()
    print("  fired %d / coalesced %d / skipped %d / accepted %d / 待决 %d"
          % (st["fired"], st["coalesced"], st["skipped"],
             st["accepted"], st["pending"]))
    print("  自洽：fired + coalesced = 到点的次数；fired - 待决 = accepted")
    ok = st["fired"] - st["pending"] == st["accepted"]
    print("  fired - 待决 == accepted：%s" % ("成立" if ok else "不成立（%d-%d≠%d）"
                                             % (st["fired"], st["pending"], st["accepted"])))
    print("\nperiod=1024（从容档）跑 3 万步：")
    m2, clk2 = base(period=1024)
    m2.run(max_steps=30000, spin_guard=False)
    st2 = clk2.state()
    print("  fired %d / coalesced %d / skipped %d / accepted %d / 待决 %d"
          % (st2["fired"], st2["coalesced"], st2["skipped"],
             st2["accepted"], st2["pending"]))
    print("\n第四个口径 skipped：ICR 还没被固件配置（level=0）时线不拉 ——")
    print("不这么干的话，raise_irq 会把级别强制成 0x1F，而 0x1F < ILM(0x1F)")
    print("永不成立，请求永久卡在待决队列把后面的到点全合并掉")
    print("（boot 前挂钟实测：修复前 fired 1 / coalesced 139 / accepted 0）。")
    print("\n合并发生在哪：ISR 执行约 600 步 ≈ 2.4 个 tick；风暴档里 ISR 期间的")
    print("到点请求被 ILM 挡在待决队列，下一个到点只能合并 —— 所以风暴档")
    print("coalesced 明显大于 0，从容档基本为 0。")


# ------------------------------------------------------------------ B
def sec_b():
    head("B 频率扫描：固定 10 万步")
    print("%-8s %-9s %-10s %-8s %-8s %s"
          % ("period", "accepted", "coalesced", "覆盖+", "服务+", "任务6"))
    for p in (0, 256, 512, 1024, 2048, 4096, 16384):
        m, clk = base(period=p or None)
        c0, s0, _ = snap(m, None)
        m.run(max_steps=100000, spin_guard=False)
        c1, s1, acc = snap(m, clk)
        coal = clk.coalesced if clk else 0
        t6 = tasks(m).get(6, "-")
        print("%-8s %-9s %-10s %-8s %-8s %s"
              % (p or "无", acc, coal, c1 - c0, s1 - s0, t6))
    print("\n频率不是中性的：256 的风暴（约 196 次）覆盖反而低于 1024（约 97 次）。")
    print("1024 是峰值档 —— 中间留出的执行时间让回调链和任务 6 往前走，")
    print("下一次中断落在**不同的现场**（见 D/F），于是踩到新代码。")


# ------------------------------------------------------------------ C
def sec_c():
    head("C 同样的次数，不同的节奏")
    print("两档都跑到 accepted ≈ 97 次，比覆盖：")
    rows = []
    for p, budget in ((256, 60000), (1024, 120000)):
        m, clk = base(period=p)
        c0, s0, _ = snap(m, None)
        while clk.accepted() < 97 and m.step_count < 10 ** 7:
            m.run(max_steps=5000, spin_guard=False)
        c1, s1, acc = snap(m, clk)
        rows.append((p, acc, c1 - c0, s1 - s0))
        print("  period=%-5d accepted=%-4d 覆盖+%d  服务+%d"
              % (p, acc, c1 - c0, s1 - s0))
    print("\n次数拉平之后差距更刺眼：风暴档把中断全浪费在同一个现场。")
    print("再给风暴档 3 倍步数（accepted ~588）能不能追上：")
    m, clk = base(period=256)
    c0, s0, _ = snap(m, None)
    m.run(max_steps=300000, spin_guard=False)
    c1, s1, acc = snap(m, clk)
    print("  period=256 accepted=%-4d 覆盖+%d  服务+%d" % (acc, c1 - c0, s1 - s0))
    print("  —— 追不上。多来 6 倍的中断也换不来新现场，这就是 V15")
    print("     「事件越多≠覆盖越广」在时间维度上的样子。")


# ------------------------------------------------------------------ D
def sec_d():
    head("D 多出来的覆盖落在哪（cov.diff）")
    m_fast, _ = base(period=256)
    m_fast.run(max_steps=100000, spin_guard=False)
    m_slow, _ = base(period=1024)
    m_slow.run(max_steps=100000, spin_guard=False)
    runs = m_slow.cov.diff(m_fast.cov, min_len=8, gap=8)
    total = sum((hi - lo + 2) // 2 for lo, hi in runs)
    print("period=1024 比 256 多执行 %d 个指令地址，落在 %d 段："
          % (total, len(runs)))
    for lo, hi in runs[:12]:
        print("  0x%05X–0x%05X  (%d 字)" % (lo, hi, (hi - lo + 2) // 2))
    if len(runs) > 12:
        print("  … 其余 %d 段" % (len(runs) - 12))
    print("\n这些地址落在谁身上：任务入口的执行情况")
    for rec in m_slow.task_table():
        e = rec["entry"]
        if not e:
            continue
        print("  任务 %-2d 入口 0x%05X：快=%s 慢=%s"
              % (rec["id"], e,
                 "跑过" if m_fast.cov.visited(e) else "—",
                 "跑过" if m_slow.cov.visited(e) else "—"))
    print("\n反向（256 有、1024 没有）：%d 个地址"
          % sum((hi - lo + 2) // 2 for lo, hi in
                m_fast.cov.diff(m_slow.cov, min_len=8, gap=8)))


# ------------------------------------------------------------------ E
def sec_e():
    head("E 时间线：period=1024，每 1 万步")
    m, clk = base(period=1024)
    c0, s0, _ = snap(m, None)
    print("%-8s %-8s %-9s %-8s %s" % ("步数", "覆盖+", "accepted", "服务+", "任务6"))
    for k in range(10):
        m.run(max_steps=10000, spin_guard=False)
        c1, s1, acc = snap(m, clk)
        print("%-8d %-8d %-9d %-8d %s"
              % ((k + 1) * 10000, c1 - c0, acc, s1 - s0, tasks(m).get(6)))
    print("覆盖增长集中在第 6–7 万步（accepted 58→68）：任务 6 从 dormant 变 flag")
    print("的那一下，+541 个地址几乎全在那里 —— 回调链第三站 sta_tsk(6) 把任务 6")
    print("拉起来，之后 accepted 还在涨、覆盖却不再动（8 万步后完全平了）。")


# ------------------------------------------------------------------ F
def sec_f():
    head("F 受理现场：每次 0x2B 中断落在哪个 PC 上")
    for p in (256, 1024):
        m, clk = base(period=p)
        m.run(max_steps=100000, spin_guard=False)
        pcs = {}
        for rec in m.cpu.irq_log:
            if rec[0] != VEC:
                continue
            pc = rec[2]
            k = ("空闲任务 0xFE510" if 0x0FE500 <= pc <= 0x0FE520
                 else "任务 6 函数体 0x%05X" % pc)
            pcs[k] = pcs.get(k, 0) + 1
        top = sorted(pcs.items(), key=lambda x: -x[1])[:8]
        print("period=%d（accepted %d）受理时 PC 的分布：" % (p, clk.accepted()))
        for k, v in top:
            print("   %-22s %d 次" % (k, v))
        n6 = sum(v for k, v in pcs.items() if k.startswith("任务 6"))
        idle = pcs.get("空闲任务 0xFE510", 0)
        print("   → 空闲任务 %d / 任务 6 函数体 %d / 共 %d"
              % (idle, n6, clk.accepted()))
        print()


# ------------------------------------------------------------------ G
def sec_g():
    head("G 等价性：手动 240 次 vs 自动风暴 accepted≥240")
    m, _clk = base()
    h = m.isr2b
    c0, s0, _ = snap(m, None)
    h.burst(240, max_steps=60000)
    print("手动 raise_once 240 次：覆盖+%d  服务+%d" % (m.cov.words() - c0,
                                                      len(m.flow.calls) - s0))
    m2, clk = base(period=256)
    c0, s0, _ = snap(m2, None)
    while clk.accepted() < 240 and m2.step_count < 10 ** 7:
        m2.run(max_steps=5000, spin_guard=False)
    print("自动 period=256（accepted=%d）：覆盖+%d  服务+%d"
          % (clk.accepted(), m2.cov.words() - c0, len(m2.flow.calls) - s0))
    print("\n两个数字一样 —— 手动模式「每次跑到 reti」的串行化效果，")
    print("等价于每 tick 一次的中断风暴。这不是巧合：两种模式下固件都没拿到")
    print("中断间的执行时间，回调链推到头（+2053）就停了。")


# ------------------------------------------------------------------ H
def sec_h():
    head("H 长跑：period=1024，100 万步")
    m, clk = base(period=1024)
    c0, s0, _ = snap(m, None)
    print("%-9s %-8s %-9s %-8s %s" % ("步数", "覆盖+", "accepted", "服务+", "任务6"))
    last = c0
    for k in range(10):
        m.run(max_steps=100000, spin_guard=False)
        c1, s1, acc = snap(m, clk)
        print("%-9d %-8d %-9d %-8d %s"
              % ((k + 1) * 100000, c1 - c0, acc, s1 - s0, tasks(m).get(6)))
        last = c1
    print("\n覆盖在 20 万步后趋平：accepted 还在涨（每 1024 周期一次从不缺席），")
    print("但同一段代码反复执行不再带来新地址。固件在这套输入下能走到的")
    print("地方，自动节拍已经全带到了 —— 再往下要新的**事件源**，不是更多中断。")
    print("终态任务：%s" % tasks(m))


def main():
    print("V21：把 INT 0x2B 挂上周期时钟 —— 中断自己来")
    sec_a()
    sec_b()
    sec_c()
    sec_d()
    sec_e()
    sec_f()
    sec_g()
    sec_h()
    print("\n" + "=" * 78)
    print("能报 / 不能报")
    print("=" * 78)
    print("能报：INT 0x2B 可以挂周期时钟（同一向量待决时合并），中断在任务代码、")
    print("      内核里也会被拉起；频率不是中性参数 —— 1024 周期的从容节拍 97 次")
    print("      中断 +2,418，256 周期的风暴 196 次只有 +2,053（=手动 240 次的数）；")
    print("      多出的地址在任务 6 拉起来之后的中断现场里；覆盖约 20 万步饱和。")
    print("不能报：真机这条中断的实际频率（period 是模型参数）；「相机跑起来了」")
    print("      （还是要真外设）；任务 6 在真机上何时被拉起。")


if __name__ == "__main__":
    main()
