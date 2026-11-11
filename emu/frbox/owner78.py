# -*- coding: utf-8 -*-
"""V78：函数边界口径、静态直接调用边形态、字段写与提交调用点的任务归属。

承接 KI#302。本模块**全部是纯函数**，不读 ROM、不跑机器；ROM 扫描与
动态反事实在 ``probe/exp_owner_v78.py`` 里做。

三件被此版更正的事：

1. 规矩 59 要收紧：``enclosing()`` 不准，但「见 ret 就切」在大函数上
   同样不准 —— 全 ROM 只有 5544 条 ``ret`` 却有 23613 条 ``enter``，
   V64 已证 ``0x171184..0x1728B6`` 的 43 条 ``ret`` 是**同一函数的多出口**。
2. 规矩 54 要量化：``Sweep.edges`` 只收目标可定的 ``call:d``。全 ROM
   另有 9160 条 ``call rel12``、4823 条 ``call @r``，用 edges 判
   「没有调用者」必然漏。
3. 8 槽清零 / 写回函数的调用者其实就在 task7 主体里，是 ``call rel12``。

设备策略与 V65–V77 逐字相同（``blive.boot(0)`` → ``step(191520)`` →
单次 ``raise_irq(0x2B)`` → ``run(500000)``）。
"""
from __future__ import division
from bisect import bisect_right

# ---------------------------------------------------------------- 全 ROM 普查
ROM_BASE = 0x00040000
ROM_SIZE = 7340032
ENTER_TOTAL = 23613           # `enter` 指令总数 = Sweep.funcs
RET_TOTAL = 5544              # ret / reti / ret:d
CALL_D_TOTAL = 25669
CALL_TOTAL = 13983            # `call`（rel12 + @r）
JMP_TOTAL = 3559
EDGES_DECIDABLE = 15435       # Sweep.edges 调用点
CALL_D_UNDECIDED = 10234      # call:d 但目标寄存器的值静态不可定
CALL_REL12 = 9160
CALL_REG = 4823               # call @rN
SWEEP_EDGES = 15435
REL12_TARGETS = 8672          # call rel12 的不同目标函数入口
EDGES_TARGETS = 1576          # Sweep.edges 的不同目标
TARGET_INTERSECT = 79         # 两种口径目标的交集
ONLY_REL12_TARGETS = 8593     # 只被 rel12 调到，edges 完全看不见
ONLY_EDGES_TARGETS = 1497

# ---------------------------------------------------------------- 两种边界口径
FIELD_PCS = 142               # V77 归约出的字段写（31 + 109 + 2）
FIELD_FN_BY_RET = 36
FIELD_FN_BY_ENTER = 18
SUBMIT_REFS = 312             # ldi:32 加载点
SUBMIT_EDGES = 302            # call:d 指令点
SUBMIT_REL12 = 5              # V76：call rel12 指令点（另一套 PC 口径）
SUBMIT_FN_BY_RET = 244
SUBMIT_FN_BY_ENTER = 168

# 四种入边口径下的「有调用者」函数数（p3 实测）
FIELD_FN_WITH_STRICT = 9
FIELD_FN_WITH_EDGE = 1
FIELD_FN_WITH_REL12 = 0
FIELD_FN_NO_CALLER = 9
SUBMIT_FN_WITH_STRICT = 50
SUBMIT_FN_WITH_EDGE = 0
SUBMIT_FN_WITH_REL12 = 0
SUBMIT_FN_NO_CALLER = 118

# ---------------------------------------------------------------- 巨型函数
BIG_FN_ENTER = 0x00171184
BIG_FN_NEXT_ENTER = 0x001728B6
BIG_FN_SPAN = 5938            # 下一个 enter 减本 enter（字节）
BIG_FN_FIELDS = 76            # 142 条字段写中落在此函数的
BIG_FN_INSNS = 2364           # V64 实测指令数
BIG_FN_RETS = 43              # V64 实测该函数内的 ret 数

# ---------------------------------------------------------------- task7 / 8 槽
TASK_TABLE = 0x00054C00
TASK_COUNT = 29
TASK7_TID = 7
TASK7_ENTRY = 0x00175192
TASK7_ENTER = 0x00175196
TASK7_END = 0x001759A6        # 下一个函数区终点（enter 0x1759A6 之前）
WAIT_FN = 0x00175876          # 等待 / 端口轮询函数（含 tslp_tsk(10) 的 0x17588E）
SLOT_CLEAR_FN = WAIT_FN       # 兼容旧名；注意它**不是**清零循环函数
SLOT_CLEAR_CALL = 0x00175234
SLOT_CLEAR_CALL2 = 0x00175418
SLOT_CLEAR_REL12 = 800
SLOT_LOOP_FN = 0x001758EC     # 真正的 24 轮清零循环 + set_flg(12,1)，到 0x175906 ret
SLOT_LOOP_PC = 0x001758FA     # 24 轮清零的 st（V77 归约点）
SLOT_LOOP_END = 0x00175906
SLOT_LOOP_IN_EDGES = 0        # call:d / ldi:32 严格 / call rel12 / jmp rel12 全 0
SLOT_CLEAR_PC = SLOT_LOOP_PC  # 清零 st 属于 SLOT_LOOP_FN，不属于 SLOT_CLEAR_FN
SLOT_WB_FN = 0x00175908       # 写回函数序言（call rel12 目标）
SLOT_WB_ENTER = 0x0017590C
SLOT_WB_CALL = 0x0017574C
SLOT_WB_REL12 = 221
SLOT_WB_PC = 0x00175958
SLOT_BASE = 0x9F59F7C8
BIG_FN_STRICT_CALLER = 0x001734EE
#: 反事实里 kernel_call(sta_tsk,7) 的 R12 回读值。与 snd_msg 服务号同值，
#: 不可直接解释成「任务已激活」——判激活要读 TCB state（规矩 55/56）。
STA_TSK_R12 = -63

# ---------------------------------------------------------------- 服务号 / task7 等待
SVC_TSLP_TSK = -37            # 0xDB；waitstat67.SVC_NAMES / abservice.SVC_NAMES[0xDB]
TSLP_NAME = "tslp_tsk"
TSLP_TOTAL_CALLS = 137        # B 段 Sweep.calls 实测
TSLP_A_TABLE_CALLS = 0        # abservice 说的「0 处」是 A 段分发表项，不是 B 段
TSLP_HIT_PC = 0x0017588E
TSLP_TIMEOUT = 10             # r4：限时睡眠 tick 数
TSLP_PORT = 0x50000000        # r7：端口轮询窗口
T7_TCB = 0x68000244
T7_STATE = 0x04
T7_WHY = -37
T7_LEFT_TICKS = 9             # TCB+0x14：剩余 tick
CALL_REG_TOTAL = 4823         # call @r 目标不可定，不能据此排除间接入边

# ---------------------------------------------------------------- 根归属
VEC_NONDEFAULT = 47           # 256 槽里非 0xFFFFFFFF 的入口数
ROOT_FUNCS = 7                # 302 调用点所在函数里，能归到任务根的
ROOT_EDGES = 12               # 这 7 个函数覆盖的调用点数
ROOT_TIDS = (7, 2, 3, 4, 9, 16, 17)
FIELD_ROOT_FUNCS = 2          # 18 个字段写函数里，归到任务根的
FIELD_ROOT_TIDS = (7, 19)

# ---------------------------------------------------------------- 设备策略
BOOT_STEPS = 191520
VEC_TICK = 0x2B
VEC_2D = 0x2D
RUN_STEPS = 500000
POST_STEPS = 200000
WINDOW_STEPS = 1000000
TBR = 0x000EFC00


# ============================================================ 算术 / 地址
def sx(v, bits):
    """有符号扩展；rel12 是 11 位、rel9 是 8 位、m4 是 4 位。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def rel12_target(pc, rel12):
    """`call rel12` 目标，与 fr_emu.py:777 逐字一致。"""
    return (pc + 2 + sx(rel12, 11) * 2) & 0xFFFFFFFF


def rel9_target(pc, rel9):
    """`b?? rel9` 目标，8 位有符号（硬规矩 57）。"""
    return (pc + 2 + sx(rel9, 8) * 2) & 0xFFFFFFFF


def vec_slot(vec, tbr=TBR):
    """向量表倒序（硬规矩 60）。"""
    return tbr + (255 - vec) * 4


def vec_of_slot(slot, tbr=TBR):
    return 255 - (slot - tbr) // 4


# ============================================================ 函数边界
def owner_by_ends(pc, ret_ends, base=ROM_BASE):
    """口径 A：按 ret/reti 之后的字节切函数起点（V76/V77 的写法）。"""
    i = bisect_right(ret_ends, pc - 1)
    return ret_ends[i - 1] if i else base


def owner_by_enter(pc, enter_pcs, base=ROM_BASE):
    """口径 B：按 enter 序言切函数起点；中间的 ret 是多出口，不切。"""
    i = bisect_right(enter_pcs, pc)
    return enter_pcs[i - 1] if i else base


def fn_span(lo, next_enter):
    return max(0, next_enter - lo)


def prologue_candidates(enter, prologue_insns=()):
    """函数入口的候选地址集合：enter 本身，以及前面 2/4 字节的 stm1 序言。

    ``bstatic.refs_to`` 的 window=8 就是为这个偏差准备的（硬规矩 26）。
    """
    out = {enter}
    p = enter
    for pc, mn, size in prologue_insns:
        if pc + size == p and mn.startswith('stm'):
            p = pc
            out.add(p)
    return out


def split_ratio(n_ret, n_enter):
    """两种口径的函数数之比；>1 说明按 ret 切是过度拆分。"""
    return n_ret / n_enter if n_enter else 0.0


def over_split(n_ret, n_enter):
    return n_ret > n_enter


def boundary_verdict(ret_fns, enter_fns, big_rets=BIG_FN_RETS,
                     big_insns=BIG_FN_INSNS):
    """规矩 59 收紧后的判据：不能只信一种边界。"""
    return {
        'ret_fns': ret_fns,
        'enter_fns': enter_fns,
        'ratio': split_ratio(ret_fns, enter_fns),
        'over_split': over_split(ret_fns, enter_fns),
        'big_multi_exit': big_rets > 1,
        'big_insns': big_insns,
    }


# ============================================================ 调用形态
def call_census(call_d=SWEEP_EDGES, edges=EDGES_DECIDABLE,
                rel12=CALL_REL12, reg=CALL_REG, jmp=JMP_TOTAL):
    return {
        'call_d': call_d,
        'edges': edges,
        'call_d_undecided': call_d - edges,
        'call_rel12': rel12,
        'call_reg': reg,
        'jmp': jmp,
    }


def undecided_share(call_d=CALL_D_TOTAL, edges=EDGES_DECIDABLE):
    """call:d 里目标静态不可定的比例。"""
    return (call_d - edges) / call_d if call_d else 0.0


def non_edge_calls(rel12=CALL_REL12, reg=CALL_REG, jmp=JMP_TOTAL):
    """完全不在 Sweep.edges 口径内的调用/跳转条数。"""
    return rel12 + reg + jmp


def edge_gap(call_d=CALL_D_TOTAL, edges=EDGES_DECIDABLE):
    return call_d - edges


def caller_verdict(edge_in, strict, rel12, reg):
    """规矩 68：报「没有调用者」前把四种入边都问一遍。"""
    kinds = {'call:d 可定目标': edge_in, 'ldi:32 严格调用者': strict,
             'call rel12': rel12, 'call @r（目标不可定）': reg}
    return {
        'kinds': kinds,
        'any': any(kinds.values()),
        'edge_only': bool(edge_in) and not (strict or rel12 or reg),
        'missed_by_edges': bool(strict or rel12) and not edge_in,
    }


def rel12_only_targets(edges_targets, rel12_targets):
    """只被 call rel12 调到、Sweep.edges 完全看不见的入口数。"""
    return len(set(rel12_targets) - set(edges_targets))


def submit_relations(refs=SUBMIT_REFS, edges=SUBMIT_EDGES,
                     rel12=SUBMIT_REL12):
    """规矩 54：三套 PC 口径，不能相加或相减。"""
    return {'refs': refs, 'edges': edges, 'rel12': rel12,
            'sum': refs + edges + rel12}


# ============================================================ 归属
def is_root(fn, task_entries, vec_map, tol=8):
    """该函数是否是任务入口（含序言回退）或向量表入口。"""
    hits = []
    for tid, entry in task_entries:
        if abs(fn - entry) <= tol:
            hits.append('task%d' % tid)
    for vec, entry in vec_map.items():
        if entry == fn:
            hits.append('vec0x%02X' % vec)
    return hits


def roots_of(fns, task_entries, vec_map, tol=8):
    return {f: is_root(f, task_entries, vec_map, tol) for f in fns}


def rooted_functions(roots):
    return sorted(f for f, r in roots.items() if r)


def root_coverage(fn_counts, roots):
    """能归到任务/ISR 根的函数数，以及它们覆盖的调用点数。"""
    fs = rooted_functions(roots)
    return len(fs), sum(fn_counts.get(f, 0) for f in fs)


def big_concentration(fields_in_big=BIG_FN_FIELDS, total=FIELD_PCS):
    return fields_in_big / total if total else 0.0


def task7_region(pc, lo=TASK7_ENTRY, hi=TASK7_END):
    return lo <= pc < hi


def slot_owner_verdict(clear_hit, wb_hit, task7_hit):
    """只报「静态调用边存在」，不报「已经执行过」。"""
    return {
        'clear_called': clear_hit,
        'writeback_called': wb_hit,
        'same_task': task7_hit,
        'both_in_task7': clear_hit and wb_hit and task7_hit,
    }


def trigger_verdict(root_edges=ROOT_EDGES, total_edges=SUBMIT_EDGES,
                    root_fns=ROOT_FUNCS, total_fns=SUBMIT_FN_BY_ENTER):
    """静态能定到任务根的只有少数，其余只能报「入口未定」。"""
    return {
        'root_edges': root_edges, 'total_edges': total_edges,
        'root_fns': root_fns, 'total_fns': total_fns,
        'undetermined_edges': total_edges - root_edges,
        'share': root_edges / total_edges if total_edges else 0.0,
    }


# ============================================================ 反事实判据
def equal_shape(shapes):
    """所有组除唯一差异外前缀相同（V51/V76/V77 口径）。

    用 repr 归一化：调用方若把 dict / list 混进形状字段，
    set() 会炸 ``unhashable type``（V78 G0 栽过一次）。
    """
    norms = [tuple(repr(x) for x in s[:3]) for s in shapes]
    return len(set(norms)) <= 1


def attribution(groups, flag):
    """归因分离：命中集合与 flag 一致，且至少有一组不命中。"""
    vals = [bool(g[flag]) for g in groups]
    return any(vals) and not all(vals)


def coverage_hits(pcs, cov):
    return len(set(pcs) & set(cov))


def coverage_delta(cov_a, cov_b):
    return len(set(cov_b) - set(cov_a))


def window_same(cov_a, cov_b):
    return len(set(cov_a) ^ set(cov_b)) == 0


def chain_verdict(field_hits, slot_hits, submit_hits, task7_hit):
    """只报静态可达 + 收敛态命中，不报显示链通了。"""
    return {
        'field_hits': field_hits,
        'slot_hits': slot_hits,
        'submit_hits': submit_hits,
        'task7_hit': task7_hit,
        'closed': bool(field_hits and slot_hits and submit_hits),
    }


def field_fn_table(rows):
    """[(fn, count)] -> {fn: count}，供探针与单测共用。"""
    out = {}
    for fn, cnt in rows:
        out[fn] = out.get(fn, 0) + cnt
    return out


def tslp_verdict(total_b, table_a, timeout, name=TSLP_NAME):
    """规矩 69：A 段分发表项数与 B 段调用点数不是同一把尺子。"""
    return {'name': name, 'b_segment_calls': total_b,
            'a_table_entries': table_a, 'timeout_ticks': timeout,
            'same_ruler': False,
            'claim': 'A 段 0 处与 B 段 %d 处不矛盾' % total_b}


def loop_entry_verdict(call_d=0, strict=0, rel12=0, jmp=0,
                       undecidable=CALL_REG_TOTAL):
    """四套可定址入边全 0 时，只能报「无可定址入边」。"""
    addressable = call_d + strict + rel12 + jmp
    return {'addressable': addressable, 'undecidable': undecidable,
            'any_addressable': addressable > 0,
            'claim': ('no_addressable_in_edges' if addressable == 0
                      else 'has_addressable_in_edges'),
            'can_say_nobody_calls': False}


def t7_wait_verdict(why, left, state, svc=SVC_TSLP_TSK,
                    name=TSLP_NAME):
    """TCB+0x20 等待原因与 TCB+0x14 剩余 tick 互证。"""
    return {'why': why, 'left_ticks': left, 'state': state,
            'is_tslp': why == svc, 'name': name if why == svc else 'svc(%s)' % why,
            'tick_driven': why == svc and left > 0}


def hard_rules_78():
    """V78 新增三条硬规矩（全文见 FRBOX-REF.md 附录 P）。"""
    return (67, 68, 69)


def model_actions():
    """本版反事实里的人工动作，全部是模型动作，不是固件行为。"""
    return ('raise_irq(0x2B)', 'raise_irq(0x2D)', 'raise_irq(0x20) + r4=5',
            'kernel_call(set_flg)', 'kernel_call(sta_tsk, 7)',
            'kernel_call(get_tid)')


def forbidden_claims():
    return ('显示链通了', '出画面', '系统跑起来了', '真机如此',
            '补一次 set_flg 就唤醒成功', '某函数从未跑过',
            '任务 7 已经填充了 8 槽',
            '清零循环函数没人调用', 'tslp_tsk 静态调用点 0 处',
            '任务 7 的 8 槽已清零')
