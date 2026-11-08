# -*- coding: utf-8 -*-
"""frbox.flaggraph —— A/B 段通用的「事件标志位级服务图」（V59，纯函数）

把 `bstatic.Sweep` 静态扫出来的服务调用点，按 **(flgid, bit)** 拆开配对：
谁在等这一位、谁在置这一位、谁在清这一位。V58 留下的
「三道 `wai_flg` 的置铃端在哪」需要的是位级粒度，而不是服务号粒度。

## 三条口径（必须随结论一起写）

1. **常量可见**：`Sweep` 是线性寄存器跟踪扫出来的，实参可能是 `None`
   （运行时变量 / 表里取的）。`None` 一律不进配对表，单独记 `unknown`。
   「置铃端 0 处」= **常量可见口径**，不等于「固件里没有置位代码」。
2. **`clr_flg` 的位语义**：按硬规矩 20，`clr_flg(id, mask)` 清的是
   **mask 里为 0 的位**（`pattern & mask`），不是为 1 的位。
   `0xFFFFDFFF` 唯一为 0 的是 bit13 ⇒ 清 `0x2000`。
3. **`mode` 不解释**：`wai_flg` 的 mode 只原样记录。ANDW / ORW / CLR
   的组合语义本项目没有在固件侧实证过，不拿它推「等到什么条件才返回」。

## 与动态的关系（硬规矩 5）

本模块**不建机器、不 boot、不写内存、不挂设备**，
`flow_table()` / `flag_snapshot()` 只是把探针传进来的已观测数据格式化。
「有置铃点」不等于「置铃点跑过」；「无置铃点」也不等于「位永远不会被置」
—— 后者还可能来自 ISR 直接写 flag 对象内存、运行时变量或跨段输入。
`kernel_store_probe()` 报的「静态可定址写点 0 笔」同样是工具口径的阴性结果。

## 函数归属（硬规矩 30）

`Sweep.enclosing()` 只是「最近的 `enter`」；没有 `enter` 的小函数会被
归到前一个 `enter` 名下。本模块沿用这一粗粒度，不允许据此断定两条 PC
在同一条线性执行路径上（V59 `0x081774` 与 `0x081490` 即属此类）。
"""
import collections

from . import bstatic

#: 参与位级配对的服务（服务号按 8 位补码，硬规矩 23）
FLAG_SVCS = (bstatic.SET_FLG, bstatic.CLR_FLG,
             bstatic.WAI_FLG, bstatic.TWAI_FLG)

SET_SVCS = frozenset((bstatic.SET_FLG,))
CLR_SVCS = frozenset((bstatic.CLR_FLG,))
WAIT_SVCS = frozenset((bstatic.WAI_FLG, bstatic.TWAI_FLG))


def expand_bits(mask):
    """32 位掩码 → 置 1 的位列表（升序）。`None` 返回空表。"""
    if not isinstance(mask, int):
        return []
    return [1 << i for i in range(32) if mask & (1 << i)]


def zero_bits(mask):
    """32 位掩码 → 为 0 的位列表（升序）。`clr_flg` 用的就是这一组（硬规矩 20）。"""
    if not isinstance(mask, int):
        return []
    return [1 << i for i in range(32) if not (mask & (1 << i))]


def _bit_group(call):
    """按服务号决定该调用点涉及哪些位。"""
    if call.svc in WAIT_SVCS or call.svc in SET_SVCS:
        return expand_bits(call.bits)
    if call.svc in CLR_SVCS:
        return zero_bits(call.bits)
    return []


def pair_table(sweep):
    """全 ROM 的位级配对表。

    返回 dict：
      `pairs`    {(flgid, bit): {"waits": [...], "sets": [...], "clrs": [...]}}
      `unknown`  Counter，按服务号记「实参不可静态确定」的调用点数
      `totals`   Counter，按服务号记总调用点数
      `no_flgid` 连 flgid 都不可定的调用点数
    """
    pairs = collections.defaultdict(
        lambda: {"waits": [], "sets": [], "clrs": []})
    unknown = collections.Counter()
    totals = collections.Counter()
    no_flgid = 0
    for c in sweep.calls:
        if c.svc not in FLAG_SVCS:
            continue
        totals[c.svc] += 1
        if c.flgid is None:
            no_flgid += 1
            continue
        bits = _bit_group(c)
        if not bits:
            unknown[c.svc] += 1
            continue
        key = "waits" if c.svc in WAIT_SVCS else (
            "sets" if c.svc in SET_SVCS else "clrs")
        for b in bits:
            pairs[(c.flgid, b)][key].append(c)
    return {"pairs": dict(pairs), "unknown": unknown,
            "totals": totals, "no_flgid": no_flgid}


def unmatched(table):
    """「有等无置」的位 → `[(flgid, bit, waits, sets)]`，按等待点数降序。"""
    out = [(f, b, d["waits"], d["sets"])
           for (f, b), d in table["pairs"].items()
           if d["waits"] and not d["sets"]]
    return sorted(out, key=lambda x: (-len(x[2]), x[0], x[1]))


def setters_for(table, flgid, bit):
    """某 (flgid, bit) 的常量可见置铃点 → `[CallSite]`。"""
    return list(table["pairs"].get((flgid, bit), {}).get("sets", ()))


def waiters_for(table, flgid, bit):
    return list(table["pairs"].get((flgid, bit), {}).get("waits", ()))


def group_by_flag(sweep, svc):
    """某 flag 服务的调用点按 flgid 分组（含 `None` 桶），返回 `{flgid: [CallSite]}`。"""
    out = collections.defaultdict(list)
    for c in sweep.calls:
        if c.svc == svc:
            out[c.flgid].append(c)
    return dict(out)


def cluster(sweep, index, calls, window=8):
    """一组调用点 → 所属函数 → 引用形态分类（互斥口径）。

    返回 `{"funcs": [...], "classes": {func: (kind, [Ref])},
            "upstream": {func: [引用者的 enclosing 函数]}}`。
    `index` 是 `svcgraph.ImmIndex`；引用形态的含义/边界见 `svcgraph` 文档。
    """
    funcs = sorted({sweep.enclosing(c.addr) for c in calls
                    if sweep.enclosing(c.addr) is not None})
    classes = {f: index.classify(f, window) for f in funcs}
    upstream = {}
    for f in funcs:
        refs = [r.pc for r in classes[f][1] if r.kind in
                ("ldi20", "ldi32", "rel12")]
        upstream[f] = sorted({u for u in
                              (sweep.enclosing(p) for p in refs)
                              if u is not None})
    return {"funcs": funcs, "classes": classes, "upstream": upstream}


def guard_map(sweep, func, points, wait_svcs=None):
    """函数体内每个调用点之前最近的等待类服务 → `[(pc, CallSite|None)]`。

    `wait_svcs` 默认用 `svcgraph.WAIT_SVCS`（含 `wai_flg` / `twai_flg` /
    `sem_wai` / `rcv_msg`）；V59 只关心 flag 等待，可显式传
    `(bstatic.WAI_FLG, bstatic.TWAI_FLG)`。
    """
    if wait_svcs is None:
        from . import svcgraph
        wait_svcs = svcgraph.WAIT_SVCS
    wait_svcs = frozenset(wait_svcs)
    import bisect
    i = bisect.bisect_right(sweep.funcs, func)
    hi = sweep.funcs[i] if i < len(sweep.funcs) else sweep.base + len(sweep.rom)
    waits = [c for c in sweep.calls
             if func <= c.addr < hi and c.svc in wait_svcs]
    out = []
    for p in sorted(points):
        prev = [w for w in waits if w.addr < p]
        out.append((p, max(prev, key=lambda w: w.addr) if prev else None))
    return out


def kernel_store_probe(sweep, lo, hi):
    """[lo, hi) 内的**静态可定址** store 写点 → `[(pc, addr, mnemonic)]`。

    `Sweep.stores` 只收能在静态寄存器跟踪里定出地址的 store；
    基址来自运行时寄存器的写点看不到。命中 0 笔是工具口径的阴性结果，
    不能反推固件不存在直接写 flag 对象内存的路径（硬规矩 31 同类）。
    """
    return [(pc, a, mn) for pc, a, mn in sweep.stores if lo <= a < hi]


def flow_table(flow_calls, name_of=lambda s: ""):
    """把 `flow.calls` 格式化成 dict 列表（纯函数，不碰机器）。

    `flow.calls` 每项至少是 `(step, pc, svc, r4, r5, r6, r7, ...)`。
    """
    out = []
    for c in flow_calls:
        out.append({"step": c[0], "pc": c[1], "svc": c[2],
                    "name": name_of(c[2]),
                    "r4": c[3], "r5": c[4], "r6": c[5], "r7": c[6]})
    return out


def flag_snapshot(flags):
    """`realos.flags()` / `bflag` 同形数据 → 精简 dict 列表（纯函数）。"""
    return [{"id": f["id"], "cb": f["cb"], "pattern": f["pattern"],
             "waiters": f["waiters"]} for f in flags]
