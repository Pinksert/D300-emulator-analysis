# -*- coding: utf-8 -*-
"""exp_v53.py —— V53：0x175630 之前不是两道分支，是一整张事件位图。

V52 收尾留了一句：

  「下一道关不是 tick，是 0x175630 之前那两个分支（0x175406 beq /
    0x175418 的 cmp r4），以及 0x50000107 的 bit3 恒 0」

这一版把「下一道关」拆开，发现 V52 把结构看小了：

  · 0x175630 之前不是两道分支，是 0x1754FE..0x17562E 一整段按位派发
    （12 处 `ld #1012, r0` + and #mask + beq），每位对应一个动作；
  · 这段在基线、V52 精确补全、甚至「只把 bit9 置 1」里都一次没执行过。
    真正的总闸是上游 0x175406 beq —— 它读事件位图（r14+1012 =
    0x9F805AD0）的 bit31，而 bit31 由固件自己每轮写回，恒为 1；
  · 0x50000107 的 bit3 才是总开关：只把它读成 1（11 次），事件位图 bit31
    自己就清零了，sta_tsk(19) / sta_tsk(22) 都执行过 —— V51/V52 从未达到。

七段、六台机器（A 只读 ROM；B/G0、G1..G4 各一台，硬规矩 13）。

⚠️ 全部是反事实打桩：只改指定 ld 的**读返回值**，不写内存、不动时基。
   固件从没收到 tick（KI#191 未解），也没有器件真的把 bit3 拉高。
   命中 0 次也是结论（说明那条指令根本没执行过）。

跑：cd emu && python probe/exp_v53.py > ../results/frbox/v53.txt
"""

import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import blive, bready, bstatic, btick, bpatch
import fr_emu
import fr_dis

RUN = 400000
STEP = 20000
ROUNDS = 30
QS = 0x68000968                 # flag 12 对象控制块（V52）
FW = 0x9F5A0284                 # 结构体基址（V51）
EVT = 0x9F805AD0                # 事件位图 = 任务 7 栈帧 r14 + 1012
PIO = 0x50000107                # 轮询寄存器（V45 的通道 A +0x107）
GATE = 0x9F58D08F               # V48 的门字节

LD31 = 0x1753FC                 # ld #1012, r0 —— bit31 检查
LD9 = 0x175616                  # ld #1012, r0 —— bit9 检查（通向 0x175630）
PIO_PCS = (0x175882, 0x175896)  # 轮询函数里读 0x50000107 的两处
GATE_PCS = (0x120176, 0x175312) # 门字节读者：V48 / V53

# 12 处 `ld #1012, r0` 的 PC 与对应的位掩码（静态反汇编结果）
LD_BITS = [
    (0x175284, 0x00800000, "bit23"),
    (0x17536A, 0x20000000, "bit29"),
    (0x1753A6, 0x00000004, "bit2"),
    (0x1753FC, 0x80000000, "bit31"),
    (0x1754FE, 0x00000400, "bit10"),
    (0x175528, 0x00004000, "bit14"),
    (0x17554A, 0x02000000, "bit25"),
    (0x17556E, 0x00000008, "bit3"),
    (0x17558C, 0x00000800, "bit11"),
    (0x1755B6, 0x00001000, "bit12"),
    (0x1755F2, 0x00200000, "bit21"),
    (0x175616, 0x00000200, "bit9 → 0x175630"),
]

WATCH = [
    (0x17525A, "主循环体入口"),
    (0x1753FC, "ld#1012 bit31"),
    (0x175406, "beq_bit31"),
    (0x175418, "call 轮询"),
    (0x17541A, "轮询返回点"),
    (0x17541C, "beq→0x1754D0"),
    (0x17541E, "bit3=1分支"),
    (0x1754FE, "派发段入口"),
    (0x175616, "ld#1012 bit9"),
    (0x17561E, "beq 跳过启动"),
    (0x175630, "启动任务19"),
    (0x175636, "call:d 启动器"),
    (0x1962A6, "启动器序言"),
    (0x1647B8, "启动器调用者A"),
    (0x1708DE, "启动器调用者B"),
    (0x19636A, "sta19真函数序言"),
    (0x1963AE, "sta_tsk(19)"),
    (0x242B92, "sta_tsk(22)"),
    (0x1758EA, "轮询 ret"),
    (0x137EB6, "btsth 结构体"),
    (0x175312, "门字节读者V53"),
]

READ_WATCH = ([(EVT, pc) for pc, _m, _t in LD_BITS]
              + [(GATE, pc) for pc in GATE_PCS]
              + [(PIO, pc) for pc in PIO_PCS])


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def hx(v):
    return "0x%08X" % v


def beq_target(pc, rel9):
    """FR 的 beq rel9：位移是 rel9*2，基址 pc+2。纯函数。"""
    return pc + 2 + rel9 * 2


def cov_line(m):
    return "  ".join("%s=%s" % (t, "过" if m.cov.visited(a) else "·")
                     for a, t in WATCH)


def dump_reads(recs, tag):
    agg = collections.defaultdict(lambda: {"n": 0, "addrs": set(), "vals": set()})
    for pc, a, n, v in recs:
        d = agg[pc]
        d["n"] += 1
        d["addrs"].add(a)
        d["vals"].add(v)
    labels = {pc: t for pc, _m, t in LD_BITS}
    labels.update({0x120176: "V48门字节", 0x175312: "V53门字节",
                   0x175882: "轮询采样1", 0x175896: "轮询采样2"})
    print("  【%s】%d 次读 / %d 个 PC" % (tag, len(recs), len(agg)))
    for pc in sorted(agg):
        d = agg[pc]
        addrs = ",".join(hx(x) for x in sorted(d["addrs"]))
        vals = ",".join(hx(x) for x in sorted(d["vals"])[:6])
        if len(d["vals"]) > 6:
            vals += ",...(%d种)" % len(d["vals"])
        print("    0x%06X %-18s 次=%-4d 地址=%-12s 值=%s"
              % (pc, labels.get(pc, "?"), d["n"], addrs, vals))


def dump_writes(recs, tag):
    agg = collections.defaultdict(lambda: [0, set()])
    for pc, _a, _n, v in recs:
        agg[pc][0] += 1
        agg[pc][1].add(v)
    print("  【%s】%d 次写 / %d 个 PC" % (tag, len(recs), len(agg)))
    for pc in sorted(agg):
        cnt, vals = agg[pc]
        print("    PC=0x%06X 次=%-4d 值=%s"
              % (pc, cnt, ",".join(hx(x) for x in sorted(vals))))


def make_rules(mode):
    if mode == "G1":
        return {(EVT, LD31): bpatch.bit_clear(0x80000000)}
    if mode == "G2":
        return {(EVT, LD31): bpatch.bit_clear(0x80000000),
                (EVT, LD9): bpatch.bit_set(0x200)}
    if mode == "G3":
        return {(EVT, LD9): bpatch.bit_set(0x200)}
    if mode == "G4":
        return {(PIO, pc): bpatch.bit_set(0x08) for pc in PIO_PCS}
    return {}


def run_case(mode, tag, on_boot=None, rounds=ROUNDS, trace_entry=False):
    m = blive.boot()
    reads, writes = [], []
    hits = bpatch.patch_read(m.bus, make_rules(mode), watch=READ_WATCH, log=reads)
    bpatch.patch_write(m.bus, (EVT,), writes)
    m.v53_entry = []
    if trace_entry:
        # 只在 G4 装：包装 cpu.step 记关键 PC 命中前的最近 24 条 PC。
        # 纯观察，不写内存、不改时基、不替固件做决定。
        import collections as _coll
        recent = _coll.deque(maxlen=24)
        seen = _coll.Counter()
        targets = {0x1962A6, 0x19636A, 0x1963AE, 0x242B92}
        orig = m.cpu.step

        def _step():
            pc = m.cpu.pc
            recent.append(pc)
            if pc in targets and seen[pc] < 2:
                seen[pc] += 1
                m.v53_entry.append((pc, m.step_count, list(recent)))
            return orig()

        m.cpu.step = _step
    m.run(max_steps=RUN, spin_guard=False)
    boot_len = len(reads)
    boot_wlen = len(writes)
    if on_boot:
        on_boot(m, reads, writes)
    for _k in range(1, rounds + 1):
        got = btick.expire_strict(m)
        woke = btick.expire_object_queue(m, QS)
        if got or woke:
            bready.dispatch(m)
        m.run(max_steps=STEP, spin_guard=False)
    print("\n  【%s】%s" % (tag, mode))
    print("  " + cov_line(m))
    print("  打桩命中=%s"
          % ({"%s@0x%06X" % (("clr31" if a == EVT and pc == LD31 else
                             "bit9" if a == EVT and pc == LD9 else "pio3"), pc): n
              for (a, pc), n in sorted(hits.items())} or "{}"))
    print("  step=%d PC=0x%06X 事件位图=%s PIO=0x%02X 结构体=%s"
          % (m.step_count, m.cpu.pc, hx(m.bus.rd(EVT, 4)),
             m.bus.rd(PIO, 1), hx(m.bus.rd(FW, 4))))
    return m, hits, reads, writes, boot_len, boot_wlen


# ============================================================ A
hr("A  静态：0x175630 之前到底有什么")

sw, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base

print("服务号（bstatic 常量）：SET_FLG=%d / CLR_FLG=%d / WAI_FLG=%d"
      % (bstatic.SET_FLG, bstatic.CLR_FLG, bstatic.WAI_FLG))
print("  → 0xD0=set_flg / 0xD1=clr_flg / 0xD2=wai_flg；"
      "0x175408 是 clr_flg(12,0x7FFFFFFF)，不是 wai_flg。\n")

print("`ld #1012, r0` 的寻址（fr_emu.TABLE.decode）：")
d = fr_emu.TABLE.decode(rom, LD31 - base)
print("  0x%06X → %s" % (LD31, d))
print("  base=0x2000 ⇒ 地址 = r14 + %d*4 = r14 + %d"
      % (d[2]["disp10"], d[2]["disp10"] * 4))
print("  ⇒「#1012」不是立即数，是**帧内相对偏移**；事件位图不是外设。\n")

print("关键分支与跳转目标（beq rel9：pc+2+rel9*2）：")
for pc, label in [(0x175406, "bit31 为 0 → 派发段入口"),
                  (0x17541C, "轮询返回 0 → 回主循环"),
                  (0x17561E, "bit9 为 0 → 跳过启动任务 19")]:
    dd = fr_emu.TABLE.decode(rom, pc - base)
    rel9 = dd[2]["rel9"]
    print("  0x%06X beq rel9=%-4d → 0x%06X   %s"
          % (pc, rel9, beq_target(pc, rel9), label))

print("\n0x1754FE..0x17562E 是按位派发段，共 12 处 `ld #1012`：")
for pc, mask, label in LD_BITS:
    mark = "  ← 闸门" if pc == LD31 else ("  ← 通向 0x175630" if pc == LD9 else "")
    print("  0x%06X  and #%s  %s%s" % (pc, hx(mask), label, mark))

print("\n0x50000107 的初始化（唯一静态写点）：")
off, end = 0x0402A2 - base, 0x0402B6 - base
while off < end:
    line, size = fr_dis.line(rom, off, base)
    print("  " + line)
    off += size
print("  ⇒ 0x0402AA `stb #0x12` 把 bit3 初始化成 0；"
      "writers_of(0x50000107) 全 ROM 只此一处。")

print("\nV48 门字节口径复核：")
lds = sorted(sw.ldi32.get(GATE, ()))
print("  ROM 中 ldi:32 #%s 的加载点 = %d 处" % (hx(GATE), len(lds)))
print("  其中 0x120170 → 0x120176（V48 记的读者）、0x17530C → 0x175312（V53 新见）")
print("  writers_of = %s  ⇒ V25/V48 的「唯一写者」链条不动"
      % ", ".join("0x%06X %s" % (pc, mn) for pc, mn in sw.writers_of(GATE)))
print("  ⚠️ V48「读者只有 0x120176」是**基线动态**口径，不是静态普查结论。")

print("\nflag 12 的静态调用点：wai_flg(12,·) %d 处 / set_flg(12,·) %d 处"
      % (len(sw.flag_waits(12)), len(sw.flag_sets(12))))
print("  KI#194 残留的答案 —— 0x175908 系列函数一进门就等铃再擦铃：")
for c in sw.flag_waits(12):
    if 0x175900 <= c.addr <= 0x175B00:
        print("    %s" % c)
print("  0x17591C wai_flg(12,1,2) → 0x17592C clr_flg(12,~1) → "
      "遍历 0x9F59F7C8 回调表（V47/V48 的 flag 6 同构）。")

print("\n0x1962A6（0x175630 加载的启动器）的调用者窗口查询：")
for pc, imm, nx in sw.callers_strict(0x1962A6, window=8):
    print("  0x%06X  imm=0x%06X  下一条=%s" % (pc, imm, nx))

print("\nsta_tsk(19) 的函数归属复核（V53 旁路更正）：")
s19 = [c for c in sw.of(-23) if c.r4 == 19]
print("  sta_tsk(19) 全 ROM 调用点 = %s（%d 处）"
      % (", ".join("0x%06X" % c.addr for c in s19), len(s19)))
print("  enclosing(0x1963AE) = 0x%06X —— 只是「最近的 enter」，粗粒度归属"
      % sw.enclosing(0x1963AE))
print("  但 0x19636A 起是另一个函数（st r8 / st dr1 序言，没有 enter）：")
_off, _end = 0x19636A - base, 0x196380 - base
while _off < _end:
    _line, _size = fr_dis.line(rom, _off, base)
    print("    " + _line)
    _off += _size
print("  ⇒ 无 enter 的小函数会被 enclosing() 归到前一个 enter 名下。")
print("    V51 把 0x1962AA 叫「任务 19 启动器」应理解为 0x175630 调用的那一支；")
print("    sta_tsk(19) 的调用点其实在后面的 0x19636A 函数里。")

# ============================================================ B
hr("B  事件位图的身份：谁在读、谁在写、值是什么")


def boot_dump(m, reads, writes):
    print("基线（boot + 40 万步）：")
    print("  " + cov_line(m))
    dump_reads(reads, "基线")
    dump_writes(writes, "基线")


m_g0, hits0, reads0, writes0, boot_len0, boot_wlen0 = run_case(
    "G0", "B/G0 对照（V52 F1 口径，只读记录、不打桩）", on_boot=boot_dump)
dump_reads(reads0[boot_len0:], "G0 补全后 30 轮累计")
dump_writes(writes0[boot_wlen0:], "G0 补全后 30 轮累计")
print("""
⇒ 事件位图真实地址 = 0x9F805AD0（任务 7 栈帧 r14+1012），
  唯一写点 0x175274 `st r4, r13` —— 值来自 0x175270 `int #0x40`
  （服务号 0xFFFFFF56）的返回值，基线 0x80000000、补全后
  0x80000001 / 0xFFFFFFFF，**bit31 恒为 1**。
  12 处 `ld #1012` 里只有 0x175284 / 0x17536A / 0x1753A6 /
  0x1753FC 四处真读到事件位图，⇒ 0x1754FE 之后的派发段
  （含 0x175616）**一次都没执行**。
  0x1758EA ret 在轮 8 起执行过 ⇒ V52「轮询函数返回了」得到直接确认，
  0x17541A 确是返回点，不是延迟槽。""")

# ============================================================ C
hr("C  bit31 是总闸：只置 bit9 完全没用")

m_g3, hits3, _r3, _w3, _b3, _bw3 = run_case("G3", "G3 只置 bit9（bit31 仍 1）")
print("""
⇒ 打桩命中 0 次，G3 与 G0 逐项相同：0x175616 根本没执行过，
  给它置 bit9 连被读到的机会都没有。V52 说的「下一道关是 0x175630
  之前那两个分支」不成立 —— 卡在更上游的 0x175406 beq。""")

# ============================================================ D
hr("D  拆开两道闸：清 bit31 → 派发段；再置 bit9 → 0x175630")

m_g1, hits1, _r1, _w1, _b1, _bw1 = run_case("G1", "G1 只清 bit31")
m_g2, hits2, _r2, _w2, _b2, _bw2 = run_case("G2", "G2 清 bit31 + 置 bit9")
print("""
⇒ G1：0x175406 一跳，0x1754FE..0x17562E 整段派发**首次执行**，
  0x175616 也执行了；但 bit9=0 ⇒ 0x17561E beq 跳过 0x175630。
⇒ G2：两处都打桩，0x175630 执行过（V51/V52 从未达到），
  但 sta_tsk(19) / sta_tsk(22) 仍没执行。启动器到底有没有被调到、
  内部哪一步停住，看 WATCH 里 0x175636 / 0x1962A6 两项。""")

# ============================================================ E
hr("E  总开关其实在 0x50000107 的 bit3")

m_g4, hits4, _r4, _w4, _b4, _bw4 = run_case(
    "G4", "G4 只把轮询读到的 bit3 置 1", trace_entry=True)
print("""
⇒ 只改轮询函数两次采样（0x175882 / 0x175896）的读返回值，
  事件位图自己变成 0x00000014（bit31 自动清零），
  0x17541E 分支、派发段、sta_tsk(19)、sta_tsk(22) **全执行过**。
  ⇒ bit3 不是「另一道并列的关」，它是让固件自己改事件位图的上游条件。
  但 0x9F5A0284 仍是 0、0x137EB6 没执行、末 PC 仍是空闲 0x0407DC
  ⇒ 启动任务 ≠ 画面链条闭合（V50 的 bit3 关卡还在更下游）。""")

# ============================================================ F
hr("F  对账与边界")

print("五组同口径（boot 40 万步 + 精确补全 30 轮，only=0x68000968）：\n")
print("  %-6s %-8s %-8s %-8s %-8s %-8s %-8s" %
      ("组", "派发段", "0x175630", "sta19", "sta22", "bit3分支", "结构体"))
for tag, m in [("G0", m_g0), ("G1", m_g1), ("G2", m_g2),
               ("G3", m_g3), ("G4", m_g4)]:
    def v(a):
        return "过" if m.cov.visited(a) else "·"
    print("  %-6s %-8s %-8s %-8s %-8s %-8s %-8s"
          % (tag, v(0x1754FE), v(0x175630), v(0x1963AE), v(0x242B92),
             v(0x17541E), "0" if m.bus.rd(FW, 4) == 0 else "非0"))

print("""
能说 / 不能说：
  · 能说：0x175630 之前是 12 处按位派发，不是两道分支（V52 的结论收窄）。
  · 能说：事件位图 bit31 恒 1 是固件自己每轮写回的，派发段因此零执行。
  · 能说：G4 里 sta_tsk(19)/sta_tsk(22) 执行过，是 V51/V52 从未达到的位置。
  · 不能说：固件真的把 0x50000107 的 bit3 置过 1（全是读打桩，且唯一
    静态写点 0x0402AA 写的是 0x12）。
  · 不能说：画面出来了 —— 0x9F5A0284 仍为 0、0x137EB6 没执行。
  · 不能说：0x175630 是 sta_tsk(19) 的必经之路（G4 走的是 0x19636A 那一支，
    见下面「G4 旁路对账」；enclosing() 把两个函数混了）。
  · 不能说：真实 tick 源找到了（KI#191 仍未解）；全部补全都是反事实。

口径纪律：
  · 五台机器各跑一遍，没有复用 cov（硬规矩 13）。
  · 打桩只改 (地址, PC) 精确匹配的读返回值，不写内存、不动时基；
    命中 0 次（G3）与命中 11 次（G4）都是结果，不是失败。
  · G0 是 V52 F1 的逐字复现口径，用于把新数字钉在同一把尺子上（硬规矩 2）。
""")

# --- F2：G4 那条「没过 0x175630 却到了 sta_tsk(19)」的旁路 ---
print("""
G4 旁路对账（0x1963AE 为什么能在没过 0x175630 时执行）：
  · 静态：sta_tsk(19) 全 ROM 只有 0x1963AE 一处；enclosing() 报 0x1962AA，
    但那是「最近的 enter」—— 0x19636A 起的另一个函数没有 enter。""")
_g4_ran = []
_off, _end = 0x1962A6 - base, 0x1963B6 - base
while _off < _end:
    _a = base + _off
    _line, _size = fr_dis.line(rom, _off, base)
    if m_g4.cov.visited(_a):
        _g4_ran.append(_a)
    _off += _size
print("  · 动态（G4 的 cov，同口径只读标记）：")
print("      0x1962A6 那一支（含 enter 0x1962AA / btsth 结构体）跑过 %d 条 PC"
      % len([a for a in _g4_ran if a < 0x19636A]))
print("      0x19636A 那一支（含 sta_tsk(19)）        跑过 %d 条 PC"
      % len([a for a in _g4_ran if a >= 0x19636A]))
print("  · G4 前驱轨迹（最近 16 条，末项即命中点）：")
for _pc, _n, _seq in m_g4.v53_entry[:4]:
    print("      0x%06X @step=%d  %s"
          % (_pc, _n, " ".join("0x%06X" % x for x in _seq[-16:])))
print("""  ⇒ G4 从内核派发 0x0406C2..0x0406E2 恢复任务上下文后，直接落在
    0x19636A 函数体中部（0x1963A4）顺序走到 sta_tsk(19)；0x1962A6
    整支（含 0x175630 → call:d 启动器）一次都没跑。
  ⇒ 0x175630 不是 sta_tsk(19) 的必经之路；V51「0x1962AA = 任务 19
    启动器」是 enclosing() 粗粒度归属造成的混称，要收窄。""")

# ============================================================ G
hr("G  结论与复现")

print("""1. 0x175630 之前不是「0x175406 beq / 0x175418 cmp r4 两道分支」。
   0x1754FE..0x17562E 是一整段按位派发：12 处 `ld #1012, r0`（实为
   r14+1012 的帧内槽）+ and #mask + beq，每位对应一个动作，bit9
   （0x200）对应启动任务 19。
2. 真正的总闸是 0x175406 beq：它要 bit31 == 0 才跳进派发段；而事件位图
   0x9F805AD0 的 bit31 由 0x175274（0x175270 int #0x40 的返回值）
   每轮写回，恒为 1 ⇒ 派发段一次都没执行（G3 的 0 命中是硬证据）。
3. KI#194 残留结清：任务 7 等 flag 12 的静态位置是 0x175908 系列函数
   （0x17591C / 0x175974 / 0x1759C0 / 0x175A34 的 wai_flg(12,1,2)），
   模式 = 等铃 → clr_flg(12,~1) 擦铃 → 遍历 0x9F59F7C8 回调表。
4. 0x50000107 的 bit3 由 0x0402AA `stb #0x12` 初始化，全 ROM 唯一写点，
   恒 0 是常量，不是 V45 器件模型漏建的状态位。
5. 反事实因果：只清 bit31 → 派发段首次执行；再置 bit9 → 0x175630 执行；
   只把轮询的 bit3 读成 1 → 事件位图 bit31 自清 → sta_tsk(19)/sta_tsk(22)
   都执行过。但结构体仍 0、末 PC 仍是空闲 ⇒ 画面链条仍未闭合。
6. V48「门字节读者只有 0x120176」是基线动态口径：静态 ldi:32 加载点
   19 处，V53 补全后 0x175312 也读了 22 次；写点仍 2 处。
7. G4 的 sta_tsk(19) 不是从 0x175630 来的：0x1963AE 位于 0x19636A
   那个无 enter 的小函数里，enclosing() 粗粒度归到了 0x1962AA。
   G4 从内核派发恢复任务后直接落在该函数体中部，0x1962A6 整支没跑
   ⇒ V51「0x1962AA = 任务 19 启动器」的叫法要收窄（见 F2 旁路对账）。

复现：
  cd emu && python probe/exp_v53.py > ../results/frbox/v53.txt
  cd emu && python frbox_selftest.py

⚠️ A 段只读 ROM、没 boot；B/G0 带只读 hook 但不打桩；G1..G4 各一台。
⚠️ 全部补全沿用 V52 的严格到期 + only=0x68000968，不引入新口径。
""")
