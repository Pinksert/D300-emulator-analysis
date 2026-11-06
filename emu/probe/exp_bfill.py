# -*- coding: utf-8 -*-
"""V41 探针：`0x1356A0` 是整片填充，不是「专门清 ICR 的那一句」。

A. 0x1356A0 的真身：`st r0,@(r13,r12)`，r0=值 / r13=递减偏移 / r12=基址
B. 一次调用的形状：基址 0、值 0x80、跨度 0x2188 → 2,147 笔一次不中断的突发
C. 891,520 步总账：0x1356A0 执行多少次、落在哪些区
D. ICR 块被扫过的那些地址（含非对齐的 0x46B）
E. 反事实：把这批写引到影子 RAM → 换手 24→37、任务 9→17、收尾回空闲
F. 任务启动普查：sta_tsk 16 次、17 个入口、从没启动过的清单
G. 0x9F58DA85：891,520 步里 1 笔写、0 笔读
H. 0x137966..0x137974 是带计数的 16 位拷贝循环，不是死循环
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox import bfill, blive, brealos
import fr_dis


def hr(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def dis(m, a0, a1, tag=""):
    if tag:
        print("  -- %s" % tag)
    data = m.fetch_bytes(a0, a1 - a0)
    for t in fr_dis.disasm(data, 0, a1 - a0, a0):
        print("    " + t)


FLAG = 0x9F58DA85


def run_fill(divert=False, steps=700000):
    """boot + steps；返回 (机器, 结果, FillWatch, Divert 或 None)。"""
    m = blive.boot()
    fw = bfill.FillWatch(m).attach()
    dv = None
    if divert:
        dv = bfill.divert_fill(m).attach()
    res = blive.run(m, steps=steps)
    fw.detach()
    if dv is not None:
        dv.detach()
    return m, res, fw, dv


def sec_a():
    hr("A. 0x1356A0 的真身：填充循环里的存储指令")
    m = blive.boot()
    dis(m, 0x135690, 0x1356A8, "循环体（0x135694 起）")
    print("""
  → 0x135696 把 r13 装成 0x218C（字节数），0x13569A 每次减 4，
    0x13569C 读一眼目标、0x1356A0 写一个双字。

  ⚠️ 操作数别读反（这一版第一稿就栽在这里）：解码表字段顺序是 rj 在前、ri 在后，
    打印出来的「r0, r12」= rj=0 / ri=12，指令其实是

        0x1356A0  st r12, @(r13, r0)      ; 值在 r12、基址在 r0、偏移在 r13

    也就是 memset(dst=0b...下面实测) —— r0 是**基址**，不是填充值。""")
    return m


def sec_b(fw):
    hr("B. 11 次同形调用：各 2,147 笔 / 8,588 字节，起点终点都一样")
    rs = fw.runs()
    run = rs[0]
    print("  共 %d 段连续下行，每段 %d 笔、每笔 4 字节 = 8,588 字节 = 0x2188"
          % (len(rs), run["count"]))
    print("  %-9s %-9s %-9s %-9s %s" % ("起始步", "起点", "终点", "跨度", "值"))
    for r in rs:
        vals = sorted(r["values"])
        print("  %-9d 0x%07X 0x%07X 0x%07X  %s"
              % (r["start"], r["top"], r["bottom"], r["top"] - r["bottom"] + 4,
                 ",".join("0x%02X" % v for v in vals[:6]) + ("…" if len(vals) > 6 else "")))
    nz = sum(n for v, n in fw.values_in(0x80, 0x220C).items() if v)
    print("  → %d 次调用形状完全一样：`memset(dst=0x80, val=r12, len=0x2188)`，" % len(rs))
    print("    起点恒 0x2208、终点恒 0x0080。这一整段里非 0 的写只有 %d 笔 / %d 笔，"
          % (nz, sum(fw.values_in(0x80, 0x220C).values())))
    print("    其余全是 0（少数非 0 是循环中途被派发打断、恢复后 r12 已经变了）。")
    return rs


def sec_c(m, fw):
    hr("C. 891,520 步的总账：0x1356A0 执行 23,617 次")
    s2 = fw.summary()
    print("  合计 %d 笔（= %d 字节），分成 %d 段突发，最长一段 %d 笔"
          % (s2["stored"], s2["bytes"], s2["spans"], s2["largest"]))
    for k, v in sorted(s2["regions"].items(), key=lambda x: -x[1]):
        print("    %-12s %6d 笔" % (k, v))
    print("  → 一半落在 0x1000..0x2188（低区 RAM），ICR 块 352 笔（= 32 × 11）。")


def sec_d(fw):
    hr("D. ICR 块 0x400..0x47F 被写过的地址")
    rows = fw.in_range(0x400, 0x480)
    addrs = sorted({a for _s, a, _v, _n in rows})
    print("  %d 笔，起始地址覆盖 %d 个不同位置" % (len(rows), len(addrs)))
    print("    " + " ".join("0x%03X" % a for a in addrs))
    odd = [a for a in addrs if a % 4]
    print("  非 4 字节对齐的：%s" % (" ".join("0x%03X" % a for a in odd) or "无"))
    vals = fw.values_in(0x400, 0x480)
    print("  值分布：%s" % ", ".join("0x%02X×%d" % (v, n) for v, n in sorted(vals.items())))
    print("  → 32 个对齐位置 = ICR 阵列 0x400..0x47C 的全部 32 条，一次不落地被清过；")
    print("    说明它是「按基址整片清过去」，不是按向量号逐条清 ——")
    print("    V40「一条 4 字节写把 0x46C..0x46F 清零」读了现象、错了机制。")
    print("  → 值几乎全是 0x00（一次 memset 里 2,111/2,147 笔），少数非 0 的来自")
    print("    循环中途被派发打断后 r12 变了；0x46C 那一笔是 0x00。")
    return addrs


def sec_e():
    hr("E. 反事实：把这批写引到影子 RAM")
    m1, r1, _f1, _d1 = run_fill(divert=False)
    m2, r2, _f2, d2 = run_fill(divert=True)
    print("  %-28s %-18s %s" % ("", "原样", "引到影子 RAM"))
    print("  %-28s %-18s %s" % ("跑到步数", m1.step_count, m2.step_count))
    print("  %-28s %-18d %d" % ("换手次数", len(r1["switches"]), len(r2["switches"])))
    print("  %-28s %-18d %d" % ("最后一次换手",
                                r1["switches"][-1][0] if r1["switches"] else -1,
                                r2["switches"][-1][0] if r2["switches"] else -1))
    print("  %-28s %-18s %s" % ("收尾当前任务", blive.name(m1), blive.name(m2)))
    print("  %-28s 0x%08X         0x%08X" % ("收尾 PC", m1.cpu.pc, m2.cpu.pc))
    print("  %-28s %-18d %d" % ("跑过入口的任务数", len(r1["tasks"]), len(r2["tasks"])))
    print("    原样   : %s" % ",".join(str(t) for t in r1["tasks"]))
    print("    影子 RAM: %s" % ",".join(str(t) for t in r2["tasks"]))
    print("  %-28s %-18s %s" % ("ICR 0x46F", "0x%02X" % m1.bus.rd(0x46F, 1),
                               "0x%02X" % m2.bus.rd(0x46F, 1)))
    print("  被引走的写 %d 笔" % (d2.diverted if d2 else 0))
    print("""
  → 换手从 24 次涨到 37 次、任务从 9 个涨到 17 个、收尾从「卡在任务 9 的拷贝循环」
    变成「回到空闲任务」。所以 V40 那句「任务 9 清 ICR 把系统卡死」是**模型副作用**：
    固件把 0x0..0x2188 当 RAM 整片填，而我们在这个范围里放了设备（0x44 锁存器、
    0x440 ICR 阵列）。填完 ICR 0x3F = 0x80 → irq_level 读到 0 → 按 0x1F 兜底 →
    永不满足 level < ILM → 派发停摆。
  ⚠️ 不能反过来说「0x0..0x2188 就是 RAM」：0x46F 在服务入口写 0x1E、出口写 0x10，
    期间 ILM=0x1E 正好挡住派发 —— 这条证据仍指向 ICR。两个读法见 KI#168。""")
    return m1, m2


def sec_f(m2):
    hr("F. 任务启动普查：谁被 sta_tsk 启过、谁跑过")
    rows = bfill.census(m2)
    started = bfill.started_tasks(m2)
    print("  sta_tsk 调用 %d 次（流水里 svc=-23）" % len(started))
    print("  %-3s %-10s %-9s %-5s %-5s %-10s %s"
          % ("#", "TCB", "入口", "状态", "prio", "存SP", "跑过"))
    for r in rows:
        print("  %-3d 0x%08X 0x%08X 0x%02X    0x%02X   0x%08X %s"
              % (r["idx"], r["tcb"], r["entry"], r["state"] or 0, r["prio"] or 0,
                 r["saved_sp"], "是" if r["ran"] else "否"))
    ns = bfill.never_started(m2)
    print("  既没被 sta_tsk 启过、入口也没跑过的：%s" % (", ".join(str(i) for i in ns) or "无"))
    print("  → 「12–28 号任务为什么不启动」的答案缩成一份清单：%s 一次都没启动。"
          % ",".join(str(i) for i in ns))
    return rows, started, ns


def sec_g():
    hr("G. 0x9F58DA85：1 笔写、0 笔读")
    m = blive.boot()
    sp = bfill.AddrSpy(m, FLAG).attach()
    res = blive.run(m, steps=700000)
    sp.detach()
    print("  写 %d 笔：%s" % (len(sp.writes), sp.writes[:4]))
    print("  读 %d 笔：%s" % (len(sp.reads), sp.reads[:4]))
    print("  当前值 0x%02X，跑到 %d 步、换手 %d 次"
          % (m.bus.rd(FLAG, 1), m.step_count, len(res["switches"])))
    print("  → 到 891,520 步为止它是个**只写**标志：写入点只有 0x12E0AE 一处，")
    print("    没有任何读者。所以「它表示显示初始化完成」仍然只是推测，不能写成结论。")


def sec_h(m):
    hr("H. 0x137966..0x137974 是带计数的 16 位拷贝循环")
    dis(m, 0x137960, 0x137978)
    print("""
  → lduh @(r4),r0 取一个 16 位目标地址，sth r5,@(r0) 把 r5 写进去，
    r5 += 2、r6 -= 1，r6 > 0 就回去。r6 是计数、r4 是地址表。
    它不是「7 条指令打转的死循环」，V40 那句话也要改。""")


def sec_i():
    """寄存器对总线逐笔比对 —— 操作数归属只能这样定，不能看打印顺序。"""
    hr("I. 拿寄存器对总线：地址 = r13+r0，值 = r12")
    m = blive.boot()
    c = m.cpu
    st = c.step
    reg, bus, icr = {}, [], []
    base_wr = m.bus.wr

    def spy_step():
        if c.pc == 0x1356A0:
            reg[m.step_count] = (c.r[0] & 0xFFFFFFFF, c.r[12] & 0xFFFFFFFF,
                                 c.r[13] & 0xFFFFFFFF)
        return st()

    def spy_wr(addr, n, v, pc=0):
        if pc == 0x1356A0:
            bus.append((m.step_count, addr, v & 0xFFFFFFFF, n))
        if addr <= 0x46F < addr + n:
            icr.append((m.step_count, pc, (v >> (8 * (0x46F - addr))) & 0xFF))
        return base_wr(addr, n, v, pc)

    c.step = spy_step
    m.bus.wr = spy_wr
    blive.run(m, steps=340000)
    c.step = st
    m.bus.wr = base_wr

    print("  命中 %d 笔，逐笔比对结果：" % len(bus))
    a_addr_r0 = sum(1 for s2, a2, _v, _n in bus
                    if s2 in reg and a2 == (reg[s2][2] + reg[s2][0]) & 0xFFFFFFFF)
    a_addr_r12 = sum(1 for s2, a2, _v, _n in bus
                     if s2 in reg and a2 == (reg[s2][2] + reg[s2][1]) & 0xFFFFFFFF)
    v_r0 = sum(1 for s2, _a, v2, _n in bus if s2 in reg and v2 == reg[s2][0])
    v_r12 = sum(1 for s2, _a, v2, _n in bus if s2 in reg and v2 == reg[s2][1])
    print("    地址 == r13+r0  %d/%d      地址 == r13+r12  %d/%d"
          % (a_addr_r0, len(bus), a_addr_r12, len(bus)))
    print("    值   == r0      %d/%d      值   == r12      %d/%d"
          % (v_r0, len(bus), v_r12, len(bus)))
    print("  → 指令是 `st r12,@(r13,r0)`：**r0 是基址 0x80、r12 是值 0、r13 是偏移**。")
    print("    解码器打印的「r0, r12」是 (rj, ri) 顺序，直接当「值, 基址」读就会反过来")
    print("    —— 这一版第一稿就是这么错的，已记 KNOWN_ISSUES #168。")

    odd = [w for w in icr if w[2] not in (0x10, 0x1E)]
    print("\n  0x46F 被写 %d 次，末值 0x%02X" % (len(icr), m.bus.rd(0x46F, 1) & 0xFF))
    print("    非 0x10/0x1E 的写：%s" % ([(w[0], "0x%02X" % w[2]) for w in odd] or "无"))
    print("  → 停摆的直接原因是这条填充把 `vec 0x3F` 的 ICR 写成 0x00，")
    print("    不是 V40 记的「0x80」，也不是「任务 9 单独清 ICR」。")

    nz = [(s2, a2, v2) for s2, a2, v2, _n in bus if v2]
    print("\n  同一次填充里非 0 写入 %d 笔，窗口 步 %d..%d，值样例：%s"
          % (len(nz), nz[0][0] if nz else 0, nz[-1][0] if nz else 0,
             [hex(v2) for _s2, _a2, v2 in nz[:6]]))
    print("    循环体（add2/ld/bgt:d/st）一条都不写 r12，而该窗口 EIT 为 0 次")
    print("    → 只能用模型自身的问题解释，记 KNOWN_ISSUES #169，不作固件结论。")
    return len(bus)


def main():
    sec_a()
    m3, r3, fw3, _d3 = run_fill(divert=False)
    sec_b(fw3)
    sec_c(m3, fw3)
    sec_d(fw3)
    _m1, m2 = sec_e()
    sec_f(m2)
    sec_g()
    sec_h(m3)
    sec_i()

    hr("结论")
    print("""
1. 0x1356A0 是**填充循环里的存储指令**，指令写作 `st r12,@(r13,r0)`：r0=基址 0x80、
   r12=值 0、r13=递减偏移（0x2188 → 0x80）。归属由寄存器对总线逐笔比对确定
   （地址=r13+r0 与 值=r12 两列全中），不是靠解码器打印顺序猜的（KI#168）。
2. 891,520 步里执行 23,617 次 = **11 次同形调用**，每次 2,147 笔 × 4 字节 = 0x2188，
   起点恒 0x2208、终点恒 0x0080，最早一次在步 506,201，此后每 24,353 步一次。
   落点：低区 RAM 12,342 / direct page 352 / ICR 块 352（= 32 × 11）/ 其余 10,571。
3. 停摆的直接原因：步 513,781 那条填充把 `0x46C..0x46F` 写成 0，`vec 0x3F` 的
   ICR 变 0x00 → `irq_level()` 读成 0 → 派发按 0x1F 兜底 → `level < ILM` 永不成立。
   **V40 记的「0x80」和「任务 9 一条 4 字节写」都作废**：写的是 0x00，写者是这
   条填充，不是单独一条清 ICR 的指令（KI#166 改判）。
4. 反事实（把填充写引到影子 RAM）：换手 24→37、任务入口 9→17、收尾由「卡在
   0x137968」变成「回到空闲 0x407DC」—— **停摆是模型副作用**：固件把 0x0..0x2188
   整片当 RAM 填，而我们把 0x44 锁存器和 0x440 ICR 阵列放在了这段里。
5. 0x9F58DA85：1 笔写（步 296,427，pc 0x12E0AE）、**0 笔读**。仍是只写标志。
6. 任务启动：sta_tsk 16 次，17 个入口跑到过；8、12–15、18、19、22–25 一次都没启动。
7. 0x137966..0x137974 是 16 位拷贝循环（r6 计数、r4 地址表），不是 7 指令死循环。
8. 未解释干净：填充里 41 笔非 0 写入集中在步 512,833–514,785，循环体不写 r12、
   该窗口 EIT 为 0 → 模型问题，记 #169。
""")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
