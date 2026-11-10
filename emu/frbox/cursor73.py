# -*- coding: utf-8 -*-
"""V73：KI#282 第一条的前提证伪与真机事件链定位（纯函数，只读 ROM / 常量）。

承接 V72。KI#282 第一条原写「真机给 ``0x9F58DA84`` 填待显示项的
中断事件流」。V73 静态 + 动态双路证明这个前提不成立：

* ``0x9F58DA84``（V72 的"有效项字节"）**不是等真机填的**。固件自己在
  第一道等待函数 ``0x12E0F8``（真实边界 ``[0x12E0FC,0x12E160)``）里
  写它，共三个写点：``0x12E112`` 写常量 0、``0x12E140``（在
  ``bne:d`` 延迟槽里）与 ``0x12E158`` 写 ``call 0x12E636`` 的返回值。
  收敛态 ``0x12E140`` **已经命中**，写入 0，所以九字节全 0 不是
  「真机没给事件」，是查询函数返回 0（规矩 58 的推广）。
* ``0x12E636`` 是 ``wai_sem(17)`` / 状态查询函数：它读对象池
  ``0x9F58D500`` 的 sem 17，返回 ``0x9F58D500+8`` 那个字节。
* 真正由真机中断事件驱动的是 ``0x9F58DA83``（描述符索引）。它唯一
  的固件写点 ``0x12F780`` 在独立短函数 ``0x12F77A`` 里，该函数
  同时清 ``0x9F58DA86`` 并 ``set_flg(3,0x10)``。
* 事件链已定位到具体向量（**向量表是倒序映射**）：
  ``vec 0x20`` -> ISR ``0x12FE54`` -> 读 ``0x9F58DA85`` 当索引
  -> 查 ``0x1BC480`` 的 24 项回调表 -> ``call @r0``；
  ``vec 0x1B`` -> ISR ``0x12FDB4`` -> 读 ``0x9F58DA86``
  -> 查 ``0x1BC490`` 表 -> ``call @r0``。
  回调 ``0x12F77A`` 是表 A 第 5 项 / 表 B 第 1 项。

**规矩 60（本版新增）**：报「某 ISR 挂哪个向量」前先按
``vector(n) = TBR + (255-n)*4`` 的**倒序**换算，别把正序槽号当 vec。
V73 G 段四组反事实全部 0 命中，就是因为沿用了收敛用的 ``0x2B``、
没打到 ``0x20``；那是实验配置错误，不是「事件链不存在」。

**规矩 61（本版新增）**：报「某字节等真机填」前先扫固件自己的写点。
DA84 有 3 个固件写点，且收敛态已命中其中一个。

**规矩 53 的等形验证**：J1 只写索引字节、不打中断，新增 0 个 PC ⇒
写数据门不等于给调度事件；J5 索引写对了但向量错配，同样 0 命中。

**规矩 59 的大规模实例**：V71 报「4 个 ``set_flg(3,0x10)`` 点全在
``0x12F540`` ISR 体、归属函数起点去重 = ``['0x12F540']``」是
``enclosing()`` 的归属假象。按 ``ret`` 拆真实边界，四点分属四个
独立小函数：``0x12F77A`` / ``0x12F950`` / ``0x12FAE8`` / ``0x12FD9E``。

所有写索引字节 / ``raise_irq`` / ``kernel_call`` 都是外部反事实补全，
不是固件行为；不能报显示链通了 / 出画面 / 真机如此。
"""

# ---------------------------------------------------------------- 地址常量
TBR = 0x000EFC00
VEC_CONV = 0x2B             # V65 起的收敛向量（不是事件链向量）
VEC_A = 0x20                # ISR_A 的向量（槽 223）
VEC_B = 0x1B                # ISR_B 的向量（槽 228）
ISR_A = 0x0012FE54          # 派发器 A：读 DA85，查 0x1BC480
ISR_B = 0x0012FDB4          # 派发器 B：读 DA86，查 0x1BC490
DISPATCH_CALL_A = 0x0012FE70
DISPATCH_CALL_B = 0x0012FE04
CB_TABLE_A = 0x001BC480     # 24 项，紧接 70 条 × 0x10 描述符表之后
CB_TABLE_B = 0x001BC490     # 表 A 第 4 项起（同一数组的另一入口）
CB_COUNT = 24
IDX_A = 0x9F58DA85          # 派发器 A 的索引字节
IDX_B = 0x9F58DA86          # 派发器 B 的索引字节
CB_FN = 0x0012F77A          # 写 DA83 的独立回调
CB_WRITE = 0x0012F780       # stb r4,@DA83
CB_CLEAR = 0x0012F78A       # stb r0,@DA86
CB_SETFLG = 0x0012F794      # int #0x40, svc -48, flg 3, bits 0x10
CB_RET = 0x0012F796

WAIT1_FN = 0x0012E0F8       # 第一道等待（V70）
WAIT1_ENTER = 0x0012E0FC
WAIT1_END = 0x0012E160
QUERY_FN = 0x0012E636       # 决定 DA84 的查询函数
SEMPOOL = 0x9F58D500
SEM_ID = 17

CUR_BASE = 0x9F58DA7C
IDX_BYTE = 0x9F58DA83       # DA83：描述符索引（真机事件侧）
VALID_BYTE = 0x9F58DA84     # DA84：查询函数返回值（固件侧）
CLEAR_BYTE = 0x9F58DA82
DESC_PTR = 0x9F58D858
D87B = 0x9F58D87B

# DA84 的三个固件写点：(PC, 写入值来源, 是否在延迟槽)
DA84_WRITERS = (
    (0x12E112, "const0", False),
    (0x12E140, "query_ret", True),
    (0x12E158, "query_ret", True),
)
# V71 的四个 set_flg(3,0x10) 点 -> 按 ret 拆出的真实函数起点
SETFLG_POINTS = (
    (0x12F794, 0x12F77A, 0x12F796),
    (0x12F964, 0x12F950, 0x12F966),
    (0x12FB10, 0x12FAE8, 0x12FB20),
    (0x12FDB0, 0x12FD9E, 0x12FDB2),
)
SETFLG_ENCLOSING = 0x12F540  # enclosing() 的错误归属

# 服务号（8 位补码）
SET_FLG = -48
CLR_FLG = -47
WAI_FLG = -46
WAI_SEM = -55
POL_SEM = -53
GET_TID = -24
FLAG_ID = 3
BIT10 = 0x10
WANTPTN_1 = 9

# 六组等形反事实：(标签, 索引地址, 索引值, 中断向量, 说明)
GROUPS = (
    ("J0", None, 0, None, "都不给"),
    ("J1", IDX_A, 5, None, "只给索引不给中断"),
    ("J2", None, 0, VEC_A, "只给 vec 0x20"),
    ("J3", IDX_A, 5, VEC_A, "索引 5 + vec 0x20"),
    ("J4", IDX_B, 1, VEC_B, "索引 1 + vec 0x1B"),
    ("J5", IDX_A, 5, VEC_CONV, "索引 5 + 错配 vec 0x2B"),
)


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """有符号扩展。"""
    m = 1 << (bits - 1)
    return (v ^ m) - m if v & m else v


def slot_of_vec(vec, tbr=TBR):
    """倒序映射：vec -> 向量表字节地址（规矩 60）。"""
    return tbr + (255 - (vec & 0xFF)) * 4


def vec_of_slot(addr, tbr=TBR):
    """倒序映射：槽地址 -> vec；非 4 对齐或越界返回 None。"""
    d = addr - tbr
    if d < 0 or d > 0x3FC or d % 4:
        return None
    return 255 - d // 4


def cb_entry_addr(table, idx, stride=4):
    """回调表里第 idx 项的地址。"""
    return table + (idx & 0xFF) * stride


def cb_index_of(target_addr, table, stride=4):
    """某表项地址对应的索引；不属于该表返回 None。"""
    d = target_addr - table
    if d < 0 or d % stride:
        return None
    return d // stride


def da84_writers_kinds():
    """三个 DA84 写点按值来源分类计数。"""
    kinds = {}
    for _pc, kind, _slot in DA84_WRITERS:
        kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def da84_is_firmware_written(cov):
    """收敛态固件是否写过 DA84：任一写点落在覆盖里。"""
    return any(pc in cov for pc, _k, _s in DA84_WRITERS)


def real_bounds(point, rets, lo_default):
    """按 ret 拆真实函数边界（规矩 59）：返回 [前一条 ret+2, 下一条 ret)。

    ``enclosing()`` 只找最近的 enter，会把连续的小函数全归给同一个
    大函数；本函数用 ret 序列把边界切开。
    """
    import bisect
    j = bisect.bisect_right(rets, point)
    lo = (rets[j - 1] + 2) if j > 0 else lo_default
    hi = rets[j] if j < len(rets) else None
    return (lo, hi)


def enclosing_mismatch(point, enclosing, rets, lo_default):
    """该点的 enclosing 归属与真实边界是否不一致。"""
    lo, _hi = real_bounds(point, rets, lo_default)
    return enclosing != lo


def three_layer(exists, converged_hits, in_isr):
    """V68 三层判据：存在性 / 执行性 / 触发性。"""
    return {"exists": exists, "executed": converged_hits,
            "needs_event": bool(exists) and not bool(converged_hits)
            and bool(in_isr)}


def equal_shape(shapes):
    """等形校验：除「索引 / 中断」外，BOOT 与 POST 必须全一致。"""
    keys = {
        "boot": {s[0] for s in shapes},
        "post": {s[2] for s in shapes},
    }
    return {"ok": all(len(v) == 1 for v in keys.values()), "keys": keys}


def attribution(group_results, flag):
    """归因分离：只看命中 flag 的组，返回组名列表。"""
    return [tag for tag, r in group_results.items() if r.get(flag)]


def event_chain(vec, idx_value, cb_table, cb_target):
    """给定向量 / 索引 / 表基址，算出该事件应调到哪个回调。"""
    if vec == VEC_A:
        table = CB_TABLE_A
    elif vec == VEC_B:
        table = CB_TABLE_B
    else:
        return {"valid": False, "reason": "vector_mismatch"}
    return {"valid": True, "isr": ISR_A if vec == VEC_A else ISR_B,
            "table": cb_table or table, "idx": idx_value,
            "entry": cb_entry_addr(cb_table or table, idx_value),
            "target": cb_target}


def wants_after_event(want_before, event_hit):
    """vec 0x20 事件后任务 5 的等待位模式（第一道 9 -> 第二道 0x74）。"""
    return 0x74 if event_hit and want_before == WANTPTN_1 else want_before


def chain_verdict(cb_hit, l2_hit, handler_hit, desc_ptr):
    """链是否闭合：回调命中不等于链闭合。"""
    return bool(handler_hit and l2_hit and desc_ptr != 0) if cb_hit else False


def window_same(cov_a, cov_b):
    """窗口自证：两个执行窗口的 PC 覆盖对称差集。"""
    return len(set(cov_a) ^ set(cov_b))
