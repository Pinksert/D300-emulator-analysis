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

三、V52 更正：老 `expire_all` 的两个口径错误

反汇编 `0x040A44`（内核自己的 tick 处理，不是中断向量入口）能看到固件怎么做：

    0x040A48  ldi:32 #0x68000110, r14   ; 64 位 tick 计数器
    0x040A52..0x040A5C                  ; 计数器 +1
    0x040A60  ldi:32 #0x68000118, r7    ; 延时队列哨兵
    0x040A78  ld    #20, r6             ; r6 = TCB+0x14（剩余 tick）
    0x040A7A  add2  #-1, r6             ; 减 1
    0x040A7C  bp:d  *80                 ; 仍 > 0 → 下一个节点，不唤醒
    0x040A7E  st    #20, r6             ; 写回（延迟槽，总执行）
    0x040A80..0x040A86                  ; 到期才摘链
    0x040A88  ld    #8, r6
    0x040A8A  ldi:32 #0xFFFCFFFB, r3
    0x040A90  and   r6, r3              ; 清 TCB+0x08 的等待位
    0x040A94  st    #8, r3
    0x040A96..0x040AAC                  ; 写 0x44 请求派发 + call 0x4185C（挂就绪队列）

对照 V50 写的 `expire_all()`：它把**延时队列里所有任务一次全唤醒**，既没看
`TCB+0x14`，也没清 `TCB+0x08`。基线实测三个任务的剩余 tick 是
`17:0 / 7:9 / 21:0` —— `tslp_tsk(10)` 的任务 7 要 8~9 次 tick 才到期，
`tick(1)` 却在第一轮就把它拽醒了，**提前了 8 个 tick**。

所以：

- `expire_all()` / `tick()` **保持原样**（V50 的 `btick.txt`、V51 的 `struct.txt`
  都是这个口径的产物，改了就复现不出来），默认仍是宽松口径；
- 新分析用 `expire_strict()` / `tick_v52()`，按 `TCB+0x14` 递减、只唤醒到 0 的；
- `0x68000968` 不是「宽松补全造出来的假象」：严格到期下任务 7 第二次
  睡眠仍挂到它。它的真身是 **事件标志（flag）ID 12 的对象控制块基址** ——
  ROM 表 `0x551B0` 是 V33（KI#124）解出的 **flag 定义表**（17 条，止于
  `0x552C4`），对象区 `0x680008B8 + (id-1)*0x10`，等待队列内嵌在对象
  `+0x00/+0x04`。ID 12 → 表项 `0x55260`、对象 `0x68000968`。
  挂链点 `0x041ED2`/`0x041ED6`。见 `object_queues()` / `expire_objects()`。

⚠️ `tick_v52` 同样是反事实补全。它只是比 V50 更贴固件语义，不等于固件真的
收到了 tick；`wake_objects=True` 尤其是凭空把 flag 等待队列上的任务拽醒。
"""

from . import bready

SENTINEL = 0x68000118          # 延时队列哨兵（双向链表，节点就是 TCB 自己）
TCB_NEXT = 0x00                # TCB +0：next
TCB_PREV = 0x04                # TCB +4：prev
TCB_STATE = 0x0B               # 状态字节：0 就绪 / 4 等待 / 0x10 休眠
TCB_PRIO = 0x0C                # 高 16 位 = 优先数
TCB_WHY = 0x20                 # 高 16 位 = 让任务进入等待的服务号（有符号）
TCB_TICKS = 0x14               # V52：剩余 tick 数（固件 0x040A78 的 `ld #20`）
TCB_FLAGS = 0x08               # V52：状态字，到期时 &= FLAG_WAIT_CLEAR
FLAG_WAIT_CLEAR = 0xFFFCFFFB   # V52：0x040A8A 那个常量
MAX_CHAIN = 64                 # 链上最多走多少个节点（防坏链死循环）
CHUNK = 30000                  # 一次 tick 之后跑多少步，让任务真的动起来

#: V52：ROM 里的**事件标志（flag）定义表**，V33 的 KI#124 已经解出：
#:   sem 表 0x54F2C（40 项）→ 止于 0x551B0；flag 表头 0x551B0 = 0x00110010（17|16），
#:   17 条记录从 0x551B4 起，止于 0x552C4。
#: 每项 16 字节：+0x00 保留 / +0x04 **对象控制块指针** / +0x08 高半 = flag ID / +0x0C 保留。
#: 对象区（V33 实测）`0x680008B8 + (id-1)*0x10`，共 17 个；**等待队列内嵌在对象
#: +0x00(next) / +0x04(prev)**，空时自指；+0x08 是位模式。
#: 所以 ID 12 → 表项 0x55260、对象 0x68000968 —— 就是 V51 说的「另一条链」。
#: ⚠️ 这张表不是 V52 发现的，V52 只是把 V51 那条链对回 V33 已解出的结构。
OBJ_TABLE = 0x551B0
OBJ_STRIDE = 0x10
OBJ_ID_FIRST = 1
OBJ_ID_LAST = 17             # V33：flag 表头 0x00110010 = 17 条记录


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


# ---------------------------------------------------------------- V52 对象等待队列
def object_entry_addr(ident):
    """flag ID → ROM 的 flag 定义表项地址（纯函数，不建机器）。"""
    return OBJ_TABLE + OBJ_STRIDE * (ident - OBJ_ID_FIRST)


def object_queues(m):
    """扫 ROM 的 flag 定义表，列出每个 flag 对象及其等待队列上的任务。

    返回 `[(ident, obj, [tcb, ...])]`；`obj` 是**对象控制块基址**（表项 +4），
    等待队列内嵌在它的 `+0x00/+0x04`，空时自指。只收「+4 落在内核区
    0x68000000..0x68010000」的项，表头/保留项跳过。

    表和对象区都是 V33 的 KI#124 解出的，这里只是运行时读回。实测
    ID 1..17 → 对象 0x680008B8..0x680009B8，其中 ID 12 → 0x68000968。
    """
    out = []
    for ident in range(OBJ_ID_FIRST, OBJ_ID_LAST + 1):
        a = object_entry_addr(ident)
        obj = m.bus.rd(a + 4, 4)
        if not (0x68000000 <= obj < 0x68010000):
            continue
        ident_rom = (m.bus.rd(a + 8, 4) >> 16) & 0xFFFF
        nodes = []
        cur = m.bus.rd(obj, 4)
        seen = set()
        for _ in range(MAX_CHAIN):
            if cur == obj or cur == 0 or cur in seen:
                break
            seen.add(cur)
            nodes.append(cur)
            cur = m.bus.rd(cur + TCB_NEXT, 4)
        out.append((ident_rom or ident, obj, nodes))
    return out


def object_pending(m):
    """所有非空 flag 等待队列上挂着的 TCB 数。"""
    return sum(len(nodes) for _i, _q, nodes in object_queues(m))


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
    """（V50 口径，**有缺陷**）让延时队列里的每个任务无条件「到期」。

    ⚠️ 这个口径忽略了 `TCB+0x14` 的剩余 tick 数，也没清 `TCB+0x08` 的等待位。
    V50 的 `btick.txt`、V51 的 `struct.txt` 都是它的产物，为可复现而保留。
    新分析请用 `expire_strict()`。
    """
    out = []
    for tcb, tid, prio, _why in delay_queue(m):
        _unlink(m, tcb)
        m.bus.wr(tcb + TCB_STATE, 1, 0, 0)
        _push_ready(m, tcb, prio)
        out.append((tcb, tid))
    return out


def _wake_one(m, tcb, prio):
    """按 `0x040A80..0x040AAE` 的顺序唤醒一个任务。

    摘链 → 清 `TCB+0x08` 等待位 → 清状态字节 → 挂就绪队列。
    `0x4185C` 那个函数还写了 0x44 请求派发，这里由调用方 `bready.dispatch()`
    统一补，避免每个任务都请求一次。
    """
    _unlink(m, tcb)
    m.bus.wr(tcb + TCB_FLAGS, 4,
             m.bus.rd(tcb + TCB_FLAGS, 4) & FLAG_WAIT_CLEAR, 0)
    m.bus.wr(tcb + TCB_STATE, 1, 0, 0)
    return _push_ready(m, tcb, prio)


def expire_strict(m):
    """（V52）按固件语义走一次 tick：递减剩余 tick，只唤醒到 0 的任务。

    返回 `[(tcb, 任务号)]`。每个节点的 `TCB+0x14` 都会减 1（延迟槽总执行），
    减完 `<= 0`（有符号）才摘链唤醒。`0x040A7C bp:d` 在结果为 0 时也不跳，
    所以 0 算到期。
    """
    out = []
    for tcb, tid, prio, _why in delay_queue(m):
        v = (m.bus.rd(tcb + TCB_TICKS, 4) - 1) & 0xFFFFFFFF
        m.bus.wr(tcb + TCB_TICKS, 4, v, 0)
        if 0 < v < 0x80000000:          # 还有剩余 tick，本轮不动它
            continue
        _wake_one(m, tcb, prio)
        out.append((tcb, tid))
    return out


def expire_object_queue(m, obj):
    """（V52）把某个 flag 对象等待队列上的任务全部摘下来挂就绪。

    ⚠️ 这是比 `tick_v52` 更强的反事实：固件里这些任务要等 flag 被置位 /
    超时才会醒，这里直接替它决定「现在醒」。只在探边界时用。
    """
    out = []
    cur = m.bus.rd(obj, 4)
    seen = set()
    for _ in range(MAX_CHAIN):
        if cur == obj or cur == 0 or cur in seen:
            break
        seen.add(cur)
        nxt = m.bus.rd(cur + TCB_NEXT, 4)
        prio = (m.bus.rd(cur + TCB_PRIO, 4) >> 16) & 0xFF
        _wake_one(m, cur, prio)
        out.append(cur)
        cur = nxt
    return out


def expire_objects(m):
    """（V52）所有非空对象等待队列，返回被唤醒的 TCB 列表。"""
    out = []
    for _ident, q, nodes in object_queues(m):
        if nodes:
            out.extend(expire_object_queue(m, q))
    return out


def tick_v52(m, n=1, steps=CHUNK, log=None, wake_objects=True, only=None):
    """（V52）推荐入口：严格到期 + 可选补 flag 等待队列。

    `only=<flag 对象基址>` 时只补那一条队列；不传则补所有非空 flag 队列。

    ⚠️ **补全范围要跟问题匹配。** 基线里各 flag 队列上一共挂着 14 个任务，
    把它们全拽醒会把整机推到另一条路上（实测连 `0x17541A` 都到不了，
    最终 PC 也不是空闲地址），反而比只补 flag 12 更差。见 `tick2.txt` F 段。
    分析具体问题时优先用 `only=`。

    与老 `tick()` 的三点不同：

    1. `expire_strict()` 按 `TCB+0x14` 递减，不再一次全唤醒；
    2. 唤醒时清 `TCB+0x08` 等待位；
    3. `wake_objects=True` 时把 flag 对象等待队列上的任务也拽醒。

    `spent` 的判据也改了：看「两条队列是不是都空」，而不是「这轮有没有
    唤醒任务」—— 严格口径下连续几轮都在递减计数、一个都没醒是正常的。

    ⚠️ 仍是反事实补全，不是固件行为。
    """
    out = []
    for k in range(1, n + 1):
        got = expire_strict(m)
        if not wake_objects:
            woke = []
        elif only is not None:
            woke = expire_object_queue(m, only)
        else:
            woke = expire_objects(m)
        if not delay_queue(m) and (not wake_objects or not object_pending(m)):
            out.append({"tick": k, "expired": [], "objects": [],
                        "spent": False, "pc": m.cpu.pc,
                        "cur": m.bus.rd(bready.CUR_TCB, 4), "steps": 0})
            break
        if got or woke:
            bready.dispatch(m)
        m.run(max_steps=steps, spin_guard=False)
        rec = {"tick": k,
               "expired": [t for _tcb, t in got],
               "objects": ["0x%08X" % t for t in woke],
               "spent": True, "pc": m.cpu.pc,
               "cur": m.bus.rd(bready.CUR_TCB, 4),
               "steps": m.step_count}
        out.append(rec)
        if log is not None:
            log.append(rec)
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
    q = delay_queue(m)
    return {
        "timer_csr": m.timer.csr,
        "timer_underflows": m.timer.underflows,
        "irq_taken": len(m.cpu.irq_log),
        "delay_len": len(q),
        "delay": q,
        "delay_ticks": [(t, m.bus.rd(c + TCB_TICKS, 4)) for c, t, _p, _w in q],
        "object_pending": object_pending(m),
        "object_queues": [(i, "0x%08X" % a, len(n)) for i, a, n in object_queues(m) if n],
        "ready_map": bready.ready_map(m),
        "ready_tasks": len(occ),
        "cur": m.bus.rd(bready.CUR_TCB, 4),
        "pc": m.cpu.pc,
        "steps": m.step_count,
    }


def describe(m):
    """一行摘要，方便探针直接打印。

    ⚠️ 格式锁死：V50 的 `results/frbox/btick.txt` 逐字引用这一行，
    加字段会让历史结果复现不出来。V52 的新字段请走 `describe_v52()`。
    """
    d = diagnose(m)
    return ("step=%d pc=0x%06X 定时器 csr=0x%04X 下溢=%d 中断=%d 延时队列=%d 就绪=%d"
            % (d["steps"], d["pc"], d["timer_csr"], d["timer_underflows"],
               d["irq_taken"], d["delay_len"], d["ready_tasks"]))


def describe_v52(m):
    """（V52）带剩余 tick 与对象队列的摘要。"""
    d = diagnose(m)
    return ("step=%d pc=0x%06X 延时队列=%d 剩余tick=%s 对象队列等待=%d 就绪=%d"
            % (d["steps"], d["pc"], d["delay_len"], d["delay_ticks"],
               d["object_pending"], d["ready_tasks"]))
