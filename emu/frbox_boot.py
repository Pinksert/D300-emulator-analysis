# -*- coding: utf-8 -*-
"""启动诊断：把两台机器各自「能跑多远、卡在哪一条指令」测出来。

跑法：python frbox_boot.py [--max 3000000]

判据（三个都是「跑不下去」的不同形态，必须分开记）：
  停机       CPU 自己 halted（无法解码/遇到未接钩子的 int）
  起飞       PC 离开 ROM 区（跳到 RAM 或 0）
  自旋       连续 N 步都在同一 4KB 窗口里（在等某个位）
"""
import argparse
import collections
import sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")



sys.stdout.reconfigure(encoding="utf-8")

from fr_emu import TABLE                       # noqa: E402
from frbox.machine import create                # noqa: E402


def boot(m, max_steps=3000000, spin_limit=200000, window=0x100):
    c = m.cpu
    rom_lo, rom_hi = m.bus.rom_base, m.bus.rom_end
    pcs = set()
    buf = collections.deque(maxlen=10)
    base, cnt = None, 0
    left, spin = None, None
    for i in range(max_steps):
        if c.halted:
            break
        if not (rom_lo <= c.pc < rom_hi):
            left = i
            break
        pcs.add(c.pc)
        buf.append(c.pc)
        if base is None or abs(c.pc - base) > window:
            base, cnt = c.pc, 1
        else:
            cnt += 1
            if cnt >= spin_limit:
                spin = (base, cnt)
                break
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    return {"steps": i, "pcs": len(pcs), "left": left, "spin": spin,
            "tail": list(buf), "halted": c.halted, "reason": c.halt_reason}


def describe(r):
    if r["halted"]:
        return f"停机：{r['reason']}"
    if r["spin"]:
        base, cnt = r["spin"]
        return f"自旋：0x{base:08X} 附近连续 {cnt:,} 步"
    if r["left"] is not None:
        return "起飞：PC 离开 ROM"
    return "到达步数上限"


def tail_disasm(m, pcs):
    out = []
    for pc in pcs[-6:]:
        ins = TABLE.decode(m.fetch_bytes(pc, 10), 0)
        txt = f"{ins[0]} {ins[2]}" if ins else "<无法解码>"
        out.append(f"    0x{pc:08X}  {txt}")
    return "\n".join(out)


def run_case(title, machine_name, itron=False, latch=None, max_steps=3000000,
             clk_lock=None):
    m = create(machine_name, clk_lock_ticks=clk_lock)
    m.load()
    if itron:
        m.attach_itron()
    if latch:
        for a, v, n in latch:
            m.bus.io_after[a] = (n, v)
    r = boot(m, max_steps)
    print(f"\n=== {title} ===")
    print(f"  {describe(r)}")
    print(f"  已执行 {r['steps']:,} 步，碰到 {r['pcs']:,} 个不同 PC")
    print("  最后几条指令：")
    print(tail_disasm(m, r["tail"]))
    st = m.stats()
    print(f"  未映射读 {st['unmapped_read_times']} 次（{st['unmapped_read_addr']} 个地址），"
          f"未映射写 {st['unmapped_write_times']} 次")
    print(f"  直接页未建模读 {st['dp_unknown_read_times']:,} 次"
          f"（{st['dp_unknown_read_addr']} 个寄存器），"
          f"外设写 {st['io_write_times']} 次，中断 {st['irq_taken']} 次")
    print(f"  时钟寄存器组 0x482=0x{m.clk.read(0x02):02X} 0x484=0x{m.clk.read(0x04):02X}"
          f" 锁定 {m.clk.locks} 次")
    top = sorted(m.dp.unknown_reads.items(), key=lambda x: -x[1])[:6]
    if top:
        print("  最热的未建模寄存器：" + "，".join(f"0x{a:03X}×{c}" for a, c in top))
    if m.itron is not None:
        print(f"  μITRON 服务调用 {sum(m.itron.count.values())} 次："
              + "，".join(f"{k}×{v}" for k, v in m.itron.count.items()))
    return m, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=3000000)
    args = ap.parse_args()

    print("D300 启动诊断（1.11 固件，两台机器各自独立）")
    print("=" * 78)
    run_case("A 段：靠时钟设备模型自己锁定（默认 8 tick）", "d300-a",
             max_steps=args.max)
    run_case("A 段：时钟立刻就绪（--clk-lock 0）", "d300-a", clk_lock=0,
             max_steps=args.max)
    run_case("B 段：原样", "d300-b", max_steps=args.max)
    run_case("B 段：内核走真代码 + μITRON 半主机", "d300-b", itron=True,
             max_steps=args.max)


if __name__ == "__main__":
    main()
