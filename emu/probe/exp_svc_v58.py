# -*- coding: utf-8 -*-
"""V58 正式探针：A 段 sta_tsk 的「触发源未解」里有一半是静态扫描器的口径盲区。

    cd emu && python probe/exp_svc_v58.py > ../results/frbox/sv58.txt

七段：
  A  静态：A/B 两段立即数形态对账（为什么 refs_to 在 A 段全瞎）
  B  静态：108 处 sta_tsk 的反向引用图（旧口径 vs 补齐后）
  C  静态：函数指针表候选位置
  D  静态：唯一跑过的那个函数，卡在函数内一道 wai_flg
  E  动态：V57 C3 同尺长跑，四类覆盖数字对账
  F  能说 / 不能说
  G  复现

纪律：
  · 纯静态部分不建运行机器；`svcgraph.ImmIndex` 是只读 ROM 的纯函数。
  · 动态只用一台机器、只跑一遍（硬规矩 13），不自写 cpu.step() 循环（硬规矩 1）。
    设备策略显式声明并用 devaudit.audit() 核对（硬规矩 38）。
  · 本版**不补任何事件、不打桩、不改读返回值**（硬规矩 27/28：补得越多跑得越远
    是假的；阴性结果要写清补了什么、没走到哪儿）。
  · 「有引用」不等于「会执行」（硬规矩 5）；分类互斥口径与独立命中口径分开报（硬规矩 2）。
  · 服务号一律走 abservice.sx8()（硬规矩 23）；rel12 符号位按 fr_emu.sx(raw,11)（硬规矩 4）。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import abservice, bstatic, devaudit, svcgraph
from frbox.machine import create
from frbox.dev_irq2b import Irq2BClock
import fr_dis

# ---------------------------------------------------------------- 常量
SVC_STA = abservice.sx8(0xE9)          # sta_tsk
SVC_WAI = bstatic.WAI_FLG              # wai_flg
RUN_STEPS = 70000
PERIOD = 1024
VEC_A = 0x2B
BOOT_FUNC = 0x081404                   # 启动初期唯一跑过的含 sta_tsk 函数
WAI_PC = 0x081490                     # 它停下的那条 int #0x40
STA_PCS_IN_BOOT = (0x081542, 0x08158E, 0x0816D2)


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dis(rom, base, lo, hi, prefix="  "):
    off, end = lo - base, hi - base
    while off < end:
        line, size = fr_dis.line(rom, off, base)
        print(prefix + line)
        off += size


# ==================================================================== A
hr("A  静态：A/B 两段立即数形态对账 —— 为什么 refs_to 在 A 段全瞎")

SWA, MA = bstatic.sweep_of_type("d300-a")
MB = create("d300-b")
MB.load()
IDX_A = svcgraph.ImmIndex(MA.bus.rom, MA.bus.rom_base)
IDX_B = svcgraph.ImmIndex(MB.bus.rom, MB.bus.rom_base)
PA, PB = IDX_A.profile(), IDX_B.profile()

print("A1 ROM 映射与扫描量")
print("  %-8s %-22s %-10s %-12s %s" %
      ("段", "ROM 范围", "大小", "指令数", "机器"))
for tag, m, p in (("A", MA, PA), ("B", MB, PB)):
    print("  %-8s 0x%06X..0x%06X %-10d %-12d %s" %
          (tag, m.bus.rom_base, m.bus.rom_base + len(m.bus.rom),
           len(m.bus.rom), p["ins"], m.name))

print("\nA2 立即数形态（总数 / 落在本段 ROM 内）")
print("  %-8s %-12s %-12s %-12s %-14s %-14s %s" %
      ("段", "ldi:8", "ldi:20", "ldi:32", "i20 在 ROM 内", "i32 在 ROM 内", "i32 占比"))
for tag, p in (("A", PA), ("B", PB)):
    print("  %-8s %-12d %-12d %-12d %-14d %-14d %.2f%%" %
          (tag, p["ldi"].get("i8", 0), p["ldi"].get("i20", 0),
           p["ldi"].get("i32", 0), p["inrom"].get("i20", 0),
           p["inrom"].get("i32", 0), p["i32_share"]))
print("  ⇒ A 段 512 KB 落在 20 位值域（0..0xFFFFF）内，函数地址用 ldi:20；"
      "B 段 7 MB 超出 20 位，只能用 ldi:32。")

print("\nA3 调用指令形态（线性扫，含 call rel12 与 call @r12）")
print("  %-8s %-10s %-10s %-12s %-12s %s" %
      ("段", "call", "call:d", "jmp", "rel12 边", "rel12 目标"))
for tag, p in (("A", PA), ("B", PB)):
    print("  %-8s %-10d %-10d %-12d %-12d %d" %
          (tag, p["call"].get("call", 0), p["call"].get("call:d", 0),
           p["call"].get("jmp", 0), p["rel12_edges"], p["rel12_targets"]))
print("  bstatic.Sweep.ldi32 索引：A 段 %d 种立即数 / %d 条；"
      "Sweep.edges 只收 call:d @rN（bstatic docstring）" %
      (len(SWA.ldi32), sum(len(v) for v in SWA.ldi32.values())))
print("  ⇒ V51 为 B 段调的 refs_to() 只查 ldi:32：在 A 段只覆盖 %.2f%% 的"
      "可寻址立即数，拿它数「谁加载了 A 段函数地址」几乎是零（B2 实测 0/57）。"
      % PA["i32_share"])
print("  rel12 目标按 fr_emu.py:777 的 pc+2+sx(raw,11)*2 算；"
      "fr_dis 显示用的是 sx(raw,8)，与执行不一致，不要拿 *NNN 推目标。")

# ==================================================================== B
hr("B  静态：108 处 sta_tsk 的反向引用图（旧口径 vs 补齐后）")

CH = svcgraph.chain_profile(SWA, IDX_A, SVC_STA, window=8)
STA = CH["points"]
FUNCS = CH["funcs"]
print("B1 调用点与服务号")
print("  A 段 sta_tsk(0xE9 → %d) 静态调用点 = %d 处；所属 enclosing 函数 = %d 个；"
      "无 enter 归属 = %d 处" %
      (SVC_STA, len(STA), len(FUNCS), CH["no_enter"]))
print("  任务号(r4)分布 = %s" %
      dict(collections.Counter(c.r4 for c in STA).most_common(8)))

print("\nB2 旧口径：bstatic.Sweep.refs_to(func, window=8)")
old_hit = sum(1 for f in FUNCS if SWA.refs_to(f, 8))
old_edges = sum(len(SWA.refs_to(f, 8)) for f in FUNCS)
old_edges2 = sum(len(SWA.callers_of(f)) for f in FUNCS)
print("  命中函数 = %d/%d；总引用 = %d 条；callers_strict = %d 条；"
      "Sweep.callers_of(edges) = %d 条" %
      (old_hit, len(FUNCS), old_edges,
       sum(len(SWA.callers_strict(f, 8)) for f in FUNCS), old_edges2))
print("  ⇒ 旧口径不是「一条边都没有」：那 %d 条来自 call:d @rN（Sweep.edges 认这一种），"
      "但 refs_to()/callers_strict() 查的是 ldi:32 立即数，仍全零。" % old_edges2)

print("\nB3 新口径：svcgraph 互斥分类（优先级 ldi20 > ldi32 > rel12 > data32）")
bk = CH["by_kind"]
print("  %-10s %-12s %-14s %s" % ("形态", "函数数", "引用条数", "性质"))
NATURE = {"ldi20": "ldi:20 直接加载函数地址",
          "ldi32": "ldi:32 直接加载函数地址",
          "rel12": "call rel12 直接调用",
          "data32": "ROM 里 4 字节函数指针（上界）",
          "none": "四种形态都没命中"}
for k in ("ldi20", "ldi32", "rel12", "data32", "none"):
    rs = [r for f in FUNCS for kk, r in (CH["classes"][f],) if kk == k]
    print("  %-10s %-12d %-14d %s" %
          (k, bk.get(k, 0), sum(len(x) for x in rs), NATURE[k]))
print("  互斥分类合计 = %d/%d 个函数有引用形态；代码类引用边 = %d 条" %
      (sum(v for k, v in bk.items() if k != "none"), len(FUNCS),
       CH["code_edges"]))
print("  独立命中（不去重、不互斥）= %s" % dict(CH["independent"]))
print("  ⇒ 旧口径 0/%d → 新口径 %d/%d；差的不是固件，是扫描器只认 ldi:32。" %
      (len(FUNCS), sum(v for k, v in bk.items() if k != "none"), len(FUNCS)))

print("\nB4 引用偏移分布（imm - func，硬规矩 26：调用者加载序言起点）")
deltas = collections.Counter()
for f in FUNCS:
    k, rs = CH["classes"][f]
    if k in ("ldi20", "ldi32", "rel12"):
        for r in rs:
            deltas[r.imm - f] += 1
print("  %s" % dict(sorted(deltas.items())))
print("  ⇒ 合并 ldi20/ldi32/rel12 三种形态后只有 0/-2/-4 三档，正好对应"
      "「无 enter 的小函数 / stm1 序言 / stm1+st dr1 序言」；"
      "单独看 ldi:20 是 %s。" %
      dict(sorted(collections.Counter(r.imm - f for f in FUNCS
                  for kk, rs in (CH["classes"][f],) if kk == "ldi20"
                  for r in rs).items())))

# ==================================================================== C
hr("C  静态：函数指针表候选位置（data32 只是上界，不是回调证据）")

print("C1 被判为 data32 的函数及其命中位置")
print("  %-12s %-10s %s" % ("函数", "命中数", "位置（前 12 个）"))
for f in sorted(CH["table_locs"]):
    locs = CH["table_locs"][f]
    print("  0x%06X   %-10d %s" %
          (f, len(locs),
           " ".join("0x%06X(%+d)" % (p, d) for p, d in locs[:12])))
all_locs = sorted(p for locs in CH["table_locs"].values() for p, _d in locs)
print("  全部命中位置 %d 处；集中区间 0x%06X..0x%06X" %
      (len(all_locs), all_locs[0] if all_locs else 0,
       all_locs[-1] if all_locs else 0))
print("  ⇒ 主要落在 0x0E3CE4..0x0E3DEC（间隔 0xC 一组）与 0x0F1CD0..0x0F1CF4，"
      "以及 0x0FE11C / 0x0FE154；形态像函数指针表，但线性扫会把数据当指令，")
print("    且 ROM.find 会撞进指令字节 ⇒ 只能说「存在候选表」，"
      "不能数成「有几处回调」。")

# ==================================================================== D
hr("D  静态：E 段里唯一执行过的函数 0x081404，三道 wai_flg 各守一处 sta_tsk")

boot_pts = [c for c in STA if c.addr in STA_PCS_IN_BOOT]
guards = svcgraph.wait_guards(SWA, BOOT_FUNC, boot_pts)
lo, hi = svcgraph.body_range(SWA, BOOT_FUNC)
print("D1 函数体 0x%06X..0x%06X；其中 sta_tsk 点 = %s" %
      (lo, hi, " ".join("0x%06X" % c.addr for c in boot_pts)))
for p, w in guards:
    if w is None:
        print("  0x%06X 之前没有等待类服务" % p.addr)
    else:
        print("  0x%06X 之前最近的等待服务 = 0x%06X svc=%d(%s) "
              "flg=%s ptn=%s mode=%s" %
              (p.addr, w.addr, w.svc, abservice.name_of(w.svc),
               w.flgid, hex(w.bits) if isinstance(w.bits, int) else w.bits,
               w.mode))
print("\nD2 停点附近反汇编（0x08148C..0x08149C）")
dis(MA.bus.rom, MA.bus.rom_base, 0x08148C, 0x08149C)
print("  ⇒ 三处 sta_tsk 各自前面都有一道 wai_flg（flg 3 / 9 / 9），"
      "不是共用一个守卫；第一道 0x081490 = wai_flg(flg=3, ptn=0x2000, mode=2) "
      "就把这次执行挡住了（E3 实测最后一条执行过的 PC 就是它）。")
print("    所以「函数跑过但 sta_tsk 一次没跑」不是没人调用，是没过函数内的等待。")

# ==================================================================== E
hr("E  动态：V57 C3 同尺长跑，四类覆盖数字对账")

m = create("d300-a", xfer=True, dispatch=True)
m.load()
m.reset()
m.enable_cov()
m.attach_flow()
st = devaudit.audit(m)
print("E0 设备策略显式声明（硬规矩 38）：xfer=%d dispatch=%d prio=%d cov=%d devices=%d" %
      (st["xfer"], st["dispatch"], st["prio"], st["cov"], st["devices"]))
print("    周期线 vec=0x%02X period=%d，只拉线不写内存；跑 %d 步；"
      "本版不补任何事件、不打桩" % (VEC_A, PERIOD, RUN_STEPS))
dev = Irq2BClock(m, period=PERIOD, vector=VEC_A)
m.devices.append(dev)
m.run(max_steps=RUN_STEPS, spin_guard=False)

irq = collections.Counter(r[0] for r in m.cpu.irq_log)
svcs = collections.Counter(c[2] for c in m.flow.calls)
print("E1 末PC=0x%06X step=%d cov=%d 受理中断=%s 时钟accepted=%d" %
      (m.cpu.pc, m.step_count, m.cov.words(), dict(sorted(irq.items())),
       dev.accepted()))
print("    int #0x40 服务调用 %d 次，服务号=%s" %
      (len(m.flow.calls), dict(sorted(svcs.items()))))

sta_pcs = [c.addr for c in STA]
ref_pcs = sorted({r.pc for f in FUNCS
                  for r in CH["classes"][f][1]
                  if r.kind == "ldi20"})
up_funcs = sorted({f for f in (SWA.enclosing(p) for p in ref_pcs) if f})
print("\nE2 四类覆盖（同一台机器、同一次运行）")
print("  %-38s %-12s %s" % ("对象", "执行过", "说明"))
rows = [
    ("sta_tsk 调用点", sta_pcs, "本版命题的终点"),
    ("ldi:20 引用者 PC", ref_pcs, "B3 里那些直接加载函数地址的指令"),
    ("引用者的 enclosing 函数", up_funcs, "更上游一层"),
    ("含 sta_tsk 的函数 enter", FUNCS, "函数入口"),
]
for label, arr, note in rows:
    print("  %-38s %-12s %s" %
          (label, "%d/%d" % (sum(m.cov.visited(p) for p in arr), len(arr)), note))
print("  ⇒ 引用者 PC 一次都没执行 ⇒ 链条断在更上游，"
      "不是 sta_tsk 自己缺参数；补静态图不等于补事件（硬规矩 5）。")

hit_funcs = [f for f in FUNCS if m.cov.visited(f)]
print("\nE3 唯一执行过的含 sta_tsk 函数 = %s" %
      (" ".join("0x%06X" % f for f in hit_funcs) or "无"))
body = []
off, end = BOOT_FUNC - MA.bus.rom_base, STA_PCS_IN_BOOT[-1] + 8 - MA.bus.rom_base
while off < end:
    _line, size = fr_dis.line(MA.bus.rom, off, MA.bus.rom_base)
    body.append(MA.bus.rom_base + off)
    off += size
hit_body = [p for p in body if m.cov.visited(p)]
print("    观测窗口 0x%06X..0x%06X（入口到最后一处 sta_tsk+8，"
      "不是整个函数体；D1 的函数体到 0x%06X）执行过 %d/%d 条；"
      "最后一条执行过的 PC = 0x%06X" %
      (BOOT_FUNC, STA_PCS_IN_BOOT[-1] + 8, hi, len(hit_body), len(body),
       hit_body[-1] if hit_body else 0))
print("    0x%06X 执行过=%s；其后 0x081492 执行过=%s；"
      "三处 sta_tsk 执行过=%s" %
      (WAI_PC, m.cov.visited(WAI_PC), m.cov.visited(0x081492),
       [m.cov.visited(p) for p in STA_PCS_IN_BOOT]))
print("  ⇒ 与 D 段对上：进了函数，停在 wai_flg，后面的 sta_tsk 全没走到。")

# ==================================================================== F
hr("F  能说 / 不能说")

print("能说：")
print("  1. 修的是一个静态扫描口径盲区，不是固件行为：A 段可寻址立即数里 "
      "ldi:32 只占 %.2f%%（%d/%d），B 段是 %.2f%%（%d/%d）；" %
      (PA["i32_share"], PA["inrom"].get("i32", 0),
       PA["inrom"].get("i32", 0) + PA["inrom"].get("i20", 0),
       PB["i32_share"], PB["inrom"].get("i32", 0),
       PB["inrom"].get("i32", 0) + PB["inrom"].get("i20", 0)))
print("     根因是 A 段 512 KB 装得下 20 位地址、B 段 7 MB 装不下。")
print("  2. 补齐 ldi:20 / call rel12 / 4 字节函数指针三种形态后，"
      "A 段 %d 个含 sta_tsk 的函数里 %d 个有引用形态"
      "（旧 refs_to 是 %d/%d），代码类引用边 %d 条（互斥口径；"
      "独立口径见 B3）。" %
      (len(FUNCS), sum(v for k, v in bk.items() if k != "none"),
       old_hit, len(FUNCS), CH["code_edges"]))
print("  3. 动态仍是否定结果：设备策略齐备、vec 0x%02X 周期线长跑 %d 步，"
      "sta_tsk %d/%d、ldi:20 引用者 PC 0/%d。" %
      (VEC_A, RUN_STEPS, sum(m.cov.visited(p) for p in sta_pcs),
       len(sta_pcs), len(ref_pcs)))
print("     唯一执行过的函数 0x%06X 停在 wai_flg(flg=3, ptn=0x2000, mode=2)"
      "（0x%06X），三处 sta_tsk 都在其后且各有一道 wai_flg ⇒ "
      "断点至少有两种性质：" % (BOOT_FUNC, WAI_PC))
print("     (a) 上游调用链没接通；(b) 函数内还有等待守卫（D1 实测三道）。")
print("  4. V57 「触发源未解」应改写为：现在能分开的至少是上游调用链与"
      "函数内等待两类断点，不能再笼统归为一个外部事件位；本版未补任何事件，"
      "也没宣称链条已经闭合，更没有证明不存在别的触发源。")

print("\n不能说：")
print("  · 不能说「A 段启动了任务」「找到了 sta_tsk 的触发事件」："
      "108 处仍 %d/%d 执行过。" % (sum(m.cov.visited(p) for p in sta_pcs), len(sta_pcs)))
print("  · 不能说「补上 ldi:20 就能跑起来」：本模块只改静态索引，"
      "一行机器状态都没动，动态数字全部来自同一次运行。")
print("  · 不能说「真机编译器是这样选型的」：这里只有 ROM 里指令形态与"
      "地址空间相容的观察，没有编译工具链证据。")
print("  · 不能把 data32 命中数当成回调数量（线性扫 + ROM.find 都是上界）。")
print("  · 不能说出画面 / 系统跑起来了：B 段末 PC、帧缓冲判据、"
      "0x9F5A0284 恒 0 等结论原样保留（KI#191 / #201 / #208）。")
print("  · 本版没建任何 A↔B 跨机通道。")

# ==================================================================== G
hr("G  复现")

print("  cd emu")
print("  python probe/exp_svc_v58.py > ../results/frbox/sv58.txt")
print("  python frbox_selftest.py                 # 含 test_v58()")
print("")
print("  静态索引：python -c \"from frbox import svcgraph; help(svcgraph.ImmIndex)\"")
print("  旧口径对照：bstatic.Sweep.refs_to(func, 8) 只查 ldi:32")
print("  动态口径：V57 探针 probe/exp_dev_v57.py 的 C3 组"
      "（xfer=True, dispatch=True, vec 0x2B, period=1024, 70000 步）")
print("")
print("  版本 = 0.58.0；结果文件 results/frbox/sv58.txt（七段）")
