# -*- coding: utf-8 -*-
"""waitstat67.py —— V67：等待原因解码 / 对象队列归属 / 唤醒条件（纯函数）。

三条口径，都来自本版实证，别再混：

1. **服务号按立即数宽度解码**。ROM 里服务号有两种装法：
   ``ldi:8 #0xD2`` 装的是 8 位字段，要按 8 位补码读成 −46；
   ``ldi:32 #0xFFFFFF56`` 装的已经是 32 位值，要按 32 位补码读成 −170。
   ``bstatic.sx8`` / ``btick.sx8`` 统一取低 8 位，把后者读成 +86。
   全 ROM 有 91 个 −170 点、2 个 −171 点因此被误编号。

2. **``TCB+0x20`` 高半是 r12 的低 16 位**，不是 8 位服务号。
   所以 ``0xFF56`` 要按 16 位补码读成 −170，不能读成 +86。
   −23 / −37 / −46 / −85 这几个服务两种读法恰好一致，问题只在
   绝对值大于 127 的服务号上暴露。

3. **``TCB+0x14`` 语义随等待原因变化**：``wai_flg`` / ``twai_flg``
   里它是等待位模式（``0x041B54 st @(R14,+0x14), r6``），
   ``dly_tsk`` / ``tslp_tsk`` 里它是剩余 tick（``0x040A78 ld #20``）。
   V52 把它命名为 ``TCB_TICKS`` 只在延时队列语境下成立。

另外 ``flag 对象 +0x08`` 是当前位模式：``0x041A5C ld +8`` /
``0x041A60 st +8`` 做 OR 后写回，``0x041B32 ld +8`` 读出来和等待位模式比。

本模块全部是纯函数，不建机器、不读总线，便于自检。
"""
from __future__ import unicode_literals

from collections import Counter

# ---- 服务号 ---------------------------------------------------------------
SVC_NAMES = {
    -23: "sta_tsk", -24: "get_tid", -25: "ter_tsk", -27: "chg_pri",
    -37: "tslp_tsk", -41: "slp_tsk", -46: "wai_flg", -47: "clr_flg",
    -48: "set_flg", -53: "wai_sem", -55: "sig_sem", -61: "rcv_msg",
    -63: "snd_msg", -83: "set_tim", -85: "dly_tsk",
    -170: "twai_flg(-170)", -171: "svc(-171)",
}

#: ROM 里 ``ldi:32`` 装、绝对值 > 127 的服务号（8 位口径会读错的两个）
WIDE_SERVICES = (-170, -171)

# ---- 内核对象 -------------------------------------------------------------
FLAG_OBJ_BASE = 0x680008B8
FLAG_OBJ_STRIDE = 0x10
FLAG_ID_FIRST = 1
FLAG_ID_LAST = 17
DELAY_SENTINEL = 0x68000118

# ---- TCB / 对象字段 -------------------------------------------------------
TCB_NEXT = 0x00
TCB_PREV = 0x04
TCB_FLAGS = 0x08
TCB_WANT = 0x14          # flag 等待：等待位模式；延时等待：剩余 tick
TCB_SAVED_SP = 0x18
TCB_WHY = 0x20           # 高半 = r12 低 16 位
OBJ_NEXT = 0x00
OBJ_PREV = 0x04
OBJ_PAT = 0x08           # flag 当前位模式

READY = 0x00
WAIT = 0x04
DORMANT = 0x10

TRAP_ENTRY = 0x040668
SET_FLG_FN = 0x041A48
WAI_FLG_FN = 0x041B0A
#: 静态证据：把等待位模式写进 TCB+0x14 的那条指令
WANT_STORE_PC = 0x041B54
#: 静态证据：set_flg 读写 flag 对象 +8 的两条指令
PAT_OR_PCS = (0x041A5C, 0x041A60)
#: 静态证据：wai_flg 读出 flag 对象 +8 的那条指令
PAT_LOAD_PC = 0x041B32


# ------------------------------------------------------------------ 解码
def sx8(v):
    """低 8 位补码（旧口径，只在 ``ldi:8`` 装的服务号上成立）。"""
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def sx16(v):
    """16 位补码 —— ``TCB+0x20`` 高半的正确读法。"""
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def sx32(v):
    """32 位补码 —— ``ldi:32`` 装的完整立即数。"""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def svc_of_r12(v, width=32):
    """按**立即数宽度**解码 r12 里的服务号。

    ``width`` 取 8 / 16 / 20 / 32。``ldi:8 #0xD2`` → −46；
    ``ldi:32 #0xFFFFFF56`` → −170。宽度不匹配正是 V67 修掉的口径问题。
    """
    if width not in (8, 16, 20, 32):
        raise ValueError("width must be 8/16/20/32")
    if width == 32:
        return sx32(v)
    mask = (1 << width) - 1
    x = v & mask
    return x - (1 << width) if x & (1 << (width - 1)) else x


def why_svc(raw):
    """``TCB+0x20`` 高半 → 服务号（16 位补码）。"""
    return sx16(raw)


def why_old(raw):
    """旧口径（低 8 位补码），只用来对照它错在哪。"""
    return sx8(raw)


def svc_name(svc):
    return SVC_NAMES.get(svc, "svc(%s)" % svc)


# -------------------------------------------------------------- 对象地址
def flag_obj(ident):
    """flag ID → 内核对象控制块基址（V33 / KI#124）。"""
    return FLAG_OBJ_BASE + (ident - FLAG_ID_FIRST) * FLAG_OBJ_STRIDE


def flag_id_of(addr):
    """对象基址 → flag ID；不在 17 个对象里返回 None。"""
    if addr < FLAG_OBJ_BASE or addr >= FLAG_OBJ_BASE + FLAG_ID_LAST * FLAG_OBJ_STRIDE:
        return None
    if (addr - FLAG_OBJ_BASE) % FLAG_OBJ_STRIDE:
        return None
    return (addr - FLAG_OBJ_BASE) // FLAG_OBJ_STRIDE + FLAG_ID_FIRST


def owner_name(addr):
    """队列哨兵地址 → 人话（flag N / 延时队列 / 未知）。"""
    fid = flag_id_of(addr)
    if fid is not None:
        return "flag %d" % fid
    if addr == DELAY_SENTINEL:
        return "延时队列"
    return "0x%08X" % addr if addr is not None else "未定"


# ---------------------------------------------------------------- 归属
def queue_owner(rows, start, max_hops=64):
    """从一个等待 TCB 沿 ``next`` 走，找链上第一个**不是 TCB** 的节点（队列哨兵）。

    ``rows`` 是 ``{tcb: {"next": int, "prev": int}}``，只放 TCB 节点。
    返回 ``(owner, chain)``；``owner`` 为 None 表示链坏或走了整圈没出链。

    这条判据不查 ROM 表、不猜对象类型：内核把等待队列做成内嵌在对象
    ``+0/+4`` 的双向环链，链上唯一的非 TCB 节点就是对象自己。
    """
    chain, seen = [], set()
    cur = start
    for _ in range(max_hops):
        if cur == 0 or cur in seen:
            return (None, chain)
        seen.add(cur)
        row = rows.get(cur)
        if row is None:
            return (cur, chain)
        chain.append(cur)
        cur = row.get("next", 0)
    return (None, chain)


def build_assignments(rows, waiting):
    """``{owner: [tcb, ...]}``，按 owner 升序、链内按 TCB 地址升序。"""
    out = {}
    for t in sorted(waiting):
        owner, _ = queue_owner(rows, t)
        out.setdefault(owner, []).append(t)
    return dict(sorted(out.items(), key=lambda kv: (kv[0] is None, kv[0])))


def closure(assign, waiting):
    """闭环校验：每个等待 TCB 恰好被归到一条队列，没有外来节点。

    返回 ``{"ok", "missing", "extra", "duplicated", "queues", "assigned"}``。
    """
    flat = [t for lst in assign.values() for t in lst]
    c = Counter(flat)
    missing = sorted(set(waiting) - set(flat))
    extra = sorted(set(flat) - set(waiting))
    dup = sorted(t for t, n in c.items() if n > 1)
    return {
        "ok": not (missing or extra or dup),
        "missing": missing, "extra": extra, "duplicated": dup,
        "queues": len(assign), "assigned": len(flat),
    }


# ---------------------------------------------------------------- 唤醒
def wakeable(pat, want):
    """当前位模式 ``pat`` 能否满足等待位模式 ``want``。

    返回 ``(or_ok, and_ok, blocked)``：
      * ``or_ok``  = TWF_ORW 下会被唤醒（有一位重合即可）；
      * ``and_ok`` = TWF_ANDW 下会被唤醒（所有位都置上）；
      * ``blocked``= 两种模式**都**不唤醒（``want != 0`` 且无任何位重合）。

    ``blocked`` 不依赖等待模式字段，所以无需解出 mode 就能下结论。
    """
    hit = pat & want
    return (hit != 0, hit == want, (hit == 0 and want != 0))


def classify_wait(why, flags_word):
    """按等待原因给一个粗分类：flag / delay / other。"""
    if why in (-46, -170, -171):
        return "flag"
    if why in (-85, -37, -41):
        return "delay"
    return "other"
