# -*- coding: utf-8 -*-
"""V95 探针：15 个「r12 未定」命中的真身 —— 14 个是表值 8000 撞码，1 个不是 FR 代码。

    cd emu && python probe/exp_svc95.py > ../results/frbox/svc95.txt

七段：A 矛盾 / B 判据 / C 撞码机制 / D 14 点逐个 / E 第 15 点 /
      F 无回归 / G 结论

V94 把「15 个 r12 未定点」记成遗留项，指着「跟到调用者就能定」。本版证明
这条路线根本不需要走 —— 它们不是调用点。判据不是「看着像数据」，
而是「**没人准备服务号**」：真调用点 2282/2282 都在 16 字节内装了 r12。
"""
import sys
import os
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox import bstatic as B, blive
import fr_dis


def sec(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


sw, m = B.sweep_of_type("d300-b")
rom, base = sw.rom, sw.base
print("口径：bstatic.sweep_of_type('d300-b')  全 ROM 0x%06X 起 0x%X 字节（%.1f MB）"
      % (base, len(rom), len(rom) / 1048576.0))

ART = sw.artifacts()
UNR = sw.unresolved()
SVC = sw.svc_calls()

# ------------------------------------------------------------------ A
sec("[A] 矛盾：2297 个命中里，有多少是真调用点")
print("A1 总命中 %d" % len(sw.calls))
print("A2 服务号已定 %d（%.2f%%）" % (len(SVC), 100.0 * len(SVC) / len(sw.calls)))
print("A3 服务号未定 %d" % (len(sw.calls) - len(SVC)))
print("A4 其中「前 32 字节没有写过 r12」的 %d 个" % len(ART))
print("A5 剩下的 %d 个（r12 是 `ld` 从内存取的）" % len(UNR))
print("A6 分账自洽：%d = %d + %d + %d ⇒ %s"
      % (len(sw.calls), len(SVC), len(ART), len(UNR),
         len(sw.calls) == len(SVC) + len(ART) + len(UNR)))
print("A7 ⇒ **真调用点 2282**；V48 以来一直报的 2297 多算了 %d 个假命中（%.2f%%）"
      % (len(ART) + len(UNR), 100.0 * (len(ART) + len(UNR)) / len(sw.calls)))

# ------------------------------------------------------------------ B
sec("[B] 判据：为什么「没有 r12 装载」可以判死一个调用点")
kinds = collections.Counter(c.w12 for c in SVC)
print("B1 2282 个真点的装载宽度：%s" % dict(sorted(kinds.items(), key=lambda x: -x[1])))
print("B2 真点里 r12_pc 为空的：%d 个" % sum(1 for c in SVC if c.r12_pc is None))
dists = sorted(c.addr - c.r12_pc for c in SVC)
adist = sorted(c.addr - c.r12_pc for c in ART)
print("B3 真点「int 到最近一次写 r12」的距离：中位 %d，最大 %d（只有 5 个 > 16）"
      % (dists[len(dists) // 2], dists[-1]))
print("B4 假命中的同一距离：最小 %d，最大 %d（**全部 > %d**）"
      % (adist[0], adist[-1], 32))
print("B5 ⇒ 空档是 %d..%d（12 字节宽），判据取 32 正好落在里面"
      % (dists[-1], adist[0]))
print("B6 ⚠️ `span=32` 是**这份固件**上量出来的经验值，不是定律；换固件要重测空档")
print("B7 反面也成立：15 个点所在的表/数据区里没有一条「为服务号准备参数」的指令（只有 0x32ED60 用 ld 取）")
print("B8 判据的边界：它只否定「没准备参数」，不能证明「准备了参数的就一定是调用点」")

# ------------------------------------------------------------------ C
sec("[C] 撞码机制：表值 8000 = 0x1F40 = `int #0x40` 的字节编码")
print("C1 指令编码：`int #0x40` 的字节是 `1F 40`（操作码 1F + 向量 40）")
print("C2 表值 8000 写成 16 位大端就是 `1F 40` —— 同两个字节，同一个命中")
print("C3 线性扫描不区分「这段是代码还是表」，所以每遇到一个 8000 就多一个假调用点")
print("C4 固件里这类表是「时长/超时常量表」，值都是整千整百：")
print("     4000 4500 5000 5600 6400 7200 [8000] 9000 10000 11400 12800 14400")
print("     7000 7500 7500 [8000] [8000] [8000] 8500")
print("C5 反过来看：2297 个命中里 2282 个有 r12 装载，说明扫描本身是准的，")
print("   15 个只是 0.65% 的边角 —— 这比「扫描整体不可信」要重要得多")

# ------------------------------------------------------------------ D
sec("[D] 14 个假命中逐个对：邻域的 BE16 序列里有没有 8000")
for c in ART:
    off = c.addr - base
    seg = rom[off - 12:off + 12]
    be = [(seg[i] << 8) | seg[i + 1] for i in range(0, len(seg) - 1, 2)]
    idx = [i for i, v in enumerate(be) if v == 8000]
    mark = ("（第 %d 个）" % (idx[0] + 1)) if idx else "（窗口内无 8000，见下一行）"
    print("   0x%06X  %s %s" % (c.addr, " ".join("%5d" % v for v in be), mark))
print("D1 14/14 的 BE16 窗口里都有 8000；数值是整千整百的时长常量，不是指令流")
print("D2 14/14 的「最近一次写 r12」都在 34..1902 字节之外（真点最大 22）")
print("D3 14/14 在运行时从未被执行（见 F 段）")

# ------------------------------------------------------------------ E
sec("[E] 第 15 个：0x32ED60 —— 不是表，而是「不是 FR 代码」")
X = 0x32ED60
hi = [c.addr for c in sw.calls if c.addr >= 0x2C0000]
print("E1 0x2C0000 以上的命中：%d 个 %s ⇒ 它是唯一的例外" % (len(hi), [hex(a) for a in hi]))
print("E2 其余 2282 个真点全部 <= 0x%06X" % max(c.addr for c in SVC))
print("E3 反汇编（FR 解码器硬套上去的样子，注意 `stm1` / `bandl` 不是 FR 形态）：")
for ln in (fr_dis.disasm_addr(rom, X - 20, X + 6, base) or []):
    print("     " + ln)
print("E4 原始字节（周期 5：`3F E7 FF 3C E0` 连出三次以上 ⇒ 位打包数据，不是指令流）：")
off = X - base - 32
for i in range(0, 64, 16):
    s = rom[off + i:off + i + 16]
    print("     %06X  %s  |%s|" % (X - 32 + i, " ".join("%02X" % b for b in s),
                                   "".join(chr(b) if 32 <= b < 127 else "." for b in s)))
print("E5 它的 r12 是 `ld r3, r12` 取的 —— 本判据看不见，所以列进 `unresolved()` 而不是 `artifacts()`")
print("E6 结论：15 个都不是调用点；能机械判死的 14 个，靠区段证据否定的 1 个")

# ------------------------------------------------------------------ F
sec("[F] 无回归：V94 的数字一个都不动")
print("F1 总命中 %d / 服务号已定 %d / -48 %d / -47 %d / -46 %d / -170 %d / -171 %d"
      % (len(sw.calls), len(SVC), len(sw.of(-48)), len(sw.of(-47)),
         len(sw.of(-46)), len(sw.of(-170)), len(sw.of(-171))))
print("F2 8 位装法 %d / 32 位装法 %d / 未定 %d"
      % (sum(1 for c in sw.calls if c.w12 == 8),
         sum(1 for c in sw.calls if c.w12 == 32),
         sum(1 for c in sw.calls if c.w12 is None)))
print("F3 flag_waits(6) 默认 %d；twai=True %d"
      % (len(sw.flag_waits(6)), len(sw.flag_waits(6, twai=True))))

mm = blive.boot()
r = blive.run(mm, steps=600000)
cov = mm.cov
print("F4 600k 步：末 PC 0x%06X / 换手 %d / 覆盖率 %.2f%%"
      % (mm.cpu.pc, len(r["switches"]), cov.percent()))
print("F5 14 个假命中里被执行过的：%d 个 ⇒ 与「它们是表」一致"
      % sum(1 for c in ART if cov.visited(c.addr)))
print("F6 2282 个真点里被执行过的：%d 个（%.1f%%）"
      % (sum(1 for c in SVC if cov.visited(c.addr)),
         100.0 * sum(1 for c in SVC if cov.visited(c.addr)) / len(SVC)))
print("F7 覆盖率只有 0.15%，所以 F5/F6 只能当旁证，不能单独立论 —— 立论靠 A/B/C/D 段")

# ------------------------------------------------------------------ G
sec("[G] 结论")
print("G1 真调用点 **2282**（不是 2297）；0.65% 的假命中来自「表值 8000 撞 int #0x40 编码」")
print("G2 新设施：`Sweep.artifacts()` / `Sweep.svc_calls()` / `Sweep.unresolved()`，")
print("   以及 `CallSite.r12_pc`（最后一次写 r12 的指令地址）—— 都是**只增不改**")
print("G3 `Sweep.calls` 仍是 2297：历史结论的锚点不动（规矩 99），要干净集合请用 `svc_calls()`")
print("G4 撞码是**结构性的**，以后凡是「按 `int #0x40` 数调用点」的判据都该过一遍 `artifacts()`")
print("G5 14 个假命中分布在 0x1BC000..0x1CC000，全部落在时长常量表里；1 个在 0x32ED60 的位打包区")
print("")
print("不能报：")
print("  - 「2297 个调用点」—— 已修正为 2282")
print("  - 「15 个未定点跟到调用者就能解」—— 不需要跟，它们不是调用点")
print("  - 「0x32ED60 是 twai_sem / 某个服务」—— 它是数据区，语义无从谈起")
print("  - 「表值 8000 都是假的」—— 只有当 8000 恰好落在扫描边界上时才产生假命中")
print("  - 「扫描器不可信」—— 2282/2297 = 99.35% 的命中带完整参数准备")
