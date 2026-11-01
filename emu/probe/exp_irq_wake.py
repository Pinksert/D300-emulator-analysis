#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_irq_wake.py —— 死锁现场逐个向量注入，看哪条中断能把这台相机推下去

前置（exp_realos_flow.py 的结论）：固件自己的 REALOS 跑到 80,000 步时是这样停的

    任务 5  wai_flg(flg2, 0x4000/AND)
    任务 1  wai_flg(flg3, 0x2000/AND)
    flg3 的等待队列头 = 任务 1 的 TCB，位模式 0 —— 没人在等它，也没人置它。

于是问题变成：**哪条中断的 ISR 会去置 flg3 的 bit13？** 这个问题只能实测，
因为静态扫描里那条 set_flg 的位模式是算出来的（不是常量）。

方法：
  1. 跑到死锁；把现场存进快照；
  2. 每个向量单独 loadvm 一份现场，按 ICR 里的级别注入一次；
  3. 跑 20,000 步，记：受理了没有 / 产生了哪些服务调用（尤其是 set_flg）/
     有没有新任务被 sta_tsk / 有没有任务从等待里出来 / 停在哪。

判据：
  「醒」= 注入之后出现了 set_flg(flg3, 位含 0x2000) 或任务 1 重新产生服务调用；
  「拉起」= 任务表里有新的记录变成激活（状态字最高位）。
  注意区分「被 ILM 挡住」和「ISR 跑了但什么都没做」——ICR=0x1F 的向量在
  当前模型下永远不会受理，所以对它们额外用 0x1E 再打一次做对照。

用法：cd emu && python probe/exp_irq_wake.py [-v]
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

import fr_svc
from frbox.machine import create
from frbox import realos, snapshot

NAMES = {k: v[0].replace("sys_", "") for k, v in fr_svc.load_props().items()}
CUR_TCB = realos.CUR_TCB
IDLE_PC = 0x000FE510
FLAG3 = 0x00045C58            # flg3 控制块
FLAG3_PAT = FLAG3 + 8


def rd(m, a, n=4):
    d = m.fetch_bytes(a, n)
    return int.from_bytes(d, "big") if len(d) == n else None


def to_deadlock(m, budget=200000, chunk=20000):
    """跑到空闲任务连续两次、且没有新服务调用为止。"""
    n0 = m.cpu.int40
    idle = 0
    while m.step_count < budget:
        m.run(max_steps=chunk, spin_guard=False)
        if m.cpu.pc == IDLE_PC and m.cpu.int40 == n0:
            idle += 1
            if idle >= 2:
                break
        else:
            idle = 0
            n0 = m.cpu.int40
    return m.cpu.pc == IDLE_PC


def main():
    verbose = "-v" in sys.argv
    m = create("d300-a")
    m.load()
    m.boot_realos(task_id=1)
    if not to_deadlock(m):
        print("没跑到死锁，PC=0x%08X" % m.cpu.pc)
        return
    print("死锁现场：PC=0x%08X 步数 %d 服务调用 %d 次 flg3=0x%08X"
          % (m.cpu.pc, m.step_count, m.cpu.int40, rd(m, FLAG3_PAT) or 0))
    tasks0 = {r["id"]: r["active"] for r in realos.records(m)}
    print("  已激活：%s" % [t for t, a in tasks0.items() if a])

    f = tempfile.NamedTemporaryFile(suffix=".snp", delete=False)
    f.close()
    snapshot.save(m, f.name)

    rows = []
    for vec in list(range(0x10, 0x40)):
        lvl = m.irq_level(vec)
        trials = [None] if lvl != 0x1F else [None, 0x1E]   # ICR=0x1F 的再强打一次对照
        for forced in trials:
            snapshot.load(m, f.name)
            ev = []
            cur = {"n": 0}

            def hook(cpu, num, pc, ev=ev, cur=cur):
                svc = cpu.r[12] & 0xFF
                svc = svc - 0x100 if svc & 0x80 else svc
                ev.append((svc, cpu.r[4], cpu.r[5], cpu.r[6], cpu.r[7],
                           rd(m, CUR_TCB) or 0, pc))
                cur["n"] += 1

            m.attach_int_hook(hook)
            before3 = rd(m, FLAG3_PAT) or 0
            lv = forced if forced is not None else lvl
            nirq0 = len(m.cpu.irq_log)
            m.icu.raise_vec(vec, lv)
            isr = m.vector(vec)
            m.run(max_steps=20000, spin_guard=False)
            after3 = rd(m, FLAG3_PAT) or 0
            tasks = {r["id"]: r["active"] for r in realos.records(m)}
            new = [t for t, a in tasks.items() if a and not tasks0.get(t)]
            sta = [(a[0] and a[1]) for a in ev if a[0] == -23]
            woke = [e for e in ev if e[0] == -48 and e[2] == 3 and ((e[3] or 0) & 0x2000)]
            rows.append(dict(vec=vec, isr=isr, lvl=lvl, used=lv,
                             accepted=len(m.cpu.irq_log) > nirq0,
                             n=len(ev), new=new, sta=len(sta), woke=bool(woke),
                             d3=after3 != before3, pat=after3,
                             pc=m.cpu.pc, halted=m.cpu.halted, ev=ev))
        # 只保留有意义的行
    print("\n向量注入结果（只列产生了服务调用 / 拉起任务 / 改了 flg3 的）")
    print("  %-5s %-10s %-5s %-4s %-6s %-6s %-8s %s"
          % ("vec", "ISR", "ICR", "步后", "服务", "sta", "flg3", "备注"))
    hit = 0
    for r in rows:
        if not (r["n"] or r["new"] or r["d3"]):
            continue
        hit += 1
        note = []
        if r["woke"]:
            note.append("★置了 flg3/0x2000")
        if r["new"]:
            note.append("拉起任务 %s" % r["new"])
        if r["d3"]:
            note.append("flg3→0x%X" % r["pat"])
        if r["halted"]:
            note.append("停机")
        if not r["accepted"]:
            note.append("未被受理")
        if r["lvl"] == 0x1F and r["used"] == 0x1E:
            note.append("（ICR=0x1F，按 0x1E 强打）")
        if r["pc"] != IDLE_PC:
            note.append("PC=0x%X" % r["pc"])
        print("  0x%02X  0x%08X 0x%02X  %-4d %-6d %-6d %-8s %s"
              % (r["vec"], r["isr"], r["lvl"], r["n"], r["n"], r["sta"],
                 "变了" if r["d3"] else "-", " ".join(note)))
        if verbose:
            for svc, a4, a5, a6, a7, tcb, pc in r["ev"][:12]:
                print("        %-8s a4=%-6s a5=%-6s tcb=0x%X @0x%X"
                      % (NAMES.get(svc, svc), a4, a5, tcb, pc))
    print("\n  有效果的向量 %d / %d 次注入" % (hit, len(rows)))

    # 每个任务最后停在哪：把"苏醒"的现场再跑长一点
    best = [r for r in rows if r["new"] or r["woke"]]
    print("\n最值得继续推的向量：%s" % [("0x%02X" % r["vec"]) for r in best] or "无")
    os.unlink(f.name)


if __name__ == "__main__":
    main()
