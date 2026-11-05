# -*- coding: utf-8 -*-
"""brealos.py —— B 段（d300-b）的 REALOS 内核调用与任务视图（V22）。

前提：V22 把 0x68000000（内核对象区）和 0x9F800000（任务栈区）建了 RAM 之后，
B 段自带的 REALOS 内核原生跑起来了：boot 191,520 步 → 派发 retI → 空闲任务
0x407DC（`bra -1` 自旋）。之后内核就在等两件事：

  * 系统调用（int #0x40 → B 向量表 0x40 槽 → 0x40668 陷阱入口）
  * 调度请求（INT 0x3F → 0x40D32 的内联派发：存当前任务现场 →
    USP←[TCB+0x18] → 恢复下一任务现场）

本模块把「替固件发一次系统调用 / 请求一次派发」做成可复用的工具，
用法与 A 段 `realos.kernel_call` 相同（V17 建立的口径）：

    from frbox import brealos
    r = brealos.kernel_call(m, brealos.SVC_STA_TSK, r4=3)   # sys_sta_tsk(3)
    brealos.request_dispatch(m)                              # INT 0x3F
    brealos.states(m)                                        # 任务状态表

⚠️ 边界（写报告必须带上）：
  * 这是「注入外部事件」，不是硬件真的产生了事件；
  * 调用发生在被中断的当前任务上下文里（EIT 用系统栈），返回后寄存器原样恢复；
  * sys_sta_tsk 的 err=0 只说明内核受理了，任务要等下一次派发才真正上 CPU。
"""

# ---- 常量（全部实测，见 docs/03-frbox.md V22 段）----

SAFE_PC = 0x000407DC        # 空闲任务 `bra -1`：当「系统调用已返回」的标记
TRAP_VEC = 0x40             # int #0x40 陷阱（→ 0x40668）
DISPATCH_VEC = 0x3F         # 调度请求中断（→ 0x40D32 内联派发）
IDLE_PC = 0x000407DC

TASK_TABLE = 0x00054C00     # 任务表真实基址（V23 修正：V22 用的 0x54C10 偏了 0x10）
TASK_COUNT = 28             # 正常任务 28 条；空闲任务是表后第 29 条（IDLE_ENTRY）
IDLE_ENTRY = 0x00054F10     # 空闲任务表项：+0x10 入口 0x407DC、+0x14 栈 0x68000B98
TCB_BASE = 0x6800013C       # TCB 数组（步长 0x2C；0x6800013C 起实测自指）
TCB_STRIDE = 0x2C
CUR_TCB_SLOT = 0x68000000   # 当前任务 TCB 槽（派发序列写 [0x68000000]=r14）

# 任务表字段（V23 修正后的布局，表头 0x54BFC 的 0x001C001C = 步长标记）：
#   entry[k] @ 0x54C00 + k*0x1C（k=0..27，任务号 = (+0x04>>16)）
#     +0x00  TCB 指针（0x6800013C + k*0x2C）—— 与 TCB+0x24 背指表项基址互为双向链
#     +0x04  任务号对（高 16 位 = 1 基任务号；低 16 位含义未定，不是纯序号）
#     +0x0C  激活标志：位 30 常见，位 31 只有任务 2（0xC0000000）和空闲（0x80000000）
#     +0x10  入口
#     +0x14  栈顶
#   派发序列读的 [0x54F10] 就是空闲任务表项的 +0x00（idle TCB）。
# V22 的「表 +0x0C 域与 TCB 错位一格」其实是基址偏 0x10 读串了相邻表项：
# 旧 +0x0C 落在新表下一项的 +0x00（下一任务的 TCB），所以旧读数恒差一条。
F_TCB = 0x00
F_IDS = 0x04
F_FLAGS = 0x0C
F_ENTRY = 0x10
F_STACK = 0x14

# μITRON 服务号（负数，32 位补码进 R12 —— V17 的口径）
SVC_STA_TSK = -23
SVC_TER_TSK = -25
SVC_CHG_PRI = -27
SVC_WUP_TSK = -39
SVC_SLP_TSK = -41
SVC_EXT_TSK = -45
SVC_WAI_FLG = -46
SVC_CLR_FLG = -47
SVC_SET_FLG = -48
SVC_WAI_SEM = -53
SVC_SIG_SEM = -55
SVC_RCV_MSG = -61
SVC_SND_MSG = -63
SVC_SET_TIM = -83

# 服务号 → 处理函数（V24 动态解出：在陷阱派发点 0x406B2 抓 `call r12` 的目标，
# 见 probe/exp_bflag.py A 段）。陷阱入口 0x40668 先把 R12 存起来、进内核态，
# 在 0x406A6 装 0x40660、0x406AC 用「以 R12 为索引」的方式取跳转入口，
# 最后 0x406B2 `call r12` —— 所以静态读表会被 0x40660 那段代码误导，
# 只能动态抓。这张表是 1.11 的实测值：
SERVICES = {
    SVC_STA_TSK: 0x000414C8,     # 挂任务进就绪队列
    SVC_TER_TSK: 0x000415A0,
    SVC_CHG_PRI: 0x0004167C,
    SVC_SLP_TSK: 0x00040660,     # 空桩 `ret:d`（B 段未实现，调用返回负值）
    SVC_EXT_TSK: 0x00040664,     # 空桩
    SVC_WAI_FLG: 0x00041B0A,     # 事件标志等待（语义见 bflag.py）
    SVC_CLR_FLG: 0x00041AE2,
    SVC_SET_FLG: 0x00041A48,
    SVC_WAI_SEM: 0x00041902,
    SVC_SIG_SEM: 0x000418C0,
    SVC_RCV_MSG: 0x00041D38,
    SVC_SND_MSG: 0x00041CDE,
    SVC_SET_TIM: 0x00040EDE,
}

FLAG_TABLE = 0x000551B0     # 事件标志对象表（bflag.py 也用这个）

DISPATCH_CALL_PC = 0x000406B2   # 陷阱里 `call r12` 的那条指令（抓目标用）


def service_handler(m, svc, max_steps=20000):
    """动态求「服务号 → 处理函数」：跑一次陷阱，在派发点抓 R12。

    比读表可靠（0x40660 那块是代码不是表，静态读会读成垃圾）。
    调用期间不改变机器状态以外的语义 —— 它真的执行一次系统调用，
    只是参数全 0，多半返回参数错误（E_PAR/E_ID），不影响后续实验。
    """
    c = m.cpu
    saved = (list(c.r), list(c.dr), c.ps, c.pc, c.delay, c.halted)
    c.r[4] = c.r[5] = c.r[6] = c.r[7] = 0
    c.r[12] = svc & 0xFFFFFFFF
    c.pc = SAFE_PC
    c.eit(TRAP_VEC)
    tgt, n = None, 0
    while n < max_steps and not c.halted:
        m.step(1)               # 走机器时基（设备 tick 才不会被绕过，见 V20）
        n += 1
        if c.pc == DISPATCH_CALL_PC:
            tgt = c.r[12]
        if c.pc == SAFE_PC and n > 1:
            break
    c.r[:], c.dr[:], c.ps, c.pc, c.delay, c.halted = saved
    return tgt



def kernel_call(m, svc, r4=0, r5=0, r6=0, r7=0, r13=0, max_steps=80000):
    """在 B 段自己的 REALOS 内核里执行一次系统调用。

    与 realos.kernel_call（A 段）同构：参数进 R4–R7/R12（R12 是 32 位补码），
    执行一次 eit(0x40)，`reti` 回到 SAFE_PC（空闲任务的 bra -1）就算返回，
    之后把寄存器原样恢复。返回 {'steps', 'err'}。
    """
    c = m.cpu
    saved = (list(c.r), list(c.dr), c.ps, c.pc, c.delay, c.halted)
    c.r[4], c.r[5], c.r[6], c.r[7], c.r[13] = r4, r5, r6, r7, r13
    c.r[12] = svc & 0xFFFFFFFF
    c.pc = SAFE_PC
    c.eit(TRAP_VEC)
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


def sta_tsk(m, tid):
    """sys_sta_tsk(tid, 1)：让内核把任务 tid 挂进就绪队列。"""
    return kernel_call(m, SVC_STA_TSK, r4=tid, r5=1)


def request_dispatch(m, max_steps=200000):
    """请求一次调度（INT 0x3F），跑到派发完成或步数用尽。

    返回 (覆盖增量, 结束 PC)。0x3F 的受理要求 ICR 电平 < ILM ——
    固件把 0x3F 的 ICR 配成 0x1E，空闲任务跑在 ILM=0x1F 下，可受理。
    """
    c0 = m.cov.words() if m.cov else 0
    m.raise_irq(DISPATCH_VEC)
    m.run(max_steps=max_steps, spin_guard=False)
    return (m.cov.words() - c0) if m.cov else 0, m.cpu.pc


def boot(m=None, idle_extra=0):
    """新建 d300-b 并 boot 到空闲任务（191,520 步，确定性）。"""
    from .machine import create
    m = m or create("d300-b")
    if m.cpu is None or m.step_count == 0:
        m.load()
        m.reset()
        m.enable_cov()
        m.attach_flow()
    n = 0
    while not m.cpu.halted and n < 191520:
        m.step(1)
        n += 1
    if idle_extra:
        m.run(max_steps=idle_extra, spin_guard=False)
    return m


def records(m, include_idle=False):
    """读任务表：TCB / 任务号 / 标志 / 入口 / 栈顶。

    include_idle=True 时把表后第 29 条（空闲任务 @0x54F10）也带上一并返回。
    """
    out = []
    n = TASK_COUNT + (1 if include_idle else 0)
    raw = m.fetch_bytes(TASK_TABLE, n * 0x1C)
    for i in range(n):
        e = raw[i * 0x1C:(i + 1) * 0x1C]
        f = [int.from_bytes(e[j:j + 4], "big") for j in (0, 4, 12, 16, 20)]
        out.append({"id": (f[1] >> 16) & 0xFFFF or i + 1,
                    "idx": i + 1,
                    "tcb": f[0], "ids": f[1],
                    "flags": f[2], "entry": f[3], "stack": f[4],
                    "addr": TASK_TABLE + i * 0x1C})
    return out


def states(m):
    """各任务当前状态：入口执行过没有（cov）+ TCB+0x18 的保存 SP。"""
    out = []
    for r in records(m):
        sp = m.bus.rd(r["tcb"] + 0x18, 4) if 0x68000000 <= r["tcb"] < 0x680010000 else 0
        out.append({"idx": r["idx"], "id": r["id"], "entry": r["entry"],
                    "tcb": r["tcb"], "saved_sp": sp,
                    "ran": bool(m.cov and m.cov.visited(r["entry"]))})
    return out


def summary(m):
    """跑完之后的概览。"""
    cur = m.bus.rd(CUR_TCB_SLOT, 4)
    ran = [t["idx"] for t in states(m) if t["ran"]]
    return {"cur_tcb": cur, "pc": m.cpu.pc, "steps": m.step_count,
            "cov": m.cov.words() if m.cov else 0,
            "ran_tasks": ran, "calls": len(m.flow.calls) if m.flow else 0}
