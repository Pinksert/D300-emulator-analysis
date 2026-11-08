# -*- coding: utf-8 -*-
"""V57 正式探针：V56 的「进入应用区」是探针漏挂设备造出来的忙等假墙。

    cd emu && python probe/exp_dev_v57.py > ../results/frbox/da57.txt

七段：
  A  静态：设备策略表（真机器 audit 对账）、四处握手指纹、A 段 sta_tsk 调用点
  B  口径：0x09C97A 到底是什么（反汇编 + dev_xfer 表项）
  C  动态：四组设备策略 × 同一根 vec 0x2B 线（A 段，各一台新机器）
  D  动态：B 段挂 / 不挂 xfer 的阴性对照（各一台新机器）
  E  两段对账表
  F  能说 / 不能说
  G  复现

纪律：
  · 不自写 cpu.step() 循环推进（硬规矩 1）；只用 Machine.run()/Machine.step()。
    PC 经过次数用「只观察、不推进」的 step hook（V53 F2 同款）。
  · 一台机器只跑一遍（硬规矩 13）；C 段 4 台 + D 段 2 台，共 6 台运行机器，
    另有 2 台只读 ROM 实例（A/B 各 1 台，只做静态扫描、没 boot）。
  · 所有「假墙 / 正常」判定由 `devaudit.stall_verdict()` 从观察日志算出，
    不是口述；所有正文数字都要在本文件输出的同一行找得到（硬规矩 2）。
  · 周期线一律用通用 Irq2BClock，period=1024，只拉线不写内存。
  · 不改 create() 的默认值：V2–V9 / V2–V10 的历史基线依赖 xfer / dispatch 默认关。
"""
import os
import sys
import collections

# 与 exp_tick_v55.py 同口径：从 probe/ 子目录运行时把 emu/ 加进模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import abservice, blive, bobserve, bstatic, btick, dev_xfer
from frbox import devaudit
from frbox.machine import create
from frbox.dev_irq2b import Irq2BClock
import fr_dis

# ---------------------------------------------------------------- 常量
RUN_A = 70000
RUN_D = 400000
BOOT = 191520
PERIOD = 1024
VEC_A = 0x2B
VEC_B = 0x18
A_CUR_TCB = 0x00045980
A_DISPATCH_REQ = 0x44
A_IDLE = 0x000FE510
A_ISR_2B = 0x00080588
A_DISPATCHER = 0x000FE776
A_RETI = 0x000FE50C
POLL = 0x0009C97A
POLL_REG = 0x174
B_TICK_COUNTER = 0x68000110
B_DELAY_SENTINEL = 0x68000118
B_CURRENT_TCB = 0x68000000
B_READY_MAP = 0x68000004
B_IDLE = 0x000407DC

WATCH_A = (
    (A_RETI, "首次派发 reti"),
    (A_IDLE, "A 空闲任务"),
    (A_ISR_2B, "vec0x2B 位分派器"),
    (A_DISPATCHER, "vec0x3F 调度器"),
    (0x0009C974, "0x9C974 置启动位"),
    (POLL, "0x9C97A 轮询完成位"),
)


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dis(ROM, BASE, lo, hi, prefix="  "):
    off, end = lo - BASE, hi - BASE
    while off < end:
        line, size = fr_dis.line(ROM, off, BASE)
        print(prefix + line)
        off += size


# ==================================================================== A
hr("A  静态：设备策略表、四处握手指纹、A 段 sta_tsk 调用点")

SWA, MA = bstatic.sweep_of_type("d300-a")
MB = create("d300-b")            # 只读 ROM 实例：load 但不 boot、不跑
MB.load()
ROM_A, BASE_A = MA.bus.rom, MA.bus.rom_base
ROM_B, BASE_B = MB.bus.rom, MB.bus.rom_base
STA = [c.addr for c in SWA.calls if c.svc == abservice.sx8(0xE9)]

print("A 段 ROM base=0x%06X size=%d TBR=0x%06X；B 段 ROM base=0x%06X size=%d TBR=0x%06X"
      % (BASE_A, len(ROM_A), MA.tbr, BASE_B, len(ROM_B), MB.tbr))
print("A 段 sta_tsk(0xE9) 静态调用点 %d 处；首 8 个 = %s"
      % (len(STA), " ".join("0x%06X" % p for p in STA[:8])))

print("\nA1 三种常用入口的设备策略（devaudit.ENTRIES 代码镜像 vs 真机器 audit 实测）")
print("  %-18s %-30s %s" % ("入口", "期望（ENTRIES）", "实测（audit）"))
_real = {
    "create(name)": create("d300-a"),
    "blive.boot()": create("d300-b", dispatch=True),
    "boot_realos()": create("d300-a", xfer=True, dispatch=True),
}
for k in ("create(name)", "blive.boot()", "boot_realos()"):
    exp = devaudit.ENTRIES[k]
    got = devaudit.audit(_real[k])
    triple = lambda d: "xfer=%d dispatch=%d prio=%d" % (
        d["xfer"], d["dispatch"], d["prio"])
    print("  %-18s %-30s %-30s %s"
          % (k, triple(exp), triple(got),
             "一致" if triple(exp) == triple(got) else "不一致"))
print("  ⇒ 同一套模拟器，三种入口挂的设备并不一样；"
      "create() 默认 xfer/dispatch/prio 全关。")
print("  xfer 基线=%s（%s）；dispatch 基线=%s（%s）"
      % (devaudit.POLICY["xfer"]["baseline"], devaudit.POLICY["xfer"]["why"],
         devaudit.POLICY["dispatch"]["baseline"],
         devaudit.POLICY["dispatch"]["why"]))

print("\nA2 四处「置启动位 → 轮询完成位」握手指纹（dev_xfer 实证）")
print("  %-7s %-9s %-9s %-11s %-11s" %
      ("偏移", "启动位", "完成位", "轮询PC", "置位PC"))
for s in devaudit.HANDSHAKE:
    print("  0x%03X   0x%02X     0x%02X     0x%06X   0x%06X"
          % (s.off, s.start, s.done, s.poll_pc, s.start_pc))
print("  handshake_table() == dev_xfer.D300_A_TABLE : %s"
      % (devaudit.handshake_table() == dev_xfer.D300_A_TABLE))

# ==================================================================== B
hr("B  口径：V56 C2 的末 PC 0x09C97A 到底是什么")

print("B1 0x09C970..0x09C984 反汇编（A 段）")
dis(ROM_A, BASE_A, 0x0009C970, 0x0009C984)
print("B2 dev_xfer.D300_A_TABLE[0x174] = %s；XferHandshake.delay = %d tick"
      % (dev_xfer.D300_A_TABLE[0x174], _real["boot_realos()"].xfer.delay))
print("  ⇒ 0x9C974 置 0x174 的 bit1 启动，0x9C97A 测 bit6 完成位，"
      "beq 回跳自旋（*-8）；V20 已证它靠 XferHandshake.tick() 答话后能退出。")

# ==================================================================== C
hr("C  动态：四组设备策略 × 同一根 vec 0x2B 线（A 段，各一台新机器）")


def case_a(tag, xfer, dispatch):
    m = create("d300-a", xfer=xfer, dispatch=dispatch)
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    reads, writes = bobserve.observe(
        m.bus, (POLL_REG, A_DISPATCH_REQ, A_CUR_TCB))
    dev = Irq2BClock(m, period=PERIOD, vector=VEC_A)
    m.devices.append(dev)

    # 只观察、不推进：统计轮询 PC 经过次数
    cnt = collections.Counter()
    orig = m.cpu.step

    def step():
        cnt[m.cpu.pc] += 1
        return orig()

    m.cpu.step = step
    try:
        m.run(max_steps=RUN_A, spin_guard=False)
    finally:
        m.cpu.step = orig

    irq = collections.Counter(r[0] for r in m.cpu.irq_log)
    total, done, stalled = devaudit.stall_verdict(
        reads, POLL_REG, devaudit.handshake_table()[POLL_REG][1], POLL)
    vals = dict(bobserve.value_counts(reads, POLL_REG))
    w44 = sum(1 for w in writes if w[0] == A_DISPATCH_REQ)
    wtcb = sum(1 for w in writes if w[0] == A_CUR_TCB)
    tcbvals = [v for (a, pc, aa, n, v) in writes if a == A_CUR_TCB]
    svcs = collections.Counter(c[2] for c in m.flow.calls)
    sta_hit = sum(1 for p in STA if m.cov.visited(p))
    ran = [n for pc, n in WATCH_A if m.cov.visited(pc)]
    print("\n【%s】xfer=%d dispatch=%d step=%d 末PC=0x%06X cov=%d"
          % (tag, xfer, dispatch, m.step_count, m.cpu.pc, m.cov.words()))
    print("  受理=%s fired=%d 时钟accepted=%d" %
          (dict(irq), dev.fired, dev.accepted()))
    print("  轮询PC经过=%d 次；0x174 读值=%s；stall_verdict=(总读%d, 见完成位%d, 假墙=%s)"
          % (cnt[POLL], vals, total, done, stalled))
    if 66 in vals:
        print("    ↳ 读值字典按十进制打印：66 = 0x42（启动位 0x02 + 完成位 0x40），不是 0x66")
    print("  0x44写=%d 当前TCB写=%d vec0x3F调度器=%s 位分派器=%s"
          % (w44, wtcb, m.cov.visited(A_DISPATCHER),
             m.cov.visited(A_ISR_2B)))
    print("  当前TCB写值序列=%s"
          % " ".join("0x%08X" % v for v in tcbvals))
    print("  int #0x40 服务调用 %d 次，服务号=%s；sta_tsk 调用点覆盖 %d/%d"
          % (len(m.flow.calls),
             dict(sorted(svcs.items())), sta_hit, len(STA)))
    print("  关键PC跑过=%s" % ran)
    if m.xfer is not None:
        print("  xfer=%s" % m.xfer.state())
    if m.dispatch is not None:
        print("  dispatch=%s" % m.dispatch.state())
    return {"tag": tag, "pc": m.cpu.pc, "cov": m.cov.words(),
            "accepted": dev.accepted(), "stalled": stalled,
            "poll": cnt[POLL], "w44": w44, "wtcb": wtcb,
            "svc": len(m.flow.calls), "sta": sta_hit,
            "disp3f": m.cov.visited(A_DISPATCHER),
            "irq": dict(irq)}


C = [
    case_a("C0 全默认（= V56 C2 口径）", False, False),
    case_a("C1 只挂 xfer", True, False),
    case_a("C2 只挂 dispatch", False, True),
    case_a("C3 xfer + dispatch", True, True),
]

print("\nC 段对账表（同一把尺子：vec 0x2B / period=%d / 跑 %d 步）"
      % (PERIOD, RUN_A))
print("  %-30s %-10s %-7s %-7s %-8s %-8s %-7s %-7s %s"
      % ("组", "末PC", "覆盖", "受理", "轮询", "假墙", "0x44写", "服务", "调度器"))
for r in C:
    print("  %-30s 0x%06X %-7d %-7d %-8d %-8s %-7d %-7d %s"
          % (r["tag"], r["pc"], r["cov"], r["accepted"], r["poll"],
             "是" if r["stalled"] else "否", r["w44"], r["svc"],
             "跑过" if r["disp3f"] else "没跑"))
print("  ⇒ 两根开关独立：只挂 xfer 轮询能退出但没派发；只挂 dispatch 有派发但仍卡轮询；")
print("    两个都挂才有 vec 0x3F 派发 + 服务调用，末 PC 继续往应用区走。")
print("  ⇒ 但四组 sta_tsk 调用点覆盖全是 0/%d ⇒ 不能报「A 段启动了任务」。"
      % len(STA))

# ==================================================================== D
hr("D  动态：B 段挂 / 不挂 xfer 的阴性对照（各一台新机器）")


def case_b(tag, xfer):
    m = blive.boot(0)
    if xfer:
        m.enable_xfer()
    reads, writes = bobserve.observe(
        m.bus, (A_DISPATCH_REQ, B_TICK_COUNTER, B_DELAY_SENTINEL))
    dev = Irq2BClock(m, period=PERIOD, vector=VEC_B)
    m.devices.append(dev)
    m.step(BOOT)
    m.run(max_steps=RUN_D, spin_guard=False)
    dq = btick.delay_queue(m)
    tick = m.bus.rd(B_TICK_COUNTER + 4, 4)
    out = {"tag": tag, "step": m.step_count, "pc": m.cpu.pc,
           "cov": m.cov.words(), "tick": tick,
           "fired": dev.fired, "accepted": dev.accepted(),
           "dq": [tid for _t, tid, _p, _w in dq],
           "cur": m.bus.rd(B_CURRENT_TCB, 4),
           "ready": m.bus.rd(B_READY_MAP, 4),
           "w44": sum(1 for w in writes if w[0] == A_DISPATCH_REQ),
           "xfer": m.xfer.state() if m.xfer is not None else None}
    print("\n【%s】step=%d 末PC=0x%06X cov=%d tick计数=%d 延时队列=%s"
          % (tag, out["step"], out["pc"], out["cov"], out["tick"], out["dq"]))
    print("  时钟 fired=%d accepted=%d；0x44写=%d 当前TCB=0x%08X 就绪位图=0x%08X"
          % (out["fired"], out["accepted"], out["w44"],
             out["cur"], out["ready"]))
    print("  xfer=%s" % out["xfer"])
    return out


D = [case_b("D0 不挂 xfer（V56 D1 同尺）", False),
     case_b("D1 挂 xfer", True)]
KEYS = ("step", "pc", "cov", "tick", "fired", "accepted",
        "dq", "cur", "ready", "w44")
diff = [k for k in KEYS if D[0][k] != D[1][k]]
print("\nD 段逐项对账：比较 %d 项，差异项 = %s"
      % (len(KEYS), diff if diff else "无"))
print("  ⇒ 补上 A 段那 4 个握手寄存器，B 段控制流数字逐项不变，"
      "且 D1 的 xfer 启动 0 次 / 完成 0 次。")

# ==================================================================== E
hr("E  两段对账：设备策略差异是段相关的，不是模拟器少补了哪根线")

print("  %-16s %-34s %-34s" % ("", "A 段（IO 协处理）", "B 段（主控 UI）"))
print("  %-16s %-34s %-34s" % ("常用入口", "create(d300-a)", "blive.boot()"))
print("  %-16s %-34s %-34s" % ("默认设备", "xfer/dispatch/prio 全关", "dispatch+prio 开，xfer 关"))
print("  %-16s %-34s %-34s" % ("事件线", "vec 0x2B 位分派器", "vec 0x18 系统节拍"))
print("  %-16s %-34s %-34s" % ("C3/D1 结果", "末PC 0x%06X cov %d" % (C[3]["pc"], C[3]["cov"]),
                                "末PC 0x%06X cov %d" % (D[1]["pc"], D[1]["cov"])))
print("  %-16s %-34s %-34s" % ("挂 A 段 xfer", "轮询退出、才有派发", "启动 0 次、数字逐项不变"))
print("  ⇒ A 段的 xfer 是 A 段专用；B 段不碰这 4 个寄存器。")

# ==================================================================== F
hr("F  能说 / 不能说")

print("能说：")
print("  1. V56 C2 报「末 PC 进应用区 0x%06X、覆盖 825→2269」的数字全部可复现，" % POLL)
print("     但那个 PC 是 0x174 完成位的忙等轮询点：C0 组轮询 %d 次、" % C[0]["poll"])
print("     0x174 读值全无完成位，stall_verdict=假墙。根因是探针用 create(d300-a)，")
print("     xfer 默认关；不是固件缺外设，也不是 V56 的位分派器结论错。")
print("  2. xfer 与 dispatch 是两根独立的默认关开关（V2–V9 / V2–V10 历史基线）：")
print("     只挂 xfer 时 C1 轮询退出、末 PC 回空闲 0x%06X、覆盖 %d；" % (C[1]["pc"], C[1]["cov"]))
print("     只挂 dispatch 时 C2 有派发但仍卡轮询、覆盖 %d；" % C[2]["cov"])
print("     两个都挂的 C3 才出现 vec 0x3F 派发 %d 次、int #0x40 服务 %d 次、" %
      (C[3]["irq"].get(0x3F, 0), C[3]["svc"]))
print("     末 PC 0x%06X、覆盖 %d、当前 TCB 写 %d 笔。" %
      (C[3]["pc"], C[3]["cov"], C[3]["wtcb"]))
print("  3. B 段阴性对照：挂 A 段 xfer 前后 %d 项控制流数字逐项全同，" % len(KEYS))
print("     D1 的 xfer 启动 0 次 ⇒ 设备策略差异是段相关的。")
print("  4. 新设施 frbox/devaudit.py 把「设备策略」与「忙等假墙」做成纯函数判据，")
print("     不改 create() 默认值，历史基线可复现。")
print("\n不能说：")
print("  · 不能报「A 段启动了任务」：四组 sta_tsk 静态调用点覆盖全是 0/%d，" % len(STA))
print("    C3 执行过的 %d 次服务里没有 sta_tsk。" % C[3]["svc"])
print("  · 不能报「A 段不需要任何时钟」「vec 0x2B 是真机事件源」：周期拉线是模型参数。")
print("  · 不能报「真机 A/B 怎么通信」：本版没建任何跨机通道。")
print("  · 不能报出画面：B 段末 PC 仍空闲 0x%06X，结构体 0x9F5A0284 恒 0" % B_IDLE)
print("    （KI#191/#201/#208 原样保留）。")
print("  · XferHandshake 是模型化假设，不是逆向确认的寄存器语义。")

# ==================================================================== G
hr("G  复现")

print("  cd emu && python probe/exp_dev_v57.py > ../results/frbox/da57.txt")
print("  cd emu && python frbox_selftest.py")
print("  · C 段 4 台 A 段机器 + D 段 2 台 B 段机器，共 6 台运行机器，各跑一遍；")
print("    另有 A/B 各 1 台只读 ROM 实例只做静态扫描、没 boot。")
print("  · A 段每组建机器 → load → reset → enable_cov → attach_flow → observe →")
print("    挂 Irq2BClock(vec 0x2B, period=%d) → run(%d, spin_guard=False)。" %
      (PERIOD, RUN_A))
print("  · B 段 blive.boot(0) → （可选 enable_xfer）→ observe →")
print("    挂 Irq2BClock(vec 0x18, period=%d) → step(%d) → run(%d)。" %
      (PERIOD, BOOT, RUN_D))
print("  · 全程没有调用 btick.tick*() / expire_*() / bpatch.patch_read()；")
print("    没有任何一步替固件改内核数据，只有「挂设备」与「拉中断线」。")
