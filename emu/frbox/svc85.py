# -*- coding: utf-8 -*-
"""frbox.svc85 —— 短窗口把时机差读成路径差（V85，纯函数）。

承接 KNOWN_ISSUES KI#331 最后一条：V84 记了一句「EARLY_B / EARLY_AB 清掉
B 位但门1 关时 PC 数 166→260、末 r4 0xA→0x1BDBAE，−85 出口未命中，
具体去向本版未追」。V85 把这句追到底，结论是 **V84 的「副作用」不是另一条
执行路径，而是同一次任务换手被提前了 203 步**；260 步的观察窗口太短，
把「时机差」截断成了「路径差」。

分叉门：B 单元自己的 and/bne
----------------------------
``0x041B28..0x041B88`` 这段共享尾部里，``0x041B32 ld disp10=2,r8`` 在
r14=0x68000968 时读的正是 **B = 0x68000970**（V84 定的内核对象位标志字）。
``0x041B44 mov r9,r8`` → ``0x041B46 and r9,r6`` → ``0x041B48 bne rel9=20``：
B=0x80000000 时 ``B & want = 0x80000000`` 非零 ⇒ 跳 ``0x041B72``，走
``0x041B82 ret:d`` + 延迟槽 ``0x041B84 ldi:8 #0,r12`` 的**正常返回 0 尾**
（V83 已证这条尾归 −46 号服务）；B=0 时 and 结果 0 ⇒ 落到 ``0x041B4A``，
走另一条尾部。

另一条尾部通向任务换手，不是错误路径
------------------------------------
``0x041B4A..0x041B70``（写 TCB 字段、``0x041B60 call:d rel12=428`` 到
``0x041EBA`` 队列操作、``0x041ECA call:d rel12=1249`` 到 ``0x04188E``
就绪位图更新、``0x041B6E st`` 写 −1）→ 派发器 ``0x040D32`` →
``0x040E18`` 读 ``0x68000004`` 就绪位图 + ``0x040E20 srch1`` 选最高优先级
→ ``0x040E50 stm1/stm0`` 保存上下文、``0x040E9C ldm0/ldm1`` 恢复 →
``0x040EB6 reti``。**CUR_TCB 从 0x68000244（任务 7，入口 0x175192）
换成 0x6800029C（任务 9，入口 0x1352EA）**；``reti`` 后第一条就是任务 9
的入口 ``0x1352EA``。

末 r4 不是状态字，是 ROM 常量立即数
-----------------------------------
任务 9 跑 ``0x1352F6 call 0x1378EA`` 进表下载函数，``0x1378FA ldi:32
#0x001BDBAE,r4`` 把 r4 装成 **0x001BDBAE**，再 ``0x13790A call rel12=45``
到 ``0x137966`` 的 16 位拷贝环。V46 的 ``dev_ctrl40.py`` 早已把
``0x001BDBAE`` 定性为 ``0x40`` 器件批量下载窗口（``0x40010000`` /
``+0x10200``）**表 0 的 ROM 源地址**：256 项线性斜坡，首值 0、末值
0x3FC0、相邻差恒 0x40，逐字节等于 ROM。所以 V84 看到的「末 r4 变了」
是**换到另一个任务后装了个表源地址**，与 −85 出口、器件事件都无关。

归因分离：是值，不是写动作；只清 B 就够
----------------------------------------
4 台独立等形机器（boot=483841、挂钩 PC 集合相同、后续 260 步全同），
只往 B 写 4 字节，不强制 PC、不注入中断、不改寄存器：

=================  ==================  ================  ==================
组                  早补(陷阱口)        晚补(HOOK2)       结果
=================  ==================  ================  ==================
BASE                —                   —                 B=0x80000000，正常返回
EARLY_B             B=0                —                 B=0，提前挂起
SAME_B              B=0x80000000       —                 与 BASE 逐项相同
LATE_B              —                   B=0               与 EARLY_B 完全相同
=================  ==================  ================  ==================

``SAME_B`` ≡ ``BASE`` ⇒ 不是「写内存这个动作」造成的；
``EARLY_B`` ≠ ``SAME_B`` ⇒ 是**写进去的值**；
``LATE_B`` ≡ ``EARLY_B`` ⇒ 与写入时机无关（B 在这段窗口内没有被改写）。
另外 V84 的 ``LATE_AB`` 要同时清 A 才返 −85，V85 证明**只清 B、不动 A
就足以触发换手**，两件事不可合并。

自我证伪：260 步短窗口把时机差读成路径差
------------------------------------------
把窗口从 260 拉到 600 步（2 台独立机器）：

* BASE 第一次 ``reti`` 在 **step 444**，EARLY_B 在 **step 241**，提前 **203** 步；
* 两次 ``reti`` 之后 **80 步 PC 序列逐项完全相同**；
* 600 步末 PC 同为 ``0x137966``、末 CUR 同为任务 9、
  就绪位图同为 ``0x036E0000``；
* BASE 由固件自己在 **step195@0x041AFE** 把 B 清成 0，EARLY_B 在陷阱入口
  就清了 —— 203 步差来自这里。标准收敛态 coverage 对 ``0x1378FA`` /
  ``0x137968`` / ``0x1352EA`` / ``0x040EB6`` 四个 PC 全命中 ⇒
  这条任务 9 的表下载路径**本来就跑过**，不是新功能、不是唤醒。

结果层相同 ≠ 原因层相同（不许合并）
------------------------------------
任务 7 最后都停在 ``state=0x04`` 等待，但等待原因不同：
BASE ``TCB+0x20=0xFFDB0000``（−37 = tslp_tsk）、``+0x14=0x0000000A``
剩余 tick；EARLY_B ``+0x20=0xFF560000``（−170）、``+0x14=0xA2A05E1C``
等待位模式。BASE 在 600 步里还发了 2 次 ``int #0x40``
（step137@0x175414 / step233@0x17588E），EARLY_B 0 次。
⇒ 只能报「换手结果和换手后轨迹相同」，不能报「同一等待事件」。

工具口径自我证伪
----------------
``frbox/bus.py:73`` 注明 ``all_writes`` **只记 RAM**。用它去统计
``0x40010000`` 器件下载窗口的写笔数会得 0，那是判据口径错，不是证据错；
判「这条路径本来跑没跑过」要用 coverage / 指令命中。V85 草稿 C4 曾因此
得到 False，已更正。

硬规矩 82（本版新增）
---------------------
报「两条不同执行路径」前先证**观察窗口足够长**：短窗口截断会把同一事件的
**时机差**读成路径差。等形对照必须同窗口长度，并把窗口拉到两组轨迹的
汇合点之后；「末 PC / 去重 PC 数 / 末 r4 不同」在短窗口下不构成路径差异的
证据。同时分清 ``pcs[N-1]``（窗口内最后一个被记录的 PC）与 ``cpu.pc``
（跑满 N 步之后的当前 PC），两者差一条指令，不许混用。

硬规矩 83（本版新增）
---------------------
报「同一事件」前分清**结果层**与**原因层**：reti 后轨迹相同、末 CUR 相同、
就绪位图相同，只说明换手结果相同；``TCB+0x14`` / ``+0x20`` 随等待原因变
（延续规矩 56 / 71），服务调用次数也可能不同。另外判据要选对工具：
``bus.all_writes`` 只记 RAM，器件窗口用 coverage / 指令命中；
不许把「高频复用槽」叫成专用状态字（延续规矩 80）。

反事实边界与共同禁语
---------------------
所有往 ``0x68000970`` 写值都是**反事实补全**，不是固件行为、不是真机
器件事件；任务换手是仿真台里既有代码的自然执行，不等于真机发生过。
链仍未闭合。不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
「所有服务语义已解出」/「真机已复现 −85」/「−85 出口属于 −170 号函数体」/
「补一次 r13 就等于真机事件」/「写一次内存单元就等于器件事件」/
「A 是器件状态字」/「清 B 位唤醒了任务 9」/「EARLY_B 走了一条新路径」。

仍开放：真机让 ``vec 0x18`` 周期性发生的器件/事件（KI#191）、让 −170 号
服务在真机走 −85 出口的器件事件语义、两状态字偏移 0 bit3 置位源、
``vec 0x2D`` 硬件事件、317 个提交调用点非根部分的触发任务、
A 的状态如何决定「返 −85」与「挂起换手」两条大分支的优先级、
任务 9 表下载完成后是否回到任务 7。
"""

# ---------------------------------------------------------------- 静态口径
BOOT_STEP = 483841
TRAP = 0x040668
RET_PC = 0x0406B4
HOOK1 = 0x040668
HOOK2 = 0x041BFC
AFTER = 260
LONG_AFTER = 600
ALIGN = 80
MAIN_SVC_IMM32 = 0xFFFFFF56
MAIN_SVC_32 = -170

# ---------------------------------------------------------------- B 单元与分叉门
SRC_B = 0x68000970
SRC_B_SET = 0x80000000
SRC_B_CLR = 0x00000000
R14_AT_GATE = 0x68000968
LD_B_GATE = 0x041B32
LD_B_DISP = 2
AND_R9_R6 = 0x041B46
BNE_GATE = 0x041B48
TAKE_JUMP = 0x041B72
TAKE_FALL = 0x041B4A
NORMAL_RET_SLOT = 0x041B84
B_CLR_PC = 0x041AFE
B_CLR_STEP_BASE = 195
BASE_AND = 0x80000000
EARLY_AND = 0x00000000

# ---------------------------------------------------------------- 换手链
DISPATCH = 0x040D32
SCHED_SRCH = 0x040E18
RETI = 0x040EB6
RETI_STEP_BASE = 444
RETI_STEP_EARLY = 241
ADVANCE = 203
QUEUE_FN = 0x041EBA
READYMAP_FN = 0x04188E
SCHED_CALL = 0x040E46

# ---------------------------------------------------------------- 任务身份
CUR = 0x68000000
READY = 0x68000004
TCB7 = 0x68000244
TCB9 = 0x6800029C
TASK7_ID = 7
TASK9_ID = 9
TASK7_ENTRY = 0x175192
TASK9_ENTRY = 0x1352EA
READY_ANCHOR = 0x076E0000
READY_RETI = 0x036E0000
TCB7_STATE = 0x04
TCB9_STATE = 0x00

# ---------------------------------------------------------------- 等待态（结果层/原因层）
BASE_TCB7_P14 = 0x0000000A
BASE_TCB7_P20 = 0xFFDB0000
EARLY_TCB7_P14 = 0xA2A05E1C
EARLY_TCB7_P20 = 0xFF560000
BASE_WAIT_SVC = -37
EARLY_WAIT_SVC = -170
BASE_INTS_600 = 2
EARLY_INTS_600 = 0
BASE_INT_STEPS = (137, 233)
BASE_INT_PCS = (0x175414, 0x17588E)

# ---------------------------------------------------------------- 表下载 / ROM 常量
CONST_LDI = 0x1378FA
CONST_IMM = 0x001BDBAE
TABLE0_SRC = 0x001BDBAE
TABLE0_ITEMS = 256
TABLE0_FIRST = 0x0000
TABLE0_LAST = 0x3FC0
TABLE0_DELTA = 0x40
DOWNLOAD_WIN = 0x40010000
DOWNLOAD_OFF0 = 0x10200
COPY_LOOP = 0x137968
TABLE_FN = 0x1378EA
CALL_TO_COPY = 0x13790A

# ---------------------------------------------------------------- 窗口口径
SHORT_BASE_NPC = 166
SHORT_SIDE_NPC = 260
LONG_BASE_NPC = 343
LONG_SIDE_NPC = 264
SHORT_BASE_END_IN = 0x04069E       # pcs[259]，窗口内最后一条被记录的 PC
SHORT_SIDE_END_IN = 0x13796A
SHORT_BASE_END_AFTER = 0x0406A0    # 跑满 260 步之后的 cpu.pc
SHORT_SIDE_END_AFTER = 0x13796C
LONG_END_PC = 0x137966
BASE_LONG_R4 = 0x001BDBD6
EARLY_LONG_R4 = 0x001BDC10

# ---------------------------------------------------------------- 标准收敛态
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
CONVERGE_TAIL = 0x0407DC
CONVERGE_COV_PCS = (0x1378FA, 0x137968, 0x1352EA, 0x040EB6)

# ---------------------------------------------------------------- 反事实组
CF_GROUPS = (
    ("BASE",     (),                              ()),
    ("EARLY_B",  ((SRC_B, SRC_B_CLR),),          ()),
    ("SAME_B",   ((SRC_B, SRC_B_SET),),          ()),
    ("LATE_B",   (),                             ((SRC_B, SRC_B_CLR),)),
)
VALUE_TAGS = ("EARLY_B", "LATE_B")
MACHINE_COUNT = 7            # 4 短窗口 + 2 长窗口 + 1 标准收敛态
VERDICT = "short_window_artifact_resolved_early_context_switch"


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    """按 bits 位补码读无符号值。"""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def disp10_addr(r14, disp10):
    """FR 的 ld/st disp10 形态：r14 + sx8(disp10)*4（fr_emu.py:660）。"""
    return (r14 + sx(disp10, 8) * 4) & 0xFFFFFFFF


def reason_code(p20):
    """TCB+0x20 高半是等待服务号，按 16 位补码读。"""
    return sx((p20 >> 16) & 0xFFFF, 16)


def b_gate(b_val, want_mask=0xFFFFFFFF):
    """0x041B48 bne 的判据：B & want 非零则跳。"""
    return "jump" if (b_val & want_mask) else "fall"


def gate_target(kind):
    return {("jump"): TAKE_JUMP, "fall": TAKE_FALL}.get(kind, "unknown")


def equal_shape(rows):
    """等形对照：boot / hooks / post / window 四项必须全同。"""
    if not rows:
        return False
    return all(len(set(r.get(k) for r in rows)) == 1
               for k in ("boot", "hooks", "post", "window"))


def patch_value_verdict(base, same_val, clear_val):
    """归因分离：写同值 ≡ BASE、写清位 ≠ BASE ⇒ 差异来自值而非写动作。"""
    same_ok = all(same_val.get(k) == base.get(k)
                  for k in ("npc", "end_pc", "end_r4", "hit_switch"))
    clear_diff = clear_val.get("hit_switch") != base.get("hit_switch")
    if same_ok and clear_diff:
        return "value_not_write_action"
    if not same_ok:
        return "write_action_side_effect"
    return "no_difference"


def window_end_kind(in_window, after_window):
    """分清窗口内末 PC 与跑满后的当前 PC（差一条指令）。"""
    if after_window == in_window:
        return "same"
    if after_window == in_window + 2:
        return "one_sequential_instruction"
    return "other"


def advance_steps(reti_base, reti_early):
    return reti_base - reti_early


def path_identity_verdict(align_ok, same_end_pc, same_ready, same_cur):
    """结果层：换手后轨迹、末 PC、就绪位图、当前 TCB 是否全同。"""
    return "same_path_shifted" if all((align_ok, same_end_pc,
                                       same_ready, same_cur)) else "different_path"


def wait_reason_verdict(p20_base, p20_early, p14_base, p14_early):
    """原因层：等待服务号 / 等待参数是否相同。"""
    rc_b, rc_e = reason_code(p20_base), reason_code(p20_early)
    if rc_b == rc_e and p14_base == p14_early:
        return "same_reason"
    return "different_reason"


def table0_shape(first, last, items, delta):
    """表 0 线性斜坡判据：末值 = 首值 + (项数-1)*差。"""
    return last == first + (items - 1) * delta


def short_window_verdict(short_ends, long_ends, align_ok, advance):
    """短窗口截断判据：短时不同、长时汇合、轨迹对齐、有明确提前步数。"""
    short_diff = short_ends[0] != short_ends[1]
    long_same = long_ends[0] == long_ends[1]
    if short_diff and long_same and align_ok and advance > 0:
        return "window_artifact_timing_shift"
    if short_diff and not long_same:
        return "genuine_path_difference"
    return "inconclusive"


def tool_scope_verdict(tool, target_region):
    """bus.all_writes 只记 RAM；器件窗口要用 coverage。"""
    if tool == "all_writes" and target_region != "ram":
        return "wrong_tool"
    if tool == "coverage":
        return "ok"
    return "ok" if target_region == "ram" else "unknown"


def verdict(window_ok, value_ok, path_ok, reason_diff, cov_ok,
            event_located=False):
    if not (window_ok and value_ok and path_ok and reason_diff and cov_ok):
        return "open"
    if event_located:
        return "event_semantics_located"
    return VERDICT
