# -*- coding: utf-8 -*-
"""V93 探针：11 个休眠任务的创建点图谱与表驱动启动盲区。

    cd emu && python probe/exp_task93.py > ../results/frbox/task93.txt

七段：A 休眠现场 / B 创建点映射 / C 表驱动机制 / D 分组表 /
      E 补时基对照 / F 整片未覆盖 / G 结论
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox import blive, brealos, bstatic, task93 as T, waitstat67 as W
import fr_dis


def sec(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


m = blive.boot()
r = blive.run(m, steps=600000)
print("口径：blive.boot() → run(600000)  末 PC 0x%06X 换手 %d 当前任务 %s"
      % (m.cpu.pc, len(r["switches"]), r.get("current_name")))

# ------------------------------------------------------------------ A
sec("[A] 11 个休眠任务的现场")
print("A0 TCB 标志低字节 0x10 = DORMANT（waitstat67.DORMANT），保存 SP=0、入口一条没跑")
det = T.dormant_detail(m)
print("A1 共 %d 个" % len(det))
print("   idx  TCB         入口        TCB+0x08   保存SP      入口前缀")
for d in det:
    pfx, stop = T.entry_prefix(m, d["entry"])
    print("   %2d   0x%08X  0x%06X   0x%08X   0x%08X   %d 条%s"
          % (d["idx"], d["tcb"], d["entry"], d["flags"], d["saved_sp"], pfx,
             "" if pfx else "（一条没跑）"))
print("A2 %s" % T.verdict_dormant([d["idx"] for d in det]))

# ------------------------------------------------------------------ B
sec("[B] 11 个创建点：9 静态 + 2 表驱动")
unr = T.unreached(m)
print("B1 %s" % T.verdict_bootstrap(unr))
print("B2 任务号 → 创建点（类型）")
bmap = T.bootstrap_map()
for tid in sorted(bmap):
    tag = "休眠" if tid in T.DORMANT else "跑过"
    print("   任务 %2d [%s]  %s"
          % (tid, tag, "  ".join("0x%06X/%s" % (p, k) for p, k in bmap[tid])))
print("B3 V51 留的「2 个 r4 未知」就是 0x%06X / 0x%06X —— 表驱动，r4 来自 ROM 表"
      % (T.DYNAMIC_BOOTSTRAP[0][0], T.DYNAMIC_BOOTSTRAP[1][0]))
print("B4 点数 11、任务数 11，但不是一对一：9 个静态点覆盖 7 个任务，2 个表驱动点覆盖 4 个任务")

# ------------------------------------------------------------------ C
sec("[C] 表驱动机制：0x130B2C 按索引批量 sta_tsk")
print("C0 反汇编 0x%06X..0x130B94（无 enter 的叶子函数，KI#200 的归属坑）" % T.BATCH_FN)
for ln in (fr_dis.disasm_addr(m.bus.rom, T.BATCH_FN, 0x130B94, m.bus.rom_base) or []):
    print("   " + ln)
print()
print("C1 两张 tid 表实测：")
real = T.read_tables(m)
for pc, tab, tids in T.DYNAMIC_BOOTSTRAP:
    print("   0x%06X sta_tsk → 表 0x%06X = %s（覆盖任务 %s）"
          % (pc, tab, real[tab], list(tids)))
print("C2 表一致性：%s" % T.verdict_tables(*T.tables_match(m)))
print("C3 两个直接调用者：%s" % ["0x%06X" % p for p in T.BATCH_CALLERS])
print("C4 调用者所在函数入口（序言起点）：%s" % ["0x%06X" % p for p in T.BATCH_CALLER_FUNCS])
print("C5 静态扫描器为什么读不出 r4：`ld r0, r4` 从 `r13 + r6*4` 取表值，")
print("   不是立即数 —— bstatic 只解析立即数，所以 r4 恒 None（不是工具 bug，是形态超出）")

# ------------------------------------------------------------------ D
sec("[D] 5 张任务分组表")
print("D0 表地址      内容            状态")
for addr, want, note in T.TID_TABLES:
    print("   0x%06X  %-14s  %s" % (addr, str(list(want)), note))
print("D1 11 个休眠任务里落在分组表上的：%s"
      % sorted(set(t for _a, ts, _n in T.TID_TABLES for t in ts) & set(T.DORMANT)))
print("D2 只有静态硬编码点的休眠任务：%s"
      % sorted(set(T.DORMANT) - set(t for _a, ts, _n in T.TID_TABLES for t in ts)))

# ------------------------------------------------------------------ E
sec("[E] 补时基（tick=256）对这批任务的影响")
m2 = blive.boot(tick=256)
r2 = blive.run(m2, steps=600000)
print("E0 带时基机：末 PC 0x%06X 换手 %d" % (m2.cpu.pc, len(r2["switches"])))
d1 = T.dormant(m)
d2 = T.dormant(m2)
print("E1 缺省     休眠 %d 个：%s" % (len(d1), d1))
print("E2 tick=256 休眠 %d 个：%s" % (len(d2), d2))
print("E3 %s" % T.verdict_tick(d1, d2))
h1, h2 = T.hit_map(m), T.hit_map(m2)
print("E4 11 个创建点命中数：缺省 %d/11，tick=256 %d/11"
      % (sum(1 for v in h1.values() if v), sum(1 for v in h2.values() if v)))
print("E5 任务 7 的 TCB 现场（带时基）")
rec7 = brealos.records(m2)[6]
fl = m2.bus.rd(rec7["tcb"] + W.TCB_FLAGS, 4)
wh = m2.bus.rd(rec7["tcb"] + W.TCB_WHY, 4)
print("   tcb=0x%08X flags=0x%08X why=0x%08X（高半 %d = twai_flg）sp=0x%08X"
      % (rec7["tcb"], fl, wh, W.sx16(wh >> 16), m2.bus.rd(rec7["tcb"] + W.TCB_SAVED_SP, 4)))
print("E6 任务 7 体内三个创建点（建任务 8 两处、建任务 19 一处）：")
for pc in T.T7_BOOTSTRAP:
    print("   0x%06X  缺省 %-4s  tick=256 %-4s"
          % (pc, "命中" if T._hit(m, pc) else "未命中",
             "命中" if T._hit(m2, pc) else "未命中"))
print("E7 ⇒ 时基只让任务 7 从 tslp_tsk 出来，它转而等 flag；11 个仍全休眠")

# ------------------------------------------------------------------ F
sec("[F] 表驱动链那片代码的动态覆盖")
seg = T.coverage_span(m, T.GROUND_ZERO[0], T.GROUND_ZERO[1])
for base, n in seg:
    print("   0x%06X..0x%06X  %2d 条命中" % (base, base + 0x40, n))
print("F1 %s" % T.verdict_ground_zero(T.ground_zero_hit(m)))
print("F2 对照：任务 2 装配函数入口 0x1200AC 的前缀 = %d 条" % T.entry_prefix(m, 0x1200AC)[0])

# ------------------------------------------------------------------ G
sec("[G] 结论")
print("G1 11 个休眠任务 = 9 个静态点 + 2 个表驱动点；V51 的「2 个 r4 未知」= 12/13/14/15")
print("G2 表驱动：0x130B2C 按索引 clr_flg(6,~TABLE[idx]) + sta_tsk(TABLE[idx])，")
print("   表 0x1BD070={12,13}、0x1BD068={14,15}")
print("G3 5 张分组表：{6,7,9,10} / {4,8} / {1,2} / {14,15} / {12,13}")
print("G4 补时基不改变这 11 个（任务 7 改等 flag），V92 的时基收益到此为止")
print("G5 0x131200..0x131600 整片 0 命中 ⇒ 表驱动链从最上层没进来")
print()
print("不能报：任务 12–15 的业务语义（未查）、0x130B2C 的 r4/r5 实参含义（未回查源码）、")
print("        0x50000107 bit3 是真机源（KI#199 的反事实，本版未重做）、")
print("        任务链跑通了 / 显示链通了 / 系统跑起来了。")
print()
print("DONE V93 A–G 七段，本版实跑 2 台独立机器（1 缺省 + 1 tick=256），反事实标注清楚")
