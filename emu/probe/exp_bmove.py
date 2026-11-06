# -*- coding: utf-8 -*-
"""V42 探针：那条「整片搬运」到底在搬什么、基址为什么是 0。

    cd emu && python probe/exp_bmove.py > ../results/frbox/bmove.txt
"""
import sys
sys.path.insert(0, '.')

import fr_dis
from fr_emu import TABLE
from frbox import blive, bmove


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def sec_a():
    hr("A. 五条指令的真身：dst = r9+0x80、src = r8+0x4C")
    m = blive.boot()
    rom, base = m.bus.rom, m.bus.rom_base
    for lo, hi in ((0x13568E, 0x1356A2),):
        for t in fr_dis.disasm_addr(rom, lo, hi, base):
            print("  " + t)
    print("""
  → 0x135690 `add r9,r0` 把 r0 变成 r9+0x80；0x135694 `add r8,r1` 把 r1 变成
    r8+0x4C。循环体读 [r8+0x4C+off]、写 [r9+0x80+off]，等价于
        memmove(dst=r9+0x80, src=r8+0x4C, len=0x2188)      位移 +0x34
    也就是说：**0x1356A0 不是 memset，是一条 memmove 的存储端**。
""")
    return m


def sec_b(m):
    hr("B. 11 次搬运的形状（每次都记 r8 / r9）")
    sw = bmove.ShiftWatch(m).attach()
    blive.run(m, steps=700000)
    sw.detach()
    cs = sw.calls()
    print("  共 %d 笔存储，切成 %d 次搬运" % (len(sw.stores), len(cs)))
    print("  %-4s %-9s %-17s %-17s %-6s %-6s" % ("#", "起始步", "dst", "src", "r8", "r9"))
    for i, c in enumerate(cs, 1):
        print("  %-4d %-9d 0x%04X→0x%04X   0x%04X→0x%04X   0x%04X 0x%04X"
              % (i, c["start"], c["dst_hi"], c["dst_lo"],
                 c["src_hi"], c["src_lo"], c["r8"], c["r9"]))
    s = sw.summary()
    print("\n  每段笔数：%s" % s["words"])
    print("  dst 集：%s" % ["0x%X→0x%X" % t for t in s["dst"]])
    print("  src 集：%s" % ["0x%X→0x%X" % t for t in s["src"]])
    print("  基址分布 {(r8,r9): 次数}：%s" % s["bases"])
    print("  其中 r8 或 r9 为 0 的：%d / %d" % (s["null"], s["calls"]))
    print("  落点分布：%s" % s["regions"])
    print("""
  → 11 次形状完全一致，**全部 r8 = r9 = 0**。所以「dst=0x80」只是 r9=0 的巧合，
    真实语义是 dst = r9+0x80。基址为 0 → 整块搬到 0x84..0x2208。
""")
    return sw


def sec_c():
    hr("C. 快照对拍：确实是「整块位移」，不是填 0")
    m = blive.boot()
    c = m.cpu
    st = c.step
    got = {}
    SNAP = {506_195: "before", 515_000: "after"}

    def spy():
        if m.step_count in SNAP:
            got[SNAP[m.step_count]] = bmove.snapshot(m)
        return st()

    c.step = spy
    blive.run(m, steps=340000)
    c.step = st
    res = bmove.verify_shift(got["before"], got["after"])
    print("  after[x+0x34] == before[x]：%d / %d" % (res["hit"], res["total"]))
    print("  不命中 %d 个，前 12：%s"
          % (len(res["miss"]), [(hex(x), hex(v), hex(w)) for x, v, w in res["miss"][:12]]))
    print("""
  → 2,215 / 2,227 命中（不命中的 12 个都在 0x4..0x48，落在 dst 区间
    0x84..0x2208 之外，本来就不该被搬）。**这是 memmove，不是 memset**。
""")
    return res


def sec_d(m):
    hr("D. ICR 0x46F 是怎么被踩的")
    print("  上一段已证明：dst 区间 0x84..0x2208 覆盖 ICR 阵列 0x440..0x47C。")
    print("  源区间 0x40C..0x44B 里对应的字是 0 → 搬过去就把 ICR 写成 0。")
    print("\n  0x46F 末值 = 0x%02X" % (m.bus.rd(0x46F, 1) & 0xFF))
    print("""
  → 这与 V41 的观察一致，但机制要改写：不是「固件清 ICR」，是**一次基址为 0
    的 memmove 把 ICR 当成缓冲区踩掉了**。
""")


def sec_e():
    hr("E. 静态 + 动态：函数体内 r8 / r9 的写点，到底执行过没有")
    m = blive.boot()
    rom, base = m.bus.rom, m.bus.rom_base
    w9 = bmove.writes_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r9")
    w8 = bmove.writes_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r8")
    n9 = bmove.scan_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r9")
    n8 = bmove.scan_reg(rom, base, bmove.FUNC_LO, bmove.FUNC_HI, "r8")
    print("  0x%X..0x%X：提到 r9 的 %d 条，**写** r9 的 %d 条"
          % (bmove.FUNC_LO, bmove.FUNC_HI, len(n9), len(w9)))
    for pc, t in w9:
        print("    0x%-8X %s" % (pc, t))
    print("  提到 r8 的 %d 条，**写** r8 的 %d 条" % (len(n8), len(w8)))
    for pc, t in w8:
        print("    0x%-8X %s" % (pc, t))

    # 动态：这些写点在 340,000 步里各执行几次
    hits = {pc: 0 for pc, _t in w9 + w8}
    c = m.cpu
    st = c.step

    def spy():
        p2 = c.pc
        if p2 in hits:
            hits[p2] += 1
        return st()

    c.step = spy
    blive.run(m, steps=340000)
    c.step = st
    print("\n  340,000 步内的执行次数：")
    for pc, _t in w9:
        print("    0x%-8X r9 写点 %d 次" % (pc, hits[pc]))
    for pc, _t in w8:
        print("    0x%-8X r8 写点 %d 次" % (pc, hits[pc]))
    print("""
  → 判「写」用的是**解码字段**（`ri` 几乎总是目的）+ 例外助记符表。V42 第一稿
    按「文本最后一个词」判，把 `cmp #0x0, r9`（只读）和 `bandh #0x7, r8`
    （r8 当地址）算成了写，5 条里 2 条是假的 —— 已改。
""")
    return w9, w8


def sec_f():
    hr("F. 上游：call 0x122064 返回 0，任务入口 r8=r9=0")
    m = blive.boot()
    c = m.cpu
    st = c.step
    seen = {"entry": None, "ret": None}

    def spy():
        if c.pc == bmove.FUNC_LO and seen["entry"] is None:
            seen["entry"] = (m.step_count, c.r[8] & 0xFFFFFFFF, c.r[9] & 0xFFFFFFFF,
                             c.dr[1] & 0xFFFFFFFF)
        if c.pc == bmove.NULL_RET_PC and seen["ret"] is None:
            seen["ret"] = (m.step_count, c.r[0] & 0xFFFFFFFF, c.r[4] & 0xFFFFFFFF,
                           c.r[8] & 0xFFFFFFFF)
        return st()

    c.step = spy
    blive.run(m, steps=520000)
    c.step = st
    e, r = seen["entry"], seen["ret"]
    if e:
        print("  任务体 0x%X 第一次执行：步 %d  r8=0x%08X r9=0x%08X dr1=0x%08X"
              % (bmove.FUNC_LO, e[0], e[1], e[2], e[3]))
    if r:
        print("  call 0x122064 返回后（0x%X）：步 %d  r0=0x%08X r4=0x%08X r8=0x%08X"
              % (bmove.NULL_RET_PC, r[0], r[1], r[2], r[3]))

    rom, base = m.bus.rom, m.bus.rom_base
    print("\n  callee 0x122064 头部：")
    for t in fr_dis.disasm_addr(rom, 0x122064, 0x122096, base):
        print("    " + t)
    print("\n  任务 trampoline 0x%X：" % bmove.TRAMPOLINE)
    for t in fr_dis.disasm_addr(rom, bmove.TRAMPOLINE, bmove.TRAMPOLINE + 0x10, base):
        print("    " + t)
    print("""
  → callee 在 0x12206A 明写 r8 = 0x9F58C4C0，0x122092 又 `mov r8,r0`（返回它），
    可是回到 0x13531E 时 r0/r4/r8 全为 0。调用方 `mov r4,r8` 于是把 0 灌进 r8。
  → 任务 trampoline 是 `call @r13` → `int #0x40`（服务 −11 = ext_tsk）→ 自旋，
    **不给任务体任何参数**；任务体入口 r8=r9=0。
  → 「寄存器为什么被清空」本版不下结论，记 KNOWN_ISSUES #171。
""")


def cf_row(ptr=None, steps=891520):
    """跑一遍整机；ptr 不为 None 时在 0x135624 把 r4 换成它（反事实）。"""
    m = blive.boot()
    fx = bmove.fix_dst(m, ptr=ptr).attach() if ptr is not None else None
    res = blive.run(m, steps=steps)
    if fx is not None:
        fx.detach()
    sw = res["switches"]
    return {"ptr": ptr, "steps": res["steps"], "handoffs": len(sw),
            "last": (sw[-1][0] if sw else None), "pc": m.cpu.pc,
            "tasks": res["tasks"], "icr": blive.icr(m),
            "hits": (fx.hits if fx is not None else 0),
            "disp": res["counts"]["DISP"], "req": res["counts"]["REQ"]}


def sec_g():
    hr("G. 反事实：只把 0x135624 的 r4 换成安全指针，停摆会不会消失")
    rows = [cf_row(None), cf_row(0x2200), cf_row(0x30000)]
    print("  %-10s %8s %6s %9s %10s %4s %-46s %s"
          % ("r9 取值", "步数", "换手", "末次换手", "末 PC", "ICR", "跑到过的任务入口", "修正"))
    print("  " + "-" * 118)
    for r in rows:
        tag = "原样" if r["ptr"] is None else "0x%X" % r["ptr"]
        print("  %-10s %8d %6d %9s 0x%08X 0x%02X %-46s %d"
              % (tag, r["steps"], r["handoffs"],
                 ("%d" % r["last"]) if r["last"] else "-", r["pc"], r["icr"],
                 str(r["tasks"]), r["hits"]))
    o, a = rows[0], rows[1]
    print("""
  → 只改一个寄存器（其余一概不动）：
      · 换手 %d → **%d**（+%d），末次换手 %s → **%s**
      · 收尾从「卡在 0x%08X」（18 位拷贝循环 0x137966）变成「**回到空闲 0x%08X**」
      · `0x46F` 末值 0x%02X → **0x%02X**
      · 跑到过的入口 %d → **%d** 个
    三行跑同一预算：请求 891,520 步，`blive.run` 按 20,000 步分块，`m.step_count`
    落在 %d（三行相同）。两个不同安全指针（0x2200 / 0x30000）给出**逐项相同**的结果，
    说明与具体地址无关，只与「不再踩 ICR」有关。这一组与 V41 把写引到影子 RAM
    的结果（换手 37、收尾回到空闲）**逐项吻合** ⇒ 停摆确实是**模型副作用**：
    我们在 0x84..0x2208 放了设备，固件把这段当 RAM 搬，于是 ICR 被搬掉。
""" % (o["handoffs"], a["handoffs"], a["handoffs"] - o["handoffs"],
       o["last"], a["last"], o["pc"], a["pc"], o["icr"], a["icr"],
       len(o["tasks"]), len(a["tasks"]), o["steps"]))


def main():
    sec_a()
    m = blive.boot()
    sec_b(m)
    sec_c()
    sec_d(m)
    sec_e()
    sec_f()
    sec_g()

    hr("结论")
    print("""
1. `0x1356A0` 是一条 **memmove 的存储端**：`dst = r9+0x80`、`src = r8+0x4C`、
   `len = 0x2188`、位移 +0x34。V41 说的 `memset(dst=0x80, val=0, len=0x2188)`
   只在 r9=0 时数值成立，**语义是错的**（KI#168 续集：别只看算出来的地址，
   要往上追一层看基址寄存器）。
2. 快照对拍 2,215 / 2,227 命中 `after[x+0x34] == before[x]` —— 是整块搬，
   不是填 0。V41 那 41 笔「非 0 写入」= 搬过来的源数据，**KI#169 结案：
   不是模型问题**。
3. 11 次搬运**全部 r8 = r9 = 0**，所以它们落在 0x84..0x2208，把 ICR 阵列
   0x440..0x47C 当缓冲区踩掉（`0x46F` 被写成 0x00，70 万步 11 次）。
4. 静态：函数 0x1352EA..0x135700 共 404 条指令，提到 r9 的 30 条、**写 r9 的 4 条**
   （0x13534E / 0x1354F4 / 0x1355B2 / 0x135624）；前两条这条路径**一次都没执行**，
   后两条各 **1 次** —— 基址是在 0x1355B2 被显式清成 0、在 0x135624 被 `mov r4,r9`
   灌成 r4（当时 r4=0）。提到 r8 的 55 条、写 r8 的 3 条，真正执行到的只有 0x13531E。
5. 上游：`0x135608 cmp #0x0,r9` → `bne` 到 0x1356A2（跳过整段拷贝），不跳则
   `0x135616 call 0x122D32` 取缓冲区、`0x135624 mov r4,r9` 存指针 —— **调用方不判空**。
   写 r8 的 `0x13531E mov r4,r8` 执行时 r4 = 0；r4 来自 `call 0x122064`，那个 callee
   明明在 0x12206A 设了 r8=0x9F58C4C0 并在 0x122092 `mov r8,r0` 返回它，回到调用方
   却 r0=r4=r8=0（KI#171 OPEN）。
6. 任务体入口（步 484,584，dr1=0x404F8）r8=r9=0；trampoline 0x404F0 只做
   `call @r13` → `int #0x40`（ext_tsk）→ 自旋，**不给任务体任何参数**。
7. 反事实 G 段：只改 0x135624 的 r4，停摆的全部症状一次消失 —— 说明「停摆」不是
   固件的稳态，是我们把设备摆在 0x84..0x2208 造成的。
""")


if __name__ == "__main__":
    main()
