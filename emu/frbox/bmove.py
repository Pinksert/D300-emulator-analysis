# -*- coding: utf-8 -*-
"""B 段那条「整片搬运」的真身：`memmove(r8+0x4C → r9+0x80, 0x2188)`。—— V42

V41 把 `0x1356A0` 说成 `memset(dst=0x80, val=0, len=0x2188)`，并把「11 次 ×
2,147 笔」当成固件在清 `0x80..0x2208`。**两句都要改**：基址不是常数 0x80。

## 五条指令的真身（0x13568E 开始的第 2 段）

    0x13568E  ldi:8   #0x80, r0        ; r0 = 0x80
    0x135690  add     r9, r0           ; r0 = r9 + 0x80      ← 目的基址在 r9
    0x135692  ldi:8   #0x4C, r1        ; r1 = 0x4C
    0x135694  add     r8, r1           ; r1 = r8 + 0x4C      ← 源基址在 r8
    0x135696  ldi:20  #0x218C, r13
    0x13569A  add2    r13, #-4, r13
    0x13569C  ld      @(r13,r1), r12   ; r12 = [r8+0x4C+off]
    0x13569E  bgt:d   0x13569A
    0x1356A0  st      r12, @(r13,r0)   ; [r9+0x80+off] = r12

所以它等价于 `memmove(dst=r9+0x80, src=r8+0x4C, len=0x2188)`，位移 **+0x34**。
`r0`/`r1` 只是算出来的地址，**寄存器 r8/r9 才是两个缓冲区的基址**。

## 两次踩同一个坑（KI#168 的续集）

V41 靠「地址 == r13+r0、值 == r12」对上了 2,147/2,147，于是以为找到了真身 ——
可那一版**只验了 r0/r1，没往上追一层**：r0 是 `r9+0x80` 算出来的。因为本次运行
里 `r9 = 0`，`r0` 恰好等于 `0x80`，两种读法在数字上无法区分。

同理 `mov` 也踩过一次：解码器打印 `mov r4, r8`，按 `fr30-opc.c` 的
`/* mov $Rj,$Ri */` 与 `fr_emu.py` 的 `r[ri] = r[rj]`，它的语义是 **`r8 = r4`**
（打印顺序是 `(rj, ri)`，最后一个才是目的）。判「哪个寄存器被写」只能看
**最后一个操作数**，不能看第一个。

## 11 次搬运（70 万步，含 boot 共 891,520）

| 序号 | 起始步 | dst | src | r8 | r9 |
|---|---|---|---|---|---|
| 1 | 506,201 | 0x2208→0x0084 | 0x21D4→0x0050 | 0 | 0 |
| 2 | 652,331 | 同上 | 同上 | 0 | 0 |
| … | 每 24,353 步 | | | 0 | 0 |

每次 2,147 笔 × 4 字节 = 8,588 = `0x2188`。11 次全部 `r8 = r9 = 0`。

## 位移是真的（快照对拍）

填充前后各取 `0x0..0x2300` 的 4 字节快照：

* `after[x + 0x34] == before[x]`：**2,215 / 2,227** 命中
  （不命中的 12 个全在 `0x4..0x48`，落在 dst 区间 `0x84..0x2208` 之外）。
* 所以它是**整块搬**，不是填 0。V41 里那 41 笔「非 0 写入」= 搬过来的源数据
  （`0x10` / `0x1c` / `0x02105500` …），KI#169 结案：**不是模型问题**。

## 基址为什么是 0：一串可复现的链条（KI#171 改判）

先看 `0x1355B2` 那一段（**r9 的 4 个写点里有 2 个在每次搬运前各执行 1 次**）：

    0x1355B2  ldi:8    #0x0, r9        ; r9 = 0（显式清零）
    0x1355B4  ldi:32   #0x1358A8, r12
    0x1355BA  jmp:d    @r12            ; 尾跳
    ...
    0x135608  cmp      #0x0, r9        ; 判空
    0x13560A  bne      *150            ; r9 != 0 → 跳到 0x1356A2（正好跳过整段拷贝）
    0x13560C  call     0x12590C
    0x135616  call     0x122D32        ; 取缓冲区 → 返回值在 r4
    0x135624  mov      r4, r9          ; r9 = 缓冲区指针
    ...
    0x13568E  ...                        ; memmove 用 r9 当基址

实测（340,000 步）：

| 写点 | 指令 | 执行次数 | 当时 r4 |
|---|---|---|---|
| 0x13534E | `mov r4,r9` | 0 | — |
| 0x1354F4 | `mov r4,r9` | 0 | — |
| 0x1355B2 | `ldi:8 #0x0,r9` | **1** | — |
| 0x135624 | `mov r4,r9` | **1** | **0** |

所以 r9 = 0 不是「寄存器丢了」，而是**取缓冲区的函数返回了空指针**：
`0x122D32` 里先 `call 0x124F4E`（链表 `is_empty`，V35 记的四件套之一），
按返回值分支，最后返回 0；`mov r4,r9` 把它灌进 r9；调用方**没有判空**就搬运。

任务体入口（步 484,584，`dr1 = 0x404F8`，`r8 = r9 = 0`）前面是 trampoline
`0x404F0`：`call @r13` → `int #0x40`（服务 −11 = ext_tsk）→ `jmp 0x404DE` 自旋，
**不传任何参数**。

## 反事实（`fix_dst`）

只把 `0x135624` 处的 r4 换成一个安全指针（其余一概不动）：

* `r9 ← 0x2200`：搬运落到 `0x2280..0x4408`，**ICR 不再被踩**，整机继续跑。
* 具体数字见 `results/frbox/bmove.txt` 的 G 段。

这就把 V40/V41 的「停摆」钉到了**一次空指针搬运**上，而不是「固件主动清 ICR」。
"""

#: 目的端存储指令 `st r12,@(r13,r0)`
SHIFT_ST = 0x1356A0
#: 源端装载指令 `ld @(r13,r1),r12`
SHIFT_LD = 0x13569C
#: `add r9,r0` —— 算出目的基址
BASE_DST = 0x135690
#: `add r8,r1` —— 算出源基址
BASE_SRC = 0x135694
#: 目的偏移（相对 r9）
DST_OFF = 0x80
#: 源偏移（相对 r8）
SRC_OFF = 0x4C
#: 每次搬多少字节
LEN = 0x2188
#: 位移 = dst - src
DELTA = DST_OFF - SRC_OFF
#: 每次搬运的笔数（4 字节一笔）
#: 循环是 `add2 r13,#-4` → `ld` → `bgt:d` → `st`，r13 从 `0x2188` 走到 `0x80`，
#: 因此实际落 **2,147** 笔 = `LEN//4 + 1`（偏移 0x80 那一笔也算），比 0x2188/4
#: 多一笔。别把 `LEN//4` 当笔数用（V42 自检栽过一次）。
WORDS = LEN // 4 + 1
#: 调用点（`call:d 0x125992`，循环在它之后）
CALL_PC = 0x13568A
#: 含这两段循环的函数入口
FUNC_LO = 0x1352EA
FUNC_HI = 0x135700
#: `call 0x122064`（返回空指针的上一层）
NULL_CALL_PC = 0x13531C
NULL_RET_PC = 0x13531E
#: 任务 trampoline
TRAMPOLINE = 0x404F0
#: 取缓冲区的函数（它的返回值进 r9）
ALLOC = 0x122D32
#: 取到空指针后 `mov r4,r9`
DST_ASSIGN_PC = 0x135624
#: 判空分支 `cmp #0x0,r9 / bne`（只有 r9 == 0 才进这一段）
NULL_TEST_PC = 0x135608
#: 空时的分支目标（= 0x1356A2，正好跳过整段拷贝）
NULL_SKIP_PC = 0x1356A2


class fix_dst(object):
    """反事实：在 `0x135624 mov r4,r9` 处把 r4 换成一个安全缓冲区指针。

    只改这一个寄存器，别的一概不动 —— 用来判断「搬运砸到 ICR」是不是**唯一**
    让整机停摆的原因。
    """

    def __init__(self, m, ptr=0x2200, pc=DST_ASSIGN_PC):
        self.m = m
        self.ptr = ptr & 0xFFFFFFFF
        self.pc = pc
        self.hits = 0

    def attach(self):
        m, cpu, st = self.m, self.m.cpu, self.m.cpu.step
        self._st = st

        def step():
            if cpu.pc == self.pc:
                cpu.r[4] = self.ptr
                self.hits += 1
            return st()

        cpu.step = step
        return self

    def detach(self):
        if getattr(self, "_st", None) is not None:
            self.m.cpu.step = self._st
            self._st = None
        return self

    def __enter__(self):
        return self.attach()

    def __exit__(self, *a):
        self.detach()
        return False


def region_of(addr):
    """把落点归到粗粒度区域，报表用。"""
    if addr < 0x100:
        return "direct page"
    if addr < 0x400:
        return "0x100..0x3FF"
    if addr < 0x440:
        return "0x400..0x43F"
    if addr < 0x480:
        return "ICR 块"
    if addr < 0x1000:
        return "0x480..0xFFF"
    if addr < 0x2208:
        return "低区 RAM"
    return "0x2208 以上"


class ShiftWatch(object):
    """记录 `0x1356A0` 的每一笔存储，**同时记下当时的 r8/r9**。

    只记地址和值是不够的 —— 决定「搬到哪」的是 r8/r9，不是那两条 `ldi:8`。
    """

    def __init__(self, m):
        self.m = m
        self.stores = []          # (step, addr, val, size, r8, r9)
        self._wr = None

    def attach(self):
        m, base, cpu = self.m, self.m.bus.wr, self.m.cpu

        def wr(a, n, v, pc=0):
            if pc == SHIFT_ST:
                self.stores.append((m.step_count, a & 0xFFFFFFFF, v & 0xFFFFFFFF,
                                    n, cpu.r[8] & 0xFFFFFFFF, cpu.r[9] & 0xFFFFFFFF))
            return base(a, n, v, pc)

        self._wr = base
        self.m.bus.wr = wr
        return self

    def detach(self):
        if self._wr is not None:
            self.m.bus.wr = self._wr
            self._wr = None
        return self

    def __enter__(self):
        return self.attach()

    def __exit__(self, *a):
        self.detach()
        return False

    # ---- 视图 ----
    def calls(self):
        """按「地址每笔减 4、步数每笔 +4」切成一次次搬运。"""
        out, cur = [], None
        for s, a, v, n, r8, r9 in self.stores:
            if (cur is not None and a == cur["dst_lo"] - 4 and s - cur["last"] == 4):
                cur["count"] += 1
                cur["last"] = s
                cur["dst_lo"] = a
                cur["src_lo"] = a - DELTA
            else:
                cur = {"start": s, "last": s, "count": 1,
                       "dst_hi": a, "dst_lo": a,
                       "src_hi": a - DELTA, "src_lo": a - DELTA,
                       "r8": r8, "r9": r9, "values": set()}
                out.append(cur)
            cur["values"].add(v)
        return out

    def call_count(self):
        return len(self.calls())

    def null_base_calls(self):
        """r8 或 r9 为 0 的那些搬运 —— 它们会落到绝对地址 0 附近。"""
        return [c for c in self.calls() if c["r8"] == 0 or c["r9"] == 0]

    def bases(self):
        """{(r8, r9): 次数}。"""
        out = {}
        for c in self.calls():
            k = (c["r8"], c["r9"])
            out[k] = out.get(k, 0) + 1
        return out

    def coverage(self):
        out = {}
        for _s, a, _v, _n, _r8, _r9 in self.stores:
            k = region_of(a)
            out[k] = out.get(k, 0) + 1
        return out

    def summary(self):
        cs = self.calls()
        return {"stores": len(self.stores), "calls": len(cs),
                "words": sorted({c["count"] for c in cs}),
                "dst": sorted({(c["dst_hi"], c["dst_lo"]) for c in cs}),
                "src": sorted({(c["src_hi"], c["src_lo"]) for c in cs}),
                "bases": self.bases(),
                "null": len(self.null_base_calls()),
                "regions": self.coverage()}


def snapshot(m, lo=0x0, hi=0x2300):
    """按 4 字节取快照，{地址: 字}。"""
    return {a: m.bus.rd(a, 4) & 0xFFFFFFFF for a in range(lo, hi, 4)}


def verify_shift(before, after, delta=DELTA):
    """核对「整块位移」：after[x+delta] 是否等于 before[x]。"""
    hit, miss, tot = 0, [], 0
    for x, v in before.items():
        y = x + delta
        if y in after:
            tot += 1
            if after[y] == v:
                hit += 1
            else:
                miss.append((x, v, after[y]))
    return {"hit": hit, "total": tot, "miss": miss}


class BaseWatch(object):
    """在 `0x135690` / `0x135694` 两条 `add` 上取 r8/r9，看基址是怎么算出来的。"""

    def __init__(self, m):
        self.m = m
        self.rows = []            # (step, pc, r8, r9, r0, r1)

    def attach(self):
        m, cpu, st = self.m, self.m.cpu, self.m.cpu.step
        self._st = st

        def step():
            p = cpu.pc
            if p in (BASE_DST, BASE_SRC):
                self.rows.append((m.step_count, p,
                                  cpu.r[8] & 0xFFFFFFFF, cpu.r[9] & 0xFFFFFFFF,
                                  cpu.r[0] & 0xFFFFFFFF, cpu.r[1] & 0xFFFFFFFF))
            return st()

        cpu.step = step
        return self

    def detach(self):
        if getattr(self, "_st", None) is not None:
            self.m.cpu.step = self._st
            self._st = None
        return self

    def __enter__(self):
        return self.attach()

    def __exit__(self, *a):
        self.detach()
        return False


def scan_reg(rom, base, lo, hi, reg):
    """文本层面扫「提到某个寄存器」的指令。

    ⚠️ 别用字段值去比 —— `vals` 里存的是**寄存器号（整数）**，拿 `"r9"` 去比
    永远不命中（V42 第一稿就这么漏了 30 条）。一律按反汇编文本扫。
    """
    import fr_dis
    from fr_emu import TABLE
    out = []
    off = lo - base
    while base + off < hi:
        ins = TABLE.decode(rom, off)
        if ins is None:
            off += 2
            continue
        t, size = fr_dis.line(rom, off, base)
        body = t.split("  ", 2)[-1].rstrip()
        if reg in body:
            out.append((base + off, body))
        off += size
    return out


#: 最后一个寄存器操作数**不是**目的的那些助记符（存、比较、位测、内存位操作）
_NOT_DST = {
    "st", "stb", "sth", "stm0", "stm1", "dmovb", "dmovh", "dmov",
    "cmp", "btst", "btstl", "btsth",
    "bandh", "borh", "bandl", "borl",
    "bra", "bra:d", "beq", "beq:d", "bne", "bne:d", "bgt", "bgt:d",
    "bge", "bge:d", "blt", "blt:d", "ble", "ble:d", "bc", "bc:d", "bnc",
    "jmp", "jmp:d", "call", "call:d", "ret", "ret:d", "reti", "int",
    "push", "pop", "nop", "enter", "leave",
}


def dst_reg(mn, vals):
    """目的寄存器号；没有就返回 None。

    判据是**解码字段**（`ri` 在 FR 里几乎总是目的），加一张「例外助记符」表 ——
    比「文本最后一个词像不像 r9」可靠：那样会把 `cmp #0, r9`（只读）和
    `bandh #0x7, r8`（r8 当地址）算成写。
    """
    if mn in _NOT_DST:
        return None
    v = vals.get("ri")
    return v if isinstance(v, int) else None


def writes_reg(rom, base, lo, hi, reg):
    """真正**写**该寄存器的指令：[(pc, text)]。"""
    import fr_dis
    from fr_emu import TABLE
    want = int(reg[1:])
    out = []
    off = lo - base
    while base + off < hi:
        ins = TABLE.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mn, _o, vals, size, _w = ins
        if dst_reg(mn, vals) == want:
            t, _ = fr_dis.line(rom, off, base)
            out.append((base + off, t.split("  ", 2)[-1].rstrip()))
        off += size
    return out
