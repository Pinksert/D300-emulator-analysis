# -*- coding: utf-8 -*-
"""tick92.py —— V92：缺省时基与「等待-置位自锁」两个判据（纯函数为主）。

## 这一版回答的两个问题

V54 就有 `dev_tick18`（INT 0x18 系统节拍的硬件拉线模型），但它是**可选**的：
`blive.boot()` 从头到尾没调用过 `enable_auto_tick18()`。于是自 V40 起的每一台
「整机自跑」机器，实际上是在**没有任何节拍源**的条件下跑的。

V92 把这件事量化：

| | period=None | period=256 |
|---|---|---|
| 延时队列 | 3 个，永久积压 | 0 个 |
| 换手 | 34 | 224 |
| 末 PC | `0x0407DC`（空闲任务） | `0x1A4BEE`（任务 16 的 twai_flg 返回点） |
| 内核 tick 计数 | 0 | 2344 |

三个永久积压的任务是 17（`dly_tsk` 1 tick）、7（`tslp_tsk`）、21（`dly_tsk` 0 tick）——
它们等的不是事件位，是**时间**。没有节拍源，时间就是不动的。

## 判据一：period 收敛

period ∈ {32..256} 时逐条换手序列**完全相同**（224 次、末 PC `0x1A4BEE`、
tick 计数 2344）；period ∈ {272,288,320,384,448,512} 各自不同（222/225/220/210/192/186）。

所以「补上时基之后整机推进到哪」这个结论**只在 period 足够密时才稳定**。
阈值 256 是实测的，机制未解（OPEN，别写成「256 是固件某个周期的整数倍」）。

⚠️ 读数坑：`0x68000110` 是 64 位计数器的**高半**，低半在 `+4`。
只读 `0x68000110` 会得到恒 0，误判「ISR 没跑」。ISR 体 `0x040A44` 里
`0x040A5A st #4, r7` / `0x040A5C st #0, r6` 就是这两半的写点。

## 判据二：等待-置位自锁

任务 16 停在 `0x1A4BEE`（`0x1A4BE6 int #0x40` 的返回点，服务号
`0xFFFFFF56` = −170 = `twai_flg`），等 flag 17 的 `0x00000002`。

全 ROM 有 300 个 `flgid=17` 的 `set_flg`，其中能置 `0x2` 的只有 5 个：

    0x1A4C7A  0x1A4CAC  0x1A4D18  0x1A4D3A  0x1A4DC8

它们的**共同点不是「在同一个函数里」**（V68 只看到这一层），而是：

1. 全部落在 `[0x1A4B8A, 0x1A4FBA)` —— 一个 1072 字节、区间内无其它 `enter` 的单一函数；
2. **全部位于 `0x1A4BE6` 那个 `twai_flg(17, 0x2)` 之后**；
3. 动态命中 0/5。

合起来是**等待-置位自锁**：置位代码在等待的下游，任何不先置上 `0x2` 的执行流
都到不了它们。所以「让置位代码跑起来」这条路在这里**结构上不成立**——
`0x2` 的首次置位必须来自这个函数之外（真机是外部事件）。

这与 V68 的提法有实质差别：V68 说「置位点命中 0 ⇒ 缺的是事件流」，
在 flag 17 这一项上不够——它缺的不是「事件把代码推进去」，而是
「代码结构上就在等待之后，事件得直接把 0x2 置上」。

## 工具口径盲区（本版发现）

`svcgraph.wait_guards()` 给这 5 个点报的前驱等待是
`wai_sem(-53) @0x1A4BB0`，漏掉了 `0x1A4BE6`。实测 `bstatic` 把那里读成
`svc=86`：那条服务号装法是 `ldi:32 #0xFFFFFF56`，取低 8 位是 0x56=+86。

这是 `waitstat67` 记过的「宽服务号」缺陷，但**它只修了读 TCB 现场那一路**
（`waitstat67.svc_of_r12(v, 32)`），静态扫描器这一路一直没修。
后果：`WAIT_SVCS` 里虽然含 −170，`CallSite.svc` 却是 86，`wait_guards` 自然漏掉。

本模块用 `fix_wide()` 在**判据侧**补上（只在需要判「在不在等待之后」时用），
不去改 `bstatic` 的公共解码 —— 那个值被历史结论大量引用，改口径要另开一版。
偏差清单见 `WIDE_MISREAD`。
"""
from __future__ import unicode_literals

from . import bstatic, btick, dev_tick18

# ---- 节拍 ---------------------------------------------------------------
#: 64 位内核 tick 计数器的两半（ISR 体 0x040A5A/0x040A5C 的写点）
TICK_COUNTER_HI = 0x68000110
TICK_COUNTER_LO = 0x68000114
#: ISR 入口 / ISR 体 / 延时队列哨兵（与 dev_tick18 同源，不重定义）
TICK_ISR = dev_tick18.ISR
TICK_BODY = dev_tick18.TICK_HANDLER
DELAY_SENTINEL = btick.SENTINEL

#: 实测收敛阈值：period ≤ 此值时逐条换手序列完全相同
CONVERGE_PERIOD = 256
#: 本版实测的周期扫描点（报告与自检共用这一份，别各写一份）
PERIODS_V92 = (None, 512, 448, 384, 320, 288, 272, 256, 240, 224,
               192, 160, 128, 96, 64, 32)

#: 缺省时基下永久积压的三个延时任务（V92 实测：任务号, 服务号）
STUCK_V92 = ((17, -85), (7, -37), (21, -85))

#: 任务 16 的等待点与它等的对象
TASK16_WAIT_PC = 0x1A4BEE
TASK16_WAIT_SVC = -170
TASK16_FLGID = 17
TASK16_WANT = 0x00000002

#: flag 17 那个单一函数（body_range 实测 1072 字节、区间内无其它 enter）
F17_FUNC = 0x1A4B8A
F17_FUNC_END = 0x1A4FBA
#: 能置 0x2 的 5 个 set_flg
F17_SET_PCS = (0x1A4C7A, 0x1A4CAC, 0x1A4D18, 0x1A4D3A, 0x1A4DC8)

#: `bstatic` 宽服务号的误读表：读到 8 位正数 → 实为 32 位负数
#: （`ldi:32 #0xFFFFFF56` 取低 8 位 = 0x56 = +86，实为 −170）
#:
#: ⚠️ V94：`bstatic` 已改为按装载宽度解码，**这张表不再是必需补丁**；
#: 保留它是因为本模块与几个探针在判据侧还在调用 `fix_wide()`（幂等，无害）。
WIDE_MISREAD = {86: -170, 85: -171}
#: 判「等待」时纳入的服务号（含宽服务号的误读形态）
WAIT_SVC_SET = frozenset((-46, -170, -41, -37, -53, -61)) | frozenset(WIDE_MISREAD)


def fix_wide(svc):
    """历史口径补丁。V94 之后 `bstatic` 直接给 32 位值，本函数退化为恒等。

    留着是为了「跨版本对照」：同一份 ROM，把 `bstatic` 的新口径再喂进这里
    不会二次偏移（`{86:−170}` 对 −170 不命中）；反过来，**如果谁又拿到 +86，
    说明有模块退回了 8 位解码**，这是本函数现在唯一的用处。
    """
    return WIDE_MISREAD.get(svc, svc)


# ------------------------------------------------------------------ 节拍读数
def kernel_tick(m):
    """读 64 位内核 tick 计数（高半在 0x68000110，低半在 +4）。

    只读高半会恒得 0 —— 这是本版踩过的坑，单独做成函数免得再踩。
    """
    hi = m.bus.rd(TICK_COUNTER_HI, 4)
    lo = m.bus.rd(TICK_COUNTER_LO, 4)
    return (hi << 32) | lo


def delay_rows(m):
    """延时队列 → `[(任务号, 服务号)]`（`None` 任务号表示翻不回来）。"""
    return [(tid, why) for _tcb, tid, _prio, why in btick.delay_queue(m)]


def stuck(delay):
    """延时队列里是否仍有 V92 那三个任务（用来判「时基没补」）。"""
    ids = {tid for tid, _why in delay}
    return [tid for tid, _w in STUCK_V92 if tid in ids]


# ------------------------------------------------------------------ 受控对比
def run_one(period=None, steps=600000, boot_steps=None, keep=False):
    """新建一台独立机器跑一遍。

    ⚠️ 每次都新建（KI#163：同一台机器跑第二遍会失效）。
    `keep=True` 时把机器一并返回，供后续取证；否则不留在结果里（省内存）。
    """
    from . import blive, machine as _m, brealos
    if boot_steps is None:
        boot_steps = blive.BOOT_STEPS
    m = _m.create("d300-b", dispatch=True)
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    if period is not None:
        m.enable_auto_tick18(period=period)
    n = 0
    while not m.cpu.halted and n < boot_steps:
        m.step(1)
        n += 1
    r = blive.run(m, steps=steps)
    sw = r["switches"]
    seq = [s[0] for s in sw] if isinstance(sw, list) else []
    dev = m.tick18_clock.state() if getattr(m, "tick18_clock", None) else {}
    row = {
        "period": period,
        "steps": m.step_count,
        "pc": m.cpu.pc,
        "current": r["current_name"],
        "switches": len(seq),
        "seq": seq,
        "tick": kernel_tick(m),
        "fired": dev.get("fired"),
        "skipped": dev.get("skipped"),
        "delay": delay_rows(m),
        "ran": [t["id"] for t in brealos.states(m) if t["ran"]],
    }
    if keep:
        row["machine"] = m
    return row


def matrix(periods=PERIODS_V92, steps=600000):
    """周期扫描：逐项返回 `run_one` 的行（每行一台独立机器）。"""
    return [run_one(p, steps=steps) for p in periods]


def converge_bound(rows):
    """从扫描结果里取「逐条序列与最快那台相同」的最大周期。

    返回 `(bound, ref_row, same, diff)`：`same` 是序列相同的周期列表，
    `diff` 是不同的。基准取扫描点里周期最小的那台（tick 最密）。
    """
    real = [r for r in rows if r["period"] is not None]
    if not real:
        return (None, None, [], [])
    ref = min(real, key=lambda r: r["period"])
    same, diff = [], []
    for r in rows:
        (same if r["seq"] == ref["seq"] else diff).append(r["period"])
    bound = max([p for p in same if p is not None], default=None)
    return (bound, ref, same, diff)


def first_diff(a, b):
    """两条换手步数序列的首个差异 → `(下标, a 值, b 值)`；无差异返回 None。"""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return (i, x, y)
    if len(a) != len(b):
        return ("长度", len(a), len(b))
    return None


# ---------------------------------------------- 等待-置位自锁（纯静态）
def setters(sweep, flgid, want):
    """全 ROM 里「常量可见、能置上 want」的 set_flg 调用点。"""
    out = []
    for c in sweep.calls:
        if c.svc != bstatic.SET_FLG:
            continue
        if c.flgid != flgid:
            continue
        if isinstance(c.bits, int) and (c.bits & want):
            out.append(c)
    return out


def waits_between(sweep, lo, hi):
    """区间 `[lo, hi)` 内的等待类调用点（按地址升序，svc 已按 32 位口径修正）。"""
    out = [c for c in sweep.calls
           if lo <= c.addr < hi and fix_wide(c.svc) in WAIT_SVC_SET]
    return sorted(out, key=lambda c: c.addr)


def self_lock(sweep, flgid=TASK16_FLGID, want=TASK16_WANT,
              func=F17_FUNC, end=F17_FUNC_END):
    """判「这个 flag 的置位点是否全部位于同一函数的等待之后」。

    返回 dict：
      `points` 置位点 / `in_func` 落在函数内的 / `outside` 函数外的 /
      `waits` 函数体内的等待点 / `all_after` 是否全部在某条等待之后 /
      `guard` 最近的那条等待 / `locked` 结论（in_func==points 且 all_after）
    """
    pts = setters(sweep, flgid, want)
    in_func = [c for c in pts if func <= c.addr < end]
    outside = [c for c in pts if not (func <= c.addr < end)]
    waits = waits_between(sweep, func, end)
    after = []
    guard = None
    for c in in_func:
        before = [w for w in waits if w.addr < c.addr]
        g = before[-1] if before else None
        after.append(g is not None)
        if g is not None and (guard is None or g.addr > guard.addr):
            guard = g
    all_after = bool(in_func) and all(after)
    return {
        "points": pts,
        "in_func": in_func,
        "outside": outside,
        "waits": waits,
        "all_after": all_after,
        "guard": guard,
        "locked": (len(in_func) == len(pts) and all_after and not outside),
    }


def verdict_bound(bound=CONVERGE_PERIOD):
    """收敛结论的短句（报告与自检引用同一份措辞）。"""
    return "period<=%d 时逐条换手序列相同" % bound


def verdict_lock(lock):
    """自锁结论的短句。"""
    if lock["locked"]:
        g = lock["guard"]
        return ("%d 个置位点全在 [0x%06X,0x%06X) 内、全在等待 0x%06X(%d) 之后 ⇒ "
                "等待-置位自锁，首次置位必来自函数外"
                % (len(lock["points"]), F17_FUNC, F17_FUNC_END,
                   g.addr if g else 0, fix_wide(g.svc) if g else 0))
    return "置位点不构成自锁（存在函数外或等待前的置位点）"
