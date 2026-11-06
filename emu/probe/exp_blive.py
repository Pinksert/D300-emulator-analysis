# -*- coding: utf-8 -*-
"""V40 探针：整机自跑 —— 派发请求由固件自己写，模型一句 raise_irq 都不加。

A. boot 末的现场：一条待决派发、ICR 0x46F、设备计数
B. 0x44 是锁存器：boot 里两次写 1 的来路（0x41886），重叠置位不重复挂中断
C. 0x44 会被读：写后哑读（0x41888 / 0x40EAE）—— 修正 V11「从不被读」
D. ICR 0x46F 的身份：48 条 ICR 里唯一不被 init 循环写的一条
E. 推迟铁律：服务里 ICR=0x1E + ILM=0x1E 挡住 → reti 后 ILM=0x1F 才受理
F. 整机自跑 40 万步：24 次换手、9 个任务、显示初始化在 294,388 步
G. 确定性：两次独立运行换手表逐条相同
H. kernel_call 为什么不能和 dispatch=True 一起用（V39 的 80,000 步）
I. 边界：跑到 513,781 步被任务 9 一条宽写清零 0x46C..0x46F，之后不再派发
J. 旁证：锁存语义让 A 段首切从 43,622 提前到 43,540（−82），其余不变
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox import bready, blive, brealos
from frbox.machine import create


def hr(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def dis(m, a0, a1, tag=""):
    if tag:
        print("  -- %s" % tag)
    c = m.cpu
    a = a0
    while a < a1:
        ins = c.fetch(a)
        if ins is None:
            print("    0x%08X  <?>" % a)
            a += 2
            continue
        mn, base, vals, size, w = ins
        print("    0x%08X  %-7s %s" % (a, mn, {k: hex(v) for k, v in vals.items()}))
        a += size


# ---------------------------------------------------------------- A
hr("A. boot 末的现场：一条待决派发")
m = blive.boot()
print("  步数 = %d，当前 = %s" % (m.step_count, blive.name(m)))
print("  就绪位图 = 0x%08X" % m.bus.rd(blive.READY_MAP, 4))
print("  ICR 0x46F（vec 0x3F）= 0x%02X" % blive.icr(m))
print("  CPU 待决派发 = %d 条，irq_pending = %s"
      % (blive.pending_dispatch(m), m.cpu.irq_pending))
print("  设备: %s" % m.dispatch.state())
print("  → boot 全程 i=0、ILM=0x10（整个 boot 只变过 6 次），")
print("    所以 boot 里那两次写 1 挂上的中断一直躺在 CPU 待决队列里等开中断 ——")
print("    **这就是 V39 之前必须人工补一次 vec 0x3F 的全部原因**。")

# ---------------------------------------------------------------- B
hr("B. 0x44 是锁存器：boot 里那两次写")
boot_req = []
m2 = create("d300-b", dispatch=True)
m2.load(); m2.reset()
c2 = m2.cpu
base_wr_2 = m2.bus.wr


def spy_b(a, n, v, pc=0):
    if a == blive.DISPATCH_REG and (v & 0xFF):
        boot_req.append((m2.step_count, pc, m2.bus.rd(blive.DISPATCH_ICR, 1), c2.i))
    return base_wr_2(a, n, v, pc)


m2.bus.wr = spy_b
n = 0
while not c2.halted and n < blive.BOOT_STEPS:
    m2.step(1)
    n += 1
m2.bus.wr = base_wr_2
print("  boot 期间写 0x44=1 共 %d 次：" % len(boot_req))
for s, pc, icr_v, iv in boot_req:
    print("    步 %7d  pc=0x%08X  ICR=0x%02X  i=%d" % (s, pc, icr_v, iv))
print("  设备计数: %s" % m2.dispatch.state())
print("  → 两次写只挂上 1 条中断（锁存位已置，重复置位不再挂）——")
print("    旧模型会挂 2 条，多出来的那条会在下一次开中断时被当成真请求。")
dis(m2, 0x41876, 0x4188A, "写 1 的位置（sta_tsk 尾部：置位图 → 请求派发）")
print("  → 0x41888 是 `dmovb @0x44 -> R13`：写完立刻读回一次（见 C 段）。")

# ---------------------------------------------------------------- C
hr("C. 0x44 会被读：写后哑读（修正 V11 的「固件里从头到尾没有任何一处读它」）")
reads, writes = [], []


def rd_spy(a, n, pc=0):
    if a == blive.DISPATCH_REG:
        reads.append((m2.step_count, pc, base_rd_2(a, n, pc) & 0xFF))
    return base_rd_2(a, n, pc)


def wr_spy(a, n, v, pc=0):
    if a == blive.DISPATCH_REG:
        writes.append((m2.step_count, pc, v & 0xFF))
    return base_wr_2(a, n, v, pc)


base_rd_2, base_wr_2 = m2.bus.rd, m2.bus.wr
m2.bus.rd, m2.bus.wr = rd_spy, wr_spy
m2.run(max_steps=300000, spin_guard=False)
m2.bus.rd, m2.bus.wr = base_rd_2, base_wr_2
print("  30 万步：写 %d 次 / 读 %d 次" % (len(writes), len(reads)))
for s in reads[:6]:
    print("    读 步 %7d pc=0x%08X → 0x%X" % s)
print("  读点只有两个：0x41888（写 1 之后）、0x40EAE（写 0 之后），")
print("  两条后面紧跟 ret:d，读回值被丢弃 → **写后哑读**，不是读状态。")
print("  但读回值本身说明了它是锁存器：写 1 之后读回 1，写 0 之后读回 0。")

# ---------------------------------------------------------------- D
hr("D. ICR 0x46F 的身份：48 条 ICR 里唯一不被 init 循环写的一条")
icr_writes = []
base_wr_3 = None


def spy_d(a, n, v, pc=0):
    if blive.ICR_BASE <= a < blive.ICR_BASE + blive.ICR_COUNT:
        icr_writes.append((a, v & 0xFF, pc))
    return base_wr_3(a, n, v, pc)


m3 = create("d300-b", dispatch=True)
m3.load(); m3.reset()
base_wr_3 = m3.bus.wr
m3.bus.wr = spy_d
n = 0
while not m3.cpu.halted and n < blive.BOOT_STEPS:
    m3.step(1)
    n += 1
m3.bus.wr = base_wr_3
addrs = sorted({a for a, _v, _p in icr_writes})
loop = sorted({a for a, _v, pc in icr_writes if 0x120900 <= pc < 0x121000})
other = [(a, v, pc) for a, v, pc in icr_writes if not (0x120900 <= pc < 0x121000)]
allw = sorted(set(addrs))
print("  init 阶段一共写过 %d 条 ICR（0x%X..0x%X）" % (len(allw), allw[0], allw[-1]))
print("  其中 task-2 的那个循环（pc 0x1209xx–0x120Cxx）写了 %d 条：0x%X..0x%X"
      % (len(loop), loop[0], loop[-1]))
print("  循环没写、由别处补的：%s"
      % ", ".join("0x%X←0x%02X @0x%08X" % (a, v, pc) for a, v, pc in other))
print("  0x46F 从头到尾不在这个循环里 —— 它的写点只有：")
for a, v, pc in other:
    if a == blive.DISPATCH_ICR:
        print("    0x46F ← 0x%02X @0x%08X" % (v, pc))
dis(m3, 0x40866, 0x40872, "boot 里设 ICR(0x3F)=0x10 的那三句（0x440 + 0x2F）")
dis(m3, 0x40694, 0x4069E, "服务入口：ICR(0x3F) ← 0x1E")
dis(m3, 0x406CE, 0x406D8, "服务出口：ICR(0x3F) ← 0x10")
print("  → 0x440 + 0x2F 正好是 vec 0x3F 的 ICR（索引 = vec − 0x10）。")
print("    它不在 init 循环里，是被内核服务自己当成「派发器优先级」来管的。")

# ---------------------------------------------------------------- E
hr("E. 推迟铁律：服务里 ICR=0x1E + ILM=0x1E 挡住，reti 后 ILM=0x1F 才受理")
m = blive.boot()
res = blive.run(m, steps=120000)
rok, rbad, dok, dbad = res["relay"]
print("  12 万步：REQ=%d ACK=%d DISP=%d SW=%d" % tuple(
    res["counts"][k] for k in ("REQ", "ACK", "DISP", "SW")))
print("  铁律核对：REQ 里 ICR=0x1E 且 ILM=0x1E 的 %d 条 / 不符合 %d 条" % (rok, rbad))
print("            DISP 里 ILM=0x1F 的 %d 条 / 不符合 %d 条" % (dok, dbad))
for s, k, t in res["events"][:10]:
    print("    %8d %-4s %s" % (s, k, t))
print("  → 请求写在服务里（ILM=0x1E），派发在 reti 之后（ILM=0x1F）：")
print("    这就是 V39 KI#161 问的「受理时机」，答案是 0x46F 那条 ICR。")

# ---------------------------------------------------------------- F
hr("F. 整机自跑 40 万步：不补任何中断")
m = blive.boot()
res = blive.run(m, steps=400000)
print("  跑到步数 %d，当前 %s，位图 0x%08X"
      % (res["steps"], res["current_name"], res["ready_map"]))
print("  事件计数: %s" % res["counts"])
print("  设备计数: %s" % res["device"])
print("  换手 %d 次：" % len(res["switches"]))
for s, t in res["switches"]:
    print("    %8d → %s (0x%08X)" % (s, blive.name(m, t), t))
print("  入口执行过的任务: %s" % ", ".join("任务%d" % t for t in res["tasks"]))
print("  显示初始化第一笔写（哨兵 0x9F58D500）在步 %s" % res["display_init"])
print("  槽位 +8 = %s" % [m.bus.rd(blive.SLOT_BASE + 8 + i * 12, 1) for i in range(5)])
print("  0x9F58DA85 标志 = %d" % m.bus.rd(0x9F58DA85, 1))
print("  → 与 V39 的区别：V39 是 `run_until()` 在任务阻塞时**人工补一次 0x3F**，")
print("    这里是内核自己写 0x44、自己受理，一句 raise_irq 都没有。")

# ---------------------------------------------------------------- G
hr("G. 确定性：两次独立运行，换手表逐条相同")
m = blive.boot()
r1 = blive.run(m, steps=120000)
m = blive.boot()
r2 = blive.run(m, steps=120000)
print("  第一次 %d 次换手、第二次 %d 次换手，完全一致 = %s"
      % (len(r1["switches"]), len(r2["switches"]), r1["switches"] == r2["switches"]))
print("  事件序列完全一致 = %s" % (r1["events"] == r2["events"]))

# ---------------------------------------------------------------- H
hr("H. kernel_call 不能和 dispatch=True 一起用")
print("  kernel_call 的做法是「保存整份 CPU 现场 → eit(0x40) → 跑 → 恢复现场」。")
print("  可派发一旦真的生效，服务里就会**换任务**：恢复现场等于把换手结果盖回去。")
m = create("d300-b", dispatch=True)
m.load(); m.reset(); m.enable_cov()
n = 0
while not m.cpu.halted and n < blive.BOOT_STEPS:
    m.step(1)
    n += 1
before = m.bus.rd(blive.READY_MAP, 4)
r = brealos.sta_tsk(m, bready.TASK5_ID)
after = m.bus.rd(blive.READY_MAP, 4)
print("  sta_tsk(5) 返回 %s" % r)
print("  位图 0x%08X → 0x%08X（位 28 = 任务 5 的那一位）" % (before, after))
print("  ICR 0x46F 现在 = 0x%02X，当前 = %s"
      % (m.bus.rd(blive.DISPATCH_ICR, 1), blive.name(m)))
print("  → 8 万步上限用完也没回到 SAFE_PC，任务 5 的位没留在位图里。")
print("    这是 V39 KI#161 的真实原因：**不是 ICR 时机，是调用方法**。")

# ---------------------------------------------------------------- I
hr("I. 边界：为什么只跑到 51.5 万步")
m = blive.boot()
wide = []
base_wr_4 = m.bus.wr


def spy_i(a, n, v, pc=0):
    if a <= blive.DISPATCH_ICR < a + n and a != blive.DISPATCH_ICR:
        wide.append((m.step_count, pc, a, n, v))
    return base_wr_4(a, n, v, pc)


m.bus.wr = spy_i
res = blive.run(m, steps=340000)
m.bus.wr = base_wr_4
print("  跑到步数 %d，当前 %s" % (m.step_count, blive.name(m)))
print("  覆盖 0x46F 的宽写 %d 条：" % len(wide))
for s, pc, a, n, v in wide:
    print("    步 %d  pc=0x%08X  st 0x%X n=%d ← 0x%X" % (s, pc, a, n, v))
print("  ICR 0x46C..0x46F 现在是: %s"
      % " ".join("%02X" % m.bus.rd(0x46C + i, 1) for i in range(4)))
last_req = [e for e in res["events"] if e[1] in ("REQ", "DISP")]
if last_req:
    s, k, t = last_req[-1]
    print("  最后一对事件：%d %s %s" % (s, k, t))
print("  → 任务 9 用一条 4 字节写把 vec 0x3C..0x3F 的 ICR 清零，")
print("    派发器的级别变成 0 → 请求按 0x1F 挂上，永远不满足 level < ILM，")
print("    于是整机停在最后一次请求之后不再换手。**不能报「系统跑起来了」。**")
print("    能报的是：从 boot 到 51.5 万步，派发完全由固件自己驱动。")

hr("J. 锁存语义顺手改动的一处旁证：A 段首切步数 −82")
from frbox import dev_dispatch as _dd

_NEW_WRITE = _dd.DispatchRequest.write


def _legacy_write(self, off, size, val):
    """V39 及以前的语义：写 1 每次都挂一条中断，写 0 什么都不做。"""
    v = val & 0xFF
    mm = self.machine
    if not v:
        self.acks += 1
        return
    self.requests += 1
    self.latch = 1
    lvl = mm.irq_level(self.vec) or 0x1F
    mm.raise_irq(self.vec, lvl)
    self.raised += 1
    self.last_level = lvl


def _a_seg():
    ma = create("d300-a")
    ma.load()
    fa = ma.attach_flow()
    ma.boot_realos(1)
    boot_dev = (ma.dispatch.requests, ma.dispatch.raised)
    n = 0
    while not ma.cpu.halted and n < 200000:
        ma.step(1)
        n += 1
    first_set = [c for c in fa.calls if c[2] == -48]
    return (fa.switches[0][0], len(fa.switches), len(fa.calls),
            ma.step_count, first_set[0][0] if first_set else None, boot_dev)


_dd.DispatchRequest.write = _legacy_write
old = _a_seg()
_dd.DispatchRequest.write = _NEW_WRITE
new = _a_seg()
print("  A 段（d300-a，boot_realos(1) 跑到死锁）")
print("  %-26s %10s %10s" % ("", "V39 旧语义", "V40 锁存"))
print("  %-26s %10d %10d" % ("boot 里写 1 的次数", old[5][0], new[5][0]))
print("  %-26s %10d %10d" % ("其中真正挂上中断（次）", old[5][1], new[5][1]))
print("  %-26s %10d %10d" % ("第一次任务切换（步）", old[0], new[0]))
print("  %-26s %10d %10d" % ("第一次 set_flg（步）", old[4], new[4]))
print("  %-26s %10d %10d" % ("切换次数", old[1], new[1]))
print("  %-26s %10d %10d" % ("服务调用次数", old[2], new[2]))
print("  %-26s %10d %10d" % ("跑到死锁的总步数", old[3], new[3]))
print("  → boot 里那 3 次写 1 在旧语义下挂 3 条中断、新语义只挂 1 条；")
print("    多出来的两次服务不再排在前面，首切和首次 set_flg 各提前 82 步。")
print("    除此两个时间点，切换次数/服务次数/总步数/末 PC 全部不变 ——")
print("    这不是功能回退，是「重复写 1 不该变成两条请求」的必然结果。")

hr("结论")
print("""
1. 0x44 是**锁存器**：写 1 置位挂中断、写 0 清位并撤销待决项、读回锁存值。
   V11「固件从不读它」作废（写后哑读 2 处），V39 的「只置不清」补齐。
2. 0x46F = vec 0x3F 的 ICR，48 条里唯一不被 init 循环写的一条；内核服务入口
   抬到 0x1E、出口写回 0x10。服务期间 ILM=0x1E 正好挡住 0x1E 的派发请求，
   reti 之后 ILM=0x1F 才受理 —— V39 KI#161「受理时机」结案。
3. 打开 dispatch=True 之后整机**自己跑**：boot 后第一件事就是派发、换到任务 2，
   任务 2 把 3/4/5/6/7/9/10/11 依次挂起来，任务 5 上 CPU 后显示初始化自己跑完
   （294,388 步）。40 万步 24 次换手，两次运行逐条一致。
4. kernel_call 与 dispatch 不兼容（保存/恢复现场 vs 任务切换），V39 那条
   「sta_tsk 不返回、位图丢任务 5」是调用方法造成的，不是内核没请求派发。
5. 边界：51.5 万步时任务 9 一条 4 字节写清掉 ICR 0x46C..0x46F，派发器从此
   挂不上请求，整机停在那里。下一版要接的是「谁、在什么条件下清这批 ICR」。
6. 锁存的副作用已量化：A 段 boot 里 3 次写 1 由「挂 3 条」变成「挂 1 条」，
   首切 43,622 → 43,540、首次 set_flg 44,073 → 43,991（各 −82），其余不变。
   自检里这两条期望值按新语义换基线（见 J 段）。
""")
