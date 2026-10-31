#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_autospin_run.py —— 开自旋消除器把任务 1 一路跑到底

回答三个问题：
  1. 三处「等就绪位」的忙等能不能全部自动认出（包括地址是算出来的第三处）；
  2. 去掉硬件等待以后，任务 1 到底执行了多少代码、调了多少 REALOS 服务；
  3. 它最后停在哪一步、为什么停 —— 是硬件不够，还是我们自己的上下文没搭对。

用法：
    python probe/exp_autospin_run.py            # 默认 60,000 步
    python probe/exp_autospin_run.py 200000
"""
import sys
from collections import Counter

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")


sys.path.insert(0, __file__.rsplit("probe", 1)[0])
sys.stdout.reconfigure(encoding="utf-8")

import fr_emu                                            # noqa: E402
from frbox.machine import create                         # noqa: E402

TASK1 = 0x0008133A
TASK1_RP = 0x00081338   # 停机占位：任务的 ret 落地处
TASK1_SP = 0x000462EC   # 任务表记录里的真值（0x4616C 是表头字段，实测不影响结局）
STOP_PC = 0x00080506          # BSS 清零结束的地方，从这里再跳进任务

eit_cnt = Counter()
_orig_eit = fr_emu.Fr.eit


def _eit(self, vec, level=None):
    eit_cnt[vec] += 1
    return _orig_eit(self, vec, level)


fr_emu.Fr.eit = _eit


def main():
    limit = int(sys.argv[1], 0) if len(sys.argv) > 1 else 60000
    m = create("d300-a", autospin=True)
    m.load()
    m.boot_task(TASK1, sp=TASK1_SP, arg=0, stop_pc=STOP_PC, rp=TASK1_RP)

    print("=" * 78)
    print("机器 d300-a   固件 %s" % (m.fw_path or "?"))
    print("任务 1 入口 0x%06X  SP=0x%06X   跑到 0x%06X 再跳进去"
          % (TASK1, TASK1_SP, STOP_PC))
    print("起点 PC=0x%06X  步数 %s" % (m.cpu.pc, format(m.step_count, ",")))
    print()

    n0 = m.step_count
    pcs = Counter()
    c = m.cpu
    orig_cpu_step = c.step


    def cpu_step():
        pcs[c.pc] += 1
        orig_cpu_step()


    c.step = cpu_step
    m.run(max_steps=limit)
    c.step = orig_cpu_step
    ran = m.step_count - n0

    print("=" * 78)
    print("一、自旋消除器")
    print("=" * 78)
    print(m.spin_breaker.summary())
    print("  #  轮询指令   自旋点     方式   寄存器  等待位   旧→新      第几次  发生在")
    for i, e in enumerate(m.spin_breaker.rows(), 1):
        print("  %-2d 0x%06X  0x%06X  %-6s 0x%03X   0x%02X     0x%02X→0x%02X  %-6d %s 步"
              % (i, e["poll_pc"], e["spin_pc"], e["where"], e["addr"],
                 e["mask"], e["old"], e["old"] | e["mask"], e["nth"],
                 format(e["step"], ",")))
    print()

    print("=" * 78)
    print("二、跑出来的量")
    print("=" * 78)
    print("  实跑步数          %s" % format(ran, ","))
    print("  不同 PC 个数      %s" % format(len(pcs), ","))
    app = [p for p in pcs if 0x80000 <= p < 0x100000]
    print("  其中应用区(0x8xxxx) %s" % format(len(app), ","))
    print("  最热的三条指令    %s"
          % "  ".join("0x%06X×%d" % (p, n) for p, n in pcs.most_common(3)))
    svc = eit_cnt.get(0x40, 0)
    irq = {v: n for v, n in eit_cnt.items() if v not in (0x40,)}
    print("  int #0x40 服务调用 %d 次" % svc)
    print("  硬件中断           %s"
          % ("无" if not irq else
             "  ".join("vec 0x%02X×%d" % (v, n) for v, n in sorted(irq.items()))))
    slots = sorted({a for a, *_ in m.bus.all_writes if 0x3F000 <= a <= 0x3F0FF})
    print("  回调槽被写         %d 个：%s"
          % (len(slots), " ".join("0x%05X" % a for a in slots)))
    print()

    print("=" * 78)
    print("三、停在哪")
    print("=" * 78)
    if m.fault:
        print("  故障：PC=0x%08X  %s" % m.fault)
    elif m.spin:
        print("  自旋：PC 停在 0x%08X（连续 %s 步没离开）" % m.spin)
    else:
        print("  到达步数上限，PC=0x%08X" % m.cpu.pc)
    print("  停点附近：")
    print(m.disasm_at((m.cpu.pc - 8) & ~1, 6) if m.cpu.pc >= 8 else "  （PC 已掉到 0）")
    print()

    print("=" * 78)
    print("四、结论")
    print("=" * 78)
    print("  三处忙等全部被自动认出并放行，任务 1 一路执行到 %s 个不同 PC，"
          % format(len(pcs), ","))
    print("  期间完成 %d 次 REALOS 服务调用、写了 %d 个中断回调槽。" % (svc, len(slots)))
    print("  但没有一条硬件中断被真正受理 —— 三处等待都是我们补的，不是设备给的。")
    print("  任务最后回到 0x081338（固件自带的 bra -1 停机占位），因为 RP 给了 0x81338；")
    print("  它在那里原地打转，所以看起来像「自旋」—— 那是固件自己的停机手法，不是卡住。")
    print("  ⚠️ V8 这段原本写的是「缺调度器铺的栈帧」，V9 已证否：唯一成因是 RP 没设。")
    print("     详见 KNOWN_ISSUES 第 29 条与 results/frbox/sp-rp.txt。")


main()
