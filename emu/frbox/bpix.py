# -*- coding: utf-8 -*-
"""bpix.py —— 「到底有没有像素」的判据复核（V49）。

## 为什么要有这个模块

V26 说「出不了画面」，给了三条证据，其中第一条是：

> 整场运行盯 SDRAM 的每次写：21,323 个半字地址被写，
> 最长连续段 1,063 个半字 = **2,126 字节**；而 640×480 单色一帧要 38,400 字节。

这条论据在两处需要修正：

1. **机器换了。** 那批数字是在旧机器上量的（SDRAM 只铺 0x20000、
   `0x40`/`0x60`/`0x50000000` 都还没建模）。V41 的整片填充一上来，
   同一口径的「最长连续区间」直接变成 **217,008 字节** —— 比一帧大 5 倍。
2. **判据本身不成立。** 「最长连续区间够大」只说明**有一块够大的内存被碰过**，
   不说明那块内存里是图像。填充 200 KB 的 0x00 一样满足这一条。

所以本模块把判据换成**三条同时成立**才算「有帧缓冲」：

    L  连续区间 >= 38,400 字节（一帧 640×480 1bpp 的体积）
    V  该区间内 4 KB 块的值种数中位数 >= 16（填充块 = 1 种值，表/结构几十种）
    R  该区间内被写 >= 2 次的半字占比 >= 50%（能重画；一次性填充是 0）

三条都过才算「有」。V48 这台机器上：L 过、V 不过（填充块值种数 = 1）→ **没有**。

## 两个「连续」口径必须分开报

    R1  写入顺序连续：本次写的地址 == 上一次写的终点（最严）
    R2  被写过地址集合里的最长连续区间（V26 报告用的就是这个）

V26 的 2,126 B 属于 R2。本版用 `run_v26()` 复现它的运行方式，用 `run_live()`
跑 V48 口径的整机自跑 —— **两套数不许混比**，报告里必须并列写清。

## 边界

1. 只统计 `bus.wr` 能看到的写；器件内部状态不进账（`0x70000000` /
   `0x50000000` 这类 IO 写单独计数）。
2. 「值种数」按**写进去的原始值**算，不按字节拆。位串行那种「逐位改同一字节」
   会让值种数虚高，但那只发生在 IO 口，不在 SDRAM。
3. 判据只回答「有没有一块能装下一帧、内容像图像、且被反复重画的内存」。
   它**不**回答「画的是什么」—— 那要有真帧才谈得上。

用法：

    from frbox import bpix
    m, mt = bpix.run_live()           # V48 口径
    m2, mt2 = bpix.run_v26()          # V26 口径（复现 2,126 B 那次运行）
    bpix.report(mt)
    bpix.verdict(mt)                  # {'framebuffer': (bool, 说明), ...}
"""

from collections import Counter

from . import bgate, blive, brealos
from .machine import create

#: B 段 SDRAM 窗口（V43 起的映射）
SDRAM = 0x9F580000
SDRAM_SZ = 0x280000
#: 一帧的字节数 / 位数（640×480，单色 1 位/点）
FRAME_BYTES = 640 * 480 // 8
FRAME_BITS = 640 * 480
#: B↔EXPEED 位串行端口（V27）
EXPEED_LO, EXPEED_HI = 0x70000000, 0x70100000
#: 0x50000000 三通道位串行控制块（V45）
CH3_LO, CH3_HI = 0x50000000, 0x50100000
#: 4 KB 分块
BLK = 0x1000
#: 判据阈值
MIN_VALUE_KINDS = 16        # 一块「像图像」的 4 KB 块至少要有这么多种写入值
MIN_REWRITE_RATIO = 0.5     # 帧缓冲会被反复画


class Meter(object):
    """挂在 `machine.bus` 上的写入计量器（必须在 `m.load()` 之前 attach）。"""

    def __init__(self, watch=None):
        """`watch=(lo, hi)` 额外统计这段地址的写点 PC（用来定性「这块是谁写的」）。"""
        self.watch = watch
        self.watch_pc = Counter()
        self.writes = 0
        self.seen = bytearray(SDRAM_SZ // 2)      # 每个半字是否被写过
        self.hits = bytearray(SDRAM_SZ // 2)      # 被写次数（封顶 255）
        self.blk_bytes = Counter()
        self.blk_vals = {}
        self.r1 = [0, 0, 0]     # 写入顺序连续：[字节, 起, 止]
        self.ex_w = Counter()   # EXPEED: (addr, pc) -> 次数
        self.ex_r = Counter()
        self.ch3_w = Counter()
        self._cur = [0, 0, 0]
        self._last = [None]
        self._ow = None
        self._or = None
        self._m = None

    # -- 挂钩 ---------------------------------------------------------
    def attach(self, m):
        self._m = m
        self._ow, self._or = m.bus.wr, m.bus.rd
        m.bus.wr, m.bus.rd = self._hook_w, self._hook_r
        return self

    def detach(self):
        if self._m is not None:
            self._m.bus.wr, self._m.bus.rd = self._ow, self._or
            self._m = None
        return self

    def _hook_w(self, a, sz, v, pc=0):
        self.writes += 1
        if self.watch is not None and self.watch[0] <= a < self.watch[1]:
            self.watch_pc[pc] += 1
        if EXPEED_LO <= a < EXPEED_HI:
            self.ex_w[(a, pc)] += 1
        if CH3_LO <= a < CH3_HI:
            self.ch3_w[a] += 1
        if SDRAM <= a < SDRAM + SDRAM_SZ:
            o = a - SDRAM
            b = o // BLK
            self.blk_bytes[b] += sz
            self.blk_vals.setdefault(b, set()).add(v)
            for i in range(0, sz, 2):
                j = (o + i) >> 1
                self.seen[j] = 1
                if self.hits[j] < 255:
                    self.hits[j] += 1
            if self._last[0] == a:
                self._cur[0] += sz
                self._cur[2] = a + sz
            else:
                self._cur[0], self._cur[1], self._cur[2] = sz, a, a + sz
            if self._cur[0] > self.r1[0]:
                self.r1[:] = self._cur[:]
            self._last[0] = a + sz
        return self._ow(a, sz, v, pc)

    def _hook_r(self, a, sz, pc=0):
        if EXPEED_LO <= a < EXPEED_HI:
            self.ex_r[(a, pc)] += 1
        return self._or(a, sz, pc)

    # -- 查询 ---------------------------------------------------------
    def halfwords(self):
        """被写过的半字个数。"""
        return sum(self.seen)

    def r2(self, window=None):
        """R2：被写过地址集合里的最长连续区间 (字节, 起址, 止址)。

        `window` 用来把统计限制在窗口开头的一段 —— V26 当时的钩子只看
        `0x9F580000 + 0x20000`，要复现它那句 2,126 B 必须给同样的窗口。
        """
        best = [0, 0, 0]
        start = None
        n = len(self.seen) if window is None else min(len(self.seen), window // 2)
        for i in range(n + 1):
            on = i < n and self.seen[i]
            if on and start is None:
                start = i
            elif not on and start is not None:
                ln = (i - start) * 2
                if ln > best[0]:
                    best = [ln, SDRAM + start * 2, SDRAM + i * 2]
                start = None
        return tuple(best)

    def regions(self, min_len=FRAME_BYTES):
        """所有长度 >= `min_len` 的被写连续区间。

        返回 [(起址, 字节, 值种数中位数, 重写半字占比)]，按字节降序。
        """
        out = []
        start = None
        n = len(self.seen)
        for i in range(n + 1):
            on = i < n and self.seen[i]
            if on and start is None:
                start = i
            elif not on and start is not None:
                ln = (i - start) * 2
                if ln >= min_len:
                    out.append(self._describe(SDRAM + start * 2, start, i, ln))
                start = None
        out.sort(key=lambda r: -r[1])
        return out

    def _describe(self, base, lo_hw, hi_hw, ln):
        b0, b1 = (base - SDRAM) // BLK, (base - SDRAM + ln - 1) // BLK
        kinds = [len(self.blk_vals.get(b, ())) for b in range(b0, b1 + 1)]
        kinds.sort()
        med = kinds[len(kinds) // 2] if kinds else 0
        rw = sum(1 for j in range(lo_hw, hi_hw) if self.hits[j] >= 2)
        ratio = 1.0 * rw / (hi_hw - lo_hw) if hi_hw > lo_hw else 0.0
        return (base, ln, med, ratio)

    def blocks(self, limit=12, min_bytes=2048):
        """按写入字节降序的 4 KB 块：[(基址, 字节, 覆盖率, 值种数)]。"""
        out = []
        for b, byt in self.blk_bytes.most_common():
            if byt < min_bytes:
                break
            lo, hi = b * BLK // 2, (b + 1) * BLK // 2
            cov = 100.0 * sum(1 for i in range(lo, hi) if self.seen[i]) / (BLK // 2)
            out.append((SDRAM + b * BLK, byt, cov, len(self.blk_vals.get(b, ()))))
            if len(out) >= limit:
                break
        return out

    def value_kinds(self):
        """「值种数 >= 16 且写满」的块：块数 / 最长连续块数 / 字节 / 地址范围。

        这是**诊断**，不是判据：链表节点区、表区同样满足「值多样」，
        区别在重写率（`regions()` 给）—— 图像要一帧一帧重画。
        """
        good = []
        for b in self.blk_bytes:
            lo, hi = b * BLK // 2, (b + 1) * BLK // 2
            full = all(self.seen[j] for j in range(lo, hi))
            if full and len(self.blk_vals.get(b, ())) >= MIN_VALUE_KINDS:
                good.append(b)
        good.sort()
        best = run = 0
        best_lo = prev = None
        for b in good:
            if prev is not None and b == prev + 1:
                run += 1
            else:
                run, lo_b = 1, b
            if run > best:
                best, best_lo = run, lo_b
            prev = b
        return {"blocks": len(good), "longest_run": best,
                "bytes": best * BLK, "total_bytes": len(good) * BLK,
                "span": ((SDRAM + good[0] * BLK, SDRAM + (good[-1] + 1) * BLK)
                         if good else (0, 0)),
                "longest_at": (SDRAM + best_lo * BLK if best_lo is not None else 0)}

    def expeed(self):
        """B↔EXPEED 位串行汇总（写一次 = 半位，V26/V27 口径）。"""
        per, perr = Counter(), Counter()
        for (a, _pc), c in self.ex_w.items():
            per[a] += c
        for (a, _pc), c in self.ex_r.items():
            perr[a] += c
        pcs = Counter()
        for (a, pc), c in self.ex_w.items():
            pcs[pc] += c
        return {"w": sum(per.values()), "r": sum(perr.values()),
                "bits": sum(per.values()) // 2,
                "by_addr": dict(sorted(per.items())),
                "rd_by_addr": dict(sorted(perr.items())),
                "pcs": pcs}

    def ch3(self):
        """0x50000000 三通道位串行写次数。"""
        return {"w": sum(self.ch3_w.values()), "addrs": len(self.ch3_w)}


def _mk_meter():
    return Meter()


def run_live(steps=800000, boot_steps=blive.BOOT_STEPS, watch=None):
    """V48 口径：`dispatch=True` 的整机，boot 到空闲后自己跑 `steps` 步。"""
    mt = Meter(watch=watch)
    m = create("d300-b", dispatch=True)
    mt.attach(m)
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    n = 0
    while not m.cpu.halted and n < boot_steps:
        m.step(1)
        n += 1
    m.run(max_steps=steps, spin_guard=False)
    return m, mt


def run_v26(steps1=200000, steps2=800000):
    """V26 口径：不建 0x44 设备，手工 `sta_tsk(2)` + 补一次 `vec 0x3F`。"""
    mt = Meter()
    m = create("d300-b")
    mt.attach(m)
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    n = 0
    while not m.cpu.halted and n < blive.BOOT_STEPS:
        m.step(1)
        n += 1
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=steps1, spin_guard=False)
    m.run(max_steps=steps2, spin_guard=False)
    bgate.open_gate(m)
    return m, mt


def verdict(mt):
    """三条件判定，全部用现算值，不写死任何数字。

    帧缓冲必须**同一个连续区间**同时满足 L（够大）/ V（值多样）/ R（被重画）；
    任一不成立就是「没有」。分开判会互相打架：V48 这台机器上
    「值多样」的块有 21 个（节点/表区），但它们几乎不重写 —— 那不是图像。
    """
    regs = mt.regions()
    ex = mt.expeed()
    ok_frame = False
    if regs:
        hits = [r for r in regs
                if r[2] >= MIN_VALUE_KINDS and r[3] >= MIN_REWRITE_RATIO]
        if hits:
            ok_frame = True
            why = ("%d 个区间同时满足：最大一处 %d B @0x%X，值种数中位数 %d，重写 %.0f%%"
                   % (len(hits), hits[0][1], hits[0][0], hits[0][2], hits[0][3] * 100))
        else:
            b = regs[0]
            why = ("最大连续区间 %d B @0x%X 值种数中位数 %d（要 >= %d）、重写 %.0f%%（要 >= %.0f%%）"
                   % (b[1], b[0], b[2], MIN_VALUE_KINDS, b[3] * 100, MIN_REWRITE_RATIO * 100))
    else:
        why = "没有 >= %d B 的连续被写区间" % FRAME_BYTES
    rw = sum(1 for h in mt.hits if h >= 2) if mt.hits else 0
    return {
        "framebuffer": (ok_frame, why),
        "rewrite": (rw * 2 >= FRAME_BYTES,
                    "被写 >= 2 次的半字 %d 个 = %d 字节（一帧 %d B）"
                    % (rw, rw * 2, FRAME_BYTES)),
        "bitstream": (ex["bits"] >= FRAME_BITS,
                      "位串行 %d 位 vs 一帧 %d 位（差 %.0f 倍）"
                      % (ex["bits"], FRAME_BITS,
                         FRAME_BITS / ex["bits"] if ex["bits"] else float("inf"))),
    }


def report(mt, out=print):
    """把计量结果打成一段可读文本。"""
    r1, r2 = mt.r1, mt.r2()
    ex = mt.expeed()
    out("总线写 %d 次" % mt.writes)
    out("SDRAM 被写半字 %d 个 = %d 字节（窗口 %d 字节，覆盖率 %.1f%%）"
        % (mt.halfwords(), mt.halfwords() * 2, SDRAM_SZ,
           100.0 * mt.halfwords() * 2 / SDRAM_SZ))
    out("R1 写入顺序连续：%d 字节 @0x%X" % (r1[0], r1[1]))
    out("R2 地址集合连续：%d 字节 @0x%X..0x%X" % (r2[0], r2[1], r2[2]))
    out("EXPEED 位串行：写 %d / 读 %d ≈ %d 位" % (ex["w"], ex["r"], ex["bits"]))
    out("0x50000000 三通道：写 %d 次 / %d 个地址" % (mt.ch3()["w"], mt.ch3()["addrs"]))
    out("4 KB 块（写 >= 2 KB，前 12）：")
    for base, byt, cov, nv in mt.blocks():
        out("  0x%X  写入 %6d B  覆盖 %5.1f%%  值种数 %4d" % (base, byt, cov, nv))
    out("判定：")
    for k, (ok, why) in verdict(mt).items():
        out("  %-12s %-2s （%s）" % (k, "有" if ok else "没有", why))
