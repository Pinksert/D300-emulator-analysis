# -*- coding: utf-8 -*-
"""frbox.dispatch62 —— vec 0x2B 业务分派器的纯静态解剖（V62）

只读 ROM，不建机器、不挂设备、不改任何运行状态；所有函数都是纯函数。

V61 找到 vec 0x2B ISR 的唯一业务出口 `call 0x17171E`，并证明总闸是
`0x6B000030`。V62 接着解剖这个出口：它是一个**按位派发器** —— 先一次性
读 6 个 `0x65000000` 段状态字，再按 15 条位分支调用 15 个 handler。

⚠️ 三个口径纪律：

1. `call rel12` 的目标必须按 `fr_emu.py:777` 算
   ``pc + 2 + sx(rel12, 11) * 2`` —— 符号位是 **bit10**，11 位字段。
   `fr_dis` 用 bit8，不许拿它的 ``*NNN`` 文本推目标（硬规矩 40）。
2. `ld @(R13,Rj)` 的地址是 ``r13 + r[rj]``（硬规矩 25）。分派器把
   `0x65000000` 装进 `r4`、把偏移装进 `r13`，所以 6 次 `ld` 读的是
   `0x65000008/10/104/10C/114/11C`，不是 6 条无关的指令。
3. 「静态能定址的写点」只覆盖可定址立即数形态；寄存器间接写看不见
   （硬规矩 25/31）。报「0 个写点」不等于没人写。

V62 实证（见 `probe/exp_dsp_v62.py` / `results/frbox/dsp62.txt`）：
  * 6 个状态字 / 15 个 call 目标 / 10 条短路 bra；
  * `0x65000000` 段 47 个静态写点（42 个前带 `or` 置位）⇒ 固件自维护；
  * `0x6B000020/30/34` 四个引用点全部静态 0 写点 ⇒ 外部输入；
  * 15 个 handler 没有一个静态引用显示区 `0x120000..0x140000`；
  * 分派器是**优先级短路**结构：`rel12` 分支命中后紧跟 `bra` 到
    `0x171830`（函数尾），一次中断不是 15 条位分支全派发。D2 把 6 个
    状态字全打桩成 1 也只命中 2 个 handler 入口。
"""
from collections import namedtuple

#: vec 0x2B ISR 的唯一业务出口（V61 KI#230）
DISPATCH_ENTRY = 0x17171E
DISPATCH_RET = 0x171834
#: 优先级短路：10 条 `bra` 全指向函数尾 0x171830（ld / ldm1 / ret）
BRA_TAIL = 0x171830
BRA_COUNT = 10
FIRST_HANDLERS = (0x171836, 0x1714EE)
STATUS_BASE = 0x65000000
GATE_SLOT = 0x6B000030

#: 6 个状态字：(偏移, 读它的 PC, 读进哪个寄存器)
STATUS_OFFSETS = (0x08, 0x10, 0x104, 0x10C, 0x114, 0x11C)
STATUS_READ_PCS = (0x17172A, 0x17172E, 0x171734,
                   0x17173A, 0x171740, 0x171750)
STATUS_REGS = (8, 5, 6, 9, 10, 4)

#: 15 个 handler 入口（11 条 call rel12 + 4 条 call @r12）
HANDLERS = (0x1714EE, 0x171836, 0x17184C, 0x171862, 0x1718D8,
            0x171AF0, 0x171BC0, 0x171C2E, 0x171D3C, 0x171EFE,
            0x171FCE, 0x17209A, 0x1721E0, 0x1722C4, 0x17232A)

#: 外部输入槽：三个同构 ISR 各读一个（V61 只看了中间那个）
GATE_SLOTS = (0x6B000020, 0x6B000030, 0x6B000034)
SIBLING_ISR_PC = 0x042E82
SIBLING_ISR_TARGET = 0x1A4902

#: handler 家族共同围绕的控制块（V55 KI#208 的 `0x9F59F7C8` 近邻）
HANDLER_CB = 0x9F59EF50

#: 显示链 5 个关键 PC（V60 起沿用，口径不变）
DSP_PCS = (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752)

Insn = namedtuple("Insn", "pc mn size vals")
Call = namedtuple("Call", "pc kind target")
Read = namedtuple("Read", "pc off reg")
Row = namedtuple("Row", "pc mask and_src and_dst beq_pc beq_target "
                        "call_kind call_target")


def sx(v, bits=11):
    """把 `bits` 位无符号字段符号扩展成有符号整数。"""
    m = 1 << (bits - 1)
    return (v ^ m) - m


def rel12_target(pc, rel12):
    """`call rel12` 目标，与 `fr_emu.py:777` 逐字一致。"""
    return (pc + 2 + sx(rel12, 11) * 2) & 0xFFFFFFFF


def rel9_target(pc, rel9):
    """`beq/bra rel9` 目标（V61 实证：rel9=4 → pc+0xA）。"""
    return (pc + 2 + rel9 * 2) & 0xFFFFFFFF


def _decode(rom, off):
    import fr_emu
    try:
        return fr_emu.TABLE.decode(rom, off)
    except Exception:
        return None


def decode_range(rom, base, lo, hi):
    """线性反汇编 `[lo, hi)`（硬规矩 21：从头线性扫，不靠锚点重同步）。"""
    out = []
    off, pc = lo - base, lo
    while pc < hi:
        ins = _decode(rom, off)
        if ins is None:
            out.append(Insn(pc, "?", 2, {}))
            pc += 2
            off += 2
            continue
        mn, _b, v, size, _w = ins
        size = size if size and size > 0 else 2
        out.append(Insn(pc, mn, size, v if isinstance(v, dict) else {}))
        pc += size
        off += size
    return out


def call_targets(insns):
    """把 `call rel12` 与 `call @rN` 混排的调用点解成绝对目标。

    `call @rN` 向上找最近一条 `ldi:32` 写同一个 `ri` 的指令取立即数；
    遇到 `ret` / `bra` 就停，不跨函数边界找。
    """
    out = []
    for i, ins in enumerate(insns):
        if not ins.mn.startswith("call"):
            continue
        v = ins.vals
        if "rel12" in v:
            out.append(Call(ins.pc, "rel12", rel12_target(ins.pc, v["rel12"])))
            continue
        reg = v.get("ri")
        imm = None
        if reg is not None:
            for j in range(i - 1, -1, -1):
                p = insns[j]
                if p.mn.startswith("ret") or p.mn.startswith("bra"):
                    break
                if p.mn.startswith("ldi") and p.vals.get("ri") == reg \
                        and "i32" in p.vals:
                    imm = p.vals["i32"]
                    break
        out.append(Call(ins.pc, "reg@r%d" % reg if reg is not None else "reg", imm))
    return out


def status_reads(insns, base=STATUS_BASE):
    """从 `ldi:32 #base,rN` + `ldi:8/20 #off,r13` + `ld @rN` 配出状态字读点。

    只认基址寄存器等于装载 `base` 的那个寄存器的 `ld`；
    `ldm1` / 栈帧读不会被算进来。
    """
    out = []
    base_reg, pending_off = None, None
    for ins in insns:
        v = ins.vals
        if ins.mn.startswith("ldi") and v.get("i32") == base:
            base_reg = v.get("ri")
        elif ins.mn.startswith("ldi"):
            for k in ("i8", "i20"):
                if k in v:
                    pending_off = v[k]
                    break
        elif ins.mn in ("ld", "ldub", "lduh", "ldh") and base_reg is not None \
                and v.get("rj") == base_reg and pending_off is not None:
            out.append(Read(ins.pc, pending_off, v.get("ri")))
    return out


def mask_chain(insns):
    """把「ldi mask → and → beq → call」串成按位派发表。

    多级条件（如 `0x1717A8` + `0x1717B0` 两级 `and`）只记录**最近一级**
    `and` 的 mask 与源/目的寄存器；报告里另行说明嵌套。
    """
    rows = []
    cur_mask = None
    last_and = None       # (pc, mask, rj, ri)
    last_beq = None       # (pc, target)
    calls = dict((c.pc, c) for c in call_targets(insns))
    for ins in insns:
        v = ins.vals
        if ins.mn.startswith("ldi"):
            for k in ("i32", "i20", "i8"):
                if k in v and v.get("ri") in (0, 1):
                    cur_mask = v[k]
                    break
        elif ins.mn == "and":
            last_and = (ins.pc, cur_mask, v.get("rj"), v.get("ri"))
        elif ins.mn in ("beq", "beq:d", "bne", "bne:d"):
            last_beq = (ins.pc, rel9_target(ins.pc, v["rel9"])
                        if "rel9" in v else None)
        elif ins.mn.startswith("call") and ins.pc in calls:
            c = calls[ins.pc]
            a = last_and or (None, None, None, None)
            b = last_beq or (None, None)
            rows.append(Row(ins.pc, a[1], a[2], a[3], b[0], b[1],
                            c.kind, c.target))
            last_beq = None
    return rows


def handler_bounds(rom, base, target, max_size=0x600):
    """返回一个 handler 的 `[入口, 第一条 ret 之后一条)` 边界。"""
    insns = decode_range(rom, base, target, target + max_size)
    for i, ins in enumerate(insns):
        if ins.mn.startswith("ret"):
            end = insns[min(i + 1, len(insns) - 1)].pc
            return target, end, insns[:min(i + 2, len(insns))]
    return target, insns[-1].pc if insns else target, insns


def imm_refs(insns, lo, hi):
    """区间内落在 `[lo, hi)` 的可定址立即数引用（i32 / i20 / i8）。"""
    out = []
    for ins in insns:
        for k in ("i32", "i20", "i8"):
            x = ins.vals.get(k)
            if isinstance(x, int) and lo <= x < hi:
                out.append((ins.pc, k, x))
    return out
