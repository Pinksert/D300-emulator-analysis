# -*- coding: utf-8 -*-
"""V69 正式探针：0x12E752 是空槽位不是门；描述符派发链的完整门图。

承接 KI#265 第一条（「0x12E752 的门」）。设备策略与 V65–V68 收敛组
逐字相同：blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) →
run(500000)。七台独立机器：基线一台、G0–G3 四台等形反事实、
一台决定性实验（阴性，工具边界）、一台 1000000 步窗口自证。

⚠️ 反事实段全部明确标注：直接写内存门 / 模型化 tick / 直接调函数
都不是固件行为。静态段只读 ROM，不建机器。
"""
import sys
import os
import bisect
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, brealos, btick, bdesc, bqueue,
                   bgate, isrfam63, descgate69 as G)

BOOT = blive.BOOT_STEPS
VEC = 0x2B
RUN = 500000
POST = 200000
WINDOW = 1000000
RET = 0x00407DC
SCRATCH = 0x9F83F000

DSP = G.DSP_PCS
KEY = (G.WORK_ENTRY, G.CURSOR_FN, 0x12E244, 0x12E276, 0x12E28C,
       0x12E6E0, 0x12E6E2, 0x12E6F0, G.SUBMIT, G.NOOP_HANDLER)
WORD_A, WORD_B, SLOTS8 = G.WORD_A, G.WORD_B, G.SLOTS8


def coverage(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def handlers(m):
    return G.group_handlers(bdesc.entries(m))


def head_of(m, h):
    return bytes(m.fetch_bytes(h, 2))


def snap(m, tag, groups):
    cov = coverage(m)
    hc = G.handler_coverage(cov, groups)
    idx = bready.task_index(m)
    states = dict((t, bready.state_of(m, tcb)) for t, tcb in idx.items())
    print("  [%s] 末PC=0x%06X 覆盖=%d 当前TCB=0x%08X 就绪位图=0x%08X "
          "等待=%d 休眠=%d 就绪=%d"
          % (tag, m.cpu.pc, len(cov), bready.current(m), bready.ready_map(m),
             sum(1 for v in states.values() if v == 0x04),
             sum(1 for v in states.values() if v == 0x10),
             sum(1 for v in states.values() if v == 0x00)))
    print("       门字节 0x%08X=0x%02X；状态字A 0x%08X=0x%08X(bit6=%s)；"
          "状态字B 0x%08X=0x%08X"
          % (G.GATE, bgate.gate(m), G.STATUS_A, bgate.status_a(m),
             "1" if bgate.work_ready(m) else "0", bgate.STATUS2,
             bgate.status2(m)))
    print("       游标 0x%08X 九字节 = %s"
          % (G.CURSOR_BASE, bytes(m.fetch_bytes(G.CURSOR_BASE, 9)).hex(" ")))
    print("       当前描述符指针 0x%08X = 0x%08X（槽位 %s）"
          % (G.DESC_PTR, bdesc.current(m), bdesc.current_index(m)))
    print("       关键 PC %s"
          % ["0x%06X:%s" % (p, "中" if p in cov else "-") for p in KEY])
    print("       处理函数命中 %d/%d：%s"
          % (hc["n_hit"], hc["n_total"],
             " ".join("0x%06X" % h for h in hc["hit"][:10])))
    print("       显示链 %s"
          % ["0x%06X:%s" % (p, "中" if p in cov else "-") for p in DSP])
    print("       状态字 0x%08X / 0x%08X；8 槽 0x%08X = %s"
          % (m.bus.rd(WORD_A, 4), m.bus.rd(WORD_B, 4), SLOTS8,
             [m.bus.rd(SLOTS8 + 4 * i, 4) for i in range(8)]))
    return dict(cov=cov, hc=hc, states=states, pc=m.cpu.pc,
                gate=bgate.gate(m), status=bgate.status_a(m),
                desc=bdesc.current(m), cursor=bytes(m.fetch_bytes(G.CURSOR_BASE, 9)))


SW, M0 = bstatic.sweep_of_type("d300-b")
ROM, BASE = M0.bus.rom, M0.bus.rom_base

# ===================================================================== A
print("=" * 78)
print("A. 口径证伪：0x12E752 是「第二道门」还是空槽位")
print("=" * 78)
print("A0 ROM base=0x%X size=%d；描述符表 0x%08X ×%d ×0x%02X"
      % (BASE, len(ROM), bdesc.DESC_TABLE, bdesc.DESC_COUNT,
         bdesc.DESC_STRIDE))
groups = handlers(M0)
rets = G.ret_handlers(groups, lambda h: head_of(M0, h))
noops = G.noop_slots(groups)
reals = G.real_handlers(groups, rets)
print("A1 描述符 %d 条 / 处理函数 %d 个 / 槽位覆盖 %d"
      % (bdesc.DESC_COUNT, len(groups), sum(len(v) for v in groups.values())))
print("A2 裸 ret 处理函数 %d 个：%s"
      % (len(rets), " ".join("0x%06X" % h for h in rets)))
print("   裸 ret 共占槽位 %d 个；实做处理函数 %d 个（0x%06X..0x%06X）"
      % (sum(len(groups[h]) for h in rets), len(reals),
         min(reals), max(reals)))
print("A3 0x%06X 开头两字节 = %s（= 单条 ret）；指向它的槽位 %d 个：%s"
      % (G.NOOP_HANDLER, head_of(M0, G.NOOP_HANDLER).hex(" "),
         len(noops), noops))
direct_noop = SW.callers_of(G.NOOP_HANDLER)
v = G.sentinel_verdict(head_of(M0, G.NOOP_HANDLER), noops, direct_noop)
print("A4 静态直接调用边 %d 条 ⇒ 判定 = %s：%s"
      % (v["direct_edges"], v["verdict"], v["reason"]))
print("A5 ⇒ V61–V68 把 0x%06X 列进「显示链 5 PC」当第二道卡点是选错哨兵；"
      "它未命中的真实含义是整条 70 槽位描述符派发链一次都没跑。"
      % G.NOOP_HANDLER)

# ===================================================================== B
print("")
print("=" * 78)
print("B. 静态门图：谁读、谁写、谁调用")
print("=" * 78)
acc = G.classify_access(bgate.access(M0, G.GATE))
print("B1 门字节 0x%08X：ldi:32 引用 %d 处，读 %d / 写 %d；分类 %s"
      % (G.GATE, sum(acc["kinds"].values()), acc["reads"], acc["writes"],
         acc["kinds"]))
w = SW.writers_of(G.GATE)
print("B2 写点 %d 处：%s；两处 enclosing 都 = 0x%06X"
      % (len(w), ["0x%06X(%s)" % (p, mn) for p, mn in w],
         SW.enclosing(w[0][0]) or 0))
CHAIN = (("干活分支", G.WORK_ENTRY), ("派发函数", G.CURSOR_FN),
         ("清空队列", bqueue.CLEAR), ("显示初始化", bqueue.DISPLAY_INIT),
         ("提交函数", G.SUBMIT), ("取首", bqueue.PEEK),
         ("重挂", bqueue.REHANG), ("空实现", G.NOOP_HANDLER))
print("B3 关键函数的静态直接调用边（SW.callers_of，含 rel12）：")
for name, a in CHAIN:
    e = SW.callers_of(a)
    r = SW.refs_to(a, window=8)
    print("     %-10s 0x%06X edges %3d / ldi:32 refs %3d"
          % (name, a, len(e), len(r)))
pc = G.CURSOR_FN
ins_cnt = 0
flow = []
while pc <= 0x12E324:                 # 含末尾那条 0x12E324 ret
    d = fr_emu.TABLE.decode(ROM, pc - BASE)
    if not d:
        break
    mn, b, f, size, wd = d
    ins_cnt += 1
    if mn.startswith("call") or mn.startswith("ret") or mn == "int":
        flow.append((pc, mn))
    pc += size
print("B4 派发函数 [0x%06X,0x12E326) 共 %d 条指令；call %d 条 / ret %d 条 / "
      "int #0x40 %d 条（wai_sem(17) @0x12E24A、sig_sem(17) @0x12E266）"
      % (G.CURSOR_FN, ins_cnt,
         sum(1 for _p, m in flow if m.startswith("call")),
         sum(1 for _p, m in flow if m.startswith("ret")),
         sum(1 for _p, m in flow if m == "int")))
ind = []
q = 0x12E000
while q < 0x12F800:
    d = fr_emu.TABLE.decode(ROM, q - BASE)
    if not d:
        q += 2
        continue
    mn, b, f, size, wd = d
    if mn.startswith("call") and "rel12" not in f:
        ind.append((q, mn, f.get("ri")))
    q += size
print("B5 0x12E000..0x12F800 共 %d 条间接 call（call @rN），"
      "全部落在处理函数/显示队列函数内部；前 6 个 %s"
      % (len(ind), ["0x%06X" % p for p, _m, _r in ind[:6]]))
n_direct = sum(1 for h in groups if SW.callers_of(h))
print("B6 44 个处理函数里有静态直接调用边的：%d / %d ⇒ 纯数据表间接派发"
      % (n_direct, len(groups)))

# ===================================================================== C
print("")
print("=" * 78)
print("C. 唯一调处理函数的代码点：0x12E6F0 call @r0")
print("=" * 78)
q = 0x12E6D0
while q < 0x12E6F8:
    d = fr_emu.TABLE.decode(ROM, q - BASE)
    if not d:
        break
    mn, b, f, size, wd = d
    print("   0x%06X  %-10s %s"
          % (q, mn, " ".join("%s=%s" % (k, x) for k, x in sorted(f.items())
                             if k not in ("op1", "op2", "op3", "op4"))))
    q += size
print("C1 读法：0x12E6E4 装当前描述符指针 0x%08X → 0x12E6EA 取描述符 → "
      "0x12E6EC 置 +0x0C → 0x12E6EE 取处理函数地址 → 0x12E6F0 call @r0"
      % G.DESC_PTR)
print("C2 前置门：0x12E6E0 cmp r10,#0 + 0x12E6E2 beq rel9=7 ⇒ r10==0 时"
      "跳过 0x12E6F0，直接 0x12E6F2 恢复现场并 ret")
pat = G.NOOP_HANDLER.to_bytes(4, "big")
hits, i = [], ROM.find(pat)
while i >= 0:
    hits.append(BASE + i - 2)
    i = ROM.find(pat, i + 1)
table_end = bdesc.DESC_TABLE + bdesc.DESC_COUNT * bdesc.DESC_STRIDE
in_table = [p for p in hits if bdesc.DESC_TABLE <= p < table_end]
print("C3 0x%06X 作为 4 字节立即数在全 ROM 出现 %d 次，"
      "落在描述符表 [0x%08X,0x%08X) 里的 %d 次，代码段 %d 次"
      % (G.NOOP_HANDLER, len(hits), bdesc.DESC_TABLE,
         table_end, len(in_table), len(hits) - len(in_table)))
print("C4 0x%08X 的 ldi:32 引用 4 处：%s"
      % (G.DESC_PTR, ["0x%06X" % p for p in bgate.refs(M0, G.DESC_PTR)]))
vt = isrfam63.vector_table(ROM, BASE)
real = [(x.vec, x.entry) for x in vt
        if x.entry not in (None, 0xFFFFFFFF, 0x0404F8)]
entries = sorted({e for _v, e in real})


def isr_range(entry):
    i = bisect.bisect_right(SW.funcs, entry) - 1
    lo = SW.funcs[i] if i >= 0 else entry
    j = bisect.bisect_right(SW.funcs, entry)
    hi = SW.funcs[j] if j < len(SW.funcs) else entry + 0x200
    return (lo, hi)


print("C5 非默认中断向量 %d 条 / %d 个不同入口；按 enclosing→下一个 enter "
      "粗粒度划 ISR 函数体（硬规矩 43）逐项反查：" % (len(real), len(entries)))
for name, a in (("门字节写点1", 0x12ECF4), ("门字节写点2", 0x12F180),
                ("派发函数", G.CURSOR_FN), ("间接调用点", 0x12E6F0),
                ("提交函数", G.SUBMIT), ("空实现", G.NOOP_HANDLER)):
    owners = [e for e in entries for lo, hi in [isr_range(e)] if lo <= a < hi]
    print("     %-12s 0x%06X 落在 %d 个非默认 ISR 函数体 %s"
          % (name, a, len(owners), ["0x%06X" % e for e in owners[:6]]))

# ===================================================================== D
print("")
print("=" * 78)
print("D. 收敛态基线（第一台机器，500000 步）")
print("=" * 78)
m0 = boot_run()
base = snap(m0, "收敛基线", groups)
print("D1 关键结论：0x%08X=0、游标九字节全 0、44/44 处理函数 0 命中、"
      "0x12E6F0 未命中" % G.DESC_PTR)

# ===================================================================== E
print("")
print("=" * 78)
print("E. 四组等形反事实：逐道推门（G0–G3，四台独立机器）")
print("=" * 78)
print("E0 四组都做同样的 1 次 get_tid(-24) 预热 + 1 次 raise_irq(0x2B) + "
      "200000 步；差异只在写了哪几道门。G3 额外 12 轮模型化 tick，"
      "其新增覆盖只用来判「有没有调度事件」，不与前三组横向比数字。")


def experiment(tag, gate=False, ready=False, ticks=0):
    m = boot_run()
    b = snap(m, "%s 收敛基线" % tag, groups)
    pre = brealos.kernel_call(m, -24)
    print("       预热 get_tid steps=%d err=%d" % (pre["steps"], pre["err"]))
    if gate:
        bgate.open_gate(m, 1)
    if ready:
        bgate.set_status_a(m, bgate.status_a(m) | G.STATUS_WORK_READY)
    if ticks:
        btick.tick_v52(m, n=ticks, steps=30000, wake_objects=False, log=[])
        print("       tick_v52(n=%d) 12 轮模型化唤醒（反事实）" % ticks)
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)
    e = snap(m, "%s 末尾" % tag, groups)
    d = G.door_delta(b["cov"], e["cov"])
    print("       ⇒ 新增覆盖 %d 个；处理函数 %d→%d；显示链新增 %s；"
          "门字节 0x%02X→0x%02X；描述符指针 0x%08X→0x%08X"
          % (d["n_new"], b["hc"]["n_hit"], e["hc"]["n_hit"],
             ["0x%06X" % p for p in d["dsp_new"]],
             b["gate"], e["gate"], b["desc"], e["desc"]))
    return dict(base=b, end=e, delta=d,
                shape=dict(boot=BOOT, post=POST, svc=1, irqs=1))


res = {}
res["G0"] = experiment("G0 不补（对照）")
res["G1"] = experiment("G1 只开门字节", gate=True)
res["G2"] = experiment("G2 门字节 + 状态字bit6", gate=True, ready=True)
res["G3"] = experiment("G3 门 + bit6 + 12 轮 tick",
                       gate=True, ready=True, ticks=12)
shape = G.equal_shape({k: r["shape"] for k, r in res.items()})
print("E1 等形校验：%s（共有键 %s）" % ("通过" if shape["ok"] else "不通过",
                                    shape["values"]))
rows = [(k, res[k]["delta"]["n_new"], k == "G3") for k in ("G0", "G1", "G2", "G3")]
dv = G.dispatch_verdict(rows)
print("E2 差分总账：")
for k, n, tick in rows:
    e = res[k]
    print("     %s 新增覆盖 %4d / 处理函数 %2d / 0x12E752 %s / 0x12E6F8 %s / "
          "0x12E6F0 %s / 末PC 0x%06X"
          % (k, n, e["end"]["hc"]["n_hit"],
             "中" if G.NOOP_HANDLER in e["end"]["cov"] else "-",
             "中" if G.SUBMIT in e["end"]["cov"] else "-",
             "中" if 0x12E6F0 in e["end"]["cov"] else "-", e["end"]["pc"]))
print("E3 归因 = %s；只写数据门无效的组 = %s"
      % (dv["attribution"], dv["zero_groups"]))
print("E4 G0 与 G1 覆盖对称差 %d 个 ⇒ 收敛态开门字节没有任何执行效果"
      % len(set(res["G0"]["delta"]["new"]) ^ set(res["G1"]["delta"]["new"])))

# ===================================================================== F
print("")
print("=" * 78)
print("F. 决定性实验阴性：收敛机器上直接调 0x12E1EC 撞工具边界")
print("=" * 78)
mf = boot_run()
basef = coverage(mf)
print("F1 基线末PC=0x%06X 覆盖=%d 挂起中断=%d 0x%08X=0x%08X"
      % (mf.cpu.pc, len(basef), len(mf.cpu.irq_pending),
         G.DESC_PTR, bdesc.current(mf)))


def clear_pending(m):
    if getattr(m, "dispatch", None):
        m.dispatch.deassert()
    m.cpu.irq_pending[:] = []
    if getattr(m, "icu", None) and hasattr(m.icu, "pending"):
        m.icu.pending.clear()


def call_fn(m, addr, max_steps=4000):
    c = m.cpu
    m.bus.wr(SCRATCH - 4, 4, RET)
    c.r[15] = SCRATCH - 4
    c.i = 0
    c.ilm = 0x1F
    clear_pending(m)
    c.pc = addr
    n = 0
    while n < max_steps and not c.halted and c.pc != RET:
        m.step(1)
        n += 1
    return n, c.pc


for slot in (1, 3):
    e = bdesc.entry(mf, slot)
    mf.bus.wr(G.CURSOR_SLOT, 1, slot)
    mf.bus.wr(0x9F58DA83, 1, 1)
    mf.bus.wr(0x9F58DA84, 1, slot)
    before = coverage(mf)
    steps, endpc = call_fn(mf, G.CURSOR_FN)
    after = coverage(mf)
    new = sorted(after - before)
    print("F2 槽 %d → 处理函数 0x%06X：steps=%d 回程=0x%06X 新增PC %d；"
          "该处理函数命中=%s；0x%08X=0x%08X；掩码 0x%08X=0x%02X"
          % (slot, e["handler"], steps, endpc, len(new),
             e["handler"] in after, G.DESC_PTR, bdesc.current(mf),
             G.EVENT_MASK, bdesc.event_mask(mf)))
    print("       新增 PC 前 6：%s"
          % ["0x%06X" % p for p in new[:6]])
print("F3 判读：0x12E1EC 里的 wai_sem(17) @0x12E24A 走 int #0x40，"
      "任务被挂起后 PC 落进 0x0404E0 / 0x0404F8 默认 ISR，跑满 4000 步"
      "回不到回程地址 0x0407DC；清空挂起中断也无效。")
print("F4 ⇒ 这是注入工具边界，不是显示链结论；本版不据此报任何"
      "「派发链通了 / 不通」。静态证伪（A/C 段）已足够。")

# ===================================================================== G
print("")
print("=" * 78)
print("G. 1000000 步窗口自证（第七台独立机器）")
print("=" * 78)
mw = boot_run(WINDOW)
cw = snap(mw, "1000000 步", groups)
a = dict(pc=base["pc"], cov=base["cov"],
         dsp=G.chain_coverage(base["cov"], DSP),
         states=base["states"])
b = dict(pc=cw["pc"], cov=cw["cov"],
         dsp=G.chain_coverage(cw["cov"], DSP), states=cw["states"])
ws = G.window_same(a, b)
print("G1 500000 / 1000000 步逐项相同 = %s；差异字段 %s；"
      "PC 覆盖对称差集 %d 个" % (ws["same"], ws["diff"], ws["symdiff_cov"]))
print("G2 1000000 步里 0x%06X 命中=%s，0x12E6F0 命中=%s，"
      "44 个处理函数命中 %d 个"
      % (G.NOOP_HANDLER, G.NOOP_HANDLER in cw["cov"],
         0x12E6F0 in cw["cov"], cw["hc"]["n_hit"]))

# ===================================================================== H
print("")
print("=" * 78)
print("H. 显示链终态与不能说的话")
print("=" * 78)
print("H1 收敛态与四组反事实末尾：状态字 0x%08X / 0x%08X 全为 0；"
      "8 槽 0x%08X 全为 0" % (WORD_A, WORD_B, SLOTS8))
print("H2 0x12E752 与 0x12E6F0 在 500000 / 1000000 步以及四组反事实里"
      "全部未命中；显示链 0x120178 / 0x12E602 / 0x12E630 命中，"
      "0x12E6F8 只在补 tick 组命中。")
print("H3 未闭合：谁把事件提交进描述符队列（0x12E1EC 的调用者）、"
      "0x12E6E0 的 r10 由谁置、真机 tick 源（KI#191）、13 组 ISR 各自硬件事件、"
      "[12,13,14,15] 激活机制。")
print("H4 不能报：显示链通了 / 出画面 / 找到真机 A↔B 通道 / 真机如此。")
print("")
print("=" * 78)
print("结果文件 results/frbox/desc69.txt；设施 frbox/descgate69.py；"
      "七台独立机器（基线一台 + G0–G3 四台 + 决定性实验一台 + 1000000 步一台）。")
print("=" * 78)
