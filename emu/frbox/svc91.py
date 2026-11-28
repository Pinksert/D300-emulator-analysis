# -*- coding: utf-8 -*-
"""V91 设施：8 组服务号别名的同 half 静态绑定普查 + 余下 2 组动态等形补全。

承接 KI#355 的可证部分：V89 只对 6/8 组别名做了动态等形，余下
``0x040DE4`` / ``0x041DE4`` 两组仅静态普查，因此 V89 明确声明
「不据此宣布 8 组全程不分流」。本版把这两组补做动态等形，
使 8 组**全部**有 1200 步等长窗口的逐条对照；并新增一项静态发现：

1. **同 half 静态绑定（8/8）**。分发表里每组的多个服务号落在不同的
   slot，但存着**同一个 half**（G1=0x12 / … / 0x040DE4=0x784 /
   0x041DE4=0x1784 / …），``target_of(half)`` 因此折叠到同一入口。
   这是「两组等形机轨迹相同」的**分发表层结构成因**，不是运行时按号分流。
2. **余下 2 组动态等形补全（本版新跑 4 台）**。改 TRAP 处 r12 为组内
   两个服务号（反事实，只改寄存器），1200 步 PC 逐条全序列相同；服务号槽
   值确实不同（0x7FFFFF14/0x00000040、0x7FFFFF09/0x00000016）。
   判据 ``slot_differs_sequence_identical_no_dispatch_in_window``。
   G7 末 PC 0x0004DC / G8 末 PC 0x9F5963B8 落在非代码区，轨迹只用于同组对照。
3. **窗口有界口径**。V89 的 6 组结果从 ``svc89.txt`` 引用（本版不重跑那 12 台），
   加本版 4 台 = 8 组全部有动态对照。结论只能说「8 组在统一 1200 步窗口内
   逐条全序列相同 + 分发表层同 half 绑定」，**不得**说「8 组全程不分流」/
   「语义已解出」（禁语，延续 90/92）。

新增规矩 94：宣布「全组别名窗口内不分流」前必须每组都做等长窗口动态等形
（不许只做部分组就宣布全组结论，V89 只做 6/8），并先做同 half 静态绑定普查
区分「分发表层折叠」与「运行时按号分流」；逐条序列相同只证该窗口内未被按号
分流，不等于语义相同、不等于全程不分流（延续 78/86/90/91/92）。

**边界**：改 TRAP 处 r12 是反事实补全（模型动作，只改寄存器），不是固件行为、
不是真机器件事件；非代码区末 PC 只用于同组对照。本版结的是**分发表结构与
窗口内可见性**，不是器件事件语义，**链未闭合**。
不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 / 所有服务语义已解出 /
真机已复现 −85 / 「补一次值就等于真机事件」/「8 对别名语义已解出」/
「8 组全程不分流」/「服务号槽没人读」。
"""

M32 = 0xFFFFFFFF

# ---------------------------------------------------------------- 收敛锚点（V65–V90 逐字相同）
BOOT_STEP = 483841
TRAP = 0x040668
HOOK2 = 0x041BFC
AFTER_LONG = 600
PAIR_WINDOW = 1200
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC
CALL_R12 = 0x0406B2
SLOT_SVC = 0x000083E0
ALIAS_D12_PC = 0x041DE0

# ---------------------------------------------------------------- 分发表全表口径
ROM_DECODED = 3543659
TBL_ZERO = 0x040660
TABLE_SVC_COUNT = 100
TABLE_ENTRY_COUNT = 89
TABLE_ALIAS_PAIRS = 8
ALIAS_SVC_COUNT = 19

# 每组：(入口, 服务号元组, 全组共同的 half)，8 组
# half 由只读 ROM slot 实测（侦查 D），组内各号 half 全同
ALIAS_ROWS = (
    (0x040672, (-254, -214), 0x0012),
    (0x040AA0, (28, 56), 0x0440),
    (0x040D6E, (20, 52, 72), 0x070E),
    (0x040DE4, (-236, 64), 0x0784),
    (0x041D6E, (4, 67), 0x170E),
    (0x041DE4, (-247, 22), 0x1784),
    (0x046B80, (114, 125), 0x6520),
    (0x046E60, (-230, 74, 93, 104), 0x6800),
)

# V97：每组各号 slot **实测 half 的多元组**（svc91.txt A2 段只读 ROM 真值）。
# ALIAS_ROWS 第三列是「全组共同 half」这一个标量，承载不了组内比较；
# V91 的判据对单元素 (half,) 做 same_half 恒为真，无法证伪。本常量补上真多元组。
ALIAS_HALF_TUPLES = (
    (0x0012, 0x0012),
    (0x0440, 0x0440),
    (0x070E, 0x070E, 0x070E),
    (0x0784, 0x0784),
    (0x170E, 0x170E),
    (0x1784, 0x1784),
    (0x6520, 0x6520),
    (0x6800, 0x6800, 0x6800, 0x6800),
)
GROUP_COUNT = 8
SAME_HALF_COUNT = 8
HALF_BIND_VERDICT = "static_same_half_binding_dispatch_folds_to_one_entry"

# ---------------------------------------------------------------- V89 已跑 6 组（引用，不重跑）
# tag -> (入口, 两号, 服务号槽 a/b, 逐条全序列相同)
V89_DYNAMIC = (
    ("G1", 0x040AA0, (28, 56),       (0x0000001C, 0x00000038), True),
    ("G2", 0x041D6E, (4, 67),        (0x00000004, 0x00000043), True),
    ("G3", 0x040672, (-254, -214),   (0x7FFFFF02, 0x7FFFFF2A), True),
    ("G4", 0x046E60, (-230, 74),     (0x7FFFFF1A, 0x0000004A), True),
    ("G5", 0x040D6E, (20, 52),       (0x00000014, 0x00000034), True),
    ("G6", 0x046B80, (114, 125),     (0x00000072, 0x0000007D), True),
)
V89_GROUP_COUNT = 6
V89_GROUP_MACHINES = 12

# ---------------------------------------------------------------- V91 新跑 2 组（本版 4 台）
# 侦查 C 实测；末 PC 落非代码区，只用于同组对照
NEW_DYNAMIC = (
    ("G7", 0x040DE4, (-236, 64),
     (0x7FFFFF14, 0x00000040), 0x041A48, 0x0004DC, 1010, 0, 0, True),
    ("G8", 0x041DE4, (-247, 22),
     (0x7FFFFF09, 0x00000016), 0x041DE4, 0x9F5963B8, 1200, 0, 0, True),
)
# 字段: tag, entry, svcs, svc_slots(a,b), call_target, end_pc, npc,
#       d12_hits_a, d12_hits_b, seq_same
NEW_GROUP_COUNT = 2
NEW_GROUP_MACHINES = 4
NEW_NONCODE_ENDPCS = (0x0004DC, 0x9F5963B8)

# ---------------------------------------------------------------- 合计
TOTAL_DYNAMIC_GROUPS = 8             # V89 6 + V91 2
ALL_SEQ_SAME = True
CONVERGE_MACHINE_COUNT = 1
MACHINE_TOTAL = 5                    # 4 新等形机 + 1 标准收敛态机（本版实跑）

VERDICT = "all_eight_alias_groups_same_half_binding_equal_shape_in_1200_step_window"
VERDICT_OPEN = "open"

# ---------------------------------------------------------------- 入口结构（侦查 B）
ENTRY_R12_READ = {
    0x040DE4: True,   # 0x040DE4 ld r14, [r12] 入口即读 r12
    0x041DE4: False,  # 0x041DE4 ldub disp8=128 r0，入口不读 r12
}
ENTRY_D12_HITS = {0x040DE4: 0, 0x041DE4: 1}   # 0x041DE0 ld disp10=12 在 0x041DE4 邻域


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def extsh(h):
    return sx(h & 0xFFFF, 16)


def sx8(v):
    return sx(v & 0xFF, 8)


def disp10_addr(r14, disp10):
    """``ld/st disp10`` 有效地址：``r14 + sx8(disp10)*4``（fr_emu 口径）。"""
    return (r14 + sx8(disp10) * 4) & M32


def slot_of(svc32):
    """真内核 32 位有符号服务号表项槽（规矩 76）。"""
    return (TBL_ZERO + ((svc32 << 1) & M32)) & M32


def target_of(half):
    return (TBL_ZERO + extsh(half)) & M32


def same_half(half_tuple):
    """组内各号是否落同一个 half。"""
    return len(set(half_tuple)) == 1


def half_binding_verdict(rows=ALIAS_HALF_TUPLES):
    """全 8 组是否都同 half 绑定；**V97 改为可证伪判据**。

    V91 原实现 ``all(same_half(r[2:]) for r in rows)`` 里 ``r[2:]`` 是单元素
    元组 ``(half,)``，``len(set(...))==1`` 恒为真：构造 8 行 half 全不同仍返回
    HALF_BIND_VERDICT。这不等于 V91 结论错（结论另有静态分发表与逐组等形动态证据），
    但这条判据本身不构成证据。

    现要求：行数 == GROUP_COUNT，且每行都是**长度 >= 2 的组内各号实测 half
    多元组**且组内去重只剩一个值；任一组不满足即 ``unresolved``。单元素元组
    因此被显式排除，负对照见 test_v97。V91 结论（8/8 同 half）不变。
    """
    if len(rows) != GROUP_COUNT:
        return "unresolved"
    for halves in rows:
        if len(halves) < 2 or not same_half(halves):
            return "unresolved"
    return HALF_BIND_VERDICT


def svc_saved_vs_dispatch(slot_a, slot_b, seq_same, window):
    """规矩 92：服务号被保存 ≠ 被读取并分流。"""
    if slot_a == slot_b:
        return "service_number_not_changed"
    if seq_same and window > 0:
        return "slot_differs_sequence_identical_no_dispatch_in_window"
    return "unresolved"


def full_dynamic_verdict(seq_flags, window=PAIR_WINDOW):
    """8 组是否全部在等长窗口内逐条相同（引用 V89 6 + 本版 2）。"""
    if len(seq_flags) != TOTAL_DYNAMIC_GROUPS:
        return "incomplete_group_coverage"
    if window <= 0:
        return "no_window_declared"
    if all(seq_flags):
        return "all_eight_groups_equal_shape_in_window"
    return "some_groups_differ"


def source_verdict(bind_ok, dynamic_ok, converge_ok, event_located=False):
    """综合判据：链未闭合（真机器件事件语义仍未定位）。"""
    if not (bind_ok and dynamic_ok and converge_ok):
        return VERDICT_OPEN
    if event_located:
        return "event_semantics_located"
    return VERDICT
