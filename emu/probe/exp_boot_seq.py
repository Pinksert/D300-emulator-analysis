# -*- coding: utf-8 -*-
"""V9：任务 1 的完整启动序列。

三条对照：
  A. 旧办法（wai_flg 立刻返回）—— 看着跑了很远，其实是空转
  B. 只有内核（真阻塞、不送事件）—— 停在第一个握手上
  C. 内核 + 自动送事件 —— 走完整条启动链，最后 sys_ext_tsk 退出
"""
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox.itron import ItronKernel

ENTRY = 0x0008133A
RP = 0x00081338            # 固件自带的 bra -1 停机占位（任务返回的落地处）
STOP = 0x00080506


def sep(t=""):
    print()
    print("=" * 78)
    if t:
        print(t)
        print("=" * 78)


sep("零、任务表")
m0 = create("d300-a")
m0.load()
rows = m0.task_table()
print("  表头 @0x%06X：任务数 %d" % (m0.TASK_TABLE, m0.bus.rd(m0.TASK_TABLE, 4)))
print("  记录 @0x%06X 起，每条 0x%X 字节" % (m0.TASK_REC0, m0.TASK_REC))
print()
print("  #   TCB      任务号  属性  状态        入口       栈顶")
for r in rows:
    print("  %-3d %06X   %-5d  0x%02X  0x%08X  0x%06X   0x%06X  %s"
          % (r["index"], r["tcb"], r["id"], r["attr"], r["state"],
             r["entry"], r["sp"], "已激活" if r["active"] else "-"))
print()
print("  注意：栈顶不是任务表头部的 0x%06X，而是各自一项里的 +0x14。"
      % m0.bus.rd(m0.TASK_TABLE + 4, 4))


# ---------------------------------------------------------------- A：旧办法
sep("一、旧办法：把 wai_flg 当成立即返回的空实现")
mA = create("d300-a", autospin=True)
mA.load()
mA.boot_task(ENTRY, from_table=True, arg=0, stop_pc=STOP, rp=RP)
BASE_A = mA.step_count
mA.run(max_steps=500000)
nA = mA.step_count - BASE_A
seenA = set()


def _count(m, store):
    c = m.cpu
    orig = c.step

    def s():
        store.add(c.pc)
        orig()

    c.step = s
    return orig


print("  任务入口前铺垫 %s 步，入口后跑了 %s 步，累计 %s 步，PC=0x%X"
      % (format(BASE_A, ","), format(nA, ","), format(BASE_A + nA, ","), mA.cpu.pc))
print("  结束 PC=0x%08X（0x81338 是固件自带的 bra -1 停机占位 → 任务正常返回）"
      % mA.cpu.pc)


# ---------------------------------------------------------------- B：真内核
sep("二、挂上会阻塞的内核（不送事件）")
mB = create("d300-a", autospin=True)
mB.load()
mB.attach_kernel(auto_event=False)
mB.boot_task(ENTRY, from_table=True, arg=0, stop_pc=STOP, rp=RP)
BASE_B = mB.step_count
mB.run(max_steps=500000)
nB = mB.step_count - BASE_B
print("  任务入口前铺垫 %s 步，之后 %s 步就停下（累计 %s 步）"
      % (format(BASE_B, ","), format(nB, ","), format(BASE_B + nB, ",")))
print("  " + ItronKernel.describe(mB.blocked))
print("  阻塞点 PC=0x%08X（int 指令）" % mB.blocked["pc"])
print("  已发生的服务调用：")
for name, c in mB.kernel.stats():
    print("    %-18s %d" % (name, c))


# ---------------------------------------------------------------- C：送事件
sep("三、内核 + 自动送事件")
mC = create("d300-a", autospin=True)
mC.load()
mC.attach_kernel(auto_event=True)
mC.boot_task(ENTRY, from_table=True, arg=0, stop_pc=STOP, rp=RP)
BASE_C = mC.step_count
seen = set()
origC = _count(mC, seen)
mC.run(max_steps=500000)
nC = mC.step_count - BASE_C
mC.cpu.step = origC

print("  任务入口前铺垫 %s 步，之后 %s 步（累计 %s 步），走到 %d 个不同 PC"
      % (format(BASE_C, ","), format(nC, ","), format(BASE_C + nC, ","), len(seen)))
print("  服务调用合计 %d 次：" % sum(mC.kernel.calls.values()))
for name, c in mC.kernel.stats():
    print("    %-18s %d" % (name, c))
print()
print("  自动送出的 %d 个事件（这就是启动握手序列；步数从复位算起）："
      % len(mC.kernel.events))
print("   #  步数      PC       标志  等待位     模式  送来之前的值")
for i, e in enumerate(mC.kernel.events, 1):
    print("  %-3d %-9s 0x%06X  %-5d 0x%-8X %-5d 0x%08X"
          % (i, format(e["step"], ","), e["pc"], e["flag"],
             e["pattern"], e["mode"], e["was"]))
print()
if mC.exited is not None:
    print("  结束方式：%s" % ItronKernel.describe(mC.exited))
    print("  退出点 PC=0x%08X" % mC.exited["pc"])
else:
    print("  结束方式：仍然阻塞 —— %s" % ItronKernel.describe(mC.blocked))

print()
print("  反汇编退出点附近：")
print(mC.disasm_at(mC.exited["pc"] - 8, 6) if mC.exited else "")

# sta_tsk 的 tsk_id 序列
sep("四、sta_tsk 启动了哪些任务")
print("  （用第一条跑出来的记录，不再重跑）")
ops = mC.kernel.task_ops
print("  sta_tsk / ter_tsk 共 %d 次：" % len(ops))
for o in ops:
    ent = ""
    for r in rows:
        if r["id"] == o["tsk_id"]:
            ent = "  → 入口 0x%06X 栈顶 0x%06X" % (r["entry"], r["sp"])
    print("    第 %-7s 步  %-12s tsk_id=%-3d param=0x%X%s"
          % (format(o["step"], ","), o["svc"], o["tsk_id"], o["param"], ent))

sep("五、一句话")
n_sta = sum(1 for o in ops if o["svc"] == "sys_sta_tsk")
print("  任务 1 是初始化任务：入口后 %s 步内等了 %d 个事件、启动 %d 个任务，"
      "然后 sys_ext_tsk 退出。"
      % (format(nC, ","), len(mC.kernel.events), n_sta))
print("  旧办法（wai_flg 立刻返回）入口后 %s 步就「跑完」，看起来更顺利 ——"
      % format(nA, ","))
print("  但它并没有真的等过任何一个事件，路径与真内核下的完全不同。")
