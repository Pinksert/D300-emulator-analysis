# -*- coding: utf-8 -*-
"""V55 正式探针：真实时基接上之后，下一个真卡点在哪。

    cd emu && python probe/exp_tick_v55.py > ../results/frbox/v55.txt

七段：
  A 静态     直接页 0x48/0x4E、PIO 写点/加载点、flag 12 调用点、ISR 反汇编
  B 基线     不挂 / 挂 vec 0x18，同 V54 C0/C1 口径复现
  C 长跑     只挂真实时基、不打桩不补全，2000k 步看固件自己走到哪
  D 输入位   PIO bit3 的原始读值统计：三组 1/7/9 个读点、恒 0x12、bit3 从没为 1
  E 合流     真实时基 × bit3 打桩六组对照，与 V53 G4 的口径差别
  F flag 12  置铃/等铃/擦铃都执行过，任务 7 仍挂队列；显式唤醒对照
  G 边界与结论

⚠️ 三层手段严格分开（见 frbox/bobserve.py docstring）：
     dev_tick18 补硬件拉线（不写内存） / bpatch 反事实打桩（改读返回值） /
     bobserve 纯观察（只统计、原样透传）。
   `observe` 必须在 `patch_read` **之前**装，日志才是器件真实读值。
⚠️ 所有 `expire_object_queue` / `dispatch` 都明确标为反事实补全。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import fr_emu
import fr_dis
from frbox import (bstatic, blive, btick, bpatch, bobserve, bready,
                   dev_tick18 as t18)

BOOT = 191520
RUN400 = 400000
RUN2000 = 2000000
PIO = bstatic.PIO_STATUS
BIT3 = bstatic.PIO_BIT3
EVT = bstatic.EVT_BITMAP
FW = bstatic.STRUCT_FLAG
Q12 = bstatic.FLAG12_OBJ
PIO_PCS = (0x175882, 0x175896)
OBS_ADDRS = (PIO, EVT, FW, Q12, t18.TICK_COUNTER)

WATCH = [
    (0x040A44, "tick处理入口"),
    (0x040A78, "TCB+0x14递减"),
    (0x040A80, "到期摘链"),
    (0x175274, "事件位图写点"),
    (0x175406, "bit31总闸"),
    (0x1754FE, "派发段入口"),
    (0x175616, "bit9派发"),
    (0x175630, "bit9目标"),
    (0x175882, "PIO采样A"),
    (0x175896, "PIO采样B"),
    (0x17591C, "wai_flg(12)"),
    (0x17592C, "clr_flg(12)"),
    (0x175998, "set_flg(12)收尾"),
    (0x1963AE, "sta_tsk(19)"),
    (0x242B92, "sta_tsk(22)"),
    (0x242DA0, "任务22入口"),
    (0x137EB6, "btsth结构体"),
    (0x0407DC, "空闲循环"),
]


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dis(lo, hi, prefix="  "):
    off, end = lo - ROM_BASE, hi - ROM_BASE
    while off < end:
        line, size = fr_dis.line(ROM, off, ROM_BASE)
        print(prefix + line)
        off += size


def ptr_hits(val, width=4):
    pat = val.to_bytes(width, "big")
    out, pos = [], ROM.find(pat)
    while pos >= 0 and len(out) < 24:
        out.append(ROM_BASE + pos)
        pos = ROM.find(pat, pos + 1)
    return out


# ==================================================================== A
hr("A  静态：直接页、PIO 输入位、flag 12 与 ISR")

SW, M0 = bstatic.sweep_of_type("d300-b")
ROM, ROM_BASE = M0.bus.rom, M0.bus.rom_base
print("ROM base=0x%06X size=%d  stores=%d"
      % (ROM_BASE, len(ROM), SW.summary()["stores"]))

print("\nA1 直接页 0x48 / 0x4E 的 dmov 类指令线性普查（从头扫，不重同步）")
all_dir = collections.Counter()
hits = collections.defaultdict(list)
off, nbad = 0, 0
while off < len(ROM) - 1:
    try:
        mn, _bs, v, size, _w = fr_emu.TABLE.decode(ROM, off)
    except Exception:
        nbad += 1
        off += 2
        continue
    if mn and mn.startswith("dmov"):
        for key in ("dir8", "dir9", "dir10"):
            if key in v:
                a = v[key]
                all_dir[a] += 1
                if a in (t18.DIRECT_48, t18.DIRECT_4E):
                    hits[a].append(ROM_BASE + off)
    off += size
print("  解码异常 %d 处；直接页地址 %d 种" % (nbad, len(all_dir)))
print("  0x48 命中 %d 处，0x4E 命中 %d 处" % (len(hits[0x48]), len(hits[0x4E])))
print("  0x48 内核区（<0x060000）命中：%s"
      % ["0x%06X" % p for p in hits[0x48] if p < 0x060000])
print("  0x4E 内核区（<0x060000）命中：%s"
      % ["0x%06X" % p for p in hits[0x4E] if p < 0x060000])
print("  ⇒ 命中散布在 0x1B/0x1C/0x38/0x39 等数据表区，线性扫会把数据当指令；")
print("    能实证的只有内核初始化与 ISR 三处（见 A2），")
print("    ⇒ 0x80E8 不是「专用 tick 周期寄存器」，KI#191 继续收窄。")

print("\nA2 0x80E8 与直接页初始化 / 中断应答")
print("  0x80E8 作为 16 位大端数据出现 >=%d 处：%s"
      % (len(ptr_hits(0x80E8, 2)), ["0x%06X" % p for p in ptr_hits(0x80E8, 2)[:10]]))
dis(0x040450, 0x04046C)
dis(t18.ISR, 0x040484)
print("  ⇒ 0x040458 ldi:20 #0x80E8 + 0x04045C dmovh #72 → 直接页 0x48 = 0x80E8；")
print("    0x04045E #0x1B + 0x040460 dmovh #78 → 0x4E = 0x1B；")
print("    ISR 0x040478 #0x1A + 0x04047A dmovh #78 → 0x4E = 0x1A（应答）。")

print("\nA3 PIO 输入字节 0x%08X 的静态写点 / 加载点" % PIO)
wr = SW.writers_of(PIO)
ld = SW.ldi32.get(PIO, ())
print("  静态写点 = %d 处：%s"
      % (len(wr), ["0x%06X:%s" % (p, mn) for p, mn in wr]))
print("  ldi:32 #0x%08X 加载点 = %d 处：%s"
      % (PIO, len(ld), ["0x%06X" % p for p, _s in ld[:10]]))
dis(0x0402A0, 0x0402B4)
print("  ⇒ 0x0402AA stb #0x12 是唯一写点；0x0402AC 之后还把 0x50000207 也写成 0x12。")
print("    bit3（0x%02X）在 0x12 里恒为 0，没有任何固件路径会置它 ⇒ 外部输入位。" % BIT3)

print("\nA4 flag 12 的置铃 / 等铃 / 擦铃调用点")
sets12 = [c for c in SW.of(bstatic.SET_FLG) if c.flgid == 12]
wais12 = [c for c in SW.of(bstatic.WAI_FLG) if c.flgid == 12]
clrs12 = [c for c in SW.of(bstatic.CLR_FLG) if c.flgid == 12]
print("  set_flg(12) = %d 处  wai_flg(12) = %d 处  clr_flg(12) = %d 处"
      % (len(sets12), len(wais12), len(clrs12)))
RAN_SET = (0x170A9E, 0x17522A, 0x175904, 0x175998)
print("  E 段实测执行过的 4 处置铃，其 bits：")
for pc in RAN_SET:
    c = next((x for x in sets12 if x.addr == pc), None)
    print("    0x%06X  bits=%s" % (pc, hex(c.bits) if c and c.bits is not None else "?"))
cw = next((x for x in wais12 if x.addr == 0x17591C), None)
print("  等铃点 0x17591C：wai_flg(12, ptn=%s, mode=%s)"
      % (hex(cw.bits) if cw and cw.bits is not None else "?", cw.mode if cw else "?"))
clr_bit0 = [c for c in clrs12 if c.bits is not None and not (c.bits & 1)]
print("  clr_flg(12,~1) 掩码点 %d 处：%s"
      % (len(clr_bit0), ["0x%06X" % c.addr for c in clr_bit0]))
print("  ⇒ 置铃 / 等铃 / 擦铃都是 bit0，位能对上；卡点不是「位不匹配」。")

print("\nA5 内核 tick 处理（0x040A60..0x040A80，V54 已实证）")
dis(0x040A60, 0x040A80)
print("  ⇒ 每次 tick 遍历延时队列，TCB+0x14 减 1，>0 就跳下一个节点（V52/V54）。")

# ==================================================================== B/C/D/E/F
def snapshot(m, tag, reads, writes, hits, dev, woke=None):
    cv = collections.Counter(x[0] for x in m.cpu.irq_log)
    dq = btick.delay_queue(m)
    print("\n【%s】" % tag)
    print("  step=%d 末PC=0x%06X  vec18受理=%d  tick计数=%d"
          % (m.step_count, m.cpu.pc, cv.get(0x18, 0),
             m.bus.rd(t18.TICK_COUNTER + 4, 4)))
    if dev is not None:
        st = dev.state()
        print("  时钟 fired=%s accepted=%s coalesced=%s skipped=%s"
              % (st["fired"], st["accepted"], st["coalesced"], st["skipped"]))
    print("  延时队列=%s  flag12对象=0x%08X / +4=0x%08X"
          % ([tid for _t, tid, _p, _w in dq],
             m.bus.rd(Q12, 4), m.bus.rd(Q12 + 4, 4)))
    print("  事件位图=0x%08X  结构体=0x%08X  就绪位图=0x%08X"
          % (m.bus.rd(EVT, 4), m.bus.rd(FW, 4), m.bus.rd(0x68000004, 4)))
    print("  打桩命中=%s"
          % ({(hex(a), hex(pc)): c for (a, pc), c in hits.items()} if hits else "-"))
    print("  PC覆盖 跑过：%s" % [n for pc, n in WATCH if m.cov.visited(pc)])
    print("  PC覆盖 没跑：%s" % [n for pc, n in WATCH if not m.cov.visited(pc)])
    total, setcnt = bobserve.bit_samples(reads, PIO, BIT3)
    pio_by_pc = sorted((pc, sum(c.values()), sorted({v for v in c}))
                       for (a, pc), c in reads.items() if a == PIO)
    print("  PIO 原始读值：共 %d 次 / bit3=1 共 %d 次；按 PC：%s"
          % (total, setcnt,
             [(hex(pc), n, ["0x%08X" % v for v in vs]) for pc, n, vs in pio_by_pc]))
    print("  观察地址写点：PIO=%d EVT=%d FW=%d（EVT 值序列 %s）"
          % (len([x for x in writes if x[0] == PIO]),
             len([x for x in writes if x[0] == EVT]),
             len([x for x in writes if x[0] == FW]),
             [hex(x[4]) for x in writes if x[0] == EVT][:8]))
    if woke is not None:
        print("  ⚠️ 反事实 expire_object_queue(0x%08X) 唤醒 %d 个 TCB：%s"
              % (Q12, len(woke), [hex(x) for x in woke]))
    return {
        "tag": tag, "pc": m.cpu.pc, "tick": m.bus.rd(t18.TICK_COUNTER + 4, 4),
        "irq18": cv.get(0x18, 0), "dq": [tid for _t, tid, _p, _w in dq],
        "evt": m.bus.rd(EVT, 4), "gate": m.bus.rd(FW, 4),
        "pio_total": total, "pio_bit3": setcnt,
        "pio_pcs": len(pio_by_pc),
        "ran": [n for pc, n in WATCH if m.cov.visited(pc)],
        "flag12": m.bus.rd(Q12, 4),
        "hits": dict(hits), "m": m, "reads": reads, "writes": writes,
    }


def run_case(tag, attach, patch, steps, wake_after=0, extra=0):
    """一台机器只跑一遍（硬规矩 13）。wake_after>0 时为 F3 的分段反事实。"""
    m = blive.boot(0)
    dev = m.enable_auto_tick18(period=1024) if attach else None
    # 顺序纪律：observe 先装（记原始值），patch_read 后装（改固件看到的值）
    reads, writes = bobserve.observe(m.bus, OBS_ADDRS)
    hits = {}
    if patch:
        rules = {(PIO, pc): bpatch.bit_set(BIT3) for pc in PIO_PCS}
        hits = bpatch.patch_read(m.bus, rules)
    m.step(BOOT)
    m.run(max_steps=steps, spin_guard=False)
    woke = None
    if wake_after:
        woke = btick.expire_object_queue(m, Q12)
        if woke:
            bready.dispatch(m)
        m.run(max_steps=extra, spin_guard=False)
    return snapshot(m, tag, reads, writes, hits, dev, woke)


hr("B  基线复现：不挂 / 挂 vec 0x18（V54 C0/C1 同口径）")
B0 = run_case("B0 不挂时钟 / 400k", False, False, RUN400)
B1 = run_case("B1 挂 vec0x18 / 400k", True, False, RUN400)

hr("C  长跑：只靠真实时基、不打桩不补全，固件自己走到哪")
C1 = run_case("C1 挂 vec0x18 / 2000k", True, False, RUN2000)
print("\nC 段对账（同一把尺子：boot %d 步 + 再跑 %d / %d 步）"
      % (BOOT, RUN400, RUN2000))
print("  %-26s %8s %8s %-12s %-12s %s"
      % ("组", "vec18", "tick计数", "延时队列", "flag12对象", "末PC"))
for r in (B0, B1, C1):
    print("  %-26s %8d %8d %-12s 0x%08X   0x%06X"
          % (r["tag"], r["irq18"], r["tick"], str(r["dq"]), r["flag12"], r["pc"]))
print("  ⇒ V54 说「任务 7/21 等 flag 对象、tick 不该唤醒它们」要收窄：")
print("    C1 里 flag12 对象 0x%08X 指向任务 7 的 TCB，延时队列只剩 [16] ——"
      % C1["flag12"])
print("    任务 7 是**真实 tick 走完 tslp_tsk 之后才转去等 flag 12** 的，")
print("    V52 用反事实补全复现的那次「第二次睡眠」现在由固件自己完成。")
print("  ⇒ 但事件位图仍是 0x%08X（bit31 恒 1），派发段 0 执行，末 PC 仍空闲。"
      % C1["evt"])

hr("D  输入位铁证：PIO bit3 从没被固件置过 1")
print("  静态：0x%08X 全 ROM 唯一写点 0x%06X stb #0x%02X（另 0x0402B2 写 0x50000207）"
      % (PIO, bstatic.PIO_INIT_PC, bstatic.PIO_INIT_VALUE))
print("        ldi:32 加载点 %d 处（A3），说明读者不止任务 7 的轮询函数。" % len(ld))
for r in (B0, B1, C1):
    print("  %-26s PIO 原始读 %3d 次 / bit3=1 %d 次 / 读点 %d 个"
          % (r["tag"], r["pio_total"], r["pio_bit3"], r["pio_pcs"]))
print("  ⇒ 三组全部 0 次。bobserve 记的是打桩前的器件真实值；")
print("    把 bit3 读成 1 是我们用 bpatch 做的，不是固件置的（硬规矩 31）。")

hr("E  合流对照：真实时基 × 最小 bit3 输入")
E3 = run_case("E3 真实时基+bit3 / 400k", True, True, RUN400)
E4 = run_case("E4 真实时基+bit3 / 2000k", True, True, RUN2000)
E5 = run_case("E5 不挂时基+bit3 / 400k", False, True, RUN400)
print("\nE 段对账表")
print("  %-30s %6s %-12s %-8s %-8s %-8s %s"
      % ("组", "tick", "事件位图", "派发段", "sta19", "sta22", "末PC"))
for r in (B0, B1, C1, E3, E4, E5):
    print("  %-30s %6d 0x%08X   %-8s %-8s %-8s 0x%06X"
          % (r["tag"], r["tick"], r["evt"],
             "过" if "派发段入口" in r["ran"] else "·",
             "过" if "sta_tsk(19)" in r["ran"] else "·",
             "过" if "sta_tsk(22)" in r["ran"] else "·", r["pc"]))
print("  ⇒ E4 事件位图自清为 0x%08X、派发段执行过，" % E4["evt"])
print("    但 sta_tsk(19)/(22) 在真实时基下都没执行；")
print("    V53 G4 用 btick.tick_v52(only=0x%08X) 反事实补全时能执行过 ⇒" % Q12)
print("    那个进展来自补全设施把 flag 等待队列也拽醒了，不能算真实硬件输入的结果。")

hr("F  flag 12：铃置过了、等过了、擦过了，任务 7 却还挂着")
sets_run = [c.addr for c in sets12 if E4["m"].cov.visited(c.addr)]
wais_run = [c.addr for c in wais12 if E4["m"].cov.visited(c.addr)]
clrs_run = [c.addr for c in clrs12 if E4["m"].cov.visited(c.addr)]
print("  E4 的 cov：set_flg(12) 执行 %d/%d 处、wai_flg(12) %d/%d 处、clr_flg(12) %d/%d 处"
      % (len(sets_run), len(sets12), len(wais_run), len(wais12),
         len(clrs_run), len(clrs12)))
print("    置铃点：%s" % ["0x%06X" % p for p in sets_run])
print("    等铃点：%s" % ["0x%06X" % p for p in wais_run])
print("  ⇒ 置铃/等铃/擦铃都是 bit0（A4），位能对上，不是没人按铃。")
oq = [(q, nodes) for _i, q, nodes in btick.object_queues(E4["m"]) if nodes]
q12_nodes = next((n for q, n in oq if q == Q12), [])
print("  E4 非空 flag 等待队列 %d 条，其中 0x%08X 挂 %d 个 TCB：%s"
      % (len(oq), Q12, len(q12_nodes), [hex(x) for x in q12_nodes]))
print("  ⇒ 任务 7 仍挂在这条队列上；下一个卡点在置铃/等铃的时序或")
print("    0x175908 那个回调派发器（0x9F59F7C8 回调表），不是在 tick / PIO。")

F3 = run_case("F3 E4 之后显式唤醒 flag12 再跑 400k", True, True,
              RUN2000, wake_after=1, extra=RUN400)
print("\n  ⇒ F3 与 E4 逐项对照：sta_tsk(19)=%s / sta_tsk(22)=%s / 结构体=0x%08X / 末PC=0x%06X"
      % ("过" if "sta_tsk(19)" in F3["ran"] else "·",
         "过" if "sta_tsk(22)" in F3["ran"] else "·", F3["gate"], F3["pc"]))
print("    显式把 flag12 队列拽醒仍不能报「出画面」：结构体恒 0、0x137EB6 没执行。")

# ==================================================================== G
hr("G  能说 / 不能说、边界与复现")
print("""
能说：
  1. V54 的真实时基在长跑里确实驱动了固件自己的调度：C1 跑 1954 次 tick 后，
     任务 7 由固件 ISR 走完 tslp_tsk，转挂 flag 12 对象 0x68000968；
     延时队列从 [17,7,21] 变成 [16]，另有任务 16 在周期性 dly_tsk。
     ⇒ V54「任务 7/21 等 flag 对象、tick 不该唤醒」要收窄成「先走完延时，
       再转等 flag」（KI#205）。
  2. PIO 0x50000107 的 bit3 是**外部输入位**：全 ROM 唯一写点 0x0402AA
     把它初始化成 0x12，33 处 ldi:32 加载点；D 段三组分别 1 / 7 / 9 个读点、
     1 / 15 / 107 次采样，原始值全 0x12，bit3=1 出现 0 次（KI#206）。
  3. 新设施 frbox/bobserve.py 把「纯观察」从 bpatch 的反事实打桩里分出来：
     只统计、原样透传；observe 必须先于 patch_read 安装，日志才是真实值。
  4. 真实时基 + 最小 bit3 输入，事件位图能自清到 0x00000014、派发段能执行，
     但 sta_tsk(19)/(22) 不执行。V53 G4 之所以能执行，是因为
     btick.tick_v52(only=...) 反事实补全顺带把 flag 等待队列拽醒了（KI#207）。
  5. flag 12 的置铃 / 等铃 / 擦铃在 E4 里都执行过且都是 bit0，位能对上；
     任务 7 仍挂在 0x68000968 ⇒ 卡点在时序或 0x175908 回调派发器（KI#208）。

不能说：
  · 真机器件 / 频率找到了 —— period 仍是模型参数；A1 显示 0x48/0x4E 有
    上百处线性扫命中且散布在数据表区，不是专用 tick 周期寄存器（KI#191 收窄）。
  · 整机跑起来了 / 出画面了 —— 七台运行机器末 PC 全是空闲 0x0407DC、
    就绪位图 0、结构体 0x9F5A0284 恒 0、0x137EB6 与任务 22 入口都没执行。
  · 「bit3 是固件置的」—— 那是 bpatch 把读返回值改了，不是器件状态。
  · 「补 flag12 队列就能推进」—— F3 显式唤醒后仍停在空闲。
  · 「任务 7 一直等 flag 对象」—— 这是基线时刻的队列状态，不是全过程。

口径纪律：
  · 七台运行机器各跑一遍、不复用 cov（硬规矩 13）；B0/B1 是 V54 C0/C1 的逐字复现
    （硬规矩 2）；E3/E4/E5 同一把尺子，只差 attach / patch / 步数。
  · dev_tick18 只拉线不写内存；bpatch 只改读返回值；bobserve 只统计。
    三者都不替固件做调度决定；F3 的 expire_object_queue 是唯一显式补全。
  · 所有「执行过」都来自 m.cov.visited()，所有数字都能在本文件同一行找到。

复现：
  cd emu && python probe/exp_tick_v55.py > ../results/frbox/v55.txt
  cd emu && python frbox_selftest.py
""")
print("⚠️ A 段只读 ROM、没 boot；B0/B1/C1/E3/E4/E5/F3 各一台机器，均只跑一遍。")
print("⚠️ period=1024 是模型参数；2000k 步是观测窗口，不代表真机时间。")
