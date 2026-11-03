# -*- coding: utf-8 -*-
"""V22 探针（A 段）：位分派器待办区 61 位逐位扫描。

V19 已证明 INT 0x2B（0x80588）是 14 字节待办标志 0x3F402–0x3F40F 的位分派器：
61 条位测试 → 57 个处理函数。V18/V19/V20/V21 只走过其中 2 个回调槽
（0x3F404 bit1 → 0x8223A、0x3F40D bit2 → 0x822CE）—— 其余 55 位从没执行过。

V22 的 A 段实验：**把 61 位一位一位点亮**，看每个处理函数：
  · 跑没跑（目标地址有没有被执行）
  · 跑多远（覆盖增量）
  · 干了什么（μITRON 服务调用：set_flg / sta_tsk / snd_msg…）
  · 结束在哪（回空闲 / 自旋 / 别处）
  · 位有没有被自己清掉（自动应答）

口径与 V19–V21 相同：预热 12×5000 步、不注入任何事件（基线 1,443）。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox.isr2b import table as dispatch_table, attach as attach_irq, VEC


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def base():
    """V19–V21 口径的基线机：boot REALOS + 回调槽 + INT 0x2B，预热 12×5000。"""
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


def flag_bytes(m):
    return {a: m.bus.rd(a, 1) for a in range(0x3F402, 0x3F410)}


def sec_a():
    head("A 基线与位分派表")
    m, h = base()
    print("基线覆盖 %d（口径：12×5000 预热不注入事件）" % m.cov.words())
    t = dispatch_table(m)
    print("位分派表 %d 条 → 处理函数 %d 个（去重）" % (len(t), len(set(x[4] for x in t))))
    pending = h.pending()
    print("boot 后待办区自带标志：%s" % (pending if pending else "无"))


def sec_b():
    head("B 61 位逐位点亮：单次 INT 0x2B 每位一次")
    m, h = base()
    t = dispatch_table(m)
    baseline_cov = m.cov.words()
    # 记录基线状态
    rows = []
    for byte, mask, btst, call, tgt in t:
        if tgt == 0x0008095C:            # 尾声子程序起点（V19 已证不是处理函数）
            kind = "尾声"
        else:
            kind = "处理"
        # 清位 → 点亮这一位 → 一次中断
        old = m.bus.rd(byte, 1)
        m.bus.wr(byte, 1, mask, m.cpu.pc)
        b_cov = m.cov.words()
        b_svc = len(m.flow.calls)
        b_irq = len(m.cpu.irq_log)
        h.raise_once()
        # 跑完 ISR（raise_once 内部跑到 reti；再多给 2000 步看后续）
        m.run(max_steps=2000, spin_guard=False)
        dcov = m.cov.words() - b_cov
        dsvc = len(m.flow.calls) - b_svc
        accepted = len(m.cpu.irq_log) - b_irq == 1
        ran = m.cov.visited(call)          # call 点有没有执行
        tgt_ran = m.cov.visited(tgt)
        after = m.bus.rd(byte, 1)
        acked = not (after & mask)          # 位被处理函数自己清掉了吗
        pc = m.cpu.pc
        rows.append((byte, mask, kind, tgt, accepted, ran, tgt_ran,
                     dcov, dsvc, acked, pc))
        # 位恢复原状（避免影响下一位）
        m.bus.wr(byte, 1, old, m.cpu.pc)
    print("基线 %d。逐位结果：" % baseline_cov)
    print("标志字节     掩码  类型   处理函数   受理  call执行  目标执行  覆盖+  调用+  自动应答  结束PC")
    ran_n = ack_n = svc_n = 0
    for byte, mask, kind, tgt, acc, ran, tr, dc, ds, ack, pc in rows:
        print("  0x%08X  0x%02X  %s  0x%05X   %d     %d       %d      %4d  %3d    %d      0x%05X"
              % (byte, mask, kind, tgt, acc, ran, tr, dc, ds, ack, pc))
        ran_n += ran
        ack_n += ack
        svc_n += ds
    print("统计：61 位里 call 点执行 %d / 目标执行 %d / 自动应答 %d / 有服务调用 %d"
          % (sum(r[5] for r in rows), sum(r[6] for r in rows),
             ack_n, svc_n))


def sec_c():
    head("C 全点亮：14 字节全 0xFF 一次中断，看分派器全链")
    m, h = base()
    b = m.cov.words()
    for a in range(0x3F402, 0x3F410):
        m.bus.wr(a, 1, 0xFF, m.cpu.pc)
    h.raise_once()
    m.run(max_steps=30000, spin_guard=False)
    print("全 0xFF 一次中断 + 3 万步：覆盖 %d → %d（+%d），PC=0x%05X"
          % (b, m.cov.words(), m.cov.words() - b, m.cpu.pc))
    t = dispatch_table(m)
    hit = sum(1 for _bt, _m, _c, call, tgt in t if m.cov.visited(tgt) and tgt != 0x8095C)
    print("14 字节全亮时被点到的处理函数：%d / 57" % hit)
    print("待办区剩余：%s" % (h.pending() or "全清"))


def sec_d():
    head("D 单开最有结果的位 ×64 次（找自动应答的循环源）")
    # 先跑一遍 B 段的统计再挑：直接重跑统计（确定性）
    m, h = base()
    t = dispatch_table(m)
    scores = []
    for byte, mask, btst, call, tgt in t:
        if tgt == 0x8095C:
            continue
        old = m.bus.rd(byte, 1)
        m.bus.wr(byte, 1, mask, m.cpu.pc)
        b = m.cov.words()
        h.raise_once()
        m.run(max_steps=2000, spin_guard=False)
        scores.append((m.cov.words() - b, byte, mask, tgt))
        m.bus.wr(byte, 1, old, m.cpu.pc)
    scores.sort(reverse=True)
    print("单发覆盖增量前 8：")
    for dc, byte, mask, tgt in scores[:8]:
        print("  [0x%08X]&0x%02X → 0x%05X：+%d" % (byte, mask, tgt, dc))
    top = scores[:3]
    for dc, byte, mask, tgt in top:
        m2, h2 = base()
        b = m2.cov.words()
        for _ in range(64):
            m2.bus.wr(byte, 1, mask, m2.cpu.pc)
            h2.raise_once()
            m2.run(max_steps=2000, spin_guard=False)
        print("[0x%08X]&0x%02X ×64：覆盖 %d → %d（+%d），PC=0x%05X，调用 %d"
              % (byte, mask, b, m2.cov.words(), m2.cov.words() - b,
                 m2.cpu.pc, len(m2.flow.calls)))


def main():
    sec_a()
    sec_b()
    sec_c()
    sec_d()
    print("\n完成。")


if __name__ == "__main__":
    main()
