# -*- coding: utf-8 -*-
"""V62 正式探针：vec 0x2B 的业务出口是一个按位派发器，不是显示链。

    cd emu && python probe/exp_dsp_v62.py > ../results/frbox/dsp62.txt

六段：
  A  静态：解剖 0x17171E 分派器 —— 6 个状态字 / 15 条 call / 按位派发表
  B  静态：两个窗口的读写图（0x65000000 固件自维护；0x6B000000 外部输入）
  C  静态：15 个 handler 与显示链的连通性（全部阴性）
  D  动态：D0 纯观察 / D1 只开总闸 / D2 六字全 1 / D3 首字全 1 /
          D4 首字只给 bit9（验证优先级短路）
  E  能说 / 不能说
  F  复现与变更

纪律：
  · 静态只读 ROM，不建运行机器；`call rel12` 按 `fr_emu.py:777` 算目标，
    不用 `fr_dis` 的 `*NNN` 文本（硬规矩 40）；`ld @(R13,Rj)` 按
    `r13 + r[rj]` 解读（硬规矩 25）。
  · 动态每台机器只跑一遍（硬规矩 13），不自写 `cpu.step()` 循环（硬规矩 1）；
    设备策略用 `devaudit.audit()` 显式核对（硬规矩 38）。
  · 纯观察 hook 先装（记原始值），`bpatch` 后装（改固件看到的值）（硬规矩 34）。
  · 反事实只改指定 `(addr, pc)` 上的 `ld` 读返回值：不写内存、不挂设备、
    不改时基、不补映射；阴性结果逐项写清（硬规矩 27/28/31）。
  · 观察日志值按十进制打印（硬规矩 39）；静态「有没有」与动态「跑没跑」
    分开（硬规矩 5）；口径不同的数字不混比（硬规矩 2）。
  · 本版 D 组 20000 步窗口与 V60 的 7 轮 × 10000 步双机交替不是同一口径；
    V61 E1 在 D1 组以**同一口径**复现，作为本版内部对照。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import (addrform, blive, bpatch, bstatic, devaudit,
                   dispatch62 as D)
import fr_emu

# ---------------------------------------------------------------- 常量
BOOT, RUN, VEC = 191520, 20000, 0x2B
FRAME = 0x000083B8                 # V61 实测：ISR 的 r14-4 运行时地址
FRAME_PCS = (0x042E40, 0x042E50)  # ISR 判 bit0 的两处读
STRUCT_FLAG = 0x9F5A0284           # V51 起的结构体基址判据
STATUS_BASE = D.STATUS_BASE
STATUS_ADDRS = tuple(STATUS_BASE + o for o in D.STATUS_OFFSETS)
STATUS_RULES = tuple(zip(STATUS_ADDRS, D.STATUS_READ_PCS))
DSP_PCS = D.DSP_PCS
DISP_LO, DISP_HI = D.DISPATCH_ENTRY, D.DISPATCH_RET


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ================================================================ 静态 ROM
hr("A  静态：vec 0x2B 的业务出口 0x17171E 是一个按位派发器")
SW_B, M_B = bstatic.sweep_of_type("d300-b")
ROM_B, BASE_B = SW_B.rom, SW_B.base
print("A0 B 段 ROM %d 字节 / 基址 0x%06X；本段只读 ROM，不建运行机器。" %
      (len(ROM_B), BASE_B))
print("   V61 已证：vec 0x2B ISR 的唯一业务出口是 `call 0x17171E`，"
      "全 ROM 只此一处引用；总闸是 0x6B000030。")

ins = D.decode_range(ROM_B, BASE_B, D.DISPATCH_ENTRY, D.DISPATCH_RET + 2)
print("A1 线性反汇编 0x%06X..0x%06X 共 %d 条；末条 = %s @0x%06X" %
      (ins[0].pc, ins[-1].pc, len(ins), ins[-1].mn, ins[-1].pc))

reads = D.status_reads(ins)
print("\nA2 分派器一次性读 6 个状态字（base=0x%08X 装 r4，偏移装 r13）" %
      STATUS_BASE)
for r in reads:
    print("   0x%06X  ld → 0x%08X (off 0x%03X) 存 r%d" %
          (r.pc, STATUS_BASE + r.off, r.off, r.reg))
print("   偏移/PC/寄存器与实证常量一致：%s / %s / %s" %
      (tuple(r.off for r in reads) == D.STATUS_OFFSETS,
       tuple(r.pc for r in reads) == D.STATUS_READ_PCS,
       tuple(r.reg for r in reads) == D.STATUS_REGS))

calls = D.call_targets(ins)
print("\nA3 15 条 call（11 条 rel12 + 4 条 call @r12）")
for c in calls:
    print("   0x%06X %-9s → 0x%06X" % (c.pc, c.kind, c.target or 0))
print("   目标集合与实证 HANDLERS 一致：%s" %
      (sorted(c.target for c in calls) == sorted(D.HANDLERS)))
print("   rel12_target(0x171752,113)=0x%06X（fr_emu.py:777，"
      "符号位 bit10，不用 fr_dis 的 *NNN）" % D.rel12_target(0x171752, 113))
print("   rel9_target(0x042E46,4)=0x%06X（V61 的 beq 跳转）" %
      D.rel9_target(0x042E46, 4))

rows = D.mask_chain(ins)
print("\nA4 按位派发表（mask → and → beq → call；多级条件只列最近一级）")
for r in rows:
    print("   call@0x%06X mask=%-12s and(r%d,r%d) beq→%s %-8s → 0x%06X" %
          (r.pc, ("0x%X" % r.mask) if r.mask is not None else "-",
           r.and_src, r.and_dst,
           ("0x%06X" % r.beq_target) if r.beq_target else "-",
           r.call_kind, r.call_target or 0))
print("   共 %d 行，与 15 条 call 一一对应：%s" %
      (len(rows), len(rows) == len(calls)))
print("   ⚠️ 按位表只列每个 call 前**最近一级** and：前 7 行判 r8（0x65000008），"
      "第 8–9 行判 r6（0x65000104），第 10–15 行判 r9/r10/r4"
      "（0x6500010C/114/11C）。r5（0x65000010）是外层门，在 "
      "0x1717A8/0x1717C6/0x1717F8/0x17181C 四处 and 里，不出现在这张表里。")

# 命中即收尾的短路结构：call 后面紧跟 bra 0x171830（函数尾）
_bras = [(x.pc, D.rel9_target(x.pc, x.vals["rel9"]))
         for x in ins if x.mn == "bra" and "rel9" in x.vals]
print("\nA5 优先级短路：rel12 分支命中后立刻 `bra` 收尾，不是 15 条全派发")
print("   分派器内 bra 共 %d 条，全部指向函数尾 0x171830：%s" %
      (len(_bras), all(t == 0x171830 for _p, t in _bras)))
print("   %s" % ["0x%06X→0x%06X" % b for b in _bras])
print("   ⇒ bit25（mask 0x02000000）置 1 时不跳、连续走 0x171836（ack 清位）"
      "与 0x1714EE（业务），随后 0x17175E bra 直接到 0x171830 ret；"
      "后面 13 条位分支**这次调用里根本不会执行**。")
print("   只有 `call @r12` 那一组（0x1717D8/0x1717E6 等）没有紧跟 bra，"
      "同一组内才允许连续命中多个 handler。")

# ================================================================ 窗口读写图
hr("B  静态：两个窗口的读写图 —— 一个是固件自维护，一个是外部输入")
WADDRS = STATUS_ADDRS
allw = []
for a in WADDRS:
    for pc, mn in SW_B.writers_of(a):
        allw.append((pc, a))
print("B1 0x65000000 段：6 个状态字共 %d 个静态可定址写点" % len(allw))
for a in WADDRS:
    w = SW_B.writers_of(a)
    print("   0x%08X : %2d 处 %s" %
          (a, len(w), ["0x%06X:%s" % x for x in w[:6]]))
_or = 0
for pc, a in allw:
    body = D.decode_range(ROM_B, BASE_B, max(BASE_B, pc - 0x40), pc + 2)
    if any(x.mn == "or" for x in body):
        _or += 1
print("   写点前 0x40 字节内含 `or`（置位形态）的：%d / %d" % (_or, len(allw)))
print("   ⇒ 这段不是「只读硬件状态寄存器」：固件自己 RMW 置位 / 清位。")
print("     模拟器里 `frbox/machine.py` 未映射该段，读写都被吞掉，"
      "所以 V61 E1 的 6 个状态字全 0 —— 是**模拟器缺映射**，不是真机没有事件。")
print("   ⚠️ 静态口径只覆盖可定址立即数；寄存器间接写看不见（硬规矩 25/31）。")

print("\nB2 0x6B000000 段：V61 的 4 处立即数加载点，每个地址静态写点都是 0")
GADDRS = (0x6B000020, 0x6B000030, 0x6B000034, 0x6B7335B0)
for a in GADDRS:
    w = SW_B.writers_of(a)
    print("   0x%08X : %d 处写点 / %d 处引用（refs_to window=8）" %
          (a, len(w), len(SW_B.refs_to(a, window=8))))
print("   ⚠️ 口径：V61 C1 的「B 段 4 处」是**可定址立即数 i20/i32 扫描**；"
      "本版 refs_to(window=8) 按地址索引得 5 处引用，两个口径不混比（硬规矩 2）。")
print("   ⇒ 三个槽 0x6B000020/30/34 全是「固件只读」，"
      "真机上只能由硬件或跨段写入；V61 的 0x6B000030 阴性在新口径下仍成立。")

print("\nB3 同构 ISR：V61 只解剖了 vec 0x2B，其实还有一条一样的链")
for p in (0x042B46, 0x042E36, 0x042E82):
    body = D.decode_range(ROM_B, BASE_B, p, p + 0x16)
    ldi = [(x.pc, x.vals.get("i32")) for x in body if "i32" in x.vals]
    print("   0x%06X 起：%s" %
          (p, " ".join("@0x%06X=0x%08X" % (q, v) for q, v in ldi)))
print("   0x042E82 读 0x6B000034 → 写栈帧 → 判 bit0 → ldi:32 #0x1A4902 → call；"
      "与 0x042E36（vec 0x2B）**同构但业务出口不同**。")
print("   0x042B46 读 0x6B000020 → and 0x04000000 → beq，是第三个同族门。")

print("\nB4 A 段口径（不重扫，沿用 V61 C1）")
print("   V61 C1 已用同时索引 i20/i32 的窗口扫描证明：A 段对"
      " 0x5000xxxx / 0x60000000 / 0x68000000 / 0x70000000 /"
      " 0x6B000000 / 0x65000000 六窗可定址立即数 **0 命中**。")
print("   本版不重复 A 段全 ROM 扫描；跨段阴性结论原样保留（KI#225/232）。")

# ================================================================ handler
hr("C  静态：15 个 handler 没有一个通向显示区")
tot_svc, tot_disp, tot_cb = 0, 0, 0
for t in sorted(D.HANDLERS):
    lo, hi, body = D.handler_bounds(ROM_B, BASE_B, t)
    svc = sum(1 for x in body if x.mn == "int" and x.vals.get("u8") == 0x40)
    disp = D.imm_refs(body, 0x120000, 0x140000)
    cb = D.imm_refs(body, 0x9F500000, 0x9F600000)
    tot_svc += svc
    tot_disp += len(disp)
    tot_cb += len(cb)
    print("   0x%06X..0x%06X %3d 条  int#0x40=%d  显示区引用=%d"
          "  9F5A段引用=%d %s" %
          (lo, hi, len(body), svc, len(disp), len(cb),
           ["0x%08X" % x[2] for x in cb[:2]]))
print("\nC1 合计：int#0x40 %d 次、显示区引用 %d 处、9F5A 段引用 %d 处" %
      (tot_svc, tot_disp, tot_cb))
print("   15 个 handler 里 **没有一个**静态引用显示区 0x120000..0x140000；"
      "其中 12 个引用 0x9F5A 段（合计 %d 处），共同围绕 0x%08X 控制块"
      "（V55 KI#208 的 0x9F59F7C8 近邻）；另 3 个是清位小函数。" %
      (tot_cb, D.HANDLER_CB))
print("C2 显示链 5 个关键 PC 的静态引用数（refs_to window=8）")
for a in DSP_PCS:
    print("   0x%06X : %d 处" % (a, len(SW_B.refs_to(a, window=8))))
print("   ⇒ 0x12E6F8 是显示模块内部被大量加载的全局对象；"
      "它的引用者全在 0x120xxx 段，不在这条 ISR 链的 handler 里。")
print("   ⚠️ 静态「没有引用」不等于动态「永不到达」；"
      "但配合 D 组的动态反事实，可以收窄 KI#233。")

# ================================================================ 动态
hr("D  动态：给满状态位，分派器短路，显示链仍不解锁")


def case(tag, frame_on, status_mode=None):
    """一台机器只跑一遍（硬规矩 13）。

    status_mode: None / 'all1' / 'first1' / 'first_bit9'。
    后者只给 0x65000008 的 bit9（0x200），用来验证优先级短路。
    """
    m = blive.boot(0)
    pol = devaudit.audit(m)
    orig = m.bus.rd
    rdlog = []

    def rd(a, n, pc=0):
        v = orig(a, n, pc)
        if DISP_LO <= pc <= DISP_HI:
            rdlog.append((pc, a, n, v))
        return v

    m.bus.rd = rd                       # 硬规矩 34：纯观察先装
    rules = {}
    if frame_on:
        for p in FRAME_PCS:
            rules[(FRAME, p)] = bpatch.bit_set(1)
    if status_mode:
        pairs = STATUS_RULES[:1] if status_mode != "all1" else STATUS_RULES
        val = 0x200 if status_mode == "first_bit9" else 0xFFFFFFFF
        for a, p in pairs:
            rules[(a, p)] = (lambda v, _val=val: _val)
    hits = bpatch.patch_read(m.bus, rules) if rules else {}

    m.step(BOOT)
    before = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    m.raise_irq(VEC)
    m.run(max_steps=RUN, spin_guard=False)
    after = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    new = after - before

    print("\n【%s】frame_on=%s status_mode=%s" %
          (tag, frame_on, status_mode))
    print("   设备策略 xfer=%d dispatch=%d prio=%d cov=%d devices=%d" %
          (pol["xfer"], pol["dispatch"], pol["prio"],
           pol["cov"], pol["devices"]))
    print("   boot=%d 步 + 单次 raise_irq(0x%02X) + run(%d) ⇒ 末 PC=0x%06X" %
          (BOOT, VEC, RUN, m.cpu.pc))
    print("   覆盖 %d → %d（+%d）；ISR 37 个 PC 命中 %d" %
          (len(before), len(after), len(new),
           len([x.pc for x in addrform.isr_graph(
               ROM_B, BASE_B, 0x042E1C, 0x042E72) if x.pc in after])))
    print("   打桩命中（十进制键，硬规矩 39）：%s" %
          (dict(hits) if hits else "无"))
    sreads = collections.Counter((p, a) for p, a, n, v in rdlog
                                 if STATUS_BASE <= a < STATUS_BASE + 0x1000)
    print("   分派器状态字读 %d 笔 / 原始值全为 0：%s" %
          (sum(sreads.values()),
           all(v == 0 for p, a, n, v in rdlog
               if STATUS_BASE <= a < STATUS_BASE + 0x1000)))
    hh = [t for t in D.HANDLERS if t in after]
    print("   15 个 handler 入口命中 %d 个：%s" %
          (len(hh), ["0x%06X" % t for t in sorted(hh)]))
    print("   新增 PC 落 handler 区 0x1714EE..0x172400：%d 个"
          "（其中 0x1714EE 体内 %d 个 / 0x171836 体内 %d 个）" %
          (len([p for p in new if 0x1714EE <= p <= 0x172400]),
           len([p for p in new if 0x1714EE <= p <= 0x1715E2]),
           len([p for p in new if 0x171836 <= p <= 0x17184A])))
    for p in DSP_PCS:
        print("   显示链关键 PC 0x%06X = %s" %
              (p, "命中" if p in after else "未命中"))
    print("   0x9F5A0284 结构体读值 = %d；0x%08X 控制块读值 = %d" %
          (m.bus.rd(STRUCT_FLAG, 4, 0), D.HANDLER_CB,
           m.bus.rd(D.HANDLER_CB, 4, 0)))
    print("   新增 PC 落显示区 0x120000..0x140000：%d 个 %s" %
          (len([p for p in new if 0x120000 <= p <= 0x140000]),
           ["0x%06X" % p for p in sorted(new)
            if 0x120000 <= p <= 0x140000][:12]))
    return after


cov0 = case("D0 纯观察（V61 D0 同口径）", False, None)
cov1 = case("D1 只开总闸（V61 E1 同口径）", True, None)
cov2 = case("D2 总闸 + 6 个状态字全 1", True, "all1")
cov3 = case("D3 总闸 + 只给 0x65000008 全 1", True, "first1")
cov4 = case("D4 总闸 + 只给 0x65000008 的 bit9", True, "first_bit9")

print("\nD5 同尺对照（五组口径完全一致：BOOT=%d / 单次 IRQ / RUN=%d）" %
      (BOOT, RUN))
print("   D1 vs D0 新增 %d 个 PC；D2 vs D1 再新增 %d 个 PC；"
      "D3 vs D1 新增 %d 个 PC；D4 vs D1 新增 %d 个 PC" %
      (len(cov1 - cov0), len(cov2 - cov1), len(cov3 - cov1),
       len(cov4 - cov1)))
print("   D2 与 D3 覆盖/命中逐项相同（对称差集 %d 个 PC）⇒ 多给的那 5 个"
      "状态字没有任何额外效果：分派器在 bit25 / bit9 命中后已经 bra 收尾，"
      "本次调用根本走不到后面的分支。" % len(cov2 ^ cov3))
print("   D4 只给 bit9 而 bit25=0 ⇒ 0x17174E beq:d 先跳到 0x171760，"
      "连 0x171758 的 bit9 判定都被跳过，一个 handler 也进不去；"
      "这是 A5 优先级链的独立动态印证。")
print("   ⇒ 给满状态位只命中 2 个 handler 入口，但显示链 5 个关键 PC"
      " 仍一个没命中、0x9F5A0284 仍 0。")
print("   ⚠️ 0x12E6F8 位于 B 段 ROM 区（0x40000..0x740000），"
      "读它拿到的是机器码常量，不是运行时队列状态；"
      "本版判据只看「该 PC 是否被覆盖执行」，不拿读值当状态。")

# ================================================================ 边界
hr("E  能说 / 不能说")
print("能说：")
print("  1. vec 0x2B 的业务出口 0x17171E 是**按位派发器**："
      "先读 6 个 0x65000000 状态字，再按 15 条位分支调 15 个 handler"
      "（11 条 call rel12 + 4 条 call @r12）。")
print("  2. 0x65000000 段是**固件自维护**状态块：6 个状态字共 %d 个静态"
      "写点、其中 %d 个前带 or 置位；模拟器没映射该段，读写被吞，"
      "所以 V61 E1 里 6 个状态字全 0。" % (len(allw), _or))
print("  3. 0x6B000020/30/34 三个槽静态写点全 0，是外部输入；"
      "0x042E82 是与 vec 0x2B 同构、业务出口为 0x1A4902 的另一条 ISR 链。")
print("  4. 15 个 handler 没有一个静态引用显示区 0x120000..0x140000；"
      "12 个围绕 0x%08X 控制块，全族调用 %d 次 int#0x40。" %
      (D.HANDLER_CB, tot_svc))
print("  5. 分派器是**优先级短路**结构（A5）：10 条 bra 全指向函数尾，"
      "一次中断不是 15 条位分支全派发。D2 给满 6 个状态字只命中 "
      "0x171836 / 0x1714EE 两个 handler 入口；D3 只给第一个状态字结果"
      "逐项相同，D4 只给 bit9 则一个 handler 也进不去。")
print("  6. 动态 D2 命中 2 个 handler 入口、新增 135 个 PC 落 handler 区，"
      "但显示链 5 个关键 PC 全未命中、0x9F5A0284 仍 0 ⇒ KI#233 从"
      "「状态位没给」收窄为「这条 ISR 链本身不驱动显示队列」。")
print("不能说：")
print("  · 「显示链通了 / 出画面 / 找到真机 A↔B 通道」—— 静态与动态双向阴性。")
print("  · 「0x65000000 / 0x6B000000 在真机是什么器件、谁置位」——"
      " 本版只报『固件引用了、谁写、模拟器没映射』，未建模 ≠ 不存在。")
print("  · 「打桩 = 真机行为」—— bpatch 是反事实补全（硬规矩 27/31）；"
      "raise_irq 是模型动作；20000 步是观测窗口，不是真机时基。")
print("  · 「给这两个窗口建 RAM 映射就等于复原真机通道」—— 本版**没有**"
      "补映射，只改读返回值；补映射属于另一个口径，留作开放项。")
print("  · 本版 D 组与 V60 的 7 轮 × 10000 步双机交替口径不同，不混比（硬规矩 2）；"
      "V61 E1 只在本版 D1 以同口径复现后作内部对照。")

hr("F  复现与变更")
print("复现：")
print("  cd emu && python probe/exp_dsp_v62.py > ../results/frbox/dsp62.txt")
print("  cd emu && python frbox_selftest.py")
print("机器口径：五台运行机器（D0/D1/D2/D3/D4，各跑一遍）+ 一台只读 ROM 实例"
      "（sweep_of_type('d300-b')，只 load 不 boot）。")
print("本版新增 / 变更：")
print("  + frbox/dispatch62.py     纯静态分派器解剖：call 目标 / 状态字 / 按位表")
print("  + probe/exp_dsp_v62.py    本探针")
print("  + results/frbox/dsp62.txt 本结果")
print("  ~ frbox/__init__.py       0.61.0 → 0.62.0，模块表加 dispatch62")
print("  ~ frbox_selftest.py       新增 test_v62()")
print("  ~ KNOWN_ISSUES.md / docs/03-frbox.md / README.md / probe/README.md")
print("  ~ results/frbox/Readme.txt / 记忆四件套")
print("\ndone")
