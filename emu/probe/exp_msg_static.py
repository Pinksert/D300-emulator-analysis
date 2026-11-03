# -*- coding: utf-8 -*-
"""exp_msg_static.py —— 消息缓冲（mailbox）的可达性侦察（V17 第 1 步）

V16 收尾时留了一个明摆着的口子：**任务 5 从场景 A 到 C 一路卡在 `msg` 上**
（PC 0x0A6326，状态字 0x00400004），场景 C 跑满 11/12 个任务它还在那儿等。
flag 那条路已经证明是「握手闸门」，但消息是另一种同步原语，得单独算。

本脚本不解语义，只回答三个能静态回答的问题：

  1. `snd_msg`(-63) / `rcv_msg`(-61) 在 A 段各有几处、参数（mbx id / 消息指针）定不定；
  2. 消息缓冲控制块在哪 —— 内核 `rcv_msg` 实现里必然要按 id 索引一块内存；
  3. 静态上看，**有没有任何一处 `snd_msg` 是发给任务 5 等的那个信箱的**。

做法：线性扫 ROM 找 `int #0x40`，再往前回溯 24 条指令找 `ldi r12,#imm`
（固件写服务号只用 `ldi`，不用寄存器传；这条 V11 已经坐实过）。
回溯法比 CFG 便宜，且不会漏掉「不可达但是存在」的调用点 —— 后者正是我们要数的。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

import fr_emu                                    # noqa: E402

TABLE = fr_emu.TABLE
BASE = 0x00080000
ROM_PATH = os.path.join(os.path.dirname(_EMU), "data", "A3000111.bin")
if not os.path.exists(ROM_PATH):
    ROM_PATH = os.path.join(_EMU, "..", "nft", "Java", "Emulator", "A3000111.bin")
ROM = open(ROM_PATH, "rb").read()
END = BASE + len(ROM)

MSG_SVC = {-61: "rcv_msg", -63: "snd_msg", -60: "ref_mbx", -59: "pget_mbx"}


def dec(addr):
    return TABLE.decode(ROM, addr - BASE)


def sx(v, bits):
    return fr_emu.sx(v, bits)


def scan_ints():
    """线性扫全部 `int #0x40`，回溯 R12 / R4 / R5 的 ldi 常量。"""
    out = []
    for off in range(0, len(ROM) - 2, 2):
        ins = TABLE.decode(ROM, off)
        if ins is None:
            continue
        mnem, _b, v, size, _w = ins
        if mnem != "int" or v.get("u8") != 0x40:
            continue
        pc = BASE + off
        regs = {}
        back = pc
        for _ in range(28):
            back -= 2
            if back < BASE:
                break
            prev = dec(back)
            if prev is None:
                break
            pm, _pb, pv, _ps, _pw = prev
            if pm.startswith("ldi"):
                d = pv.get("ri")
                imm = pv.get("i8", pv.get("i20", pv.get("i32")))
                if d is not None and imm is not None and d not in regs:
                    regs[d] = sx(imm, 32) if imm & 0x80000000 else imm
            elif pm == "int":
                break
            # 其它会改 r12/r4/r5 的指令一律视为污染
            if pm in ("call", "call:d"):
                break
        out.append((pc, regs))
    return out


def main():
    ints = scan_ints()
    named = {}
    for pc, regs in ints:
        n = regs.get(12)
        if n is None:
            continue
        n = (n - 0x100000000) if n > 0x7FFFFFFF else n
        n &= 0xFF
        n = n - 0x100 if n & 0x80 else n
        named.setdefault(n, []).append((pc, regs))

    print("=" * 78)
    print("A 段 1.11 · `int #0x40` 总览（线性扫 + 回溯 ldi）")
    print("  ROM %d 字节（0x%08X–0x%08X）" % (len(ROM), BASE, END - 1))
    print("  `int #0x40` 共 %d 处；R12 常量可定 %d 处"
          % (len(ints), sum(len(v) for v in named.values())))
    print("\n  服务号分布（前 16）")
    for n in sorted(named, key=lambda x: -len(named[x]))[:16]:
        print("    %-4d ×%-4d 首处 0x%08X" % (n, len(named[n]), named[n][0][0]))

    print("=" * 78)
    print("消息相关服务")
    for n, name in sorted(MSG_SVC.items()):
        pcs = named.get(n, [])
        print("  %-8s (%3d)  %d 处" % (name, n, len(pcs)))
        for pc, regs in pcs[:12]:
            print("     0x%08X  r4=%-12s r5=%-12s r6=%s"
                  % (pc, _hx(regs.get(4)), _hx(regs.get(5)), _hx(regs.get(6))))
        if len(pcs) > 12:
            print("     … 另 %d 处" % (len(pcs) - 12))

    # ---- 内核实现：rcv_msg 的 id → 控制块换算
    print("=" * 78)
    print("内核侧对象换算（找 id 步长）")
    ids = [(pc, regs.get(4)) for pc, regs in named.get(-61, [])]
    print("  rcv_msg 的 mbx id（R4）取值：%s"
          % sorted({i for _, i in ids if i is not None}))
    ids2 = [(pc, regs.get(4)) for pc, regs in named.get(-63, [])]
    print("  snd_msg 的 mbx id（R4）取值：%s"
          % sorted({i for _, i in ids2 if i is not None}))
    both = {i for _, i in ids if i is not None} | {i for _, i in ids2 if i is not None}
    print("  两侧 id 交集：%s" % sorted(both))

    # ---- 控制块搜索：对象登记表 0xFE180 里 mbx 的记录
    print("=" * 78)
    print("对象登记表 0x000FE180（步长 0x10，+0x04 控制块指针，+0x08 对象号<<16）")
    off = 0x000FE180 - BASE
    for i in range(40):
        o = off + i * 0x10
        if o + 0x10 > len(ROM):
            break
        cb = int.from_bytes(ROM[o + 4:o + 8], "big")
        tag = int.from_bytes(ROM[o + 8:o + 12], "big")
        nex = int.from_bytes(ROM[o + 12:o + 16], "big")
        if cb == 0 and tag == 0:
            print("  #%-3d  空" % i)
            continue
        print("  #%-3d cb=0x%08X obj=0x%08X next=0x%08X"
              % (i, cb, tag, nex))


def _hx(x):
    return "-" if x is None else "0x%X" % x


if __name__ == "__main__":
    main()
