# -*- coding: utf-8 -*-
"""V78 正式探针：函数边界口径、静态直接调用边、字段写与提交调用点的任务归属（A–H）。

承接 KI#302。设备策略与 V65–V77 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；归约段是**静态数据流**，不跑机器；反事实段的
raise_irq / r4 注入 / kernel_call 全部标注为模型动作，不是固件行为。
独立机器：H0–H5 六组 + 1M 窗口 + I 段只读取证 = 8 台。
"""
import sys
import os
import collections
from bisect import bisect_right

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, brealos, cursor73 as K,
                   cursor74 as D, wait271 as W, waitstat67 as W67,
                   abservice, dispatch62 as D62, reduce77 as R,
                   submit76 as S, taskchain64 as T, owner78 as O)

BOOT, VEC, RUN, POST, WINDOW = (blive.BOOT_STEPS, 0x2B,
                                O.RUN_STEPS, O.POST_STEPS, O.WINDOW_STEPS)
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
ROMEND = base + len(rom)


def hr(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)
    sys.stdout.flush()


def decode_at(pc):
    ins = fr_emu.TABLE.decode(rom, pc - base)
    if ins is None:
        return None
    mn, bs, v, size, w = ins
    return (pc, mn, bs, dict(v), size)


def dec(lo, hi):
    out, pc = [], lo
    while pc < hi - 1:
        d = decode_at(pc)
        if d is None:
            pc += 2
            continue
        out.append(d)
        pc += d[4]
    return out


def u32(a):
    return int.from_bytes(rom[a - base:a - base + 4], 'big')


# ==================================================================== 全 ROM 扫描
RET_ENDS, RET_PCS, ENTER_PCS = [], [], []
REL12 = collections.defaultdict(list)   # 目标 -> [call rel12 调用点]
JMP_REL12 = collections.defaultdict(list)  # 目标 -> [jmp rel12 调用点]
CALLD_KNOWN = set()
CALLD_ALL, CALL_REG, JMP_CNT = 0, 0, 0
off, n = 0, len(rom)
while off < n - 1:
    if rom[off:off + 2] in bstatic.SKIP2:
        off += 2
        continue
    ins = fr_emu.TABLE.decode(rom, off)
    if ins is None:
        off += 2
        continue
    mn, bs, v, size, w = ins
    pc = base + off
    if mn in ('ret', 'reti', 'ret:d'):
        RET_PCS.append(pc)
        RET_ENDS.append(pc + size)
    elif mn == 'enter':
        ENTER_PCS.append(pc)
    if mn == 'call' and 'rel12' in v:
        REL12[O.rel12_target(pc, v['rel12'])].append(pc)
    elif mn == 'call' and 'ri' in v:
        CALL_REG += 1
    elif mn == 'call:d':
        CALLD_ALL += 1
    elif mn == 'jmp':
        JMP_CNT += 1
        if 'rel12' in v:
            JMP_REL12[O.rel12_target(pc, v['rel12'])].append(pc)
    off += size
RET_ENDS.sort()
ENTER_PCS.sort()
CALLD_KNOWN = set().union(*SW.edges.values()) if SW.edges else set()


def by_ends(pc):
    return O.owner_by_ends(pc, RET_ENDS, base)


def by_enter(pc):
    return O.owner_by_enter(pc, ENTER_PCS, base)


# ==================================================================== V77 归约复现
def fn_start_local(pc, back=0x900):
    lo, last = max(base, pc - back), None
    p = lo
    while p < pc - 1:
        d = decode_at(p)
        if d is None:
            p += 2
            continue
        if d[1] in ('ret', 'reti', 'ret:d'):
            last = p + d[4]
        p += d[4]
    return last if last is not None else lo


def track(lo, hi, max_steps=1200):
    regs = [None] * 16
    out, pc, steps = [], lo, 0
    while pc < hi - 1 and steps < max_steps:
        d = decode_at(pc)
        if d is None:
            pc += 2
            steps += 1
            continue
        _, mn, bs, v, size = d
        ri, rj = v.get('ri'), v.get('rj')
        if mn.startswith('ldi'):
            regs = R.prop_ldi(regs, ri, v.get('i8', v.get('i20', v.get('i32'))))
        elif mn == 'mov':
            regs = R.prop_mov(regs, ri, rj)
        elif mn == 'add':
            regs = R.prop_add(regs, ri, rj)
        elif mn == 'add2':
            regs = R.prop_add2(regs, ri, v.get('m4', 0))
        elif mn in ('st', 'stb', 'sth'):
            a, w = R.reduce_store(bs, v, regs)
            out.append((pc, mn, a, w))
        elif mn in ('ld', 'ldub', 'lduh', 'ldm0', 'ldm1', 'mul', 'div',
                    'movh', 'movl', 'extsb', 'extsh', 'extub', 'extuh'):
            if mn.startswith(('ld', 'mov', 'mul', 'div')) and ri is not None:
                regs[ri] = None
        elif mn in ('call', 'call:d', 'jmp', 'int'):
            for k in (0, 1, 2, 3, 12):
                regs[k] = None
        elif ri is not None and mn not in ('cmp', 'btsth', 'btstl', 'bandh',
                                          'borh', 'beorh', 'bandl', 'borl',
                                          'beorl', 'nop', 'stm0', 'stm1',
                                          'enter', 'leave', 'ret', 'reti'):
            regs[ri] = None
        if mn in ('ret', 'reti', 'ret:d'):
            break
        pc += size
        steps += 1
    return out


TARGETS = (('WORD_A', R.WORD_A, 0x40), ('WORD_B', R.WORD_B, 0x40),
           ('SLOT8', R.SLOT8, 0x60))
INVENTORY = {}
for name, addr, span in TARGETS:
    refs = [p for p, _i, _nx in SW.refs_to(addr, window=0)]
    seen, rows = set(), []
    for pc in refs:
        lo = fn_start_local(pc)
        for spc, mn, a, w in track(lo, min(pc + 0x400, ROMEND - 2)):
            if not R.is_field_write(a, addr, span) or spc in seen:
                continue
            seen.add(spc)
            rows.append((spc, mn, R.offset_of(a, addr), w))
    INVENTORY[name] = sorted(rows)
ALL_FIELD = sorted({p for rows in INVENTORY.values() for p, *_ in rows})
SUBMIT_EDGES = sorted(SW.edges.get(S.SUBMIT, []))
FIELD_FNS_END = collections.Counter(by_ends(p) for p in ALL_FIELD)
FIELD_FNS_ENTER = collections.Counter(by_enter(p) for p in ALL_FIELD)
EDGE_FNS_END = collections.Counter(by_ends(p) for p in SUBMIT_EDGES)
EDGE_FNS_ENTER = collections.Counter(by_enter(p) for p in SUBMIT_EDGES)

# 任务表 / 向量表
RECS = T.task_table(rom, base)
TASK_ENTRIES = [(r.tid, r.entry) for r in RECS]
VEC_MAP = {}
for v in range(256):
    slot = O.vec_slot(v)
    if base <= slot < ROMEND - 3:
        VEC_MAP[v] = u32(slot)
ISR_ENTRIES = {x for x in VEC_MAP.values() if x and x != 0xFFFFFFFF}
ROOTS_A = O.roots_of(FIELD_FNS_ENTER, TASK_ENTRIES, VEC_MAP)
ROOTS_B = O.roots_of(EDGE_FNS_ENTER, TASK_ENTRIES, VEC_MAP)

# ==================================================================== A
hr("A. 静态：全 ROM 函数边界普查 —— 规矩 59 要收紧")
print("A0 ROM [0x%06X,0x%06X) len=%d；Sweep.funcs=%d edges=%d" %
      (base, ROMEND, len(rom), len(SW.funcs), sum(len(v) for v in SW.edges.values())))
print("A1 全 ROM 线性解码：enter=%d  ret/reti/ret:d=%d  call:d=%d  "
      "call=%d（rel12 %d / @r %d）  jmp=%d" %
      (len(ENTER_PCS), len(RET_PCS), CALLD_ALL,
       sum(len(v) for v in REL12.values()) + CALL_REG,
       sum(len(v) for v in REL12.values()), CALL_REG, JMP_CNT))
print("A2 enter 数是 ret 数的 %.2f 倍 ⇒ 大量函数是**多 ret 出口**，"
      "见 ret 就切会把一个函数拆成碎块（V64 已证 0x171184..0x1728B6 "
      "有 1 个 enter / 43 个 ret / 2364 条指令）" %
      O.split_ratio(len(ENTER_PCS), len(RET_PCS)))
print("A3 两种口径：``by_ends`` 取最近 ret 之后的字节，``by_enter`` 取最近 enter；"
      "后者把中间 ret 视作多出口，不新开函数。")

# ==================================================================== B
hr("B. 静态：V77 归约复盘与两种边界下的归属")
print("B0 V77 归约字段写 %d 条（A=%d B=%d 槽=%d）；提交函数 ldi:32 加载点 %d、"
      "call:d 边 %d、call rel12 点 %d（规矩 54 三套 PC 口径）" %
      (len(ALL_FIELD), *(len(INVENTORY[k]) for k in
                         ('WORD_A', 'WORD_B', 'SLOT8')),
       len(SW.refs_to(S.SUBMIT, window=0)), len(SUBMIT_EDGES), S.SUBMIT_REL12))
print("B1 字段写 %d 个 PC：按 ret 边界落在 %d 个函数，按 enter 起点落在 %d 个；"
      "过度拆分比 %.2f" %
      (len(ALL_FIELD), len(FIELD_FNS_END), len(FIELD_FNS_ENTER),
       O.split_ratio(len(FIELD_FNS_END), len(FIELD_FNS_ENTER))))
print("B2 提交调用点 %d 个：按 ret 边界落在 %d 个函数，按 enter 起点落在 %d 个；"
      "过度拆分比 %.2f" %
      (len(SUBMIT_EDGES), len(EDGE_FNS_END), len(EDGE_FNS_ENTER),
       O.split_ratio(len(EDGE_FNS_END), len(EDGE_FNS_ENTER))))
print("B3 结论：V76/V77 用「按 ret 拆边界」做局部归约仍可用（窗口内一致），"
      "但拿它报**函数数量**会高估；规矩 67。")
print("B4 enter 口径字段写函数 top8 = %s" %
      [(hex(f), c) for f, c in FIELD_FNS_ENTER.most_common(8)])

# ==================================================================== C
hr("C. 静态：76 条字段写集中在 V64 那个巨型函数")
BIG = O.BIG_FN_ENTER
j = bisect_right(ENTER_PCS, BIG)
NEXT = ENTER_PCS[j] if j < len(ENTER_PCS) else ROMEND
SPAN = O.fn_span(BIG, NEXT)
BIG_FIELDS = FIELD_FNS_ENTER.get(BIG, 0)
print("C1 巨型函数 enter=0x%06X 下一 enter=0x%06X 跨度=%d B（V64 实测 "
      "%d 条指令 / %d 个 ret）" %
      (BIG, NEXT, SPAN, O.BIG_FN_INSNS, O.BIG_FN_RETS))
print("C2 该函数含 %d / %d 条字段写（%.1f%%）；按 ret 边界这些 PC 会散进 "
      "%d 个「伪函数」" %
      (BIG_FIELDS, len(ALL_FIELD), 100 * O.big_concentration(BIG_FIELDS),
       len({by_ends(p) for p in ALL_FIELD if by_enter(p) == BIG})))
print("C3 巨型函数静态入边：call:d 可定 %d、call rel12 %d、"
      "ldi:32 严格调用者 %s" %
      (len(SW.edges.get(BIG, [])), len(REL12.get(BIG, [])),
       [hex(p) for p, _i, _nx in SW.callers_strict(BIG)]))
print("C4 规矩 67：报函数边界前先分清 `enter` 序言起点与 `ret` 多出口；"
      "报「某函数无调用者」不能只看一种入边形态。")

# ==================================================================== D
hr("D. 静态：调用形态普查 —— 规矩 54 / 68 的量化")
census = O.call_census(CALLD_ALL, len(CALLD_KNOWN),
                       sum(len(v) for v in REL12.values()), CALL_REG, JMP_CNT)
print("D1 %s" % census)
print("D2 call:d 里目标静态不可定 %d / %d（%.2f%%）；"
      "完全不在 Sweep.edges 口径内的调用/跳转 %d 条" %
      (O.edge_gap(CALLD_ALL, len(CALLD_KNOWN)), CALLD_ALL,
       100 * O.undecided_share(CALLD_ALL, len(CALLD_KNOWN)),
       O.non_edge_calls(census['call_rel12'], census['call_reg'], census['jmp'])))
ET = set(SW.edges)
LT = set(REL12)
print("D3 call:d 目标 %d 个、call rel12 目标 %d 个、交集 %d、仅 rel12 %d、"
      "仅 call:d %d ⇒ 只报 call:d 边会漏掉 %d 个函数入口" %
      (len(ET), len(LT), len(ET & LT), len(LT - ET), len(ET - LT),
       O.rel12_only_targets(ET, LT)))
for label, fns in (("18 个字段写函数", FIELD_FNS_ENTER),
                   ("168 个提交函数", EDGE_FNS_ENTER)):
    strict = sum(1 for f in fns if SW.callers_strict(f))
    edge = sum(1 for f in fns if SW.edges.get(f))
    rel = sum(1 for f in fns if REL12.get(f))
    none = sum(1 for f in fns
               if not (SW.callers_strict(f) or SW.edges.get(f) or REL12.get(f)))
    print("D4 %-18s 共 %3d ｜ ldi:32 严格调用者 %3d ｜ call:d 入边 %3d ｜ "
          "call rel12 入边 %3d ｜ 三者都 0 的 %3d" %
          (label, len(fns), strict, edge, rel, none))
print("D5 规矩 68：报「静态直接调用边 0」前先扫 call:d（可定/不可定）、"
      "call rel12、call @r、ldi:32+call 四套口径；Sweep.edges 只是其中一套。")

# ==================================================================== E
hr("E. 静态：三个相邻函数要分开 —— 等待 / 清零循环 / 写回")
print("E0 按 ret 边界与反汇编，task7 尾部是**三个**函数：")
print("   等待函数   [0x%06X,0x1758EC) 含 tslp_tsk(10) 的 0x%06X，末尾 ret"
      % (O.WAIT_FN, O.TSLP_HIT_PC))
print("   清零循环   [0x%06X,0x%06X) 24 轮 st @(r0,r13) + set_flg(12,1)"
      % (O.SLOT_LOOP_FN, O.SLOT_LOOP_END + 2))
print("   写回函数   [0x%06X,0x%06X) stm1/enter + 0x%06X 写回槽 0"
      % (O.SLOT_WB_FN, O.TASK7_END, O.SLOT_WB_PC))
for name, fn, callers in (("等待函数", O.WAIT_FN,
                           (O.SLOT_CLEAR_CALL, O.SLOT_CLEAR_CALL2)),
                          ("写回函数", O.SLOT_WB_FN, (O.SLOT_WB_CALL,))):
    got = sorted({p for p in REL12.get(fn, [])})
    print("E1 %s 序言 0x%06X ← call rel12 调用点 %s；实测 %s" %
          (name, fn, [hex(x) for x in callers], [hex(x) for x in got]))
for pc in (O.SLOT_CLEAR_CALL, O.SLOT_CLEAR_CALL2, O.SLOT_WB_CALL):
    d = decode_at(pc)
    print("E2 0x%06X %-6s rel12=%s → 0x%06X（in task7 主体 = %s）" %
          (pc, d[1], d[3].get('rel12'),
           O.rel12_target(pc, d[3].get('rel12', 0)),
           O.task7_region(pc)))
print("E3 task7 入口 0x%06X、enter=0x%06X、主体终点 0x%06X；"
      "V64 已证链起点 0x17518E `sta_tsk(7)`" %
      (O.TASK7_ENTRY, O.TASK7_ENTER, O.TASK7_END))
loop = O.SLOT_LOOP_FN
ed = list(SW.edges.get(loop, []))
st = [p for p, _i, _nx in SW.callers_strict(loop)]
rl = REL12.get(loop, [])
jm = JMP_REL12.get(loop, [])
rf = [p for p, _i, _nx in SW.refs_to(loop, window=0)]
lv = O.loop_entry_verdict(len(ed), len(st), len(rl), len(jm))
print("E4 清零循环 0x%06X 四套可定址入边：call:d=%d ldi:32严格=%d "
      "call rel12=%d jmp rel12=%d ldi 引用=%d ⇒ %s" %
      (loop, len(ed), len(st), len(rl), len(jm), len(rf), lv['claim']))
print("E5 另有 %d 条 `call @r` 目标静态不可定，故**只能报「无可定址入边」，"
      "不能报「没人调用」**（规矩 68 / 69）。" % O.CALL_REG_TOTAL)
print("E6 更正 V76/V77 与 V78 草稿：等待函数 / 写回函数不是「没有调用者」，"
      "是调用形态为 call rel12 而 `Sweep.edges` 不收（D 段）；"
      "草稿把等待函数误叫成清零循环函数，本段已按 ret 边界拆开。")

# ==================================================================== F
hr("F. 静态：任务 / ISR 根归属与阴性边界")
print("F1 任务表 %d 条（0x%06X，记录 0x1C，+0x10 入口）；向量表 256 槽里 "
      "非默认入口 %d 个（倒序 slot(n)=TBR+(255-n)*4，规矩 60）" %
      (len(RECS), O.TASK_TABLE, len(ISR_ENTRIES)))
print("F2 18 个字段写函数里能归到任务根的 %d 个：%s" %
      (len(O.rooted_functions(ROOTS_A)),
       [(hex(f), ROOTS_A[f]) for f in O.rooted_functions(ROOTS_A)]))
nrf, nre = O.root_coverage(EDGE_FNS_ENTER, ROOTS_B)
print("F3 302 个提交调用点所在 %d 个 enter 函数里，能归到任务/ISR 根的 %d 个、"
      "覆盖调用点 %d 个：%s" %
      (len(EDGE_FNS_ENTER), nrf, nre,
       [(hex(f), EDGE_FNS_ENTER[f], ROOTS_B[f])
        for f in O.rooted_functions(ROOTS_B)]))
tv = O.trigger_verdict(nre, len(SUBMIT_EDGES), nrf, len(EDGE_FNS_ENTER))
print("F4 其余 %d 个调用点在 %d 个非根函数里，静态表定不到触发任务："
      "只能报「入口未定」，不能编一条任务出来" %
      (tv['undetermined_edges'], tv['total_fns'] - tv['root_fns']))
print("F5 规矩 60 / 62 延续：根归属用任务表 +0x10 与倒序向量表，"
      "允许 8 字节序言回退（bstatic.refs_to 的 window=8）。")

# ==================================================================== G
def covset(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def fire_r4(m, r4val, idx=5):
    m.bus.wr(D.IDX_A, 1, idx)
    m.raise_irq(D.VEC_A)
    for _ in range(200):
        m.step(1)
        if m.cpu.pc == D.ISR_A:
            m.cpu.r[4] = r4val
            return True
    return False


def awaken(m, lv):
    brealos.kernel_call(m, K.SET_FLG, r4=3,
                        r5=W.WANTPTN_1 if lv == 1 else K.BIT10)
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)


def n3_prefix(m):
    brealos.kernel_call(m, K.GET_TID)
    fire_r4(m, 5)
    m.run(max_steps=POST, spin_guard=False)
    for lv in range(1, 4):
        awaken(m, lv)


GROUPS = (
    ('H0', '基线（只收敛）', ()),
    ('H1', '打 vec 0x2D', ('vec2d',)),
    ('H2', '错配 vec 0x2B', ('misvec',)),
    ('H3', 'N3 三级唤醒 + vec 0x2D', ('n3', 'vec2d')),
    ('H4', 'kernel_call sta_tsk(7)', ('sta7',)),
    ('H5', 'vec 0x2D + sta_tsk(7)', ('vec2d', 'sta7')),
)


def run_group(actions):
    m = boot_run()
    c0 = covset(m)
    sta = None
    if 'n3' in actions:
        n3_prefix(m)
    if 'vec2d' in actions:
        m.raise_irq(O.VEC_2D)
        m.run(max_steps=POST, spin_guard=False)
    elif 'misvec' in actions:
        m.raise_irq(S.VEC_TICK)
        m.run(max_steps=POST, spin_guard=False)
    if 'sta7' in actions:
        sta = brealos.sta_tsk(m, O.TASK7_TID)
        m.raise_irq(VEC)
        m.run(max_steps=POST, spin_guard=False)
    return m, c0, sta


def snapshot(m, c0, sta):
    c = covset(m)
    idx = bready.task_index(m)
    t7 = idx.get(O.TASK7_TID)
    return {
        'cov': c, 'new': len(c - c0), 'total': len(c), 'pc': m.cpu.pc,
        'f18': len(c & set(FIELD_FNS_ENTER)),
        'f168': len(c & set(EDGE_FNS_ENTER)),
        'big': BIG in c, 'task7': O.TASK7_ENTRY in c,
        'clear_fn': O.WAIT_FN in c, 'loop_fn': O.SLOT_LOOP_FN in c,
        'clear_st': O.SLOT_LOOP_PC in c,
        'wb_fn': O.SLOT_WB_FN in c, 'wb_st': O.SLOT_WB_PC in c,
        'clear_call': O.SLOT_CLEAR_CALL2 in c,
        'field': O.coverage_hits(ALL_FIELD, c),
        'edges': O.coverage_hits(SUBMIT_EDGES, c),
        't7state': bready.state_of(m, t7) if t7 else None,
        'sta_r12': sta.get('err') if sta else None,
    }


hr("G. 动态：收敛态与六组等形反事实（设备策略与 V65–V77 逐字相同）")
shapes = [(BOOT, VEC, RUN) for _tag, _d, _a in GROUPS]
print("G0 等形判据 = %s（所有组 BOOT/VEC/RUN 前缀相同，唯一差异是收敛后动作）；"
      "模型动作 = %s" % (O.equal_shape(shapes), ', '.join(O.model_actions())))
RES = {}
for tag, desc, actions in GROUPS:
    m, c0, sta = run_group(actions)
    r = snapshot(m, c0, sta)
    RES[tag] = r
    print("  %-3s %-28s 新增PC=%3d 覆盖=%4d 末PC=0x%06X ｜ f18=%d/18 "
          "f168=%d/168 巨型=%-5s" %
          (tag, desc, r['new'], r['total'], r['pc'], r['f18'], r['f168'],
           r['big']))
    print("      task7入口=%-5s 等待函数=%-5s 清零循环=%-5s 清零st=%-5s "
          "写回函数=%-5s 写回st=%-5s ｜ 字段写=%d/142 调用点=%d/302 ｜ "
          "task7 state=%s sta_tsk R12=%s" %
          (r['task7'], r['clear_fn'], r['loop_fn'], r['clear_st'],
           r['wb_fn'], r['wb_st'], r['field'], r['edges'],
           r['t7state'], r['sta_r12']))
    sys.stdout.flush()

print("G1 归因分离（vec2d 命中组 vs 非命中组）= %s" %
      O.attribution([{'tag': t, 'vec2d': 'vec2d' in a} for t, _d, a in GROUPS],
                    'vec2d'))
print("G2 更正 V77 措辞：收敛态 task7 入口与**等待函数** 0x175876 都命中，"
      "停在其体内 0x17588E 的 tslp_tsk(10)；真正的清零循环 0x1758EC 与清零 st "
      "0x1758FA 六组全 0 命中 ⇒ 8 槽全 0 仍是 RAM 初始态。"
      "「等待函数没被调用」不成立，但「清零循环已执行」也不成立。")
print("G3 六组里字段写 142、提交调用点 302、写回函数全部 0 命中 ⇒ "
      "这些反事实不触及该路径，链未闭合。")

# 窗口对称差集
mlong = boot_run(WINDOW)
clong = covset(mlong)
cbase = RES['H0']['cov']
print("G4 500k 覆盖=%d 1M 覆盖=%d 对称差集=%d window_same=%s 末PC=0x%06X" %
      (len(cbase), len(clong), len(cbase ^ clong),
       O.window_same(cbase, clong), mlong.cpu.pc))

# ==================================================================== H
hr("I. 服务号命名与 task7 等待对象（只读取证，不注入）")
print("I1 -37 统一命名 = %s（waitstat67.svc_name=%s / "
      "abservice.SVC_NAMES[0xDB]=%s / itron sys_tslp_tsk），规矩 64/69" %
      (O.TSLP_NAME, W67.svc_name(O.SVC_TSLP_TSK),
       abservice.SVC_NAMES.get(0xDB)))
c37 = [c for c in SW.calls if c.svc == O.SVC_TSLP_TSK]
tv = O.tslp_verdict(len(c37), O.TSLP_A_TABLE_CALLS, O.TSLP_TIMEOUT)
print("I2 全 ROM svc=-37 调用点 = %d；abservice 的「0 处」是 A 段分发表项，"
      "B 段 %d 处与之**不矛盾**（same_ruler=%s）⇒ 是印证，不是更正" %
      (len(c37), O.TSLP_TOTAL_CALLS, tv['same_ruler']))
hit = [c for c in c37 if c.addr == O.TSLP_HIT_PC]
print("I3 卡点 0x%06X 参数 %s ⇒ %s(%d)，r7 端口窗口 0x%08X" %
      (O.TSLP_HIT_PC, hit[0].args() if hit else '?',
       O.TSLP_NAME, O.TSLP_TIMEOUT, O.TSLP_PORT))
m1 = boot_run()
i1 = bready.task_index(m1)
t7 = i1.get(O.TASK7_TID)
why = (m1.bus.rd(t7 + 0x20, 4) >> 16) & 0xFFFF
why = why - 0x10000 if why & 0x8000 else why
left = m1.bus.rd(t7 + 0x14, 4)
wv = O.t7_wait_verdict(why, left, bready.state_of(m1, t7))
print("I4 task7 TCB=0x%08X state=0x%02X TCB+0x20=%d(%s) TCB+0x14=%d "
      "⇒ is_tslp=%s tick_driven=%s；与 V51「任务 7 卡在 tslp_tsk(10)」闭环" %
      (t7, wv['state'], wv['why'], wv['name'], wv['left_ticks'],
       wv['is_tslp'], wv['tick_driven']))
print("I5 缺的是把剩余 tick 递减到 0 的**真机时基**（KI#191），"
      "不是固件代码缺席；这不等于「补一次 set_flg 就唤醒成功」。")

hr("H. 结案与仍开放")
print("H1 KI#302 部分结案：字段写函数按 enter 口径归到 %d 个（其中 76 条在 "
      "0x171184 巨型函数）；等待函数 0x175876 ← call rel12 0x175234 / 0x175418，"
      "写回函数 0x175908 ← 0x17574C；清零循环 0x1758EC 四套可定址入边 0；"
      "收敛态任务 7 停在 0x17588E 的 tslp_tsk(10)、TCB+0x14 剩 9 tick。"
      % len(FIELD_FNS_ENTER))
print("H2 规矩 67：函数边界要分清 enter 序言起点与 ret 多出口，"
      "更要分清相邻的等待 / 清零 / 写回三个函数；"
      "规矩 68：调用边要扫四套口径，Sweep.edges 只是一套；"
      "规矩 69：A 段分发表项数与 B 段调用点数不是同一把尺子，"
      "四套入边 0 时还有 4823 条 call @r 不可定址。")
print("H3 仍开放：tslp_tsk(10) 的超时 tick 由谁递减（KI#191 真机时基）、"
      "清零循环 0x1758EC 的不可定址入边与触发条件、写回函数 0x175908 未被调到"
      "的条件、302 调用点里 290 个非根函数的触发任务、两个状态字偏移 0 "
      "的 bit3 置位源、vec 0x2D 硬件事件、真机让 r4 非 0 的事件语义、"
      "0x12E636 返回非 0 条件、13 组 ISR 硬件事件、"
      "[12,13,14,15] 激活机制（KI#257）。")
print("H4 禁语：%s" % ' / '.join(O.forbidden_claims()))
print("H5 本版所有 raise_irq / r4 注入 / kernel_call 都是模型动作，不是固件行为；"
      "静态可达 ≠ 已经执行，更不能报出画面。")
print("DONE")
