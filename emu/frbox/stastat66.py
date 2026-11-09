# -*- coding: utf-8 -*-
"""stastat66.py —— `sta_tsk` 调用的四层判定（V66，纯函数 / 只读）。

V65 收敛态报「27 个 sta_tsk 命中 16」，但 KI#253 写明「PC 覆盖只证
int #0x40 指令执行过，服务返回码、TCB 就绪态没取」。本模块把「命中」
拆成四个互不替代的层：

    1 受理      服务返回码 err == E_OK(0)
    2 已激活    TCB 脱离 DORMANT 且分配了 saved_sp
    3 上过 CPU  任务入口 PC 被覆盖率记为执行过
    4 收敛状态  当前 state / 就绪位图 / 队列占用

⚠️ **最容易选错的判据**：不能用 ``state == 0`` 或「在就绪队列上」当
「已激活」。收敛时这些任务早已跑完并挂起（state=0x04）、就绪队列清空，
用旧判据会得到「0 个建好 / 16 个入口跑过」的自相矛盾结果。

⚠️ **返回码必须来自真内核**：挂了 ``attach_itron()`` / ``attach_kernel()``
时 ``svc_hook`` 非 None，`int #0x40` 被半主机直接返回常量 E_OK，
此时 r12 没有判别力。`blive.boot(0)` 默认 svc_hook=None，走真向量表
0x040668。

⚠️ **返回点 +2 会命中两次**：`reti` 回 int_pc+2 后下一步进 0x040D32
派发器，当前任务被换出；任务重新调度回来时从 +2 续跑，于是同一 PC
再命中一次。**配对只取调用之后的第一次命中**，且不能把 ×2 当成
「服务被调用两次」。

⚠️ ``brealos.kernel_call`` 注入工具在 boot 后**第一次**调用必跑满
80000 步上限、r12 是垃圾值 748（与参数无关）。这是工具边界不是内核
行为，用之前先显式预热一次。
"""

# ---- TCB 状态字节（bready.py:78 的口径）---------------------------------
READY = 0x00
WAIT = 0x04
DORMANT = 0x10
E_OK = 0

#: `reti` 回到的地址 = `int` 指令地址 + 指令长度（FR 的 int #0x40 是 2 字节）
INT_SIZE = 2

#: 真内核陷阱入口 / 系统调用后的派发器
TRAP_ENTRY = 0x040668
DISPATCH_ENTRY = 0x040D32


def signed(v):
    """32 位无符号 → 有符号返回码。"""
    return v - 0x100000000 if v & 0x80000000 else v


def return_pcs(pcs, size=INT_SIZE):
    """{返回点 PC: 调用点 PC}。"""
    return dict((p + size, p) for p in pcs)


def pair_returns(log, sta_pcs, size=INT_SIZE):
    """把观察流水配成 [(call_pc, err, replay)]，只取调用后第一次 +size 命中。

    ``log`` 是 ``(step, pc, r4, r5, r12, cur_tcb)`` 的列表（升序步序）。
    ``err`` 取 32 位补码；``replay`` 是该返回点全部命中次数。
    """
    watch = set(sta_pcs)
    rets = [x for x in log if (x[1] - size) in watch]
    replay = {}
    for x in rets:
        replay[x[1]] = replay.get(x[1], 0) + 1
    ret_sorted = sorted(rets, key=lambda x: x[0])
    calls = [x for x in log if x[1] in watch]
    out = []
    for c in calls:
        nxt = next((r for r in ret_sorted
                    if r[0] > c[0] and r[1] == c[1] + size), None)
        out.append((c[1], signed(nxt[4]) if nxt else None,
                    replay.get(c[1] + size, 0)))
    return out


def is_activated(row):
    """TCB 是否脱离 DORMANT 且已分配任务栈。

    ``row`` 至少要有 ``state`` 与 ``sp``。**不检查就绪队列**：收敛时队列
    已清空，但任务确实建好过。
    """
    return row.get("state") != DORMANT and row.get("sp", 0) != 0


def classify(pair_rows, tcb_rows, static_tids=None):
    """四层判定，返回 dict。

    ``pair_rows``: [{tid, err, ...}]（None 的 tid 会被跳过）
    ``tcb_rows``:  [{tid, state, sp, ran, queues, ...}]
    ``static_tids``: 全 ROM `sta_tsk` 覆盖的任务号集合（可为 None）
    """
    by_tid = {}
    for r in pair_rows:
        if r.get("tid") is not None:
            by_tid.setdefault(r["tid"], []).append(r)
    tcb = dict((x["tid"], x) for x in tcb_rows)

    accepted = sorted(t for t, rs in by_tid.items()
                      if all(r.get("err") == E_OK for r in rs))
    activated = sorted(t for t in accepted
                       if t in tcb and is_activated(tcb[t]))
    ran = sorted(t for t in accepted if t in tcb and tcb[t].get("ran"))
    waiting = sorted(t for t in ran
                     if t in tcb and tcb[t].get("state") == WAIT)
    dormant = sorted(x["tid"] for x in tcb_rows
                     if x.get("state") == DORMANT)
    no_launcher = (sorted(t for t in dormant if t not in static_tids)
                   if static_tids is not None else None)
    return {
        "accepted": accepted,
        "activated": activated,
        "ran": ran,
        "waiting": waiting,
        "dormant": dormant,
        "no_launcher": no_launcher,
        "gap_1_2": sorted(set(accepted) - set(activated)),
        "gap_2_3": sorted(set(activated) - set(ran)),
    }


def layers_overlap(res):
    """三层是否完全重合（本版对 16 个任务的实测结论）。"""
    return (res["accepted"] == res["activated"] == res["ran"]
            and not res["gap_1_2"] and not res["gap_2_3"])
