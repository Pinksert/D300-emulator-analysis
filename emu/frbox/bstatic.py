# -*- coding: utf-8 -*-
"""bstatic.py —— 全 ROM 静态调用点普查（V48）。

## 为什么要有这个模块

到 V47 为止，本项目回答「谁置了某个 flag」只有一条路：**动态跑一遍，看 flow 记到了什么**。
这条路有两个死角：

1. **没被执行过的代码看不见**。V47 的结论是「`set_flg` 从没碰过 flag 6」——但那只说明
   跑过的 17 个任务没碰，不代表 ROM 里没有这句代码。
2. **问不出「谁调用某个函数」**。动态只能看「这次谁调了」，静态才能看「谁可能调」。

V48 把这件事做成了一次性设施：**从头线性扫一遍 ROM，记下每一个 `int #0x40`
调用点的服务号与实参、每一条 `call` 边的起终点、每一条能算出地址的 store。**

## 为什么是「线性扫」而不是「锚点往前重同步」

V47 草稿干过一次「找到 `1f 40`（`int #0x40`）再往前重同步解码」，2300 个锚点里
只捞出 **2 个**调用点。原因不是解码器坏，是**服务号 r12 会连续几次调用共用**：

    0x12214A  call:d @r12        ; r12 = 0x125992，这里是**跳转目标**
    0x122158  ldi:8 r12, #0xD2   ; 四条指令之后才重新装服务号
    0x12215C  int #0x40          ; wai_flg

往前的窗口里根本没有那条 `ldi:8 r12`，而 r12 在 `call:d` 里当的是**代码地址**。
FR30 是 16 位定长（`ldi:32` 由解码器给出 6 字节），所以**从头线性走**是可行的，
只要记住「谁最后写了哪个寄存器」。

## 写寄存器模型（沿用 MEMORY 硬规矩 11）

    看解码字段 `ri` + 最后一个操作数；`cmp/btst/band/bor/st/call/bra/ret/int` 不算写。

`mov` 的 `rj/ri` 方向本项目读不准（KI#168），所以**两个都当写**（保守 = 宁可未知）。

## ⚠️ 边界（写报告必须带上）

* **只能看见「立即数实参」**。若某处 `set_flg` 的 flgid 是从内存/参数寄存器来的，
  本模块记成 `None`（未知），不能据此断言「它不置 flag 6」。
* **不是 CFG**。线性扫不区分「可达 / 不可达」，也不跟踪分支；它回答「字节里写了什么」，
  不回答「这段会不会被执行」—— 后者要动态（`probe/exp_bfree.py` D 段）。
* **`call` 边只收 `call:d @r12` 且 r12 已知常量**的那一种。这条固件里绝大多数调用都是
  `ldi:32 r12,#addr` + `call:d @r12`，所以覆盖率不低；但直接 `call rel12` 与
  寄存器传参的调用**不在**图里。
* **`stores` 只收基址是已知常量的那一种**（`ldi:32 rX,#addr` 之后 `st/stb/sth`）。
  基址来自参数或内存的 store 收不到。
"""

from . import machine as _machine

#: 解码时跳过的填充字节对（不是合法指令）
SKIP2 = (b"\x00\x00", b"\xff\xff")

#: 不写通用寄存器的助记符（硬规矩 11）
NONWRITE = frozenset({
    'st', 'stb', 'sth', 'cmp', 'btst', 'band', 'bor', 'beor', 'bclr', 'bset',
    'call', 'call:d', 'bra', 'beq', 'bne', 'bhi', 'bls', 'bcc', 'bcs', 'bvc',
    'bvs', 'bnv', 'bv', 'bge', 'blt', 'bgt', 'ble', 'jmp', 'ret', 'reti',
    'int', 'nop', 'enter', 'leave', 'extsb', 'extub', 'ext',
})

#: `int #0x40` = REALOS 服务入口
SVC_VEC = 0x40

#: 服务号（低 8 位补码，见 MEMORY：服务号 = R12 低 8 位，必须按 32 位补码读）
SET_FLG = -48
CLR_FLG = -47
WAI_FLG = -46
TWAI_FLG = -22
SEM_WAI = -53
SEM_SIG = -55

#: 实参口径（**由 V47 动态证据定死，不是照抄印刷文本**）
#:   set_flg / clr_flg : r4 = flgid, r5 = bits
#:   wai_flg / twai_flg: r5 = flgid, r6 = pattern, r7 = mode
ARG_ORDER = {
    SET_FLG: ('flg', 'bits'), CLR_FLG: ('flg', 'bits'),
    WAI_FLG: ('', 'flg', 'ptn', 'mode'), TWAI_FLG: ('', 'flg', 'ptn', 'mode'),
    SEM_WAI: ('sem',), SEM_SIG: ('sem',),
    -24: ('',),           # get_tid
    -27: ('tsk', 'pri'),  # chg_pri
    -85: ('ticks',),      # dly_tsk
}

#: 主循环门字节（V25 定，见 `bgate.py`）：非 0 = 干活分支
GATE = 0x9F58D08F
#: 分配器（V47 定，见 `bpool.py`）：等 flag 6 的 bit5
POOL_FLAG = 6
POOL_BIT = 0x20


def sx8(v):
    """低 8 位补码 → 服务号。V47 的 `wai_flg(6, 0x20)` 就是这么读出来的。"""
    return v - 0x100 if v & 0x80 else v


class CallSite(object):
    """一个 `int #0x40` 调用点。未知实参为 `None`。"""

    __slots__ = ('addr', 'svc', 'r4', 'r5', 'r6', 'r7')

    def __init__(self, addr, svc, r4, r5, r6, r7):
        self.addr, self.svc = addr, svc
        self.r4, self.r5, self.r6, self.r7 = r4, r5, r6, r7

    # ---- 按服务各自的口径取实参
    @property
    def flgid(self):
        """事件标志号：set/clr 看 r4，wai/twai 看 r5。"""
        if self.svc in (SET_FLG, CLR_FLG):
            return self.r4
        if self.svc in (WAI_FLG, TWAI_FLG):
            return self.r5
        return None

    @property
    def bits(self):
        if self.svc in (SET_FLG, CLR_FLG):
            return self.r5
        if self.svc in (WAI_FLG, TWAI_FLG):
            return self.r6
        return None

    @property
    def mode(self):
        if self.svc in (WAI_FLG, TWAI_FLG):
            return self.r7
        return None

    def args(self):
        """按 `ARG_ORDER` 拼出 (名字, 值) 列表，名字为空表示该寄存器不当参数。

        `raw` 与 `order` 都从 r4 起对齐：`order[0]` 说的就是 r4。
        """
        order = ARG_ORDER.get(self.svc)
        raw = (self.r4, self.r5, self.r6, self.r7)
        if order is None:
            return [('r%d' % (4 + i), raw[i]) for i in range(4)]
        return [(n, raw[i]) for i, n in enumerate(order) if n]

    def __repr__(self):
        return '<CallSite 0x%06X svc=%s %s>' % (
            self.addr, self.svc, ' '.join('%s=%s' % (n, hex(v) if isinstance(v, int) else v)
                                          for n, v in self.args()))


class Sweep(object):
    """一次全 ROM 线性扫的结果。"""

    #: 覆盖表覆盖不到的地址（ROM 基址由 machine 提供）
    def __init__(self, rom, base):
        self.rom = rom
        self.base = base
        self.calls = []      # [CallSite]
        self.edges = {}      # 目标地址 -> [调用点地址]
        self.funcs = []      # `enter` 指令地址（升序）
        self.stores = []     # (pc, addr, mnemonic)
        self._sweep()

    # ------------------------------------------------------------ 扫描
    def _sweep(self):
        import fr_emu
        rom, base = self.rom, self.base
        regs = [None] * 16
        off, n = 0, len(rom)
        while off < n - 1:
            if rom[off:off + 2] in SKIP2:
                off += 2
                continue
            ins = fr_emu.TABLE.decode(rom, off)
            if ins is None:
                off += 2
                continue
            mn, _bs, v, size, _w = ins
            addr = base + off

            if mn == 'int' and v.get('u8') == SVC_VEC:
                svc = None if regs[12] is None else sx8(regs[12] & 0xFF)
                self.calls.append(CallSite(addr, svc, regs[4], regs[5],
                                           regs[6], regs[7]))
            elif mn == 'call:d' and v.get('ri') is not None:
                tgt = regs[v['ri']]
                if tgt is not None:
                    self.edges.setdefault(tgt, []).append(addr)
            elif mn == 'enter':
                self.funcs.append(addr)
            elif mn in ('st', 'stb', 'sth'):
                rb = v.get('rj')
                if rb is None:
                    rb = v.get('ri')
                disp = (v.get('disp8') or v.get('disp9') or v.get('disp10') or 0)
                if rb is not None and regs[rb] is not None:
                    self.stores.append((addr, regs[rb] + disp, mn))

            if mn not in NONWRITE:
                ri = v.get('ri')
                if mn.startswith('ldi'):
                    imm = v.get('i8')
                    if imm is None:
                        imm = v.get('i20')
                    if imm is None:
                        imm = v.get('i32')
                    if ri is not None:
                        regs[ri] = imm
                elif mn == 'mov':
                    if ri is not None:
                        regs[ri] = None
                    rj = v.get('rj')
                    if rj is not None:
                        regs[rj] = None
                elif ri is not None:
                    regs[ri] = None
            off += size

    # ------------------------------------------------------------ 查询
    def of(self, svc):
        """某服务号的全部调用点。"""
        return [c for c in self.calls if c.svc == svc]

    def flag_sets(self, flgid=None):
        """`set_flg` 调用点，可按 flgid 过滤。"""
        return [c for c in self.of(SET_FLG) if flgid is None or c.flgid == flgid]

    def flag_waits(self, flgid=None):
        return [c for c in self.of(WAI_FLG) if flgid is None or c.flgid == flgid]

    def enclosing(self, addr):
        """最近的（不大于 addr 的）`enter` —— 粗粒度函数归属。"""
        import bisect
        i = bisect.bisect_right(self.funcs, addr) - 1
        return self.funcs[i] if i >= 0 else None

    def callers_of(self, addr):
        return list(self.edges.get(addr, ()))

    def writers_of(self, addr):
        """能算出确切地址、且等于 addr 的 store。"""
        return [(pc, mn) for pc, a, mn in self.stores if a == addr]

    def writers_near(self, addr, span=0x20):
        return sorted(((a, pc, mn) for pc, a, mn in self.stores
                       if abs(a - addr) <= span), key=lambda x: x[0])

    # ------------------------------------------------------------ 总账
    def summary(self):
        svc = {}
        for c in self.calls:
            svc[c.svc] = svc.get(c.svc, 0) + 1
        return {
            'calls': len(self.calls),
            'unknown_svc': svc.get(None, 0),
            'edges': sum(len(v) for v in self.edges.values()),
            'targets': len(self.edges),
            'funcs': len(self.funcs),
            'stores': len(self.stores),
            'by_svc': svc,
        }


def sweep(m):
    """对一台已载入 ROM 的机器做全 ROM 静态普查（约 50 秒，7 MB）。"""
    return Sweep(m.bus.rom, m.bus.rom_base)


def sweep_of_type(name='d300-b'):
    """自己造一台（只为拿 ROM），省得外部再 load 一次。"""
    m = _machine.create(name)
    m.load()
    return sweep(m), m
