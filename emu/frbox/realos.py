# -*- coding: utf-8 -*-
"""原生 REALOS 启动：让固件自己的内核来调度任务。

以前的 frbox 靠 `itron.py` 里的 μITRON 半主机顶替内核 —— 那是在「内核跑不起来」
时候的替身。V10 发现了真正的原因：**CPU 的 `andccr`/`orccr` 没有跟着 S 位换栈**，
于是 REALOS 恢复任务上下文时读的还是旧栈，`reti` 弹出 0，看起来像「固件内核进不去」。
修掉之后，A 段从复位一路跑到 REALOS 自己的调度器，任务上下文、TCB、系统调用
全部由固件完成，不再需要半主机。

实测出来的启动链（A 段 1.11，共 43,4xx 步）：

    0x0FC000  复位：r15 = 0x4616C（引导栈）、ILM = 0x10、TBR = 0xFFC00
      └ 0xF8010 → 0x80044
    0x080044  板级初始化
      └ 0xFC020「调 main」→ 0x804FA
    0x0804FA  ★ 孤立 `ret` —— 应用 main 是个空桩
      └ 0xFC028 → `jmp 0xFE426`
    0x0FE426  REALOS 初始化（7,220 步）
      ├ 清 BSS 块 0x45980..0x461CC（0xFE512 那段，876×5 次倒计数存字节）
      ├ 建任务 1 / 任务 5 / 任务 0（空闲）的初始上下文
      ├ `call 0x804FC`  ← 应用侧初始化：再清 0x3F000..0x4592C，配直接页 0x420–0x435
    0x0804FC  ...
      └ 返回内核 0xFE462，把 `*(0x3FBB0)` 置 2（启动阶段标记）
    0x0FE470  `bra 0xFE4DE`
    0x0FE4DE  ★ 首次派发：r14 = *(0xFE160)（默认是任务 0/空闲）
              → TCB+0x18 取出保存的 SP → 切 USP → 弹寄存器 → reti

任务上下文就压在任务自己的栈顶上，前两个字是 PC / PS：

    0x462EC 任务 1   [0x08133A, 0x001F0030, 0x000FE250, 0]
    0x466B4 任务 5   [0x0A62FE, 0x001F0030, 0x000FE250, 0]
    0x4617C 任务 0   [0x0FE510, 0x001F0030, 0x000FE250, 0]   ← 0xFE510 是 `bra -1`

这三个栈顶与任务表记录里的 `sp` 字段逐字一致，首字与 `entry` 字段逐字一致 ——
也就是说「任务 1 入口 0x8133A、栈顶 0x462EC」不再是推测，是固件自己写进去的。

TCB 布局（0x2C 字节，实测）：

    +0x00 下一个 TCB（空闲时自指）      +0x04 上一个 TCB
    +0x08 0                            +0x0C 属性 << 16
    +0x10 0                            +0x14 0xFFFFFFFF
    +0x18 ★ 保存的 SP                  +0x1C 0xFFFFFFFF
    +0x20 0                            +0x24 ★ 回指自己的任务表记录
    +0x28 0xFFFFFFFF
"""

TASK_TABLE = 0x000FE000      # 头部：*(0xFE000) = 用户任务数（8）
TASK_REC = 0x1C              # 每条记录 0x1C 字节
TASK_REC0 = 0x000FE010       # 第一条记录
TASK_COUNT = 8               # 表头记的用户任务数（记录实际有 13 条）

DISPATCH_LD = 0x000FE4E6     # 刚从 *0xFE160 取到 r14，还没写回当前 TCB 变量
CUR_TCB = 0x0000045980       # 当前任务 TCB
IDLE_REC = TASK_REC0 + TASK_COUNT * TASK_REC     # 0xFE160：任务 0（空闲）的记录
IDLE_ENTRY = 0x000FE510      # `bra -1`
TCB_SP = 0x18
TCB_REC = 0x24
# V12 从死锁现场反读出来的 TCB 字段（0x459FC = 任务 1，0x45AAC = 任务 5）：
#   +0x00 等待对象控制块（不等待时是自己的就绪队列环）
#   +0x08 状态字（0x00100004 = 等标志；0x00010004 = 等消息）
#   +0x14 waiptn（等标志时的位模式，任务 1 = 0x2000；其它 = 0xFFFFFFFF）
#   +0x18 保存的 SP（**任务真正跑过之后，上下文在这里，不在任务表的初始 SP**）
TCB_WAIT = 0x00
TCB_STATE = 0x08
TCB_PAT = 0x14
TCB_NEXT = 0x00

FLAG_CB = 0x00045C38         # flg1 控制块；步长 0x10，共 10 个（登记表 0xFE180 只到 #10）
FLAG_STEP = 0x10
FLAG_COUNT = 10
FLAG_REG = 0x000FE180        # 对象登记表：+0x04 = 控制块指针，+0x08 = 对象号<<16
REG_STEP = 0x10
ISR_SLOT = 0x0003F000        # 回调槽基址（槽地址 = 0x3F01C + (vec-0x10)*4，有例外）


def records(m):
    """任务表全部记录，含 V9 之前没数进去的任务 0/9/10/11/12。

    头部 `*(0xFE000)` 记的 8 是**用户任务数**，不是记录总数；记录实际有 13 条，
    从 0xFE010 起每 0x1C 一条，最后一条（0xFE160）是 id 0 的空闲任务。
    """
    out = []
    n = int.from_bytes(m.fetch_bytes(TASK_TABLE, 4), "big")
    for i in range(TASK_COUNT + 5):
        a = TASK_REC0 + i * TASK_REC
        d = m.fetch_bytes(a, TASK_REC)
        if len(d) < TASK_REC:
            break
        tcb = int.from_bytes(d[0:4], "big")
        ident = int.from_bytes(d[4:8], "big")
        tid, attr = (ident >> 16) & 0xFFFF, ident & 0xFFFF
        state = int.from_bytes(d[0x0C:0x10], "big")
        entry = int.from_bytes(d[0x10:0x14], "big")
        sp = int.from_bytes(d[0x14:0x18], "big")
        if not tcb and not entry:
            break
        out.append({"index": i, "addr": a, "tcb": tcb, "id": tid,
                    "attr": attr, "user": i < n, "state": state,
                    "active": bool(state & 0x80000000),
                    "entry": entry, "sp": sp})
    return out


def find(m, task_id):
    for r in records(m):
        if r["id"] == task_id:
            return r
    return None


def context(m, sp):
    """任务栈顶那两个字：PC / PS。"""
    d = m.fetch_bytes(sp, 8)
    return {"pc": int.from_bytes(d[0:4], "big"),
            "ps": int.from_bytes(d[4:8], "big")}


def prepare(m, task_id=1, max_steps=600000):
    """跑到 REALOS 的首次派发点，把它改派给 `task_id`。

    返回 dict：{'record','steps','switched','context'}。
    必须在 `m.load()` 之后调用；不改任何 ROM，只在派发点上改一个寄存器。
    """
    rec = find(m, task_id)
    if rec is None:
        raise KeyError(f"任务表里没有 id={task_id}")
    m.run(max_steps=max_steps, stop_pcs={DISPATCH_LD}, spin_guard=False)
    steps = m.step_count
    before = int.from_bytes(m.fetch_bytes(CUR_TCB, 4), "big")
    m.cpu.r[14] = rec["tcb"]
    return {"record": rec, "steps": steps, "switched": rec["tcb"] != before,
            "before": before, "context": context(m, rec["sp"])}


def saved_context(m, tcb):
    """任务**当前**的上下文：TCB+0x18 是它被换出时保存的 SP，栈顶两字为 PC / PS。

    与 `context(m, sp)` 的区别很重要：`context` 读的是任务表里的**初始**栈顶，
    任务一旦跑过，那块内存已经不是上下文了 —— V11 的 `summary()` 就是用它，
    于是把运行中的任务报成 `PC=0x3F5D2` 这类非代码地址。V12 改正。
    """
    sp = int.from_bytes(m.fetch_bytes(tcb + TCB_SP, 4), "big") if tcb else 0
    if not sp:
        return {"sp": 0, "pc": 0, "ps": 0}
    # 休眠/退出态（状态字低字节 0x10）的 TCB+0x18 仍指着一个栈，但那上面不是现场：
    # 读出来会得到 0x459FC 这类「刚好是 TCB 地址」的值。任务 1 调 ext_tsk 之后就是这样。
    state = int.from_bytes(m.fetch_bytes(tcb + TCB_STATE, 4), "big")
    if state & 0xFF == 0x10:
        return {"sp": sp, "pc": 0, "ps": 0}
    d = m.fetch_bytes(sp, 8)
    if len(d) < 8:
        return {"sp": sp, "pc": 0, "ps": 0}
    return {"sp": sp, "pc": int.from_bytes(d[0:4], "big"),
            "ps": int.from_bytes(d[4:8], "big")}


def flags(m):
    """10 个事件标志对象：控制块地址 / 当前位模式 / 等待队列头 / 有几个任务在等。"""
    out = []
    for i in range(FLAG_COUNT):
        base = FLAG_CB + i * FLAG_STEP
        d = m.fetch_bytes(base, 0x10)
        if len(d) < 0x10:
            break
        # 队列是「以控制块自己当哨兵」的循环链：空时 +0x00 = 控制块地址，
        # 非空时 +0x00 = 第一个等待者（它的 +0x00 再指回控制块）。
        # ⚠️不要拿 +0x04 当哨兵：+0x04 在非空时也被写成同一个等待者。
        head = int.from_bytes(d[0:4], "big")
        pat = int.from_bytes(d[8:12], "big")
        out.append({"id": i + 1, "cb": base, "pattern": pat, "head": head,
                    "tail": int.from_bytes(d[4:8], "big"),
                    "waiters": 0 if head == base else _walk_queue(m, head, base)})
    return out


def _walk_queue(m, head, selfp, limit=13):
    n, cur = 0, head
    while cur not in (0, selfp) and n < limit:
        n += 1
        d = m.fetch_bytes(cur, 4)
        if len(d) < 4:
            break
        cur = int.from_bytes(d, "big")
    return n


# 等待原因位（V13 用「状态字 ↔ 该任务最后一句系统调用」逐项对照出来的）：
#   0x00100004 ← wai_flg   同时能在某个 flag 的等待队列里找到它
#   0x00400004 ← rcv_msg   不在任何 flag 队列里
#   0x00010004 ← slp_tsk   不在任何 flag 队列里
# 低字节：0=运行/就绪、4=等待、0x10=休眠/未启动
WAIT_FLAG = 0x00100000
WAIT_MSG = 0x00400000
WAIT_SLEEP = 0x00010000

WAIT_KIND = {WAIT_FLAG: "flag", WAIT_MSG: "msg", WAIT_SLEEP: "sleep"}


def wait_kind(state):
    """状态字 → 'flag' / 'msg' / 'sleep' / 'ready' / 'dormant'。"""
    if state & 0xFF == 0x10:
        return "dormant"
    if state & 0xFF != 4:
        return "ready"
    for bit, name in WAIT_KIND.items():
        if state & bit:
            return name
    return "wait"


def in_flag_queue(m):
    """扫描 10 个 flag 的等待队列 -> {tcb: flag_id}。

    ⚠️不能拿 TCB+0x00 当「等待对象」：它是链表的 prev 指针。
    任务在 flag 队列里时它正好等于 flag 控制块；但**休眠的任务**会留着上一次
    所在就绪队列的地址（任务 5 就是 0x45990 = 优先级 1 的就绪槽，V12 把它误当成邮箱）。
    """
    out = {}
    for f in flags(m):
        cur, n = f["head"], 0
        while cur not in (0, f["cb"]) and n < 20:
            out[cur] = f["id"]
            n += 1
            d = m.fetch_bytes(cur, 4)
            if len(d) < 4:
                break
            cur = int.from_bytes(d, "big")
    return out


def wait_of(m, tcb):
    """一个任务在等什么：{'kind','flag','pat','state'}。

    kind='flag' 时 flag 是队列里查到的那个 flag 号；'sleep'/'msg' 没有对象地址。
    """
    if not tcb:
        return {"kind": "dormant", "flag": None, "pat": 0, "state": 0}
    d = m.fetch_bytes(tcb + TCB_WAIT, 0x18)
    if len(d) < 0x18:
        return {"kind": "dormant", "flag": None, "pat": 0, "state": 0}
    state = int.from_bytes(d[TCB_STATE:TCB_STATE + 4], "big")
    pat = int.from_bytes(d[TCB_PAT:TCB_PAT + 4], "big")
    kind = wait_kind(state)
    flg = in_flag_queue(m).get(tcb) if kind == "flag" else None
    return {"kind": kind, "flag": flg, "pat": pat, "state": state}


def states(m):
    """13 条任务的完整状态表（V13）：入口 / 激活 / 在等什么 / 当前 PC。

    `wait_kind` 取 flag / msg / sleep / ready / dormant；
    `wait_flag` 只在 kind=flag 时有值（从队列反查，不看 TCB+0x00）。
    """
    cur = int.from_bytes(m.fetch_bytes(CUR_TCB, 4), "big")
    q = in_flag_queue(m)
    out = []
    for r in records(m):
        d = m.fetch_bytes(r["tcb"] + TCB_WAIT, 0x18) if r["tcb"] else b""
        state = int.from_bytes(d[TCB_STATE:TCB_STATE + 4], "big") if len(d) >= 0x18 else 0
        pat = int.from_bytes(d[TCB_PAT:TCB_PAT + 4], "big") if len(d) >= 0x18 else 0
        kind = wait_kind(state)
        cur_task = r["tcb"] == cur
        if cur_task and m.cpu is not None:
            c = {"sp": m.cpu.r[15], "pc": m.cpu.pc, "ps": m.cpu.ps}
        else:
            c = saved_context(m, r["tcb"])
        out.append({"id": r["id"], "tcb": r["tcb"], "entry": r["entry"],
                    "active": r["active"], "state": r["state"],
                    "current": cur_task, "dormant": kind == "dormant",
                    "sp": c["sp"], "pc": c["pc"],
                    "wait_kind": kind, "wait_flag": q.get(r["tcb"]),
                    "wait_obj": r["tcb"] if False else 0,
                    "wait_pat": pat, "wait_state": state})
    return out


def blockers(m):
    """当前所有在等的任务。flag 的带 flag 号与位模式；sleep/msg 只报种类。"""
    out = []
    for t in states(m):
        if t["wait_kind"] in ("ready", "dormant"):
            continue
        out.append({"task": t["id"], "tcb": t["tcb"], "kind": t["wait_kind"],
                    "flag": t["wait_flag"], "wants": t["wait_pat"]})
    return out


SAFE_PC = 0x00081338        # `bra -1`：只当「系统调用已经返回」的标记


def kernel_call(m, svc, r4=0, r5=0, r6=0, r7=0, r13=0, max_steps=50000):
    """在**固件自己的内核里**执行一次系统调用 —— 相当于替硬件按了一下快门。

    做法不是改内存、也不是半主机：把参数放进 R4–R7/R12，然后让 CPU 执行一次
    `eit(0x40)`，走固件自己的陷阱入口（A 段 0xFE3C0）→ 分发表（0xFE3B8）→ 真实现；
    `reti` 回到标记地址 0x81338（`bra -1`）就算返回。全程只借用 CPU 一次，
    返回后把寄存器原样恢复。

    ⚠️ 三条边界：
      ① 服务号必须是 **32 位补码**（r12 = -48 而不是 0xD0）——分发表按带符号 ×2 取偏移，
         写成 0xD0 会落到表的另一头，实测「调用成功但什么也没发生」；
      ② 这是「注入外部事件」，不是「硬件真的产生了事件」：报告里不能写成相机响应了按键；
      ③ 调用发生在当前任务的上下文里（EIT 用系统栈），不改变被中断任务的栈。

    返回 {'steps', 'err'}；err 来自 R12（μITRON 错误码，0 = 成功）。
    """
    c = m.cpu
    saved = (list(c.r), list(c.dr), c.ps, c.pc, c.delay, c.halted)
    c.r[4], c.r[5], c.r[6], c.r[7], c.r[13] = r4, r5, r6, r7, r13
    c.r[12] = svc & 0xFFFFFFFF
    c.pc = SAFE_PC
    c.eit(0x40)
    n = 0
    while n < max_steps and not c.halted:
        c.step()
        n += 1
        m.step_count += 1
        if c.pc == SAFE_PC and n > 1:
            break
    r12 = c.r[12]
    c.r[:], c.dr[:], c.ps, c.pc, c.delay, c.halted = saved
    return {"steps": n, "err": r12 - 0x100000000 if r12 & 0x80000000 else r12}


def set_flag(m, flag, bits):
    """用固件自己的 sys_set_flg 置位（会顺带把等待中的任务挂回就绪队列）。"""
    return kernel_call(m, -48, r4=flag, r5=bits)


def wake_task(m, tsk_id):
    """用固件自己的 sys_wup_tsk 唤醒一个睡眠任务。"""
    return kernel_call(m, -39, r4=tsk_id)


def summary(m):
    """跑完之后的概览：当前任务、各任务上下文、启动阶段。"""
    out = {"cur_tcb": int.from_bytes(m.fetch_bytes(CUR_TCB, 4), "big"),
           "stage": int.from_bytes(m.fetch_bytes(0x3FBB0, 4), "big"),
           "tasks": []}
    for t in states(m):
        out["tasks"].append({"id": t["id"], "tcb": t["tcb"], "sp": t["sp"],
                             "entry": t["entry"], "pc": t["pc"],
                             "built": t["pc"] != 0,
                             "active": t["active"], "wait_obj": t["wait_obj"],
                             "wait_pat": t["wait_pat"]})
    return out
