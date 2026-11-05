# -*- coding: utf-8 -*-
"""btable.py —— B 段 REALOS 的对象表「一张脸」：4 字节表头 + 16 字节记录（V35 实测）。

V33 说「表项 +0 是两个半字」，V34 说「第一项是表头 [条数|项宽]」，两次都对了一半：
**表头只有 4 字节**，记录从表头后 4 字节开始，所以从记录边界看过去，每条记录都像是
「上一条记录的最后 4 字节」——那个错位就是「初始计数要从下一项读」之谜的全部内容。

一、表长什么样

    表头  4 字节：高半字 = 条数、低半字 = 项宽（只认 16）
    记录 16 字节：+0x00 对象指针
                  +0x04 id << 16
                  +0x08 保留（boot 后为 0）
                  +0x0C 参数：高半字 = 初始计数、低半字 = 上限

    sem  表 @0x54F2C：表头 0x00280010 = 40|16，记录 0x54F30 起，40 条 → 止于 0x551B0
    flag 表 @0x551B0：表头 0x00110010 = 17|16，记录 0x551B4 起，17 条 → 止于 0x552C4
    —— 两张表**首尾相接**：sem 表最后一条记录的尾巴正好是 0x551AC，flag 表头接在 0x551B0。

二、对象与记录互为背指

    对象 @0x68000638 + (id-1)*0x10：+0x00 自指 / +0x04 自指（等待队列哨兵）
                                    +0x08 计数（半字） / +0x0C **记录地址**（背指）
    40 条记录的对象指针与 40 个对象的背指**双向全对**（见 audit()）。

    ⚠️ 上限**不在对象里**：V35 把内核在 boot 全程对信号量区的 983 次访问全记账，
    对象只有 +0/+4/+8/+0xC 四个字段被写过，+0xA 一次都没被碰 ——
    要拿上限只能走对象 +0xC 回记录、读记录 +0xC 的低半字。

三、初始化循环读的就是「记录 +0xC 的高半字」

    V33 看到初始化例程 0x408F4 从「下一项表项 +0」取初始计数，因此把 sem 14/27/29 的 0
    误判成「被占用」。真正的读点是记录 +0xC 的高半字（动态记账：0x408FE / 0x4090A，
    41 次读，值域 {40, 1, 0}）；「下一项 +0」只是同一个地址换个说法。

四、这套结构只有两处写者

    0x408F4..0x40940 初始化（一条记录一条记录地写 obj / 计数 / 背指）
    0x418F0 sig_sem / 0x4193A wai_sem（只动对象 +0x08 的计数）
"""
# 本模块不依赖 bsem / blist，只认「有 bus.rd/wr 的机器」
TABLE_HEADER_SIZE = 0x04
REC_STRIDE = 0x10

R_OBJ = 0x00
R_ID = 0x04
R_SPARE = 0x08
R_PARAM = 0x0C

#: (名字, 表头地址, 条数)；条数以表头为准，这里的数字只用于自检对照
TABLES = (
    ("sem", 0x00054F2C, 40),
    ("flag", 0x000551B0, 17),
)

#: 对象字段（与记录字段同形：+0 自指 / +4 自指 / +8 计数 / +C 背指）
O_SELF = 0x00
O_WAITQ = 0x04
O_COUNT = 0x08
O_RECORD = 0x0C

SEM_OBJ_BASE = 0x68000638
SEM_OBJ_STRIDE = 0x10
FLAG_OBJ_BASE = 0x680008B8


def header(m, base):
    """读表头：返回 {"raw", "count", "stride"}。"""
    raw = m.bus.rd(base, 4)
    return {"raw": raw, "count": raw >> 16, "stride": raw & 0xFFFF, "base": base}


def record_addr(base, i):
    """第 i 条记录的地址（i 从 1 数起）。"""
    return base + TABLE_HEADER_SIZE + (i - 1) * REC_STRIDE


def record(m, base, i):
    """读一条记录：obj / id / spare / init / max。"""
    a = record_addr(base, i)
    obj = m.bus.rd(a + R_OBJ, 4)
    idw = m.bus.rd(a + R_ID, 4)
    spare = m.bus.rd(a + R_SPARE, 4)
    par = m.bus.rd(a + R_PARAM, 4)
    return {"index": i, "addr": a, "obj": obj, "id": idw >> 16, "id_word": idw,
            "spare": spare, "param_word": par,
            "init": par >> 16, "max": par & 0xFFFF}


def records(m, base, count=None):
    """整张表；count 不给就用表头里的条数。"""
    n = count if count is not None else header(m, base)["count"]
    return [record(m, base, i) for i in range(1, n + 1)]


def table_end(base, count):
    """表的结束地址（最后一条记录的 +0x10）。"""
    return base + TABLE_HEADER_SIZE + count * REC_STRIDE


def record_of_object(m, obj):
    """对象的背指（obj+0xC）→ 自己的记录地址。"""
    return m.bus.rd(obj + O_RECORD, 4)


def param(m, base, i):
    """(初始计数, 上限)。"""
    r = record(m, base, i)
    return r["init"], r["max"]


def find(m, name):
    """按名字取 (base, count)。"""
    for n, base, count in TABLES:
        if n == name:
            return base, count
    raise KeyError(name)


def audit(m, verbose=False):
    """整表体检：表头自洽、对象↔记录互指、计数=初始计数。

    返回 dict：
      ok            全部通过
      headers       [(名字, 条数, 项宽, 表头是否自洽)]
      backptr_bad   obj+0xC 不等于自己记录地址的编号
      reverse_bad   记录 +0 不等于对象地址的编号
      count_bad     对象计数 ≠ 记录里的初始计数
      touching      表与表首尾相接（前一张的结尾 == 后一张的表头）
    """
    out = {"headers": [], "backptr_bad": [], "reverse_bad": [], "count_bad": []}
    spans = []
    for name, base, count in TABLES:
        h = header(m, base)
        out["headers"].append((name, h["count"], h["stride"],
                               h["count"] == count and h["stride"] == REC_STRIDE))
        spans.append((name, base, table_end(base, h["count"])))
        for r in records(m, base, h["count"]):
            if r["obj"] == 0:
                continue
            rec = record_of_object(m, r["obj"])
            if rec != r["addr"]:
                out["backptr_bad"].append((name, r["index"], r["obj"], rec, r["addr"]))
            back = m.bus.rd(r["addr"] + R_OBJ, 4)
            if back != r["obj"]:
                out["reverse_bad"].append((name, r["index"]))
            if name == "sem":
                cnt = m.bus.rd(r["obj"] + O_COUNT, 2)
                if cnt != r["init"]:
                    out["count_bad"].append((r["index"], cnt, r["init"]))
    out["touching"] = [(spans[k][0], spans[k][2], spans[k + 1][0], spans[k + 1][1],
                        spans[k][2] == spans[k + 1][1]) for k in range(len(spans) - 1)]
    out["ok"] = (not out["backptr_bad"] and not out["reverse_bad"]
                 and not out["count_bad"] and all(h[3] for h in out["headers"]))
    return out


def dump(m, name):
    """把一张表打成文本（打印用）。"""
    base, _ = find(m, name)
    h = header(m, base)
    lines = ["%s 表 @0x%08X  表头 0x%08X = %d 条 | 项宽 %d"
             % (name, base, h["raw"], h["count"], h["stride"]),
             "  idx  记录       对象        编号  初始 上限 计数"]
    for r in records(m, base, h["count"]):
        cnt = "-"
        if name == "sem":
            cnt = "%d" % m.bus.rd(r["obj"] + O_COUNT, 2)
        lines.append("  %3d  0x%08X  0x%08X  %3d  %3d  %3d  %s"
                     % (r["index"], r["addr"], r["obj"], r["id"],
                        r["init"], r["max"], cnt))
    return "\n".join(lines)


def describe():
    """一张小抄（打印用）。"""
    return "\n".join([
        "对象表 = 4 字节表头 [条数|项宽] + 16 字节记录 ×N",
        "  记录：+0 对象指针 / +4 编号<<16 / +8 0 / +C [初始计数|上限]",
        "  对象：+0 自指 / +4 等待队列哨兵 / +8 计数 / +C **记录地址**（背指）",
        "  sem  @0x%08X 40 条（记录 0x%08X 起）→ 止于 0x%08X"
        % (TABLES[0][1], record_addr(TABLES[0][1], 1), table_end(TABLES[0][1], 40)),
        "  flag @0x%08X 17 条（记录 0x%08X 起）→ 止于 0x%08X"
        % (TABLES[1][1], record_addr(TABLES[1][1], 1), table_end(TABLES[1][1], 17)),
        "⚠️ 上限只在记录 +C 的低半字，对象里没有存",
    ])
