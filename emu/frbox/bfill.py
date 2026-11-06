# -*- coding: utf-8 -*-
"""B 段的「整片填充」：`0x1356A0` 那条 `st r0,@(r13,r12)`。—— V41

V40 把 KI#166 写成「任务 9 用一条 4 字节写把 ICR `0x46C..0x46F` 清零」。
那句话在**现象**上没错，在**机制**上是错的。实测：

    0x13568E  ldi:8   #0x80, r0        ; r0 = 0x80
    0x135690  add     r9, r0           ; r0 = r9 + 0x80      ← 目的基址在 r9
    0x135692  ldi:8   #0x4C, r1        ; r1 = 0x4C
    0x135694  add     r8, r1           ; r1 = r8 + 0x4C      ← 源基址在 r8
    0x135696  ldi:20  #0x218C, r13     ; r13 = 字节数（0x2188）
    0x13569A  add2    r13, #-4, r13    ; 递减 4
    0x13569C  ld      @(r13,r1), r12   ; r12 = [r8+0x4C+off]
    0x13569E  bgt:d   0x13569A
    0x1356A0  st      r12, @(r13,r0)   ; [r9+0x80+off] = r12

⚠️ 操作数别读反（V41 踩过两次）：按 `fr30-opc.c`，`st` 打印的是 `(rj, ri)`，
`rj=0`、`ri=12`，所以是 **`st r12,@(r13,r0)`**；`r0` 是**算出来的地址**、
`r12` 是值。

⚠️⚠️ **V42 更正**：`r0` 不是常数 0x80 —— `0x135690 add r9,r0` 把它算成
`r0 = r9 + 0x80`；同理 `r1 = r8 + 0x4C`。所以这条循环的真身是

    memmove(dst = r9+0x80, src = r8+0x4C, len = 0x2188)     位移 +0x34

**不是 memset**。V41 之所以没看出来，是因为本次运行里 `r9 = 0`，算出来的 0x80
与「立即数 0x80」在数字上无法区分。详见 `bmove.py`。

操作数归属不是靠解码器打印顺序猜的，是**拿寄存器对总线实测出来的**（1,330 笔
逐笔比对，`地址 == r13+r0` 命中 2,147/2,147，`值 == r12` 命中 2,147/2,147）：

    步 506201: r0=0x00000080 r12=0x00000000 r13=0x00002188 → 总线 a=0x2208 v=0x0
    步 506205: r0=0x00000080 r12=0x00000000 r13=0x00002184 → 总线 a=0x2204 v=0x0

三次实测（`blive.boot()` 之后跑 70 万步，共 891,520 步）：

* 0x1356A0 执行 **23,617 次**，落点：`0x1000..0x2188` **12,342** / direct page
  `0x0..0xFF` **352** / ICR 块 `0x400..0x47F` **352** / 其余 **10,571**。
* 每次都一样：2,147 笔 × 4 字节 = 8,588 = `0x2188`；891,520 步里出现 **11 次**
  （506,201 起，每 24,353 步一次）。**11 次全部 `r8 = r9 = 0`**，所以实际落到
  `dst 0x84..0x2208`、`src 0x50..0x21D4`。
* ICR 块里 32 个 4 字节对齐位置（0x400..0x47C）**每次都被整片覆盖一遍**（32 × 11
  = 352 笔）—— 不是「按向量号逐条清」，是**一次基址为 0 的整块搬运路过这里**。
* **步 513,781 这一笔**落在 `0x46C..0x46F` 四个字节：`vec 0x3F` 的 ICR 被写成
  **0x00**（V40 记的「0x80」是错的，末值实测 0x00）→ `irq_level()` 读成 0 →
  `dev_dispatch` 按 0x1F 兜底 → `level < ILM` 永不成立 → 派发停摆。
  而且此后**每次填充都会再清一次**（70 万步里 11 笔、全为 0），所以这不是一次性
  事故：只要填充还会跑，ICR `0x3F` 就永远回不到 0x10。第 24 次换手在 501,944，
  之后再也没有换手 —— 时间线正好对上。

## 反事实

把这批写「引到影子 RAM」（等价于假设 `0x0..0x2188` 是普通 RAM，而不是把设备放
在这里），同样 891,520 步：

| | 原样 | 引到影子 |
|---|---|---|
| 换手 | 24 次（最后一次 501,944） | **37 次**（最后一次 660,439） |
| 跑过入口的任务 | 2,3,4,5,6,7,9,10,11 | **2,3,4,5,6,7,9,10,11,1,16,17,20,21,26,27,28** |
| 收尾 | 卡在任务 9 的拷贝循环 `0x137968` | **回到空闲任务 `0x407DC`** |
| ICR `0x46F` 末值 | 0x00（被填充清掉） | 0x10（服务出口写回的值） |

## 那 41 笔「非 0 写入」——KI#169 结案（V42）

V41 把同一次填充里 41 笔非 0 写入（`0x10` / `0x1c` / `0x02105500` …）判成「模型
自身的问题」，理由是循环体四条指令都不写 `r12`、该窗口 EIT 为 0 次。

**V42 查明：`r12` 是被 `0x13569C ld @(r13,r1),r12` 从内存读进来的**（见文件头
的 V42 更正）—— 这条循环是 memmove，不是 memset。41 笔非 0 就是**搬过来的源
数据**，一点也不异常。当时的「循环体不写 r12」漏掉了 `ld` 的目的寄存器。

## 所以 V40 的「停住」怎么改判

所以 V40 的「51.5 万步之后停住」**是模型副作用**：固件把 `0x0..0x2188` 当 RAM 整片
填，而我们把「请求派发锁存器 `0x44`」和「ICR 阵列 `0x440`」放在了这段里。填完
ICR `0x3F` 变成 0x80 → `irq_level()` 读成 0 → `dev_dispatch` 里
`lvl = ... or 0x1F` 把 0 当成「没配置」→ 按 0x1F 挂上 → 永远不满足 `level < ILM`
→ 派发停摆。

⚠️ 这是**模型的边界**，不是固件的停机。反过来也不能据此断言「0x0..0x2188 就是
RAM」—— `0x46F` 在服务入口被写 0x1E、出口写回 0x10、期间 ILM=0x1E 正好挡住派发，
这条证据仍指向 ICR。两个读法都记在 KNOWN_ISSUES #168，下一版要正面处理。

## 附带

`0x137966..0x137974` 不是「7 条指令打转的死循环」，是个**带计数的 16 位拷贝循环**：

    0x137966  lduh @(r4), r0     ; 取一个 16 位目标地址
    0x137968  sth  r5, @(r0)     ; 把 r5 写进去
    0x13796A  add  r5, #2
    0x13796C  add2 r6, #-1       ; 计数器
    0x13796E  cmp  r6, #0
    0x137970  bgt:d 0x137966
    0x137972  add  r4, #2
    0x137974  ret
"""

from . import brealos

#: 循环里的存储指令 `st r12,@(r13,r0)`
FILL_PC = 0x1356A0
#: ⚠️ 本次运行里 r0 的值。它不是常量：r0 = r9 + 0x80（V42），只是 r9=0
FILL_BASE = 0x80
#: 写进去的值来自 `0x13569C ld @(r13,r1),r12`，不是常数
FILL_VALUE = 0x00
#: 每次调用扫过的字节数（r13 的初值）
FILL_SPAN = 0x2188
#: 每次调用扫过的地址范围（闭区间）
FILL_TOP = 0x2208
FILL_BOTTOM = 0x0080
#: 任务 9 的入口（0x1356A0 属于它的代码段）
TASK9_ENTRY = 0x1352EA
#: 任务 9 卡住时所在的拷贝循环
COPY_LOOP = 0x137966
#: 落到这些区域时分开计数
REGIONS = (
    ("direct page", 0x0000, 0x0100),
    ("ICR 块", 0x0400, 0x0480),
    ("低区 RAM", 0x1000, 0x2188),
)


def region_of(addr):
    """地址落在哪个观测区（不在任何区里返回「其他」）。"""
    for name, lo, hi in REGIONS:
        if lo <= addr < hi:
            return name
    return "其他"


class FillWatch(object):
    """记录 `pc == FILL_PC` 的那些**存储指令**。

    `hits` 每条形如 `(步数, 地址, 值, 字节数)` —— 一笔是**一条指令**，不是
    「一个字节」。那一次 `基址 0 / 值 0x80 / 跨度 0x2188` 的调用因此是 **2,147 笔**
    （2,147 × 4 字节 = 8,588 字节），不是 8,588 笔。
    """

    def __init__(self, m, pc=FILL_PC):
        self.m = m
        self.pc = pc
        self.hits = []
        self._wr = None

    def attach(self):
        m, base = self.m, self.m.bus.wr

        def wr(a, n, v, pc=0):
            if pc == self.pc:
                self.hits.append((m.step_count, a & 0xFFFFFFFF, v & 0xFF, n))
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
    def spans(self, gap=12):
        """按步数间隔切成「突发」；返回 [{start, end, count, lo, hi, values, size}]。"""
        out, cur = [], None
        for s, a, v, n in self.hits:
            if cur is None or s - cur["end"] > gap:
                cur = {"start": s, "end": s, "count": 0, "lo": a, "hi": a,
                       "values": set(), "size": n}
                out.append(cur)
            cur["end"] = s
            cur["count"] += 1
            cur["lo"] = min(cur["lo"], a)
            cur["hi"] = max(cur["hi"], a)
            cur["values"].add(v)
        return out

    def coverage(self):
        """{区域名: 笔数}。"""
        out = {}
        for _s, a, _v, _n in self.hits:
            k = region_of(a)
            out[k] = out.get(k, 0) + 1
        return out

    def in_range(self, lo, hi):
        return [(s, a, v, n) for s, a, v, n in self.hits if lo <= a < hi]

    def runs(self):
        """最长的一串「每次减 4、每 4 步一笔」——这才是**一次**调用的形状。

        相邻两次调用可能只隔几步，按步数切突发会把它们并成一段，地址和值都会混在
        一起；`runs()` 只认「地址严格按 4 递减、步数严格按 4 递增」的连续段。
        """
        out, cur = [], None
        for s, a, v, n in self.hits:
            if (cur is not None and a == cur["top"] - 4 * cur["count"]
                    and s - cur["last"] == 4):
                cur["count"] += 1
                cur["last"] = s
                cur["values"].add(v)
            else:
                cur = {"start": s, "last": s, "top": a, "count": 1,
                       "values": {v}, "size": n}
                out.append(cur)
        for c in out:
            c["bottom"] = c["top"] - 4 * (c["count"] - 1)
        return out

    def longest_run(self):
        rs = self.runs()
        return max(rs, key=lambda d: d["count"]) if rs else None

    def values_in(self, lo, hi):
        """{值: 笔数}，只看起始地址落在 [lo, hi) 的。"""
        out = {}
        for _s, a, v, _n in self.hits:
            if lo <= a < hi:
                out[v] = out.get(v, 0) + 1
        return out

    def bytes_stored(self):
        return sum(n for _s, _a, _v, n in self.hits)

    def largest_span(self):
        """最大的那段突发（对照「一次 0x2188 字节的整片填」）。"""
        sp = self.spans()
        return max(sp, key=lambda d: d["count"]) if sp else None

    def summary(self):
        sp = self.spans()
        return {"stored": len(self.hits), "bytes": self.bytes_stored(),
                "spans": len(sp),
                "largest": (max(d["count"] for d in sp) if sp else 0),
                "regions": self.coverage()}


class Divert(object):
    """反事实：把匹配的写引到影子字典，不落到总线上。

    `pred(addr, n, val, pc) -> bool`。用于回答「如果这段地址是普通 RAM 会怎样」。
    引走的字节记在 `shadow`，可以事后核对。
    """

    def __init__(self, m, pred):
        self.m = m
        self.pred = pred
        self.shadow = {}
        self.diverted = 0
        self._wr = None

    def attach(self):
        m, base = self.m, self.m.bus.wr

        def wr(a, n, v, pc=0):
            if self.pred(a, n, v, pc):
                for i in range(n):
                    self.shadow[(a + i) & 0xFFFFFFFF] = (v >> (8 * i)) & 0xFF
                self.diverted += 1
                return
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


def divert_fill(m, lo=0x0, hi=0x2200, pc=FILL_PC):
    """常用形态：把填充写里落在 `[lo, hi)` 的那些引走。"""
    return Divert(m, lambda a, n, v, addr_pc: addr_pc == pc and a < hi and a >= lo)


class AddrSpy(object):
    """数某个地址被读/写几次，并留下 (步, pc, 值)。"""

    def __init__(self, m, addr):
        self.m = m
        self.addr = addr
        self.reads = []
        self.writes = []
        self._wr = None
        self._rd = None

    def attach(self):
        m, a0 = self.m, self.addr
        base_wr, base_rd = m.bus.wr, m.bus.rd

        def wr(a, n, v, pc=0):
            if a <= a0 < a + n:
                self.writes.append((m.step_count, pc, (v >> (8 * (a0 - a))) & 0xFF))
            return base_wr(a, n, v, pc)

        def rd(a, n, pc=0):
            if a <= a0 < a + n:
                self.reads.append((m.step_count, pc, None))
            return base_rd(a, n, pc)

        self._wr, self._rd = base_wr, base_rd
        m.bus.wr, m.bus.rd = wr, rd
        return self

    def detach(self):
        if self._wr is not None:
            self.m.bus.wr, self.m.bus.rd = self._wr, self._rd
            self._wr = self._rd = None
        return self

    def __enter__(self):
        return self.attach()

    def __exit__(self, *a):
        self.detach()
        return False

    def summary(self):
        return {"reads": len(self.reads), "writes": len(self.writes),
                "first_write": self.writes[0] if self.writes else None}


def census(m):
    """29 条任务记录的现场：状态字节、优先数、保存的 SP、入口跑过没有。"""
    out = []
    for r in brealos.records(m, include_idle=True):
        tcb = r["tcb"]
        live = 0x68000000 <= tcb < 0x68001000
        out.append({"idx": r["idx"], "id": r["id"], "tcb": tcb, "entry": r["entry"],
                    "state": (m.bus.rd(tcb + 0x08, 1) & 0xFF) if live else None,
                    "prio": (m.bus.rd(tcb + 0x0C, 2) & 0xFF) if live else None,
                    "saved_sp": m.bus.rd(tcb + 0x18, 4) if live else 0,
                    "ran": bool(m.cov and m.cov.visited(r["entry"]))})
    return out


def started_tasks(m, svc=-23):
    """流水里 `sta_tsk`（默认服务号 -23）调用过的任务号列表（按时间）。"""
    if getattr(m, "flow", None) is None:
        return []
    return [c[4] for c in m.flow.calls if c[2] == svc]


def never_started(m, svc=-23):
    """既没被 `sta_tsk` 启过、入口也没跑过的任务号。"""
    started = set(started_tasks(m, svc))
    return [r["id"] for r in census(m)
            if not r["ran"] and r["id"] not in started]


def describe(m, res=None, fw=None):
    """一行摘要。"""
    cur = m.bus.rd(0x68000000, 4)
    out = "步=%-8d 当前=0x%08X" % (m.step_count, cur)
    if res:
        out += " 换手=%d 任务=%d" % (len(res["switches"]), len(res["tasks"]))
    if fw is not None:
        s = fw.summary()
        out += " 填充=%d 笔/%d 段" % (s["stored"], s["spans"])
    return out
