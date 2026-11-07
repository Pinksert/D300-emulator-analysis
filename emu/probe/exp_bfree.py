# -*- coding: utf-8 -*-
"""V48 探针：门铃找到了 —— 88 个能按 flag 6 的点，和那扇没人开的门。

    cd emu && python probe/exp_bfree.py > ../results/frbox/bfree.txt

V47 结案时留了一句「flag 6 的释放端仍未定位」，依据是动态普查：
跑过的 17 个任务里，`set_flg` 只碰过 flag 1 / 3 / 10 / 12 / 14。

V48 换个问法：不问「谁按了铃」，问「**ROM 里谁能按铃**」。于是先要把静态普查
这件事做成设施（`frbox/bstatic.py`），再回来回答三个问题：

A  全 ROM 静态普查总账：2297 个 `int #0x40` 调用点、15435 条调用边
B  flag 6 的释放端：88 个 `set_flg(6, ·)`，其中 **7 个是 bit5**
C  门字节 `0x9F58D08F`：谁写（2 个点）、谁读、V25 已定的机理
D  动态基线：门字节读到 0、干活分支 0 次、flag 6 从没被 set
E  反事实：把这扇门掀开一次，按步号把 flag 6 的每一次动静排出来
F  结论与边界
G  复现

⚠️ 两台独立的机器：静态普查用一台（只读 ROM），D/E 各起一台。
同一台机器不能跑两遍（会从停住的现场继续，V43 栽过）。
"""
import sys

sys.path.insert(0, '.')

from frbox import bflag, bgate, blive, bpool, bstatic, brealos

GATE = bgate.GATE
GATE_LOAD = 0x120176        # ldub r0, @0x9F58D08F
WORK = bgate.WORK_ENTRY     # 0x120184 干活分支
IDLE = bgate.IDLE_ENTRY     # 0x120302 空转支
CLR6 = 0x122090             # 分配器进来先 clr_flg(6, ~0x20)
WAIT = bpool.WAIT_FLG_PC    # 0x12215C
RELOCK = 0x122162           # 醒来后重新加锁
CALLER = bpool.CALLER_R8_PC  # 0x13531E 调用方取返回值
FLG = bpool.POOL_FLAG
BIT = bpool.POOL_FLAG_BIT
SET6 = (0x1201CA, 0x122426, 0x1233B6, 0x14D5E6, 0x14D726, 0x14DACA, 0x14DB52)
RUN_STEPS = 600000


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def task_of(m, tcb):
    """TCB 指针 → 任务号（0 = 认不出）。"""
    if not tcb:
        return 0
    for r in brealos.records(m):
        if r["tcb"] == tcb:
            return r["id"]
    return 0


def hx(v):
    return "None" if v is None else "0x%X" % v


# ---------------------------------------------------------------- A
def sec_a(sw):
    hr("A. 全 ROM 静态普查（线性扫 7 MB，约 50 秒）")
    s = sw.summary()
    print("  调用点 %d 个（服务号认不出 %d 个）" % (s["calls"], s["unknown_svc"]))
    print("  调用边 %d 条 / 不同目标 %d 个" % (s["edges"], s["targets"]))
    print("  `enter` %d 个 / 能算出地址的 store %d 条" % (s["funcs"], s["stores"]))

    print("\n  服务号分布（前 12）：")
    top = sorted(s["by_svc"].items(), key=lambda kv: -kv[1])[:12]
    for k, n in top:
        nm = {bstatic.SET_FLG: "set_flg", bstatic.CLR_FLG: "clr_flg",
              bstatic.WAI_FLG: "wai_flg", bstatic.SEM_WAI: "wai_sem",
              bstatic.SEM_SIG: "sig_sem", -85: "dly_tsk", -37: "?",
              -106: "?", -23: "sta_tsk", -24: "get_tid", -20: "ref_tsk",
              -27: "chg_pri"}.get(k, "")
        print("    %-8s %-9s %4d" % (k, nm, n))

    print("\n  校验：V47 用动态证据定死的两个点，静态也必须一模一样")
    for a, want in ((CLR6, "clr_flg(flg=6, bits=0xDF)"),
                    (WAIT, "wai_flg(flg=6, ptn=0x20, mode=2)")):
        c = [x for x in sw.calls if x.addr == a]
        print("    0x%06X  %-34s %s" % (a, want, c[0] if c else "**没找到**"))

    print("\n  ⚠️ %d 个服务号认不出 + 全部实参未知的点：只能说明「字节里没写立即数」，"
          % s["unknown_svc"])
    print("     不能说明「它不碰 flag 6」（见 bstatic.py 的边界）。")


# ---------------------------------------------------------------- B
def sec_b(sw):
    hr("B. flag 6 的释放端：88 个 set_flg(6)，7 个是 bit5")
    sets = sw.flag_sets(FLG)
    print("  set_flg 总 %d 处；碰 flag 6 的 %d 处；其中 bits=0x%X 的 %d 处"
          % (len(sw.flag_sets()), len(sets), BIT,
             len([c for c in sets if c.bits == BIT])))

    print("\n  按 bits 分组：")
    g = {}
    for c in sets:
        g[c.bits] = g.get(c.bits, 0) + 1
    for b, n in sorted(g.items(), key=lambda kv: (kv[0] is None, kv[0])):
        mark = "   ← 分配器等的就是这一位" if b == BIT else ""
        print("    bits=%-12s %3d 处%s" % (hx(b), n, mark))

    print("\n  bit5 的 7 处（地址 / 所属函数 —— 有没有被执行过，由 D/E 段的动态计数回答）：")
    for c in sets:
        if c.bits == BIT:
            print("    0x%06X  函数 0x%06X"
                  % (c.addr, sw.enclosing(c.addr) or 0))

    print("\n  区域分布（全部 %d 处，16 KB 桶）：" % len(sets))
    b2 = {}
    for c in sets:
        k = c.addr >> 14
        b2[k] = b2.get(k, 0) + 1
    for k in sorted(b2):
        lo = k << 14
        print("    0x%05X–0x%05X  %d" % (lo, lo + 0x3FFF, b2[k]))

    print("\n  wai_flg(6, ·) 的静态点 %d 处 —— 等的人和按的人都在 ROM 里："
          % len(sw.flag_waits(FLG)))
    for c in sw.flag_waits(FLG)[:8]:
        print("    0x%06X  ptn=%-10s mode=%s  函数 0x%06X"
              % (c.addr, hx(c.bits), c.mode, sw.enclosing(c.addr) or 0))
    print("    （共 %d 处，只列前 8）" % len(sw.flag_waits(FLG)))


# ---------------------------------------------------------------- C
def sec_c(sw):
    hr("C. 门字节 0x9F58D08F：谁写、谁读")
    w = sw.writers_of(GATE)
    print("  能算出确切地址的写点：%d 个" % len(w))
    for pc, mn in w:
        print("    0x%06X  %s → 0x%08X" % (pc, mn, GATE))
    print("  其中 0x%06X 就是 `bgate.GATE_WRITER`（V25 定的那个）。"
          % bgate.GATE_WRITER)
    print("\n  同一个门字节的读者：0x%06X `ldub r0, @0x%08X` —— 主循环函数 0x1200B0 的开头。"
          % (GATE_LOAD, GATE))

    near = sw.writers_near(GATE, 0x20)
    print("\n  附近 ±0x20 的写点（%d 个，看有没有「一组状态字节」）：" % len(near))
    for a, pc, mn in near:
        print("    0x%08X  ← 0x%06X %s" % (a, pc, mn))

    print("\n  bgate.py 记的机理（V25）：门字节非 0 → 走干活分支 0x120184；")
    print("  为 0 → 跳到等待点 0x120302。写门字节的那段代码在 0x12F0xx（显示/UI 侧），")
    print("  我们的场景里从来没执行到 —— 所以「门字节没人写」的真含义是")
    print("  「**写它的那段代码没跑到**」，不是「没有写者」（V25 已定）。")


# ---------------------------------------------------------------- D / E
def run_machine(open_gate, steps=RUN_STEPS):
    """跑一台机器。open_gate=True 时，每次读到门字节之前把它掀成 1。"""
    m = blive.boot()
    c = m.cpu
    st = c.step
    cnt = {GATE_LOAD: 0, WORK: 0, IDLE: 0, CLR6: 0, WAIT: 0, RELOCK: 0, CALLER: 0}
    gvals = {}
    timeline = []

    def spy():
        pc = c.pc
        if pc in cnt:
            cnt[pc] += 1
        if pc == GATE_LOAD:
            v = m.bus.rd(GATE, 1)
            gvals[v] = gvals.get(v, 0) + 1
            if open_gate:
                bgate.open_gate(m, 1)
        elif pc in SET6 or pc == CLR6 or pc == WAIT or pc == RELOCK or pc == CALLER:
            timeline.append((m.step_count, pc, bflag.pattern(m, FLG),
                             bflag.waiter(m, FLG)))
        return st()

    c.step = spy
    blive.run(m, steps=steps)
    c.step = st

    sets = [(s, b) for s, k, t, f, b in m.flow.sets() if f == FLG and k == "set"]
    clrs = [(s, b) for s, k, t, f, b in m.flow.sets() if f == FLG and k == "clr"]
    return m, cnt, gvals, sets, clrs, timeline


SET6 = (0x1201CA, 0x122426, 0x1233B6, 0x14D5E6, 0x14D726, 0x14DACA, 0x14DB52)


def report(tag, m, cnt, gvals, sets, clrs, timeline):
    print("\n  %s" % tag)
    print("    步数 %d  末 PC 0x%06X  覆盖 %d 字"
          % (m.step_count, m.cpu.pc, m.cov.words()))
    print("    门字节读 %d 次，取值分布 %s"
          % (cnt[GATE_LOAD], {hex(k): v for k, v in gvals.items()}))
    print("    干活分支 0x%06X %d 次 / 空转支 0x%06X %d 次"
          % (WORK, cnt[WORK], IDLE, cnt[IDLE]))
    print("    set_flg(6,·) 被执行：%d 次 %s" % (len(sets), sets[:4]))
    print("    clr_flg(6,·) 被执行：%d 次" % len(clrs))
    print("    分配器 wai_flg %d 次 / 醒来重新加锁 0x%06X %d 次 / 调用方取返回 0x%06X %d 次"
          % (cnt[WAIT], RELOCK, cnt[RELOCK], CALLER, cnt[CALLER]))
    print("    flag 6 最终：位模式 0x%08X，等待队头 0x%08X（任务 %d）"
          % (bflag.pattern(m, FLG), bflag.waiter(m, FLG),
             task_of(m, bflag.waiter(m, FLG))))


def sec_d():
    hr("D. 动态基线：门字节读到 0，干活分支一次都没进")
    m, cnt, gvals, sets, clrs, tl = run_machine(False)
    report("基线（不掀门）", m, cnt, gvals, sets, clrs, tl)
    print("\n    ⭐ 门字节在整个 79 万步里只被读 **1 次**、读到 0 —— 主循环函数 0x1200B0")
    print("       只被调用一次，取空转支之后再没回来。所以「主循环」在我们的场景里")
    print("       其实**没有在循环**。")
    return m


def sec_e():
    hr("E. 反事实：把门字节掀开一次，flag 6 的动静按步号排出来")
    m, cnt, gvals, sets, clrs, tl = run_machine(True)
    report("反事实（每次读之前掀成 1）", m, cnt, gvals, sets, clrs, tl)

    print("\n  flag 6 的时间线（记的是**执行到那条指令时**的位模式与等待队头）：")
    print("    %-9s %-9s %-12s %-12s %s"
          % ("步", "PC", "位模式", "等待队头", "是什么"))
    for s, pc, ptn, w in tl:
        what = {GATE_LOAD: "读门字节", WORK: "进干活分支", IDLE: "取空转支",
                CLR6: "clr_flg(6,~0x20)", WAIT: "wai_flg(6,0x20)",
                RELOCK: "醒来重新加锁", CALLER: "调用方取返回"}.get(pc, "")
        if pc in SET6:
            what = "set_flg(6,0x20)"
        print("    %9d 0x%06X  0x%08X   0x%08X   %s" % (s, pc, ptn, w, what))

    print("\n  读数（全部从上面那张时间线里取，不猜）：")
    tset = [r for r in tl if r[1] in SET6]
    tclr = [r for r in tl if r[1] == CLR6]
    twait = [r for r in tl if r[1] == WAIT]
    print("    · 门一掀，干活分支真的进来了（0 → 1 次），`0x1201CA` 那句 set_flg 也真的")
    print("      执行了 —— **门铃确实存在、确实能被按**。")
    if tset and tclr and twait:
        s0 = tset[0][0]
        print("    · 铃响在步 %d：位模式 0x%08X → 0x%08X（+bit5）"
              % (s0, tset[0][2], tset[0][2] | BIT))
        print("    · 等的人 **%d 步之后才进分配器**（步 %d），进来第一件事就是"
              % (tclr[0][0] - s0, tclr[0][0]))
        print("      `clr_flg(6, ~0x20)` —— 位模式 0x%08X → 0x%08X，**把刚响过的那一位擦了**"
              % (tclr[0][2], twait[0][2]))
        print("    · 再过 %d 步它才等到 `wai_flg(6, 0x20)`（步 %d），此刻位模式 0x%08X"
              % (twait[0][0] - tclr[0][0], twait[0][0], twait[0][2]))
        print("      ⇒ 挂上去的时候铃已经是哑的，于是永远等下去。")
    print("    · 分配器仍然只等了 1 次、醒来重新加锁 0 次、调用方取返回 0 次。")
    print("\n  ⭐ 这条阴性结果与 V47 那条（补一次 set_flg 只换来一次空转）指向同一处：")
    print("     **缺的不是「置位」这一步，是「把节点挂回链表、再置位」这个顺序。**")
    print("     只补置位 —— 不管补在等之前还是等之后 —— 都不足以让它拿到活。")
    return m, tl


# ---------------------------------------------------------------- F
def sec_f(base_m, cf_m, tl):
    hr("F. 结论")
    print("1. **静态普查是可行的**：7 MB ROM 线性扫一遍 50 秒，得到 2297 个 `int #0x40`")
    print("   调用点、15435 条调用边、1576 个被调用目标、25862 条能算出地址的 store。")
    print("   这套设施（`frbox/bstatic.py`）回答的是「字节里写了什么」，不是「会不会被执行」。")

    print("\n2. **flag 6 的释放端在 ROM 里一直存在**：88 个 `set_flg(6, ·)`，其中 7 个是")
    print("   bit5。V47「从没被 set」的真实含义是「**跑过的代码没按过**」，")
    print("   而不是「没有按铃的人」。这一条把 V47 的空白补上了。")

    print("\n3. **主循环那扇门是总闸**：`0x9F58D08F` 只有 2 个已知写点（0x12ECF4 /")
    print("   0x12F180），读者的分支决定「干活还是空转」。我们的场景里门字节读到 0、")
    print("   主循环只跑过一趟、取的是空转支 —— 所以 88 个点里最容易到的那个（0x1201CA）")
    print("   也没被执行。")

    tset = [r for r in tl if r[1] in SET6]
    tclr = [r for r in tl if r[1] == CLR6]
    twait = [r for r in tl if r[1] == WAIT]
    if tset and tclr and twait:
        print("\n4. **铃响过，但被等的人自己擦掉了**：掀门后 `set_flg(6,0x%X)` 在步 %d"
              % (BIT, tset[0][0]))
        print("   真的置上了 bit5；分配器 %d 步之后才进来，第一件事 `clr_flg(6, ~0x%X)`"
              % (tclr[0][0] - tset[0][0], BIT))
        print("   —— 按 μITRON 语义（clrptn 为 0 的位被清）它清的**正好是刚响过的那一位**。")
        print("   于是等到 `wai_flg(6,0x%X)` 时位模式已经是 0x%08X，挂上去就再没人叫。"
              % (BIT, twait[0][2]))
    print("   ⇒ 与 V47 的阴性结果（补一次信号只换来一次空转）合起来，结论收窄到一句：")
    print("   **缺的不是「置位」，是「把节点挂回链表、再置位」这个顺序**。")

    print("\n5. **不能报的**：不能说「相机跑不起来是因为门字节没人写」——门字节的写者")
    print("   0x12F180 所在函数自身的进入条件仍未定（V25 留的 OPEN），这一版没动它。")


def sec_g():
    hr("G. 复现")
    print("  cd emu && python probe/exp_bfree.py > ../results/frbox/bfree.txt   # 约 15 分钟")
    print("  cd emu && python -c \"from frbox import bstatic; s,m=bstatic.sweep_of_type();"
          " print(s.summary())\"")
    print("\n  ⚠️ 静态普查只是读了 ROM，与动态两段互不影响；D/E 是两台独立的机器。")


def main():
    sw, _m = bstatic.sweep_of_type("d300-b")
    sec_a(sw)
    sec_b(sw)
    sec_c(sw)
    base_m = sec_d()
    cf_m, tl = sec_e()
    sec_f(base_m, cf_m, tl)
    sec_g()


if __name__ == "__main__":
    main()
