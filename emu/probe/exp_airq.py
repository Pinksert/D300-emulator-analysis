# -*- coding: utf-8 -*-
"""exp_airq.py —— A 段中断通路验证

目的：证明 A 段的 EIT → ISR → reti 全链路在仿真台上可用，
      并验证 ISR 的「回调指针分发」与「默认处理」两条分支。

四组：
1. 基线：不注入，跑到停机
2. 注入 vec 0x10（0x3F01C 为空）→ 应走默认处理 0x81336
3. 注入 vec 0x10（0x3F01C 先填 0x80900 哨兵）→ 应 call 到哨兵
4. 注入 vec 0x20（0x3F060）→ 另一个 ISR，验证可重入/独立性
"""
import sys, os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create


def trap(m, vec, note, pre_poke=None, max_steps=3000):
    """跑到 main 中间，注入中断，记录 PC 轨迹"""
    c = m.cpu
    # 跑到 main（0x804FC）里的 BSS 清零循环中段
    m.run(max_steps=3000)
    # 继续跑到 PC 落在 0x80506 循环里
    guard = 0
    while c.pc != 0x00080506 and guard < 60000:
        c.step()
        m.step_count += 1
        guard += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    if pre_poke:
        for a, v in pre_poke:
            c.m.wr(a, 4, v)
    pc0 = c.pc
    r15_0 = c.r[15]
    ssp0 = c.dr[2]
    irq0 = len(c.irq_log)

    # 注入
    c.eit(vec)

    trace = []
    ret_pc = None
    for i in range(max_steps):
        pc = c.pc
        trace.append(pc)
        if pc == 0x809EE:
            pass
        c.step()
        m.step_count += 1
        # 回到注入点 = reti 成功
        if len(trace) > 3 and c.pc == pc0:
            ret_pc = c.pc
            break
    return dict(note=note, pc0=pc0, vec=vec, trace=trace,
                ret=ret_pc, eit=len(c.irq_log) - irq0,
                r15_ok=(c.r[15] == r15_0), ssp_ok=(c.dr[2] == ssp0),
                final=c.pc)


def show(r):
    tr = r["trace"]
    print("  [%s] vec=0x%02X  注入点=0x%08X  执行 %d 步  EIT=%d"
          % (r["note"], r["vec"], r["pc0"], len(tr), r["eit"]))
    print("    reti 回到注入点: %s   最终 PC=0x%08X"
          % (("是" if r["ret"] else "否"), r["final"]))
    print("    r15 复原=%s  SSP 复原=%s" % (r["r15_ok"], r["ssp_ok"]))
    # 压缩 PC 轨迹：只显示跳变
    comp = []
    for p in tr:
        if not comp or comp[-1] != p:
            comp.append(p)
    print("    PC 轨迹（前 30）: %s" % " ".join("0x%06X" % p for p in comp[:30]))
    if len(comp) > 30:
        print("    …… 共 %d 个不同 PC，末 6 个: %s"
              % (len(comp), " ".join("0x%06X" % p for p in comp[-6:])))
    print()


def main():
    print("=" * 78)
    print("A 段中断通路验证（EIT → ISR → reti）")
    print("=" * 78)
    print()

    # 1 基线
    m = create("d300-a")
    m.load()
    n = m.run(600000)
    print("  [基线] 无中断注入：%d 步  PC=0x%08X" % (n, m.cpu.pc))
    print("     EIT 累计 %d 次" % len(m.cpu.irq_log))
    print()

    # 2 vec 0x10，回调槽为 0 → 默认处理
    m = create("d300-a")
    m.load()
    r = trap(m, 0x10, "vec0x10 回调槽=0")
    show(r)

    # 3 vec 0x10，回调槽填哨兵 0x80900
    m = create("d300-a")
    m.load()
    r = trap(m, 0x10, "vec0x10 回调槽=0x80900", pre_poke=[(0x3F01C, 0x00080900)])
    show(r)

    # 4 vec 0x20
    m = create("d300-a")
    m.load()
    r = trap(m, 0x20, "vec0x20 回调槽=0")
    show(r)

    # 5 连续两次中断，验证可重入
    m = create("d300-a")
    m.load()
    c = m.cpu
    m.run(3000)
    while c.pc != 0x00080506:
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    before = len(c.irq_log)
    c.eit(0x10)
    for i in range(200):
        c.step()
        m.step_count += 1
    mid = c.pc
    c.eit(0x11)
    for i in range(200):
        c.step()
        m.step_count += 1
    print("  [连续两次] EIT 次数=%d  第一次后 PC=0x%08X  第二次后 PC=0x%08X"
          % (len(c.irq_log) - before, mid, c.pc))
    print("     SSP=0x%08X（应回到 0x7F000）" % c.dr[2])
    print()


if __name__ == "__main__":
    main()
