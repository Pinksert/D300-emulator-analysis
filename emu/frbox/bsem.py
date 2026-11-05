# -*- coding: utf-8 -*-
"""bsem.py —— B 段 REALOS 的信号量对象表与计数（V33 实测）。

V32 的尾巴留了一条「sem 17 的初始计数未证」，并踩到「直接调 kernel_call 问内核
一律返回 -69/-73」的坑（KNOWN_ISSUES #122）。这一版改成**静态读表**，把信号量
这一族的对象布局与计数都落到地址上。

一、信号量表 @0x54F2C，共 40 项，16 字节/项
    （紧接在任务表尾：任务表 0x54C00 + 29×0x1C = 0x54F2C，空闲任务条目后正好接上）
    +0x00  两个半字。多数是 0x00010001；例外：id 1 = 0x00280010、id 15 = 0x00000007、
           id 28 / 30 = 0x000000FF —— 语义未证
    +0x04  对象指针 = 0x68000638 + (id-1)*0x10
    +0x08  id << 16（id 17 → 0x00110000）
    +0x0C  0
    表尾 0x551AC，之后 4 字节填充，flag 表从 0x551B0 起（V24 已解）。

二、信号量对象（0x68000638 起，步长 0x10）
    +0x00  自指指针（= 本对象地址）
    +0x04  自指指针（空等待队列的哨兵？未证 —— 与 V33 解出的循环双链表同形）
    +0x08  计数（**半字**）—— 内核初始化例程 0x40910 写 1；
           wai_sem 在 0x4193A、sig_sem 在 0x418F0 改它（boot 各 6 次，正是
           sem 16/20/21 三对握手）
    +0x0A  0（未证）
    +0x0C  背指表项的对象指针字段（表项 +4）

三、初始计数的规则（40/40 命中）
    初始化例程 0x408F4 的循环读的是**下一项表项 +0 的高半字**，写进对象 +8：
        初始计数(id) = table(id+1).word0 >> 16
    表尾补字 0x551AC = 0x00010001 正好给 id 40 提供 1。
    所以 id 14/27/29 的计数是 0 —— 它们的下一项（15/28/30）的 +0 是 0x00000007 /
    0x000000FF / 0x000000FF，**高半字是 0**；那三个低半字的 7 / 255 没被这个循环用到。
    几何上说得通：表项 +0 的两个半字像是「前一号的 [初始计数 | 上限]」，
    但为什么从下一项读，语义仍未证（记 OPEN）。

四、boot 后的计数
    37 个 = 1；sem 14 / 27 / 29 = 0（初始计数就是 0，不是「被占用」）。
    wai_sem 写 0（拿锁）、sig_sem 写 1（放锁），boot 期间各 6 次，全部回到 1。
    sem 17 = 1 → 显示队列那把锁是**二值互斥量**（V32 的结论由此有了量化依据）。

⚠️ 别用 brealos.kernel_call() 问内核「某 id 存不存在」——上下文不对，见 KI#122。
"""

SEM_TABLE = 0x54F2C
SEM_STRIDE = 0x10
SEM_COUNT = 40              # id 1..40，表尾 0x551AC

SEM_OBJ_BASE = 0x68000638
SEM_OBJ_STRIDE = 0x10

# 表项字段
T_WORDS = 0x00              # 两个半字（含义未证）
T_OBJ = 0x04                # 对象指针
T_IDWORD = 0x08             # id << 16

# 对象字段
F_SELF = 0x00
F_WAITQ = 0x04
F_COUNT = 0x08              # 半字
F_BACKPTR = 0x0C

COUNT_WRITER_INIT = 0x00040910   # 内核初始化：sth 计数（值来自下一项表项 +0 高半字）
INIT_ROUTINE = 0x000408F4        # 初始化循环的起点（ldi:32 r2, #0x54F2C）
COUNT_WRITER_WAI = 0x0004193A    # wai_sem 的 sth（写 0 = 拿锁）
COUNT_WRITER_SIG = 0x000418F0    # sig_sem 的 sth（写 1 = 放锁）
TAIL_PAD = 0x000551AC            # 表尾补字 0x00010001，给 id 40 提供初始计数


def table_addr(sid):
    """第 sid 项的表项基址。"""
    return SEM_TABLE + (sid - 1) * SEM_STRIDE


def entry(m, sid):
    """读一项：返回 dict（raw 四字 + 解释字段）。"""
    a = table_addr(sid)
    w = [m.bus.rd(a + 4 * k, 4) for k in range(4)]
    return {"id": sid, "addr": a, "words": w, "hi": w[0] >> 16, "lo": w[0] & 0xFFFF,
            "obj": w[1], "idword": w[2], "id_hi": w[2] >> 16, "spare": w[3]}


def entries(m, count=SEM_COUNT):
    """整张表（默认 40 项）。"""
    return [entry(m, n) for n in range(1, count + 1)]


def object_addr(m, sid):
    """对象的真实地址（走固件自己的索引路径：表项 +4）。"""
    return m.bus.rd(table_addr(sid) + T_OBJ, 4)


def object_addr_calc(sid):
    """按实测步长算出来的对象地址（用于核对表项）。"""
    return SEM_OBJ_BASE + (sid - 1) * SEM_OBJ_STRIDE


def count(m, sid):
    """当前计数（半字 @obj+8）。"""
    return m.bus.rd(object_addr(m, sid) + F_COUNT, 2)


def set_count(m, sid, val):
    """直接改计数（替固件写，不执行内核代码）。"""
    m.bus.wr(object_addr(m, sid) + F_COUNT, 2, val & 0xFFFF)
    return val & 0xFFFF


def initial_count(m, sid):
    """按实测规则算初始计数：**下一项表项 +0 的高半字**（id 40 用表尾补字）。

    注意这**不是**本项的 +0：「本项 +0 的低半字」看起来是前一号的上限
    （id 15/28/30 的 0x0007/0x00FF），但没被初始化循环用到。
    """
    return m.bus.rd(table_addr(sid) + SEM_STRIDE, 4) >> 16


def waitq(m, sid):
    """对象的 +4（等待队列哨兵；等于自指即空）。"""
    return m.bus.rd(object_addr(m, sid) + F_WAITQ, 4)


def backptr(m, sid):
    """对象的 +0xC（背指表项 +4）。"""
    return m.bus.rd(object_addr(m, sid) + F_BACKPTR, 4)


def summary(m):
    """整表体检：条数、对象地址是否与步长一致、计数分布、异常项。"""
    rows = entries(m)
    mismatched = [r["id"] for r in rows if r["obj"] != object_addr_calc(r["id"])]
    counts = {}
    for r in rows:
        counts[r["id"]] = count(m, r["id"])
    odd_count = {k: v for k, v in counts.items() if v != 1}
    odd_words = [(r["id"], r["words"][0]) for r in rows if r["words"][0] != 0x00010001]
    rule_ok = [r["id"] for r in rows if initial_count(m, r["id"]) != counts[r["id"]]]
    return {"count": len(rows), "mismatched": mismatched, "init_rule_fail": rule_ok,
            "counts_not_one": odd_count, "odd_first_word": odd_words,
            "table_end": SEM_TABLE + len(rows) * SEM_STRIDE}


def describe():
    """一张小抄（打印用）。"""
    return "\n".join([
        "信号量表 @0x%08X  40 项 × 0x%02X（止于 0x%08X）" % (SEM_TABLE, SEM_STRIDE,
                                                         SEM_TABLE + SEM_COUNT * SEM_STRIDE),
        "  表项：+0 两半字（多 0x00010001） / +4 对象指针 / +8 id<<16 / +C 0",
        "  对象：0x%08X + (id-1)*0x%02X" % (SEM_OBJ_BASE, SEM_OBJ_STRIDE),
        "        +0 自指 / +4 自指（空等待队列？） / +8 计数（半字） / +C 背指表项+4",
        "  计数写者：0x%08X（初始化=1） / 0x%08X（wai_sem） / 0x%08X（sig_sem）"
        % (COUNT_WRITER_INIT, COUNT_WRITER_WAI, COUNT_WRITER_SIG),
    ])
