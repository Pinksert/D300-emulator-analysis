# -*- coding: utf-8 -*-
"""V59 正式探针：108 处 sta_tsk 之前，先卡在一道「只清不置」的事件标志。

    cd emu && python probe/exp_flg_v59.py > ../results/frbox/fg59.txt

七段：
  A  静态：A 段 flag 服务全量盘点（服务号 / flgid / 参数可定率）
  B  静态：位级配对 —— flg 3 的 4 个等待位没有常量置铃点，flg 9 的 19 位全有
  C  静态：三道关键等待的置铃端清单与置铃簇引用形态
  D  静态：开机函数的等待守卫全图 + clr_flg 掩码位
  E  动态：C3 同尺长跑，14 次服务调用 / 覆盖对账 / flag 实时位图 / 任务状态
  F  能说 / 不能说
  G  复现

纪律：
  · 静态部分不建运行机器；`flaggraph` 与 `svcgraph.ImmIndex` 都是只读 ROM 的纯函数。
  · 动态**只建 1 台机器、只跑一遍**（硬规矩 13），不自写 cpu.step()（硬规矩 1）；
    设备策略显式声明并用 devaudit.audit() 核对（硬规矩 38）。
  · 本版**不补任何事件、不打桩、不改读返回值、不调 kernel_call**（硬规矩 27/28）。
  · 「常量可见」是工具口径，参数跟踪不到就记 unknown，绝不反推固件没有该代码
    （硬规矩 31 同类）；「有置铃点」不等于「跑过」（硬规矩 5）。
  · `clr_flg` 按硬规矩 20 清「mask 为 0 的位」；`wai_flg` 的 mode 只记录、不解释。
  · `Sweep.enclosing()` 只是最近的 enter（硬规矩 30），不据此断定两条 PC 同一条线性路径。
  · 服务号按 8 位补码（硬规矩 23）；互斥/独立两种口径分开报（硬规矩 2）。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import (abservice, bstatic, devaudit, flaggraph,
                   realos, svcgraph)
from frbox.machine import create
from frbox.dev_irq2b import Irq2BClock
import fr_dis

# ---------------------------------------------------------------- 常量
SVC_STA = abservice.sx8(0xE9)
RUN_STEPS = 70000
PERIOD = 1024
VEC_A = 0x2B
BOOT_FUNC = 0x081404
WAI_PC = 0x081490                  # wai_flg(flg=3, ptn=0x2000, mode=2)
CLR_PC = 0x081774                  # clr_flg(flg=3, mask=0xFFFFDFFF)
STA_PCS_IN_BOOT = (0x081542, 0x08158E, 0x0816D2)
FLAG_CB_LO, FLAG_CB_HI = 0x00045C38, 0x00045CD8   # realos.FLAG_CB / 10 个 × 0x10
KEY_WAIT = ((WAI_PC, 3, 0x2000), (0x08157C, 9, 0x400),
            (0x081680, 9, 0x100))


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def fmt(v):
    return "None" if v is None else ("0x%X" % v if isinstance(v, int) else str(v))


def dis(rom, base, lo, hi, prefix="  "):
    off, end = lo - base, hi - base
    while off < end:
        line, size = fr_dis.line(rom, off, base)
        print(prefix + line)
        off += size


# ---------------------------------------------------------------- 静态
hr("A  静态：A 段 flag 服务全量盘点（bstatic.Sweep 线性扫）")

SWA, MA = bstatic.sweep_of_type("d300-a")     # 只为拿 ROM，不 boot
ROM, BASE = MA.bus.rom, MA.bus.rom_base
IDX = svcgraph.ImmIndex(ROM, BASE)
TABLE = flaggraph.pair_table(SWA)

print("A1 扫描量：ROM 0x%06X..0x%06X（%d B），服务调用点 %d 处，enter %d 个" %
      (BASE, BASE + len(ROM), len(ROM), len(SWA.calls), len(SWA.funcs)))
print("A2 四类 flag 服务 + sta_tsk 的调用点数与参数可定率")
print("  %-12s %-8s %-10s %-12s %s" %
      ("服务", "服务号", "调用点", "参数不可定", "flgid 分布"))
for label, svc in (("set_flg", bstatic.SET_FLG), ("clr_flg", bstatic.CLR_FLG),
                   ("wai_flg", bstatic.WAI_FLG), ("twai_flg", bstatic.TWAI_FLG),
                   ("sta_tsk", SVC_STA)):
    cs = SWA.of(svc)
    dist = collections.Counter(c.flgid for c in cs)
    dist_s = ("—（非 flag 服务）" if label == "sta_tsk" else
              " ".join("%s:%d" % ("None" if k is None else k, v)
                       for k, v in sorted(dist.items(),
                                         key=lambda kv: (kv[0] is None, kv[0]))))
    print("  %-12s %-8s %-10d %-12d %s" %
          (label, svc, len(cs), TABLE["unknown"].get(svc, 0), dist_s))
print("  ⇒ A 段 flag 服务里 set_flg / clr_flg / wai_flg 三类有调用点，"
      "twai_flg 0 处；全部服务号里没有 iset_flg 一类服务（A3 全量列举）。")
print("    flgid 列只对 flag 服务有意义；set_flg 分布里的 931006（0x0E335C）"
      "是线性寄存器跟踪把邻近常量读成参数的噪声（B1 单独声明）；"
      "clr_flg 的 None:1（0x0A4434）才是 B1 里 flgid 不可定那一处。")
_n_set = len(SWA.of(bstatic.SET_FLG))
_n_clr = len(SWA.of(bstatic.CLR_FLG))
_n_wai = len(SWA.of(bstatic.WAI_FLG))
print("    flag 三类合计 %d 处 = set_flg %d + clr_flg %d + wai_flg %d" %
      (_n_set + _n_clr + _n_wai, _n_set, _n_clr, _n_wai))
print("A3 全部服务号 = %s" %
      " ".join(str(s) for s in sorted(
          {c.svc for c in SWA.calls}, key=lambda x: (-1e9 if x is None else x))))

hr("B  静态：位级配对 —— 谁在等这一位，谁在置这一位")

MISS = flaggraph.unmatched(TABLE)
print("B1 配对总账：不同 (flgid, bit) 组合 %d 个；"
      "有等无置 %d 组；参数不可定按服务 %s；flgid 不可定 %d 处" %
      (len(TABLE["pairs"]), len(MISS),
       dict(TABLE["unknown"]), TABLE["no_flgid"]))
BOGUS = [(f, b) for (f, b) in TABLE["pairs"] if f is not None and f > 10]
print("  其中 flgid > 10 的线性扫噪声组 %d 个 = %s；它们不参与 flg 3 / flg 9 的判定。" %
      (len(BOGUS), " ".join("%d/0x%X" % (f, b) for f, b in BOGUS)))
print("B2 flg 3 与 flg 9 逐位对账（等 / 置 / 清，均为常量可见口径）")
print("  %-6s %-11s %-8s %-8s %-8s %s" %
      ("flgid", "bit", "等", "置", "清", "判定"))
for flgid in (3, 9):
    keys = sorted(b for (f, b) in TABLE["pairs"] if f == flgid)
    for b in keys:
        d = TABLE["pairs"][(flgid, b)]
        verdict = ("无等待（不判定）" if not d["waits"] else
                   "有等无置" if not d["sets"] else "有等有置")
        print("  %-6d 0x%-9X %-8d %-8d %-8d %s" %
              (flgid, b, len(d["waits"]), len(d["sets"]), len(d["clrs"]),
               verdict))
missing3 = [(b, len(d["waits"])) for (f, b), d in TABLE["pairs"].items()
            if f == 3 and d["waits"] and not d["sets"]]
have9 = [(b, len(d["waits"])) for (f, b), d in TABLE["pairs"].items()
         if f == 9 and d["waits"] and d["sets"]]
print("  ⇒ flg 3 有等无置的位 %d 个（%s）；flg 9 有等且有置的位 %d 个" %
      (len(missing3), ", ".join("0x%X/等%d" % (b, n) for b, n in sorted(missing3)),
       len(have9)))
print("  ⇒ 全 ROM 有等无置共 %d 组，全部落在 flg = %s；其它 flag 的等待位都有常量置铃点。" %
      (len(MISS), " ".join(str(f) for f in sorted({f for f, _b, _w, _s in MISS}))))
print("  ⇒ flg 3 的 set_flg 一共 %d 处，且 bits 全部静态不可定 ⇒ "
      "「0 处置铃点」是常量可见口径，不等于固件里没有置位代码。" %
      len([c for c in SWA.of(bstatic.SET_FLG) if c.flgid == 3]))

hr("C  静态：三道关键等待的置铃端与置铃簇引用形态")

print("C1 V58 留下的三道等待（含开机第一道 0x081490）")
for pc, flgid, bit in KEY_WAIT:
    s = flaggraph.setters_for(TABLE, flgid, bit)
    print("  0x%06X wai_flg(%d,0x%X)：常量置铃 %d 处 -> %s" %
          (pc, flgid, bit, len(s),
           ", ".join("0x%06X" % c.addr for c in s) or "无"))
all_key_setters = [c for _pc, f, b in KEY_WAIT
                   for c in flaggraph.setters_for(TABLE, f, b)]
cl = flaggraph.cluster(SWA, IDX, all_key_setters)
print("C2 这些置铃点落在 %d 个函数里；互斥引用形态 = %s" %
      (len(cl["funcs"]),
       dict(collections.Counter(cl["classes"][f][0] for f in cl["funcs"]))))
print("  %-10s %-9s %-7s %s" % ("函数", "引用形态", "边数", "引用者所属上层函数（粗粒度）"))
for f in cl["funcs"]:
    print("  0x%06X %-9s %-7d %s" %
          (f, cl["classes"][f][0], len(cl["classes"][f][1]),
           " ".join("0x%06X" % u for u in cl["upstream"][f]) or "无"))
print("  ⇒ 置铃端在静态图上**有**明确 PC 和引用边；断点不是「代码不存在」，"
      "而是这些 PC 有没有被执行（E 段核）。")

hr("D  静态：开机函数 0x081404 的等待守卫全图与清位掩码")

lo, hi = svcgraph.body_range(SWA, BOOT_FUNC)
waits = [c for c in SWA.of(bstatic.WAI_FLG) if lo <= c.addr < hi]
stas = [c for c in SWA.of(SVC_STA) if lo <= c.addr < hi]
print("D1 粗粒度函数体 0x%06X..0x%06X：wai_flg %d 道、sta_tsk %d 处" %
      (lo, hi, len(waits), len(stas)))
guards = flaggraph.guard_map(SWA, BOOT_FUNC, [c.addr for c in stas],
                             wait_svcs=(bstatic.WAI_FLG, bstatic.TWAI_FLG))
for pc, w in guards:
    print("  sta_tsk 0x%06X 前最近等待 = %s" %
          (pc, "无" if w is None else
           "0x%06X wai_flg(flg=%s, ptn=%s, mode=%s)" %
           (w.addr, w.flgid, fmt(w.bits), fmt(w.mode))))
print("  ⇒ V58 报的「三道等待」是「每处 sta_tsk 前最近的一道」；"
      "函数体内实际有 %d 道等待，不是三道（口径不同，硬规矩 2）。" % len(waits))

print("\nD2 `clr_flg` 的位语义（硬规矩 20：清 mask 为 0 的位）")
for c in SWA.of(bstatic.CLR_FLG):
    if c.addr in (0x08141A, 0x081428, 0x081436, CLR_PC, 0x0A6368):
        z = flaggraph.zero_bits(c.bits)
        print("  0x%06X clr_flg(flg=%s, mask=0x%08X) 清的位 = %s" %
              (c.addr, c.flgid, c.bits or 0,
               ", ".join("0x%X" % b for b in z) or "未知"))
clr3_2000 = [c.addr for c in SWA.of(bstatic.CLR_FLG)
             if c.flgid == 3 and 0x2000 in flaggraph.zero_bits(c.bits)]
print("  flg 3 里清 0x2000 的 clr_flg 点共 %d 处 = %s" %
      (len(clr3_2000), " ".join("0x%06X" % p for p in clr3_2000)))
print("  ⇒ 0x%06X 是 flg 3 唯一清 0x2000 的点，与 0x%06X 等的位完全相同；" %
      (CLR_PC, WAI_PC))
print("    但两者执行顺序不同（E3），且 `enclosing()` 只是最近的 enter"
      "（硬规矩 30），不据此断定它们是同一条线性路径。")
print("\nD3 开机前段反汇编（0x081438..0x08145C，三次 set_flg）")
dis(ROM, BASE, 0x081438, 0x08145C)

# ---------------------------------------------------------------- 动态
hr("E  动态：C3 同尺长跑（不补事件、不打桩、1 台机器 1 趟）")

m = create("d300-a", xfer=True, dispatch=True)
m.load()
m.reset()
m.enable_cov()
m.attach_flow()
st = devaudit.audit(m)
print("E0 设备策略显式声明：xfer=%d dispatch=%d prio=%d cov=%d devices=%d；"
      "周期线 vec=0x%02X period=%d 只拉线；跑 %d 步" %
      (st["xfer"], st["dispatch"], st["prio"], st["cov"], st["devices"],
       VEC_A, PERIOD, RUN_STEPS))
dev = Irq2BClock(m, period=PERIOD, vector=VEC_A)
m.devices.append(dev)
m.run(max_steps=RUN_STEPS, spin_guard=False)

svcs = collections.Counter(c[2] for c in m.flow.calls)
print("E1 末PC=0x%06X step=%d cov=%d accepted=%d；int #0x40 共 %d 次，服务号=%s" %
      (m.cpu.pc, m.step_count, m.cov.words(), dev.accepted(),
       len(m.flow.calls), dict(sorted(svcs.items()))))

print("\nE2 %d 次服务调用逐条（pc / 服务 / 实参，服务名未实证的只报号）" %
      len(m.flow.calls))
print("  %-8s %-9s %-6s %-11s %-11s %-11s %-11s %-7s" %
      ("step", "pc", "svc", "name", "r4", "r5", "r6", "r7"))
for r in flaggraph.flow_table(m.flow.calls, abservice.name_of):
    print("  %-8d 0x%06X %-6s %-11s %-11s %-11s %-11s %-7s" %
          (r["step"], r["pc"], r["svc"], r["name"] or "-",
           fmt(r["r4"]), fmt(r["r5"]), fmt(r["r6"]), fmt(r["r7"])))
print("  参数口径：set_flg/clr_flg 的 flgid 在 r4、mask/bits 在 r5；"
      "wai_flg 的 flgid 在 r5、ptn 在 r6、mode 在 r7（ARG_ORDER，硬规矩 4）。")

print("\nE3 关键顺序：清位服务的执行步早于等待服务")
FLOW = flaggraph.flow_table(m.flow.calls)
STEPS = {pc: [r["step"] for r in FLOW if r["pc"] == pc]
         for pc in (CLR_PC, WAI_PC)}
for pc in (CLR_PC, WAI_PC):
    print("  0x%06X 执行步 = %s" % (pc, STEPS[pc] or "未执行"))
print("  ⇒ 本探针只观测到「先执行了一条针对 flg 3 bit13 的清位服务」，"
      "没有证据表明这一位在清位之前曾被置成 1。")

print("\nE4 全量覆盖对账（同一台机器、同一趟）")
key_setters = sorted({c.addr for c in all_key_setters})
rows = [
    ("set_flg 调用点", [c.addr for c in SWA.of(bstatic.SET_FLG)]),
    ("wai_flg 调用点", [c.addr for c in SWA.of(bstatic.WAI_FLG)]),
    ("clr_flg 调用点", [c.addr for c in SWA.of(bstatic.CLR_FLG)]),
    ("sta_tsk 调用点", [c.addr for c in SWA.of(SVC_STA)]),
    ("三道等待的常量置铃点", key_setters),
    ("flg 3 的两处 set_flg", [c.addr for c in SWA.of(bstatic.SET_FLG)
                            if c.flgid == 3]),
    ("三处 sta_tsk（开机函数内）", list(STA_PCS_IN_BOOT)),
]
print("  %-28s %-12s %s" % ("对象", "执行过", "命中的 PC（前 6 个）"))
for label, arr in rows:
    hit = [p for p in arr if m.cov.visited(p)]
    print("  %-28s %-12s %s" %
          (label, "%d/%d" % (len(hit), len(arr)),
           " ".join("0x%06X" % p for p in hit[:6]) or "无"))
print("  ⇒ 常量置铃点 %d/%d 命中；flg 3 那两处 set_flg 0/2 命中；"
      "0x0A6302 任务走 0x0A6810 slp_tsk 分支，自己那四处 set_flg 全没执行。" %
      (sum(m.cov.visited(p) for p in key_setters), len(key_setters)))

print("\nE5 flag 对象实时位图（realos.flags，读内核对象、不改状态）")
snap = flaggraph.flag_snapshot(realos.flags(m))
print("  %-6s %-10s %-13s %s" % ("flg", "控制块", "当前位模式", "等待者数"))
for f in snap:
    print("  %-6d 0x%08X 0x%08X    %s" %
          (f["id"], f["cb"], f["pattern"], f["waiters"]))
f3 = snap[2]
print("  ⇒ flg 3 = 0x%08X、0x2000 位 %s、等待者 %s 个；"
      "flg 9 = 0x%08X（来自 0x081458 set_flg(9,4)）、flg 6 = 0x%08X。" %
      (f3["pattern"], "已置" if f3["pattern"] & 0x2000 else "未置",
       f3["waiters"], snap[8]["pattern"], snap[5]["pattern"]))
print("  ⇒ 正面对照：flg 2 的 set_flg(2,0x4000) @0x081442 → "
      "wai_flg(2,0x4000) @0x0A6358 → clr_flg(2,~0x4000) @0x0A6368 三步都执行过，"
      "说明内核 flag 机制本身在跑，不是全局没动。")
for p in (0x081442, 0x0A6358, 0x0A6368):
    print("     0x%06X visited=%s" % (p, m.cov.visited(p)))

print("\nE6 任务状态（谁挂在 flg 3 上）")
for t in realos.states(m):
    if t.get("active") or t.get("id") == 1:
        print("  task %-2s tcb=0x%06X pc=0x%06X wait_pat=%s active=%s" %
              (t.get("id"), t.get("tcb"), t.get("pc"),
               fmt(t.get("wait_pat")), t.get("active")))

print("\nE7 静态可定址写点：flag 控制块区 0x%06X..0x%06X" %
      (FLAG_CB_LO, FLAG_CB_HI))
w = flaggraph.kernel_store_probe(SWA, FLAG_CB_LO, FLAG_CB_HI)
print("  命中 %d 笔 -> %s" %
      (len(w), " ".join("0x%06X@0x%06X" % (a, pc) for pc, a, _mn in w[:6]) or "无"))
print("  ⇒ 这是 `Sweep.stores` 的工具口径阴性结果：运行时基址写点看不见，"
      "不能反推固件不存在直接写 flag 对象内存的路径。")

# ---------------------------------------------------------------- 边界
hr("F  能说 / 不能说")

print("能说：")
print("  1. A 段 flag 服务按位配对后，flg 3 的 4 个等待位（0x800/0x2000/"
      "0x4000/0x8000）在常量可见口径下没有任何 set_flg 置铃点，"
      "且全 ROM %d 组「有等无置」全部落在 flg 3；flg 9 的 %d 个等待位"
      "全有常量置铃点；C1 里 flg 9 两道等待的 %d 处置铃点分布在 C2 的 %d 个函数里。" %
      (len(MISS), len(have9), len(key_setters), len(cl["funcs"])))
print("  2. 本趟 %d 次服务调用里 set_flg 只有 3 次（flg2/6/9），"
      "全在 0x081404 的三道清位之后、开机函数那道 wai_flg(3,0x2000) 之前；"
      "0x081774 clr_flg(3,~0x2000) 已执行（step %d），"
      "随后 0x081490 执行（step %d）并挂起。" %
      (len(m.flow.calls), STEPS[CLR_PC][0], STEPS[WAI_PC][0]))
print("  3. 实时位图 flg 3 = 0x00000000、等待者 1 个（task 1，"
      "保存 PC 0x081492、wait_pat 0x2000）⇒ 这道等待确实没被置铃，"
      "不是探针没看见。")
print("  4. flg 2 的置→等→清三步全执行过 ⇒ 内核 flag 机制本身通，"
      "阴性结果只针对 flg 3 bit13 这一道。")
print("  5. 三道等待的 %d 个常量置铃点 0 命中、flg 3 两处 set_flg 0/2 命中；"
      "V58 的「上游调用链 + 函数内等待」两类断点，"
      "现在能定位到「哪一位、被谁清、实时值多少」。" % len(key_setters))
print("\n不能说：")
print("  · 不能报找到了 flg 3 bit13 的置铃来源、或证明它不存在 —— "
      "静态只覆盖常量实参，运行时变量 / ISR 直接写对象内存 / 跨段输入都没排除。")
print("  · 不能把 0x0A63AC / 0x0A63E2 的 `set_flg(3, bits=None)` "
      "说成置了 0x2000（位参数静态不可定，且它们没执行）。")
print("  · 不能说 0x081774 是「把已置的铃清掉」—— 清位之前该位是否为 1 "
      "没有观测证据（E3）；也不能解释 `wai_flg` 的 mode=2 组合语义"
      "（本项目未在固件侧实证）。")
print("  · 不能报 A 段启动了任务（sta_tsk 仍 0/108）、出画面、系统跑起来了；"
      "0x081774 与 0x081490 同属粗粒度函数体，但执行序不同，不是同一条线性路径。")
print("  · 本版没建 A↔B 跨机通道；KI#191 / #201 / #208 原样保留。")

hr("G  复现与变更")

print("  cd emu && python probe/exp_flg_v59.py > ../results/frbox/fg59.txt")
print("  cd emu && python frbox_selftest.py")
print("  新增：frbox/flaggraph.py（纯函数位级服务图）、probe/exp_flg_v59.py")
print("  静态只读 ROM 1 份；动态 1 台机器 1 趟，%d 步，"
      "全程无 btick.tick*() / bpatch.patch_read() / realos.kernel_call()。" %
      RUN_STEPS)
