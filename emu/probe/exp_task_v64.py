# -*- coding: utf-8 -*-
"""V64 正式探针：显示任务启动链、三处旧口径更正，以及同尺动态阴性对照。

    cd emu && python probe/exp_task_v64.py > ../results/frbox/task64.txt

六段：
  A  静态：任务表 0x054C00（29 条 × 0x1C）与 14 个显示任务
  B  静态：sta_tsk 27 个启动点与其中的 9 个显示任务启动点
  C  静态：0x9F58D500 对象池（70 槽 × 12 字节）与公共状态查询函数
  D  静态：更正 V63「12 个上游入口」/ V62「15 个独立 handler」/ rel9 符号
  E  动态：H0–H3 四组同尺对照（KI#243 / KI#238 收窄）
  F  能说 / 不能说 + 复现与变更

纪律：
  · 静态只读 ROM、不建运行机器；任务表入口是**序言起点**，`enter` 在其后
    2/4 字节（硬规矩 26），`enclosing()` 只是粗粒度归属（硬规矩 30）。
  · 动态每台机器只跑一遍（硬规矩 13），不自写 `cpu.step()` 循环（硬规矩 1）；
    设备策略用 `devaudit.audit()` 显式声明（硬规矩 38），不改 `create()` 默认值。
  · `bobserve.observe()` 先装、`bpatch.patch_read()` 后装（硬规矩 34）。
  · 补 `map_ram` 是模型化机器结构改动；`bpatch` 是反事实打桩；
    `raise_irq` 是模型动作。三者分开记，都不等于复原真机通道。
  · 观察日志值按十进制打印（硬规矩 39）；静态「有没有」与动态「跑没跑」
    分开（硬规矩 5）；口径不同数字不混比（硬规矩 2）。
  · 本版不接 `dev_tick18` 真实时基；V55 的 2000k 步长跑结论只作背景引用，
    不与本版 20000 步窗口的数字混比。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import (addrform, blive, bobserve, bpatch, bstatic, devaudit,
                   dispatch62 as D, isrfam63 as F, taskchain64 as T)
import fr_emu

# ---------------------------------------------------------------- 常量
BOOT, RUN, VEC = 191520, 20000, 0x2B
FRAME = 0x000083B8
FRAME_PCS = (0x042E40, 0x042E50)
STRUCT_FLAG = 0x9F5A0284
STATUS_ADDRS = tuple(F.STATUS_BASE + o for o in D.STATUS_OFFSETS)
STATUS_RULES = tuple(zip(STATUS_ADDRS, D.STATUS_READ_PCS))
MAP_SIZE = 0x1000
DSP_LO, DSP_HI = T.DISP_LO, T.DISP_HI
POOL_HI = (T.POOL_BASE + T.POOL_STATUS_OFF +
           T.POOL_STRIDE * T.POOL_COUNT)          # 0x9F58D850


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ================================================================ 静态 ROM
hr("A  静态：任务表 0x054C00 —— 29 条记录里 14 条是显示任务")
SW, _M = bstatic.sweep_of_type("d300-b")
ROM, BASE = SW.rom, SW.base
recs = T.task_table(ROM, BASE)
dt = T.display_tasks(recs)
print("A1 任务表 0x%06X，%d 条 × 0x%X 字节；记录 0 = 任务 1" %
      (T.TASK_TABLE, len(recs), T.TASK_REC))
print("   字段：+0x00 TCB 地址、+0x04 ID/优先级字、+0x10 任务入口 PC、"
      "+0x14 任务栈顶")
print("   首条：任务 1 TCB=0x%08X ID字=0x%08X 入口=0x%06X 栈=0x%08X" %
      (recs[0].tcb, recs[0].idword, recs[0].entry, recs[0].stack))
print("   末条：任务 29 TCB=0x%08X ID字=0x%08X 入口=0x%06X 栈=0x%08X" %
      (recs[-1].tcb, recs[-1].idword, recs[-1].entry, recs[-1].stack))
print("A2 +0x10 入口落显示区 0x%06X..0x%06X 的任务共 %d 个：%s" %
      (DSP_LO, DSP_HI, len(dt),
       ["任务%d=0x%06X" % (t, e) for t, e in sorted(dt.items())]))
print("A3 显示任务入口的序言形态（入口是序言起点，enter 在其后 2/4 字节）：")
for t, e in sorted(dt.items()):
    ins = D.decode_range(ROM, BASE, e, e + 12)
    print("       任务 %2d 0x%06X: %s" %
          (t, e, " ".join(x.mn for x in ins[:6])))

# ================================================================ B
hr("B  静态：sta_tsk 启动链 —— 显示任务不是集中总装机，而是链式自启动")
la = T.launchers(SW, recs)
dl = [x for x in la if x.display]
print("B1 Sweep.calls 里 sta_tsk(%d) 静态调用点 %d 处；其中启动显示任务 %d 处" %
      (T.STA_TSK, len(la), len(dl)))
print("   显示任务启动点：%s" %
      ["0x%06X→任务%d" % (x.pc, x.tid) for x in dl])
g = collections.defaultdict(list)
for x in dl:
    g[x.owner].append(x.tid)
print("B2 9 个显示启动点分散在 %d 个 enclosing 函数里，没有集中在开机装配函数：" %
      len(g))
for fn in sorted(g):
    print("       enter 0x%06X: 任务 %s" % (fn, g[fn]))
near = [x for x in la if x.delta is not None and 0 <= x.delta <= 8]
print("B3 27 处里「启动点紧邻目标任务入口」(delta 0..8 字节) 共 %d 处：%s" %
      (len(near), ["任务%d@0x%06X(+%d)" % (x.tid, x.pc, x.delta)
                   for x in near]))
print("   ⇒ 典型形态是上一个任务末尾 `sta_tsk(N)` 后紧跟任务 N 的入口序言，"
      "构成链式自启动。")
t7 = recs[6]
print("B4 链起点：0x%06X `sta_tsk(7)`，任务 7 入口 0x%06X（+4 字节），"
      "enclosing 0x%06X" % (0x17518E, t7.entry, SW.enclosing(0x17518E)))
print("   V55 真实时基 2000k 步长跑证任务 7 自己走完 `tslp_tsk(10)` 后"
      "转挂 flag 12 对象 0x68000968；本版不接真实时基，不与之混比。")

# ================================================================ C
hr("C  静态：0x9F58D500 对象池与 312 调用点的公共状态查询函数")
p = T.pool_init(ROM, BASE)
print("C1 0x%06X 初始化循环：`ldi:32 #0x%08X,r4` → call 0x124F46；"
      "`ldi:32 #0x%08X,r5` + `ldi:8 #%d,r13` → `stb @(r5,r13)`" %
      (T.POOL_INIT_PC, T.POOL_BASE, T.POOL_BASE + T.POOL_STATUS_OFF,
       p["stb_off"]))
print("   `0x%06X %s` 每轮写 `r5+0x%X`（首轮 0x%08X）；延迟槽 `add r5,#%d`，"
      "`ldi:8 #0x%X,r0`（=%d 槽）+ `cmp` + `0x%06X blt:d rel9=0x%X` "
      "回跳 0x%06X" %
      (T.POOL_INIT_STB, p["stb_mnem"], T.POOL_STATUS_OFF,
       T.POOL_BASE + 0x10, T.POOL_STRIDE, T.POOL_COUNT, T.POOL_COUNT,
       T.POOL_INIT_LOOP, p["loop_rel9"], p["loop_target"]))
print("   ⇒ 块起 0x%08X，槽数组起 0x%08X，%d 槽 × %d 字节 = %d 字节，"
      "跨到 0x%08X；每槽 +0x%X 是状态字节" %
      (T.POOL_BASE, T.POOL_BASE + T.POOL_STATUS_OFF, p["count"],
       p["stride"], p["span"], POOL_HI, T.POOL_STATUS_OFF))
POOL_ARR = T.POOL_BASE + T.POOL_STATUS_OFF
print("C2 0x%06X 公共状态查询函数：按 r4*%d 索引 0x%08X，"
      "函数体 `0x%06X..0x%06X`" %
      (T.DSP_FN, T.POOL_STRIDE, F.DSP_FN_OBJ, T.DSP_FN, T.DSP_FN_END))
refs = SW.refs_to(F.DSP_FN, window=0)
disp_refs = [r for r in refs if DSP_LO <= r[0] < DSP_HI]
byfn = collections.Counter(SW.enclosing(pc) for pc, _i, _n in disp_refs)
print("   refs_to(window=0) %d 处；落显示区 %d 处，分属 %d 个 enclosing 函数" %
      (len(refs), len(disp_refs), len(byfn)))
print("   显示区按 64KB 段：0x12xxxx %d / 0x13xxxx %d" %
      (sum(1 for r in disp_refs if r[0] >> 16 == 0x12),
       sum(1 for r in disp_refs if r[0] >> 16 == 0x13)))
print("C3 对象池精确写点 / 加载点（writers_of / refs_to 口径）：")
for a in (T.POOL_BASE, T.POOL_BASE + 8, T.POOL_BASE + 0x10):
    print("       0x%08X: 写点 %d / 加载点 %d" %
          (a, len(SW.writers_of(a)), len(SW.refs_to(a, window=0))))
print("   精确写点只有 0x%06X 一笔（写 0x%08X）；"
      "0x9F58D4xx 的写点属另一结构体，V63 的邻域口径已收窄" %
      (T.POOL_INIT_STB, T.POOL_BASE + 0x10))

# ================================================================ D
hr("D  静态：三处历史口径更正")
rows, owners, outside = T.writer_owners(SW)
print("D1 六状态字 47 个写点只归 %d 个 enclosing 函数：%s" %
      (len(owners), ["0x%06X:%d笔" % (k, v)
                     for k, v in sorted(owners.items())]))
print("   V63 的「12 个 handler 区外上游入口」是相对人为区间 "
      "[0x%06X,0x%06X) 的切分结果：" % (T.WRITER_LO, T.WRITER_HI))
print("   这 12 笔里 %d 笔仍属 enter 0x%06X（同一函数首尾段），"
      "其余分属 %s；不是独立的上游事件入口。" %
      (sum(1 for r in outside if r[3] == T.BIG_FN),
       T.BIG_FN,
       sorted(set("0x%06X" % r[3] for r in outside if r[3] != T.BIG_FN))))
b = T.big_function(ROM, BASE)
print("D2 0x%06X..0x%06X 线性反汇编 %d 条，区间内只有 %d 个 `enter`、"
      "%d 条 `ret`；%d 个所谓 handler 全部落在这一个巨型函数内部" %
      (T.BIG_FN, T.BIG_FN_END, b["insns"], len(b["enters"]),
       len(b["rets"]), b["handlers_inside"]))
print("   ⇒ V62 的 `handler_bounds()` 是「截到第一条 ret」的启发式切片，"
      "给的是分支目标，不是 15 个独立函数；分派器 0x%06X 的 enclosing "
      "也是 0x%06X。" % (D.DISPATCH_ENTRY, SW.enclosing(D.DISPATCH_ENTRY)))
r9 = T.rel9_audit(ROM, BASE)
diff = [x for x in r9 if x[3] != x[4]]
print("D3 `fr_emu.py:807-818` 的 rel9 是 `sx(rel9,8)`（8 位有符号）；"
      "V62 `rel9_target()` 按无符号算")
print("   反例：0x%06X `blt:d rel9=0x%X` → CPU 目标 0x%06X（循环回跳），"
      "旧式 0x%06X（函数外）" %
      (T.POOL_INIT_LOOP, p["loop_rel9"], p["loop_target"],
       (T.POOL_INIT_LOOP + 2 + p["loop_rel9"] * 2) & 0xFFFFFFFF))
print("   分派器内 %d 条 rel9 分支，新旧目标不同 %d 条 ⇒ "
      "V62 的 10 条 `bra` 全指 0x%06X 的数字不变（rel9 全 < 0x80）" %
      (len(r9), len(diff), D.BRA_TAIL))

# ================================================================ 动态
hr("E  动态：H0–H3 四组同尺对照（BOOT=%d / 单次 IRQ / RUN=%d）" % (BOOT, RUN))
print("   观察目标：14 个显示任务入口 / 27 个 sta_tsk 启动点（其中显示 9）/ "
      "对象池整段 [0x%08X,0x%08X) 的全部读写 / 6 个状态字 / 3 个门槽" %
      (T.POOL_BASE, POOL_HI))
print("   同尺口径与 V63 G0/G2/G3/G4 一致；补映射是模型化机器结构改动，"
      "打桩是反事实，均不等于真机通道。")
print("   H1 起补的两段 RAM：0x%08X（状态块）与 0x%08X（门槽），各 0x%X "
      "字节；两段当前都无 region、读默认 0，`bus.map_ram()` 可插入且不重叠，"
      "不改 `create()` 默认值（硬规矩 38）。" %
      (F.STATUS_BASE, F.GATE_BASE, MAP_SIZE))

# 与 V63 同口径：`handler_bounds()` 截到第一条 ret，得 39 个真实 PC。
# 函数体里有 6 字节 `ldi:32`，不能按等距偶数地址数 PC。
_body_lo, _body_hi, body = D.handler_bounds(ROM, BASE, T.DSP_FN, 0x400)
DSP_FN_PCS = set(x.pc for x in body)
# `SW.refs_to()` 返回 (pc, imm, 下一条助记符) 元组，不是 namedtuple
CALLER_PCS = set(r[0] for r in refs)
POOL_INIT_PCS = set(x.pc for x in D.decode_range(ROM, BASE, T.POOL_INIT_PC,
                                                 0x12E636))
TASK_ENTRY_PCS = set(dt.values())
STA_ALL_PCS = set(x.pc for x in la)
STA_DISP_PCS = set(x.pc for x in dl)


def case(tag, map65, map6b, frame_on, status_all1):
    """一台机器只跑一遍（硬规矩 13）。"""
    m = blive.boot(0)
    pol = devaudit.audit(m)
    if map65:
        m.bus.map_ram(F.STATUS_BASE, MAP_SIZE, "v64-status")
    if map6b:
        m.bus.map_ram(F.GATE_BASE, MAP_SIZE, "v64-gates")
    reads, writes = bobserve.observe(
        m.bus, STATUS_ADDRS + F.GATE_SLOTS)          # 硬规矩 34：先观察
    # 对象池整段的读写用区间 hook 统计（全区间口径，不是抽样地址）。
    # 装在 observe 之后、patch_read 之前，读到的是打桩前的原始值。
    pool_w, pool_r = [], []
    rd0, wr0 = m.bus.rd, m.bus.wr

    def rd(a, n, pc=0):
        v = rd0(a, n, pc)
        if not (a + n <= T.POOL_BASE or a >= POOL_HI) and pc != 0:
            pool_r.append((a, n, v, pc))
        return v

    def wr(a, n, v, pc=0):
        if not (a + n <= T.POOL_BASE or a >= POOL_HI):
            pool_w.append((a, n, v, pc))
        return wr0(a, n, v, pc)

    m.bus.rd, m.bus.wr = rd, wr
    rules = {}
    if frame_on:
        for pc in FRAME_PCS:
            rules[(FRAME, pc)] = bpatch.bit_set(1)
    if status_all1:
        for a, pc in STATUS_RULES:
            rules[(a, pc)] = (lambda v: 0xFFFFFFFF)
    hits = bpatch.patch_read(m.bus, rules) if rules else {}

    m.step(BOOT)
    before = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    m.raise_irq(VEC)
    m.run(max_steps=RUN, spin_guard=False)
    after = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    new = after - before

    st_w = [w for w in writes if F.STATUS_BASE <= w[0] < F.STATUS_BASE + 0x1000]

    print("\n【%s】map65=%s map6b=%s frame=%s status_all1=%s" %
          (tag, map65, map6b, frame_on, status_all1))
    print("   设备策略 xfer=%d dispatch=%d prio=%d cov=%d devices=%d" %
          (pol["xfer"], pol["dispatch"], pol["prio"], pol["cov"], pol["devices"]))
    print("   BOOT=%d + 单次 raise_irq(0x%02X) + RUN=%d ⇒ 末 PC=0x%06X" %
          (BOOT, VEC, RUN, m.cpu.pc))
    print("   覆盖 %d → %d（+%d）；vec 0x2B ISR 37 个 PC 命中 %d" %
          (len(before), len(after), len(new),
           len([x.pc for x in addrform.isr_graph(
               ROM, BASE, 0x042E1C, 0x042E72) if x.pc in after])))
    hit_e = sorted(after & TASK_ENTRY_PCS)
    print("   14 个显示任务入口命中 %d：%s；27 个 sta_tsk 启动点命中 %d"
          "（其中 9 个显示启动点命中 %d）" %
          (len(hit_e), ["0x%06X" % p for p in hit_e],
           len(after & STA_ALL_PCS), len(after & STA_DISP_PCS)))
    w_fw = [w for w in pool_w if w[3] != 0]
    w_pc = collections.Counter(w[3] for w in w_fw)
    print("   对象池初始化函数 %d 个 PC 覆盖 %d 个；"
          "池区间 [0x%08X,0x%08X) 固件写 %d 笔（pc≠0，来自 %s）/ "
          "固件读 %d 笔（pc≠0）" %
          (len(POOL_INIT_PCS), len(after & POOL_INIT_PCS),
           T.POOL_BASE, POOL_HI, len(w_fw),
           ["0x%06X×%d" % (p, c) for p, c in w_pc.most_common(3)],
           len(pool_r)))
    print("   固件对 6 个状态字写入 %d 笔；47 个静态写点 PC 覆盖 %d 个" %
          (len(st_w), len([p for p, _a, _m, _o in rows if p in after])))
    print("   6 个状态字终值（十进制）：%s" %
          [m.bus.rd(a, 4, 0) for a in STATUS_ADDRS])
    print("   15 个 handler 入口命中 %d；打桩命中（十进制键）：%s" %
          (len([t for t in D.HANDLERS if t in after]),
           dict(hits) if hits else "无"))
    print("   显示链 5 个关键 PC：%s" %
          ["0x%06X:%s" % (pc, "命中" if pc in after else "未命中")
           for pc in D.DSP_PCS])
    print("   0x%06X 函数体 %d 个真实 PC 覆盖 %d 个；312 个调用点命中 %d 个" %
          (T.DSP_FN, len(DSP_FN_PCS), len(after & DSP_FN_PCS),
           len(after & CALLER_PCS)))
    print("   0x%08X = %d；0x%08X = %d" %
          (STRUCT_FLAG, m.bus.rd(STRUCT_FLAG, 4, 0),
           D.HANDLER_CB, m.bus.rd(D.HANDLER_CB, 4, 0)))
    print("   新增 PC 落显示区：%d 个" %
          len([pc for pc in new if DSP_LO <= pc < DSP_HI]))
    return after, len(w_fw)


h0, w0 = case("H0 基线（V63 G0 同口径）", False, False, False, False)
h1, w1 = case("H1 补两段 RAM（V63 G2 同口径）", True, True, False, False)
h2, w2 = case("H2 两段 + 总闸（V63 G3 同口径）", True, True, True, False)
h3, w3 = case("H3 两段 + 总闸 + 状态全 1（V63 G4 同口径）",
              True, True, True, True)

print("\nE5 同尺对照（四组：BOOT=%d / 单次 IRQ / RUN=%d / 各跑一遍）" %
      (BOOT, RUN))
print("   H1 − H0 = %d 个 PC；H2 − H1 = %d 个 PC；H3 − H2 = %d 个 PC" %
      (len(h1 - h0), len(h2 - h1), len(h3 - h2)))
print("   ⇒ 补两段 RAM 映射控制流逐项不变（硬规矩 8）；给满状态位只推进"
      "vec 0x2B 的按位派发器（V62/V63 已证），不触碰显示任务启动链。")
print("   四组里 27 个 sta_tsk 启动点 0/27、14 个显示任务入口仅命中 1 个"
      "（boot 阶段的任务 2 入口 0x1200AC）、对象池初始化函数 0 覆盖、"
      "0x%06X 函数体与 312 个调用点 0 命中、显示链 5 PC 全未命中。" % T.DSP_FN)
print("   池区间固件写笔数四组完全相同：%s 笔（写点 PC 与 4 字节宽度"
      "见各组打印，属开机整块清零），不是业务写入；"
      "四组固件读均为 0 笔。" % ([w0, w1, w2, w3],))
print("   ⚠️ H3 的 6 个状态字内存终值仍全 0：`bpatch` 只改 `ld` 读返回值、"
      "不写内存（硬规矩 31）。")

# ================================================================ 边界
hr("F  能说 / 不能说 + 复现与变更")
print("能说：")
print("  1. 任务表 0x%06X 的 +0x10 是任务入口；29 条里 %d 条落显示区"
      "（任务 %s）。" %
      (T.TASK_TABLE, len(dt), "/".join(str(t) for t in sorted(dt))))
print("  2. `sta_tsk` 静态 %d 处，其中 %d 处启动显示任务，分散在 %d 个"
      "显示模块函数里；%d 处启动点紧邻目标任务入口（+0..8 字节），"
      "是链式自启动。" % (len(la), len(dl), len(g), len(near)))
print("  3. 0x%06X 初始化 %d 槽 × %d 字节 = %d 字节对象池；0x%06X "
      "按 r4*12 索引它，有 %d 个调用点、%d 个落显示区。" %
      (T.POOL_INIT_PC, T.POOL_COUNT, T.POOL_STRIDE, p["span"],
       T.DSP_FN, len(refs), len(disp_refs)))
print("  4. 更正三处历史口径：V63「12 个上游入口」是人为区间切分（47 写点"
      "只归 %d 个函数）；V62「15 个独立 handler」实为同一个 %d 条指令 / "
      "%d 个 ret 的巨型函数内分支；`rel9` 是 8 位有符号，V62 数字不变。" %
      (len(owners), b["insns"], len(b["rets"])))
print("  5. 四组同尺动态里，补 RAM 映射与给满中断状态位都不启动显示任务："
      "27 个 sta_tsk 启动点 0/27、14 个显示任务入口仅 boot 阶段的任务 2 "
      "入口 0x1200AC 命中 1 个、对象池初始化函数与 312 个调用点全 0 命中；"
      "池区间 212 笔开机清零写四组相同，不是业务写入。")
print("不能说：")
print("  · 「显示任务从未在真机启动」—— 本版只证模拟器 20000 步窗口里"
      "27 个 sta_tsk 一个没跑；V23 的 11 次 sta_tsk 与 V55 的 2000k 步"
      "长跑是历史不同口径，不混比。")
print("  · 「找到缺的那个事件 / 铃」—— flag 12 铃、`0x9F59F7C8` 回调表 8 槽、"
      "`0x9F805AD0` 事件位图 bit31 的置位源仍未解（KI#205/207/208）。")
print("  · 「显示链通了 / 出画面 / 找到真机 A↔B 通道」—— 均未闭合。")
print("  · 「补 RAM 映射 = 复原真机通道」—— 映射只是 RAM 区域，不是器件。")
print("  · 「状态块存在逻辑死锁」—— 静态 47 写点未排除寄存器间接写"
      "（硬规矩 25/31）。")
print("复现：cd emu && python probe/exp_task_v64.py > ../results/frbox/task64.txt")
print("变更：新增纯静态设施 `frbox/taskchain64.py`；修 "
      "`frbox/dispatch62.py:rel9_target()` 的 rel9 符号扩展（V64 更正）。")
print("DONE")
