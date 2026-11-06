# -*- coding: utf-8 -*-
"""bready.py —— B 段 REALOS 的「就绪结构」，以及任务 5 是怎么被调度上去的（V39）。

一、先纠一个地址

    V35–V38 一直把 `0x45984` 当成 B 段的就绪位图。那是 **A 段** 的位图地址：
    A 段 RAM 是 0x1000–0x80000，0x45984 落在 RAM 里；而 B 段 ROM 从 0x40000 起、
    长 7 MB，**0x45984 在 B 段是 ROM**。往它写值会被总线丢弃，读回来的是固件
    字节（0x2E80A800），所以「它一直不变」是必然的 —— 不是内核没更新位图。

    真正的就绪结构在 REALOS 的内核对象区，紧贴 TCB 区（见下表）。

二、内核区头部的布局（boot 后实测，0x68000000 起）

    +0x00  当前 TCB                       boot 后 = 空闲任务 0x6800060C
    +0x04  就绪位图（32 位，大端）        boot 后 = 0x80000000（只有队列 0）
    +0x08  就绪队列哨兵 第 0 号（8 字节）  每个哨兵 {next, prev}，空队列自指
    +0x10  就绪队列哨兵 第 1 号
    …      每 8 字节一号

    两条关系在 boot 后与 `sta_tsk(5)` 后两个状态里 100% 成立：

        队列号 = 优先数 − 1        （优先数 = TCB +0x0C 的高 16 位）
        位号   = 31 − 队列号       （即位图第 31 位对应队列 0 = 最高优先）

    所以「优先数 1 / 4」的两条任务分别落在队列 0 / 3、位 31 / 28。

三、sta_tsk 到底做了什么（一次调用 128 步，全部写点）

    0x680001F4 ← 0        清 TCB +0x08 那个字的低半
    0x68000204 ← 0x9F8036A4   存栈顶（TCB +0x18）
    0x68000024 / 0x68000020 ← 0x680001EC   把 TCB 挂进队列 3 的尾
    0x680001EC / 0x680001F0 ← 0x68000020   TCB 的 next/prev 指向哨兵（空队列尾插）
    0x68000004 ← 0x90000000   置位图第 28 位
    0x00000044 ← 1            **写「请求调度」寄存器**（0x41886）

    最后这一笔是关键：内核自己请求了派发，只是这台机器上 0x44 没建模（默认
    `dispatch=False`，V2–V10 的基线前提），于是请求落到影子存储、没人应答。

四、派发器在哪、怎么选

    vec 0x3F → 0x00040D32。ICR 在 0x46F（0x440 + (0x3F − 0x10)），boot 后 = 0x1E，
    是启用向量里级别号最大的一个（最低优先级）—— 和 A 段同一套安排。

    选中规则是「位图里最高的那一位」= 队列号最小 = 优先数最小。实测：

        空闲（队列 0 空）→ 挂一次 0x3F → 任务 2（优先数 1，队列 0）
        任务 2 阻塞在 flag 3 → 再挂一次 → 任务 4（优先数 3，队列 2）
        任务 4 也阻塞 → 再挂一次 → **任务 5（优先数 4，队列 3）**

五、任务 5 上 CPU 之后

    它走 0x12DFAE → 0x12DFB8 → `call 0x12E006`，显示初始化**自己跑完**：
    队列哨兵 0x9F58D500 自指、70 个槽位编号、`0x9F58DA85 = 1`。
    V38 手动调用的那个 746 步，这一版是内核把任务 5 送上去之后发生的。

    ⚠️ 但这一次调度**不是固件自己完成的**：0x44 没建模，所以是探针在「当前任务
       状态字节 ≠ 0（阻塞/休眠）」时补了一次 0x3F。谁补的必须说清楚 —— 这不是
       固件的原始触发链，而是补上了缺失的硬件应答。见 `run_until()`。
"""

MASK = 0xFFFFFFFF

# ---- 内核区头部 -----------------------------------------------------------
KERNEL = 0x68000000
CUR_TCB = 0x68000000          # 当前任务 TCB
READY_MAP = 0x68000004        # 32 位就绪位图
QUEUE0 = 0x68000008           # 第 0 号就绪队列哨兵
QUEUE_STRIDE = 8              # 每个哨兵 {next, prev}
QUEUE_MAX = 32                # 位图 32 位 → 最多 32 条队列

# ---- 容易认错的地址 -------------------------------------------------------
#: A 段的就绪位图（realos.py 用的）。在 B 段这段地址是 **ROM**：读得到固件字节、
#: 写进去被丢弃。V35–V38 把它当 B 段位图，才有了「位图恒 0x2E80A800」的误会。
A_READY_MAP = 0x45984

# ---- TCB 字段 -------------------------------------------------------------
TCB_STATE = 0x0B              # 状态字节：0 就绪 / 4 等待 / 0x10 休眠
TCB_PRIO = 0x0C               # 高 16 位 = 优先数
TCB_SP = 0x18                 # 保存的用户栈指针

# ---- 调度 ----------------------------------------------------------------
DISPATCH_VEC = 0x3F
DISPATCH_ICR = 0x440 + (DISPATCH_VEC - 0x10)    # 0x46F
DISPATCH_ENTRY = 0x00040D32
DISPATCH_REQ = 0x44           # 「请求调度」寄存器（直接页）

#: 任务 5：入口与它要调的那段显示初始化
TASK5_ID = 5
TASK5_ENTRY = 0x0012DFAE
DISPLAY_INIT = 0x0012E006


# --------------------------------------------------------------------- 读
def current(m):
    """当前任务的 TCB 地址。"""
    return m.bus.rd(CUR_TCB, 4)


def ready_map(m):
    """就绪位图（32 位）。"""
    return m.bus.rd(READY_MAP, 4)


def queue_addr(i):
    """第 i 号就绪队列的哨兵地址。"""
    return QUEUE0 + i * QUEUE_STRIDE


def bit_of(i):
    """第 i 号队列在位图里的位号（31 − 队列号）。"""
    return 31 - i


def queue_occupants(m, i):
    """第 i 号队列里的 TCB 列表（按链序，哨兵自指即空）。

    链是 {+0 next / +4 prev} 的双向链表，哨兵自己也在链上，所以判空看
    `next == 自己`。带步数上限，防止链坏了死循环。
    """
    q = queue_addr(i)
    out = []
    cur = m.bus.rd(q, 4)
    for _ in range(QUEUE_MAX):
        if cur == q:
            break
        out.append(cur)
        nxt = m.bus.rd(cur, 4)
        if nxt == cur or nxt == 0:
            break
        cur = nxt
    return out


def occupancy(m):
    """{TCB: [队列号, …]} —— 扫全部 32 条队列。"""
    out = {}
    for i in range(QUEUE_MAX):
        for tcb in queue_occupants(m, i):
            out.setdefault(tcb, []).append(i)
    return out


def prio_of(m, tcb):
    """Tcb 的优先数（TCB +0x0C 的高 16 位）。"""
    return (m.bus.rd(tcb + TCB_PRIO, 4) >> 16) & 0xFF


def state_of(m, tcb):
    """状态字节：0 就绪 / 4 等待 / 0x10 休眠。"""
    return m.bus.rd(tcb + TCB_STATE, 1)


def expected_queue(m, tcb):
    """按「队列号 = 优先数 − 1」算它该在哪条队列。"""
    return prio_of(m, tcb) - 1


def task_index(m):
    """{任务号: TCB}，含空闲任务（表后第 29 条）。"""
    from . import brealos
    return {r["id"]: r["tcb"] for r in brealos.records(m, include_idle=True)}


def tcb_name(m, tcb, index=None):
    """把 TCB 翻译成「任务 N」/「空闲」。"""
    index = index or task_index(m)
    for tid, t in index.items():
        if t == tcb:
            return "空闲" if tid == 29 else "任务%d" % tid
    return "0x%08X" % tcb


def flag_waiters(m, flgid):
    """某个事件标志的等待者 TCB（flag 对象 +4 的链表头，空则等于对象自己）。

    flag 对象在 `0x680008B8 + (flgid − 1) * 0x10`（V24 实测）。
    """
    o = 0x680008B8 + (flgid - 1) * 0x10
    head = m.bus.rd(o + 4, 4)
    return [] if head in (o, o + 4, 0) else [head]


def map_consistent(m):
    """核对「队列号 = 优先数 − 1」+「位 = 31 − 队列号」，返回 (ok, bad, rows)。

    rows 每项：{tcb, name, prio, queue, expected, bit, bit_set}
    """
    index = task_index(m)
    bm = ready_map(m)
    ok = bad = 0
    rows = []
    for tcb, qs in sorted(occupancy(m).items()):
        prio = prio_of(m, tcb)
        exp = prio - 1
        for q in qs:
            bit = bit_of(q)
            set_ = (bm >> bit) & 1 if bit >= 0 else 0
            row = {"tcb": tcb, "name": tcb_name(m, tcb, index), "prio": prio,
                   "queue": q, "expected": exp, "bit": bit, "bit_set": bool(set_)}
            rows.append(row)
            if q == exp:
                ok += 1
            else:
                bad += 1
    return ok, bad, rows


def describe(m):
    """一句话概括当前就绪结构。"""
    bm = ready_map(m)
    cur = current(m)
    occ = occupancy(m)
    busy = sorted({q for qs in occ.values() for q in qs})
    return ("当前 %s（TCB 0x%08X，状态 0x%02X）｜位图 0x%08X（%d 条队列有任务：%s）"
            "｜在队列里的任务 %d 条"
            % (tcb_name(m, cur), cur, state_of(m, cur), bm, len(busy),
               ",".join(str(q) for q in busy) or "无", len(occ)))


# ------------------------------------------------------------------- 调度
def dispatch(m, max_steps=20000, chunk=500):
    """挂一次 vec 0x3F 并跑到停下，返回 {before, after, switched, steps}。

    `before/after` 是当前 TCB。判「真的换任务」用 TCB 是否变了 —— 派发器发现
    没有更高优先级的就绪任务时会原地返回，那时 TCB 不变。
    """
    before = current(m)
    m.raise_irq(DISPATCH_VEC)
    n = 0
    while n < max_steps:
        m.run(max_steps=chunk, spin_guard=False)
        n += chunk
        if current(m) != before:
            break
    after = current(m)
    return {"before": before, "after": after, "switched": after != before, "steps": n}


def run_until(m, entry=DISPLAY_INIT, chunk=2000, max_rounds=400, log=None):
    """跑到 `entry` 被执行；CPU 闲着（空闲任务）或当前任务阻塞时补一次派发请求。

    ⚠️ **这不是固件的原始触发链。** 真机上任务阻塞时内核自己写 0x44 请求派发，
        硬件应答后进 vec 0x3F；这台机器默认没建模 0x44（dispatch=False），请求
        落进影子存储。所以这里在两种情况下代为挂一次中断：

          * 当前任务是空闲任务（`TCB == 任务 29`）—— CPU 真的没事干；
          * 当前任务状态字节 ≠ 0（4 等待 / 0x10 休眠）—— 内核已经把它挂出去了。

        补的是**硬件应答**，不是替内核做决定：选谁仍然完全由固件的派发器决定。

    返回 [(步数, 事件, TCB, 状态字节)]，事件为 "switch" 或 "dispatch"。
    """
    idle = task_index(m).get(29)
    trail = []
    prev = current(m)
    for _ in range(max_rounds):
        m.run(max_steps=chunk, spin_guard=False)
        tcb = current(m)
        st = state_of(m, tcb)
        if tcb != prev:
            trail.append((m.step_count, "switch", tcb, st))
            prev = tcb
        if m.cov is not None and m.cov.visited(entry):
            break
        if st != 0 or (idle is not None and tcb == idle):
            m.raise_irq(DISPATCH_VEC)
            trail.append((m.step_count, "dispatch", tcb, st))
    if log is not None:
        log.extend(trail)
    return trail


def audit(m):
    """体检：位置、位图一致性、任务 5 在不在队列里。返回 [(项目, 结论)]。"""
    out = []
    reg = m.bus.region_at(A_READY_MAP, 4)
    out.append(("0x45984 在 B 段的归属", "%s（%s）" % (reg.kind, reg.name)))
    ok, bad, _rows = map_consistent(m)
    out.append(("队列号 = 优先数 − 1", "%d 对 / %d 不对" % (ok, bad)))
    bm = ready_map(m)
    idx = task_index(m)
    t5 = idx.get(TASK5_ID)
    occ = occupancy(m).get(t5, []) if t5 else []
    out.append(("任务 5 在队列里", ("队列 " + ",".join(map(str, occ))) if occ
                else "不在（未启动或已派发出去）"))
    out.append(("位图第 28 位（队列 3）", "1" if (bm >> 28) & 1 else "0"))
    out.append(("显示初始化跑过", "是" if (m.cov and m.cov.visited(DISPLAY_INIT)) else "否"))
    out.append(("任务 5 入口跑过", "是" if (m.cov and m.cov.visited(TASK5_ENTRY)) else "否"))
    return out


def dump(m):
    """整块结构的多行文本：头部 + 每条非空队列。"""
    idx = task_index(m)
    lines = ["内核区头部 0x%08X：" % KERNEL,
             "  +0x00 当前 TCB     0x%08X（%s）" % (current(m), tcb_name(m, current(m), idx)),
             "  +0x04 就绪位图     0x%08X  = %s" % (ready_map(m), format(ready_map(m), "032b"))]
    for i in range(QUEUE_MAX):
        occ = queue_occupants(m, i)
        if not occ:
            continue
        bit = bit_of(i)
        lines.append("  队列 %2d @0x%08X  位 %2d=%d  %s"
                     % (i, queue_addr(i), bit, (ready_map(m) >> bit) & 1,
                        " → ".join(tcb_name(m, t, idx) for t in occ)))
    return "\n".join(lines)
