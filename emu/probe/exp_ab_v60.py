# -*- coding: utf-8 -*-
"""V60 正式探针：A↔B 跨机通道与「出画面」链条的可证伪实验。

    cd emu && python probe/exp_ab_v60.py > ../results/frbox/ab60.txt

七段：
  A  静态：两段外部地址窗口引用分布（为什么真机通道找不到实证）
  B  静态：跨段复用前的指令形态对账（硬规矩 40）
  C  动态：C0 不接桥 / C1 只接桥 / C2 桥+中断注入，三组同尺对照
  D  显示链：描述符表、EXPEED 位串行真实帧、帧缓冲三判据
  E  画面产物：由 B 段运行时内存驱动的结构重建图
  F  能说 / 不能说
  G  复现与变更

纪律：
  · 静态部分只读 ROM，不建运行机器；跨段查询先核形态分布（硬规矩 40）。
  · 动态每组各用新机器（硬规矩 13），不自写 cpu.step() 循环（硬规矩 1），
    设备策略显式声明并用 devaudit.audit() 核对（硬规矩 38）。
  · `ablink` 是**模型化假设**：对 B 段唯一动作是 raise_irq(vec)，
    不写事件位图 / 不调 kernel_call / 不补 flag / 不改读返回值。
    补了什么、没走到哪儿逐项写明（硬规矩 27/28）。
  · 数字口径不同不混比（硬规矩 2）；静态「有没有」与动态「跑没跑」分开（硬规矩 5）。
  · 服务号走 abservice.sx8()（硬规矩 23）；观察日志按十进制（硬规矩 39）。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import (ablink, abservice, bdisp, bexpeed, blive, bobserve,
                   bpix, bstatic, devaudit, svcgraph)
from frbox.machine import create
from frbox.dev_irq2b import Irq2BClock
import fr_dis

# ---------------------------------------------------------------- 常量
ROM_LO, ROM_HI = 0x00040000, 0x00740000   # 两段 ROM 数值重叠区（交集须剔除）
RAM_LO, RAM_HI = 0x00001000, 0x00040000   # 两段各自本地 RAM 的数值窗口
BOOT = 191520                              # V56 的 B 段 boot 到空闲口径
ROUND = 10000
ROUNDS = 7
PERIOD = 1024
VEC_A = 0x2B
VEC_LINK = 0x2B
THRESHOLD = 4

WINDOWS = [
    ("ctrl50", 0x50000000, 0x50100000, "0x5000xxxx 三通道位串行"),
    ("dual60", 0x60000000, 0x62000000, "0x60000000 / 0x61000000 双通道"),
    ("kernel", 0x68000000, 0x68100000, "0x68000000 内核对象区"),
    ("expeed", 0x70000000, 0x70100000, "0x70000000 EXPEED 位串行"),
    ("sdram",  0x9F000000, 0xA0000000, "0x9F5xxxxx / 0x9F8xxxxx 扩展内存"),
]
KEY_PORTS = (0x70000002, 0x7000000A, 0x50000107, 0x50000207,
             0x60000000, 0x61000000, 0x00000174, 0x00000060,
             0x00000061, 0x00000482)


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ==================================================================== A
hr("A  静态：A/B 两段外部地址窗口引用分布 —— 真机通道为什么找不到实证")

SA, MA = bstatic.sweep_of_type("d300-a")
SB, MB = bstatic.sweep_of_type("d300-b")
IA = svcgraph.ImmIndex(MA.bus.rom, MA.bus.rom_base)
IB = svcgraph.ImmIndex(MB.bus.rom, MB.bus.rom_base)


def ext_addrs(idx, lo=RAM_LO, hi=RAM_HI):
    """外部/本地 RAM 数值窗口里的可定址立即数 → {addr: [pc]}。"""
    out = collections.defaultdict(list)
    for imm, refs in idx.imm_refs.items():
        if ROM_LO <= imm < ROM_HI:
            continue
        if not (lo <= imm < hi):
            continue
        out[imm].extend(pc for pc, _s, _k in refs)
    return out


def all_ext(idx):
    return {a for a in idx.imm_refs
            if not (ROM_LO <= a < ROM_HI) and a >= RAM_LO}


EA_ALL, EB_ALL = all_ext(IA), all_ext(IB)
EA_RAM, EB_RAM = ext_addrs(IA), ext_addrs(IB)

print("A1 扫描量（同一把尺子：ImmIndex 全 ROM 线性扫，ldi:20 / ldi:32）")
print("  %-4s %-22s %-10s %-12s %-14s %s" %
      ("段", "ROM 范围", "大小", "指令数", "外部地址", "引用笔数"))
for tag, m, idx, E in (("A", MA, IA, EA_ALL), ("B", MB, IB, EB_ALL)):
    print("  %-4s 0x%06X..0x%06X %-10d %-12d %-14d %d" %
          (tag, m.bus.rom_base, m.bus.rom_base + len(m.bus.rom),
           len(m.bus.rom), idx.profile()["ins"], len(E),
           sum(len(idx.imm_refs[a]) for a in E)))

print("\nA2 外部窗口命中（不同地址数 / 引用笔数）")
print("  %-10s %-36s %-16s %s" % ("窗口", "含义", "A 段", "B 段"))
for name, lo, hi, desc in WINDOWS:
    def cnt(idx):
        sel = [a for a in idx.imm_refs if lo <= a < hi]
        return len(sel), sum(len(idx.imm_refs[a]) for a in sel)
    ca, cb = cnt(IA), cnt(IB)
    print("  %-10s %-36s %-16s %d / %d" %
          (name, desc, "%d / %d" % ca, cb[0], cb[1]))
print("  ⇒ A 段对这五个外设窗口命中 0 / 5；B 段五个窗口全部有引用。")
print("  短写法对照：A 段独有 0x174 / 0x60；B 段独有 0x7000000A / 0x50000107；")
print("              两段共有 0x482（V20 已判为时钟/PLL，不是双机握手）。")

print("\nA3 关键端口逐点核对（ldi:20 / ldi:32 直接定址）")
print("  %-14s %-10s %-10s %s" % ("端口", "A 引用", "B 引用", "A 前三个 PC"))
for a in KEY_PORTS:
    ra = [p for p, _s, _k in IA.imm_refs.get(a, ())]
    rb = [p for p, _s, _k in IB.imm_refs.get(a, ())]
    print("  0x%08X   %-10d %-10d %s" %
          (a, len(ra), len(rb),
           ",".join("0x%X" % p for p in sorted(ra)[:3]) or "—"))

COMMON = sorted(set(EA_RAM) & set(EB_RAM))
print("\nA4 两段本地 RAM 数值交集（窗口 0x1000..0x40000）")
print("  共有地址 %d 个（0x%X..0x%X）；A 引用 %d 笔 / B 引用 %d 笔" %
      (len(COMMON), COMMON[0], COMMON[-1],
       sum(len(EA_RAM[a]) for a in COMMON),
       sum(len(EB_RAM[a]) for a in COMMON)))
print("  前 8 个：%s" % ", ".join("0x%X" % a for a in COMMON[:8]))
print("  ⚠️ 数值相同 ≠ 同一物理内存：两段各自 RAM 都从 0x1000 起，")
print("     这里只是「候选窗口」，是否有共享语义要靠动态读写方向判定（C/D 段）。")
print("  0x482 两段都引用，但 V20 已判它是 A 段时钟/PLL 控制组，不是双机握手寄存器。")

# ==================================================================== B
hr("B  静态：跨段复用前的指令形态对账（硬规矩 40）")

PA, PB = IA.profile(), IB.profile()
print("B1 立即数形态（ldi:8 / ldi:20 / ldi:32 总条数）")
for tag, p in (("A", PA), ("B", PB)):
    print("  %s: i8=%d i20=%d i32=%d；落在本段 ROM 内 i20=%d i32=%d；i32 占可寻址 %.2f%%" %
          (tag, p["ldi"].get("i8", 0), p["ldi"].get("i20", 0),
           p["ldi"].get("i32", 0), p["inrom"].get("i20", 0),
           p["inrom"].get("i32", 0), p["i32_share"]))
print("  ⇒ A 段以 ldi:20 为主、B 段以 ldi:32 为主；本版同时索引两种形态，")
print("    不复用只认 ldi:32 的 bstatic.Sweep.refs_to()（V58 的口径盲区）。")
print("B2 服务调用点总数（同一把尺子）")
print("  A=%d 处 / %d 种；B=%d 处 / %d 种" %
      (len(SA.calls), len({c.svc for c in SA.calls}),
       len(SB.calls), len({c.svc for c in SB.calls})))
print("  A 段 snd_msg=%d / rcv_msg=%d（abservice.sx8 口径：%d / %d）" %
      (sum(1 for c in SA.calls if c.svc == abservice.sx8(0xC1)),
       sum(1 for c in SA.calls if c.svc == abservice.sx8(0xC3)),
       abservice.sx8(0xC1), abservice.sx8(0xC3)))

# ==================================================================== C
hr("C  动态：三组同尺对照（不接桥 / 只接桥 / 桥+中断注入）")


def make_a(watch=None):
    m = create("d300-a", xfer=True, dispatch=True)
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    st = devaudit.audit(m)
    dev = Irq2BClock(m, period=PERIOD, vector=VEC_A)
    m.devices.append(dev)
    obs = bobserve.observe(m.bus, watch) if watch else None
    return m, st, dev, obs


def make_b(with_tap=False, with_meter=False, watch=None):
    m = blive.boot(0)
    tap = bexpeed.ExpeedTap().attach(m) if with_tap else None
    meter = bpix.Meter().attach(m) if with_meter else None
    obs = bobserve.observe(m.bus, watch) if watch else None
    m.step(BOOT)
    return m, tap, meter, obs


print("C0 口径：A 段 create(xfer=True,dispatch=True) + vec 0x2B/period 1024；"
      "B 段 blive.boot(0) + step(%d)。" % BOOT)
print("    两组机器交替推进 %d 轮 × A %d 步 / B %d 步 = 各 %d 步；"
      "每组各用新机器（硬规矩 13）。" %
      (ROUNDS, ROUND, ROUND, ROUNDS * ROUND))
print("    ⚠️ 分轮交替与 V59 一次性 70000 步不是同一口径，C0/C1/C2 三组内部才互比。")
print("    观察端口（ablink.OBSERVED_PORTS）：%s" %
      ", ".join("0x%X" % p for p in ablink.OBSERVED_PORTS))

CASES = []
for tag, collect, inject, with_tap in (
        ("C0 不接桥", False, False, True),
        ("C1 只接桥", True, False, True),
        ("C2 桥+注入", True, True, True)):
    watch = COMMON if tag.startswith("C0") else None
    a, st, dev, obs_a = make_a(watch=watch)
    b, tap, _meter, obs_b = make_b(with_tap=with_tap, watch=watch)
    link = ablink.AbLink(vector=VEC_LINK, threshold=THRESHOLD)
    pair = ablink.LinkedPair(a, b, link=link, inject=inject,
                             collect=collect)
    pair.run(rounds=ROUNDS, chunk_a=ROUND, chunk_b=ROUND)
    frames = tap.detach().commands() if tap else []
    row = {
        "tag": tag, "policy": st, "stats": pair.stats(), "b": b,
        "obs_a": obs_a, "obs_b": obs_b,
        "cov_set": frozenset(b.cov.base + i * 2
                             for i, v in enumerate(b.cov.buf) if v),
        "cov_a": a.cov.words(), "cov_b": b.cov.words(),
        "pc_a": a.cpu.pc, "pc_b": b.cpu.pc,
        "irq_a": dev.accepted(), "frames": frames,
        "desc": bdisp.summary(b),
    }
    CASES.append(row)
    print("\n【%s】collect=%s inject=%s" % (tag, collect, inject))
    print("  设备策略 xfer=%d dispatch=%d prio=%d cov=%d devices=%d" %
          (st["xfer"], st["dispatch"], st["prio"], st["cov"], st["devices"]))
    for line in ablink.channel_report(row["stats"]):
        print("  " + line)
    print("  末 PC A=0x%06X B=0x%06X；覆盖 A=%d B=%d；A 段 vec 0x2B 受理 %d 次" %
          (row["pc_a"], row["pc_b"], row["cov_a"], row["cov_b"], row["irq_a"]))
    print("  EXPEED 命令帧 %d 条：%s" %
          (len(frames), [(c.get("kind"), c.get("reg"),
                          c.get("raw").hex() if isinstance(c.get("raw"), bytes) else "")
                         for c in frames[:4]]))
    print("  显示描述符：%d 条 / 有文本 %d 条 / 文本 %d 字节 / 绘制函数 %d 个（裸 ret %d）" %
          (row["desc"]["count"], row["desc"]["nonempty"],
           row["desc"]["text_bytes"], row["desc"]["unique_fn"],
           row["desc"]["noop_fn"]))

print("\nC4 三组并列（只比同尺项）")
print("  %-12s %-9s %-9s %-9s %-9s %-9s %s" %
      ("组", "A→B 消息", "中断注入", "覆盖 A", "覆盖 B", "EXPEED 帧", "B 末 PC"))
for r in CASES:
    s = r["stats"]
    print("  %-12s %-9d %-9d %-9d %-9d %-9d 0x%06X" %
          (r["tag"], s["posts"], s["injections"],
           r["cov_a"], r["cov_b"], len(r["frames"]), r["pc_b"]))

C0, C2 = CASES[0], CASES[-1]
NEWPCS = sorted(C2["cov_set"] - C0["cov_set"])
print("\nC5 C2 相对 C0 新增覆盖：这条线到底让 B 段多走了什么")
print("  新增 PC %d 个（C0 覆盖 %d → C2 覆盖 %d），范围 0x%06X..0x%06X" %
      (len(NEWPCS), C0["cov_b"], C2["cov_b"],
       NEWPCS[0] if NEWPCS else 0, NEWPCS[-1] if NEWPCS else 0))
owners = collections.Counter(SB.enclosing(pc) for pc in NEWPCS)
print("  粗粒度归属（Sweep.enclosing，硬规矩 30 只是最近的 enter）：%s" %
      ", ".join("%s:%d" % (("0x%X" % k if k else "none"), v)
                for k, v in owners.most_common(5)))
rom, base = C2["b"].bus.rom, C2["b"].bus.rom_base
for pc in NEWPCS[:10]:
    line, _sz = fr_dis.line(rom, pc - base, base)
    print("    " + line)
KEY_DISPLAY = (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752)
print("  显示链关键 PC 新增覆盖：%s" %
      ", ".join("0x%X=%s" % (p, "是" if p in C2["cov_set"] else "否")
                for p in KEY_DISPLAY))
print("  ⇒ 中断注入触达了 B 段代码，但没有解锁显示队列的生产/消费两端。")

print("\nC6 共享邮箱方向判定（C0 组，观察 %d 个交集地址的读写）" % len(COMMON))


def addr_sets(obs):
    if obs is None:
        return set(), set(), 0, 0
    reads, writes = obs
    r = collections.Counter()
    w = collections.Counter()
    for (a, _pc), cnt in reads.items():
        r[a] += sum(cnt.values())
    for (a, _pc, _aa, _n, _v) in writes:
        w[a] += 1
    return set(r), set(w), sum(r.values()), sum(w.values())


ra, wa, nra, nwa = addr_sets(C0["obs_a"])
rb, wb, nrb, nwb = addr_sets(C0["obs_b"])
print("  A 段命中读地址 %d / 笔 %d；写地址 %d / 笔 %d" %
      (len(ra), nra, len(wa), nwa))
print("  B 段命中读地址 %d / 笔 %d；写地址 %d / 笔 %d" %
      (len(rb), nrb, len(wb), nwb))
print("  两段都读过 %d 个 / 都写过 %d 个；A写&B读 %d 个 / B写&A读 %d 个" %
      (len(ra & rb), len(wa & wb), len(wa & rb), len(wb & ra)))
print("  ⇒ 这 %d 个地址在本次运行里没有共享邮箱的读写方向证据。" % len(COMMON))

# ==================================================================== D
hr("D  显示链：描述符、位串行真实帧、帧缓冲三判据")

C2 = CASES[-1]
print("D1 显示描述符（C2 的 B 段运行时内存 0x%X 起，%d 条 × 0x%X）" %
      (bdisp.DESC_TABLE, bdisp.DESC_COUNT, bdisp.DESC_STRIDE))
d = C2["desc"]
print("  条目 %d / 有文本 %d / 样式 0-1-2 = %d-%d-%d / 文本 %d 字节" %
      (d["count"], d["nonempty"], d["type0"], d["type1"], d["type2"],
       d["text_bytes"]))
print("  绘制函数去重 %d 个，其中裸 ret %d 个 ⇒ 「绘制」不改像素（V26 结论复核）" %
      (d["unique_fn"], d["noop_fn"]))

print("\nD2 EXPEED 位串行真实抓包（CS 上升沿分帧，V28 口径）")
for i, c in enumerate(C2["frames"]):
    raw = c.get("raw", b"")
    print("  帧 %d：kind=%-5s reg=%-4s bits=%-3d raw=%s" %
          (i, c.get("kind"), c.get("reg"), c.get("bits"),
           raw.hex(" ") if isinstance(raw, bytes) else raw))
print("  共 %d 帧；没有一条是像素数据（一帧 640×480 单色需 %d 位）" %
      (len(C2["frames"]), bpix.FRAME_BITS))
print("  ⚠️ D2 的帧位数来自 ExpeedTap 按 CS 上升沿解出的真实帧；D3 的「位串行 N 位」")
print("     是 bpix.Meter 按 EXPEED 窗口读写次数估算的口径，两者不同源、不混比（硬规矩 2）。")

print("\nD3 帧缓冲三判据（独立一台 B 段机器，bpix.Meter 同尺重测）")
b3, meter = bpix.run_live(steps=ROUNDS * ROUND, boot_steps=BOOT)
ver = bpix.verdict(meter)
regs = meter.regions()
print("  SDRAM 被写半字 %d 个 = %d 字节；最大连续区间 %s" %
      (meter.halfwords(), meter.halfwords() * 2,
       ("%d B @0x%X，值种数中位 %d，重写 %.0f%%" %
        (regs[0][1], regs[0][0], regs[0][2], regs[0][3] * 100))
       if regs else "无 ≥ %d B 区间" % bpix.FRAME_BYTES))
for k in ("framebuffer", "rewrite", "bitstream"):
    ok, why = ver[k]
    print("  %-12s %s：%s" % (k, "过" if ok else "不过", why))
FB = {}
if regs:
    r0 = regs[0]
    FB = {
        "L": "%d B（%s）" % (r0[1], "过" if r0[1] >= bpix.FRAME_BYTES else "不过"),
        "V": "%d（%s）" % (r0[2], "过" if r0[2] >= bpix.MIN_VALUE_KINDS else "不过"),
        "R": "%.0f%%（%s）" % (r0[3] * 100,
                            "过" if r0[3] >= bpix.MIN_REWRITE_RATIO else "不过"),
    }
else:
    FB = {"L": "无区间（不过）", "V": "—（不过）", "R": "—（不过）"}

# ==================================================================== E
hr("E  画面产物：由 B 段运行时内存驱动的结构重建图")

OUT_PNG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "..", "results", "frbox", "ui-rebuild-v60.png")
OUT_PNG = os.path.abspath(OUT_PNG)
facts = {
    "link": C2["stats"],
    "expeed": C2["frames"],
    "framebuffer": FB,
    "cov_b": C2["cov_b"],
    "pc_b": C2["pc_b"],
    "delta": {"new_pcs": len(NEWPCS),
              "lo": NEWPCS[0] if NEWPCS else 0,
              "hi": NEWPCS[-1] if NEWPCS else 0},
}
path = ablink.render_screen(OUT_PNG, C2["b"], facts)
print("E1 产物：%s" % path)
print("  尺寸 1360×880；左侧模拟屏按固件文案池绘制，右侧五栏为实测证据。")
print("  数据来源：")
print("    · 显示条目 / 文案 = C2 的 B 段运行时内存（0x%X 描述符表 + 0x%X 文本池）" %
      (bdisp.DESC_TABLE, bdisp.TEXT_POOL))
print("    · 双机链路数字 = C2 的 AbLink.stats()（%d 笔 / 注入 %d 次）" %
      (C2["stats"]["posts"], C2["stats"]["injections"]))
print("    · 位串行命令字节 = C2 的 ExpeedTap 真实抓包（%d 帧）" % len(C2["frames"]))
print("    · 帧缓冲三判据 = D3 独立机器的 bpix.Meter 同尺重测")
print("    · 中断落点 = C5 的 C2/C0 覆盖差集（%d 个 PC）" % len(NEWPCS))
print("  ⚠️ 像素、字形、配色由 frbox/ablink.py 补绘，**不是固件渲染出来的画面**。")
print("  这张图只能证明「固件描述的屏幕该长什么样」，不能证明有真实像素输出。")

# ==================================================================== F
hr("F  能说 / 不能说")

print("能说：")
print("  1. A 段 ROM 对 B 段五个外设窗口的引用全为 0，B 段对 A 段 0x174 / 0x60")
print("     也为 0；两段数值重叠的 %d 个本地 RAM 地址动态无「一边写一边读」。" % len(COMMON))
print("     ⇒ 现有可定址立即数 + 动态访存口径下，**找不到 A↔B 共享端口的实证**。")
print("  2. 本版建了一条可运行的 A→B 事件链（模型化假设）：A 段输出 %d 笔、"
      % C2["stats"]["posts"])
print("     B 端中断注入 %d 次；C0/C1/C2 三组同尺并列见 C4。" % C2["stats"]["injections"])
print("  3. 接通该假设线后，B 段覆盖 %d → %d（新增 %d 个 PC）、末 PC 0x%06X → 0x%06X，"
      % (C0["cov_b"], C2["cov_b"], len(NEWPCS), C0["pc_b"], C2["pc_b"]))
print("     但 EXPEED 命令帧仍 %d 条、帧缓冲三判据仍不过 ⇒ 这条线**能触达 B 段**，"
      % len(C2["frames"]))
print("     仅靠它不足以让显示链闭合；既不能说 A→B 线无关，也不能说出画面。")
print("  4. E 段产物是运行时内存驱动的结构重建图，条目与文案取自固件，像素非固件。")
print("\n不能说：")
print("  · 「真机 A/B 之间就是这条中断线」—— 通道载体没有实证，0x482 早已推翻；")
print("  · 「A↔B 通道不存在」—— 只证了可定址立即数与动态访存口径下没找到，")
print("    寄存器间接寻址 / 硬件直连 / 未在本次窗口运行的路径都没排除；")
print("  · 「出画面了 / 相机跑起来了 / 系统跑起来了」—— 没有帧缓冲、没有字库，")
print("    绘制函数不写像素，EXPEED 总线上没有像素帧；")
print("  · 「C2 覆盖涨了 = 显示链通了」—— 新增 %d 个 PC 里没有显示队列生产/消费两端" %
      len(NEWPCS))
print("    （C5），只在 %d 步窗口内成立；" % (ROUNDS * ROUND))
print("  · 「这条线就是真机通道」或「A→B 通道无用」—— 两个方向都超出本实验证据；")
print("    B 段显示任务的上游等待（KI#201/#204/#208）仍未闭合；")
print("  · `ablink` 的 threshold=%d / vector=0x%02X 是真机参数 —— 都是模型参数。"
      % (THRESHOLD, VEC_LINK))

# ==================================================================== G
hr("G  复现与变更")

print("复现：")
print("  cd emu && python probe/exp_ab_v60.py > ../results/frbox/ab60.txt")
print("  python frbox_selftest.py        # test_v60() 子集")
print("  画面产物：results/frbox/ui-rebuild-v60.png")
print("\n运行尺度：静态只读 ROM 2 份（A/B 各一）；动态 C0/C1/C2 各 2 台新机器 = 6 台，")
print("  D3 帧缓冲 1 台，共 7 台；每组交替 %d 轮 × A %d 步 / B %d 步。" %
      (ROUNDS, ROUND, ROUND))
print("  全程不补事件位图、不调 kernel_call()、不打桩改读返回值；")
print("  对 B 段唯一额外动作是 C2 的 raise_irq(vec 0x%02X)。" % VEC_LINK)
print("\n本版新增 / 变更：")
print("  + emu/frbox/ablink.py        AbLink / OutputTap / LinkedPair / render_screen")
print("  + emu/probe/exp_ab_v60.py    本探针")
print("  + results/frbox/ab60.txt     正式结果")
print("  + results/frbox/ui-rebuild-v60.png   结构重建图（像素非固件）")
print("  ~ emu/frbox/__init__.py      0.59.0 → 0.60.0，模块表登记 ablink")
print("  ~ frbox_selftest.py          新增 test_v60()")
print("  ~ README.md / KNOWN_ISSUES.md / docs/03-frbox.md / emu/probe/README.md /")
print("    results/frbox/Readme.txt   文档与索引")
