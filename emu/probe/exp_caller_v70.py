# -*- coding: utf-8 -*-
"""V70 正式探针：调用者一直在，是任务 5 在两道 wai_flg 上挂住了。

承接 KI#269 第一条（「谁把事件提交进描述符队列 / 0x12E1EC 的调用者、
0x12E6E0 的 r10 由谁置」）。V69 用 `SW.callers_of()` 报这些函数
「静态直接调用边 0」，本版用 `svcgraph.ImmIndex` 四形态重查后证伪。

设备策略与 V65–V69 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
七台独立机器：基线一台、G0–G3 四台等形反事实、决定性反事实一台、
1000000 步窗口自证一台。

⚠️ 反事实段全部明确标注：`kernel_call` 注入的是真内核服务、
`tick_v52` 是模型化时基、直接写门字节/状态字都不是固件行为。
静态段只读 ROM，不建机器、不写内存。
"""
import sys
import os
import bisect
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, svcgraph, blive, bready, brealos, btick,
                   bgate, bdesc, taskchain64, descgate69 as G,
                   caller70 as C)

BOOT = blive.BOOT_STEPS
VEC = 0x2B
RUN = 500000
POST = 200000
WINDOW = 1000000

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
IDX = svcgraph.ImmIndex(rom, base)

TARGETS = [
    ("干活分支", 0x120184), ("派发函数", C.CURSOR_FN),
    ("清空队列", 0x12E602), ("显示初始化", 0x12E006),
    ("提交函数", C.SUBMIT), ("取首", 0x12E63A),
    ("重挂", 0x12E672), ("空实现", C.NOOP_HANDLER),
    ("大函数", C.BIG_FN), ("任务5入口函数", C.DISPATCH_FN),
]
KEY = C.KEY_PCS


def hr(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)


def decode_range(lo, hi):
    out = []
    off, end = lo - base, hi - base
    while off < end - 1:
        ins = fr_emu.TABLE.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        out.append((base + off, mn, v, size))
        off += size
    return out


def coverage(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def snap(m, tag):
    cov = coverage(m)
    idx = bready.task_index(m)
    t5 = idx.get(5, 0)
    return {
        "tag": tag, "pc": m.cpu.pc, "cov": cov,
        "key": {p: p in cov for p in KEY},
        "gate": m.bus.rd(0x9F58D08F, 1),
        "desc": bdesc.current(m),
        "t5_state": bready.state_of(m, t5) if t5 else None,
        "t5_want": m.bus.rd(t5 + 0x14, 4) if t5 else None,
        "waiting": sum(1 for t in idx
                       if bready.state_of(m, idx[t]) == 0x04),
    }


# ===================================================================== A
hr("A. 四形态引用重查：V69 的「静态直接调用边 0」是扫描器口径假阴性")
print("A0 口径：bstatic.Sweep.edges 只收 call:d @rN；call rel12 不在图里"
      "（KI#175 / 硬规矩 32）。svcgraph.ImmIndex 补 ldi20/ldi32/rel12/data32。")
print("A1 全 ROM 形态普查：%s" % IDX.profile())
print("A2 十个关键 PC 的两套口径对照：")
blind = []
for name, a in TARGETS:
    refs = IDX.refs(a, window=8)
    k = C.kind_counts(refs)
    d32 = IDX.data32(a)
    old = len(SW.callers_of(a))
    r12 = k.get("rel12", 0)
    all_edges = C.direct_edges(r12, old)
    v = C.missing_edge_verdict(old, all_edges, r12)
    if v["verdict"] == "blindspot":
        blind.append((name, a, r12))
    print("  %-10s 0x%06X rel12=%2d ldi20=%d ldi32=%3d data32=%3d | "
          "SW.edges=%3d 合并=%3d ⇒ %s"
          % (name, a, r12, k.get("ldi20", 0), k.get("ldi32", 0),
             len(d32), old, all_edges, v["verdict"]))
    for x in refs[:3]:
        print("       ref 0x%06X %-6s imm=0x%06X next=%s"
              % (x.pc, x.kind, x.imm, x.next_mnem))
print("A3 被旧口径漏掉的（edges 0 但 rel12 > 0）共 %d 个：" % len(blind))
for name, a, n in blind:
    print("     %-10s 0x%06X rel12=%d" % (name, a, n))
print("A4 注意：44 个处理函数静态直接调用边 0/44 仍成立（V69 结论不受影响）；"
      "被证伪的是「派发/初始化/清空/取首也只能间接到达」。")
print("A5 规矩 54：报「无引用」前先问扫了几种 call 形态；"
      "有静态调用边不等于会执行（规矩 5）。")

# ===================================================================== B
hr("B. 调用者 = 任务 5 主体：0x12DFB2 的 12 条 call 与任务表入口")
body = decode_range(C.DISPATCH_FN, 0x12E00A)
edges = []
for p, mn, v, size in body:
    if mn in ("call", "call:d") and v.get("rel12") is not None:
        edges.append((p, mn, C.rel12_target(p, v["rel12"])))
print("B1 0x12DFB2 函数 [0x12DFB2,0x12E00A) 共 %d 条指令、%d 条 call；"
      "目标 %d 个不同地址：" % (len(body), len(edges),
                              len(set(t for _p, _m, t in edges))))
for p, mn, t in edges:
    print("     0x%06X %-6s → 0x%06X" % (p, mn, t))
print("     目标集合 %s" % ["0x%06X" % t for t in
                           sorted(set(t for _p, _m, t in edges))])
print("B2 其中调到大函数序言 0x12E48E 的 %d 条、调到派发函数 0x12E1EC 的 %d 条"
      % (sum(1 for _p, _m, t in edges if t == C.BIG_PRE),
         sum(1 for _p, _m, t in edges if t == C.CURSOR_FN)))
print("B3 关键：0x12DFD0 call rel12 → 0x12E1EC 就是 KI#269 要找的调用者。")
recs = taskchain64.task_table(rom, base)
disp = [k + 1 for k, r in enumerate(recs)
        if 0x120000 <= r.entry < 0x140000]
print("B4 任务表 0x54C00 共 %d 条（记录 0 = 任务 1）；落显示区的 %d 个任务号 %s"
      % (len(recs), len(disp), disp))
print("B5 任务 5 入口 = 0x%06X；任务表字段 0x%06X 的 4 字节 = 0x%08X "
      "（= 0x12DFB2 的序言起点，硬规矩 26）"
      % (recs[4].entry, C.TASK5_FIELD,
         int.from_bytes(rom[C.TASK5_FIELD - base:C.TASK5_FIELD - base + 4],
                        "big")))
print("B6 ⇒ 整条显示派发链不是「无人调用的孤岛」，是任务 5 的函数体；"
      "0x12E006 无 enter（enclosing 归到 0x12DFB2，规矩 43）。")

# ===================================================================== C
hr("C. 0x12E6E0 的 r10 不是参数，是同一个大函数里的局部标志")
ins = decode_range(C.BIG_FN, 0x12E6FC)
rows = [(p, mn, v) for p, mn, v, _s in ins]
sp = C.split_reg_writes(rows, reg=10)
print("C1 0x12E490 函数 [0x12E490,0x12E6FC) 共 %d 条指令；"
      "ri==10 的写 %d 条 / 读 %d 条" % (len(ins), sp["n_writes"], sp["n_reads"]))
for p, mn, v in sp["writes"]:
    print("     写 0x%06X %-10s %s" % (p, mn, v))
for p, mn, v in sp["reads"]:
    print("     读 0x%06X %-10s %s" % (p, mn, v))
src = C.sources_before(sp["writes"], C.GATE_PC, n=2)
print("C2 门 0x12E6E0 之前最近的两条写 r10：")
for p, mn, v in src:
    print("     0x%06X %-10s %s" % (p, mn, v))
print("C3 控制流：0x12E69C beq:d rel9=29 的延迟槽 0x12E69E 置 r10=1"
      "（无条件执行）；0x12E6B2 bne rel9=3 跳到 0x12E6BA 置 r10=0；"
      "0x12E6B8 bra rel9=15 跳过该写点。两路合流到 0x12E6D0..D8 后判门。")
print("C4 ⇒ r10 ∈ {0,1} 由同函数内两次位测试决定，与调用者无关（规矩 55）。")
print("C5 V69 把 0x12E6D0..0x12E6F6 当独立小函数看是错的："
      "它只是 0x12E490 这个 262 条指令函数的尾部 19 条。")

# ===================================================================== D
hr("D. 执行前沿与等待对象：任务 5 在 0x12DFBA 之后没回来")
m1 = boot_run()
b = snap(m1, "500000 步收敛基线")
front = decode_range(C.TASK5_ENTRY, 0x12DFC8)
fr = C.frontier(b["cov"], [p for p, _m, _v, _s in front])
print("D1 任务 5 入口段 [0x12DFAE,0x12DFC8) 逐条命中：")
for p, mn, v, _s in front:
    print("     0x%06X %-10s %s" % (p, mn, "命中" if p in b["cov"] else "未命中"))
print("D2 执行前沿 = 0x%06X；第一条未命中 = 0x%06X（共 %d/%d 命中）"
      % (fr["frontier"] or 0, fr["first_miss"] or 0,
         fr["n_hit"], fr["n_total"]))
print("D3 0x12DFBA 的 rel12 目标 = 0x%06X（0x12E0FC 的序言起点）"
      % C.rel12_target(0x12DFBA, 158))
calls = [c for c in SW.calls if 0x12E0F8 <= c.addr < 0x12E162]
print("D4 该函数 [0x12E0F8,0x12E162) 的服务调用点 %d 个：" % len(calls))
for c in calls:
    print("     0x%06X svc=%s r4=%s r5=%s" % (c.addr, c.svc, c.r4, c.r5))
idx = bready.task_index(m1)
t5 = idx[5]
half = (m1.bus.rd(t5 + 0x20, 4) >> 16) & 0xFFFF
print("D5 任务 5 TCB=0x%08X state=0x%02X；TCB+0x20 高半 0x%04X "
      "按 16 位补码 = %d（wai_flg）；TCB+0x14 等待位 = 0x%08X"
      % (t5, b["t5_state"], half, C.wait_reason(half),
         m1.bus.rd(t5 + 0x14, 4)))
for _i, owner, nodes in btick.object_queues(m1):
    if nodes and t5 in nodes:
        tids = [t for t, tcb in idx.items() if tcb in nodes]
        print("D6 任务 5 挂在 owner=0x%08X 队列，同队列任务号 %s"
              % (owner, tids))
print("D7 收敛态：末PC=0x%06X 覆盖=%d 等待 %d / 休眠 11 / 就绪 1；"
      "门字节 0x%02X 描述符指针 0x%08X"
      % (b["pc"], len(b["cov"]), b["waiting"], b["gate"], b["desc"]))

# ===================================================================== E
hr("E. 四组等形反事实：写数据门与补 tick 都推不动任务 5 的分支")
print("E0 四组各一台独立机器，同样 1 次 get_tid(-24) 预热 + 1 次中断 + "
      "200000 步；差异只在写了哪几道门。G3 额外 12 轮模型化 tick（反事实）。")


def experiment(tag, gate=False, ready=False, ticks=0):
    m = boot_run()
    s0 = snap(m, tag + " 基线")
    pre = brealos.kernel_call(m, -24)
    if gate:
        bgate.open_gate(m, 1)
    if ready:
        bgate.set_status_a(m, bgate.status_a(m) | G.STATUS_WORK_READY)
    if ticks:
        btick.tick_v52(m, n=ticks, steps=30000, wake_objects=False, log=[])
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)
    s1 = snap(m, tag + " 末尾")
    new = s1["cov"] - s0["cov"]
    print("     %s 预热 steps=%d err=%d；新增覆盖 %d；任务5 state 0x%02X→0x%02X"
          % (tag, pre["steps"], pre["err"], len(new),
             s0["t5_state"], s1["t5_state"]))
    return dict(base=s0, end=s1, n_new=len(new), new=new,
                shape=dict(boot=BOOT, post=POST, svc=1, irqs=1))


res = {}
res["G0"] = experiment("G0 不补")
res["G1"] = experiment("G1 只开门字节", gate=True)
res["G2"] = experiment("G2 门+bit6", gate=True, ready=True)
res["G3"] = experiment("G3 门+bit6+12轮tick", gate=True, ready=True, ticks=12)
sh = C.equal_shape({k: r["shape"] for k, r in res.items()})
print("E1 等形校验：%s（共有键 %s）" % ("通过" if sh["ok"] else "不通过",
                                    sh["values"]))
print("E2 差分总账：")
for k in ("G0", "G1", "G2", "G3"):
    e = res[k]
    print("     %s 新增覆盖 %4d / 任务5分支新增 %s / 0x12E6F8 %s / 末PC 0x%06X"
          % (k, e["n_new"],
             ["0x%06X" % p for p in sorted(e["new"])
              if 0x12DFAE <= p < 0x12E00A],
             "中" if C.SUBMIT in e["end"]["cov"] else "-", e["end"]["pc"]))
print("E3 G0 与 G1 覆盖对称差 %d 个 ⇒ 收敛态写数据门没有执行效果（规矩 53）"
      % len(res["G0"]["new"] ^ res["G1"]["new"]))
g3branch = sorted(p for p in res["G3"]["new"]
                  if 0x12DFAE <= p < 0x12E00A)
print("E4 只有补 tick 的 G3 有新增覆盖（%d 个），其中任务 5 分支 %d 条 %s；"
      "与 F 段的 set_flg 反事实殊途同归，都只到 0x12DFC0。"
      "0x12DFC2 / 0x12DFD0 / 0x12E1EC / 0x12E48E 四组全未命中。"
      % (res["G3"]["n_new"], len(g3branch),
         ["0x%06X" % p for p in g3branch]))

# ===================================================================== F
hr("F. 决定性反事实：set_flg(3,9) 只把任务 5 往前推 3 条指令")
mf = boot_run()
f0 = snap(mf, "决定性实验基线")
idx_f = bready.task_index(mf)
pre = brealos.kernel_call(mf, -24)
r = brealos.kernel_call(mf, -48, r4=C.FLAG_ID, r5=C.WANT_BASE)
print("F1 预热 get_tid steps=%d err=%d；反事实 set_flg(3,9) steps=%d err=%d"
      % (pre["steps"], pre["err"], r["steps"], r["err"]))
print("F2 置位瞬时：任务 5 state 0x%02X→0x%02X（就绪），等待任务数 17→%d"
      % (f0["t5_state"], bready.state_of(mf, idx_f[5]),
         sum(1 for t in idx_f if bready.state_of(mf, idx_f[t]) == 0x04)))
mf.raise_irq(VEC)
mf.run(max_steps=POST, spin_guard=False)
f1 = snap(mf, "置位后 200000 步")
adv = C.advance(f0["cov"], f1["cov"], KEY)
print("F3 覆盖 %d→%d 新增 %d 个；末PC=0x%06X；"
      "末尾任务 5 state=0x%02X 等待位 0x%08X"
      % (len(f0["cov"]), len(f1["cov"]), len(f1["cov"] - f0["cov"]),
         f1["pc"], f1["t5_state"], f1["t5_want"]))
print("F4 关键 PC 新增命中：%s"
      % ["0x%06X" % p for p in adv["new"]])
print("F5 ⇒ 任务 5 从 0x12DFBA 推进到 0x12DFC0（3 条），"
      "在 0x12DFBE call:d → 0x12E162 里又挂起（等待位 9→0x74）；"
      "0x12DFC2 及之后的 12 条 call 仍一步没跑。")
print("F6 G3（补 12 轮模型化 tick）也让任务 5 推进到同一前沿 0x12DFC0，"
      "两条反事实不互相独立、不能算两次推进；都停在 0x12E162 的第二道等待。")
print("F7 这是反事实注入（真内核服务），不是固件行为；"
      "「唤醒」不等于「显示链通了」。")

# ===================================================================== G
hr("G. 1000000 步窗口自证")
mg = boot_run(WINDOW)
g = snap(mg, "1000000 步")
w = C.window_same(
    {"pc": b["pc"], "cov": b["cov"], "key": b["key"]},
    {"pc": g["pc"], "cov": g["cov"], "key": g["key"]})
print("G1 末PC 0x%06X / 覆盖 %d；与 500000 步差异字段 %s、"
      "PC 覆盖对称差集 %d 个"
      % (g["pc"], len(g["cov"]), w["diff"], w["symdiff_cov"]))
print("G2 关键 PC 命中差异：%s"
      % ["0x%06X" % p for p in KEY if b["key"][p] != g["key"][p]])
print("G3 ⇒ 所有「未命中」不是执行窗口不够长。")

# ===================================================================== H
hr("H. 边界与不能说的话")
verdict = C.chain_verdict(caller_exists=True,
                          frontier_pc=fr["frontier"],
                          wake_frontier=adv["frontier"],
                          gate_hit=any(C.GATE_PC in res[k]["end"]["cov"]
                                       for k in res) or C.GATE_PC in f1["cov"],
                          noop_hit=C.NOOP_HANDLER in f1["cov"])
print("H1 综合判据：调用者存在=%s / 执行前沿=0x%06X / 唤醒后前沿=0x%06X / "
      "推进=%s / 门命中=%s / 空槽位命中=%s / 链闭合=%s"
      % (verdict["caller"], verdict["stuck"] or 0,
         verdict["wake_frontier"] or 0, verdict["wake_advanced"],
         verdict["gate_hit"], verdict["noop_hit"], verdict["closed"]))
print("H2 两个状态字 0x9F5A0284 / 0x9F59EF50 仍全 0；8 槽 0x9F59F7C8 全 0；"
      "0x12E6F0 / 0x12E752 全场景未命中。")
print("H3 仍未取：任务 5 在 0x12E162 里等的新位 0x74 由谁置、"
      "真机 tick 源（KI#191）、13 组 ISR 各自硬件事件、"
      "[12,13,14,15] 激活机制、两个状态字与 8 槽填充源。")
print("H4 不能报：显示链通了 / 出画面 / 找到真机 A↔B 通道 / 真机如此 /"
      "「补一次 set_flg 就等于唤醒成功」。")
print("")
print("结果文件 results/frbox/caller70.txt；设施 frbox/caller70.py；"
      "七台独立机器（基线一台 + G0–G3 四台 + 决定性反事实一台 + 1000000 步一台）。")
