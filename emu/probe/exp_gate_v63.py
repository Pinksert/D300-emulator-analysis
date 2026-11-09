# -*- coding: utf-8 -*-
"""V63 正式探针：四条同族门、被认错的公共函数，以及补映射实验。

    cd emu && python probe/exp_gate_v63.py > ../results/frbox/gate63.txt

六段：
  A  静态：ROM 中断向量表反查 —— 三个槽挂在 vec 0x26 / 0x2B / 0x2C
  B  静态：更正 KI#239 —— 0x12E6F8 是 312 个调用点的公共状态查询函数
  C  静态：0x65000000 状态块 47 个写点的归属
  D  静态：两窗口读写图与 0x6B7335B0 的口径更正
  E  动态：G0–G4 五组同尺补映射实验（KI#238）
  F  能说 / 不能说 + 复现与变更

纪律：
  · 静态只读 ROM，不建运行机器；`call rel12` 按 `fr_emu.py:777` 算
    （硬规矩 40）；`call:d`/`call` 下一条是延迟槽（硬规矩 19）。
  · 判 ISR 归属先按 `TBR + (255-v)*4` 反查向量表，不靠 `enclosing()`
    （硬规矩 30 / 43：无 `enter` 的 ISR 会归错）。
  · 动态每台机器只跑一遍（硬规矩 13），不自写 `cpu.step()` 循环
    （硬规矩 1）；设备策略用 `devaudit.audit()` 显式声明（硬规矩 38），
    不改 `create()` 默认值。
  · `bobserve.observe()` 先装、`bpatch.patch_read()` 后装（硬规矩 34）。
  · 补 `map_ram` 是本版唯一的**机器结构改动**，属模型化动作；
    `bpatch` 是反事实打桩，`raise_irq` 是模型动作，三者分开记。
  · 观察日志值按十进制打印（硬规矩 39）；静态「有没有」与动态
    「跑没跑」分开（硬规矩 5）；口径不同数字不混比（硬规矩 2）。
  · 本版 E 组 20000 步窗口与 V60 双机交替、V62 D 组五组同尺但
    **多了映射**，与 V62 的 D0/D1/D2 只在 G0/G3/G4 三处同口径对照。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import (addrform, blive, bobserve, bpatch, bstatic, devaudit,
                   dispatch62 as D, isrfam63 as F)
import fr_emu

# ---------------------------------------------------------------- 常量
BOOT, RUN, VEC = 191520, 20000, 0x2B
FRAME = 0x000083B8
FRAME_PCS = (0x042E40, 0x042E50)
STRUCT_FLAG = 0x9F5A0284
STATUS_ADDRS = tuple(F.STATUS_BASE + o for o in D.STATUS_OFFSETS)
STATUS_RULES = tuple(zip(STATUS_ADDRS, D.STATUS_READ_PCS))
MAP_SIZE = 0x1000
DSP_LO, DSP_HI = 0x120000, 0x140000


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ================================================================ 静态 ROM
hr("A  静态：ROM 中断向量表反查 —— 三个外部输入槽各挂一个中断向量")
SW, _M = bstatic.sweep_of_type("d300-b")
ROM, BASE = SW.rom, SW.base
print("A0 B 段 ROM %d 字节 / 基址 0x%06X；本段只读 ROM，不建运行机器。" %
      (len(ROM), BASE))

tab = F.vector_table(ROM, BASE)
print("A1 向量表 TBR=0x%06X，公式 slot = TBR + (255 - v) * 4（V54 实证）" % F.TBR)
print("   vec 0x18 槽 = 0x%06X（V54 的 0x000EFF9C）" % F.vec_slot(0x18))
print("   vec_of_slot(0x000EFF9C) = 0x%02X" % F.vec_of_slot(0x000EFF9C))
print("   vec 0x2B 槽 = 0x%06X" % F.vec_slot(0x2B))
entries = collections.Counter(v.entry for v in tab)
print("   256 槽里 %d 个填默认入口 0x%06X；非默认槽 %d 个、不同入口值 %d 个" %
      (entries[F.DEFAULT_HANDLER], F.DEFAULT_HANDLER,
       256 - entries[F.DEFAULT_HANDLER],
       len([1 for e, n in entries.items() if e != F.DEFAULT_HANDLER])))

print("\nA2 四条同族门与向量表的逐项核验")
rows = F.gate_rows(tab)
for g, entry, ok in rows:
    print("   slot=%s vec=0x%02X 表项=0x%06X ISR=0x%06X 门PC=%s mask=%s "
          "出口=%s call=%d 一致=%s" %
          (("0x%08X" % g.slot) if g.slot else "-", g.vec, entry or 0,
           g.isr, ("0x%06X" % g.gate_pc) if g.gate_pc else "-",
           ("0x%08X" % g.mask) if g.mask else "-",
           ",".join("0x%06X" % x for x in g.exits), g.calls, ok))
print("   四条门全部与真实向量表一致：%s" % all(ok for g, e, ok in rows))

print("\nA3 双调用的 r4 参数在延迟槽里（硬规矩 19）")
third = F.decode_range(ROM, BASE, 0x042B2A, 0x042B70)
args3 = [(p, v) for p, v in F.delayed_args(ROM, BASE, third)
         if p in F.THIRD_DOUBLE_PCS]
print("   vec 0x26 两次 call:d 的 rel12 目标：0x%06X / 0x%06X" %
      (D.rel12_target(F.THIRD_DOUBLE_PCS[0], F.THIRD_DOUBLE_RELS[0]),
       D.rel12_target(F.THIRD_DOUBLE_PCS[1], F.THIRD_DOUBLE_RELS[1])))
print("   延迟槽 r4 参数：%s" % [(hex(p), v) for p, v in args3])
sib = F.decode_range(ROM, BASE, 0x042E72, 0x042EBE)
args2 = [(p, v) for p, v in F.delayed_args(ROM, BASE, sib)
         if p in F.SIBLING_DOUBLE_PCS]
print("   vec 0x2C 两次 call:d 0x%06X 的延迟槽 r4 参数：%s" %
      (F.SIBLING_TARGET, [(hex(p), v) for p, v in args2]))
print("   ⇒ 不是 15 条位分支全派发（硬规矩 42），而是「同一出口、两种通道参数」；"
      "r4 是 0x%04X 表（80 字节步长）/ 0x%06X 表（4 字节步长）的通道号。" %
      (F.CHAN_TABLE_80, F.CHAN_TABLE_4))

print("\nA4 无 enter 的 ISR 序言（硬规矩 43）")
pro = F.decode_range(ROM, BASE, 0x042B2A, 0x042B40)
print("   vec 0x26 ISR 0x042B2A 开头：%s" % " | ".join(x.mn for x in pro[:8]))
print("   is_prologue() = %s；Sweep.enclosing(0x042B46) = 0x%06X（归到无关函数）" %
      (F.is_prologue(pro), SW.enclosing(0x042B46) or 0))
pro2 = F.decode_range(ROM, BASE, 0x042E1C, 0x042E30)
print("   vec 0x2B ISR 0x042E1C 开头：%s → %s" %
      (" | ".join(x.mn for x in pro2[:8]), F.is_prologue(pro2)))

# ---------------------------------------------------------------- B
hr("B  静态：更正 KI#239 —— 0x12E6F8 是 312 个调用点的公共函数")
body_lo, body_hi, body = D.handler_bounds(ROM, BASE, F.DSP_FN, 0x400)
print("B1 0x%06X 序言 = %s → %s；函数体 0x%06X..0x%06X 共 %d 条" %
      (F.DSP_FN, " | ".join(x.mn for x in body[:3]),
       F.is_prologue(body), body_lo, body_hi, len(body)))
refs, kinds, segs = F.ref_kinds(SW, F.DSP_FN, window=0)
print("B2 refs_to(window=0) %d 处；下一条指令形态 %s" %
      (len(refs), dict(kinds)))
print("   调用点按 64KB 段：%s" %
      ", ".join("0x%02Xxxxx:%d" % (s, n) for s, n in sorted(segs.items())))
print("   落显示区 0x%06X..0x%06X：%d / %d" %
      (DSP_LO, DSP_HI,
       len([1 for r in refs if DSP_LO <= r.pc < DSP_HI]), len(refs)))
print("   落 handler 家族 0x1714EE..0x172400：%d；落 ISR 区：%d" %
      (len([1 for r in refs if 0x1714EE <= r.pc < 0x172400]),
       len([1 for r in refs if 0x040000 <= r.pc < 0x043000])))
fns = sorted(set(SW.enclosing(r.pc) for r in refs if SW.enclosing(r.pc)))
print("   分布在 %d 个 enclosing 函数里（V62 写「全在 0x120xxx」不成立）" %
      len(fns))
print("B3 函数体做什么：r4*%d 索引 0x%08X 对象池，%d 次 int#0x40" %
      (F.DSP_FN_OBJ_STRIDE, F.DSP_FN_OBJ,
       sum(1 for x in body if x.mn == "int" and x.vals.get("u8") == 0x40)))
print("   9F5A 段引用：%s" %
      [(hex(p), hex(v)) for p, k, v in
       F.imm_refs(body, 0x9F500000, 0x9F600000)])
print("   显示区引用：%s" %
      [(hex(p), hex(v)) for p, k, v in F.imm_refs(body, DSP_LO, DSP_HI)])
allargs = collections.Counter()
for r in refs:
    if r.next != "call:d":
        continue
    seq = F.decode_range(ROM, BASE, r.pc + 8, r.pc + 14)
    if seq and seq[0].mn.startswith("ldi") and seq[0].vals.get("ri") == 4:
        v = seq[0].vals.get("i8", seq[0].vals.get("i20"))
        allargs[v] += 1
print("B4 302 条 call:d 延迟槽的 r4 参数前 8：%s" % allargs.most_common(8))
print("   ⇒ r4 是对象/通道索引；显示链判据应改为「这个函数体是否被覆盖执行」"
      "与「312 个调用点里命中几个」，不再把该地址当数据对象读值。")

# ---------------------------------------------------------------- C
hr("C  静态：0x65000000 状态块 47 个写点的归属")
allw, inside, outside = F.status_writer_buckets(SW, STATUS_ADDRS)
print("C1 六个状态字共 %d 个可定址写点；落 handler 家族区 %d / 其它 %d" %
      (len(allw), inside, outside))
fnc = collections.Counter(SW.enclosing(p) for p in allw if SW.enclosing(p))
print("   按 enclosing(enter) 前 6：%s" %
      ["0x%06X:%d" % (f, n) for f, n in fnc.most_common(6)])
print("   写点区间 0x%06X..0x%06X，与分派器 0x%06X 同属一片模块" %
      (allw[0], allw[-1], D.DISPATCH_ENTRY))
print("C2 六个状态字的 ldi:32 直接加载点：%s" %
      [len(SW.ldi32.get(a, [])) for a in STATUS_ADDRS])
print("   ⇒ 全 0 是因为分派器用「基址 0x%08X 装 r4 + 偏移装 r13 + "
      "ld @(r13,r4)」形态（硬规矩 25），不是没有读者。" % F.STATUS_BASE)
print("C3 静态有 42 个写点前带 or 置位（V62），但静态「有」不等于动态「跑了」"
      "（硬规矩 5）；本版 E 组逐笔数固件实际写入。")

# ---------------------------------------------------------------- D
hr("D  静态：两窗口读写图与 0x6B7335B0 的口径更正")
for a in F.GATE_SLOTS:
    r = SW.refs_to(a, window=0)
    print("D1 0x%08X：ldi:32 加载 %d 处（%s），writers_of %d 处" %
          (a, len(r), ["0x%06X" % p for p, i, n in r],
           len(SW.writers_of(a))))
r4 = SW.refs_to(F.EXTRA_SLOT, window=0)
print("D2 0x%08X：加载点 %s，writers_of %d 处" %
      (F.EXTRA_SLOT, ["0x%06X" % p for p, i, n in r4],
       len(SW.writers_of(F.EXTRA_SLOT))))
seq = F.decode_range(ROM, BASE, F.EXTRA_SLOT_REF, F.EXTRA_SLOT_REF + 12)
print("   0x%06X 后续：%s" %
      (F.EXTRA_SLOT_REF, " | ".join("%s" % x.mn for x in seq[:5])))
print("   ⇒ V62 写「静态写点全 0」是 `writers_of()` 口径（只认可定址立即数）；"
      "这里是基址装载后的 `st @(rX, r0)` 间接写，工具看不见（硬规矩 25/31）。"
      "两个口径不混比（硬规矩 2）。")
print("D3 窗口补映射可行性：向量表 0x000EFC00..0x000F0000（256 槽 × 4B）；"
      "两段 0x65000000 / 0x6B000000 当前都无 region、读默认 0；"
      "`bus.map_ram(base, 0x1000)` 可插入且不重叠（侦查 B 实证写后读回 "
      "0xDEADBEEF）。本版只在 E 组运行时机器上补，不改 `create()` 默认值"
      "（硬规矩 38）。")

# ================================================================ 动态
hr("E  动态：G0–G4 五组同尺补映射实验（KI#238）")

# 按真实反汇编的 PC 集合取判据，不用等距偶数地址：函数体里有 6 字节
# `ldi:32`，39 条指令并不等于 (0x12E750-0x12E6F8)/2 个 PC。
DSP_FN_PCS = set(x.pc for x in body)
CALLER_PCS = set(r.pc for r in refs)


def case(tag, map65, map6b, frame_on, status_all1):
    """一台机器只跑一遍（硬规矩 13）。补映射是模型化动作，不是真机复原。"""
    m = blive.boot(0)
    pol = devaudit.audit(m)
    if map65:
        m.bus.map_ram(F.STATUS_BASE, MAP_SIZE, "v63-status")
    if map6b:
        m.bus.map_ram(F.GATE_BASE, MAP_SIZE, "v63-gates")
    reads, writes = bobserve.observe(
        m.bus, STATUS_ADDRS + F.GATE_SLOTS)     # 硬规矩 34：先装纯观察
    rules = {}
    if frame_on:
        for p in FRAME_PCS:
            rules[(FRAME, p)] = bpatch.bit_set(1)
    if status_all1:
        for a, p in STATUS_RULES:
            rules[(a, p)] = (lambda v: 0xFFFFFFFF)
    hits = bpatch.patch_read(m.bus, rules) if rules else {}

    m.step(BOOT)
    before = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    m.raise_irq(VEC)
    m.run(max_steps=RUN, spin_guard=False)
    after = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    new = after - before

    print("\n【%s】map65=%s map6b=%s frame=%s status_all1=%s" %
          (tag, map65, map6b, frame_on, status_all1))
    print("   设备策略 xfer=%d dispatch=%d prio=%d cov=%d devices=%d" %
          (pol["xfer"], pol["dispatch"], pol["prio"],
           pol["cov"], pol["devices"]))
    print("   BOOT=%d + 单次 raise_irq(0x%02X) + RUN=%d ⇒ 末 PC=0x%06X" %
          (BOOT, VEC, RUN, m.cpu.pc))
    print("   覆盖 %d → %d（+%d）；vec 0x2B ISR 37 个 PC 命中 %d" %
          (len(before), len(after), len(new),
           len([x.pc for x in addrform.isr_graph(
               ROM, BASE, 0x042E1C, 0x042E72) if x.pc in after])))
    print("   固件对 6 个状态字的写入 %d 笔：%s" %
          (len(writes), [("0x%08X" % t, "0x%06X" % pc, v)
                         for t, pc, a, n, v in writes[:8]]))
    vals = [m.bus.rd(a, 4, 0) for a in STATUS_ADDRS]
    print("   6 个状态字终值（十进制）：%s" % vals)
    print("   47 个静态写点 PC 被覆盖执行 %d 个" %
          len([p for p in allw if p in after]))
    print("   打桩命中（十进制键，硬规矩 39）：%s" %
          (dict(hits) if hits else "无"))
    sreads = [(t, pc, dict(c)) for (t, pc), c in reads.items()
              if F.STATUS_BASE <= t < F.STATUS_BASE + 0x1000
              and pc in D.STATUS_READ_PCS]
    print("   分派器状态字读（满额 6 笔；原始值，排除探针自身 pc=0 的读）："
          "%d 笔 %s" %
          (len(sreads), [(hex(t), hex(p), c) for t, p, c in sreads[:8]]))
    hh = [t for t in D.HANDLERS if t in after]
    print("   15 个 handler 入口命中 %d：%s" %
          (len(hh), ["0x%06X" % t for t in sorted(hh)]))
    print("   显示链 5 个关键 PC：%s" %
          ["0x%06X:%s" % (p, "命中" if p in after else "未命中")
           for p in D.DSP_PCS])
    print("   0x12E6F8 函数体 %d 个真实 PC 覆盖 %d 个；312 个调用点命中 %d 个" %
          (len(DSP_FN_PCS), len(after & DSP_FN_PCS),
           len(after & CALLER_PCS)))
    print("   0x9F5A0284 = %d；0x%08X = %d" %
          (m.bus.rd(STRUCT_FLAG, 4, 0), D.HANDLER_CB,
           m.bus.rd(D.HANDLER_CB, 4, 0)))
    print("   新增 PC 落显示区：%d 个 %s" %
          (len([p for p in new if DSP_LO <= p <= DSP_HI]),
           ["0x%06X" % p for p in sorted(new)
            if DSP_LO <= p <= DSP_HI][:12]))
    return after


g0 = case("G0 基线（V62 D0 同口径）", False, False, False, False)
g1 = case("G1 只补 0x65000000 段 RAM", True, False, False, False)
g2 = case("G2 再补 0x6B000000 段 RAM", True, True, False, False)
g3 = case("G3 两段 + 总闸（V62 D1 同口径）", True, True, True, False)
g4 = case("G4 两段 + 总闸 + 状态全 1（V62 D2 同口径）",
          True, True, True, True)

print("\nE5 同尺对照（五组：BOOT=%d / 单次 IRQ / RUN=%d / 各跑一遍）" %
      (BOOT, RUN))
print("   G1 − G0 = %d 个 PC；G2 − G1 = %d 个 PC；"
      "G3 − G2 = %d 个 PC；G4 − G3 = %d 个 PC" %
      (len(g1 - g0), len(g2 - g1), len(g3 - g2), len(g4 - g3)))
print("   ⇒ 补两段 RAM 映射后控制流逐项不变（硬规矩 8：补对了），"
      "因为观测窗口里固件对 6 个状态字写入 0 笔、47 个写点命中 0 个。")
print("   只有打桩给满状态位（G4）才出现 1 笔固件自己的写："
      "`0x17184A` 把 0x65000008 写成 0（ack 清位）。")
print("   ⚠️ G4 的 6 个状态字内存终值仍是全 0：`bpatch` 只改 `ld` 的读"
      "返回值、不写内存（硬规矩 31），固件自己那笔写又是清位。")
print("   五组显示链 5 PC 全未命中、0x12E6F8 函数体覆盖 0 个 PC、"
      "312 个调用点命中 0 个、0x9F5A0284 与 0x%08X 仍 0。" % D.HANDLER_CB)

# ================================================================ 边界
hr("F  能说 / 不能说 + 复现与变更")
print("能说：")
print("  1. ROM 0x%06X..0x%06X 是 256 槽中断向量表；三个外部输入槽分别挂在 "
      "vec 0x26 / 0x2B / 0x2C，另有 vec 0x3C 一条无槽 ISR。" %
      (F.TBR, F.TBR + 1024))
print("  2. vec 0x26 与 vec 0x2C 是「同一出口、两种通道参数」的双调用"
      "（r4=0/1 装在 call:d 延迟槽里），不是 15 条位分支全派发（硬规矩 19/42）。")
print("  3. 0x12E6F8 是有 312 个调用点（302 条 call:d + 10 条 call）的"
      "公共状态查询函数，跨 %d 个 enclosing 函数、多个 64KB 段；"
      "函数体按 r4*12 索引 0x%08X 对象池。V62 的定性已更正（KI#239）。" %
      (len(fns), F.DSP_FN_OBJ))
print("  4. 补 0x65000000 / 0x6B000000 两段 RAM 映射后，"
      "同尺五组里 G1/G2 与基线覆盖对称差集 0 个 PC；"
      "观测窗口内固件写状态字 0 笔、47 个静态写点命中 0 个。")
print("  5. G4（反事实给满状态位）复现 V62 D2：覆盖 1241→1621、"
      "只命中 2 个 handler 入口，且唯一 1 笔实际写入是 ack 清位。")
print("不能说：")
print("  · 「显示链通了 / 出画面 / 找到真机 A↔B 通道」—— 五组双向阴性。")
print("  · 「补映射等于复原真机通道」—— 本版补的是 RAM 区域，"
      "没有给三个槽建器件、没有外部事件源；映射只让读写不再被吞。")
print("  · 「状态块存在逻辑死锁」—— 本版只观测到「上游事件没来 → 不派发 → "
      "不写状态位」，静态 47 个写点里有 12 个在 handler 区外，"
      "未排除寄存器间接写与未跑到的路径（硬规矩 25/31）。")
print("  · 「0x6B7335B0 没人写」—— `writers_of()` 口径报 0，"
      "但存在 1 处基址装载后的间接 `st`；两口径不混比。")
print("  · 「三个槽在真机是什么器件、谁置 bit0」—— 仍属 KI#237 未结；"
      "本版只解出中断向量号与出口结构。")
print("  · 本版 E 组与 V60 的 7 轮×10000 步双机交替口径不同；"
      "与 V62 D 组只在 G0/G3/G4 三处同口径对照（G1/G2 多了映射）。")

print("\n复现：")
print("  cd emu && python probe/exp_gate_v63.py > ../results/frbox/gate63.txt")
print("  cd emu && python frbox_selftest.py")
print("机器口径：五台运行机器（G0/G1/G2/G3/G4，各跑一遍）+ 一台只读 ROM 实例"
      "（`sweep_of_type()`）。")
print("新设施：frbox/isrfam63.py（纯静态）；新硬规矩 43。")
print("DONE V63")
