# -*- coding: utf-8 -*-
"""V76：提交函数的真实身份、`0x41` 等待的置位源、门字节与 8 槽的填充源。

承接 KI#294 第一条。全部是**纯函数**：不读 ROM、不碰机器、不改状态，
只把 V76 的静态判据与反事实口径写成可复算的形式。动态数字由
``probe/exp_submit_v76.py`` 实测后填进 ``results/frbox/submit76.txt``。

这一版要更正三处口径：

* **V63 把 ``0x12E6F8`` 叫「公共状态查询函数」不准确**。函数体是
  ``wai_sem(17)`` → 判槽位 → ``call:d 0x124F5A`` 入队 →
  ``set_flg(3,0x08)`` → ``sig_sem(17)``，即 **sem 17 保护的队列生产者**
  （V38 的 ``bqueue.py`` 已这样定性）。312 个加载点跨全 ROM，收敛态
  0 命中不是「没有调用者」，是调用点都在未执行路径上。
* **门字节 ``0x9F58D08F`` 不是等真机填的**（规矩 61）。全 ROM 有
  两个精确写点：``0x12ECF4`` 写 0、``0x12F180`` 写 1。
* **``0x41`` 是第五道 ``twai_flg`` 的 waiptn，不是 flgptn**。V75 拿它去套
  L3 的 ``(pat&0x74)==0x10`` 并说「不满足判据」是跨级串味（规矩 56 的
  延续）。真正的置位源：全 ROM 唯一 ``set_flg(3,0x40)`` 在 ``0x12FEAC``，
  属于 **vec 0x2D 的 ISR ``0x12FE84``**（向量表倒序，规矩 60）；
  另有 3 点 ``set_flg(3,0x01)``。等待前 ``clr_flg(3,0xBF)`` 先清 0x40。

两个状态字 / 8 槽的静态可定址写点是 0，但 ``stores_unknown`` 有
224489 条 —— **只能报「静态可定址写点 0」，不能报「固件没有写点」**。
8 槽 ``0x9F59F7C8`` 由 ``0x1758EC`` 起的 24 轮开机清零循环覆盖，
收敛态全 0 是固件自己清零的结果，不是「等真机填」。

边界：全部 r4 注入 / 写索引字节 / ``raise_irq`` / ``kernel_call`` 都是
反事实补全。不能报显示链通了 / 出画面 / 系统跑起来了 / 真机如此 /
「补一次 ``set_flg`` 就唤醒成功」。
"""

# ---------------------------------------------------------------- 地址常量
SUBMIT = 0x0012E6F8
SUBMIT_END = 0x0012E752
SUBMIT_REFS = 312            # refs_to / callers_strict（ldi:32 加载点）
SUBMIT_EDGES = 302           # Sweep.edges（call:d 指令点）
SUBMIT_REL12 = 5             # call rel12 指令点
LDI32_LEN = 6                # 两类 PC 相差的字节数（规矩 54）

SUBMIT_SVC_PCS = (0x0012E712, 0x0012E738, 0x0012E748)
SUBMIT_SVCS = (-53, -48, -55)          # wai_sem / set_flg / sig_sem
SUBMIT_SVC_NAMES = ("wai_sem", "set_flg", "sig_sem")
INSERT_FN = 0x00124F5A       # 0x12E728 call:d @r12 的目标
SEM_ID = 17
SEM_POOL = 0x9F58D508        # 0x12E704 装载的槽基址
SLOT_STRIDE = 12             # 0x12E6FE ldi:8 #12 / mul
SUBMIT_SET_BITS = 0x08       # 0x12E732 ldi:8 #8 -> set_flg(3,8)
FLAG_ID = 3

# 门字节：V25 起叫「主循环门字节」，非 0 = 干活分支
GATE_BYTE = 0x9F58D08F
# (写点 PC, 写入值, 真实函数起点, 真实函数 ret)
GATE_WRITES = (
    (0x0012ECF4, 0, 0x0012ECE2, 0x0012ED04),
    (0x0012F180, 1, 0x0012F158, 0x0012F1AA),
)
GATE_LOADS = 19              # ldi:32 #GATE_BYTE 的直接加载点数

# 第五道等待
WAIT41_A = 0x0012E5AA
WAIT41_B = 0x0012E5EA
WAIT41_IMM_A = 0x0012E59E
WAIT41_IMM_B = 0x0012E5DE
WAIT41_PTN = 0x41
TWAI_SVC_32 = -170           # ldi:32 #0xFFFFFF56 按 32 位读
TWAI_SVC_LOW8 = 86           # 低 8 位假象（规矩 48）
CLR_BF = 0xBF                # clr_flg(3,0xBF) 清 0x40
CLR_BF_PCS = (0x0012E592, 0x0012E5D2)
SET_40_PC = 0x0012FEAC       # 全 ROM 唯一 set_flg(3,0x40)
SET_01_PCS = (0x0012E0EC, 0x0012E51A, 0x0012FE1C)
FLAG3_SETTERS = 44
MASK74, EXPECT10 = 0x74, 0x10   # L3 的检查常量，不属于第五道等待

# 向量表（规矩 60：倒序）
TBR = 0x000EFC00
VEC_2D = 0x2D
ISR_2D = 0x0012FE84
ISR_2D_END = 0x0012FEB4
VEC_20, ISR_20 = 0x20, 0x0012FE54
VEC_1B, ISR_1B = 0x1B, 0x0012FDB4

# 四个填充源
WORD_A = 0x9F5A0284
WORD_B = 0x9F59EF50
SLOTS8 = 0x9F59F7C8
DESC_PTR = 0x9F58D858
STORES_UNKNOWN = 224489

# 8 槽：4 个直接加载点与开机清零循环
SLOT_LOADS = (0x001758EC, 0x00175930, 0x001759AC, 0x00175A20)
SLOT_CLEAR_FN = 0x001758EC
SLOT_CLEAR_ST = 0x001758FA   # bgt:d 延迟槽里的 st r1,@(r13,r0)
SLOT_CLEAR_LEN = 96          # ldi:8 #96,r13
SLOT_STORE_WIDTH = 4         # st 是 4 字节
SLOT_COUNT = 8

# 设备策略（与 V65–V75 逐字相同）
BOOT_STEPS = 191520
VEC_TICK = 0x2B
RUN_STEPS = 500000
POST_STEPS = 200000
WINDOW_STEPS = 1000000

# 置位反事实：(标签, 补的位, 是否外部写门字节, 期望新增 PC 是否 >392)
BIT_GROUPS = (
    ("R0", 0x40, False),
    ("R1", 0x01, False),
    ("R2", 0x41, False),
    ("R3", 0x10, False),
    ("R4", None, True),
)


# ---------------------------------------------------------------- 纯函数
def sx(v, bits):
    m = 1 << (bits - 1)
    return (v ^ m) - m if v & m else v


def rel9_target(pc, rel9):
    return (pc + 2 + sx(rel9 & 0xFF, 8) * 2) & 0xFFFFFFFF


def svc32(imm32):
    """服务号按 32 位补码读（0xFFFFFF56 -> -170）。"""
    return sx(imm32 & 0xFFFFFFFF, 32)


def svc_low8(imm32):
    """取低 8 位无符号 —— 规矩 48 的假象值。"""
    return imm32 & 0xFF


def svc_width_mismatch(imm32):
    """-> (32 位真值, 低 8 位假象, 是否不一致)。"""
    a, b = svc32(imm32), svc_low8(imm32)
    return (a, b, a != b)


def submit_slot_addr(idx, pool=SEM_POOL, stride=SLOT_STRIDE):
    """``0x12E6FE..0x12E70A``：r0 = idx*12；r8 = pool + r0。"""
    return (pool + idx * stride) & 0xFFFFFFFF


def submit_services():
    """函数体三个 ``int #0x40`` 的服务号（按 PC 升序）。"""
    return tuple(zip(SUBMIT_SVC_PCS, SUBMIT_SVCS, SUBMIT_SVC_NAMES))


def submit_is_producer(svcs=SUBMIT_SVCS):
    """有拿锁 + 放锁 + 置旗标才算队列生产者，不是「查询函数」。"""
    return (-53 in svcs) and (-55 in svcs) and (-48 in svcs)


def call_site_relations(refs, edges, rel12, width=LDI32_LEN):
    """规矩 54：加载点 / call:d 点 / rel12 点是三套 PC 口径，不能相减。"""
    return {
        "refs": refs, "edges": edges, "rel12": rel12,
        "gap": width, "sum": refs + edges + rel12,
        "distinct_kinds": 3,
    }


def gate_write_values(writes=GATE_WRITES):
    """-> {写入值: 写点 PC}。"""
    return {v: pc for pc, v, _s, _e in writes}


def gate_write_bounds(writes=GATE_WRITES):
    """-> [(起点, ret)]，按 ret 拆的真实函数边界（规矩 59）。"""
    return [(s, e) for _p, _v, s, e in writes]


def firmware_writes_gate(writes=GATE_WRITES):
    """规矩 61：有固件写点就不得说「等真机填」。"""
    return len(writes) > 0


def clear_mask(clrptn, width=7):
    """``clr_flg`` 清的是 clrptn 里为 0 的位：``~clrptn & 0x7F``。

    0x89 -> 0x76、0x8F -> 0x70、0xBF -> 0x40。
    """
    return (~clrptn) & ((1 << width) - 1)


def orw_match(flgptn, wait_ptn=WAIT41_PTN):
    """TWF_ORW：位域里任一命中即唤醒。"""
    return (flgptn & wait_ptn) != 0


def andw_match(flgptn, wait_ptn=WAIT41_PTN):
    return (flgptn & wait_ptn) == wait_ptn


def wait_setters(bits):
    """能置上第五道等待位域的静态点（只按 bits 精确匹配）。"""
    if bits == 0x40:
        return (SET_40_PC,)
    if bits == 0x01:
        return SET_01_PCS
    return ()


def wait41_setter_count():
    return 1 + len(SET_01_PCS)


def vec_slot(vec, tbr=TBR):
    """规矩 60：``slot(n) = TBR + (255-n)*4``，倒序。"""
    return tbr + (255 - (vec & 0xFF)) * 4


def vec_of_slot(slot, tbr=TBR):
    d = slot - tbr
    if d < 0 or d > 0x3FC or d % 4:
        return None
    return 255 - d // 4


def isr_owner(pc, bounds):
    """按 (start, end) 列表判归属；返回 (start, end) 或 None。"""
    for s, e in bounds:
        if s <= pc <= e:
            return (s, e)
    return None


def cross_level_misread(wait_ptn, mask=MASK74, expect=EXPECT10):
    """V75 把 waiptn 当 flgptn 套 L3 检查常量 —— 判是否属于跨级串味。

    ``0x41 & 0x74 = 0x40 != 0x10`` 只能说明「拿错了对象」，
    不能用来断言第五道等待「不满足判据」（规矩 56）。
    """
    return (wait_ptn & mask) != expect


def level_check_verdict(flgptn, mask=MASK74, expect=EXPECT10):
    """L3 自己的判据，只对 flgptn 用。"""
    return (flgptn & mask) == expect


def three_layer(exists, converged_hits, in_isr):
    """存在性 / 执行性 / 触发性三层必须一起报（V68）。"""
    return {"exists": bool(exists), "converged_hits": converged_hits,
            "in_isr": bool(in_isr),
            "all": bool(exists) and converged_hits > 0 and bool(in_isr)}


def writers_verdict(exact_writers, unknown=STORES_UNKNOWN, loads=0):
    """静态可定址写点为 0 时的正确措辞：不能断言固件没有写点。"""
    return {"exact": len(exact_writers), "loads": loads,
            "unknown": unknown,
            "may_assert_absent": len(exact_writers) == 0 and unknown == 0,
            "verdict": ("no-addressable-writer" if not exact_writers
                        else "firmware-writer-exists")}


def slot_clear_rounds(length=SLOT_CLEAR_LEN, width=SLOT_STORE_WIDTH):
    """``r13`` 从 96 每轮 -4，``st`` 在 bgt:d 延迟槽无条件执行。"""
    return length // width


def slot_zero_explained(clear_hit, slots_all_zero, rounds=None):
    """8 槽全 0 是否可由固件开机清零解释（不要求外部事件）。"""
    r = slot_clear_rounds() if rounds is None else rounds
    return bool(clear_hit) and bool(slots_all_zero) and r * SLOT_STORE_WIDTH >= SLOT_CLEAR_LEN


def submit_trigger(refs_hit, edges_hit, submit_hit, total_refs=SUBMIT_REFS):
    """提交函数 0 命中的归因：调用点在不在、有没有跑到。"""
    return {"refs_hit": refs_hit, "edges_hit": edges_hit,
            "submit_hit": bool(submit_hit),
            "callers_exist": total_refs > 0,
            "verdict": ("callsites-not-executed"
                        if total_refs > 0 and not submit_hit
                        else "executed")}


def equal_shape(shapes):
    """等形对照：各组的 (BOOT, POST, WINDOW) 必须逐字相同。"""
    return len(set(shapes)) == 1


def attribution(groups, flag):
    """按某布尔字段挑出命中的组标签。"""
    return sorted(k for k, v in groups.items() if v.get(flag))


def chain_verdict(handler_hit, submit_hit, words_nonzero, slots_nonzero,
                  gate_written):
    return bool(handler_hit and submit_hit and words_nonzero
                and slots_nonzero and gate_written)


def window_same(cov_a, cov_b):
    """两个窗口覆盖的对称差集大小。"""
    return len(set(cov_a) ^ set(cov_b))
