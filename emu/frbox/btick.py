# -*- coding: utf-8 -*-
"""btick.py —— 补上缺失的 tick 源，以及「整机为什么会停」（V50）。

一、这台机器上没有 tick

REALOS 的 `sys_dly_tsk(n)` 要求「n 个 tick 之后把任务放回就绪队列」。实测：

  - 重载定时器（直接页 0x10–0x17）的 `csr` 从 boot 到 99 万步一直是 0，
    `underflows = 0` —— 固件从没使能过它；
  - `irq_taken` 涨到 230 次，但 `irq_log` 里全是 `(63, …)` 和 `(64, …)`，
    即 vec 0x3F（`0x44` 请求锁存器，V45）与 `int #0x40`，没有一条是周期中断；
  - 延时队列哨兵 `0x68000118` 在 99 万步里只被碰 10 次，**全是入队，没有一次出队**。

于是 `dly_tsk` 的任务永久挂起：整机最后「就绪位图 = 0、只剩空闲任务自旋」。

二、这个模块做什么

把「内核收到一次 tick 时该做的事」手工做一遍：

    1. 沿延时队列哨兵把每个 TCB 摘出来
    2. 状态字节 TCB+0x0B 清 0（等待 → 就绪）
    3. 按「队列号 = 优先数 − 1」挂进对应就绪队列（头插）
    4. 置就绪位图第 `31 − 队列号` 位
    5. 请求派发（vec 0x3F）

⚠️ **这是反事实补全，不是固件行为。** 真实的 tick 源没有定位到（见 KI#189），
所以 `tick()` 只能证明「差这一口」，不能证明「固件就是这么跑的」。
⚠️ 摘链只按哨兵链走一遍；如果延时队列本身坏了（例如节点不在链上），这里看不出来。
"""

from . import bready

SENTINEL = 0x68000118          # 延时队列哨兵（双向链表，节点就是 TCB 自己）
TCB_NEXT = 0x00                # TCB +0：next
TCB_PREV = 0x04                # TCB +4：prev
TCB_STATE = 0x0B               # 状态字节：0 就绪 / 4 等待 / 0x10 休眠
TCB_PRIO = 0x0C                # 高 16 位 = 优先数
TCB_WHY = 0x20                 # 高 16 位 = 让任务进入等待的服务号（有符号）
MAX_CHAIN = 64                 # 链上最多走多少个节点（防坏链死循环）
CHUNK = 30000                  # 一次 tick 之后跑多少步，让任务真的动起来


def sx8(v):
    """低 8 位补码 → 服务号。

    服务号在 R12 的低 8 位（`ldi:8` + `extsb` + `int #0x40`）。内核把它存进
    TCB+0x20 时做了**符号扩展**：`0xAB` 存成 `0xFFAB`。所以这个字段要
    先取低 8 位再补码 —— 按 16 位补码算会把 `0x00E9`（`sta_tsk`）读成 233。
    """
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def delay_queue(m):
    """延时队列里的任务：[(tcb, 任务号, 优先数, 等待原因服务号)]。

    沿 `SENTINEL` 的 next 链走，哨兵自指即空。任务号靠 `bready.task_index`
    把 TCB 翻回来；翻不到给 None。
    """
    index = bready.task_index(m)
    rev = {t: k for k, t in index.items()}
    out = []
    cur = m.bus.rd(SENTINEL, 4)
    seen = set()
    for _ in range(MAX_CHAIN):
        if cur == SENTINEL or cur == 0 or cur in seen:
            break
        seen.add(cur)
        out.append((cur,
                    rev.get(cur),
                    (m.bus.rd(cur + TCB_PRIO, 4) >> 16) & 0xFF,
                    sx8(m.bus.rd(cur + TCB_WHY, 4) >> 16)))
        cur = m.bus.rd(cur + TCB_NEXT, 4)
    return out


def _unlink(m, tcb):
    """从双向链里摘掉一个节点。"""
    nxt = m.bus.rd(tcb + TCB_NEXT, 4)
    prv = m.bus.rd(tcb + TCB_PREV, 4)
    if prv:
        m.bus.wr(prv + TCB_NEXT, 4, nxt, 0)
    if nxt:
        m.bus.wr(nxt + TCB_PREV, 4, prv, 0)


def _push_ready(m, tcb, prio):
    """挂进「队列号 = 优先数 − 1」的队头，并置位图。"""
    i = prio - 1
    if i < 0:
        return False
    q = bready.queue_addr(i)
    old = m.bus.rd(q, 4)
    if old in (0, q):          # 空队列：哨兵自指，或还没初始化（读回 0）
        old = q
    m.bus.wr(tcb + TCB_NEXT, 4, old, 0)
    m.bus.wr(tcb + TCB_PREV, 4, q, 0)
    m.bus.wr(q, 4, tcb, 0)
    m.bus.wr(old + TCB_PREV, 4, tcb, 0)
    m.bus.wr(bready.READY_MAP, 4,
             m.bus.rd(bready.READY_MAP, 4) | (1 << bready.bit_of(i)), 0)
    return True


def expire_all(m):
    """让延时队列里的每个任务「到期」。返回 [(tcb, 任务号)]。"""
    out = []
    for tcb, tid, prio, _why in delay_queue(m):
        _unlink(m, tcb)
        m.bus.wr(tcb + TCB_STATE, 1, 0, 0)
        _push_ready(m, tcb, prio)
        out.append((tcb, tid))
    return out


def tick(m, n=1, steps=CHUNK, log=None):
    """给 n 次 tick，每次到期后跑 `steps` 步。

    返回 [{tick, expired, steps, pc, cur, spent}]，`spent=False` 表示这次
    没有任务被唤醒（延时队列空）—— 那之后就不该再给 tick 了。
    """
    out = []
    for k in range(1, n + 1):
        got = expire_all(m)
        if not got:
            out.append({"tick": k, "expired": [], "spent": False,
                        "pc": m.cpu.pc, "cur": m.bus.rd(bready.CUR_TCB, 4),
                        "steps": 0})
            break
        bready.dispatch(m)
        m.run(max_steps=steps, spin_guard=False)
        rec = {"tick": k, "expired": [t for _tcb, t in got], "spent": True,
               "pc": m.cpu.pc, "cur": m.bus.rd(bready.CUR_TCB, 4),
               "steps": m.step_count}
        out.append(rec)
        if log is not None:
            log.append(rec)
    return out


def diagnose(m):
    """这台机器现在的「时基」状态。"""
    occ = bready.occupancy(m)
    return {
        "timer_csr": m.timer.csr,
        "timer_underflows": m.timer.underflows,
        "irq_taken": len(m.cpu.irq_log),
        "delay_len": len(delay_queue(m)),
        "delay": delay_queue(m),
        "ready_map": bready.ready_map(m),
        "ready_tasks": len(occ),
        "cur": m.bus.rd(bready.CUR_TCB, 4),
        "pc": m.cpu.pc,
        "steps": m.step_count,
    }


def describe(m):
    """一行摘要，方便探针直接打印。"""
    d = diagnose(m)
    return ("step=%d pc=0x%06X 定时器 csr=0x%04X 下溢=%d 中断=%d 延时队列=%d 就绪=%d"
            % (d["steps"], d["pc"], d["timer_csr"], d["timer_underflows"],
               d["irq_taken"], d["delay_len"], d["ready_tasks"]))
