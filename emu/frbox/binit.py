# -*- coding: utf-8 -*-
"""binit.py —— 显示模块的初始化序列 0x12E006（V38 首次完整跑通）。

一、它是谁调的

    V35 报「0x12E006 立即数引用 0 处、rel12 调用点 0 处，所以自己也没人调」。
    V38 扫全片 call 指令找到了唯一一处：

        0x12DFB8  call rel12=0x26  →  0x12DFB8 + 2 + 0x26*2 = 0x12E006

    而 0x12DFAE 又是**任务 5 的入口**（任务表 0x54C00 记录 4 的 +0x10）：

        0x0012DFAE  st r8,@(0,sp) / st rp,@(8,sp) / enter #1
        0x0012DFB4  r8 = 0x62
        0x0012DFB8  call 0x12E006        ← 显示初始化
        0x0012DFBA  call 0x12E006+0x13C
        0x0012DFBC  r0 = 0x13
        0x0012DFBE  call:d …
        ……  然后进主循环

    所以「没人调用」的真正原因是**任务 5 从来没被启动过**，不是这段代码没有主人。

    ⚠️ 任务表记录 0 = 任务 1，记录 4 = 任务 5。V26 记的「显示任务 = 任务 4 = 0x12DFAE」
       是错的：任务 4 的入口是 0x126454，0x12DFAE 属于**任务 5**。

二、跑起来是什么样

    直接调用（`brealos.call_fn(m, INIT_FN)`）**746 步**返回，0 次阻塞 ——
    这是整个 B 段第一次把这段代码完整执行完。它做五件事：

    ① 初始化队列哨兵       0x124F48 / 0x124F4E  → head / head+4 双双自指 0x9F58D500
    ② 给 70 个槽位编号     0x12E61C stb r4,@(8,r5)  循环 → slot[i]+8 = i（70/70）
    ③ 通知显示事件         0x12E630 set_flg(3, 0x80)
    ④ 配硬件定时器         0x5C003000 = 0（sth）、0x5C003008 = 0x0002039F
    ⑤ 置「已初始化」标志   0x9F58DA85 = 1

    另外还写了一批 direct page 寄存器（0x38/0x39/0x3A/0x60/0x62/0x63/0x66/0x67）
    和两个内核对象（0x680008E0 = 0x80、0x68000132 = 0）。

三、为什么以前跑不完

    以前的探针用「往栈上写返回地址 + 改 SP」的方式调固件函数。但 FR 的 `ret`
    读的是**返回地址寄存器 DR1（RP）**，不读栈（见 brealos.call_fn 的说明）。
    这段函数内部有 `call`（0x12E01E → 0x12E602），DR1 被子调用覆盖，结尾的
    `ret` 于是回到「子调用之后」，一路撞进 0x404E0 的兜底自旋 —— 看上去跑了
    几十万步，其实一次都没返回。V38 改用 `cpu.dr[1] = RET` 之后 746 步就干净退出。
"""

MASK = 0xFFFFFFFF

INIT_FN = 0x0012E006          # 显示初始化（本模块的主角）
QUEUE_INIT_FN = 0x0012E602    # 被它调用的那个：清空队列 + 给 70 槽位编号
INIT_CALLER = 0x0012DFB8      # 唯一的调用点（在任务 5 入口里）
TASK_ID = 5                   # 任务表记录 4 → 任务 5
TASK_ENTRY = 0x0012DFAE

QUEUE_HEAD = 0x9F58D500
SLOT_BASE = 0x9F58D508
SLOT_STRIDE = 0x0C
SLOT_COUNT = 70

HW_TIMER = 0x5C003000         # V23 建的 hw-counter 窗口
HW_PERIOD = 0x5C003008
INIT_FLAG = 0x9F58DA85        # 初始化完成后置 1

#: 关键副作用：(地址, 宽度) —— 用来做「这一步做了没」的体检
WITNESSES = (
    ("队列哨兵自指", QUEUE_HEAD, 4),
    ("哨兵尾指针", QUEUE_HEAD + 4, 4),
    ("槽位 0 的编号", SLOT_BASE + 8, 1),
    ("槽位 69 的编号", SLOT_BASE + 69 * SLOT_STRIDE + 8, 1),
    ("硬件定时器周期", HW_PERIOD, 4),
    ("初始化标志", INIT_FLAG, 1),
)

#: 以前 V35/V36 记的「0x12E006 没有调用者」—— 现在知道是被任务 5 入口调的
CALLER_CHAIN = (
    (0x0012DFAE, "任务 5 入口（任务表 0x54C70 记录 4 的 +0x10）"),
    (0x0012DFB8, "call 0x12E006（rel12 = 0x26）"),
    (0x0012E006, "显示初始化本体"),
    (0x0012E01E, "它开头第一件事：call 0x12E602"),
    (0x0012E602, "清空队列 + 70 槽位编号"),
)


def run(m, max_steps=200000):
    """跑一遍显示初始化。返回 {'steps', 'pc', 'ret', 'writes'}。

    需要 `frbox.brealos` 里的 call_fn（返回地址必须设 DR1）。
    """
    from . import brealos
    log = []

    def spy(addr, sz, val, pc=None):
        log.append((pc, addr, sz, val))
        return base_wr(addr, sz, val, pc)

    base_wr = m.bus.wr
    m.bus.wr = spy
    try:
        r = brealos.call_fn(m, INIT_FN, max_steps=max_steps)
    finally:
        m.bus.wr = base_wr
    r["writes"] = log
    return r


def queue_init(m):
    """只跑 0x12E602（清空 + 编号），不碰外设。"""
    from . import brealos
    return brealos.call_fn(m, QUEUE_INIT_FN)


def slot_index(m, i):
    """槽位 i 的 +8 —— 初始化后应该正好等于 i。"""
    return m.bus.rd(SLOT_BASE + i * SLOT_STRIDE + 8, 1)


def payload_map(m, count=SLOT_COUNT):
    """初始化后 70 个槽位的 +8 全表。"""
    return [slot_index(m, i) for i in range(count)]


def is_initialized(m):
    """初始化标志（0x9F58DA85）—— 只有跑过 0x12E006 才会是 1。"""
    return m.bus.rd(INIT_FLAG, 1) == 1


def status(m):
    """不用真跑，直接读内存判断「初始化做到了哪一步」。"""
    return {
        "initialized": is_initialized(m),
        "head": m.bus.rd(QUEUE_HEAD, 4),
        "head_ok": m.bus.rd(QUEUE_HEAD, 4) == QUEUE_HEAD
                    and m.bus.rd(QUEUE_HEAD + 4, 4) == QUEUE_HEAD,
        "payload": payload_map(m),
        "hw_period": m.bus.rd(HW_PERIOD, 4),
    }


def audit(m):
    """对一台**已经跑过初始化**的机器做体检。"""
    st = status(m)
    pay = st["payload"]
    return {
        "initialized": st["initialized"],
        "head_ok": st["head_ok"],
        "numbered": pay == list(range(SLOT_COUNT)),
        "payload": pay,
        "hw_period": st["hw_period"],
        "bad": [i for i, v in enumerate(pay) if v != i],
    }


def describe(m):
    """人读的一行摘要。"""
    a = audit(m)
    if not a["initialized"]:
        return ("显示初始化**没跑过**：0x9F58DA85 = 0，队列哨兵 %s，"
                "70 个槽位的 +8 全 0 → 取首恒返回 0"
                % ("已自指" if a["head_ok"] else "未自指"))
    return ("显示初始化已跑：哨兵自指、%s、硬件定时器已配置（写 0x0002039F，"
            "这台设备的寄存器只保留低字节，读回 0x9F）、标志 0x9F58DA85 = 1"
            % ("70 个槽位编号 0..69 全对" if a["numbered"]
               else "但编号有 %d 格不对" % len(a["bad"])))


def dump(m):
    a = audit(m)
    lines = ["显示初始化 0x%08X（调用者 0x%08X，任务 %d）" % (INIT_FN, INIT_CALLER, TASK_ID),
             "  " + describe(m)]
    for name, addr, n in WITNESSES:
        v = m.bus.rd(addr, n)
        lines.append("  %-14s @0x%08X = 0x%0*X" % (name, addr, n * 2, v))
    return "\n".join(lines)


__all__ = ["INIT_FN", "QUEUE_INIT_FN", "INIT_CALLER", "TASK_ID", "TASK_ENTRY",
           "QUEUE_HEAD", "SLOT_BASE", "SLOT_STRIDE", "SLOT_COUNT",
           "HW_TIMER", "HW_PERIOD", "INIT_FLAG", "WITNESSES", "CALLER_CHAIN",
           "run", "queue_init", "slot_index", "payload_map", "is_initialized",
           "status", "audit", "describe", "dump"]
