# -*- coding: utf-8 -*-
"""V23 探针：B 段任务 2 的两道墙拆掉 —— 开机装配全程。

背景（V23 诊断，全部实测）：
  墙 1（V22 的 SDRAM 半字表扫描）：任务 2（入口 0x1200AC，开机装配）在
  0x14E834 的函数里扫半字表 —— 0xAA55=记录头、0xFFFF=终止符。表起点来自
  ROM 指针表 0x1C2A98（r4=0 → 0x740000，r4=1 → 0x750000），两块都在
  7MB 镜像之外（镜像正好到 0x740000 截止，尾部 FFFF 填充 + CRC16）。
  之前未映射读回 0，扫描永远等不到终止符 → r5 一路爬过 0x740000+。
  拆法：按擦除态 flash 建模（全 0xFF）—— 扫描立刻命中终止符干净退出，
  「无记录」也是合法的机器状态。

  墙 2（0x125A12 的读两次等变化）：墙 1 拆掉后任务 2 跑到 0x125A0A ——
  连读两次 [0x5C003034]，差值小于容差就继续读（「等计数器走字」的
  时间安全读法）。0x5C003000 是任务 2 专用外设窗口（固件里 33 处引用
  全在 0x120xxx-0x12Exxx；0x120C8E 初始化写周期 0x813@+0x30、
  控制 0xFFFFFFFF@+0x38），+0x34 定性为当前计数值。
  拆法：挂自由运行计数器（每个机器 tick +1）。

  两道墙拆掉后：任务 2 跑完整个开机装配 —— sys_set_tim、信号量握手、
  11 次 sys_sta_tsk 拉起任务 26/5/17/4/9/27/28/1/21/7/16、sys_chg_pri，
  最后落进主事件循环（flag 3 上 clr_flg+wai_flg 轮询）。

A 墙 1 静态定性 / B 墙 1 现场（冻结 flash 演示）/ C 墙 1 拆除
D 墙 2 静态定性 / E 墙 2 拆除：开机装配全程 / F 终点与新边界
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import brealos


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def dispatch(m, steps=200000):
    c0 = m.cov.words()
    m.raise_irq(0x3F)
    m.run(max_steps=steps, spin_guard=False)
    return m.cov.words() - c0, m.cpu.pc


def disasm_snippet(m, addr, n, picks):
    txt = m.disasm_at(addr, n).splitlines()
    keep = [ln for ln in txt if any(p in ln for p in picks)]
    for ln in keep:
        print("   " + ln)


# ---------------------------------------------------------------- A 墙 1 定性
head("A 墙 1 静态定性：扫描起点指针表 + ROM 尾部形态")

m = create("d300-b")
m.load()
print("指针表 @0x1C2A98（扫描起点，来自 0x14E83A 的 ldi:32 r13）：")
for i in range(6):
    print("  [%d] @%08X = %08X" % (i, 0x1C2A98 + i * 4, m.bus.rd(0x1C2A98 + i * 4, 4)))
print("[2] 起是另一张表的基址（0x14E62A 装进 r9 的 0x1C2AA0），别混。")
print()
print("ROM 镜像尾部（0x73FF80-0x740000）：")
for a in range(0x73FF80, 0x740000, 16):
    hw = [m.bus.rd(a + i, 2) for i in range(0, 16, 2)]
    print("  %08X: " % a + " ".join("%04X" % h for h in hw))
print("尾部 FFFF 填充 + 最后 2 字节 CRC —— 镜像到 0x740000 截止，")
print("0x740000/0x750000 两块扫描区在镜像之外（标定/参数区，没提取到）。")
print()
print("扫描解释器（0x14E834 函数体，关键字节）：")
disasm_snippet(m, 0x14E834, 40, ["ldi:32", "lduh", "0xffff", "0xaa55", "beq", "bne"])
print()
print("两遍扫描的调用方（0x14E622）：")
disasm_snippet(m, 0x14E622, 14, ["ldi:32", "call", "ldi:8"])

# ---------------------------------------------------------------- B 墙 1 现场
head("B 墙 1 现场：把 flash-erased 摘掉，扫描立刻掉坑（对照实验）")

m = brealos.boot()
# 演示：把 extra ROM 从总线上摘掉，复现 V22 的墙
m.bus.regions = [r for r in m.bus.regions if r.name != "flash-erased"]
m.bus._starts = [r.base for r in m.bus.regions]
m.bus._cache = None
brealos.sta_tsk(m, 2)
for rnd in range(4):
    dc, pc = dispatch(m, 200000)
    print("第 %d 轮派发：覆盖 +%-4d 结束 PC=%08X  r5=%08X" %
          (rnd + 1, dc, pc, m.cpu.r[5]))
print("r5 一路爬过 0x740000（ROM 之外的未映射区）—— 读回全 0，")
print("既不是 0xFFFF（终止符）也不是 0xAA55（记录头），扫描永远走不完。")

# ---------------------------------------------------------------- C 墙 1 拆除
head("C 墙 1 拆除：擦除态 flash（全 0xFF），扫描立刻命中终止符")

m = brealos.boot()
print("机器默认挂了 flash-erased（extra_rom）：[0x740000] = %04X" % m.bus.rd(0x740000, 2))
# 冻结 hw_counter：先只看墙 1 拆掉后的中间态
real_tick = m.hw_counter.tick
m.hw_counter.tick = lambda cycles: None
brealos.sta_tsk(m, 2)
dc, pc = dispatch(m, 300000)
print("墙 1 拆掉、墙 2 还在（计数器冻结）：覆盖 +%-4d 结束 PC=%08X" % (dc, pc))
print("新调用：", [(s[2], hex(s[3])) for s in m.flow.calls])
print("任务 2 走过了扫描（r5 不再爬墙），撞上墙 2：0x125A1x 的「等走字」。")

# ---------------------------------------------------------------- D 墙 2 定性
head("D 墙 2 静态定性：0x5C003000 硬件定时器")

m2 = create("d300-b")
m2.load()
print("0x125A00 起的等待循环（连读两次 [0x5C003034] 等变化）：")
for ln in m2.disasm_at(0x125A00, 13).splitlines():
    print("   " + ln)
print()
print("0x120C8E 起的初始化（0x5C003000 窗口）：")
for ln in m2.disasm_at(0x120C8C, 12).splitlines():
    print("   " + ln)
print()
print("寄存器布局（实测推定）：+0x30 周期/比较值 0x813（半字）、")
print("+0x34 当前计数（本墙）、+0x38 控制/预分频 0xFFFFFFFF。")
print("固件里 0x5C003000 立即数 33 处，全在任务 2 的代码区 0x120xxx-0x12Exxx。")

# ---------------------------------------------------------------- E 墙 2 拆除
head("E 墙 2 拆除：开机装配全程（两道墙都拆掉）")

m = brealos.boot()
brealos.sta_tsk(m, 2)
cov_a = m.cov.words()           # boot 1241 + sta_tsk 内核路径 +42
dc, pc = dispatch(m, 200000)
print("一次派发（200k 步）：覆盖 +%-4d 调用 %d 次 结束 PC=%08X" %
      (dc, len(m.flow.calls), pc))
m.run(max_steps=800000, spin_guard=False)
print("再跑 80 万步：覆盖停在 +%-4d（派发起算）/ +%-4d（boot 起算，含 sta_tsk 内核路径的 +42）"
      % (m.cov.words() - cov_a, m.cov.words() - 1241))
print("调用 %d 次（装配完成、主循环不再涨）" % len(m.flow.calls))

from collections import Counter
dist = dict(sorted(Counter(s[2] for s in m.flow.calls).items()))
print("服务分布：", dist)
print()
print("开机装配的动作序列（按发生顺序）：")
names = {-23: "sta_tsk", -25: "ter_tsk", -27: "chg_pri", -39: "wup_tsk",
         -41: "slp_tsk", -45: "ext_tsk", -46: "wai_flg", -47: "clr_flg",
         -48: "set_flg", -53: "wai_sem", -55: "sig_sem", -83: "set_tim"}
for st, p, svc, r4, r5, r6, r7, r13, tcb in m.flow.calls[:30]:
    print("  step=%-8d %-8s pc=%08X r4=%08X r5=%08X" %
          (st, names.get(svc, str(svc)), p, r4, r5))
print("  ……（之后 3373 对 clr_flg/wai_flg 是主事件循环的稳态）")

sta = [r4 for _, _, svc, r4, *_ in m.flow.calls if svc == -23]
print()
print("sta_tsk 拉起的 11 个任务：", sta)
print("sem 握手对象（wai+sig 成对）：",
      sorted(set(r4 for _, _, s, r4, *_ in m.flow.calls if s in (-53, -55))))

# ---------------------------------------------------------------- F 终点
head("F 终点与新边界")

wai = [c for c in m.flow.calls if c[2] == -46]
print("主事件循环：全部 wai_flg 等 flag 3，样例 r4(p_flgptn)=%08X r6(ptn)=%08X r7(mode)=%d"
      % (wai[-1][3], wai[-1][5], wai[-1][6]))
print("任务 2 的 TCB+0x08 状态字 = %08X（等待位有效）" % m.bus.rd(0x68000168 + 8, 4))
print("跑过 CPU 的任务：", [t["idx"] for t in brealos.states(m) if t["ran"]])
print()
print("能报：")
print("  * 墙 1 的定性（扫描起点指针表、镜像边界、擦除态建模后干净退出）")
print("  * 墙 2 的定性（0x5C003000 定时器、+0x34 计数器、读两次等变化）")
print("  * 任务 2 跑完开机装配：set_tim + sem 握手 + 11 次 sta_tsk + chg_pri")
print("  * 终点是 flag 3 的 clr/wai 轮询（主事件循环）")
print("不能报：")
print("  * flag 3 位 0x200000/0x80 的置位者是谁（等的事件源未证）")
print("  * wai_flg mode=2 内核立即返回 E_OK 的语义（r13=0x305 的来源未证）")
print("  * 其余 27 个任务的行为（它们从未上过 CPU）")
print("  * 「相机跑起来了」（真实外设事件源一个都没建模）")
print()
print("⚠️ 口径：本探针的 hw_counter 每个 tick（256 步）+1；真实硬件的计数")
print("频率由 0x5C003030=0x813 配置决定，未标定 —— 延时比例不代表真实时长。")
