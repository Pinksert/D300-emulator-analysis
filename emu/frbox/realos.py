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


def summary(m):
    """跑完之后的概览：当前任务、各任务上下文、启动阶段。"""
    out = {"cur_tcb": int.from_bytes(m.fetch_bytes(CUR_TCB, 4), "big"),
           "stage": int.from_bytes(m.fetch_bytes(0x3FBB0, 4), "big"),
           "tasks": []}
    for r in records(m):
        c = context(m, r["sp"]) if r["sp"] else {"pc": 0, "ps": 0}
        out["tasks"].append({"id": r["id"], "tcb": r["tcb"], "sp": r["sp"],
                             "entry": r["entry"], "pc": c["pc"],
                             "built": c["pc"] != 0})
    return out
