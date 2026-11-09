# -*- coding: utf-8 -*-
"""frbox.addrform —— 纯静态「访存地址形态」分类（V61）

只读 ROM，不建机器、不挂设备、不改任何运行状态。

FR 的访存指令有四类地址表达式，最容易混淆的是前两类：

===================  =================================  ==========================
字段                 地址表达式                          出现指令
===================  =================================  ==========================
``dir10/9/8``        ``@(dir * 4/2/1)``  **直接页**      ``dmov/dmovh/dmovb``
``disp10/9/8``       ``@(R14 + sx8(disp) * 4/2/1)``     ``ld/lduh/ldub/st/sth/stb``
``udisp6``           ``@(R15 + udisp6 * 4)``             ``ld/st``
``rj`` (+base)       ``@Rj`` / ``@(R13,Rj)``             多数 ``ld/st``
===================  =================================  ==========================

⚠️ ``disp*`` 是 **8 位有符号**（``fr_emu.py:658-661`` 统一走 ``sx(..., 8)``），
``disp8=252 (0xFC)`` 符号扩展后是 ``-4``，所以 ``ldub #252, r0`` 读的是
**栈帧 ``r14-4``**，不是直接页 ``0xFC``。V61 初版把两者混为一谈，
导致 ``bobserve`` 观察 0xFC 与 ``bpatch`` 打桩 0xFC 全部命中 0 次才暴露。
（同族：硬规矩 29 的 ``ld #1012`` = ``r14 + disp10*4``。）

⚠️ ``dir*`` 是**无符号**（``fr_emu.py:715-716`` 的 ``a = g(dk) * sc``），
只有 ``dmov*`` 才真的碰直接页。查「直接页 X 的读写点」前必须先分形态，
否则会把几百处栈帧局部变量访问误报成外设寄存器访问。

另：寄存器间接（``@Rj`` / ``@(R13,Rj)``）的地址运行时才算得出，
静态 ``Sweep.stores`` 对它们记为不可定址 —— 报「0 个写点」不等于没人写
（硬规矩 25/31）。
"""
from collections import namedtuple, Counter

#: V61 实证常量（见 `probe/exp_isr_v61.py` 与 `results/frbox/isr61.txt`）。
#: 探针与自检共用这一份，别各写一份。
ISR_VEC = 0x2B
ISR_ENTRY = 0x042E1C
ISR_RETI = 0x042E70
ISR_PC_COUNT = 37
ISR_BRANCH = 0x042E46
ISR_SKIPPED = (0x042E48, 0x042E4E)
ISR_PATCH_PCS = (0x042E40, 0x042E50)
GATE_SLOT = 0x6B000030
ICR_2B = 0x0000045B
DISPATCH_ENTRY = 0x17171E
STATUS_WINDOW = 0x65000000

#: 元素宽度：`disp10`/`dir10` 按 4 字节缩放，`9`→2，`8`→1
SCALE = {"disp10": 4, "disp9": 2, "disp8": 1,
         "dir10": 4, "dir9": 2, "dir8": 1}

#: `disp*` 一律按 8 位有符号解读（与 fr_emu.py:660 一致）
DISP_BITS = 8

Form = namedtuple("Form", "kind expr scale const addr")
Insn = namedtuple("Insn", "pc mn size form control vals")


def sx(v, bits=DISP_BITS):
    """把 `bits` 位无符号字段符号扩展成有符号整数。"""
    m = 1 << (bits - 1)
    return (v ^ m) - m


def classify(mn, v):
    """把一条已解码指令的 `vals` 归类成地址形态。

    返回 `Form(kind, expr, scale, const, addr)`：
      * ``kind``: ``direct_page`` / ``frame_rel`` / ``stack_rel`` /
        ``reg_indirect`` / ``none``
      * ``expr``: 人类可读的地址表达式（``r14-4`` / ``@0xFC`` ...）
      * ``addr``: 只有 `direct_page` 能静态算出绝对地址，其余为 ``None``
    """
    if not isinstance(v, dict):
        return Form("none", "", 0, None, None)

    # 真直接页：dir10/9/8 无符号，乘元素宽度
    for key in ("dir10", "dir9", "dir8"):
        if key in v:
            sc = SCALE[key]
            d = v[key]
            return Form("direct_page", "@dir*%d (%d)" % (sc, d), sc, d, d * sc)

    # 栈帧相对：disp* 有符号，乘元素宽度，基址 r14
    for key in ("disp10", "disp9", "disp8"):
        if key in v:
            sc = SCALE[key]
            raw = v[key]
            off = sx(raw, DISP_BITS) * sc
            expr = "r14%+d" % off if off else "r14"
            return Form("frame_rel", expr, sc, raw, None)

    if "udisp6" in v:
        d = v["udisp6"]
        return Form("stack_rel", "r15+%d" % (d * 4), 4, d, None)

    if "rj" in v:
        # 具体是 @Rj 还是 @(R13,Rj) 取决于 opcode base，静态层不下结论
        return Form("reg_indirect", "@Rj / @(R13,Rj)", 0, None, None)

    return Form("none", "", 0, None, None)


def _decode(rom, off):
    import fr_emu
    try:
        return fr_emu.TABLE.decode(rom, off)
    except Exception:
        return None


def _size(ins):
    return ins[3] if ins and ins[3] > 0 else 2


def isr_graph(rom, base, lo, hi):
    """线性反汇编 `[lo, hi)`，返回 `Insn` 列表。

    `control` 字段标出分支 / 调用 / 返回类助记符，其余为空串。
    用于从向量入口一路读到 `reti`，不依赖 `Sweep.enclosing()`
    （粗粒度归函数会错，硬规矩 30）。
    """
    out = []
    off, pc = lo - base, lo
    while pc < hi:
        ins = _decode(rom, off)
        if ins is None:
            out.append(Insn(pc, "?", 2, Form("none", "", 0, None, None), "", {}))
            pc += 2
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        size = _size(ins)
        control = mn if mn.startswith(("b", "call", "jmp", "ret", "int")) else ""
        out.append(Insn(pc, mn, size, classify(mn, v), control, v))
        pc += size
        off += size
    return out


def scan(rom, base, target=252):
    """全 ROM 线性扫，统计「立即数字段等于 target」的各形态命中。

    返回 ``(Counter, dict)``：Counter 按 ``kind/mn`` 计数；
    dict 按形态给出 ``[(pc, mn, expr)]`` 明细。
    用于回答「这个立即数到底出现在直接页指令还是栈帧相对指令里」。
    """
    cnt = Counter()
    detail = {}
    off, pc = 0, base
    while off < len(rom) - 1:
        ins = _decode(rom, off)
        if ins is None:
            pc += 2
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        size = _size(ins)
        if isinstance(v, dict):
            for key in ("disp10", "disp9", "disp8", "dir10", "dir9", "dir8"):
                if key in v and v[key] == target:
                    form = classify(mn, v)
                    tag = form.kind + "/" + key
                    cnt[tag] += 1
                    cnt["mn/" + mn] += 1
                    detail.setdefault(tag, []).append((pc, mn, form.expr))
        pc += size
        off += size
    return cnt, detail


def direct_page_hits(rom, base, page):
    """全 ROM 里真正读写直接页 `page` 的 PC（只认 dmov* 的 dir*）。"""
    out = []
    off, pc = 0, base
    while off < len(rom) - 1:
        ins = _decode(rom, off)
        if ins is None:
            pc += 2
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        size = _size(ins)
        form = classify(mn, v)
        if form.kind == "direct_page" and form.addr == page:
            out.append((pc, mn))
        pc += size
        off += size
    return out


def describe(form):
    """给报告用的一行说明。"""
    if form.kind == "frame_rel":
        return "栈帧相对 %s（不是直接页）" % form.expr
    if form.kind == "direct_page":
        return "直接页 0x%X" % form.addr
    if form.kind == "reg_indirect":
        return "寄存器间接（运行时才算得出）"
    return "非访存 / 其它"
