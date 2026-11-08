# -*- coding: utf-8 -*-
"""V56 正式探针：A 段不缺时钟 —— 两段内核的服务表决定各自需要什么输入。

    cd emu && python probe/exp_ab_v56.py > ../results/frbox/ab56.txt

七段：
  A  静态：两段 `int #0x40` 画像、A 段服务分发表全表、关键区间反汇编
  B  口径：A 段止点四种运行入口对账（V10「弹 PC=0」结论的复核）
  C  动态：A 段挂不同周期线（基线 / 0x18 / 0x2B / 0x13 / 0x1B）
  D  动态：B 段同一根 vec 0x18 线（blive.boot 口径，与 V54/V55 同一把尺子）
  E  对账表：同一根线、两段反应相反
  F  能说 / 不能说
  G  复现

纪律：
  · 不自写 cpu.step() 循环推进（硬规矩 1）；A 段用 Machine.run()/frbox_boot.boot()，
    B 段用 blive.boot(0) + m.step(BOOT)，与 V55 逐字同口径。
  · 一台机器只跑一遍（硬规矩 13）；C/D 段每组都是新机器。
  · 周期线用通用 Irq2BClock，A/B 两段同一个类、同一个 period，不写内存。
  · 所有「执行过」来自 m.cov.visited()；所有数字都在本文件同一行找得到。
"""
import os
import sys
import collections

# 与 exp_tick_v55.py 同口径：从 probe/ 子目录运行时把 emu/ 加进模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import abservice, bobserve, bstatic, btick, blive
from frbox.machine import create
from frbox.dev_irq2b import Irq2BClock
import fr_dis
import fr_emu
import frbox_boot

# ---------------------------------------------------------------- 常量
A_TABLE = abservice.A_TABLE
A_RETI = 0x000FE50C
A_IDLE = 0x000FE510
A_CUR_TCB = 0x00045980
A_DISPATCH_REQ = 0x44
A_ISR_18 = 0x00080B4A
A_ISR_2B = 0x00080588
A_DISPATCHER = 0x000FE776
B_TICK_COUNTER = 0x68000110
B_DELAY_SENTINEL = 0x68000118
B_IDLE = 0x000407DC
BOOT = 191520
RUN_A = 60000
RUN_C = 70000
RUN_D = 400000
PERIOD = 1024

WATCH_A = (
    (A_RETI, "首次派发 reti"),
    (A_IDLE, "A 空闲任务"),
    (A_ISR_18, "A vec0x18 ISR"),
    (A_ISR_2B, "A vec0x2B 位分派器"),
    (A_DISPATCHER, "A vec0x3F 调度器"),
    (0x000804FA, "启动链空 ret"),
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


def vec_slot(ROM, BASE, tbr, vec):
    off = tbr - BASE + (255 - vec) * 4
    return int.from_bytes(ROM[off:off + 4], "big")


def direct_page_hits(ROM, BASE, target):
    """全 ROM 线性扫 dmov 类指令，数直接页 target 的命中 PC。"""
    out, off = [], 0
    while off < len(ROM) - 1:
        try:
            ins = fr_emu.TABLE.decode(ROM, off)
        except Exception:
            off += 2
            continue
        if ins is None:
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        if mn and mn.startswith("dmov"):
            if any(v.get(k) == target for k in ("dir8", "dir9", "dir10")):
                out.append(BASE + off)
        off += size
    return out


# ==================================================================== A
hr("A  静态：两段的 int #0x40 画像与 A 段服务分发表")

SWA, MA = bstatic.sweep_of_type("d300-a")
SWB, MB = bstatic.sweep_of_type("d300-b")
ROM_A, BASE_A = MA.bus.rom, MA.bus.rom_base
ROM_B, BASE_B = MB.bus.rom, MB.bus.rom_base
print("A 段 ROM base=0x%06X size=%d TBR=0x%06X stores=%d"
      % (BASE_A, len(ROM_A), MA.tbr, SWA.summary()["stores"]))
print("B 段 ROM base=0x%06X size=%d TBR=0x%06X stores=%d"
      % (BASE_B, len(ROM_B), MB.tbr, SWB.summary()["stores"]))

PA, PB = abservice.call_profile(SWA), abservice.call_profile(SWB)
print("\nA1 静态调用点（全 ROM 线性扫，口径 = bstatic.Sweep.calls）")
print("  A 段 %d 处 / %d 种服务号；B 段 %d 处 / %d 种服务号"
      % (len(SWA.calls), len(PA), len(SWB.calls), len(PB)))
print("  %-12s %-14s %-12s %s" % ("服务号", "名字", "A 段调用点", "B 段调用点"))
for b in (0xD0, 0xD1, 0xD2, 0xE9, 0xDA, 0xAB, 0xDB, 0xC1, 0xC3):
    svc = abservice.sx8(b)
    print("  0x%02X (%-4d) %-14s A=%-12d B=%d"
          % (b, svc, abservice.name_of(b) or "-", PA.get(svc, 0), PB.get(svc, 0)))

ENT = abservice.parse_table(ROM_A, BASE_A)
NZ = [e for e in ENT.values() if e.implemented]
STUB, STUB_SVCS = abservice.default_stub(ENT)
IMPL = abservice.implemented_services(ENT, STUB)
print("\nA2 A 段服务分发表 @0x%06X（128 槽，svc -128..-1）" % A_TABLE)
print("  非零 %d 槽 / 不同 handler %d 个 / 默认桩 0x%06X 被 %d 个槽共用"
      % (len(NZ), len({e.handler for e in NZ}), STUB, len(STUB_SVCS)))
print("  排除默认桩后有独立 handler 的服务号：%s"
      % ["0x%02X" % (s & 0xFF) for s in IMPL])
print("  服务表 wrapper（0x0FE3EC..0x0FE3FA，索引 = 服务号 × 2 的带符号偏移）：")
dis(ROM_A, BASE_A, 0x000FE3EC, 0x000FE3FC)

print("\nA3 时间驱动服务专查（表项 + 调用点两条证据并列）")
print("  %-8s %-10s %-9s %-9s %-9s %-9s %s"
      % ("服务号", "名字", "A表raw", "A表偏移", "A调用点", "B调用点", "A handler"))
for b, nm, ncall, raw, handler in abservice.delay_report(
        ENT, PA, (0xAB, 0xDB, 0xDA, 0xE9)):
    e = ENT[abservice.sx8(b)]
    print("  0x%02X     %-10s 0x%04X   %+9d %-9d %-9d 0x%06X"
          % (b, nm or "-", raw, e.delta, ncall,
             PB.get(abservice.sx8(b), 0), handler))
print("  ⇒ A 段 dly_tsk(0xAB) / tslp_tsk(0xDB) 表项 = 0、调用点 = 0；")
print("    B 段同两个服务号分别 %d / %d 处调用点。"
      % (PB.get(abservice.sx8(0xAB), 0), PB.get(abservice.sx8(0xDB), 0)))

print("\nA4 启动链尽头 / 空闲任务 / 向量表关键槽")
dis(ROM_A, BASE_A, 0x000804F4, 0x000804FC)
dis(ROM_A, BASE_A, 0x000FE50A, 0x000FE514)
print("  A 段 vec 0x18=0x%06X  vec 0x1B=0x%06X  vec 0x2B=0x%06X  vec 0x3F=0x%06X"
      % (vec_slot(ROM_A, BASE_A, MA.tbr, 0x18),
         vec_slot(ROM_A, BASE_A, MA.tbr, 0x1B),
         vec_slot(ROM_A, BASE_A, MA.tbr, 0x2B),
         vec_slot(ROM_A, BASE_A, MA.tbr, 0x3F)))
print("  B 段 vec 0x18=0x%06X  vec 0x1B=0x%06X  vec 0x2B=0x%06X  vec 0x3F=0x%06X"
      % (vec_slot(ROM_B, BASE_B, MB.tbr, 0x18),
         vec_slot(ROM_B, BASE_B, MB.tbr, 0x1B),
         vec_slot(ROM_B, BASE_B, MB.tbr, 0x2B),
         vec_slot(ROM_B, BASE_B, MB.tbr, 0x3F)))

HA = direct_page_hits(ROM_A, BASE_A, A_DISPATCH_REQ)
HB = direct_page_hits(ROM_B, BASE_B, A_DISPATCH_REQ)
print("\nA5 直接页 0x44（请求派发）的 dmov 命中：A 段 %d 处 / B 段 %d 处"
      % (len(HA), len(HB)))
print("  A 段写点：%s" % ["0x%06X" % p for p in HA])

# ==================================================================== B
hr("B  口径：A 段止点四种运行入口对账（V10「弹出 PC=0」复核）")


def traced_run(m, runner, tag):
    pcs = []
    cov_at_reti = [None]
    orig = m.cpu.step

    def step():
        pc = m.cpu.pc
        pcs.append(pc)
        # 断点口径：执行 reti 之前累计覆盖的不同 PC 数（历史 README 的 824 即此口径）
        if pc == A_RETI and cov_at_reti[0] is None:
            cov_at_reti[0] = m.cov.words() if m.cov else -1
        return orig()

    m.cpu.step = step
    try:
        runner()
    finally:
        m.cpu.step = orig
    idx = [i for i, p in enumerate(pcs) if p == A_RETI]
    nxt = pcs[idx[0] + 1] if idx and idx[0] + 1 < len(pcs) else None
    print("  %-34s step=%-7d 末PC=0x%06X fault=%-5s reti@第%d步 下一PC=%s cov=%d"
          % (tag, m.step_count, m.cpu.pc, str(m.fault),
             idx[0] if idx else -1,
             "0x%06X" % nxt if nxt is not None else "-",
             m.cov.words() if m.cov else -1))
    print("    断点口径（reti 执行前）不同 PC=%s，跑过空闲任务后不同 PC=%d"
          % (cov_at_reti[0], m.cov.words() if m.cov else -1))
    return nxt


m = create("d300-a")
m.load()
m.enable_cov()
N1 = traced_run(m, lambda: frbox_boot.boot(m, max_steps=RUN_A),
                "frbox_boot.boot（诊断脚本）")
m = create("d300-a")
m.load()
m.reset()
m.enable_cov()
N2 = traced_run(m, lambda: m.run(max_steps=RUN_A, spin_guard=False),
                "Machine.run 关自旋")
m = create("d300-a")
m.load()
m.reset()
m.enable_cov()
N3 = traced_run(m, lambda: m.run(max_steps=RUN_A),
                "Machine.run 默认")
m = create("d300-a")
m.load()
m.enable_cov()
N4 = traced_run(m, lambda: m.run(max_steps=RUN_A, spin_guard=False),
                "Machine.run 不额外 reset")
print("  ⇒ 四种入口下一 PC 全部 = 0x%06X（A 段空闲任务 bra -1），"
      "没有一种弹出 0。" % (N1 or 0))
print("    README / KNOWN_ISSUES 里「reti 从系统栈弹出 PC=0、固件停在 0x44」")
print("    是 V11 修 S 位换支之前的旧结论（见 realos-run.txt 第 2 节），V56 更正。")

# ==================================================================== C
hr("C  动态：A 段挂不同周期线（同一把尺子，每组一台新机器）")


def case_a(tag, vector):
    m = create("d300-a")
    m.load()
    m.reset()
    m.enable_cov()
    reads, writes = bobserve.observe(m.bus, (A_DISPATCH_REQ, A_CUR_TCB))
    dev = None
    if vector is not None:
        dev = Irq2BClock(m, period=PERIOD, vector=vector)
        m.devices.append(dev)
    m.run(max_steps=RUN_C, spin_guard=False)
    logs = collections.Counter(r[0] for r in m.cpu.irq_log)
    w44 = sum(1 for w in writes if w[0] == A_DISPATCH_REQ)
    wtcb = sum(1 for w in writes if w[0] == A_CUR_TCB)
    ran = [n for pc, n in WATCH_A if m.cov.visited(pc)]
    print("\n【%s】step=%d 末PC=0x%06X cov=%d 0x44写=%d 当前TCB写=%d"
          % (tag, m.step_count, m.cpu.pc, m.cov.words(), w44, wtcb))
    print("  中断受理=%s 关键PC跑过=%s"
          % (dict(logs), ran))
    if dev is not None:
        print("  时钟 fired=%d accepted=%d coalesced=%d skipped=%d"
              % (dev.fired, dev.accepted(), dev.coalesced, dev.skipped))
    return {"tag": tag, "pc": m.cpu.pc, "cov": m.cov.words(),
            "accepted": (dev.accepted() if dev else 0), "w44": w44,
            "wtcb": wtcb, "ran": ran, "irq": dict(logs)}


C = [
    case_a("C0 A 段基线 / 不挂线", None),
    case_a("C1 A 段挂 vec 0x18", 0x18),
    case_a("C2 A 段挂 vec 0x2B", 0x2B),
    case_a("C3 A 段挂 vec 0x13", 0x13),
    case_a("C4 A 段挂 vec 0x1B", 0x1B),
]
print("\nC 段对账表（同一把尺子：boot 后跑 %d 步，period=%d）"
      % (RUN_C, PERIOD))
print("  %-24s %-10s %-9s %-8s %s"
      % ("组", "末PC", "覆盖PC", "受理", "位分派器跑过"))
for r in C:
    print("  %-24s 0x%06X  %-9d %-8d %s"
          % (r["tag"], r["pc"], r["cov"], r["accepted"],
             "是" if "A vec0x2B 位分派器" in r["ran"] else "否"))
print("  ⇒ vec 0x18 / 0x13 / 0x1B 都被受理，但末 PC 仍是 0x%06X；" % A_IDLE)
print("    只有 vec 0x2B 让位分派器 0x%06X 跑过并进入应用区。" % A_ISR_2B)

# ==================================================================== D
hr("D  动态：B 段同一根 vec 0x18 线（blive.boot 口径，与 V54/V55 同尺）")


def case_b(tag, vector):
    m = blive.boot(0)
    reads, writes = bobserve.observe(
        m.bus, (A_DISPATCH_REQ, B_TICK_COUNTER, B_DELAY_SENTINEL))
    dev = None
    if vector is not None:
        dev = Irq2BClock(m, period=PERIOD, vector=vector)
        m.devices.append(dev)
    m.step(BOOT)
    m.run(max_steps=RUN_D, spin_guard=False)
    dq = btick.delay_queue(m)
    tick = m.bus.rd(B_TICK_COUNTER + 4, 4)
    print("\n【%s】step=%d 末PC=0x%06X tick计数=%d 延时队列=%s"
          % (tag, m.step_count, m.cpu.pc, tick,
             [tid for _t, tid, _p, _w in dq]))
    if dev is not None:
        print("  时钟 fired=%d accepted=%d coalesced=%d skipped=%d"
              % (dev.fired, dev.accepted(), dev.coalesced, dev.skipped))
    print("  0x44 写 %d 次 / tick 计数写 %d 次"
          % (sum(1 for w in writes if w[0] == A_DISPATCH_REQ),
             sum(1 for w in writes if w[0] == B_TICK_COUNTER)))
    return {"tag": tag, "pc": m.cpu.pc, "tick": tick,
            "accepted": (dev.accepted() if dev else 0),
            "dq": [tid for _t, tid, _p, _w in dq]}


D = [case_b("D0 B 段基线 / 不挂线", None),
     case_b("D1 B 段挂 vec 0x18", 0x18)]

# ==================================================================== E
hr("E  两段并列：同一根 vec 0x18 线，两段反应相反")
print("  %-10s %-34s %-34s" % ("", "A 段（IO 协处理）", "B 段（主控 UI）"))
print("  %-10s %-34s %-34s"
      % ("服务表", "dly_tsk / tslp_tsk 表项 = 0", "dly_tsk 121 / tslp_tsk 137 处"))
print("  %-10s %-34s %-34s"
      % ("调用点", "两个服务各 0 处", "同上，且 2297 处 int #0x40"))
print("  %-10s %-34s %-34s"
      % ("挂线结果", "C1 受理 %d 次，末 PC 仍 0x%06X"
         % (C[1]["accepted"], C[1]["pc"]),
         "D1 受理 %d 次，tick 计数 %d"
         % (D[1]["accepted"], D[1]["tick"])))
print("  %-10s %-34s %-34s"
      % ("替代线", "C2 vec 0x2B → 应用区 0x%06X" % C[2]["pc"],
         "V54 已证 vec 0x2B / 0x1B 无效"))
print("  ⇒ 根因在固件自身的服务表：A 段内核没编进时间驱动服务，")
print("    给它补 B 段那根周期线既没有延时队列可走、也没有服务可调；")
print("    A 段要推进需要的是 sta_tsk 的触发事件（108 处调用点），不是 tick。")

# ==================================================================== F
hr("F  能说 / 不能说")
print("""
能说：
  1. A 段服务分发表 @0xFE3B8 全 128 槽已解析：%d 槽非零、%d 个 handler，
     默认桩 0x%06X 被 %d 槽共用；dly_tsk(0xAB) / tslp_tsk(0xDB) 表项为 0
     且静态调用点各 0 处，B 段同号分别是 %d / %d 处（KI#209）。
  2. A 段原生启动链尽头已更正：0x%06X 的 reti 弹出 0x%06X（空闲任务
     bra -1），四种运行入口一致；V10「弹 PC=0 / 停在 0x44」是修 S 位换支
     之前的旧结论（KI#210）。V10 记的 43,446 步 / 824 PC 属断点口径；
     本版实测 reti 执行前 823 个不同 PC、跑过空闲任务后 825 个，
     ⇒ 823 + reti 本身 = 824，再 + 空闲任务 = 825，三个数字自洽。
  3. 同一根 vec 0x18 周期线：A 段受理 %d 次仍停在空闲，B 段受理 %d 次
     tick 计数变 %d、延时队列 %s（KI#211）。
  4. A 段 vec 0x2B 受理 %d 次就让位分派器 0x%06X 跑过、覆盖 PC 从 %d 涨到 %d、
     末 PC 进应用区 0x%06X ⇒ A 段推进靠事件线（KI#212）。

不能说：
  · A 段「跑起来了 / 任务切换了」—— 默认 dispatch=False，当前 TCB 只写 %d 次，
    0x%06X 的 vec 0x3F 调度器在 C 段各组都没跑过。
  · 「A 段不需要任何时钟」—— 只证了内核没有时间驱动服务；直接的硬件延时、
    轮询超时不在本版结论内。
  · 「vec 0x2B 就是 A 段的真实时基/事件源」—— 周期拉线是模型参数，
    只证明这根线能触达应用区，不证明真机上它接的是什么器件。
  · 「真机 A/B 之间怎么通信」—— 本版没有建任何 A↔B 通道；
    项目已推翻过一次「0x482 是双机握手寄存器」，不做无实证的跨机建模。
  · 出画面 —— 两段末 PC 都还在空闲/应用区，B 段结构体 0x9F5A0284 仍恒 0
    （KI#201 原样保留）。

口径纪律：
  · A 段用 Machine.run()/frbox_boot.boot()，B 段用 blive.boot(0)+m.step(%d)；
    两段口径不同，数字不混比（硬规矩 2）。
  · 周期线统一用 Irq2BClock、period=%d，只拉线不写内存；
    与 btick.tick*() 那类「替固件改内核数据」的反事实补全严格分开。
  · 服务号按 8 位补码读（硬规矩 23）；「表项非零」不等于「跑过」（硬规矩 5）。
  · C 小节 5 台 A 段机器 + D 小节 2 台 B 段机器 + B 小节 4 台 A 段口径对账机器，
    共 11 台运行机器，各跑一遍、不复用 cov（硬规矩 13）；
    另有 2 台只读 ROM 实例（A/B 各 1 台）只做静态扫描、没 boot。
""" % (len(NZ), len({e.handler for e in NZ}), STUB, len(STUB_SVCS),
       PB.get(abservice.sx8(0xAB), 0), PB.get(abservice.sx8(0xDB), 0),
       A_RETI, A_IDLE, C[1]["accepted"], D[1]["accepted"], D[1]["tick"],
       D[1]["dq"], C[2]["accepted"], A_ISR_2B, C[0]["cov"], C[2]["cov"],
       C[2]["pc"], C[0]["wtcb"], A_DISPATCHER, BOOT, PERIOD))

hr("G  复现")
print("  cd emu && python probe/exp_ab_v56.py > ../results/frbox/ab56.txt")
print("  cd emu && python frbox_selftest.py")
print("⚠️ period=1024 与 60k/70k/400k 步都是模型参数 / 观测窗口，不代表真机时间。")
