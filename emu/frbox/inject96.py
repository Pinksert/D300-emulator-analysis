# -*- coding: utf-8 -*-
"""inject96.py —— V96：把「投一个事件进去，看谁醒来」做成可复现实验台。

## 为什么要有这个模块

V47 起本项目一直用同一个句式描述 flag 6：**「等 flag 6 的 bit5，没人置过」**。
V93 又把 `clr_flg(6, ~TABLE[idx])` 认成「启动铃」。两条线索合起来暗示
flag 6 是**一张按位分给任务的位图**，但一直到 V95 为止，项目里没有任何一次
实验能回答下面这三个问题：

1. 位 → 任务 是不是**一对一**？
2. 一个位进去，**只有**那个任务动，还是别人也被牵连？
3. 有些位静态代码里有等待点、现场却没人等 —— 投进去会怎样？

V96 的做法不是「看着像位图就说它是位图」，而是**先算预测、再注入、再对账**：

    预测（静态 set/wait 点 + 现场 TCB 的 want） → 注入 set_flg(6, bits) → 实测谁上 CPU

预测与实测在 **21 个格**（7 个活位 / 7 个死位 / 4 个复合掩码 / 1 次全位）里
逐个对上，这个结论才成立。任何一格对不上，本模块的价值就是**证伪**，
而不是「再解释一遍」。

## 现场怎么读（不猜字段）

`wait_rows()` 只读四个地方，全部有前版实测出处：

| 读什么 | 位置 | 出处 |
|---|---|---|
| 任务号 ↔ TCB | 任务表 0x54C00，含表后第 29 条空闲 | `brealos.records()` |
| 等待状态字节 | `TCB+0x0B`：0 就绪 / 4 等待 / 0x10 休眠 | `btick.TCB_STATE` |
| 等待原因服务号 | `TCB+0x20` 高半（16 位补码，V94 定型） | `svcnum.of_why()` |
| 等待位模式 | `TCB+0x14` | flag 等待者读的是 waiptn；延时队列上的任务读的是剩余 tick，两者靠队列归属区分 |

「这个 TCB 在等哪个 flag」不靠 `TCB+0x00`（那是链表 next），而是**从 flag
对象反查**：对象 0x680008B8 + (id−1)·0x10 的 `+0x00` 是环形等待链的头，
链尾指回对象自身。这和 `btick.object_queues()` 是同一套走法。

## 怎么判「谁醒了」

不看 PC、不看反汇编、不看位模式 —— **看 `0x68000000`（当前 TCB 槽）被写成谁**。
那是派发序列唯一会写的地方（`blive.Watch` 同一口径）。注入前挂一个写钩子，
注入后跑 `TAIL_STEPS` 步，钩子记到的任务号就是「这次注入真的让它上过 CPU」的清单。

这比「注入后谁的 state 变了」硬：醒来的任务可能跑完又原地等回去，
state 读数会假装它从没动过；而「上过 CPU」是一次**不可撤销**的事实。

**空闲任务（29）不算**：被唤醒的任务跑完必然又等回去，派发随即落回空闲，
那是收敛态的常态，不是「被这个位唤醒」的证据。所以 `got` 一律滤掉 29。

## 边界（写报告必须带上）

* 注入是**替固件调内核**（`bflag.set`），不是真机上有谁置了这一位；
* 「上过 CPU」不等于「处理完了业务」：本模块只证明调度层面被唤醒；
* 死位（静态有等待点、现场无人等）**只说明当前启动路径没走到那段等待代码**，
  不说明真机不置这一位，也不说明该位永远为空；
* 每次 `trial()` 都是**一台 fresh 机器**（boot 191,520 步 + 跑 600,000 步），
  互不共享状态；这也意味着它慢（约 25 秒/格），不适合放进自检的常跑段。
"""

from . import bflag, bready, brealos, btick, blive, svcnum

#: 收敛态：boot 到空闲任务之后再跑的步数（与 V84/V95 同口径）
BOOT_STEPS = blive.BOOT_STEPS
CONVERGE_STEPS = 600000
#: 注入之后再跑多少步（默认格）；足够让被唤醒的任务走完「取件→处理→再等」一圈
TAIL_STEPS = 60000
#: 空闲任务 `bra -1`。末 PC 落在这里 = 系统回到收敛态，没有卡死
IDLE_PC = brealos.IDLE_PC

#: 主角：flag 6（V47 的分配器 bit5 就在这张位图上）
MAILBOX_FLAG = 6
#: flag 对象区（V24 / KI#124）：flag N = BASE + (N−1)·0x10
FLAG_BASE = 0x680008B8
FLAG_STRIDE = 0x10
FLAG_COUNT = 17

#: TCB 字段
TCB_NEXT = 0x00
TCB_STATE = 0x0B
TCB_WANT = 0x14
TCB_WHY = 0x20
#: 状态字节里的「等待」值
ST_WAIT = 0x04

#: 当前 TCB 槽：派发序列唯一的写点（`blive.CURRENT_TCB` 同一个地址）
CURRENT_TCB = blive.CURRENT_TCB

MAX_CHAIN = 64

#: 空闲任务的任务号（任务表后第 29 条）。它被派发不算「谁醒了」。
IDLE_TID = 29

#: 服务号 → 名字。`bhandler.SVC_NAMES` 是 V75 定下的那份，这里补它没有的。
_SVC_EXTRA = {-37: "tslp_tsk", -38: "slp_tsk", -85: "dly_tsk",
              -170: "twai_flg", -171: "svc_171"}


def svc_name(num):
    """服务号 → 名字；未知给 `svc_<n>`。"""
    if num is None:
        return "—"
    from .bhandler import SVC_NAMES
    return SVC_NAMES.get(num) or _SVC_EXTRA.get(num) or "svc_%d" % num


# ---------------------------------------------------------------- 位运算
def bits_of(mask):
    """32 位掩码 → 升序的单一位列表。`0 → []`。"""
    out = []
    v = mask & 0xFFFFFFFF
    b = 1
    while b <= 0x80000000:
        if v & b:
            out.append(b)
        b <<= 1
    return out


# ---------------------------------------------------------------- 建机器
def converged(steps=CONVERGE_STEPS, tick=None):
    """一台跑到收敛态（末 PC = 空闲任务 `bra -1`）的 d300-b。

    `tick=None` 与 V40 以来所有结论同口径（不挂节拍源）。传 `tick=256`
    会得到 V92 那台「延时队列能走」的机器，本版不用它 —— V93 已证
    时基收益不覆盖这 11 个休眠任务，掺进来只会让对照变糊。

    顺带在跑的过程中数**任务换手次数**（`0x68000000` 写值与上一次不同），
    存在返回机器的 `m.swaps` 上。这是纯观测钩子，不影响机器行为。
    """
    m = blive.boot(tick=tick)
    swaps = [0]
    cur = [m.bus.rd(CURRENT_TCB, 4)]
    orig = m.bus.wr

    def wr(a, n, v, pc=0):
        if a == CURRENT_TCB and v != cur[0]:
            cur[0] = v
            swaps[0] += 1
        return orig(a, n, v, pc)

    m.bus.wr = wr
    try:
        done = 0
        while done < steps and not m.cpu.halted:
            k = min(20000, steps - done)
            m.run(max_steps=k, spin_guard=False)
            done += k
    finally:
        m.bus.wr = orig
    m.swaps = swaps[0]
    return m


# ---------------------------------------------------------------- 现场
def flag_queues(m):
    """`{flgid: [tcb, ...]}` —— 从每个 flag 对象的环形等待链读出来。

    对象 `+0x00` 是链头，空时自指；走到指回对象自身（或其 `+0x00`）为止。
    与 `btick.object_queues()` 同一走法，只是这里按 id 建索引并带链长上限。
    """
    out = {}
    for ident in range(1, FLAG_COUNT + 1):
        obj = FLAG_BASE + (ident - 1) * FLAG_STRIDE
        cur = m.bus.rd(obj, 4)
        nodes, seen = [], set()
        for _ in range(MAX_CHAIN):
            if cur in (obj, 0) or cur in seen:
                break
            seen.add(cur)
            nodes.append(cur)
            cur = m.bus.rd(cur + TCB_NEXT, 4)
        out[ident] = nodes
    return out


def wait_rows(m):
    """29 条 TCB 的等待视图（含空闲）。

    每行：`tid / tcb / state / svcnum / svc / flgid / want / delay`

    * `flgid` 由 flag 对象的等待链反查（不在链上 = 0）；
    * `delay` 表示这个 TCB 挂在延时队列上（`btick.delay_queue()` 的成员）。
      同一个任务不可能既在 flag 链上又在延时链上，所以这两个字段互斥。
    """
    index = bready.task_index(m)
    owner = {}
    for ident, nodes in flag_queues(m).items():
        for tcb in nodes:
            owner[tcb] = ident
    delay = {t for t, _tid, _pri, _why in btick.delay_queue(m)}
    rows = []
    for tid, tcb in sorted(index.items(), key=lambda kv: kv[1]):
        state = m.bus.rd(tcb + TCB_STATE, 1) & 0xFF
        why = svcnum.of_why(m.bus.rd(tcb + TCB_WHY, 4) >> 16)
        rows.append({"tid": tid, "tcb": tcb, "state": state,
                     "svcnum": why if state == ST_WAIT else None,
                     "svc": svc_name(why) if state == ST_WAIT else "—",
                     "flgid": owner.get(tcb, 0),
                     "want": m.bus.rd(tcb + TCB_WANT, 4) & 0xFFFFFFFF,
                     "delay": tcb in delay})
    return rows


def waiters(m, rows=None):
    """只在等待（`TCB+0x0B == 4`）的任务行。收敛态是 17 条。"""
    rows = wait_rows(m) if rows is None else rows
    return [r for r in rows if r["state"] == ST_WAIT]


def mailbox_map(rows):
    """`{bit: [tid, ...]}` —— 从等待行反推「每位归谁」。

    `want` 有多位就登记到每一位（本版 flag 6 上每位只对应一个任务，
    但别的 flag 存在多位等待，比如任务 3 等 flag 5 的 0x970F）。
    """
    out = {}
    for r in rows:
        if r["flgid"] != MAILBOX_FLAG or r["state"] != ST_WAIT:
            continue
        for b in bits_of(r["want"]):
            out.setdefault(b, []).append(r["tid"])
    return {b: sorted(v) for b, v in sorted(out.items())}


def snapshot(m, rows=None):
    """收敛态全景：末 PC / 是否回空闲 / 覆盖 / 等待表 / 位图 / 位模式。

    `rows` 不传就现读 —— 参数默认值不能在函数签名里求值（要一台机器）。
    """
    rows = wait_rows(m) if rows is None else rows
    ws = waiters(m, rows)
    return {"end_pc": m.cpu.pc,
            "idle": m.cpu.pc == IDLE_PC,
            "cov": m.cov.words() if m.cov is not None else 0,
            "waiters": len(ws),
            "tasks": [r["tid"] for r in ws],
            "mailbox": mailbox_map(rows),
            "pattern": bflag.pattern(m, MAILBOX_FLAG),
            "queues": {f: [r["tid"] for r in rows if r["flgid"] == f] for f in
                       sorted({r["flgid"] for r in rows if r["flgid"]})}}


# ---------------------------------------------------------------- 静态规格
def spec(sw, flgid=MAILBOX_FLAG):
    """flag 6 的静态「邮箱规格」：谁 set、谁 wait、出现了哪些位。

    返回字典：

        set        {调用点地址: 位掩码}      实参读得出的 set_flg(6)
        set_none   [调用点地址]              实参读不出的 set_flg(6)
        wait       {调用点地址: 等待位}      实参读得出的 wai/twai_flg(6)
        wait_none  [调用点地址]
        set_masks  {掩码: 出现次数}
        wait_masks {等待位: 出现次数}
        bits       [出现过的单一位]（set 与 wait 的并集，升序）

    ⚠️ `set` 只收 `set_flg`（−48）。`clr_flg(6, ~bit)` 那批（V93 的「启动铃清除」）
    不在里面 —— 它们是清，不是投递。
    """
    out = {"set": {}, "set_none": [], "wait": {}, "wait_none": [],
           "set_masks": {}, "wait_masks": {}, "bits": []}
    for c in sw.flag_sets(flgid):
        if isinstance(c.bits, int):
            out["set"][c.addr] = c.bits & 0xFFFFFFFF
            mk = c.bits & 0xFFFFFFFF
            out["set_masks"][mk] = out["set_masks"].get(mk, 0) + 1
        else:
            out["set_none"].append(c.addr)
    for c in sw.flag_waits(flgid, twai=True):
        if isinstance(c.bits, int):
            out["wait"][c.addr] = c.bits & 0xFFFFFFFF
            mk = c.bits & 0xFFFFFFFF
            out["wait_masks"][mk] = out["wait_masks"].get(mk, 0) + 1
        else:
            out["wait_none"].append(c.addr)
    bits = set()
    for mk in list(out["set_masks"]) + list(out["wait_masks"]):
        bits.update(bits_of(mk))
    out["bits"] = sorted(bits)
    return out


def per_bit(sp):
    """`{bit: {'set': [pc], 'wait': [pc]}}` —— 静态点按位归并。"""
    out = {b: {"set": [], "wait": []} for b in sp["bits"]}
    for pc, mk in sp["set"].items():
        for b in bits_of(mk):
            out[b]["set"].append(pc)
    for pc, mk in sp["wait"].items():
        for b in bits_of(mk):
            out[b]["wait"].append(pc)
    for b in out:
        out[b]["set"].sort()
        out[b]["wait"].sort()
    return out


def dead_bits(sp, live):
    """静态出现过、但收敛态没有任何等待者的位（升序）。"""
    return [b for b in sp["bits"] if b not in live]


# ---------------------------------------------------------------- 注入
def trial(bits, steps=None, m=None):
    """投一次 `set_flg(6, bits)`，看谁真的上了 CPU。

    返回字典：

        inject     注入回执（字符串：内核返回码与步数）
        pred       预测唤醒的任务号列表（现场 want 与 bits 有交集）
        got        实测被派发过的任务号列表（0x68000000 的写值反查，**不含空闲**）
        ok         pred == got
        new_cov    这次注入带来的新指令地址数
        pattern    注入并跑完之后 flag 6 的位模式（残留位 = 没人消费）
        end_pc     末 PC
        idle       末 PC 是否回了空闲任务
        before     注入前的位模式

    `m` 不传就现造一台 fresh 收敛机（约 25 秒）。
    """
    if m is None:
        m = converged()
    steps = TAIL_STEPS if steps is None else steps

    rows = wait_rows(m)
    live = mailbox_map(rows)
    pred = sorted({t for b in bits_of(bits) for t in live.get(b, ())})
    before = bflag.pattern(m, MAILBOX_FLAG)
    before_cov = m.cov.words() if m.cov is not None else 0

    index = bready.task_index(m)
    rev = {tcb: tid for tid, tcb in index.items()}
    seen = []

    orig = m.bus.wr

    def wr(a, n, v, pc=0):
        if a == CURRENT_TCB:
            tid = rev.get(v)
            if tid is not None and tid != IDLE_TID and tid not in seen:
                seen.append(tid)
        return orig(a, n, v, pc)

    m.bus.wr = wr
    try:
        r = bflag.set(m, MAILBOX_FLAG, bits)
        done = 0
        while done < steps and not m.cpu.halted:
            k = min(20000, steps - done)
            m.run(max_steps=k, spin_guard=False)
            done += k
    finally:
        m.bus.wr = orig

    after_cov = m.cov.words() if m.cov is not None else 0
    got = sorted(seen)
    return {"inject": "err=%d steps=%d" % (r["err"], r["steps"]),
            "pred": pred, "got": got, "ok": pred == got,
            "new_cov": after_cov - before_cov,
            "pattern": bflag.pattern(m, MAILBOX_FLAG),
            "end_pc": m.cpu.pc, "idle": m.cpu.pc == IDLE_PC,
            "before": before, "bits": bits & 0xFFFFFFFF}
