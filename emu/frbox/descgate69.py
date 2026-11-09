# -*- coding: utf-8 -*-
"""descgate69.py —— V69：描述符派发链的「门」判据（纯函数，不建机器）。

V61–V68 把 ``0x12E752`` 列进「显示链 5 PC」、叫它「第二个卡点 / 门」。
V69 静态证伪：它是 ``97 20`` 一条裸 ``ret``，70 个描述符槽位里 **27 个**
指向它；5 个裸 ret 处理函数共占 31 个槽位，实做的只有 39 个。
44 个处理函数**没有一个**有静态直接调用边 ⇒ 它们靠 ``+0x0C`` 函数指针
间接派发。所以「0x12E752 未命中」不等于「第二道门没过」，而是
**整条 70 槽位描述符派发链一次都没跑**。

两条最容易再踩的纪律（硬规矩 52 / 53）：

* 52 把某 PC 叫「门」之前，先证它不是裸 ``ret`` 的空实现、且有静态
  直接调用边。裸 ret + 多槽位指向 + 0 直接边 ⇒ 是**空槽位哨兵**。
* 53 「写数据门」与「给调度事件」是两层：V65+ 收敛态里 CPU 在
  ``0x0407DC`` 空闲任务自旋、17 个任务全在等待，此时把门字节
  ``0x9F58D08F`` 或状态字 ``0x9F58D450`` 的 bit6 写上，收敛态新增
  覆盖 **0 个 PC** —— 没有任务在读它。V25 的「开门有效」是在
  ``brealos.boot()`` + 100 万步那台主循环还活着的机器上做的，
  **两台机器不可混比**。反事实必须报清「有没有调度事件」这一层。

另：在收敛机器上直接调 ``0x12E1EC`` 做决定性实验会撞工具边界 ——
函数里的 ``wai_sem(17)`` 走 ``int #0x40``，任务被挂起后 PC 落进
``0x0404E0`` 默认 ISR，清空挂起中断也没用。这不是显示链结论。
"""

RET_BYTES = b"\x97\x20"

NOOP_HANDLER = 0x0012E752
GATE = 0x9F58D08F
STATUS_A = 0x9F58D450
STATUS_WORK_READY = 0x40000000      # 字口径：字节 +0 的 bit6
CURSOR_FN = 0x0012E1EC
SUBMIT = 0x0012E6F8
WORK_ENTRY = 0x00120184
DESC_PTR = 0x9F58D858
CURSOR_BASE = 0x9F58DA7C
EVENT_MASK = 0x9F58DA81
CURSOR_SLOT = 0x9F58DA82
WORD_A = 0x9F5A0284
WORD_B = 0x9F59EF50
SLOTS8 = 0x9F59F7C8

DSP_PCS = (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752)

__all__ = ["RET_BYTES", "NOOP_HANDLER", "GATE", "STATUS_A",
           "STATUS_WORK_READY", "CURSOR_FN", "SUBMIT", "WORK_ENTRY",
           "DESC_PTR", "CURSOR_BASE", "EVENT_MASK", "CURSOR_SLOT",
           "WORD_A", "WORD_B", "SLOTS8", "DSP_PCS",
           "is_ret", "group_handlers", "ret_handlers", "noop_slots",
           "real_handlers", "sentinel_verdict", "classify_access",
           "chain_coverage", "handler_coverage", "door_delta",
           "equal_shape", "window_same", "dispatch_verdict"]


def is_ret(head2):
    """函数开头两字节是不是单条 ``ret``。"""
    return bytes(head2) == RET_BYTES


def group_handlers(entries):
    """``[{index, handler}, ...]`` → ``{handler: [槽位号...]}``（地址升序）。"""
    g = {}
    for e in entries:
        h = e["handler"] if isinstance(e, dict) else e[1]
        i = e["index"] if isinstance(e, dict) else e[0]
        g.setdefault(h, []).append(i)
    return {k: sorted(v) for k, v in sorted(g.items())}


def ret_handlers(groups, head_of):
    """裸 ret 的处理函数地址列表。``head_of(h)`` 返回该函数开头 2 字节。"""
    return sorted(h for h in groups if is_ret(head_of(h)))


def noop_slots(groups, noop=NOOP_HANDLER):
    """指向空实现 ``0x12E752`` 的槽位号。"""
    return sorted(groups.get(noop, []))


def real_handlers(groups, rets):
    """除裸 ret 之外的实做处理函数地址。"""
    s = set(rets)
    return sorted(h for h in groups if h not in s)


def sentinel_verdict(head2, slots, direct_edges):
    """判某 PC 是「门」还是「空槽位哨兵」。

    返回 ``{"is_ret", "slots", "direct_edges", "verdict", "reason"}``。
    三条同时成立才判 ``empty_slot``：裸 ret + 至少 1 个槽位指向 +
    0 条静态直接调用边。任一不成立都只给 ``inconclusive``，不许硬判门。
    """
    r = is_ret(head2)
    n = len(slots)
    e = len(direct_edges)
    ok = r and n > 0 and e == 0
    return {
        "is_ret": r, "slots": n, "direct_edges": e,
        "verdict": "empty_slot" if ok else "inconclusive",
        "reason": ("裸 ret + %d 个槽位指向 + 0 静态直接调用边 ⇒ 空实现槽位，不是门"
                   % n) if ok else
                  ("不满足三条判据（is_ret=%s slots=%d edges=%d），不能下结论"
                   % (r, n, e)),
    }


def classify_access(rows):
    """``[(pc, kind, text)]`` → ``{kind: n}``；并给读/写两档总数。

    ``ldub`` 等加载归 'other'（读），``stb`` 归 'store'（写）。
    """
    c = {}
    for _p, k, _t in rows:
        c[k] = c.get(k, 0) + 1
    return {"kinds": dict(sorted(c.items())),
            "reads": sum(v for k, v in c.items() if k != "store"),
            "writes": c.get("store", 0)}


def chain_coverage(cov, pcs):
    """关键 PC 的命中表 ``{pc: bool}``。"""
    return {p: p in cov for p in pcs}


def handler_coverage(cov, handlers):
    """44 个处理函数里命中几个。"""
    hit = sorted(h for h in handlers if h in cov)
    return {"hit": hit, "n_hit": len(hit), "n_total": len(handlers),
            "all_zero": len(hit) == 0}


def door_delta(base_cov, end_cov, dsp=DSP_PCS):
    """一组反事实的新增覆盖与显示链新增命中。"""
    new = set(end_cov) - set(base_cov)
    return {"new": sorted(new), "n_new": len(new),
            "dsp_new": [p for p in dsp if p in new]}


def equal_shape(groups):
    """等形对照校验：每组步数/服务次数/中断次数是否一致。

    ``groups`` 是 ``{tag: {boot, post, svc, irqs, ...}}``。
    只比较四个共有键；反事实补全带来的额外步数要写进 ``extra`` 单列，
    不参与等形判定。
    """
    keys = ("boot", "post", "svc", "irqs")
    vals = {k: {g[k] for g in groups.values() if k in g} for k in keys}
    bad = [k for k, s in vals.items() if len(s) > 1]
    return {"ok": not bad, "bad": bad, "values": {k: sorted(s) for k, s in vals.items()}}


def window_same(a, b):
    """两个窗口的收敛自证：末 PC / 覆盖 / 显示链逐项相同。

    ``a``/``b`` 是 ``{pc, cov, dsp, states}``；返回差异字段列表。
    """
    diff = []
    if a.get("pc") != b.get("pc"):
        diff.append("pc")
    if set(a.get("cov", ())) != set(b.get("cov", ())):
        diff.append("cov")
    if dict(a.get("dsp", {})) != dict(b.get("dsp", {})):
        diff.append("dsp")
    if dict(a.get("states", {})) != dict(b.get("states", {})):
        diff.append("states")
    return {"same": not diff, "diff": diff,
            "symdiff_cov": len(set(a.get("cov", ())) ^ set(b.get("cov", ())))}


def dispatch_verdict(rows):
    """四组等形反事实的门归因：只有补了调度事件的那组才有新增覆盖。

    ``rows`` 是 ``[(tag, n_new, has_tick), ...]``。返回
    ``{"zero_groups", "nonzero_groups", "attribution", "door_only_useless"}``。
    """
    zero = [t for t, n, _k in rows if n == 0]
    nonzero = [(t, n) for t, n, _k in rows if n > 0]
    tick_only = all(k for _t, n, k in rows if n > 0)
    return {
        "zero_groups": zero, "nonzero_groups": nonzero,
        "attribution": "tick" if nonzero and tick_only else "mixed",
        "door_only_useless": bool(zero) and all(
            not k for t, n, k in rows if t in zero and n == 0),
    }
