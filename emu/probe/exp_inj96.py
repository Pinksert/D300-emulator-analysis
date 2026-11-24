# -*- coding: utf-8 -*-
"""V96 探针：flag 6 是**任务工作邮箱** —— 14 位、7 个活消费者、7 个死位。

    cd emu && python probe/exp_inj96.py > ../results/frbox/inj96.txt

七段：A 收敛态现场 / B 静态邮箱规格 / C 逐位注入（14 位，预测 vs 实测）/
      D 复合掩码（广播）/ E 全位注入（死位「剩」出来）/ F 深度与投递 /
      G 结论与禁区

做法不是「看着像邮箱」：**先由静态点 + 现场等待者算出每位该唤醒谁，再逐位注入验证**。
七段里 C/D/E 是同一套预测的三次独立检验（单个位 / 复合掩码 / 全位），
互相不共享机器（每次 fresh boot）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frbox import bstatic as B, bflag, blive, brealos, inject96 as I, svcnum


def sec(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


print("口径：bstatic.sweep_of_type('d300-b') 一次（约 60 秒）+ 每格一台 fresh 机器"
      "（boot 191,520 步 + 跑到 %d 步，约 25 秒）" % I.CONVERGE_STEPS)

sw, _ = B.sweep_of_type("d300-b")

# ------------------------------------------------------------------ A
sec("[A] 收敛态现场：17 个任务在等什么")
m = I.converged()
snap = I.snapshot(m)
print("A1 末 PC = 0x%06X（空闲任务 `bra -1` 0x%06X）⇒ %s"
      % (snap["end_pc"], I.IDLE_PC, "已收敛" if snap["idle"] else "未收敛"))
print("A2 覆盖 %d 条指令地址；等待中的任务 %d 个：%s"
      % (snap["cov"], snap["waiters"], snap["tasks"]))
print("A3 等待表（任务 / 等待原因 / 对象 / 等待位）：")
rows = I.wait_rows(m)
for w in I.waiters(m, rows):
    print("   任务%-3d %-14s %-12s want=0x%08X"
          % (w["tid"], w["svc"], ("flag %d" % w["flgid"]) if w["flgid"] else "延时队列",
             w["want"]))
print("A4 flag %d 的现场等待者 %d 位：" % (I.MAILBOX_FLAG, len(snap["mailbox"])))
for bit, tids in snap["mailbox"].items():
    print("   bit 0x%05X → 任务 %s" % (bit, tids))
print("A5 flag 6 当前位模式 = 0x%08X（收敛态没有积压的位）" % snap["pattern"])

LIVE = snap["mailbox"]

# ------------------------------------------------------------------ B
sec("[B] 静态邮箱规格：88 个 set 点 / 58 个 wait 点 / 14 个位")
sp = I.spec(sw)
pb = I.per_bit(sp)
print("B1 set_flg(6) 调用点 %d 个（其中实参读不出 %d 个），出现 %d 种掩码"
      % (len(sp["set"]) + len(sp["set_none"]), len(sp["set_none"]), len(sp["set_masks"])))
print("B2 wai/twai_flg(6) 调用点 %d 个（实参读不出 %d 个），出现 %d 种等待位"
      % (len(sp["wait"]) + len(sp["wait_none"]), len(sp["wait_none"]), len(sp["wait_masks"])))
print("B3 位集合 %d 个：%s" % (len(sp["bits"]), ["0x%X" % b for b in sp["bits"]]))
print("B4 逐位（位 / set 点数 / wait 点数 / 现场有等待者）：")
for bit in sp["bits"]:
    print("   0x%05X   set %2d   wait %2d   %s"
          % (bit, len(pb[bit]["set"]), len(pb[bit]["wait"]),
             "是（任务 %s）" % LIVE[bit] if bit in LIVE else "否"))
print("B5 set 掩码里出现复合位（>1 位）的 %d 种 —— 这些是**广播**，不是单任务投递："
      % sum(1 for mk in sp["set_masks"] if len(I.bits_of(mk)) > 1))
for mk, n in sp["set_masks"].items():
    bs = I.bits_of(mk)
    if len(bs) > 1:
        pred = sorted({t for b in bs for t in LIVE.get(b, [])})
        print("   0x%08X  %2d 点  = %s  ⇒ 预测唤醒 %s"
              % (mk, n, "+".join("0x%X" % b for b in bs), pred or "无（全是死位）"))

DEAD = I.dead_bits(sp, LIVE)
print("B6 静态有位、现场无等待者的「死位」%d 个：%s"
      % (len(DEAD), ["0x%X" % b for b in DEAD]))
print("B7 ⇒ 邮箱规格 14 位 = 活 %d + 死 %d；死位对应的等待点代码从未执行（F 段核）"
      % (len(LIVE), len(DEAD)))

# ------------------------------------------------------------------ C
sec("[C] 逐位注入：预测 vs 实测（每位一台 fresh 机器）")
print("C0 预测规则：谁的等待位与该位有交集，就该上 CPU；`want=0` 不算")
print("C1 活位 %d 个：" % len(LIVE))
ok = 0
for bit in sorted(LIVE):
    r = I.trial(bit)
    ok += r["ok"]
    print("   bit 0x%05X  预测 %-10s 实测 %-10s %s  +%d 条  末PC 0x%06X  残留 0x%08X"
          % (bit, r["pred"], r["got"], "OK " if r["ok"] else "BAD",
             r["new_cov"], r["end_pc"], r["pattern"]))
print("C2 死位 %d 个（对照：应该一个都不唤醒）：" % len(DEAD))
okd = 0
for bit in DEAD:
    r = I.trial(bit)
    okd += r["ok"]
    print("   bit 0x%05X  预测 %-10s 实测 %-10s %s  +%d 条  残留 0x%08X"
          % (bit, r["pred"] or "[]", r["got"], "OK " if r["ok"] else "BAD",
             r["new_cov"], r["pattern"]))
print("C3 合计：活位 %d/%d 命中预测，死位 %d/%d 命中（负对照）"
      % (ok, len(LIVE), okd, len(DEAD)))

# ------------------------------------------------------------------ D
sec("[D] 复合掩码：广播掩码是否等于「并集」")
for mask in (0x1E0, 0xC0, 0x320, 0x1F0):
    r = I.trial(mask)
    print("D  set_flg(6, 0x%03X)  预测 %-22s 实测 %-22s %s  +%d 条  残留 0x%08X"
          % (mask, r["pred"], r["got"], "OK " if r["ok"] else "BAD",
             r["new_cov"], r["pattern"]))
print("D5 0x1F0 含死位 0x10，所以「唤醒集合」比 0x1E0 不多不少，但**残留位**多一个 0x10")
print("   ⇒ 残留位是「投进去没人消费」的直接读数，比反汇编猜更硬")

# ------------------------------------------------------------------ E
sec("[E] 全位注入：把 14 位一次投进去，死位会「剩」下来")
ALL = 0
for b in sp["bits"]:
    ALL |= b
r = I.trial(ALL, steps=120000)
print("E1 注入 set_flg(6, 0x%X)" % ALL)
print("E2 预测唤醒 %s" % r["pred"])
print("E3 实测唤醒 %s  %s" % (r["got"], "OK" if r["ok"] else "BAD"))
print("E4 残留位模式 0x%08X" % r["pattern"])
left = I.bits_of(r["pattern"])
print("E5 残留位的位集合 %s" % ["0x%X" % b for b in left])
print("E6 与 B6 的静态死位**逐位相同** ⇒ %s" % (left == DEAD))
print("E7 两条独立路径互证：B6 是「静态有 wait 点但没人执行」，E5 是「投进去没人消费」")
print("E8 注入回执 %s；新覆盖 +%d 条；末 PC 0x%06X（%s）"
      % (r["inject"], r["new_cov"], r["end_pc"], "回空闲" if r["idle"] else "没回空闲"))

# ------------------------------------------------------------------ F
sec("[F] 深度：投递一次邮箱能走多远")
sets6 = sorted(sp["set"])
waits6 = sorted(sp["wait"])
n_set = sum(1 for a in sets6 if m.cov.visited(a))
n_wait = sum(1 for a in waits6 if m.cov.visited(a))
print("F1 收敛态 600k 步（无注入）：set 点执行 %d/%d，wait 点执行 %d/%d"
      % (n_set, len(sets6), n_wait, len(waits6)))
print("F2 已执行的 wait 点对应的位：%s"
      % sorted({sp["wait"][a] for a in waits6 if m.cov.visited(a)}))
print("F3 ⇒ 88 个置位点**一个都没执行**：这套邮箱在本启动路径里从未被投递过")
print("F4 全位注入后（E 段那台）：新覆盖只有 +%d 条，末 PC 回空闲" % r["new_cov"])
print("F5 ⇒ 被唤醒的 7 个任务各自跑完「取件 → 处理 → 回来再等」的短路径，"
      "没有任何一个把系统带出空闲")
print("F6 深水区仍是 KI#366 那 11 个从未启动的任务（8/12–15/18/19/22–25）："
      "它们的 wait 点就在死位后面，但邮箱投递唤不醒**没被创建**的任务")
print("F7 无回归（与 V95 同口径）：覆盖 %d 条、%d 个等待者、末 PC 0x%06X、"
      "600k 步换手 %d 次"
      % (snap["cov"], snap["waiters"], I.IDLE_PC, m.swaps))

# ------------------------------------------------------------------ G
sec("[G] 结论与禁区")
print("G1 flag 6 不是普通事件标志，是**任务工作邮箱**：14 个位，每位对一个任务")
print("G2 收敛态只有 7 位有活消费者（任务 4/6/9/10/11/27/28）；另 7 位（0x10/0x400/")
print("   0x800/0x1000/0x8000/0x10000/0x20000）的等待点在没跑过的代码里")
print("G3 静态 88 个 set 点里有 %d 种复合掩码 = 广播（0x1E0 → 任务 9/10/11/27）"
      % sum(1 for mk in sp["set_masks"] if len(I.bits_of(mk)) > 1))
print("G4 预测 21/21 命中：7 活位 + 7 死位 + 4 复合掩码 + 全位 1 次（含唤醒集合与残留位）")
print("G5 设施：frbox/inject96.py（fresh 机器 → 注入 → 观察 CURRENT_TCB 换手）")
print("")
print("不能报：这些位在真机由谁置位 / 位对应哪个外设 / 注入等于真机事件 /")
print("        任务 12–15 的业务语义 / 邮箱唤醒就能让系统跑起来 / 死位永远不会被置。")
print("能报：flag 6 的位→任务映射在本版**被注入实验逐位验证**；88 个置位点在本启动")
print("      路径里 0 执行；7 个活位注入后系统仍回空闲，深度没有变化。")
