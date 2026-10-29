# -*- coding: utf-8 -*-
"""查 0x482 握手位：A 段自旋前后，谁在读、谁在写。

三路证据：
  1. 动态：watch 0x480–0x48F，跑到自旋为止，列出每次访问的 PC/值/方向
  2. 静态：全 A 段反汇编里所有直接出现 0x482 附近立即数的指令
  3. 语义：读那几个 PC 附近的代码，看清 0x08 / 0x80 的来历
"""
import sys, collections
import os


_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

from fr_emu import TABLE
from frbox.machine import create
from frbox.trace import Trace

MASK = 0x482


def dyn():
    m = create("d300-a")
    m.load()
    m.spin_limit = 200000
    for a in range(0x480, 0x490):
        m.bus.watch.add(a)
    n = m.run(max_steps=400000)
    print(f"[动态] 执行 {n:,} 步，spin={m.spin and hex(m.spin[0])}")
    print(f"[动态] 最终 PC=0x{m.cpu.pc:08X}")
    log = m.bus.access_log
    print(f"[动态] watch 命中 {len(log)} 次")
    if log:
        by = collections.Counter((k, a) for k, a, *_ in log)
        for (k, a), c in sorted(by.items()):
            print(f"    {k} 0x{a:08X} × {c}")
        print("    前 25 条明细：")
        for rec in log[:25]:
            k, a, sz, v, pc = rec
            print(f"      {k} 0x{a:08X} sz={sz} val=0x{v:02X} @pc=0x{pc:08X}")
        print("    后 10 条明细：")
        for rec in log[-10:]:
            k, a, sz, v, pc = rec
            print(f"      {k} 0x{a:08X} sz={sz} val=0x{v:02X} @pc=0x{pc:08X}")
        w = [r for r in log if r[0] == "W"]
        if w:
            print("    写点 PC 去重：" + " ".join(f"0x{r[4]:08X}(v=0x{r[3]:02X})" for r in w))
    return m


def stat(m):
    """全 ROM 反汇编，找立即数落在 0x478–0x48C 的指令"""
    rom = m.bus.rom
    base = m.bus.rom_base
    hits = []
    off = 0
    n = len(rom)
    while off < n - 1:
        ins = TABLE.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mnem, _b, vals, size, word = ins
        for k, v in vals.items():
            if isinstance(v, int) and 0x478 <= v <= 0x48C:
                hits.append((base + off, mnem, k, v, size, word))
                break
        off += max(2, size)
    print(f"[静态] ROM {n:,} 字节，扫描完，命中 {len(hits)} 条（立即数/位移落在 0x478–0x48C）")
    for pc, mnem, k, v, size, word in hits[:40]:
        print(f"    0x{pc:08X}: {word:0{size*2}X}  {mnem} {k}=0x{v:X}")
    return hits


def ctx(m, addrs, span=9):
    """打印若干 PC 前后的反汇编"""
    for a in addrs:
        print(f"\n[上下文] 0x{a:08X} 附近")
        print(m.disasm_at(a - 8 * 2, span))


if __name__ == "__main__":
    m = dyn()
    hits = stat(m)
    # 动态写点 + 静态命中点合并看上下文
    wpc = sorted({r[4] for r in m.bus.access_log if r[0] == "W"})
    if wpc or hits:
        ctx(m, (wpc[:2] + [h[0] for h in hits[:2]])[:3])
