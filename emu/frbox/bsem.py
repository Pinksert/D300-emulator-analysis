# -*- coding: utf-8 -*-
"""bsem.py —— B 段 REALOS 的信号量表与计数（V33 立，V35 按表结构重写）。

一、表：4 字节表头 + 16 字节记录（结构见 btable.py）
    表头 @0x54F2C = 0x00280010 → 40 条 | 项宽 16
    记录 i @ 0x54F30 + (i-1)*0x10  →  +0 对象指针 / +4 编号<<16 / +8 保留 0 /
                                     +0xC 参数（高半字 = 初始计数、低半字 = 上限）
    表止于 0x551B0（flag 表的表头正好接在这里）

二、对象（0x68000638 起，步长 0x10）
    +0x00 自指 / +0x04 自指（等待队列哨兵） / +0x08 计数（半字） / +0x0C **记录地址**
    V35 全程记账（boot 191,520 步，内核碰信号量区 983 次）：对象只被写 +0/+4/+8/+0xC，
    +0xA 一次都没碰 → **上限不存对象里**，要拿就顺背指回记录读 +0xC 的低半字。

三、计数
    初始：初始化循环 0x408F4 逐条读记录 +0xC 的高半字（动态记账 0x408FE / 0x4090A）
    运行：wai_sem 0x4193A 写 0、sig_sem 0x418F0 写 1（只动对象 +0x08）
    boot 到空闲后：37 个 = 1、sem 14 / 27 / 29 = 0（它们本来就是 0 起，不是「被占用」）

四、上限有没有被检查？
    有检查代码，但没跑到：0x418E8 `lduh` → 0x418EA `add #1` → 0x418EC `cmp` →
    0x418EE `bgt`（超上限就跳过写回）→ 0x418F0 `sth`。
    V35 实测：boot 到空闲（191,520 步）＋再空转 600,000 步，0x418F0 / 0x4193A 命中 **0 次**
    —— wai_sem / sig_sem 在这台模拟里一次都没执行过（V33 说的「boot 各 6 次」是静态计数，不是动态）。

⚠️ 别用 brealos.kernel_call() 问内核「某 id 存不存在」——上下文不对，见 KI#122。
⚠️ table_addr() 是历史接口：它比记录地址早 4 字节。要用记录请用 record_addr()。
"""
from . import btable

SEM_TABLE = 0x54F2C
SEM_STRIDE = 0x10
SEM_COUNT = 40

SEM_OBJ_BASE = 0x68000638
SEM_OBJ_STRIDE = 0x10

#: 对象字段
F_SELF = 0x00
F_WAITQ = 0x04
F_COUNT = 0x08
F_BACKPTR = 0x0C            # = 记录地址（V35 更正）

#: 记录字段（= btable 的 R_*）
T_OBJ = btable.R_OBJ
T_IDWORD = btable.R_ID
T_PARAM = btable.R_PARAM

COUNT_WRITER_INIT = 0x00040910   # 初始化：sth 计数（值 = 记录 +0xC 高半字）
INIT_ROUTINE = 0x000408F4        # 初始化循环起点（ldi:32 r2, #0x54F2C）
COUNT_WRITER_WAI = 0x0004193A    # wai_sem 的 sth（写 0 = 拿锁）
COUNT_WRITER_SIG = 0x000418F0    # sig_sem 的 sth（写 1 = 放锁）
SIG_MAX_CHECK = 0x000418E8       # 上限检查那段：lduh / add 1 / cmp / bgt / sth

TAIL_PAD = 0x000551AC            # 记录 40 的 +0xC（参数 0x00010001）；V33 叫它「表尾补字」
TABLE_END = 0x000551B0           # 表真正结束的地方（= flag 表表头）


def table_addr(sid):
    """历史接口：第 sid 条的「错位表项」地址 = 记录地址 − 4（V33 的叫法）。"""
    return SEM_TABLE + (sid - 1) * SEM_STRIDE


def record_addr(sid):
    """第 sid 条记录的地址（0x54F30 + (sid-1)*16）。"""
    return btable.record_addr(SEM_TABLE, sid)


def entry(m, sid):
    """读一条记录（新旧键都给，方便老探针）。"""
    r = btable.record(m, SEM_TABLE, sid)
    a = table_addr(sid)
    words = [m.bus.rd(a + 4 * k, 4) for k in range(4)]
    r = dict(r)
    r.update({"id": sid, "words": words, "hi": words[0] >> 16, "lo": words[0] & 0xFFFF,
              "idword": r["id_word"], "id_hi": r["id_word"] >> 16,
              "param": r["param_word"], "init": r["init"], "max": r["max"]})
    return r


def entries(m, count=SEM_COUNT):
    """整张表（默认 40 条）。"""
    return [entry(m, n) for n in range(1, count + 1)]


def object_addr(m, sid):
    """对象的真实地址（走固件自己的路径：记录 +0）。"""
    return m.bus.rd(record_addr(sid) + T_OBJ, 4)


def object_addr_calc(sid):
    """按实测步长算出来的对象地址（用于核对记录）。"""
    return SEM_OBJ_BASE + (sid - 1) * SEM_OBJ_STRIDE


def count(m, sid):
    """当前计数（半字 @obj+8）。"""
    return m.bus.rd(object_addr(m, sid) + F_COUNT, 2)


def set_count(m, sid, val):
    """直接改计数（替固件写，不执行内核代码）。"""
    m.bus.wr(object_addr(m, sid) + F_COUNT, 2, val & 0xFFFF)
    return val & 0xFFFF


def params(m, sid):
    """记录里的 (初始计数, 上限)。"""
    return btable.param(m, SEM_TABLE, sid)


def initial_count(m, sid):
    """初始计数 = 记录 +0xC 的高半字。"""
    return params(m, sid)[0]


def max_count(m, sid):
    """上限 = 记录 +0xC 的低半字（对象里没有这一格）。"""
    return params(m, sid)[1]


def is_counting(m, sid):
    """上限 > 1 就是计数信号量。"""
    return max_count(m, sid) > 1


def waitq(m, sid):
    """对象的 +4（等待队列哨兵；等于自指即空）。"""
    return m.bus.rd(object_addr(m, sid) + F_WAITQ, 4)


def backptr(m, sid):
    """对象的 +0xC（= 自己的记录地址）。"""
    return m.bus.rd(object_addr(m, sid) + F_BACKPTR, 4)


def summary(m):
    """整表体检：条数、记录↔对象是否互指、计数分布、参数异常项。"""
    rows = entries(m)
    counts = {r["id"]: count(m, r["id"]) for r in rows}
    return {"count": len(rows),
            "mismatched": [r["id"] for r in rows if r["obj"] != object_addr_calc(r["id"])],
            "init_rule_fail": [r["id"] for r in rows if initial_count(m, r["id"]) != counts[r["id"]]],
            "counts_not_one": {k: v for k, v in counts.items() if v != 1},
            "odd_first_word": [(r["id"], r["words"][0]) for r in rows
                               if r["words"][0] != 0x00010001],
            "not_binary": [r["id"] for r in rows if max_count(m, r["id"]) != 1],
            "table_end": TABLE_END}


def describe():
    """一张小抄（打印用）。"""
    return "\n".join([
        "信号量表 @0x%08X：表头 4B + 40 条 × 0x%02X（记录 0x%08X 起，止于 0x%08X）"
        % (SEM_TABLE, SEM_STRIDE, record_addr(1), TABLE_END),
        "  记录：+0 对象 / +4 编号<<16 / +8 0 / +C [初始计数 | 上限]",
        "  对象：0x%08X + (id-1)*0x%02X；+0 自指 / +4 等待哨兵 / +8 计数 / +C 记录地址"
        % (SEM_OBJ_BASE, SEM_OBJ_STRIDE),
        "  计数写者：0x%08X（初始化） / 0x%08X（wai_sem） / 0x%08X（sig_sem）"
        % (COUNT_WRITER_INIT, COUNT_WRITER_WAI, COUNT_WRITER_SIG),
        "  上限检查代码在 0x%08X，但实测未执行" % SIG_MAX_CHECK,
    ])
