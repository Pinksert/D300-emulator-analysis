# -*- coding: utf-8 -*-
"""exp_isr2b.py —— INT 0x2B：V18 那条「没有调用者的主循环」到底是什么（V19）

七个场景，同一套预热口径（12 × 5000 步，**不**注入 vec 0x13/0x19，
好用一台「没有挂起中断」的干净机器来接线）：

    A 接线        向量表 / ICR / 入口 / 收尾，回答「谁进 0x80588」
    B 位分派表    54 条位测试、14 个待办标志字节、50 个处理函数
    C 基线        不注入：0x80588 一次都没执行（复现 V18）
    D 注入一次    进 ISR、跑完、reti 回来，看新覆盖多少
    E 连续 240 次 两个回调槽不用手动 tick 就自己往前走，走到哪一站
    F 全开任务    7 个 dormant 一起放开再接中断，服务数与任务状态
    G 不答话      不替外设写 0x174 bit6 → 卡在哪一条、第几次

⚠️ 口径三条：
   ① 预热只有 12×5000 步、且不注入事件向量，所以基线覆盖 1,443，
      **不能**和 V18 报告里的 6,382 相减 —— 那是另一套预热（40×5000 + 每批喂事件）。
   ② 「请求一次中断」是我们拿 ICR 里固件自己配的级别 0x1A 去请求，等价于
      「外设拉了一下这条线」；请求频率是我们定的，固件里没有节拍器模型。
   ③ `answer=True` 是替外设往直接页 0x174 写 0x40 —— 这是**假装硬件答话**，
      不是固件行为、更不是「相机跑起来了」。⚠️ V20 起 `answer` 默认 False，
      靠 XferHandshake 自己答；本脚本为了复现 V19 的现场，G 段显式用 run_tick=False。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import isr2b, taskboot           # noqa: E402

WARM = 12
BATCH = 5000


def base(boot_all=False):
    m = create("d300-a")
    m.load()
    if boot_all:
        taskboot.activate(m, "dormant")
    m.reset()
    m.enable_cov()
    m.boot_realos(1)
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
    head("A 接线：0x80588 是谁")
    vec = isr2b.vectors(m)
    print("向量表基址 TBR = 0x%05X（= ROM 文件偏移 0x7FC00），真接线 %d 条"
          % (isr2b.TBR, len(vec)))
    print("  第 0xD4 项（INT n = 255-0xD4 = 0x%02X）→ 0x%05X"
          % (isr2b.VEC, m.vector(isr2b.VEC)))
    print("  这一项的 ICR = 0x%05X 里的 0x%02X（低 5 位 = 级别 %d）"
          % (m.icr_base + ((isr2b.VEC - 0x10) & 0x3F),
             m.irq_level(isr2b.VEC), m.irq_level(isr2b.VEC)))
    print("  installed() = %s" % h.installed())
    lo, hi = isr2b.ENTRY, isr2b.RETI
    print("处理函数范围 0x%05X–0x%05X（%d 字节；收尾是 0x%05X 的 reti）"
          % (lo, hi, hi - lo + 2, hi))
    print("\n开头六条：")
    print(m.disasm_at(lo, 6))
    print("\n结尾四条：")
    print(m.disasm_at(hi - 6, 4))
    print("\n向量表里有 20 项落在 0x809BE–0x8129C 这一片 stub 区，"
          "0x80588 就在同一片代码里 —— 它是这批处理函数中的一个，不是「主程序」。")


# ------------------------------------------------------------------ B
def sec_b(m):
    head("B 位分派表：一个中断，61 个开关")
    rows = isr2b.table(m)
    flags = sorted({r[0] for r in rows})
    inline = [r for r in rows if r[2] < isr2b.RETI]
    tail = [r for r in rows if r[2] >= isr2b.TAIL]
    print("位测试 %d 条（主路径 %d + 尾声子程序 0x%05X 里 %d），"
          "覆盖 %d 个标志字节（0x%05X–0x%05X），指向 %d 个不同处理函数"
          % (len(rows), len(inline), isr2b.TAIL, len(tail),
             len(flags), flags[0], flags[-1],
             len({r[4] for r in rows if r[4]})))
    print("\n按标志字节分组（掩码按 V15 铁律：btstl 立即数就是掩码、btsth 是它 << 4）：")
    for f in flags:
        sub = [r for r in rows if r[0] == f]
        items = []
        for _f, mask, addr, call, tgt in sub:
            items.append("%d:0x%05X" % (mask.bit_length() - 1, tgt))
        print("  0x%05X  %d 位  bit[%s]"
              % (f, len(sub), " ".join(items)))
    print("\n两个回调槽的位置（V18 的派发器就藏在这张表里）：")
    for f, mask, addr, call, tgt in rows:
        if tgt in (0x0008223A, 0x000822CE):
            print("  0x%05X bit%d：测 0x%05X → 装 0x%05X → call 0x%05X"
                  % (f, mask.bit_length() - 1, addr, tgt, call))
    print("\n0x8095C 那个被 9 个位共用的「处理函数」其实是尾声：它先 call 0x8096A"
          "\n（0x3F403 的 7 位在这里分派），再恢复寄存器、reti。")


# ------------------------------------------------------------------ C
def sec_c(m, h):
    head("C 基线：不注入，什么都不发生")
    c0 = m.cov.words()
    print("预热 %d×%d 步后：步数 %d，执行地址 %d，服务 %d"
          % (WARM, BATCH, m.step_count, c0, len(m.flow.calls)))
    print("  0x80588 覆盖 = %s（这就是 V18 看到的「一次都没跑过」）"
          % m.cov.visited(isr2b.ENTRY))
    print("  0x80606（槽 0 派发调用点）覆盖 = %s" % m.cov.visited(0x00080606))
    print("  " + h.state()["cb"])
    return c0


# ------------------------------------------------------------------ D
def sec_d(m, h, c0):
    head("D 注入一次：一条中断进去，多少新代码出来")
    r = h.raise_once()
    print("进 ISR = %s，%d 步，回到 reti = %s，覆盖 +%d，服务 +%d"
          % (r["entered"], r["steps"], not r["cut"], r["delta"], r["svc"]))
    print("  0x80588 覆盖 = %s ；0x80606 覆盖 = %s ；派发器 0x8223A 覆盖 = %s"
          % (m.cov.visited(isr2b.ENTRY), m.cov.visited(0x00080606),
             m.cov.visited(0x0008223A)))
    print("  " + r["cb"])
    print("  累计：执行地址 %d（+%d）" % (m.cov.words(), m.cov.words() - c0))


# ------------------------------------------------------------------ E
def sec_e(m, h, c0):
    head("E 连续 240 次：两个回调槽自己往前走")
    print("%-6s %-7s %-6s %s" % ("次数", "步数", "覆盖+", "回调槽"))
    prev_ptr = m.cb.slots[0].state()["cb"]
    stops = []
    last = None
    zero = None
    for _ in range(240):
        r = h.raise_once()
        ptr = r["cb_after"][0]
        if ptr != prev_ptr:
            stops.append((r["n"], r["steps"], r["delta"], r["cb"]))
            print("%-6d %-7d %-6d %s" % (r["n"], r["steps"], r["delta"], r["cb"]))
            prev_ptr = ptr
        elif zero is None and r["cb"].startswith("槽0 cb=0x000822B6 count=0x0000"):
            zero = r
        last = r
    print("\n换站 %d 次；最后一条：" % len(stops))
    print("  " + last["cb"])
    if zero is not None:
        print("  计数归零发生在第 %d 次中断（那时标志位 0x3F404 已经清 0，链注销）"
              % zero["n"])
    print("  待办标志：%s" % ({hex(k): v for k, v in h.pending().items()} or "全 0"))
    print("  累计：执行地址 %d（+%d），服务 %d，步数 %d"
          % (m.cov.words(), m.cov.words() - c0, len(m.flow.calls), m.step_count))
    print("  任务：%s" % tasks(m))

    print("\n链的走法：")
    for n, _s, _d, cb in stops:
        print("  第 %3d 次中断 → %s" % (n, cb.split("|")[0].strip()))
    return stops


# ------------------------------------------------------------------ F
def sec_f():
    head("F 全开 7 个 dormant 任务 + 240 次中断")
    m, h = base(boot_all=True)
    c0 = m.cov.words()
    svc0 = len(m.flow.calls)
    st0 = tasks(m)
    print("起点：执行地址 %d，服务 %d" % (c0, svc0))
    print("  任务：%s" % st0)
    stops = []
    for _ in range(240):
        r = h.raise_once()
        if r["changed"]:
            stops.append((r["n"], r["delta"], r["cb"]))
    print("\n结束：执行地址 %d（+%d），服务 %d（+%d），步数 %d"
          % (m.cov.words(), m.cov.words() - c0, len(m.flow.calls),
             len(m.flow.calls) - svc0, m.step_count))
    print("  任务：%s" % tasks(m))
    print("  回调槽摘要变化 %d 次，最后一条：%s" % (len(stops), h.state()["cb"]))
    return m, h, c0


# ------------------------------------------------------------------ G
def sec_g():
    head("G 不替外设答话：卡在哪一条")
    print("⚠️ V20 更正：这一段现在**不会**卡住了。`0x174` 归 XferHandshake，")
    print("   它的 tick() 会把 bit6 置起来。本段改用 run_tick=False 来复现 V19 的现场。")
    m, h = base()
    c0 = m.cov.words()
    for k in range(1, 21):
        r = h.raise_once(answer=False, max_steps=4000, run_tick=False)
        if r["cut"]:
            print("第 %d 次中断就回不来了：%d 步后停在 0x%05X，覆盖 +%d"
                  % (k, r["steps"], m.cpu.pc, m.cov.words() - c0))
            print("\n  卡住的地方：")
            print(m.disasm_at(isr2b.POLL_PC - 6, 5))
            print("\n  说明：0x174 是直接页里的一个握手寄存器，代码先置它的 bit1、"
                  "再等 bit6。run_tick=False 让设备 tick 不跑，bit6 永远不出现 → `beq -4` 自转。")
            print("  前 %d 次能过，是因为那条分支要等到更后面的把手才被走到。" % (k - 1))
            print("\n  V20 的修法：改走 Machine.step(1)，设备模型自己答话（见 exp_tick.py）。")
            return k
        print("  第 %2d 次：%5d 步  覆盖+%d" % (k, r["steps"], r["delta"]))
    print("20 次都没卡住")
    return None


# ------------------------------------------------------------------ H
def sec_h():
    head("H 换一种接法：V18 的手动 tick vs V19 的中断")
    m, h = base()
    c0 = m.cov.words()
    ev = m.cb.chain(400)
    d_v18 = m.cov.words() - c0
    print("V18 写法：cb.chain() 扫到没有槽挂指针 —— 真正执行回调 %d 次，覆盖 +%d"
          % (len(ev), d_v18))
    for t, name, r in ev:
        print("  第 %-4d 圈 %s 0x%05X → 0x%05X（%d 步 / +%d）"
              % (t, name, r["cb"], r["next"], r["steps"], r["delta"]))
    m2, h2 = base()
    c1 = m2.cov.words()
    stops = 0
    for _ in range(240):
        if h2.raise_once()["changed"]:
            stops += 1
    d_v19 = m2.cov.words() - c1
    print("\nV19 写法：irq2b 请求 240 次 —— 槽变化 %d 次，覆盖 +%d" % (stops, d_v19))
    print("两者都比基线高，但走的路不一样：手动 tick 只调派发器本体，")
    print("中断进去的是整个 0x80588（378 条指令 + 它自己的 14 字节标志块）。")


def main():
    m, h = base()
    sec_a(m, h)
    sec_b(m)
    c0 = sec_c(m, h)
    sec_d(m, h, c0)
    sec_e(m, h, c0)
    sec_f()
    sec_g()
    sec_h()
    print("\n" + "=" * 78)
    print("边界")
    print("=" * 78)
    print("能报：0x80588 = INT 0x2B 的处理函数（向量表第 0xD4 项）；它是 14 字节")
    print("      待办标志块的分派器（61 条位测试 / 57 个处理函数）；接上这条线之后")
    print("      两个回调槽会自己往前走，走到第四站后计数归零、标志位清零。")
    print("不能报：真机上这条线多久来一次（固件里没有节拍器模型）；0x174 那个")
    print("      握手背后是什么器件；更不能报「相机跑起来了」——依然是注入。")


if __name__ == "__main__":
    main()
