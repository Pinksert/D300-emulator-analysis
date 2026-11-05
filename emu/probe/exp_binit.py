# -*- coding: utf-8 -*-
"""exp_binit.py —— V38：显示初始化 0x12E006 第一次完整跑通。

A. 载荷 +8 的真身：清空函数里的编号循环（静态 + 70/70 动态）
B. 0x12E006 完整跑通：746 步、151 笔写、五件副作用
C. 调用者链：0x12DFB8 → 0x12E006，而 0x12DFAE 是任务 5 的入口
D. 任务 5 起不来：sta_tsk(5) 改了 TCB，就绪位图没变
E. 方法学：`ret` 读 DR1 不读栈 —— 旧探针的「跑完」是假返回
F. 阻塞型服务在非任务上下文里的表现（返回值 / 卡死）
G. 取首的真实寄存器语义：R4 = is_empty，R5 = 载荷
H. 边界

跑法：cd emu && python probe/exp_binit.py > ../results/frbox/binit.txt
"""
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fr_emu
from frbox import brealos, binit, blist, bqueue, bdesc

BASE = 0x40000
ROM_PATH = os.path.join(os.path.dirname(_ROOT), "data", "B3000111.bin")
ROM = open(ROM_PATH, "rb").read()

RET = 0x00407DC
SCR = 0x9F83F000


def sx(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def hr(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------- A
hr("A. 载荷 +8 的真身：清空函数 0x12E602 里的编号循环")

m = brealos.boot()
print("静态（0x12E602 全段）：")
print(m.disasm_at(0x12E612, 8))
print()
print("  r5 = 槽位基址 0x9F58D508、r4 = 0、r13 = 8；循环体是")
print("  `stb r4,@(r13,r5)`（0x12E61C）→ `add r4,#1` → `cmp r4,#70` → `blt` →")
print("  `add r5,#0x0C`。所以 **+8 = 槽位自己的编号**。")
print()
print("  ⚠️ V35 判「没人写 +8」是因为静态只找『立即数 0x9F58D508 的引用』，")
print("     而这个循环的基址在 r5 里 —— `stb @(r13,r5)` 用的是寄存器偏移。")

log = []


def spy(addr, sz, val, pc=None):
    log.append((pc, addr, sz, val))
    return base_wr(addr, sz, val, pc)


base_wr = m.bus.wr
m.bus.wr = spy
r = brealos.call_fn(m, binit.QUEUE_INIT_FN)
m.bus.wr = base_wr
pay = [(a, v) for _pc, a, sz, v in log
       if binit.SLOT_BASE <= a < binit.SLOT_BASE + 70 * binit.SLOT_STRIDE
       and (a - binit.SLOT_BASE) % binit.SLOT_STRIDE == 8]
print()
print("动态（只跑 0x12E602，%d 步返回=%s）：槽位 +8 写入 %d 笔" % (r["steps"], r["ret"], len(pay)))
ok = all(v == (a - binit.SLOT_BASE - 8) // binit.SLOT_STRIDE for a, v in pay)
print("  70/70 且值 = 索引：%s" % ok)
print("  抽样：" + "，".join("槽%d=%d" % ((a - binit.SLOT_BASE - 8) // 12, v) for a, v in pay[:5]))
print("  写入者 = 0x%08X（`stb` 本身；首笔落在 0x%08X）"
      % (pay[0][0] if pay else 0, pay[0][1] if pay else 0))
print()
print("  末尾还发了一个事件：0x12E630 set_flg(3, 0x80) —— 「队列初始化好了」")
print("  最后 0x12E634 ret。")


# ---------------------------------------------------------------- B
hr("B. 显示初始化 0x12E006 第一次完整跑通")

m = brealos.boot()
print("boot 之后（空闲任务）：")
print("  " + binit.describe(m))
print("  队列哨兵 head   = 0x%08X" % m.bus.rd(binit.QUEUE_HEAD, 4))
print("  槽位 69 的 +8   = %d" % binit.slot_index(m, 69))
print("  0x9F58DA85      = %d" % m.bus.rd(binit.INIT_FLAG, 1))
print()
res = binit.run(m)
print("调用 0x12E006（返回地址设 DR1 = 0x%08X）：" % RET)
print("  %d 步、pc = 0x%08X、正常返回 = %s、写入 %d 笔"
      % (res["steps"], res["pc"], res["ret"], len(res["writes"])))
print()
print("跑完之后：")
print("  " + binit.describe(m))
print("  队列哨兵        = 0x%08X / 0x%08X（自指 ✓）"
      % (m.bus.rd(binit.QUEUE_HEAD, 4), m.bus.rd(binit.QUEUE_HEAD + 4, 4)))
print("  70 个槽位的 +8  = %s ... %s"
      % (binit.payload_map(m)[:8], binit.payload_map(m)[-3:]))
print("  0x9F58DA85      = %d（初始化标志）" % m.bus.rd(binit.INIT_FLAG, 1))
print("  0x5C003008 写入 0x0002039F，读回 0x%02X（该设备只保留低字节）"
      % m.bus.rd(binit.HW_PERIOD, 1))
print()
print("副作用按区段：")
cnt = Counter()
for _pc, a, _sz, _v in res["writes"]:
    if binit.SLOT_BASE <= a < binit.SLOT_BASE + 70 * binit.SLOT_STRIDE:
        cnt["槽位区（+8 编号 ×70）"] += 1
    elif 0x9F58D000 <= a < 0x9F58E400:
        cnt["其他显示区"] += 1
    elif 0x5C000000 <= a < 0x5D000000:
        cnt["外设 0x5C00（硬件定时器）"] += 1
    elif a < 0x1000:
        cnt["direct page 寄存器"] += 1
    elif a < 0x40000:
        cnt["低区 RAM（陷阱帧 / 系统栈）"] += 1
    elif 0x68000000 <= a < 0x69000000:
        cnt["内核对象 0x6800"] += 1
    elif 0x9F000000 <= a < 0xA0000000:
        cnt["任务栈 9F8x（保存寄存器）"] += 1
    else:
        cnt["其他"] += 1
for k, v in cnt.most_common():
    print("  %-26s %d" % (k, v))
print()
print("direct page 那几笔（外设寄存器）：")
for _pc, a, sz, v in res["writes"]:
    if a < 0x1000:
        print("    0x%08X 写 %d 字节 = 0x%X" % (a, sz, v))
print()
print("五件副作用：")
print("  ① 队列哨兵自指      0x124F48 / 0x124F4E")
print("  ② 70 个槽位编号     0x12E61C（0..69）")
print("  ③ set_flg(3, 0x80)  0x12E630")
print("  ④ 硬件定时器        0x5C003000 = 0、0x5C003008 = 0x0002039F")
print("  ⑤ 初始化标志        0x9F58DA85 = 1")


# ---------------------------------------------------------------- C
hr("C. 调用者链：任务 5 入口 → 0x12E006")

calls = []
for off in range(0, len(ROM) - 2, 2):
    d = fr_emu.TABLE.decode(ROM, off)
    if d is None:
        continue
    mn, _b, v, _size, _w = d
    if mn in ("call", "call:d") and "rel12" in v:
        pc = BASE + off
        calls.append((pc, (pc + 2 + sx(v["rel12"], 11) * 2) & 0xFFFFFFFF))
print("全片 call 指令 %d 条" % len(calls))
for t, name in ((binit.INIT_FN, "0x12E006 显示初始化"),
                (binit.TASK_ENTRY, "0x12DFAE 任务 5 入口")):
    hits = [pc for pc, tg in calls if tg == t]
    print("  %-24s ← %d 处 %s" % (name, len(hits), " ".join("0x%X" % x for x in hits)))
print()
print("0x12DFAE 自己不是被 call 进来的 —— 它是任务入口。任务表 0x54C00 记录 4：")
rec = 0x54C00 + 4 * 0x1C
print("  @0x%08X  %s" % (rec, m.fetch_bytes(rec, 0x1C).hex(" ")))
print("    +0x00 TCB   = 0x%08X" % m.bus.rd(rec, 4))
print("    +0x04 编号  = 0x%08X" % m.bus.rd(rec + 4, 4))
print("    +0x0C flags = 0x%08X" % m.bus.rd(rec + 0x0C, 4))
print("    +0x10 **入口** = 0x%08X" % m.bus.rd(rec + 0x10, 4))
print("    +0x14 栈顶  = 0x%08X" % m.bus.rd(rec + 0x14, 4))
print()
print("⚠️ 记录 0 = 任务 1，所以记录 4 = **任务 5**。0x12DFAE 是任务 5 的入口，")
print("   不是 V26 记的「任务 4」。任务 4 的入口是 0x%08X。"
      % m.bus.rd(0x54C00 + 3 * 0x1C + 0x10, 4))
print()
print("任务 5 入口的前几条：")
print(m.disasm_at(binit.TASK_ENTRY, 9))
print()
print("  → 0x12DFB8 `call 0x12E006`：显示初始化是任务 5 开机第一件事。")
print("  → 所以「0x12E006 没人调用」的真正原因是**任务 5 从来没被启动过**。")


# ---------------------------------------------------------------- D
hr("D. 任务 5 起不来：sta_tsk 改了 TCB，派发却不选它")

m = brealos.boot()
tcb5 = m.bus.rd(0x54C00 + 4 * 0x1C, 4)
print("boot 后任务 5 的 TCB @0x%08X：" % tcb5)
print("  " + m.fetch_bytes(tcb5, 0x2C).hex(" "))
print("  +0x08 状态 = 0x%02X（0x10 = 休眠）" % m.bus.rd(tcb5 + 8, 4))
print()
res = brealos.sta_tsk(m, binit.TASK_ID)
print("sta_tsk(5) → %s" % res)
print("  之后 TCB：" + m.fetch_bytes(tcb5, 0x2C).hex(" "))
print("  +0x08 状态 = 0x%02X（0 = 就绪）、+0x18 保存 SP = 0x%08X"
      % (m.bus.rd(tcb5 + 8, 4), m.bus.rd(tcb5 + 0x18, 4)))
print()
print("  但就绪位图 0x45984 = 0x%08X（sta_tsk 前后一样）" % m.bus.rd(0x45984, 4))
print("  → TCB 进了「就绪」，调度器的那张位图却没它 —— 所以派发永远选不到任务 5。")
print("  这一格留给下一版（要接的是「就绪位图 ↔ TCB」的同步点）。")


# ---------------------------------------------------------------- E
hr("E. 方法学：`ret` 读 DR1，不读栈 —— 旧探针的「跑完」是假返回")

print("fr_emu 里 `ret` 的实现：")
print("    if mn in ('ret', 'ret:d'):  tgt = self._dr(1)   # ← 返回地址寄存器")
print("    dr = [TBR, RP, SSP, USP, MDH, MDL]               # dr[1] 就是 RP")
print()
print("所以「往栈上写返回地址 + 改 SP」根本没人看。函数体内若有 `call`，")
print("DR1 会被子调用的返回地址覆盖，结尾的 `ret` 于是回到「子调用之后」。")
print()


def stack_way(addr, r4=0):
    m = brealos.boot()
    c = m.cpu
    m.bus.wr(SCR - 4, 4, RET)
    c.r[15] = SCR - 4
    c.r[4] = r4
    c.i = 0
    c.pc = addr
    n = 0
    while n < 30000 and not c.halted and c.pc != RET:
        m.step(1)
        n += 1
    return n, c.pc, m


def dr_way(addr, r4=0):
    m = brealos.boot()
    r = brealos.call_fn(m, addr, r4=r4)
    return r["steps"], r["pc"], m


print("  %-22s %-28s %s" % ("函数", "栈法", "DR1 法"))
for addr, name in ((binit.INIT_FN, "0x12E006 初始化"),
                   (bqueue.SUBMIT, "0x12E6F8 提交"),
                   (bdesc.APPLY_FN, "0x12E1EC 取描述符")):
    n1, pc1, _m1 = stack_way(addr)
    n2, pc2, _m2 = dr_way(addr)
    print("  %-22s %6d 步 停在 0x%08X   %6d 步 停在 0x%08X"
          % (name, n1, pc1, n2, pc2))
print()
print("  栈法全部撞进 0x404E0（`bra -1` 兜底自旋）—— 看上去「跑了很多步」，")
print("  其实一次都没返回。**结论里的内存值仍然有效**（写入发生在自旋之前），")
print("  但「函数跑完了」这件事是假的。V38 起统一用 brealos.call_fn。")


# ---------------------------------------------------------------- F
hr("F. 阻塞型服务在非任务上下文里：报错或卡死")

m = brealos.boot()
print("裸内核调用（kernel_call，从空闲任务上下文发起）：")
for name, svc, r4, r5 in (("set_flg", -48, 3, 1),
                          ("clr_flg", -47, 3, 0xFFFFFFFE),
                          ("wai_flg", -46, 3, 1),
                          ("wai_sem", -53, 17, 0),
                          ("slp_tsk", -41, 0, 0)):
    r = brealos.kernel_call(m, svc, r4=r4 % 0x100000000, r5=r5 % 0x100000000)
    print("  %-8s(id=%4d) → err = %-5s（%d 步）" % (name, svc, r["err"], r["steps"]))
print()
m = brealos.boot()
r = brealos.call_fn(m, bqueue.PEEK, r4=0, max_steps=3000)
print("整段调取首 0x12E63A（它第一件事就是 wai_sem(17)）：")
print("  → %s" % r)
print("  wai_sem 在非任务上下文里**不返回错误、也不返回成功**，就是卡住。")
print("  这更正 V36 的「直接进 0x12E63A，wai_sem(17) 立刻执行并返回 0」——")
print("  那句是栈法假返回的产物。")


# ---------------------------------------------------------------- G
hr("G. 取首的真实寄存器语义：R4 = is_empty，R5 = 载荷")

head = blist.DISPLAY_QUEUE_HEAD
node = bqueue.slot_addr(0)
for nonempty, pay in ((True, 0x5A), (True, 37), (False, 0)):
    m = brealos.boot()
    if nonempty:
        m.bus.wr(head, 4, node)
        m.bus.wr(head + 4, 4, node)
        m.bus.wr(node, 4, head)
        m.bus.wr(node + 4, 4, head)
        m.bus.wr(node + 8, 1, pay)
        m.bus.wr(node + 9, 1, 0xA5)
    else:
        m.bus.wr(head, 4, head)
        m.bus.wr(head + 4, 4, head)
    c = m.cpu
    reads = []
    base_rd = m.bus.rd

    def spy_rd(a, n, pc=None):
        if 0x9F58D000 <= a < 0x9F58DC00:
            reads.append(a)
        return base_rd(a, n, pc)

    m.bus.rd = spy_rd
    # 跳过 wai_sem，从 0x12E64A（is_empty 那一步）开始，停在 sig_sem 之前
    c.r[8] = head
    c.dr[1] = RET
    c.r[15] = brealos.CALL_STACK
    c.r[4], c.r[5] = 0, 0
    c.i = 0
    c.pc = 0x0012E64A
    n = 0
    while n < 400 and not c.halted and c.pc != 0x0012E664:
        m.step(1)
        n += 1
    m.bus.rd = base_rd
    print("  %s 载荷=%-3s → %2d 步  r4=%02X  r5=%02X  读了 %s"
          % ("非空" if nonempty else "空表", pay if nonempty else "-",
             n, c.r[4], c.r[5],
             "、".join("%X" % a for a in reads)))
print()
print("  0x12E64A is_empty → 0x12E652 空则返回 0 → 0x12E662 `ldub r5,@(8,r0)`")
print("  → 0x12E66E `mov r4,r5`。所以 **R5 才是载荷**，R4 是中间结果；")
print("  搬到 R4 发生在 sig_sem 之后 —— V36 说「返回值在 R4」只在整段跑完时才成立。")


# ---------------------------------------------------------------- H
hr("H. 边界与口径")

print("可报：")
print("  · 槽位 +8 = 槽位编号（0..69，70/70），由 0x12E602 的循环写入")
print("  · 0x12E006 完整跑通（746 步 / 151 笔写），做了五件事")
print("  · 它唯一的调用者是 0x12DFB8，而 0x12DFAE 是**任务 5** 的入口")
print("  · sta_tsk(5) 把 TCB 状态改成就绪，但就绪位图不变 → 派发不到")
print("  · `ret` 读 DR1（RP）；栈法调用是假返回")
print("  · wai_sem 在非任务上下文里卡死，wai_flg 返回 -33")
print()
print("不可报：")
print("  · 「显示任务跑起来了」——任务 5 仍然没被调度（就绪位图那一格没通）")
print("  · 任何画面（V26 已定论：绘制函数不写像素、无帧缓冲）")
print("  · direct page 那几个寄存器（0x38/0x39/0x3A/0x60/0x62/0x63/0x66/0x67）的语义")
print("  · 0x9F58DA85 是「初始化标志」只是**位置 + 时机**推断，没有读者证据")
print("  · -33 这个错误码的具体名字（只报数值）")
