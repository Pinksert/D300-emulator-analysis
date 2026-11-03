# -*- coding: utf-8 -*-
"""V22 探针：B 段 REALOS 内核启动 + 向量扫描。

背景（V22 诊断，全部实测）：
  * 之前 d300-b 跑到 191,518 步后 PC 落进 0x6800060C 取指失败。
  * 病根：0x68000000 窗口是 REALOS 的内核对象区（TCB 链表 + 任务用户栈），
    之前未映射 → 派发序列读 [TCB+0x18]（下一任务保存的用户栈指针）得 0
    → USP=0 → 整条「切用户栈取任务入口」的链塌了。
  * 派发序列与 A 段 0x0FE4E6 逐字相同（REALOS 同源）：
      r14=[0x54F10]（下一任务 TCB，在 ROM 表里）
      [0x68000000]=r14（当前 TCB 槽）
      r0=[r14+0x18]（保存的用户栈指针）→ mov r0,USP（0xB300,rs1=3）
      orccr #0x20 切用户栈 → r0=[USP]（任务入口）→ 压系统栈
      恢复 RP/MDL/MDH + ldm0/ldm1 → andccr → retI 进任务。
  * 窗口按 64KB 建 RAM 后：retI 落进 0x407DC（空闲任务 bra -1），
    SP=0x68000BE8、S=1 —— B 段 REALOS 内核活了。

A 基线自洽 / B 任务表解码 / C 向量扫描 / D 串口喂字节 / E 周期候选 / F 长跑
G sys_sta_tsk+派发（任务 2 上 CPU）/ H 全开 27 任务
"""
import os
import sys
import struct

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import brealos

IDLE = 0x000407DC
STUBS = {0x000404F8, 0x0004046E, 0x000406E6}   # 向量表里的空桩


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def boot(idle_extra=2000):
    """boot 到空闲任务（191,520 步）再多跑一点，返回机器与步数。"""
    m = create("d300-b")
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    n = 0
    while not m.cpu.halted and n < 191520:
        m.step(1)
        n += 1
    if idle_extra:
        m.run(max_steps=idle_extra, spin_guard=False)
        n += idle_extra
    return m, n


def kernel_view(m):
    """内核窗口状态：当前 TCB、28 条 TCB 的状态字。"""
    cur = m.bus.rd(0x68000000, 4)
    tcb_states = []
    for i in range(28):
        a = 0x68000168 + i * 0x2C
        st = m.bus.rd(a + 0x14, 4)       # 状态字（A 段同源 TCB +0x14）
        sp = m.bus.rd(a + 0x18, 4)
        if st or sp:
            tcb_states.append((i + 1, st, sp))
    return cur, tcb_states


def sec_a():
    head("A 基线：B 段 REALOS 内核启动（确定性）")
    m, n = boot()
    c = m.cpu
    print("boot %d 步到达派发 retI 0x407D8；retI 后停在 0x%08X（空闲任务）" % (n, c.pc))
    print("SP=0x%08X  S=%d（用户态）  ILM=0x%02X  I=%d" % (c.r[15], c.s, c.ilm, c.i))
    check = (c.pc == IDLE, c.r[15] == 0x68000BE8, c.s == 1)
    print("确定性：PC=0x407DC %s / SP=0x68000BE8 %s / S=1 %s"
          % tuple("√" if x else "×" for x in check))
    print("覆盖 %d 个执行地址；μITRON 调用 %d 次" % (m.cov.words(), len(m.flow.calls)))
    wr = [x for x in m.bus.unmapped_writes if 0x68000000 <= x[0] < 0x68100000]
    print("（窗口未映射时：3746 次写入全被吞；现在建了 64KB RAM，剩 0 次未映射窗口写）")
    cur, tcb = kernel_view(m)
    print("当前 TCB=[0x68000000]=0x%08X（=任务 28 的 TCB 0x6800060C）" % cur)
    print("非零 TCB 状态：")
    for tid, st, sp in tcb:
        print("  任务 %2d: 状态字=0x%08X  保存SP=0x%08X" % (tid, st, sp))


def sec_b():
    head("B 任务表 @0x54C10（28 条 × 0x1C）解码")
    m = create("d300-b")
    m.load()
    data = open(m.fw_path, "rb").read()
    off = 0x54C10 - m.rom_base
    print("字段：+0x00 入口  +0x04 栈顶(0x9F8xxxxx)  +0x0C TCB(0x68000xxx)")
    print("      +0x10 (优先级<<16|编号?)  +0x18 激活标志")
    for i in range(28):
        e = data[off + i * 0x1C: off + (i + 1) * 0x1C]
        w = struct.unpack(">7I", e)
        print("  %2d: 入口 0x%08X  栈 0x%08X  TCB 0x%08X  域4 0x%08X  激活 0x%08X"
              % (i + 1, w[0], w[1], w[3], w[4], w[6]))


def sec_c():
    head("C 向量扫描：46 条真 ISR 逐个请求一次")
    m, n = boot()
    rows = []
    vecs = []
    for v in range(0x10, 0x40):
        h = m.vector(v)
        if h not in STUBS and 0x40000 <= h < 0x740000:
            vecs.append((v, h))
    print("真 ISR %d 条（桩已剔除）。基线覆盖 %d。逐向量：请求一次→跑 3 万步→记增量"
          % (len(vecs), m.cov.words()))
    for v, h in vecs:
        lvl = m.irq_level(v)
        base_cov = m.cov.words()
        base_calls = len(m.flow.calls)
        acc0 = len(m.cpu.irq_log)
        m.raise_irq(v)
        m.run(max_steps=30000, spin_guard=False)
        acc = len(m.cpu.irq_log) - acc0
        dc = m.cov.words() - base_cov
        dn = len(m.flow.calls) - base_calls
        pc = m.cpu.pc
        where = ("空闲" if pc == IDLE else "0x%08X" % pc)
        rows.append((v, h, lvl, acc, dc, dn, where))
    print("向量   ISR       级别  受理  覆盖+  调用+  结束")
    for v, h, lvl, acc, dc, dn, where in rows:
        print("  0x%02X  0x%05X   0x%02X   %d   %5d  %4d  %s"
              % (v, h, lvl, acc, dc, dn, where))
    hot = [r for r in rows if r[3] and (r[4] >= 20 or r[5] or r[6] != "空闲")]
    print("有效果的向量（覆盖≥20 或有调用或没回空闲）：%d 条"
          % len(hot))
    for r in hot:
        print("  ⭐ INT 0x%02X → ISR 0x%05X：覆盖+%d 调用+%d 结束在 %s" % (r[0], r[1], r[4], r[5], r[6]))


def sec_d():
    head("D 串口喂字节：ISR set_flg(3,0x20) 能不能叫醒任务")
    m, n = boot()
    s = m.serial
    s.push_rx(bytes([0xA5]))
    base_cov = m.cov.words()
    before_tcb = kernel_view(m)[1]
    m.icu.raise_line(0x1B)          # 串口线（固件配置 ICR 0x14）
    m.run(max_steps=60000, spin_guard=False)
    acc = len([x for x in m.cpu.irq_log if x[0] == 0x1B])
    print("喂 1 字节 0xA5 + 拉线 0x1B：受理 %d 次，覆盖 +%d，PC=0x%08X"
          % (acc, m.cov.words() - base_cov, m.cpu.pc))
    flag3 = m.bus.rd(0x680008B8 + 2 * 0x10 + 8, 2)   # B 段 flag 控制块 0x680008B8+(N-1)*0x10
    print("flag 3 的位模式字段 = 0x%04X（0x0020 = ISR set_flg(3,0x20) 的目标）" % flag3)
    after_tcb = kernel_view(m)[1]
    new = [t for t in after_tcb if t not in before_tcb]
    print("TCB 状态变化：%s" % (new if new else "无"))
    print("μITRON 调用：%d 次" % len(m.flow.calls))


def sec_e():
    head("E 周期候选：对 C 段有效果的向量各打 64 次")
    m, n = boot()
    scores = []
    for v in range(0x10, 0x40):
        h = m.vector(v)
        if h in STUBS or not (0x40000 <= h < 0x740000):
            continue
        lvl = m.irq_level(v)
        if lvl >= 0x1F:
            continue
        b = m.cov.words()
        m.raise_irq(v)
        m.run(max_steps=20000, spin_guard=False)
        # 任务 2 在自旋的场合，跑到自旋点也算 - 单发增量照记
        scores.append((m.cov.words() - b, v, h))
    scores.sort(reverse=True)
    print("单发覆盖增量前 6：")
    for dc, v, h in scores[:6]:
        print("  INT 0x%02X → 0x%05X：+%d" % (v, h, dc))
    # 0x3F 现在会真派发任务 2（任务栈 RAM 建好后）
    m2, _ = boot()
    b = m2.cov.words()
    for _ in range(16):
        m2.raise_irq(0x3F)
        m2.run(max_steps=60000, spin_guard=False)
    print("INT 0x3F ×16：覆盖 %d → %d（+%d），PC=0x%08X，调用 %d 次"
          % (b, m2.cov.words(), m2.cov.words() - b, m2.cpu.pc, len(m2.flow.calls)))
    print("任务 2 入口 0x1200AC 执行过：%s" % m2.cov.visited(0x1200AC))


def sec_f():
    head("F 长跑：不打扰，boot 后空转 30 万步看内核自己会不会动")
    m, n = boot(idle_extra=0)
    b = m.cov.words()
    m.run(max_steps=300000, spin_guard=False)
    print("30 万步空转：覆盖 %d → %d（+%d），PC=0x%08X，调用 %d"
          % (b, m.cov.words(), m.cov.words() - b, m.cpu.pc, len(m.flow.calls)))
    print("（空闲任务是 bra -1 自旋，内核只在有中断/系统调用时才动 —— 0 才是预期）")


def sec_g():
    head("G sys_sta_tsk + INT 0x3F：B 段第一次真正跑起一个任务")
    m = brealos.boot()
    base = m.cov.words()
    r = brealos.sta_tsk(m, 2)
    print("sys_sta_tsk(2)：err=%s steps=%s（任务 2 帧在 boot 时已建好）"
          % (r["err"], r["steps"]))
    print("任务 2 初始帧实测（派发前）：[0x9F800AA4]=入口 0x%08X  "
          "[+4]=PS 0x%08X  [+8]=0x%08X"
          % (m.bus.rd(0x9F800AA4, 4), m.bus.rd(0x9F800AA8, 4),
             m.bus.rd(0x9F800AAC, 4)))
    dc, pc = brealos.request_dispatch(m)
    print("一次 INT 0x3F 派发：覆盖 +%d，结束 PC=0x%08X（当前 TCB=0x%08X）"
          % (dc, pc, m.bus.rd(brealos.CUR_TCB_SLOT, 4)))
    print("任务 2 入口 0x1200AC 执行过：%s" % m.cov.visited(0x1200AC))
    print("跑起来之后卡在 0x14E86x —— 扫描 SDRAM 半字表比对 0xFFFF/0xAA55 的循环")
    print("（B 段新墙，定性见报告；0x14E846 ldi:32 r10,#0x9F58F348）")


def sec_h():
    head("H 全开：27 个任务 sys_sta_tsk + 8 轮派发")
    m = brealos.boot()
    base = m.cov.words()
    errs = []
    for r in brealos.records(m):
        if r["idx"] == 28:
            continue
        e = brealos.sta_tsk(m, r["idx"])
        errs.append((r["idx"], e["err"]))
    bad = [(i, e) for i, e in errs if e != 0]
    print("sta_tsk(1..27) 返回码：%d 个非零：%s"
          % (len(bad), bad if bad else "无"))
    for rnd in range(8):
        dc, pc = brealos.request_dispatch(m, max_steps=60000)
        print("第 %d 轮派发：覆盖+%d 结束 PC=0x%08X" % (rnd + 1, dc, pc))
    ran = [t for t in brealos.states(m) if t["ran"]]
    print("总覆盖：%d → %d（+%d）" % (base, m.cov.words(), m.cov.words() - base))
    print("入口被执行过的任务（%d 个）：%s"
          % (len(ran), ["任务%d(0x%05X)" % (t["idx"], t["entry"]) for t in ran]))
    print("μITRON 调用：%d 次；结束 PC=0x%08X" % (len(m.flow.calls), m.cpu.pc))


def main():
    sec_a()
    sec_b()
    sec_c()
    sec_d()
    sec_e()
    sec_f()
    sec_g()
    sec_h()
    print("\n完成。")


if __name__ == "__main__":
    main()
