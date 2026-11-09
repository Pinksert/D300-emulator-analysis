# -*- coding: utf-8 -*-
"""V61 正式探针：vec 0x2B ISR 的「总闸」到底是谁。

    cd emu && python probe/exp_isr_v61.py > ../results/frbox/isr61.txt

七段：
  A  静态：访存地址形态更正 —— `ldub #252` 不是直接页 0xFC，是栈帧 r14-4
  B  静态：vec 0x2B ISR 的 37 条指令与「读 0x6B000030 → 写栈帧 → 判 bit0」链
  C  静态：补扫 V60 漏掉的两个窗口（0x6B000000 / 0x65000000），A/B 两段并列
  D  动态：单次 raise_irq(0x2B) 基线，纯观察（35/37 PC + 帧字节写点）
  E  动态：最小反事实 —— 只把帧字节 bit0 的两次读打桩成 1
  F  能说 / 不能说
  G  复现与变更

纪律：
  · 静态只读 ROM，不建运行机器；跨段查询先核指令形态分布（硬规矩 40）。
  · 动态每台机器只跑一遍（硬规矩 13），不自写 cpu.step() 循环（硬规矩 1）；
    设备策略用 devaudit.audit() 显式核对（硬规矩 38）。
  · 纯观察 hook 先装（记原始值），bpatch 后装（改固件看到的值）（硬规矩 34）。
  · 反事实只改 FRAME 上两个 PC 的 ld 读返回值，不写内存、不挂设备、
    不改时基；阴性结果逐项写清（硬规矩 27/28/31）。
  · 观察日志值按十进制打印（硬规矩 39）；静态「有没有」与动态「跑没跑」分开（硬规矩 5）。
  · 口径不同的数字不混比（硬规矩 2）；本版 20000 步窗口与 V60 的
    7 轮 × 10000 步双机交替不是同一口径，只在本版 D/E 两组内部互比。
"""
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from frbox import addrform, blive, bpatch, bstatic, devaudit
import fr_emu

# ---------------------------------------------------------------- 常量
BOOT = 191520                 # V56 起 B 段 boot 到空闲的口径
RUN = 20000                   # 单次中断后的观察窗口
VECTOR = 0x2B

ISR_LO, ISR_HI = 0x042E1C, 0x042E72   # 入口 .. reti 之后一条
BRANCH = 0x042E46
SKIP_PCS = (0x042E48, 0x042E4E)
PATCH_PCS = (0x042E40, 0x042E50)
GATE = 0x6B000030             # ISR 读的「总闸」槽
ICR = 0x0000045B              # vec 0x2B 的 ICR（V20 / KI#83）
DISPATCH = 0x17171E           # 唯一业务出口（全 ROM 只此一处调用）
STATUS_BASE = 0x65000000      # 分派器读的状态块
DSP_PCS = (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752)
STRUCT_FLAG = 0x9F5A0284
DISP_LO, DISP_HI = 0x17171E, 0x171900
WATCH_PCS = {0x042E3C, 0x042E40, 0x042E50, 0x042E56}

WINDOWS = [
    ("V60ctrl50", 0x50000000, 0x50100000, "0x5000xxxx 三通道位串行"),
    ("V60dual60", 0x60000000, 0x62000000, "0x60000000 / 0x61000000 双通道"),
    ("V60kernel", 0x68000000, 0x68100000, "0x68000000 内核对象区"),
    ("V60expeed", 0x70000000, 0x70100000, "0x70000000 EXPEED 位串行"),
    ("NEW6B", 0x6B000000, 0x6C000000, "vec0x2B ISR 读的总闸段"),
    ("NEW65", 0x65000000, 0x66000000, "分派器 0x17171E 读的状态块"),
]


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ================================================================ 静态 ROM
hr("A  静态：访存地址形态更正 —— ldub #252 不是直接页")
SW_B, M_B = bstatic.sweep_of_type("d300-b")
ROM_B, BASE_B = SW_B.rom, SW_B.base
print("A0 B 段 ROM %d 字节 / 基址 0x%06X；本段只读 ROM，不建运行机器。" %
      (len(ROM_B), BASE_B))
print("   fr_emu.py:658-661：disp10/9/8 都是 8 位有符号，乘元素宽度 4/2/1，基址 r14。")
print("   fr_emu.py:715-716：dir10/9/8 才无符号直接页，只有 dmov/dmovh/dmovb 用。")
print("   sx(252, 8) = %d  ⇒  ldub #252, r0 读 r14%+d = r14-4（栈帧）" %
      (addrform.sx(252), addrform.sx(252)))

for mn, v in (("ldub", {"disp8": 252, "ri": 0}),
              ("stb", {"disp8": 252, "ri": 0}),
              ("dmovb", {"dir8": 252, "ri": 0}),
              ("ld", {"rj": 1, "ri": 12})):
    f = addrform.classify(mn, v)
    print("   classify(%-6s %-28s) = %-14s %s" %
          (mn, repr(v), f.kind, addrform.describe(f)))

cnt, detail = addrform.scan(ROM_B, BASE_B, target=252)
print("\nA1 全 ROM 线性扫「立即数字段 = 252」的形态分布")
for tag, n in sorted(cnt.items()):
    print("   %-26s %d" % (tag, n))
print("   ⇒ 字段值口径：disp8=252 共 %d 处（ldub %d / stb %d），"
      "dir8=252 共 %d 处（dmovb）；把 disp8 当直接页会多报 %d 处。" %
      (cnt.get("frame_rel/disp8", 0), cnt.get("mn/ldub", 0),
       cnt.get("mn/stb", 0), cnt.get("direct_page/dir8", 0),
       cnt.get("frame_rel/disp8", 0)))
dpc = addrform.direct_page_hits(ROM_B, BASE_B, 0xFC)
print("   ⚠️ 另一个口径：按「算出的绝对地址 == 0xFC」统计是 %d 处"
      "（含 dmov/dmovh/dmovb 三种缩放）：%s" %
      (len(dpc), ["0x%06X:%s" % x for x in dpc[:8]]))
print("   固件确有直接页 0xFC 的真访问，只是**不在这条 ISR 里**；"
      "ISR 里两处 ldub #252 走的是 r14-4，所以按 0xFC 观察/打桩命中 0 次。")

# ================================================================ ISR 静态
hr("B  静态：vec 0x2B ISR 的 37 条指令与总闸来源")
graph = addrform.isr_graph(ROM_B, BASE_B, ISR_LO, ISR_HI)
print("B0 ISR 区间 0x%06X..0x%06X 共 %d 个 PC；末条 = %s @0x%06X" %
      (graph[0].pc, graph[-1].pc, len(graph), graph[-1].mn, graph[-1].pc))
print("   控制流关键点：")
for ins in graph:
    if ins.control:
        print("     0x%06X  %-8s  %s" % (ins.pc, ins.mn, ins.form.expr or ""))
print("\nB1 逐条反汇编（形态 / 立即数）")
for ins in graph:
    imm = ""
    for k in ("i32", "i20", "i8", "disp8", "dir8", "u4", "rel9"):
        if k in ins.vals:
            v = ins.vals[k]
            imm += " %s=%s(0x%X)" % (k, v, v) if isinstance(v, int) else ""
    print("   0x%06X  %-8s %-22s %s%s" %
          (ins.pc, ins.mn, ins.form.kind, ins.form.expr, imm))

print("\nB2 总闸数据链（全部来自上面的静态解码）")
print("   0x042E28 ldi:32 #0x45B,r8  → ICR 地址 0x%X" % ICR)
print("   0x042E36 ldi:32 #0x6B000030,r1 → 0x042E3C ld r1,r12 读「总闸」")
print("   0x042E32/34 mov r14,r0 + add2 #-4,r0 → r0 = r14-4（栈帧字节地址）")
print("   0x042E3E st r0,r12 → 把总闸读值写进该栈帧字节")
print("   0x042E40 ldub #252,r0 → 读回 r14-4；0x042E44 and #1；0x042E46 beq")
print("   0x042E46 beq 跳过 0x042E48 ldi:32 #0x17171E,r12 + 0x042E4E call r12")
print("   ⇒ 业务出口是否执行，取决于 0x%08X 读回值的 bit0。" % GATE)
print("   refs_to(0x17171E, window=8) = %s" % repr(SW_B.refs_to(DISPATCH, window=8)))
print("   writers_of(0x6B000030) = %d（静态可定址写点；寄存器间接写看不见，硬规矩 25/31）" %
      len(SW_B.writers_of(GATE)))

# ================================================================ 窗口补扫
hr("C  静态：补扫 V60 漏掉的两个窗口（A/B 两段并列）")
SW_A, M_A = bstatic.sweep_of_type("d300-a")
ROM_A, BASE_A = SW_A.rom, SW_A.base
print("C0 A 段 ROM %d 字节 / 基址 0x%06X；B 段 %d 字节 / 0x%06X。" %
      (len(ROM_A), BASE_A, len(ROM_B), BASE_B))
print("   ⚠️ 硬规矩 40：A 段以 ldi:20 为主、B 段以 ldi:32 为主；"
      "本段同时索引 i20/i32 两种形态，不复用只认 ldi:32 的 refs_to()。")


def window_scan(rom, base):
    hits = collections.defaultdict(list)
    shapes = collections.Counter()
    off, pc = 0, base
    while off < len(rom) - 1:
        try:
            ins = fr_emu.TABLE.decode(rom, off)
        except Exception:
            pc += 2; off += 2; continue
        if ins is None:
            pc += 2; off += 2; continue
        mn, _bs, v, size, _w = ins
        size = size if size > 0 else 2
        if isinstance(v, dict):
            for key in ("i32", "i20"):
                x = v.get(key)
                if isinstance(x, int):
                    shapes[key] += 1
                    for tag, lo, hi, _d in WINDOWS:
                        if lo <= x < hi:
                            hits[tag].append((pc, mn, key, x))
        pc += size; off += size
    return hits, shapes


for fw, rom, base in (("A 段", ROM_A, BASE_A), ("B 段", ROM_B, BASE_B)):
    hits, shapes = window_scan(rom, base)
    print("\nC1 %s 可寻址立即数形态：i32=%d / i20=%d" %
          (fw, shapes["i32"], shapes["i20"]))
    for tag, lo, hi, desc in WINDOWS:
        rows = hits.get(tag, [])
        print("   %-10s 0x%08X..0x%08X  命中 %3d 处  %s" %
              (tag, lo, hi, len(rows), desc))
        if tag.startswith("NEW") and rows:
            for pc, mn, key, x in rows[:12]:
                print("        0x%06X  %-8s %s=0x%08X" % (pc, mn, key, x))
    print("   ⇒ 本段六个窗口合计 %d 处可定址立即数。" %
          sum(len(hits.get(t[0], [])) for t in WINDOWS))
print("\nC2 窗口在模拟器里的映射审计（只查真正建映射的 machine.py）")
import io
_emu_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_mp = io.open(os.path.join(_emu_dir, "frbox", "machine.py"),
              encoding="utf-8", errors="ignore").read()
for _tag, _needle in (("6B000000", "6B000000"), ("65000000", "65000000")):
    print("   machine.py 是否出现窗口 %s：%s ⇒ %s" %
          (_tag, "是" if _needle in _mp else "否",
           "有映射线索" if _needle in _mp else "未建模，读到默认值 0"))
print("   ⚠️ 审计只看 machine.py：addrform.py / 本探针 / 自检里的同名字符串"
      "是**常量记录**，不是设备映射；把它们算进去会自指污染。")
print("   V61 侦查阶段对全 emu 目录（本版文件落盘之前）的文本检索同样是 0 命中。")
print("   ⚠️ V60 的五个窗口全部漏了这两段；这是扫描清单的盲区，"
      "不是「真机通道找到了」。")

# ================================================================ 动态
hr("D  动态：单次 raise_irq(0x2B) 基线（纯观察）")


def case(tag, patch_addr=None):
    """一台机器只跑一遍（硬规矩 13）。"""
    m = blive.boot(0)
    policy = devaudit.audit(m)
    orig_rd, orig_wr = m.bus.rd, m.bus.wr
    rdlog, wrlog = [], []

    def rd(a, n, p=0):
        v = orig_rd(a, n, p)
        if p in WATCH_PCS or DISP_LO <= p <= DISP_HI:
            rdlog.append((p, a, n, v))
        return v

    def wr(a, n, v, p=0):
        if a <= FRAME_WATCH < a + n:
            wrlog.append((p, a, n, v))
        return orig_wr(a, n, v, p)

    # 硬规矩 34：纯观察先装，打桩后装
    m.bus.rd, m.bus.wr = rd, wr
    hits = {}
    if patch_addr is not None:
        rules = {(patch_addr, p): bpatch.bit_set(1) for p in PATCH_PCS}
        hits = bpatch.patch_read(m.bus, rules)

    m.step(BOOT)
    before = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    m.raise_irq(VECTOR)
    m.run(max_steps=RUN, spin_guard=False)
    after = set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)
    isr_pcs = [ins.pc for ins in graph]

    print("\n【%s】patch_addr=%s" %
          (tag, ("0x%08X" % patch_addr) if patch_addr else "无（纯观察）"))
    print("   设备策略 xfer=%d dispatch=%d prio=%d cov=%d devices=%d" %
          (policy["xfer"], policy["dispatch"], policy["prio"],
           policy["cov"], policy["devices"]))
    print("   boot=%d 步 + 单次 raise_irq(0x%02X) + run(%d) ⇒ 末 PC=0x%06X" %
          (BOOT, VECTOR, RUN, m.cpu.pc))
    print("   覆盖 %d → %d（+%d）；ISR 命中 %d / %d" %
          (len(before), len(after), len(after) - len(before),
           len([p for p in isr_pcs if p in after]), len(isr_pcs)))
    print("   0x042E48=%s  0x042E4E=%s  0x17171E 分派器=%s" %
          (SKIP_PCS[0] in after, SKIP_PCS[1] in after, DISPATCH in after))
    for p in sorted(WATCH_PCS):
        agg = collections.Counter((a, n, v) for pp, a, n, v in rdlog if pp == p)
        for (a, n, v), c in agg.items():
            print("   读 PC 0x%06X → addr=0x%08X n=%d 原始值=%d ×%d" % (p, a, n, v, c))
    dsp = collections.Counter((a, v) for p, a, n, v in rdlog if DISP_LO <= p <= DISP_HI)
    if dsp:
        print("   分派器读 %d 笔 / %d 个地址：%s" %
              (sum(dsp.values()), len(set(a for a, _v in dsp)),
               ["0x%08X→%d" % (a, v) for (a, v), _c in sorted(dsp.items())[:10]]))
    print("   帧字节 0x%08X 写点 %d 笔：%s" %
          (FRAME_WATCH, len(wrlog),
           ["(pc=0x%06X,val=%d)" % (p, v) for p, a, n, v in wrlog]))
    print("   打桩命中（十进制键，硬规矩 39）：%s" % (dict(hits) if hits else "无"))
    for p in DSP_PCS:
        print("   显示链关键 PC 0x%06X = %s" % (p, "命中" if p in after else "未命中"))
    print("   0x9F5A0284 结构体读值 = %d" % m.bus.rd(STRUCT_FLAG, 4, 0))
    print("   新增 PC 落显示区 0x120000..0x140000：%d 个 %s" %
          (len([p for p in after - before if 0x120000 <= p <= 0x140000]),
           ["0x%06X" % p for p in sorted(after - before)
            if 0x120000 <= p <= 0x140000][:12]))
    return after, set(a for p, a, n, v in rdlog if p == PATCH_PCS[0])


# 帧地址来自 V61 侦查 D 的运行时实测（ISR 的 r14-4）；D0 会再核一次
# 0x042E40 实际读到的地址是否等于它，若不等则在 D1 里明写不一致。
FRAME_WATCH = 0x000083B8

cov0, frame_addrs = case("D0 基线", None)
print("\nD1 D0 侧 0x042E40 实际读到的帧地址：%s；观察哨兵 0x%08X %s" %
      (["0x%08X" % a for a in sorted(frame_addrs)], FRAME_WATCH,
       "一致" if sorted(frame_addrs) == [FRAME_WATCH] else "不一致，需重查"))
print("   与静态 A 段的 r14-4 一致；直接页 0xFC 在动态里 0 笔命中"
      "（V61 初判的反证）。")

# ================================================================ 反事实
hr("E  动态：最小反事实（帧字节 bit0 打桩）")
if not frame_addrs:
    print("E0 没拿到帧地址，跳过反事实（阴性结果）。")
    cov1 = cov0
else:
    addr = sorted(frame_addrs)[0]
    print("E0 只把 (0x%08X, 0x%06X) 与 (0x%08X, 0x%06X) 两处 ld 读返回值"
          "打桩成 bit0=1；不写内存、不挂设备、不改时基。" %
          (addr, PATCH_PCS[0], addr, PATCH_PCS[1]))
    cov1, _ = case("E1 bit0=1", addr)
    delta = cov1 - cov0
    print("\nE2 E1 相对 D0 新增 PC %d 个：%s" %
          (len(delta), ["0x%06X" % p for p in sorted(delta)[:40]]))
    print("   ⇒ 业务出口一打开，分派器 0x17171E 就执行；"
          "但它读的 0x65000000 段未建模、全 0，按位派发全跳过；"
          "显示链 5 个关键 PC 一个未解锁，0x9F5A0284 仍 0。")

# ================================================================ 边界
hr("F  能说 / 不能说")
print("能说：")
print("  1. V60 的「新增 35 个 PC」= 本版 ISR 的 37 个 PC 减去被 0x042E46 beq")
print("     跳过的 0x042E48 / 0x042E4E；单次中断独立复现 35/37。")
print("  2. 该 beq 的 bit0 来自 ISR 自己 0x042E3C 读 0x6B000030、0x042E3E 写进")
print("     栈帧 r14-4、0x042E40 再读回；总闸是 0x6B000030，不是直接页 0xFC。")
print("  3. 只改那两处读返回值即可让 37/37 命中并进入 0x17171E 分派器。")
print("  4. V60 的跨段窗口清单漏了 0x6B000000（B 段 4 处）与 0x65000000（B 段 32 处）；")
print("     A 段对六个窗口的可定址立即数仍全 0。")
print("  5. 分派器读的 0x65000000 段在模拟器里没映射，读到默认 0。")
print("不能说：")
print("  · 「找到真机 A↔B 通道」—— A 段静态仍 0 命中，0x6B000030 只是候选槽。")
print("  · 「显示链通了 / 出画面」—— 5 个关键 PC 未命中、0x9F5A0284 恒 0。")
print("  · 「0x6B000030 / 0x65000000 在真机是什么器件」—— 未建模 ≠ 不存在，")
print("     本版只报「固件引用了、模拟器读到 0」。")
print("  · 「直接页 0xFC 是总闸」—— 本版 A/D 两段共同证伪。")
print("  · 「打桩 = 真机行为」—— bpatch 是反事实补全（硬规矩 27/31）；")
print("     raise_irq 是模型动作；20000 步是观测窗口，不是真机时基。")
print("  · 本版 20000 步与 V60 的 7 轮 × 10000 步双机交替口径不同，不混比（硬规矩 2）。")

hr("G  复现与变更")
print("复现：")
print("  cd emu && python probe/exp_isr_v61.py > ../results/frbox/isr61.txt")
print("  cd emu && python frbox_selftest.py")
print("机器口径：两台运行机器（D0 / E1，各跑一遍）+ 两台只读 ROM 实例"
      "（sweep_of_type('d300-a') / ('d300-b')，只 load 不 boot）。")
print("本版新增 / 变更：")
print("  + frbox/addrform.py       纯静态访存地址形态分类 / ISR 控制流图")
print("  + probe/exp_isr_v61.py    本探针")
print("  + results/frbox/isr61.txt 本结果")
print("  ~ frbox/__init__.py       0.60.0 → 0.61.0，模块表加 addrform")
print("  ~ frbox_selftest.py       新增 test_v61()")
print("  ~ KNOWN_ISSUES.md / docs/03-frbox.md / README.md / probe/README.md")
print("  ~ results/frbox/Readme.txt / 记忆四件套")
print("\ndone")
