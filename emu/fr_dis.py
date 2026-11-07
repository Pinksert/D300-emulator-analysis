# -*- coding: utf-8 -*-
"""FR 反汇编文本格式化（配合 fr_emu.TABLE 使用）"""
import sys
from fr_emu import TABLE, sx

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")


SCALE = {"disp10": 4, "disp9": 2, "disp8": 1, "udisp6": 4, "u10": 4,
         "dir10": 4, "dir9": 2, "dir8": 1}

#: `TABLE.decode` 会把 ifmt 编号一并塞进 vals（`op1..op4`），那是**指令格式号**，
#: 不是操作数。不过滤掉会输出成 `enter op1=0, op2=15, #8` 这种噪声（V51 修）。
NOISE = ("op1", "op2", "op3", "op4")


def fmt_val(k, raw):
    if k == "m4":
        return f"#{raw - 16}"
    if k in SCALE:
        return f"#{raw * SCALE[k]}"
    if k == "s10":
        return f"#{sx(raw, 8) * 4}"
    if k == "rel9":
        return f"*{sx(raw, 8) * 2}"
    if k == "rel12":
        return f"*{sx(raw, 11) * 2}"
    if k == "i8":
        return f"#0x{raw:X}"
    if k == "i20":
        return f"#0x{raw:X}"
    if k == "i32":
        return f"#0x{raw:X}"
    if k == "u4" or k == "u4c":
        return f"#0x{raw:X}"
    if k == "u8":
        return f"#0x{raw:X}"
    if k.startswith("reglist"):
        off = 8 if k.endswith("hi-ld") or k.endswith("hi-st") else 0
        return "{" + ",".join(f"r{i + off}" for i in range(8) if raw >> i & 1) + "}"
    if k == "ri" or k == "rj":
        return f"r{raw}"
    if k == "rs1" or k == "rs2":
        return f"dr{raw}"
    return f"{k}={raw}"


def line(data, off, base_addr):
    """-> (text, size) ；off 为文件偏移

    ⚠️ 操作数顺序：本函数按 `v.items()` 的**字段定义序**输出，而 `TABLE.decode`
    插入顺序是 `op1, op2, rj, ri`。所以 `ld/st` 会显示成 `st r4, r0`，
    而 FR 手册的语义是 `st Ri, @(R13, Rj)` —— **`ri` 才是数据寄存器，`rj` 是索引**，
    读的时候要反过来理解（实测依据见 `fr_emu.py:684-708`：写内存用的是 `r[g("ri")]`，
    基址/索引用的是 `g("rj")`）。

    V51 只过滤了 `op1..op4` 噪声，没有重排操作数 —— 历史结果文件都是这个序，
    改了会让新旧输出不可比。要重排请连同旧结果一起重生成。
    """
    ins = TABLE.decode(data, off)
    if ins is None:
        return f"{base_addr + off:08X}  {data[off:off+2].hex()}          ??", 2
    mn, bs, v, size, w = ins
    raw = data[off:off + size].hex()
    ops = ", ".join(fmt_val(k, x) for k, x in v.items() if k not in NOISE)
    return f"{base_addr + off:08X}  {raw:<14} {mn:<8} {ops}", size


def disasm(data, start, end, base_addr):
    """start/end 是**文件偏移**；base_addr 只用于显示"""
    out, off = [], start
    while off < end:
        t, sz = line(data, off, base_addr)
        out.append(t)
        off += sz
    return out


def disasm_addr(data, addr, end_addr, base_addr):
    """按**地址**反汇编（内部换算成偏移，避免混用）"""
    return disasm(data, addr - base_addr, end_addr - base_addr, base_addr)
