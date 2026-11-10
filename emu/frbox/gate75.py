# -*- coding: utf-8 -*-
"""V75：L4 恒过、r10 门默认开、处理函数首次命中（纯函数，只读 ROM / 常量）。

承接 V74 / KI#290 第三条。V74 的 L4 路径已让 L2 派发 / L2 bne /
L3 等待命中、D858 由固件自身算出，但停在等待位 0x74。V75 补第三次
唤醒后首次走完整条到处理函数：

* **L4 ``0x12E412`` 不是条件门，它是恒过的**。函数末尾
  ``0x12E48A ret:d`` 的延迟槽是 ``0x12E48C ldi:8 #1,r4``，
  无论前面走哪条分支，返回时 r4 都被置 1；任务 5 主体
  ``0x12DFF4 cmp r4,#0 / 0x12DFF6 bne 0x12DFFE`` 因此必然跳到
  第五个 call。V72 把任务 5 叫「四级串联状态机」容易让人误以为
  第四级也有判据 —— 它没有。
* **跨 L3 才是真条件**。L3 ``0x12E3F6 wai_flg(3,0x74,2,100)`` 与
  V71 第二道等待逐字同构，通过判据仍是 ``(pat&0x74)==0x10``；
  且 ``0x12E34A clr_flg(3,0x8F)`` 先清 0x70。两轮唤醒只够到 L3，
  第三轮补 0x10 才能跨到 L4。六组等形反事实 N0–N5：
  只有 N3 / N5（三级唤醒）命中 L4 / 第五个 call / 门 / 间接调用。
* **V69 的「门」``0x12E6E0 cmp r10,#0`` 默认是开的**。
  ``0x12E674`` 函数体内 r10 只有两个写点：
  ``0x12E69E ldi:8 #1,r10`` 落在 ``0x12E69C beq:d`` 的**延迟槽**里
  （无条件执行），``0x12E6BA ldi:8 #0,r10`` 在另一条分支上。
  N3 单步时序实测 r10=1 一直保持到 ``0x12E6F0``，
  ``0x12E6E2 beq`` 不跳 ⇒ 间接调用执行（规矩 55 / 57）。
* **处理函数首次命中 1/44**。``0x12E6EE ld @(r0,r13)`` 从
  ``D858+0x0C`` 读处理函数指针；N3 的 D858=``0x001BC070``
  ⇒ 指针 ``0x0012E806``，``0x12E6F0 call @r0`` 下一条 PC 真的
  进了 ``0x12E806``。不同 r4 索引等形反事实 Q0–Q5：
  Q1/Q5 → 空槽 ``0x12E752``（裸 ret）、Q2 → ``0x12E806``、
  Q3 → ``0x12E856``，Q0（r4=0）与 Q4（r4=0x46 越界）连 L4 都不到。
  V69 报「27/70 槽指向空槽位」在动态里得到印证。
* **链仍未闭合**：提交函数 ``0x12E6F8`` 0 命中，两个状态字
  ``0x9F5A0284`` / ``0x9F59EF50``、门字节 ``0x9F58D08F``、
  8 槽 ``0x9F59F7C8`` 全 0；处理函数 ``0x12E806`` 的函数体
  ``[0x12E806,0x12E854)`` 只做位域比较与
  ``set_flg(10,0x200000)``，不写这些结构。末态任务 5 挂在
  等待位 ``0x41``（第五道等待，来自 ``0x12E5AA`` /
  ``0x12E5EA`` 的 ``ldi:8 #0x41``）；四 / 五级唤醒不再新增 PC
  （392 不变），不是唤醒次数不够。

**规矩 63（本版新增）**：报「某一级 / 某个门是条件」前先查
``ret:d`` / ``beq:d`` 的延迟槽有没有无条件给返回值或标志赋值。
L4 的 r4=1 与 ``0x12E674`` 的 r10=1 都在延迟槽里，
不是判据结果（规矩 57 / 58 的推广）。

**规矩 48 的又一组实例**：``0x12E5A4`` / ``0x12E5E4`` /
``0x12E3F0`` 的 ``ldi:32 #0xFFFFFF56`` 按 32 位补码是 −170，
``Sweep.calls`` 取低 8 位报 ``svc=86`` 是假象；
``0x41`` 等待位来自 ``0x12E59E`` / ``0x12E5DE`` 的
``ldi:8 #0x41``。

**规矩 54 的口径实例**：V63 记 ``0x12E6F8`` 有「312 调用点」，
那是 ``refs_to`` / ``callers_strict`` 口径（``ldi:32`` 加载函数
地址的指令 PC）；``Sweep.edges`` 的 302 是 ``call:d`` 指令 PC，
另有 ``call rel12`` 5 点。两者 PC 相差 6 字节（``ldi:32`` 长度），
点集交集为 0 属正常，不可混用或相减。

所有 r4 寄存器注入 / 写索引字节 / ``raise_irq`` / ``kernel_call``
都是外部反事实补全，不是固件行为；不能报显示链通了 / 出画面 /
系统跑起来了 / 真机如此 /「补一次 set_flg 就唤醒成功」。
"""

# ---------------------------------------------------------------- 地址常量
TASK5_FN = 0x0012DFB2
LOOP_HEAD = 0x0012DFBA
WAIT1_FN = 0x0012E0F8
WAIT2_FN = 0x0012E162       # L1
DISPATCH_FN = 0x0012E1EC    # L2
WAIT3_FN = 0x0012E326       # L3
L4_FN = 0x0012E412          # L4
BIG_PROLOGUE = 0x0012E48E   # 每级失败路径的 call:d 目标
FIFTH_CALL = 0x0012DFFE
FIFTH_FN = 0x0012E674
GATE_PC = 0x0012E6E0
INDIRECT_CALL = 0x0012E6F0
SUBMIT = 0x0012E6F8
NOOP_HANDLER = 0x0012E752

# L4 / 门 / 延迟槽的关键 PC
L4_RET = 0x0012E48A
L4_RET_SLOT = 0x0012E48C    # ret:d 延迟槽 ldi:8 #1,r4
L4_BNE = 0x0012DFF6
L3_WAIT = 0x0012E3F6
L3_CLEAR = 0x0012E34A
R10_ONE = 0x0012E69E        # beq:d 延迟槽 ldi:8 #1,r10
R10_ZERO = 0x0012E6BA
R10_BEQD = 0x0012E69C
GATE_BEQ = 0x0012E6E2
DESC_LOAD = 0x0012E6EE
WAIT41_A = 0x0012E5AA
WAIT41_B = 0x0012E5EA
WAIT41_IMM_A = 0x0012E59E
WAIT41_IMM_B = 0x0012E5DE

DESC_TABLE = 0x001BC020
DESC_COUNT = 70
DESC_STRIDE = 0x10
DESC_HANDLER_OFF = 0x0C
DESC_PTR = 0x9F58D858
DA82 = 0x9F58DA82
DA83 = 0x9F58DA83
DA84 = 0x9F58DA84

# 处理函数
HANDLER_NOOP = 0x0012E752   # 裸 ret 空槽
HANDLER_A = 0x0012E806      # Q2 命中
HANDLER_B = 0x0012E856      # Q3 命中
HANDLER_A_END = 0x0012E854
HANDLER_B_END = 0x0012E8E0
N_HANDLERS = 44
NOOP_SLOTS = (0, 1, 2, 10, 13, 15, 16, 19, 27, 28, 34, 37, 40, 46,
              47, 53, 54, 55, 56, 58, 61, 63, 64, 65, 67, 68, 69)

WORD_A = 0x9F5A0284
WORD_B = 0x9F59EF50
SLOTS8 = 0x9F59F7C8
GATE_BYTE = 0x9F58D08F

FLAG_ID = 3
BIT10 = 0x10
MASK74 = 0x74
WANTPTN_1 = 9
WAIT41 = 0x41
SET_FLG = -48
CLR_FLG = -47
WAI_FLG = -46
GET_TID = -24
SVC_WAI_32 = -170           # ldi:32 #0xFFFFFF56 按宽度读
SVC_WAI_LOW8 = 86           # 规矩 48 的低 8 位假象

# 提交函数三种调用边口径（规矩 54）
SUBMIT_REFS = 312           # refs_to / callers_strict（ldi:32 加载点）
SUBMIT_EDGES = 302          # Sweep.edges（call:d 指令点）
SUBMIT_REL12 = 5            # call rel12 指令点
LDI32_LEN = 6               # 两种 PC 口径相差的字节数

# 六组唤醒级数反事实：(标签, r4事件, 唤醒级数, 外部写D858, L4命中)
LEVEL_GROUPS = (
    ("N0", False, 0, False, False),
    ("N1", True, 1, False, False),
    ("N2", True, 2, False, False),
    ("N3", True, 3, False, True),
    ("N4", False, 3, False, False),
    ("N5", True, 3, True, True),
)
# 六组索引反事实：(标签, r4值, 期望 DA83, 期望 D858, 期望处理函数)
INDEX_GROUPS = (
    ("Q0", 0x00, 0x00, 0x00000000, 0),
    ("Q1", 0x01, 0x01, 0x001BC030, HANDLER_NOOP),
    ("Q2", 0x05, 0x05, 0x001BC070, HANDLER_A),
    ("Q3", 0x06, 0x06, 0x001BC080, HANDLER_B),
    ("Q4", 0x46, 0x46, 0x00000000, 0),
    ("Q5", 0x45, 0x45, 0x001BC470, HANDLER_NOOP),
)
# 多级唤醒阴性结果：(级数, 新增PC, 处理函数命中)
EXTRA_AWAKEN = ((3, 392, True), (4, 392, True), (5, 392, True))


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    m = 1 << (bits - 1)
    return (v ^ m) - m if v & m else v


def rel9_target(pc, rel9):
    return pc + 2 + sx(rel9, 9) * 2


def rel12_target(pc, rel12):
    return pc + 2 + sx(rel12, 12) * 2


def desc_ptr(idx, table=DESC_TABLE, stride=DESC_STRIDE):
    """D858 = 表基址 + DA82*0x10（规矩 62：idx 来自 DA82 中转）。"""
    return table + (idx & 0xFF) * stride


def handler_slot(idx):
    """描述符 idx 的 +0x0C 处理函数指针所在地址。"""
    return desc_ptr(idx) + DESC_HANDLER_OFF


def in_table(idx, count=DESC_COUNT):
    return 0 <= (idx & 0xFF) < count


def l4_return_value(slot_value=1):
    """L4 ret:d 延迟槽无条件给 r4=1 ⇒ L4 恒过。"""
    return slot_value


def l4_always_passes(cmp_value=None):
    """第四级是否恒过：只看延迟槽值，不看前面分支。"""
    return l4_return_value() != 0


def mask_check(flgptn, mask=MASK74, expect=BIT10):
    """L3 与 V71 第二道等待同构的判据。"""
    return (flgptn & mask) == expect


def r10_from_delay_slot(beqd_slot_value=1):
    """0x12E69E 在 beq:d 延迟槽里 ⇒ r10 默认 1，门默认开。"""
    return beqd_slot_value


def gate_open(r10):
    """0x12E6E0 cmp r10,#0 / beq 跳过调用；r10!=0 ⇒ 调用执行。"""
    return r10 != 0


def index_to_handler(idx, table_ptrs=None):
    """给 idx 查处理函数（表格由调用方从 ROM 传入，纯函数查表）。"""
    if not in_table(idx) or table_ptrs is None:
        return None
    return table_ptrs.get(idx & 0xFF)


def is_noop_handler(ptr):
    return ptr == HANDLER_NOOP


def level_needed(levels):
    """跨到 L4 至少需要三级唤醒（第三级补 0x10 跨 L3）。"""
    return 3 if levels >= 3 else levels


def reaches_l4(has_r4_event, levels):
    """L4 命中需要 r4 事件 + 至少三级唤醒（等形反事实实测）。"""
    return bool(has_r4_event) and levels >= 3


def extra_awaken_noop(levels):
    """四 / 五级唤醒不再新增 PC（阴性结果）。"""
    return levels >= 3


def three_layer(exists, converged_hits, in_isr):
    return {"exists": exists, "executed": converged_hits,
            "needs_event": bool(exists) and not bool(converged_hits)
            and bool(in_isr)}


def equal_shape(shapes):
    keys = {'boot': {s[0] for s in shapes},
            'post': {s[2] for s in shapes}}
    return {'ok': all(len(v) == 1 for v in keys.values()), 'keys': keys}


def attribution(groups, flag):
    return [t for t, r in groups.items() if r.get(flag)]


def chain_verdict(handler_hit, submit_hit, words_nonzero, slots_nonzero):
    """链闭合需要四者同时成立；V75 只有 handler_hit=True。"""
    return bool(handler_hit and submit_hit and words_nonzero
                and slots_nonzero)


def call_site_relations(refs, edges, rel12, width=LDI32_LEN):
    """规矩 54：三种调用边口径的关系，返回可解释的字典。"""
    union = len(edges | rel12)
    return {
        "refs": len(refs), "edges": len(edges), "rel12": len(rel12),
        "union": union,
        "shift_matches": len({p + width for p in refs} & edges),
        "overlap": len(refs & (edges | rel12)),
        "different_pc_kinds": len(refs & (edges | rel12)) == 0,
    }


def window_same(cov_a, cov_b):
    return len(set(cov_a) ^ set(cov_b))
