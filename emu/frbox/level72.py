# -*- coding: utf-8 -*-
"""V72：任务 5 五级链与 ``0x12E1EC`` 两条数据早退路径（纯函数，只读不跑机器）。

承接 KI#278 第一条。V71 留下两问：「L2 之后 ``0x12E1EC`` 为什么返回 0」
与「``0x12DFE0 → 0x12E326`` 那一级的条件」。V72 的答案：

* ``0x12E1EC`` 不是空实现，它有 127 条指令、三条返回路径。两条早退
  路径都在延迟槽给 ``r4=0``：``0x9F58DA83 >= 70``（描述符越界，早退 A）
  或 ``0x9F58DA84 == 0``（无待显示项，早退 B）。收敛态两个字节都是 0
  ⇒ 走早退 B。正常路径 ``0x12E320 ldi:8 #1,r4`` 给 ``r4=1``。
* 正常路径把 ``0x9F58D858`` 填成 ``0x001BC020 + idx*0x10``（描述符表
  70 条 × 0x10）。V69 看到的「描述符指针恒 0」因此不是没人写，而是
  写它的函数在早退。
* 任务 5 主体是四级串联 + 第五个 call：L4 通过后 ``0x12DFFE`` 才调
  ``0x12E674``，也就是 V69 找到的门 ``0x12E6E0`` 与唯一间接调用点
  ``0x12E6F0`` 所在的函数。

**规矩 58（报「函数返回 0」前先分清早退 / 空实现 / 判据失败）**：
``0x12E1EC`` 有 127 条指令和两条数据早退路径，动态取证必须看命中 PC
落在哪条路径；从「返回 0」推不出「函数没做事」。V69 把「描述符指针
恒 0」记成开放项，V72 定位到具体的两个字节。

**规矩 59（``enclosing()`` 的函数边界不等于真实函数边界）**：
``0x12E490..0x12E752`` 有 5 个 ``ret``、3 个 ``stm1``/``enter`` 起点，
``bstatic.Sweep.enclosing()`` 却把 ``0x12E674`` / ``0x12E6E0`` /
``0x12E6F0`` 全归到 ``0x12E490``。V70 据此报的「262 条大函数内的
局部标志」应更正为「``0x12E674`` 独立函数内的局部标志」。报「某函数
内有写点」前先按 ``stm1`` / ``ret`` 拆函数边界。

**规矩 48 的大规模实例**：区内 6 个 ``Sweep.calls`` 报 ``svc=86`` 的点
全是 ``ldi:32 #0xFFFFFF56``（32 位 −170）取低 8 位的假象。

所有写字节 / ``set_flg`` 都是外部反事实补全，不是固件行为。
"""

# ---------------------------------------------------------------- 地址常量
TASK5_FN = 0x0012DFB2
LOOP_HEAD = 0x0012DFBA
FIFTH_CALL = 0x0012DFFE
FIFTH_FN = 0x0012E674

WAIT1_FN = 0x0012E0F8          # 循环头第一道等待（V70）
WAIT2_FN = 0x0012E162          # L1：第二道等待（V71，0x74 掩码）
DISPATCH_FN = 0x0012E1EC       # L2：描述符指针填充
WAIT3_FN = 0x0012E326          # L3：第三道等待
L4_FN = 0x0012E412             # L4
BIG_FN = 0x0012E490            # 失败路径大函数
BIG_PROLOGUE = 0x0012E48E      # 每级失败路径的 call:d 目标
GATE_PC = 0x0012E6E0
INDIRECT_CALL = 0x0012E6F0
SUBMIT = 0x0012E6F8
NOOP_HANDLER = 0x0012E752

DESC_TABLE = 0x001BC020
DESC_COUNT = 70
DESC_STRIDE = 0x10
DESC_PTR = 0x9F58D858

CUR_BASE = 0x9F58DA7C          # 游标九字节起点
IDX_BYTE = 0x9F58DA83          # 越界检查字节
VALID_BYTE = 0x9F58DA84        # 有效项 / 描述符索引字节
CLEAR_BYTE = 0x9F58DA82        # 早退时被清 0 的字节

WORD_A = 0x9F5A0284
WORD_B = 0x9F59EF50
SLOTS8 = 0x9F59F7C8
GATE = 0x9F58D08F

# 四级串联状态机：(call PC, cmp PC, bne PC, bne 目标, 失败 bra PC)
LEVELS = (
    (0x12DFBE, 0x12DFC2, 0x12DFC4, 0x12DFD0, 0x12DFCE),
    (0x12DFD0, 0x12DFD2, 0x12DFD4, 0x12DFE0, 0x12DFDE),
    (0x12DFE0, 0x12DFE2, 0x12DFE4, 0x12DFF0, 0x12DFEE),
    (0x12DFF0, 0x12DFF4, 0x12DFF6, 0x12DFFE, 0x12DFFC),
)
# 四级各自的 call 目标（rel12 实测）
LEVEL_TARGETS = (WAIT2_FN, DISPATCH_FN, WAIT3_FN, L4_FN)
# 四级失败路径的 call:d 目标（全部同一个大函数序言）
FAILURE_CALLS = (0x12DFCA, 0x12DFDA, 0x12DFEA, 0x12DFF8)
FAILURE_PROLOGUE = BIG_PROLOGUE

# 0x12E1EC 三条返回路径的特征 PC
EARLY_A = (0x12E226, 0x12E228, 0x12E22A, 0x12E22C)
EARLY_B = (0x12E238, 0x12E23A, 0x12E23C, 0x12E23E)
NORMAL = (0x12E242, 0x12E244, 0x12E24A, 0x12E266, 0x12E274,
          0x12E2EC, 0x12E320)
DISPATCH_RET = 0x12E324

# 区内 6 个 wai_flg 点：PC -> 等待位模式
WAIT_PCS = {
    0x12E09E: 0x02,
    0x12E1D2: 0x74,
    0x12E3F6: 0x74,
    0x12E56A: 0x10,
    0x12E5AA: 0x41,
    0x12E5EA: 0x41,
}

SVC32_RAW = 0xFFFFFF56
SVC32 = -170
SVC8_FALSE = 86
FLAG_ID = 3
EXPECT = 0x10
MASK = 0x74

KEY_PCS = (
    LOOP_HEAD, 0x12DFBE, 0x12DFC2, 0x12DFC4, 0x12DFD0, 0x12DFD2,
    0x12DFD4, 0x12DFDA, 0x12DFE0, 0x12DFF0, 0x12DFFE,
    DISPATCH_FN, DISPATCH_RET, WAIT3_FN, L4_FN, BIG_FN,
    BIG_PROLOGUE, FIFTH_FN, GATE_PC, INDIRECT_CALL, SUBMIT,
    NOOP_HANDLER, 0x12E56A,
)


# ---------------------------------------------------------------- 位与地址
def sx(v, bits):
    """按 ``bits`` 位补码解释一个无符号立即数。"""
    m = 1 << (bits - 1)
    return ((v & ((1 << bits) - 1)) ^ m) - m


def rel9_target(pc, rel9):
    """``bra/bne/beq rel9``：8 位有符号、单位 2 字节、基址 pc+2。"""
    return (pc + 2 + sx(rel9, 8) * 2) & 0xFFFFFFFF


def rel12_target(pc, rel12):
    """``call rel12``：符号位 bit10（与 ``fr_emu.py`` 一致）。"""
    return (pc + 2 + sx(rel12, 11) * 2) & 0xFFFFFFFF


def svc_width(raw, bits):
    """按立即数宽度解码服务号（规矩 48）。"""
    return sx(raw, bits)


def is_low8_artifact(raw, bits=32):
    """``ldi:32`` 的低 8 位是否会被 ``Sweep.calls`` 误报成另一个服务号。"""
    return bits == 32 and (raw & 0xFF) != svc_width(raw, bits)


def desc_ptr(idx):
    """``0x12E26A`` 起的地址算式：描述符表基址 + 索引 × 0x10。"""
    return DESC_TABLE + (idx & 0xFF) * DESC_STRIDE


def in_table(idx):
    """索引是否落在 70 条描述符表内（早退 A 的边界）。"""
    return 0 <= (idx & 0xFF) < DESC_COUNT


# ---------------------------------------------------------------- 返回判据
def early_path(idx_byte, valid_byte):
    """``0x12E1EC`` 走哪条路径：越界 / 队列空 / 正常。"""
    if not in_table(idx_byte):
        return "early_A"
    if (valid_byte & 0xFF) == 0:
        return "early_B"
    return "normal"


def return4(path):
    """``0x12E1EC`` 的返回 r4：只有正常路径给 1。"""
    return 1 if path == "normal" else 0


def level_advanced(r4):
    """``cmp r4,#0`` + ``bne``：非零才进下一级。"""
    return r4 != 0


def path_hits(cov):
    """按覆盖里的特征 PC 判定实际走过的返回路径（规矩 58）。"""
    a = sum(1 for p in EARLY_A if p in cov)
    b = sum(1 for p in EARLY_B if p in cov)
    n = sum(1 for p in NORMAL if p in cov)
    verdict = ("early_A" if a == len(EARLY_A) else
               "early_B" if b == len(EARLY_B) else
               "normal" if n >= 5 else "other")
    return {"early_A": a, "early_B": b, "normal": n,
            "max": (len(EARLY_A), len(EARLY_B), len(NORMAL)),
            "verdict": verdict}


def chain_targets(levels=LEVELS):
    """五级链全部 call 目标（四级 + 第五个 call）。"""
    return list(LEVEL_TARGETS) + [FIFTH_FN]


def failure_targets():
    """四级失败路径统一调 ``0x12E48E`` 序言。"""
    return [FAILURE_PROLOGUE] * len(LEVELS)


# ---------------------------------------------------------------- 函数边界
def function_bounds(rows):
    """按 ``stm1``/``enter`` 起点与 ``ret`` 终点拆真实函数边界（规矩 59）。

    ``rows`` 是 ``(pc, mnemonic)`` 序列；返回按起点排序的
    ``[(start, first_ret_or_end), ...]``。
    """
    starts = [p for p, mn in rows if mn in ("stm1", "enter")]
    dedup = []
    for p in starts:
        if not dedup or p - dedup[-1] > 0x40:
            dedup.append(p)
    rets = [p for p, mn in rows if mn in ("ret", "ret:d")]
    out = []
    end = (rows[-1][0] + 2) if rows else 0
    for i, s in enumerate(dedup):
        nxt = dedup[i + 1] if i + 1 < len(dedup) else end
        r = [x for x in rets if s <= x < nxt]
        out.append((s, (r[0] if r else nxt)))
    return out


def enclosing_mismatch(bounds, enclosing_map):
    """哪些真实函数起点被 ``enclosing()`` 错误归并到别的起点下。"""
    return [(s, e, enclosing_map.get(s))
            for s, e in bounds
            if enclosing_map.get(s) not in (None, s)]


# ---------------------------------------------------------------- 反事实
def equal_shape(groups):
    """等形校验：预热 / 服务次数 / 中断次数 / 后续步数逐项一致。"""
    keys = ("boot", "svc", "irqs", "post")
    out = {}
    for k in keys:
        out[k] = sorted({tuple(g.get(k, [])) for g in groups})
        if len(out[k]) != 1:
            return {"ok": False, "keys": out}
    return {"ok": True, "keys": out}


def window_same(a, b):
    """窗口自证：逐项相同 + PC 覆盖对称差集 0 个。"""
    diff = sorted(k for k in set(a) | set(b)
                  if k not in ("cov", "key") and a.get(k) != b.get(k))
    cov_diff = a.get("cov", set()) ^ b.get("cov", set())
    key_diff = set(a.get("key", [])) ^ set(b.get("key", []))
    return {"same": not diff and not cov_diff and not key_diff,
            "diff": diff, "cov_diff": len(cov_diff),
            "key_diff": sorted(key_diff)}


def chain_verdict(path, l2_hit, desc_ptr_value, gate_hit, indirect_hit,
                  handler_hit, state_zero=True):
    """综合判据：跨过 L2 ≠ 显示链通了。"""
    return {
        "path": path,
        "l2_crossed": l2_hit,
        "desc_ptr_filled": desc_ptr_value != 0,
        "desc_ptr": desc_ptr_value,
        "gate_hit": gate_hit,
        "indirect_hit": indirect_hit,
        "handler_hit": handler_hit,
        "state_zero": state_zero,
        "closed": all((l2_hit, desc_ptr_value != 0, gate_hit,
                       indirect_hit, handler_hit, not state_zero)),
    }


def three_layer(pcs, cov, isr_range=None):
    """存在性 / 执行性 / 触发性三层判据（规矩 50）。"""
    hit = [p for p in pcs if p in cov]
    inside = ([p for p in pcs if isr_range and isr_range[0] <= p < isr_range[1]]
              if isr_range else [])
    return {"points": len(pcs), "hit": len(hit),
            "unique_zero_hit": len(pcs) > 0 and not hit,
            "in_isr": len(inside), "in_isr_all": len(inside) == len(pcs)}
