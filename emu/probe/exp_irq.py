# -*- coding: utf-8 -*-
"""用新加的中断机制真跑 B 段的中断服务程序（ISR）。

以前只能从函数入口硬跑，拿不到「中断响应」这条路径；现在可以：
  TBR 设好 → request_irq(向量号) → EIT 压栈 → 从向量表跳进 ISR → reti 回来。

B 段实测（1.11）：TBR=0x000EFC00
  IRQ 0x1B (Serial IF0) 槽 0x000EFF90 → 0x0012FDB4
  IRQ 0x20 (EXT9)       槽 0x000EFF7C → 0x0012FE54
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

from fr_emu import Fr, Mem, TABLE, M32, Unimpl
from fr_itron import ItronStub

ROM = os.path.join(FW_DIR, "B3000111.bin")
ROM_BASE = 0x00040000
TBR = 0x000EFC00
SSP = 0x0003F000
USP = 0x0003E000


def build():
    m = Mem()
    m.load_rom(ROM_BASE, open(ROM, "rb").read())
    cpu = Fr(m, TABLE)
    cpu.dr[0] = TBR
    cpu.dr[2] = SSP
    cpu.dr[3] = USP
    cpu.i = 1
    cpu.ilm = 0
    cpu.r[15] = SSP
    cpu.pc = 0x00050000                       # 随便一个地点当「被打断的代码」
    return cpu, m


def run_irq(vec, level=8, max_steps=20000, quiet=0x1000):
    cpu, m = build()
    svc = ItronStub(seg="B")
    cpu.svc_hook = svc
    # 外设：把读不到的状态位当 0，写记日志；给轮询地址设逃生阀
    cpu.request_irq(vec, level=level)
    start_pc = cpu.pc
    n = 0
    peak = 0
    for n in range(max_steps):
        if cpu.halted:
            break
        try:
            cpu.step()
        except Unimpl as e:
            cpu.halted = True
            cpu.halt_reason = f"未实现指令 {e.args[0]} @0x{cpu.pc:08X}"
            break
        peak = max(peak, SSP - cpu._get_sp(0))
        if cpu.pc == start_pc:                # reti 回到被打断的位置
            n += 1
            break
    return cpu, m, svc, n, start_pc, peak


def main():
    for vec, label in ((0x1B, "Serial IF0"), (0x20, "EXT9")):
        cpu, m, svc, n, start, peak = run_irq(vec, level=8)
        slot = TBR + (255 - vec) * 4
        tgt = int.from_bytes(open(ROM, "rb").read()[slot - ROM_BASE:
                                                     slot - ROM_BASE + 4], "big")
        print(f"===== IRQ 0x{vec:02X} ({label}) =====")
        print(f"  向量槽 0x{slot:08X} → 入口 0x{tgt:08X}")
        print(f"  步数 {n}   结束 PC 0x{cpu.pc:08X}   "
              f"{'回到被打断处' if cpu.pc == start else '未返回'}")
        if cpu.halted:
            print(f"  停机原因：{cpu.halt_reason}")
        print(f"  EIT 记录 {len(cpu.irq_log)} 次；峰值栈用量 {peak} 字节")
        io_r = sorted(m.io_reads.items(), key=lambda x: -x[1])[:6]
        print(f"  外设读 {len(m.io_reads)} 个地址，最多："
              + "  ".join(f"0x{a:03X}×{c}" for a, c in io_r))
        io_w = {}
        for a, sz, v, pc in m.io_writes:
            io_w[(a, v)] = io_w.get((a, v), 0) + 1
        top_w = sorted(io_w.items(), key=lambda x: -x[1])[:6]
        print(f"  外设写 {len(m.io_writes)} 次，最多："
              + "  ".join(f"0x{a:03X}←0x{v:02X}×{c}" for (a, v), c in top_w))
        if svc.count:
            print("  μITRON 调用：" + ", ".join(f"{k}×{v}" for k, v in svc.count.items()))
        print()


if __name__ == "__main__":
    main()
