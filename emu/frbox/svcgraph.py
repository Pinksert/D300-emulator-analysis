# -*- coding: utf-8 -*-
"""V58：A/B 两段通用的「函数地址引用图」（纯静态 / 纯函数）。

## 这一层解决的问题

V57 收尾时留了一句「A 段 `sta_tsk` 有 108 处调用点、触发事件来源未解」，
四组设备策略跑下来那 108 处一次都没执行。V58 复核发现，那句「未解」里
混进了一个**静态扫描器的口径盲区**：

`bstatic.Sweep.refs_to(addr, window=8)` 是 V51 为 B 段调的，它只查
`Sweep.ldi32` —— 也就是只索引 `ldi:32` / `ldi:32m` 的立即数。这在 B 段
够用，在 A 段几乎全瞎：

| | A 段 d300-a | B 段 d300-b |
|---|---|---|
| ROM 映射 | `0x80000..0x100002`（512 KB） | `0x40000..0x740000`（7 MB） |
| 落在本段 ROM 内的 `ldi:20` 立即数 | 6342 | 861 |
| 落在本段 ROM 内的 `ldi:32` 立即数 | 48 | 29267 |
| `ldi:32` 占可寻址立即数 | **0.75%** | **97.14%** |

A 段的 20 位立即数（值域 0..0xFFFFF）装得下整段 512 KB ROM，所以编译器
用 `ldi:20 r12,#func` + `call r12` 编函数地址；B 段 7 MB 超出 20 位，
只能用 `ldi:32`。这是**地址空间大小决定的指令选型**，不是两套固件风格不同。

另外 `Sweep.edges` 只收 `call:d @rN` 那一种（bstatic 模块 docstring 明写），
`call rel12` 与 `call @r12` 都不在图里 —— V54 已经在 `callers_of()` 上
栽过一次（KI#175 / 硬规矩 32）。本模块把这三种引用形态补齐：

| kind | 来源 | 说明 |
|---|---|---|
| `ldi20` | `ldi:20 rX,#imm` | A 段主力；`refs_to` 原来看不见 |
| `ldi32` | `ldi:32 rX,#imm` | 原 `refs_to` 口径 |
| `rel12` | `call rel12` | 目标 = `pc+2 + sx(raw,11)*2`，照抄 `fr_emu.py:777` |
| `data32` | ROM 里 4 字节大端 == addr（含 `-2/-4` 序言起点） | 函数指针表候选，不是指令 |

`rel12` 的符号位**是 bit10**（`fr_emu.sx(raw, 11)`），不是 bit11；V58 草稿
按 bit11 算过一次，目标数从 1053 变成 1145，分类跟着错。判字段一律回查
执行实现（硬规矩 4）。`fr_dis.fmt_val` 里 `rel12` 用的是 `sx(raw, 8)`，
与执行不一致，**不要用反汇编文本里的 `*NNN` 去推跳转目标**。

## 边界（必须随结论一起写）

* **纯静态**：不建机器、不 boot、不写内存、不挂设备、不改任何状态。
  「有引用」只说明字节里写了这个地址，**不等于会执行**（硬规矩 5）。
* 线性扫会把数据当指令（V55 同类 caveat），`data32` 用 `ROM.find` 也会
  撞进指令字节；这两个数字都是**上界**，不能数成「有几处回调」。
* `Sweep.enclosing()` 只是「最近的 `enter`」；没有 `enter` 的小函数会被
  归到前一个 `enter` 名下（硬规矩 30 / V53）。本模块的函数归属同样是粗粒度。
* 分类按 `ldi20 > ldi32 > rel12 > data32 > none` **互斥优先级**取第一个命中，
  另有 `independent_hits()` 报各形态独立命中数，两种口径不要混比（硬规矩 2）。
* `data32` 的 `-2/-4` 窗口来自硬规矩 26：调用者加载的是**序言起点**，
  `enter` 在其后 2 或 4 字节。
"""

import bisect
import collections

import fr_emu

from . import bstatic

#: 互斥分类的优先级（前面的命中就归前面）
PRIORITY = ("ldi20", "ldi32", "rel12", "data32")

#: 会被当成「调用」的指令引用形态（data32 只是数据出现，不算）
CODE_KINDS = ("ldi20", "ldi32", "rel12")

#: 等待类服务（只列本项目实证过的；未实证的服务号不硬套）
WAIT_SVCS = frozenset((bstatic.WAI_FLG, bstatic.TWAI_FLG,
                       bstatic.SEM_WAI, -61))   # -61 = rcv_msg（V17）

Ref = collections.namedtuple("Ref", "pc kind imm next_mnem")


def sx11(raw):
    """`call rel12` 的 11 位符号扩展 —— 与 `fr_emu.py:777` 逐字对齐。"""
    return fr_emu.sx(raw, 11)


class ImmIndex(object):
    """一段 ROM 的立即数 / 调用边索引（构造时线性扫一遍，之后全是查表）。"""

    def __init__(self, rom, base):
        self.rom = rom
        self.base = base
        self.end = base + len(rom)
        self.ins_count = 0
        self.ldi = collections.Counter()        # i8 / i20 / i32 总条数
        self.inrom = collections.Counter()      # 立即数落在本段 ROM 内的条数
        self.call_kinds = collections.Counter()
        self.imm_refs = collections.defaultdict(list)   # imm -> [(pc, size, kind)]
        self.rel_edges = collections.defaultdict(list)  # target -> [pc]
        self._scan()

    # ------------------------------------------------------------ 扫描
    def _scan(self):
        rom, base = self.rom, self.base
        off, n = 0, len(rom)
        while off < n - 1:
            if rom[off:off + 2] in bstatic.SKIP2:
                off += 2
                continue
            ins = fr_emu.TABLE.decode(rom, off)
            if ins is None:
                off += 2
                continue
            mn, _bs, v, size, _w = ins
            pc = base + off
            self.ins_count += 1

            if mn.startswith("ldi"):
                for key, kind in (("i8", "i8"),
                                  ("i20", "ldi20"),
                                  ("i32", "ldi32")):
                    imm = v.get(key)
                    if imm is None:
                        continue
                    self.ldi[key] += 1
                    if base <= imm < self.end:
                        self.inrom[key] += 1
                    if kind != "i8":
                        self.imm_refs[imm].append((pc, size, kind))
                    break

            if mn in ("call", "call:d", "jmp", "ret", "reti", "bra:d"):
                self.call_kinds[mn] += 1
            if mn in ("call", "call:d") and v.get("rel12") is not None:
                tgt = (pc + 2 + sx11(v["rel12"]) * 2) & 0xFFFFFFFF
                self.rel_edges[tgt].append(pc)
            off += size

    # ------------------------------------------------------------ 普查
    def profile(self):
        """形态普查 → dict。正文引用数字时请打印这些键，别另算（硬规矩 2）。"""
        addr20 = self.inrom["i20"]
        addr32 = self.inrom["i32"]
        return {
            "ins": self.ins_count,
            "ldi": dict(self.ldi),
            "inrom": dict(self.inrom),
            "call": dict(self.call_kinds),
            "rel12_edges": sum(len(v) for v in self.rel_edges.values()),
            "rel12_targets": len(self.rel_edges),
            "imm_kinds": len(self.imm_refs),
            "i32_share": round(100.0 * addr32 / max(1, addr20 + addr32), 2),
        }

    # ------------------------------------------------------------ 查询
    def refs(self, addr, window=8, kinds=CODE_KINDS):
        """`addr` 的**指令引用** → `[Ref]`（按 pc 升序）。

        `window` 的意义与 `Sweep.refs_to()` 一致：接受 `[addr-window, addr]`，
        因为调用者加载的是序言起点而不是 `enter` 地址（硬规矩 26）。
        """
        out = []
        for imm in range(addr - window, addr + 1):
            if "ldi20" in kinds or "ldi32" in kinds:
                for pc, size, kind in self.imm_refs.get(imm, ()):
                    if kind not in kinds:
                        continue
                    nxt = fr_emu.TABLE.decode(self.rom, pc - self.base + size)
                    out.append(Ref(pc, kind, imm, nxt[0] if nxt else None))
            if "rel12" in kinds:
                for pc in self.rel_edges.get(imm, ()):
                    out.append(Ref(pc, "rel12", imm, "call"))
        return sorted(out)

    def data32(self, addr, deltas=(0, -2, -4)):
        """ROM 里 4 字节大端等于 `addr+delta` 的位置 → `[(pc, delta)]`。

        命中可能是函数指针表，也可能只是恰好相等的指令字节 —— 上界，不是证据。
        """
        out = []
        for d in deltas:
            raw = (addr + d).to_bytes(4, "big")
            pos = 0
            while True:
                i = self.rom.find(raw, pos)
                if i < 0:
                    break
                out.append((self.base + i, d))
                pos = i + 1
        return sorted(out)

    def classify(self, addr, window=8):
        """互斥分类 → `(kind, [Ref])`；`kind == "none"` 表示四种都没命中。

        `data32` 命中时 `Ref` 里放的是数据出现位置，`next_mnem` 恒为 "data"。
        """
        code = self.refs(addr, window)
        for kind in ("ldi20", "ldi32", "rel12"):
            hit = [r for r in code if r.kind == kind]
            if hit:
                return kind, hit
        locs = self.data32(addr)
        if locs:
            return "data32", [Ref(pc, "data32", addr + d, "data")
                              for pc, d in locs]
        return "none", []

    def independent_hits(self, addrs, window=8):
        """各形态**独立**命中数（不去重、不互斥）→ Counter。"""
        c = collections.Counter()
        for a in addrs:
            for r in self.refs(a, window):
                c[r.kind] += 1
            if self.data32(a):
                c["data32"] += 1
        return c


# ---------------------------------------------------------------- 函数级
def body_range(sweep, func):
    """粗粒度函数体 `[func, next_enter)`；无下一个 enter 时到 ROM 末尾。"""
    i = bisect.bisect_right(sweep.funcs, func)
    hi = sweep.funcs[i] if i < len(sweep.funcs) else sweep.base + len(sweep.rom)
    return func, hi


def wait_guards(sweep, func, points, wait_svcs=WAIT_SVCS):
    """函数体内、每个调用点之前最近的等待类服务 → `[(point, CallSite|None)]`。

    用来区分「没人调用这个函数」和「进了函数、卡在前面一道等待」两种断点。
    """
    lo, hi = body_range(sweep, func)
    waits = [c for c in sweep.calls
             if lo <= c.addr < hi and c.svc in wait_svcs]
    out = []
    for p in sorted(points, key=lambda c: c.addr):
        prev = [w for w in waits if w.addr < p.addr]
        out.append((p, max(prev, key=lambda w: w.addr) if prev else None))
    return out


def chain_profile(sweep, index, svc, window=8):
    """某服务的调用点 → 所属函数 → 引用形态分类，一次算全。

    返回 dict：
      `points` / `funcs` / `by_func` / `classes` / `by_kind` /
      `code_edges` / `table_locs` / `independent`。
    `classes` 是 `{func: (kind, [Ref])}`，互斥；`independent` 不互斥。
    """
    points = sweep.of(svc)
    enclosing = [sweep.enclosing(c.addr) for c in points]
    by_func = collections.Counter(f for f in enclosing if f is not None)
    funcs = sorted(by_func)
    classes = dict((f, index.classify(f, window)) for f in funcs)
    by_kind = collections.Counter(k for k, _r in classes.values())
    code_edges = sum(len(r) for k, r in classes.values()
                     if k in CODE_KINDS)
    table_locs = dict((f, index.data32(f)) for f, k in
                      ((f, classes[f][0]) for f in funcs)
                      if k == "data32")
    return {
        "points": points,
        "funcs": funcs,
        "by_func": by_func,
        "classes": classes,
        "by_kind": by_kind,
        "code_edges": code_edges,
        "table_locs": table_locs,
        "independent": index.independent_hits(funcs, window),
        "no_enter": sum(1 for f in enclosing if f is None),
    }
