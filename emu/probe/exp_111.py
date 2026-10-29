# -*- coding: utf-8 -*-
"""
★ 正式实验：1.10 与 1.11 在同一段光圈驱动代码上的行为对照
  起点：0x000DB8F0（两版字节完全相同，是这段逻辑的第一条指令）
  · 子程序调用打桩（只记 目标地址 + r0..r3），外设读返回 0
  · 变量：RAM 0x0003F81C 的 bit3（旧开关）与 bit6（1.11 新增开关）
  · 观测：调用序列、写进哪些 RAM 变量、写了什么值
"""
import sys, os, json, collections
sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

from fr_emu import Fr, Mem, TABLE

BASE = FW_DIR
ROM = 0x00080000
START = 0x000DB8F0
WIN = (0x000DB800, 0x000DC400)
SP0 = 0x0004616C

# 这段逻辑会用到的 RAM 变量：给个不为 0 的初值，免得所有分支都走 0
# (地址, 字节数, 值) —— 注意 0x3F815 / 0x3FAF2 是单字节标志位，不能按半字写
SEED = [(0x0004276C, 2, 0x0100), (0x00042786, 2, 0x0200), (0x0004274E, 2, 0x0080),
        (0x0003F25C, 2, 0x0000), (0x0003F815, 1, 0x02), (0x0003FAF2, 1, 0x00)]

WATCH = [0x0004276C, 0x00042786, 0x0004274E, 0x0003F25C,
         0x0003F811, 0x0003F81C, 0x0003F820]


def run(name, flag):
    data = open(os.path.join(BASE, name), "rb").read()
    m = Mem()
    m.load_rom(ROM, data)
    for a, n, v in SEED:
        m.wr(a, n, v)
    m.wr(0x0003F81C, 1, flag)
    m.all_writes.clear()
    cpu = Fr(m, TABLE)
    cpu.pc = START
    cpu.r[15] = SP0
    cpu.r[14] = SP0
    cpu.dr[1] = 0xDEADBEEF
    calls = []

    def on_call(c, tgt, pc):
        calls.append((pc, tgt, [c.r[i] for i in range(4)]))
        c.r[0] = 0
        return True

    def on_svc(c, num, pc):
        c.r[0] = 0
    cpu.call_hook = on_call
    cpu.svc_hook = on_svc

    for _ in range(2000):
        if cpu.halted or not (WIN[0] <= cpu.pc < WIN[1]):
            break
        cpu.step()
    final = {}
    for a in WATCH:
        final[a] = m.rd(a, 2)
    return dict(calls=calls, writes=list(m.all_writes), final=final,
                steps=cpu.steps, end=cpu.pc, io_reads=sum(m.io_reads.values()),
                unimpl=cpu.unimpl_seen)


def callstr(cs):
    return " → ".join(f"0x{t:06X}(r0=0x{r[0]:X},r1=0x{r[1]:X})" for _, t, r in cs)


if __name__ == "__main__":
    out = {}
    for flag in (0x00, 0x08, 0x40, 0x48):
        tag = f"bit3={1 if flag & 0x08 else 0} bit6={1 if flag & 0x40 else 0} (0x{flag:02X})"
        print("=" * 76)
        print(f"RAM[0x3F81C]: {tag}")
        r10, r11 = run("A3000110.bin", flag), run("A3000111.bin", flag)
        out[f"0x{flag:02X}"] = {"110": r10, "111": r11}
        for lbl, r in (("1.10", r10), ("1.11", r11)):
            print(f"  [{lbl}] {r['steps']:3d} 条，收尾 0x{r['end']:06X}，外设读 {r['io_reads']}")
            print(f"        调用: {callstr(r['calls'])}")
            final = ", ".join(f"[{a:06X}]=0x{v:X}" for a, v in r["final"].items() if v)
            print(f"        末态: {final}")
        # 差异
        c10, c11 = [t for _, t, _ in r10["calls"]], [t for _, t, _ in r11["calls"]]
        if set(c10) != set(c11):
            print(f"  ★ 调用集合不同：1.10 独有 {[f'0x{x:06X}' for x in set(c10)-set(c11)]}，"
                  f"1.11 独有 {[f'0x{x:06X}' for x in set(c11)-set(c10)]}")
    json.dump(out, open(os.path.join(_HERE, "exp111.json"), "w"),
              indent=1, default=str)
