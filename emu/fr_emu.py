# -*- coding: utf-8 -*-
"""
FR30/FR60/FR80 指令级解释器（函数级仿真台核心）
================================================
用途：不跑整机，只把 D300 A 固件里某个函数拿来单跑，配合 MMIO 打桩做可控实验。

指令表来源：binutils 的 cpu/fr30.cpu + opcodes/fr30-opc.c（CGEN 生成物）
语义依据：fr30.cpu 里的 (dni ...) / define-pmacro 定义
  · 目标操作数一律是「最后一个操作数」（如 `add Rj,Ri` = Ri ← Ri + Rj）
  · 延迟分支：带 :d 的分支，其后一条指令必定先执行
  · 位测试立即数是「掩码」不是位号：btstl #8 = bit3，btsth #4 = 0x40 = bit6

地址空间（D300 A 段）：
  ROM  = 0x00080000 .. +524290   （A3000110/111.bin，只读）
  MMIO = 0x00000000 .. 0x00001000（默认读 0、写丢弃，全部记日志）
  其余 = RAM（稀疏，初值 0）
"""
import re, sys

HERE = r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp"
M32 = 0xFFFFFFFF


def sx(v, bits):
    """按 bits 位宽做符号扩展"""
    if v & (1 << (bits - 1)):
        return v - (1 << bits)
    return v


# ---------------------------------------------------------------- 指令表
# FR80/FR81 专有、binutils 的 fr30 指令表里没有的三条位检索指令。
# 依据：NikonEmulator 的 FrInstructionSet.java 明确标注 "FR80/FR81 only"，
# 且 D300 固件 0x00040E20 处真的执行到 SRCH1（IRQ 0x3F 的处理程序里）。
FR80_ONLY = {0x97C0: "srch0", 0x97D0: "srch1", 0x97E0: "srchc"}


def _norm(n):
    n = n.lower()
    for p in ("f_", "f-"):
        if n.startswith(p):
            n = n[2:]
            break
    return n.replace("_", "-")


class InsnTable:
    def __init__(self, cpu_file, opc_file):
        cpu = open(cpu_file, "r", encoding="utf-8", errors="replace").read()
        opc = open(opc_file, "r", encoding="utf-8", errors="replace").read()
        self.fields = {}
        for m in re.finditer(r'^\((?:dnf|df|dnop)\s+(\S+)\s+"[^"]*"\s*\([^)]*\)\s+(\d+)\s+(\d+)',
                             cpu, re.M):
            self.fields[m.group(1)] = (int(m.group(2)), int(m.group(3)))
        self.multi = {}
        for m in re.finditer(r'^\(dnmf\s+(\S+)\s+"[^"]*"\s*\([^)]*\)\s+\w+\s*\n\s*\(([^)]*)\)',
                             cpu, re.M):
            self.multi[m.group(1)] = m.group(2).split()
        self.field_by_norm = {_norm(k): k for k in self.fields}
        self.field_by_norm.update({_norm(k): k for k in self.multi})
        self.ifmt = {}
        for m in re.finditer(r'static const CGEN_IFMT (ifmt_\w+) ATTRIBUTE_UNUSED = \{\s*'
                             r'(\d+),\s*(\d+),\s*(0x[0-9a-fA-F]+),\s*\{(.*?)\}\s*\};', opc, re.S):
            self.ifmt[m.group(1)] = (int(m.group(3)), int(m.group(4), 16),
                                     re.findall(r'F \((\w+)\)', m.group(5)))
        self.idx = {}
        for m in re.finditer(r'/\*\s*([a-z0-9:_.\-]+)(?:\s+([^*]*?))?\s*\*/\s*\{\s*\{[^}]*\},\s*'
                             r'\{(.*?)\},\s*&\s*(ifmt_\w+),\s*\{([^}]*)\}\s*\}', opc, re.S):
            mnem, fm, base = m.group(1), m.group(4), m.group(5)
            if fm not in self.ifmt:
                continue
            bitsize, mask, fs = self.ifmt[fm]
            b = int(re.findall(r'0x([0-9a-fA-F]+)', base)[0], 16)
            key = (bitsize, mask, b)
            if key not in self.idx:          # 同一编码的别名只留第一条
                self.idx[key] = (mnem, fs)
        self.keys = sorted(self.idx.keys(), key=lambda k: (-k[0], -bin(k[1]).count("1")))

    def fval(self, w, key, bitsize):
        if key in self.multi:
            v = 0
            for p in self.multi[key]:
                st, ln = self.fields[p]
                v = (v << ln) | ((w >> (bitsize - st - ln)) & ((1 << ln) - 1))
            return v
        st, ln = self.fields[key]
        return (w >> (bitsize - st - ln)) & ((1 << ln) - 1)

    def decode(self, data, off):
        """-> (mnem, base, vals, size, word) 或 None"""
        for bitsize in (48, 32, 16):
            nb = bitsize // 8
            if off + nb > len(data):
                continue
            w = int.from_bytes(data[off:off + nb], "big")
            w16 = (w >> (bitsize - 16)) & 0xFFFF
            for k in self.keys:
                if k[0] != bitsize:
                    continue
                if (w16 & k[1]) == k[2]:
                    mnem, fs = self.idx[k]
                    vals = {}
                    for f in fs:
                        fk = self.field_by_norm.get(_norm(f))
                        if fk:
                            vals[_norm(f)] = self.fval(w, fk, bitsize)
                    return mnem, k[2], vals, nb, w
        if off + 2 <= len(data):
            w16 = int.from_bytes(data[off:off + 2], "big")
            if (w16 & 0xFFF0) in FR80_ONLY:
                return FR80_ONLY[w16 & 0xFFF0], 0x97C0, {"ri": w16 & 0xF}, 2, w16
        return None


# ---------------------------------------------------------------- 内存
class Mem:
    def __init__(self):
        self.rom = b""
        self.rom_base = 0
        self.rom_end = 0
        self.ram = {}                 # addr -> int(0..255)
        self.mmio_end = 0x1000        # 低于此地址视为外设
        self.io_reads = {}            # addr -> 次数
        self.io_writes = []           # (addr, size, val)
        self.io_value = {}            # addr -> 强制返回值（打桩用）
        self.io_after = {}            # addr -> (次数阈值, 到阈值后返回的值)  轮询逃生阀
        self.watch = set()
        self.access_log = []          # (kind, addr, size, val, pc)
        self.all_writes = []          # 全部写操作（含 RAM），(addr, size, val, pc)

    def load_rom(self, base, data):
        self.rom, self.rom_base, self.rom_end = data, base, base + len(data)

    def _raw(self, a):
        if self.rom_base <= a < self.rom_end:
            return self.rom[a - self.rom_base]
        if a in self.ram:
            return self.ram[a]
        return 0

    def rd(self, a, n, pc=0):
        a &= M32
        if a < self.mmio_end:                       # 外设
            self.io_reads[a] = self.io_reads.get(a, 0) + 1
            if a in self.io_after:
                thr, val = self.io_after[a]
                if self.io_reads[a] >= thr:
                    v = val
                elif a in self.io_value:
                    v = self.io_value[a]
                else:
                    v = 0
            else:
                v = self.io_value.get(a, 0)
            if a in self.watch:
                self.access_log.append(("R", a, n, v, pc))
            return v & M32
        v = 0
        for i in range(n):
            v = (v << 8) | self._raw(a + i)
        if a in self.watch:
            self.access_log.append(("R", a, n, v, pc))
        return v

    def wr(self, a, n, v, pc=0):
        a &= M32
        v &= M32
        if a < self.mmio_end:
            self.io_writes.append((a, n, v, pc))
            if a in self.watch:
                self.access_log.append(("W", a, n, v, pc))
            return
        if self.rom_base <= a < self.rom_end:
            self.io_writes.append((a, n, v, pc))     # 写 ROM：记下来但不改
            return
        for i in range(n):
            self.ram[(a + i) & M32] = (v >> (8 * (n - 1 - i))) & 0xFF
        self.all_writes.append((a, n, v, pc))
        if a in self.watch:
            self.access_log.append(("W", a, n, v, pc))


class Unimpl(Exception):
    pass


# ---------------------------------------------------------------- CPU
class Fr:
    DR_NAMES = ["TBR", "RP", "SSP", "USP", "MDH", "MDL"]

    def __init__(self, mem, table):
        self.m = mem
        self.t = table
        self.r = [0] * 16
        self.pc = 0
        self.c = self.v = self.z = self.n = 0          # CCR 低 4 位
        self.i = 0                                     # CCR bit4（中断允许，FR80）
        self.ilm = 0                                   # 中断屏蔽（5 位）
        self.s = self.t_ = self.d0 = self.d1 = 0       # CCR bit5(S) + SCR bit0..2
        self.dr = [0] * 6                              # TBR RP SSP USP MDH MDL
        # SSP(dr2)/USP(dr3) 是权威值；R15 永远等于当前 S 位选中的那一支
        self.irq_pending = []                          # [(level, vec)] 待处理硬件中断
        self.irq_log = []                              # (vec, level, pc) 已受理
        self.eit_hook = None                           # hook(cpu, vec) -> True 表示不真跳转
        self.mdh = 0
        self.mdl = 0
        self.delay = None
        self.steps = 0
        self.halted = False
        self.halt_reason = ""
        self.trace = []                                # (pc, word, size, mnem, vals)
        self.trace_on = False
        self.unimpl_seen = {}
        self.svc_hook = None                           # μITRON 服务调用钩子：hook(cpu, num, pc)
        self.int40 = 0                                 # 执行到的 int #0x40 条数（含被钩子截住的）
        self.call_hook = None                          # 子程序调用钩子：hook(cpu, target, pc) -> True 表示打桩

    # ---- PS ----
    # 布局按富士通 CM71-00104-3E p29（与 NikonEmulator 的 FrCPUState.getPS() 一致）：
    #   PS = (ILM << 16) | (SCR << 8) | CCR
    #   CCR: C0 V1 Z2 N3 I4 S5       SCR: T0 D0_1 D1_2
    @property
    def ps(self):
        ccr = ((self.s << 5) | (self.i << 4) | (self.n << 3) | (self.z << 2)
               | (self.v << 1) | self.c)
        scr = (self.d1 << 2) | (self.d0 << 1) | self.t_
        return ((self.ilm & 0x1F) << 16) | (scr << 8) | ccr

    @ps.setter
    def ps(self, v):
        self.c = v & 1
        self.v = (v >> 1) & 1
        self.z = (v >> 2) & 1
        self.n = (v >> 3) & 1
        self.i = (v >> 4) & 1
        new_s = (v >> 5) & 1
        if new_s != self.s:                    # S 位决定 R15 走 SSP(0) 还是 USP(1)
            if self.s == 0:
                self.dr[2] = self.r[15]        # 存回 SSP
            else:
                self.dr[3] = self.r[15]        # 存回 USP
            self.s = new_s
            self.r[15] = self.dr[3] if new_s == 1 else self.dr[2]
        self.t_ = (v >> 8) & 1
        self.d0 = (v >> 9) & 1
        self.d1 = (v >> 10) & 1
        self.ilm = (v >> 16) & 0x1F

    # ---- 栈指针两支 ----
    def _get_sp(self, bank):                   # bank: 0=SSP 1=USP
        if self.s == bank:
            return self.r[15]
        return self.dr[2] if bank == 0 else self.dr[3]

    def _set_sp(self, bank, val):
        val &= M32
        if self.s == bank:
            self.r[15] = val
        elif bank == 0:
            self.dr[2] = val
        else:
            self.dr[3] = val

    # ---- 标志位 ----
    def _set_zn(self, r):
        self.z = 1 if (r & M32) == 0 else 0
        self.n = (r >> 31) & 1
        return r

    def _add(self, a, b, cin=0):
        s = a + b + cin
        r = s & M32
        self.c = 1 if s > M32 else 0
        self.v = 1 if ((a ^ r) & (b ^ r) & 0x80000000) else 0
        return self._set_zn(r)

    def _sub(self, a, b, bin_=0):
        """a - b - bin_；FR 的 C 是「借位」（a<b 时为 1），不是进位"""
        s = a + (b ^ M32) + (1 - bin_)
        r = s & M32
        self.c = 0 if s > M32 else 1
        self.v = 1 if ((a ^ b) & (a ^ r) & 0x80000000) else 0
        return self._set_zn(r)

    # ---- 取指 ----
    def fetch(self, pc):
        off = pc - self.m.rom_base
        if 0 <= off < len(self.m.rom):
            d = self.m.rom
        else:
            d = bytes(self.m.rd(pc + i, 1) for i in range(6))
            off = 0
        ins = self.t.decode(d, off)
        return ins

    def _text(self, ins):
        mnem, base, vals, size, w = ins
        return f"{mnem} {vals}"

    # ---- EIT（异常/中断/陷阱）----
    def vector_addr(self, vec):
        """向量槽地址：TBR + (255 - vec) * 4（倒序，实测自 D300 两段固件）"""
        return (self.dr[0] + (255 - (vec & 0xFF)) * 4) & M32

    def eit(self, vec, level=None):
        """进入 EIT：把旧 PS、PC 压到系统栈（SSP），切 S=1，跳向量表。

        FR 的入栈顺序是 PS 先、PC 后，所以栈顶是 PC，reti 先弹 PC 再弹 PS。
        """
        old_ps = self.ps
        sp = self._get_sp(0)
        sp = (sp - 4) & M32
        self.m.wr(sp, 4, old_ps, self.pc)
        sp = (sp - 4) & M32
        self.m.wr(sp, 4, self.pc, self.pc)
        self._set_sp(0, sp)
        if self.s != 0:                          # 进中断一律走系统栈
            self.dr[3] = self.r[15]              # 保存 USP
            self.s = 0
            self.r[15] = self.dr[2]              # 切到 SSP
        if level is not None:
            self.ilm = min(level & 0x1F, 0x1F)
        self.i = 0                               # 进中断先关中断（与 REALOS 入口一致）
        slot = self.vector_addr(vec)
        tgt = self.m.rd(slot, 4)
        self.irq_log.append((vec, level, self.pc, tgt))
        self.pc = tgt & M32
        return tgt

    def request_irq(self, vec, level=8):
        """外部请求一条硬件中断（排队，等 step() 边界受理）"""
        self.irq_pending.append((level & 0x1F, vec & 0xFF))

    def _accept_irq(self):
        if not self.irq_pending or not self.i:
            return False
        self.irq_pending.sort(key=lambda x: -x[0])
        lvl, vec = self.irq_pending[0]
        if lvl <= self.ilm:                      # 被 ILM 屏蔽
            return False
        self.irq_pending.pop(0)
        if self.eit_hook is not None and self.eit_hook(self, vec):
            return True
        self.eit(vec, lvl)
        return True

    # ---- 执行一条 ----
    def step(self):
        if self.delay is not None:
            tgt = self.delay
            self.delay = None
            self._exec_one()
            self.pc = tgt
            return
        if self.irq_pending and self._accept_irq():
            self.steps += 1
            return
        self._exec_one()

    def _exec_one(self):
        pc = self.pc
        ins = self.fetch(pc)
        if ins is None:
            self.halted = True
            self.halt_reason = f"无法解码 @0x{pc:08X}"
            return
        mnem, base, vals, size, w = ins
        self.steps += 1
        self.pc = (pc + size) & M32
        try:
            self._do(mnem, base, vals, size, w, pc)
        except Unimpl:
            raise
        if self.trace_on:
            self.trace.append((pc, w, size, mnem, dict(vals)))

    def _dr(self, i):
        if i == 2:
            return self._get_sp(0)                 # SSP
        if i == 3:
            return self._get_sp(1)                 # USP
        if i == 4:
            return self.mdh
        if i == 5:
            return self.mdl
        return self.dr[i] if i < 6 else 0

    def _set_dr(self, i, v):
        if i == 2 or i == 3:
            self._set_sp(i - 2, v)
        elif i == 4:
            self.mdh = v & M32
        elif i == 5:
            self.mdl = v & M32
        elif i < 6:
            self.dr[i] = v & M32

    # -------------------------------------------------- 指令实现
    def _do(self, mn, base, v, size, w, pc):
        r = self.r
        m = self.m
        g = v.get

        # ---------- 立即数 / 传送 ----------
        if mn in ("ldi:8", "ldi8"):
            r[g("ri")] = g("i8")
            return
        if mn in ("ldi:20", "ldi20"):
            r[g("ri")] = g("i20")
            return
        if mn in ("ldi:32", "ldi32"):
            r[g("ri")] = g("i32")
            return
        if mn == "mov":
            if base == 0x8B00:
                r[g("ri")] = r[g("rj")]
            elif base == 0xB700:                      # mov Rs1,Ri
                r[g("ri")] = self._dr(g("rs1"))
            elif base == 0xB300:                      # mov Ri,Rs1
                self._set_dr(g("rs1"), r[g("ri")])
            elif base == 0x1710:                      # mov PS,Ri
                r[g("ri")] = self.ps
            elif base == 0x0710:                      # mov Ri,PS
                self.ps = r[g("ri")]
            else:
                raise Unimpl(mn)
            return

        # ---------- 算术（目标 = 最后一个操作数）----------
        if mn == "add":
            if "rj" in v:
                r[g("ri")] = self._add(r[g("ri")], r[g("rj")])
            else:
                r[g("ri")] = self._add(r[g("ri")], g("u4"))
            return
        if mn == "add2":
            r[g("ri")] = self._add(r[g("ri")], (g("m4") - 16) & M32)
            return
        if mn == "addn":
            if "rj" in v:
                r[g("ri")] = (r[g("ri")] + r[g("rj")]) & M32
            else:
                r[g("ri")] = (r[g("ri")] + g("u4")) & M32
            return
        if mn == "addn2":
            r[g("ri")] = (r[g("ri")] + ((g("m4") - 16) & M32)) & M32
            return
        if mn == "addc":
            r[g("ri")] = self._add(r[g("ri")], r[g("rj")], self.c)
            return
        if mn == "addsp":
            r[15] = (r[15] + sx(g("s10"), 8) * 4) & M32
            return
        if mn == "sub":
            r[g("ri")] = self._sub(r[g("ri")], r[g("rj")])
            return
        if mn == "subc":
            r[g("ri")] = self._sub(r[g("ri")], r[g("rj")], self.c)
            return
        if mn == "subn":                               # SUBN 不改标志位（N 后缀 = no flag）
            r[g("ri")] = (r[g("ri")] - r[g("rj")]) & M32
            return
        if mn == "cmp":
            if "rj" in v:
                self._sub(r[g("ri")], r[g("rj")])
            else:
                self._sub(r[g("ri")], g("u4"))
            return
        if mn == "cmp2":
            self._sub(r[g("ri")], (g("m4") - 16) & M32)
            return

        # ---------- 逻辑 ----------
        if mn in ("and", "or", "eor"):
            op = {"and": lambda a, b: a & b, "or": lambda a, b: a | b, "eor": lambda a, b: a ^ b}[mn]
            if base & 0x0F00 in (0x0200, 0x0A00):        # 寄存器型
                r[g("ri")] = self._set_zn(op(r[g("ri")], r[g("rj")]) & M32)
                self.v = 0                               # 逻辑运算一律清 V
            else:                                        # @Ri 型（目标是内存，地址 = Ri 的内容）
                a = r[g("ri")]
                m.wr(a, 4, op(m.rd(a, 4), r[g("rj")]), pc)
            return
        if mn in ("andh", "orh", "eorh"):
            op = {"andh": lambda a, b: a & b, "orh": lambda a, b: a | b, "eorh": lambda a, b: a ^ b}[mn]
            a = r[g("ri")]
            m.wr(a, 2, op(m.rd(a, 2), r[g("rj")] & 0xFFFF), pc)
            return
        if mn in ("andb", "orb", "eorb"):
            op = {"andb": lambda a, b: a & b, "orb": lambda a, b: a | b, "eorb": lambda a, b: a ^ b}[mn]
            a = r[g("ri")]
            m.wr(a, 1, op(m.rd(a, 1), r[g("rj")] & 0xFF), pc)
            return
        if mn == "xchb":                               # 只换低字节，Ri 高 24 位保留
            a = r[g("rj")]
            ri = g("ri")
            tmp = r[ri] & 0xFF
            r[ri] = (r[ri] & 0xFFFFFF00) | (m.rd(a, 1, pc) & 0xFF)
            m.wr(a, 1, tmp, pc)
            return
        if mn in ("andccr", "orccr"):
            v8 = g("u8")
            ccr = ((self.s << 5) | (self.i << 4) | (self.n << 3) | (self.z << 2)
                   | (self.v << 1) | self.c)
            ccr = (ccr & v8) if mn == "andccr" else (ccr | v8)
            # 必须走 ps 的 setter：S 位一变，R15 就要在 SSP/USP 之间换支。
            # 直接给 self.s 赋值会绕过换栈，于是 REALOS 恢复任务上下文时
            # 读的还是旧栈 —— 这是 V10 之前一直"任务起不来"的底层原因之一。
            self.ps = (self.ps & ~0xFF) | ccr
            return
        if mn == "stilm":
            self.ilm = g("u8") & 0x1F
            return

        # ---------- 位操作（立即数是掩码！）----------
        if mn in ("bandl", "borl", "beorl", "bandh", "borh", "beorh", "btstl", "btsth"):
            mask = g("u4")
            if mn.endswith("h"):
                mask = (mask << 4) & 0xFF
            a = r[g("ri")]                                # @Ri：地址是寄存器内容
            old = m.rd(a, 1, pc)
            if mn in ("btstl", "btsth"):
                t = old & mask
                self.z = 1 if t == 0 else 0
                self.n = 1 if (t & 0x80) else 0     # (lt tmp 0)：只有高半字节测试才可能为 1
            else:
                op = {"bandl": lambda o, k: o & k, "bandh": lambda o, k: o & k,
                      "borl": lambda o, k: o | k, "borh": lambda o, k: o | k,
                      "beorl": lambda o, k: o ^ k, "beorh": lambda o, k: o ^ k}[mn]
                m.wr(a, 1, op(old, mask), pc)
            return

        # ---------- 移位 ----------
        if mn in ("lsl", "lsl2", "lsr", "lsr2", "asr", "asr2"):
            ri = g("ri")
            if mn.endswith("2"):
                amt = g("u4") + 16
            elif "rj" in v:
                amt = r[g("rj")] & 0x1F
            else:
                amt = g("u4")
            x = r[ri]
            if amt == 0:
                self._set_zn(x)
                return
            if mn.startswith("lsl"):
                res = (x << amt) & M32
                self.c = (x >> (32 - amt)) & 1
            elif mn.startswith("lsr"):
                res = x >> amt
                self.c = (x >> (amt - 1)) & 1
            else:
                res = (sx(x, 32) >> amt) & M32
                self.c = (x >> (amt - 1)) & 1
            r[ri] = self._set_zn(res)
            return

        # ---------- 乘除 ----------
        if mn == "mul":
            p = sx(r[g("ri")], 32) * sx(r[g("rj")], 32)
            p &= 0xFFFFFFFFFFFFFFFF
            self.mdh, self.mdl = p >> 32, p & M32
            return
        if mn == "mulu":
            p = r[g("ri")] * r[g("rj")]
            self.mdh, self.mdl = (p >> 32) & M32, p & M32
            return
        if mn == "mulh":
            p = sx(r[g("ri")] & 0xFFFF, 16) * sx(r[g("rj")] & 0xFFFF, 16)
            self.mdl = p & M32
            return
        if mn == "muluh":
            self.mdl = ((r[g("ri")] & 0xFFFF) * (r[g("rj")] & 0xFFFF)) & M32
            return
        if mn == "div0s":                              # D0=被除数符号, D1=D0^除数符号, MDH=符号扩展
            self.d0 = (self.mdl >> 31) & 1
            self.d1 = self.d0 ^ ((r[g("ri")] >> 31) & 1)
            self.mdh = M32 if self.d0 else 0
            return
        if mn == "div0u":
            self.d0 = self.d1 = 0
            self.mdh = 0
            return
        if mn == "div1":                               # 逐步除法核心（每步产出一位商）
            self.mdh = ((self.mdh << 1) | ((self.mdl >> 31) & 1)) & M32
            self.mdl = (self.mdl << 1) & M32
            d = r[g("ri")]
            if self.d1:
                s = self.mdh + d
                res = s & M32
                self.c = 1 if s > M32 else 0
            else:
                s = self.mdh - d
                res = s & M32
                self.c = 1 if s < 0 else 0
            self.z = 1 if res == 0 else 0
            if (self.d0 ^ self.d1 ^ self.c) == 0:
                self.mdh = res
                self.mdl |= 1
            return
        if mn == "div2":                               # 最后一步试减，只置标志
            d = r[g("ri")]
            s = (self.mdh + d) if self.d1 else (self.mdh - d)
            res = s & M32
            self.c = 1 if (s > M32 or s < 0) else 0
            self.z = 1 if res == 0 else 0
            if self.z:
                self.mdh = 0
            return
        if mn == "div3":
            if self.z:
                self.mdl = (self.mdl + 1) & M32
            return
        if mn == "div4s":
            if self.d1:
                self.mdl = (-self.mdl) & M32
            return

        # ---------- 扩展 ----------
        if mn == "extsb":
            r[g("ri")] = sx(r[g("ri")] & 0xFF, 8) & M32
            return
        if mn == "extub":
            r[g("ri")] = r[g("ri")] & 0xFF
            return
        if mn == "extsh":
            r[g("ri")] = sx(r[g("ri")] & 0xFFFF, 16) & M32
            return
        if mn == "extuh":
            r[g("ri")] = r[g("ri")] & 0xFFFF
            return

        # ---------- 取数 ld / lduh / ldub ----------
        if mn in ("ld", "lduh", "ldub"):
            size_ = {"ld": 4, "lduh": 2, "ldub": 1}[mn]
            ri = g("ri")
            if base in (0x0400, 0x0500, 0x0600):
                a = r[g("rj")]
            elif base in (0x0000, 0x0100, 0x0200):
                a = (r[13] + r[g("rj")]) & M32
            elif base in (0x2000, 0x4000, 0x6000):
                # disp10/disp9/disp8 都是 8 位有符号字段，再按元素宽度缩放（CGEN 的
                # get 函数：disp10→*4、disp9→*2、disp8→*1），负数偏移必须符号扩展
                a = (r[14] + sx(g({0x2000: "disp10", 0x4000: "disp9", 0x6000: "disp8"}[base]), 8)
                     * {0x2000: 4, 0x4000: 2, 0x6000: 1}[base]) & M32
            elif base == 0x0300:
                a = (r[15] + g("udisp6") * 4) & M32
            elif base == 0x0700:                       # ld @R15+,Ri
                a = r[15]
                if ri != 15:
                    r[15] = (r[15] + 4) & M32
            elif base == 0x0780:                       # ld @R15+,Rs2（专用寄存器）
                a = r[15]
                r[15] = (r[15] + 4) & M32
                self._set_dr(g("rs2"), m.rd(a, 4, pc))
                return
            elif base == 0x0790:                       # ld @R15+,PS
                a = r[15]
                r[15] = (r[15] + 4) & M32
                self.ps = m.rd(a, 4, pc)
                return
            else:
                raise Unimpl(mn)
            r[ri] = m.rd(a, size_, pc) & ({4: M32, 2: 0xFFFF, 1: 0xFF}[size_])
            return

        # ---------- 存数 st / sth / stb ----------
        if mn in ("st", "sth", "stb"):
            size_ = {"st": 4, "sth": 2, "stb": 1}[mn]
            if base in (0x1400, 0x1500, 0x1600):
                a = r[g("rj")]
            elif base in (0x1000, 0x1100, 0x1200):
                a = (r[13] + r[g("rj")]) & M32
            elif base in (0x3000, 0x5000, 0x7000):
                a = (r[14] + sx(g({0x3000: "disp10", 0x5000: "disp9", 0x7000: "disp8"}[base]), 8)
                     * {0x3000: 4, 0x5000: 2, 0x7000: 1}[base]) & M32
            elif base == 0x1300:
                a = (r[15] + g("udisp6") * 4) & M32
            elif base == 0x1700:                       # st Ri,@-R15
                r[15] = (r[15] - 4) & M32
                a = r[15]
            elif base == 0x1780:                       # st Rs2,@-R15
                r[15] = (r[15] - 4) & M32
                m.wr(r[15], 4, self._dr(g("rs2")), pc)
                return
            elif base == 0x1790:                       # st PS,@-R15
                r[15] = (r[15] - 4) & M32
                m.wr(r[15], 4, self.ps, pc)
                return
            else:
                raise Unimpl(mn)
            m.wr(a, size_, r[g("ri")], pc)
            return

        # ---------- 直接寻址传送 dmov ----------
        if mn in ("dmov", "dmovh", "dmovb"):
            size_ = {"dmov": 4, "dmovh": 2, "dmovb": 1}[mn]
            sc = {"dmov": 4, "dmovh": 2, "dmovb": 1}[mn]
            dk = {"dmov": "dir10", "dmovh": "dir9", "dmovb": "dir8"}[mn]
            a = g(dk) * sc
            if base in (0x0800, 0x0900, 0x0A00):        # @dir -> R13
                r[13] = m.rd(a, size_, pc)
            elif base in (0x0C00, 0x0D00, 0x0E00):      # @dir -> @R13+
                m.wr(r[13], size_, m.rd(a, size_, pc), pc)
                r[13] = (r[13] + size_) & M32
            elif base == 0x0B00:                        # @dir -> @-R15
                r[15] = (r[15] - 4) & M32
                m.wr(r[15], size_, m.rd(a, size_, pc), pc)
            elif base in (0x1800, 0x1900, 0x1A00):      # R13 -> @dir
                m.wr(a, size_, r[13], pc)
            elif base in (0x1C00, 0x1D00, 0x1E00):      # @R13+ -> @dir
                m.wr(a, size_, m.rd(r[13], size_, pc), pc)
                r[13] = (r[13] + size_) & M32
            elif base == 0x1B00:                        # @R15+ -> @dir
                m.wr(a, size_, m.rd(r[15], size_, pc), pc)
                r[15] = (r[15] + 4) & M32
            else:
                raise Unimpl(mn)
            return

        # ---------- 多寄存器 ----------
        if mn in ("ldm0", "ldm1"):
            lst = g("reglist-low-ld") if mn == "ldm0" else g("reglist-hi-ld")
            for i in range(8):
                if lst >> i & 1:
                    reg = i if mn == "ldm0" else i + 8
                    r[reg] = m.rd(r[15], 4, pc)
                    r[15] = (r[15] + 4) & M32
            return
        if mn in ("stm0", "stm1"):
            lst = g("reglist-low-st") if mn == "stm0" else g("reglist-hi-st")
            for i in range(8):
                if lst >> i & 1:
                    reg = (7 - i) if mn == "stm0" else (15 - i)
                    r[15] = (r[15] - 4) & M32
                    m.wr(r[15], 4, r[reg], pc)
            return

        # ---------- 栈帧 ----------
        if mn == "enter":
            r[15] = (r[15] - 4) & M32
            m.wr(r[15], 4, r[14], pc)
            r[14] = r[15]
            r[15] = (r[15] - g("u10") * 4) & M32
            return
        if mn == "leave":
            r[15] = (r[14] + 4) & M32
            r[14] = m.rd((r[15] - 4) & M32, 4, pc)
            return

        # ---------- 跳转 / 调用 ----------
        if mn == "jmp":
            self.pc = r[g("ri")] & M32
            return
        if mn == "jmp:d":
            self.delay = r[g("ri")] & M32
            return
        if mn in ("call", "call:d"):
            # FR 的返回地址放在专用寄存器 RP(dr1)，不是压栈
            if "rel12" in v:
                tgt = (pc + 2 + sx(g("rel12"), 11) * 2) & M32
            else:
                tgt = r[g("ri")] & M32
            self._set_dr(1, pc + 2 if mn == "call" else pc + 4)
            if self.call_hook is not None and self.call_hook(self, tgt, pc):
                return                                # 打桩：不真的跳进子程序
            if mn == "call":
                self.pc = tgt
            else:
                self.delay = tgt
            return
        if mn in ("ret", "ret:d"):
            tgt = self._dr(1)
            if mn == "ret":
                self.pc = tgt
            else:
                self.delay = tgt
            return
        if mn == "reti":
            sp = self._get_sp(0)                     # EIT 一律用系统栈
            tgt = m.rd(sp, 4, pc)
            sp = (sp + 4) & M32
            newps = m.rd(sp, 4, pc)
            sp = (sp + 4) & M32
            self._set_sp(0, sp)
            self.ps = newps                          # 可能切回 USP
            self.pc = tgt
            return

        # ---------- 条件分支 ----------
        if mn and (mn[0] == "b") and ("rel9" in v):
            cc = mn.split(":")[0][1:]
            cond = {"ra": True, "no": False, "eq": self.z, "ne": not self.z,
                    "c": self.c, "nc": not self.c, "n": self.n, "p": not self.n,
                    "v": self.v, "nv": not self.v,
                    "lt": bool(self.v ^ self.n), "ge": not (self.v ^ self.n),
                    "le": bool((self.v ^ self.n) or self.z),
                    "gt": not ((self.v ^ self.n) or self.z),
                    "ls": bool(self.c or self.z), "hi": not (self.c or self.z)}.get(cc)
            if cond is None:
                raise Unimpl(mn)
            tgt = (pc + 2 + sx(g("rel9"), 8) * 2) & M32
            if cond:
                if mn.endswith(":d"):
                    self.delay = tgt
                else:
                    self.pc = tgt
            return
        if mn == "nop":
            return

        # ---------- FR80 专有：位检索 ----------
        if mn in ("srch0", "srch1", "srchc"):
            v0 = r[g("ri")]
            if mn == "srch0":
                want = 0
            elif mn == "srch1":
                want = 1
            else:
                want = 1 if (v0 & 0x80000000) == 0 else 0     # 找第一个与 MSB 不同的位
            probe = (~v0) & M32 if want == 0 else v0
            pos = 32
            mask = 0x80000000
            for i in range(32):                # bit31→0 编号；查满 32 位
                if probe & mask:
                    pos = i
                    break
                mask >>= 1
            # ⚠️ 参考实现（Java 的 Format.bitSearch）循环只走 31 次、漏掉 bit0，
            #    遇到「只有 bit0 匹配」时它返回 32，这里返回 31。未定，见报告。
            r[g("ri")] = pos                   # 不动 NZVC
            return

        # ---------- 系统 / 协处理器 ----------
        if mn == "int":
            num = g("u8")
            if num == 0x40:
                self.int40 += 1                      # 先记数：不管走钩子还是走 EIT
            if self.svc_hook is not None:            # 打桩模式：直接当服务调用返回
                self.svc_hook(self, num, pc)
                return
            self.eit(num)                            # 否则真走向量表（PS 先入栈、I=0、S=0）
            return
        if mn == "inte":
            # CGEN 注释：inte = "interrupt for emulator"，即调试器断点指令
            self.unimpl_seen["inte"] = self.unimpl_seen.get("inte", 0) + 1
            self.halted = True
            self.halt_reason = f"inte 断点 @0x{pc:08X}"
            return
        if mn in ("ldres", "stres", "copop", "copld", "copst", "copsv"):
            self.unimpl_seen[mn] = self.unimpl_seen.get(mn, 0) + 1
            return

        raise Unimpl(mn)

    # -------------------------------------------------- 运行
    def run(self, max_steps=2000, stop_addrs=(), stop_on_ret_to=None):
        for _ in range(max_steps):
            if self.halted:
                break
            if self.pc in stop_addrs:
                self.halt_reason = f"到达断点 0x{self.pc:08X}"
                break
            before = self.pc
            try:
                self.step()
            except Unimpl as e:
                self.halted = True
                self.halt_reason = f"未实现指令 {e.args[0]} @0x{before:08X}"
                break
            if stop_on_ret_to is not None and self.steps > 0 and self.pc == stop_on_ret_to:
                self.halt_reason = f"返回到 0x{self.pc:08X}"
                break
        return self.steps

    def dump_regs(self):
        out = []
        for i in range(0, 16, 4):
            out.append("  " + "  ".join(f"r{i+j}={self.r[i+j]:08X}" for j in range(4)))
        out.append(f"  PC={self.pc:08X}  PS={self.ps:08X} "
                   f"(N{self.n} Z{self.z} V{self.v} C{self.c} ILM{self.ilm})")
        out.append(f"  TBR={self.dr[0]:08X} RP={self.dr[1]:08X} "
                   f"MDH={self.mdh:08X} MDL={self.mdl:08X}")
        return "\n".join(out)


TABLE = InsnTable(HERE + r"\fr30.cpu", HERE + r"\fr30-opc.c")
