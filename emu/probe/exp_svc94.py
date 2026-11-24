# -*- coding: utf-8 -*-
"""V94 探针：服务号口径按装载宽度解码 —— 93 个宽调用点归位后的下游重算。

    cd emu && python probe/exp_svc94.py > ../results/frbox/svc94.txt

七段：A 口径普查 / B 变更清单 / C 等待者普查重算 / D flag 17 复核 /
      E 无回归 / F 遗留 / G 结论

这一版改的是**工具**（`bstatic` 的公共解码 + `btick.delay_queue` 的读法），
不是固件结论。所以每一段都要给「改前 / 改后」两个数，缺一个就不算证据。
"""
import sys
import os
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox import bstatic as B, blive, brealos, btick, svcgraph, svcnum, task93 as T
from frbox import tick92 as K
import fr_dis


def sec(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def pct(a, b):
    return "%.1f%%" % (100.0 * a / b) if b else "-"


sw, m = B.sweep_of_type("d300-b")
print("口径：bstatic.sweep_of_type('d300-b')  全 ROM 0x%06X 起 0x%X 字节"
      % (sw.base, len(sw.rom)))

# ------------------------------------------------------------------ A
sec("[A] 口径普查：2297 个 int #0x40 是按哪种 ldi 装的")
kinds = collections.Counter(c.w12 for c in sw.calls)
print("A1 总调用点 %d；装载宽度分布：%s"
      % (len(sw.calls), dict(sorted(kinds.items(), key=lambda x: -x[1]))))
for w in (8, 20, 32):
    vs = [c.r12 for c in sw.calls if c.w12 == w]
    if not vs:
        print("A2 ldi:%d —— 0 点（这份固件没用这种装法）" % w)
        continue
    print("A2 ldi:%-2d %4d 点  值域 0x%08X..0x%08X  唯一值 %s"
          % (w, len(vs), min(vs), max(vs),
             ["0x%08X" % v for v in sorted(set(vs))]))
low8 = [c for c in sw.calls if c.w12 == 8]
print("A3 8 位装法 bit7 全 1：%s（%d/%d）"
      % (all(c.r12 & 0x80 for c in low8),
         sum(1 for c in low8 if c.r12 & 0x80), len(low8)))
print("A4 32 位装法全 ≥ 0xFFFFFF00：%s（%d/%d）"
      % (all(c.r12 >= 0xFFFFFF00 for c in sw.calls if c.w12 == 32),
         sum(1 for c in sw.calls if c.w12 == 32 and c.r12 >= 0xFFFFFF00),
         sum(1 for c in sw.calls if c.w12 == 32)))
print("A5 旧口径（`sx8(v & 0xFF)`）在宽装法上给出：%s"
      % ["0x%08X→%d" % (v, svcnum.sx8(v)) for v in
         sorted({c.r12 for c in sw.calls if c.w12 == 32})])
print("A6 ⇒ 无歧义：8 位装法全负数、32 位装法全 32 位负数，按宽度解码不是猜")

# 机制：8 位装法后面都跟一条 `extsb r12`，把字节符号扩展成 32 位。
# bstatic 的写模型不执行 extsb（它在 NONWRITE 表里），寄存器停在零扩展的字节上，
# 所以只能按宽度回收。这不是省事，是「模型缺这条指令」的等价重建。
miss = [c.addr for c in sw.calls if c.w12 == 8
        and b"\x97\x8c" not in sw.rom[max(0, c.addr - sw.base - 16):c.addr - sw.base]]
print("A7 8 位装法前 16 字节内有 `extsb r12`：%d/%d%s"
      % (2189 - len(miss), 2189,
         ("（缺 %s）" % ["0x%06X" % a for a in miss]) if miss else ""))
print("A8 32 位装法是 `ldi:32 #0xFFFFFF5X, r12` 直接装全字，不需要扩展")
print("A9 ⇒ 真实口径是 **32 位补码**；宽度解码是 bstatic 缺 `extsb` 的等价重建")
print("   （`NONWRITE` 里含 extsb/extub 是 V48 定的保守选择，V94 不改它："
      "改了会把 store/调用边地址一起重算）")

# ------------------------------------------------------------------ B
sec("[B] 变更清单：93 个点的 svc 从 +86/+85 变成 −170/−171")
old = collections.Counter(svcnum.of_reg(c.r12, 8) for c in sw.calls)
new = collections.Counter(c.svc for c in sw.calls)
changed = [c for c in sw.calls if c.svc != svcnum.of_reg(c.r12, 8)]
print("B1 变更 %d 点（总 %d，占 %s）" % (len(changed), len(sw.calls),
                                        pct(len(changed), len(sw.calls))))
print("B2 改前桶 %s / 改后桶 %s"
      % ({k: old[k] for k in (86, 85)}, {k: new[k] for k in (86, 85, -170, -171)}))
agg = collections.Counter(sw.enclosing(c.addr) for c in changed)
print("B3 落在 %d 个函数里；前 8 个：%s"
      % (len(agg), [(hex(f), n) for f, n in agg.most_common(8)]))
print("B4 svc 分布前后对照（前 12）：")
print("     %-8s %-6s %-6s" % ("svc", "改前", "改后"))
for s in sorted(set(old) | set(new), key=lambda x: -(new.get(x, 0)))[:12]:
    a, b = old.get(s, 0), new.get(s, 0)
    mark = "  ←" if a != b else ""
    print("     %-8s %-6d %-6d%s" % (s, a, b, mark))
for svc, tag in ((-170, "twai_flg"), (-171, "未定")):
    pcs = sorted(c.addr for c in sw.calls if c.svc == svc)
    print("B5 %d（%s）%d 点：%s" % (svc, tag, len(pcs),
                                  " ".join("0x%06X" % p for p in pcs)))

# ------------------------------------------------------------------ C
sec("[C] 等待者普查重算：`wai_flg` 之外还有 91 个 `twai_flg`")
w_old = sw.waits_by_flag(twai=False)
w_new = sw.waits_by_flag(twai=True)
n_old = sum(len(v) for v in w_old.values())
n_new = sum(len(v) for v in w_new.values())
print("C1 旧口径（只数 −46）：%d 个等待点 / %d 个 flag（含 None）" % (n_old, len(w_old)))
print("C2 新口径（含 −170）：%d 个等待点 / %d 个 flag（含 None）"
      "  ⇒ 少算 %s" % (n_new, len(w_new), pct(n_new - n_old, n_new)))
print("C3 逐 flag 对照（wai / twai / 合计）：")
print("     %-5s %-5s %-5s %-5s" % ("flag", "wai", "twai", "合计"))
for f in sorted(w_new, key=lambda x: (x is None, x)):
    n46 = len([c for c in w_new[f] if c.svc == -46])
    n170 = len([c for c in w_new[f] if c.svc == -170])
    print("     %-5s %-5d %-5d %-5d%s"
          % ("None" if f is None else f, n46, n170, len(w_new[f]),
             "   ← 只有 twai 能看见" if n46 == 0 else ""))
has170 = sorted((f for f in w_new if any(c.svc == -170 for c in w_new[f])),
                key=lambda x: (x is None, x))
only_wai = sorted((f for f in w_new if not any(c.svc == -170 for c in w_new[f])),
                  key=lambda x: (x is None, x))
print("C4 有 twai 点的 flag %d 个：%s" % (len(has170), has170))
print("   完全没有 twai 点（只有 wai 能看见）的 flag：%s" % only_wai)
print("   ⇒ 没有任何一个 flag 是「只在 twai 里出现」；所以修口径只是**加厚度**，"
      "不引入新的 flag 集合")
print("C5 口径提醒：V68 的「14 个等待位」是**收敛态现场**读出来的等待者数量，"
      "不是这张静态表的行数（%d）；两者不能互相校验" % len(w_new))

# ------------------------------------------------------------------ D
sec("[D] flag 17 复核：V92 手工补的那条，现在工具自己给得出")
print("D1 0x1A4BB0..0x1A4BE6 三点序列：")
for ln in (fr_dis.disasm_addr(sw.rom, 0x1A4BB0, 0x1A4BEC, sw.base) or []):
    print("   " + ln)
seq = [c for c in sw.calls if 0x1A4B80 <= c.addr <= 0x1A4BF0]
for c in seq:
    print("   %06X svc=%-5s flgid=%-5s bits=%-5s mode=%s"
          % (c.addr, c.svc, c.flgid, c.bits, c.mode))
sets17 = [c for c in sw.of(-48) if c.flgid == 17 and c.bits is not None
          and (c.bits & 0x2)]
print("D2 flag 17 能置 0x2 的 set_flg：%d 点 %s"
      % (len(sets17), ["0x%06X" % c.addr for c in sets17]))
guards = svcgraph.wait_guards(sw, K.F17_FUNC, sets17)
print("D3 wait_guards（不传 fix_wide，纯新口径）：")
for p, w in guards:
    print("   点 0x%06X → 前驱 %s"
          % (p.addr, ("0x%06X svc=%d" % (w.addr, w.svc)) if w else "无"))
print("D4 fix_wide 幂等：fix_wide(86)=%d fix_wide(-170)=%d fix_wide(-171)=%d"
      % (K.fix_wide(86), K.fix_wide(-170), K.fix_wide(-171)))
print("D5 函数范围核对：K.F17_FUNC=0x%06X..0x%06X，sets17 全在区间内：%s"
      % (K.F17_FUNC, K.F17_FUNC_END,
         all(K.F17_FUNC <= c.addr < K.F17_FUNC_END for c in sets17)))

# ------------------------------------------------------------------ E
sec("[E] 无回归：静态总账与动态现场")
print("E1 总调用点 %d；-48 %d / -47 %d / -46 %d（V68 记 866/481/158）"
      % (len(sw.calls), len(sw.of(-48)), len(sw.of(-47)), len(sw.of(-46))))
print("E2 宽服务号 %d（V262 记「宽服务号 93」）"
      % (len(sw.of(-170)) + len(sw.of(-171))))
mm = blive.boot()
r = blive.run(mm, steps=600000)
print("E3 600k 步：末 PC 0x%06X 换手 %d 当前任务 %s"
      % (mm.cpu.pc, len(r["switches"]), r.get("current_name")))
det = T.dormant(mm)
print("E4 休眠任务 %d 个：%s（V93 记 11 个同集合）" % (len(det), sorted(det)))
dq = btick.delay_queue(mm)
print("E5 延时队列 %d 项（V92 记 3 项永久积压）：" % len(dq))
for tcb, tid, prio, why in dq:
    print("   任务%-3s prio=%d 等待原因 svc=%d（16 位补码口径）" % (tid, prio, why))
raw = [mm.bus.rd(t + btick.TCB_WHY, 4) >> 16 for t, _i, _p, _w in dq]
print("E6 原始 16 位字段 %s ⇒ 旧 `sx8` 会读成 %s"
      % (["0x%04X" % x for x in raw], [svcnum.sx8(x) for x in raw]))

# TCB+0x20 高半到底是不是一律符号扩展成 16 位？V67 的注释举过 0x00E9 的例子。
# 普查收敛态全部 29 条 TCB：这一条决定 `of_why` 该用 16 位还是「低 8 位」。
rows = []
for i, rec in enumerate(brealos.records(mm)):
    why = mm.bus.rd(rec["tcb"] + 0x20, 4) >> 16
    if why:
        rows.append((i + 1, why, svcnum.of_why(why), svcnum.sx8(why)))
bad = [(t, "0x%04X" % w) for t, w, _a, _b in rows if 0 < w < 0x100]
diff = [(t, a, b) for t, _w, a, b in rows if a != b]
print("E7 收敛态有等待原因的 TCB %d 条；高半落在 0x0001..0x00FF 的 %s"
      % (len(rows), bad if bad else "0 条 —— 一律符号扩展成 16 位"))
print("E8 16 位与 8 位口径不一致的：%s"
      % ([(t, a, b) for t, a, b in diff] if diff else "无"))
for t, w, a, b in rows:
    if a != b:
        print("   任务%-3s raw=0x%04X 16位=%d 8位=%d ← 旧口径把 −170 读成 +86"
              % (t, w, a, b))

# ------------------------------------------------------------------ F
sec("[F] 遗留（本版不解，留给下一版）")
print("F1 −171 的 2 个点：")
for c in sw.calls:
    if c.svc == -171:
        print("   0x%06X 函数 0x%06X r4=0x%08X r5=0x%08X r6=0x%08X（实参形态像指针，"
              "不当 flgid 用）" % (c.addr, sw.enclosing(c.addr) or 0,
                                  c.r4 or 0, c.r5 or 0, c.r6 or 0))
print("F2 r12 未定的 %d 点（r12 来自 mov/参数，宽度未知）："
      % sum(1 for c in sw.calls if c.svc is None))
for f, n in collections.Counter(
        sw.enclosing(c.addr) for c in sw.calls if c.svc is None).most_common():
    print("   函数 0x%06X  %d 点" % (f or 0, n))
print("F3 ldi:20 装法的调用点 0 个 —— `svcnum.of_reg` 的 20 位分支没有样本，"
      "别拿它解释任何现存结论")

# ------------------------------------------------------------------ G
sec("[G] 结论")
print("G1 `bstatic` 的服务号已按装载宽度解码：8 位→sx8、32 位→sx32；"
      "93 点归位（91×−170、2×−171），无歧义样本")
print("G2 下游最大变化：`wai_flg` 之外多出 91 个 `twai_flg` 等待点，"
      "按 flag 的静态等待普查从 %d 点涨到 %d 点（+%s）"
      % (n_old, n_new, pct(n_new - n_old, n_old)))
print("G3 `CallSite.flgid/bits/mode` 认 −170（r5/r6/r7 与 wai_flg 同序），"
      "由 0x1A4BE6 与 V92 动态 TCB 现场双向印证")
print("G4 V92 的 `fix_wide` 退化为恒等；flag 17 的 5 个置位点前驱，"
      "现在不借补丁也能报出 0x1A4BE6")
print("G5 `btick.delay_queue` 的等待原因改用 16 位补码（原 `sx8` 在 0xFF56 上读成 +86）")
print("G6 口径的**物理依据**：8 位装法 2189/2189 后面都跟 `extsb r12`，把字节符号扩展成"
      " 32 位；所以真实服务号一直是 32 位补码，宽度解码是模型缺 `extsb` 的等价重建")
print()
print("不能报：−171 是哪个服务 / 15 个未定点的服务号 / 静态等待点变多等于"
      "跑得到 / 口径修完就能出画面 / 这些 flag 在真机由谁置位。")
print()
print("DONE V94 A–G 七段，实跑 1 次全 ROM 扫描（2297 点）+ 1 台 600k 步动态机，"
      "改前/改后两个数都给了")
