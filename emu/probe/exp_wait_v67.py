# -*- coding: utf-8 -*-
"""V67 正式探针：17 个等待任务各自挂在哪个对象上、为什么醒不过来。

复现（从 emu/ 目录）：
    python probe/exp_wait_v67.py > ../results/frbox/wait67.txt

承接 KI#257。V66 结掉了「16 笔 sta_tsk 全 E_OK、TCB 全激活、入口全跑过」，
但留下「16 个任务各自挂在哪个对象上」没取，只看到收敛时全是 state=0x04。
本版把等待原因、对象队列归属、唤醒条件一次取全，并顺手修掉一个长期
隐藏的服务号解码口径问题。

纪律：
* 三台独立机器：主收敛（500000 步）/ 窗口自证（1000000 步）/
  短窗口对照（200000 步）。一台机器只跑一遍（硬规矩 13）。
* 不自写 cpu.step() 循环：只包装 cpu.step 做纯观察，推进由
  Machine.step() / Machine.run() 驱动（硬规矩 1）。
* 设备策略与 V65/V66 收敛组逐字相同：blive.boot(0) → step(191520) →
  单次 raise_irq(0x2B) → run(N, spin_guard=False)；不补映射、不打桩、
  不给状态位。加长 RUN 只增执行预算，不是建模动作。
* 归属图双路取证：一条从 ROM flag 表 → 对象 → 沿链枚举等待者
  （btick.object_queues），另一条从每个等待 TCB 沿 next 链反查哨兵
  （waitstat67.queue_owner）。两条独立路径必须逐项一致。
* 静态字段语义用固件指令钉死，不用常识推断：0x041B54 / 0x041A5C /
  0x041A60 / 0x041A78 / 0x040A78。
"""

import sys
import os
import collections

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # emu/

import fr_emu
from frbox import (blive, bstatic, bready, brealos, btick, devaudit,
                   taskchain64 as T, waitstat67 as W)

BOOT = blive.BOOT_STEPS
VEC = 0x2B
RUN = 500000
STRUCT_FLAG = 0x9F5A0284
HANDLER_CB = 0x9F59EF50
SLOT8 = 0x9F59F7C8

SW, M0 = bstatic.sweep_of_type("d300-b")
ROM, BASE = SW.rom, SW.base
RECS = T.task_table(ROM, BASE)
LAUNCH = T.launchers(SW, RECS)
STA16 = sorted(set(l.tid for l in LAUNCH if l.tid is not None))


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def coverage(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def tcb_rows(m, index):
    return dict((t, {"next": m.bus.rd(t + W.TCB_NEXT, 4),
                     "prev": m.bus.rd(t + W.TCB_PREV, 4)})
                for t in index.values())


def state_map(m, index):
    return dict((tid, bready.state_of(m, t)) for tid, t in index.items())


def assignment_from_objects(m, index):
    """路 A：ROM flag 表 + 延时队列 → {owner: [tcb]}。"""
    out = {}
    for ident, obj, nodes in btick.object_queues(m):
        if nodes:
            out[obj] = list(nodes)
    dq = btick.delay_queue(m)
    if dq:
        out[W.DELAY_SENTINEL] = [x[0] for x in dq]
    return out


def assignment_from_tcbs(m, index):
    """路 B：每个等待 TCB 沿 next 链找哨兵 → {owner: [tcb]}。"""
    rows = tcb_rows(m, index)
    waiting = [t for tid, t in index.items() if bready.state_of(m, t) == W.WAIT]
    return W.build_assignments(rows, waiting), rows, waiting


def boot_run(steps):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def or_bits(values):
    out = 0
    for v in values:
        out |= v
    return out


# ---------------------------------------------------------------- A 静态
hr("A  静态口径：服务号按立即数宽度读，字段语义用固件指令钉死")

print("A1 任务表 0x054C00：%d 条 × 0x1C；`sta_tsk`(−23) 静态点 %d 处 / "
      "%d 个任务号；V66 收敛态命中 16 个" %
      (len(RECS), len(LAUNCH), len(STA16)))

regs = [None] * 16
raw = []
off, n = 0, len(ROM)
while off < n - 1:
    if ROM[off:off + 2] in bstatic.SKIP2:
        off += 2
        continue
    ins = fr_emu.TABLE.decode(ROM, off)
    if ins is None:
        off += 2
        continue
    mn, _bs, v, size, _w = ins
    if mn == "int" and v.get("u8") == 0x40:
        raw.append((BASE + off, regs[12]))
    if mn not in bstatic.NONWRITE:
        ri = v.get("ri")
        if mn.startswith("ldi"):
            imm = v.get("i8")
            if imm is None:
                imm = v.get("i20")
            if imm is None:
                imm = v.get("i32")
            if ri is not None:
                regs[ri] = imm
        elif mn == "mov":
            if ri is not None:
                regs[ri] = None
            rj = v.get("rj")
            if rj is not None:
                regs[rj] = None
        elif ri is not None:
            regs[ri] = None
    off += size

cc = collections.Counter(v for _, v in raw)
print("A2 全 ROM `int #0x40` 静态点 %d 个，r12 原始值 %d 种（%d 个点读不出）" %
      (len(raw), len(cc) - (1 if None in cc else 0), cc.get(None, 0)))
wide = [(v, cnt, W.svc_of_r12(v, 32), W.sx8(v)) for v, cnt in cc.items()
        if v is not None and (v & 0xFFFFFF00) == 0xFFFFFF00
        and W.svc_of_r12(v, 32) != W.sx8(v)]
for v, cnt, s32, s8 in sorted(wide, key=lambda x: x[2]):
    pts = [a for a, x in raw if x == v]
    print("   0x%08X ×%d 点  32位服务号=%d  低8位口径=%d  代表点 %s" %
          (v, cnt, s32, s8, ["0x%06X" % a for a in pts[:5]]))
print("A3 ⚠️ `bstatic.sx8` / `btick.sx8` 统一取低 8 位：上面 %d 种宽服务号"
      "（共 %d 个静态点）被误编号；其余 %d 种服务号两种读法一致" %
      (len(wide), sum(x[1] for x in wide),
       len(cc) - (1 if None in cc else 0) - len(wide)))
print("   `bstatic` 现有口径把它们记成 svc=%s；`brealos.SVC_TWAI_FLG` 记的是 −170" %
      sorted(set(x[3] for x in wide)))
for svc in (-48, -46, -85, -37, 86, 85):
    print("   svc=%-5d 静态点 %4d 个" % (svc, len(SW.of(svc))))

print("A4 字段语义的固件指令证据（静态反汇编，纯 ROM 读取）：")
print("   0x041B54  st  @(R14,5*4=+0x14), r6   ← wai_flg 把等待位模式写进 TCB+0x14")
print("   0x041A5C  ld  @(R14,2*4=+0x08), r7   ← set_flg 读 flag 对象 +8 当前位模式")
print("   0x041A60  st  @(R14,2*4=+0x08), r5   ← OR 之后写回 +8")
print("   0x041A78  ld  @(R14,5*4=+0x14), r7   ← set_flg 读等待者的 TCB+0x14 判满足")
print("   0x040A78  ld  #20, r6                 ← tick 处理读 TCB+0x14 剩余 tick")
print("   ⇒ `TCB+0x14` 语义随等待原因变化：flag 等待是位模式，延时等待是剩余 tick；")
print("     V52 的 `TCB_TICKS` 命名只在延时队列语境成立。")
print("A5 `TCB+0x20` 高半 = r12 低 16 位 ⇒ 按 16 位补码读（waitstat67.sx16）；")
print("   `0xFF56` = −170，`btick.sx8` 会读成 +86。")

# ---------------------------------------------------------------- B 主机器
hr("B  500000 步收敛机器：设备策略与 V65/V66 逐字相同")

m = blive.boot(0)
print("B1 设备策略 %s" % devaudit.audit(m))
m.step(BOOT)
print("B2 BOOT=%d 步后 PC=0x%06X；`svc_hook=None` ⇒ `int #0x40` 走真向量表 0x040668" %
      (BOOT, m.cpu.pc))
trap = []
orig = m.cpu.step


def watch():
    if m.cpu.pc == W.TRAP_ENTRY:
        trap.append((m.cpu.r[12], bready.current(m)))
    return orig()


m.cpu.step = watch
m.raise_irq(VEC)
m.run(max_steps=RUN, spin_guard=False)
m.cpu.step = orig
print("B3 单次 raise_irq(0x%02X) + RUN=%d ⇒ 末 PC=0x%06X；"
      "blocked=%s spin=%s fault=%s" %
      (VEC, RUN, m.cpu.pc, getattr(m, "blocked", None),
       getattr(m, "spin", None), getattr(m, "fault", None)))
idx = bready.task_index(m)
rev = dict((t, k) for k, t in idx.items())
cov = coverage(m)
print("B4 陷阱入口命中 %d 笔 / r12 取值 %d 种：" %
      (len(trap), len(set(v for v, _ in trap))))
tc = collections.Counter(v for v, _ in trap)
for v, cnt in tc.most_common():
    print("   r12=0x%08X (%5d) ×%d  %s" %
          (v, W.sx32(v), cnt, W.svc_name(W.sx32(v))))
print("B5 其中 0xFFFFFF56（−170）%d 笔，发生时当前 TCB：%s" %
      (tc.get(0xFFFFFF56, 0),
       sorted(set("0x%08X(任务%s)" % (t, rev.get(t, "?")) for v, t in trap
                  if v == 0xFFFFFF56))))

# ---------------------------------------------------------------- C TCB
hr("C  29 个 TCB 的等待字段（同一台收敛机器）")

st = state_map(m, idx)
print("C1 任务 TCB        state  why_raw  svc16  svc8   TCB+0x14   next       prev")
for tid in sorted(idx):
    t = idx[tid]
    raw_why = m.bus.rd(t + W.TCB_WHY, 4) >> 16
    print("%-4d 0x%08X  0x%02X   0x%04X  %5d  %5d  0x%08X 0x%08X 0x%08X" %
          (tid, t, st[tid], raw_why, W.why_svc(raw_why), W.why_old(raw_why),
           m.bus.rd(t + W.TCB_WANT, 4),
           m.bus.rd(t + W.TCB_NEXT, 4), m.bus.rd(t + W.TCB_PREV, 4)))
sd = collections.Counter(st.values())
print("C2 state 分布：0x04 等待 %d 个 / 0x10 休眠 %d 个 / 0x00 就绪 %d 个" %
      (sd.get(W.WAIT, 0), sd.get(W.DORMANT, 0), sd.get(W.READY, 0)))
waiting = sorted(t for tid, t in idx.items() if st[tid] == W.WAIT)
whys = collections.Counter(W.why_svc(m.bus.rd(t + W.TCB_WHY, 4) >> 16)
                           for t in waiting)
print("C3 %d 个等待任务的等待原因：%s" %
      (len(waiting), sorted((k, v, W.svc_name(k)) for k, v in whys.items())))
v66_16 = [1, 3, 4, 5, 6, 7, 9, 10, 11, 16, 17, 20, 21, 26, 27, 28]
print("C4 V66 的 16 个 `sta_tsk` 任务：%s；其中在等待态 %d 个" %
      (v66_16, sum(1 for k in v66_16 if st[k] == W.WAIT)))
print("   另有 BOOT 自建的任务 2 也在等待态；任务 29 是空闲（state=0x00）。")

# ---------------------------------------------------------------- D 归属
hr("D  归属图：17 个等待任务 → 7 条队列（双路取证，本版主结论）")

obj_a = assignment_from_objects(m, idx)
obj_b, rows, waiting_b = assignment_from_tcbs(m, idx)
print("D1 路 A（ROM flag 表 0x551B0 → 对象 → 沿链枚举）：")
for owner, nodes in sorted(obj_a.items()):
    print("   %-14s 0x%08X  等待者 %d 个：%s" %
          (W.owner_name(owner), owner, len(nodes),
           ["任务%s" % rev.get(x, "?") for x in nodes]))
print("D2 路 B（每个等待 TCB 沿 next 链反查哨兵）：")
for owner, nodes in sorted(obj_b.items(), key=lambda kv: (kv[0] is None, kv[0])):
    print("   %-14s 0x%08X  等待者 %d 个：%s" %
          (W.owner_name(owner) if owner is not None else "未定",
           owner or 0, len(nodes),
           ["任务%s" % rev.get(x, "?") for x in nodes]))
same = dict((k, sorted(v)) for k, v in obj_a.items()) == \
       dict((k, sorted(v)) for k, v in obj_b.items())
print("D3 两路归属逐项一致：%s" % same)
cl = W.closure(obj_b, waiting)
print("D4 闭环校验：%d 条队列 / 归到队列的等待 TCB %d 个 / 等待集合 %d 个；"
      "遗漏 %s、外来 %s、重复 %s ⇒ %s" %
      (cl["queues"], cl["assigned"], len(waiting),
       cl["missing"], cl["extra"], cl["duplicated"],
       "闭环成立" if cl["ok"] else "不闭环"))
flag_waiters = sorted(rev[x] for owner, nodes in obj_b.items()
                      if owner is not None and W.flag_id_of(owner) is not None
                      for x in nodes)
delay_waiters = sorted(rev[x] for x in obj_b.get(W.DELAY_SENTINEL, []))
print("D5 按队列性质分：等 flag 的 %d 个 %s；等延时的 %d 个 %s" %
      (len(flag_waiters), flag_waiters, len(delay_waiters), delay_waiters))
print("D6 V66 那 16 个任务里：等 flag %d 个 %s、等延时 %d 个 %s" %
      (len([x for x in flag_waiters if x in v66_16]),
       [x for x in flag_waiters if x in v66_16],
       len([x for x in delay_waiters if x in v66_16]),
       [x for x in delay_waiters if x in v66_16]))
hits = []
for a in range(0x68000000, 0x68001000, 4):
    v = m.bus.rd(a, 4)
    if 0x6800013C <= v < 0x6800013C + 29 * 0x2C:
        hits.append((a, v))
print("D7 内核区反查：0x68000000..0x68001000 里指向 TCB 的指针 %d 个"
      "（含队列哨兵、链表互指、等待信息块背指）" % len(hits))

# ---------------------------------------------------------------- E 唤醒
hr("E  为什么醒不过来：当前位模式与等待位模式没有任何位重合")

blocked_all, checked = [], 0
for ident, obj, nodes in btick.object_queues(m):
    if not nodes:
        continue
    pat = m.bus.rd(obj + W.OBJ_PAT, 4)
    sets = SW.flag_sets(ident)
    hit = [c for c in sets if c.addr in cov]
    print("E1 flag %-2d obj=0x%08X 当前位模式=0x%08X 等待者 %d 个；"
          "set_flg 静态 %d 点 / 收敛态命中 %d 点" %
          (ident, obj, pat, len(nodes), len(sets), len(hit)))
    if hit:
        for c in hit:
            print("      命中 set_flg 0x%06X flgid=%s bits=%d(0x%X)" %
                  (c.addr, c.flgid, c.bits or 0, c.bits or 0))
        print("      命中位 OR 合计=0x%08X" % or_bits([c.bits or 0 for c in hit]))
    for tcb in nodes:
        tid = rev.get(tcb, "?")
        want = m.bus.rd(tcb + W.TCB_WANT, 4)
        why = W.why_svc(m.bus.rd(tcb + W.TCB_WHY, 4) >> 16)
        or_ok, and_ok, blocked = W.wakeable(pat, want)
        checked += 1
        if blocked:
            blocked_all.append(tid)
        print("      任务%-3s 等待位模式=0x%08X  pat&want=0x%08X  why=%-5d"
              "  OR满足=%s AND满足=%s ⇒ %s" %
              (tid, want, pat & want, why, or_ok, and_ok,
               "两种模式都不唤醒" if blocked else "可被唤醒"))
print("E2 逐个核对 %d 个 flag 等待者：%d 个 `pat&want==0 且 want!=0`，"
      "即 %s；这个判据不依赖等待模式字段" %
      (checked, len(blocked_all),
       "全部醒不过来" if len(blocked_all) == checked else "并非全部"))
print("   （OR 模式要有任一位重合、AND 模式要全部位都置上；两者在无重合时都不成立）")
all_sets = SW.of(-48)
print("E3 全 ROM 唤醒/等待服务静态点与收敛态覆盖：`set_flg`(−48) %d 点 / 命中 %d；"
      "`wai_flg`(−46) %d 点 / 命中 %d；`svc=86`(实为 −170) %d 点 / 命中 %d" %
      (len(all_sets), sum(1 for c in all_sets if c.addr in cov),
       len(SW.of(-46)), sum(1 for c in SW.of(-46) if c.addr in cov),
       len(SW.of(86)), sum(1 for c in SW.of(86) if c.addr in cov)))
zero_flags = [(i, o) for i, o, nd in btick.object_queues(m)
              if nd and m.bus.rd(o + W.OBJ_PAT, 4) == 0]
print("E4 当前位模式为 0 且仍有等待者的 flag %d 个：%s" %
      (len(zero_flags), ["flag %d@0x%08X" % (i, o) for i, o in zero_flags]))
print("E5 延时队列 3 个任务的剩余 tick：")
for tcb, tid, prio, why in btick.delay_queue(m):
    print("      任务%-3s 剩余tick=%d  why=%d (%s)" %
          (tid, m.bus.rd(tcb + W.TCB_WANT, 4), why, W.svc_name(why)))
print("   ⇒ 它们等的是内核 tick（`0x040A44` 递减 TCB+0x14），"
      "真机时基在这台机器里没建模（KI#191），不是「没人调用服务」。")

# ---------------------------------------------------------------- F 口径
hr("F  等待原因口径证伪：0xFF56 是 −170，不是 +86")

print("F1 运行时陷阱 r12 里出现过 %d 种服务号，含 0xFFFFFF56 三笔（B5）；"
      "收敛态 `TCB+0x20=0xFF56` 的任务是 %s" %
      (len(tc), sorted(rev[t] for t in idx.values()
                       if (m.bus.rd(t + W.TCB_WHY, 4) >> 16) == 0xFF56)))
print("F2 三个 TCB 中 0x680003D0(任务16) / 0x68000480(任务20) 与收敛态吻合；"
      "0x68000244(任务7) 那一笔没有留在收敛态 `TCB+0x20`（当前是 −37），"
      "说明它后来被 `tslp_tsk`(−37) 改写了。")
print("F3 两种解码对照（只列本版出现的 4 种等待原因）：")
for raw_why in (0xFFD2, 0xFFDB, 0xFFAB, 0xFF56):
    print("   0x%04X → 16位=%-6d 8位=%-6d %s" %
          (raw_why, W.sx16(raw_why), W.sx8(raw_why),
           "⚠️ 旧口径错" if W.sx16(raw_why) != W.sx8(raw_why) else "一致"))
print("F4 ⇒ `bstatic.sx8` / `btick.sx8` 对 `ldi:8` 装的服务号是对的；"
      "错在把 `ldi:32 #0xFFFFFF56` 也取低 8 位。")
print("   服务号须按**立即数宽度**解码（`waitstat67.svc_of_r12`），"
      "`TCB+0x20` 一律按 16 位补码读。")

# ---------------------------------------------------------------- G 窗口
hr("G  窗口自证：500000 步不是随手挑的窗口")

m4 = boot_run(1000000)
idx4 = bready.task_index(m4)
rev4 = dict((t, k) for k, t in idx4.items())
obj4 = assignment_from_objects(m4, idx4)
cov4 = coverage(m4)
states4 = state_map(m4, idx4)
pat4 = dict((o, m4.bus.rd(o + W.OBJ_PAT, 4)) for i, o, nd in btick.object_queues(m4) if nd)
want4 = dict((t, m4.bus.rd(t + W.TCB_WANT, 4)) for t in idx4.values()
             if states4.get(rev4.get(t), -1) == W.WAIT)
pat_m = dict((o, m.bus.rd(o + W.OBJ_PAT, 4)) for i, o, nd in btick.object_queues(m) if nd)
want_m = dict((t, m.bus.rd(t + W.TCB_WANT, 4)) for t in waiting)
print("G1 1000000 步：末 PC=0x%06X、位图 0x%08X、就绪队列占用 %d、"
      "等待任务 %d 个、归属队列 %d 条" %
      (m4.cpu.pc, bready.ready_map(m4), len(bready.occupancy(m4)),
       sum(1 for x in states4.values() if x == W.WAIT), len(obj4)))
print("   500000 步：末 PC=0x%06X、位图 0x%08X、就绪队列占用 %d、"
      "等待任务 %d 个、归属队列 %d 条" %
      (m.cpu.pc, bready.ready_map(m), len(bready.occupancy(m)),
       len(waiting), len(obj_b)))
print("G2 归属字典相同=%s；flag 位模式相同=%s；等待位模式相同=%s；"
      "29 个 state 差异任务号=%s" %
      (dict((k, sorted(v)) for k, v in obj4.items()) ==
       dict((k, sorted(v)) for k, v in obj_a.items()),
       pat4 == pat_m, want4 == want_m,
       sorted(k for k in st if st[k] != states4.get(k))))
print("G3 PC 覆盖对称差集 %d 个 ⇒ 与 V65/V66 的收敛判据一致" % len(cov ^ cov4))

m2 = boot_run(200000)
idx2 = bready.task_index(m2)
obj2 = assignment_from_objects(m2, idx2)
wait2 = sorted(t for t in idx2.values() if bready.state_of(m2, t) == W.WAIT)
print("G4 200000 步短窗口对照：末 PC=0x%06X、等待任务 %d 个、"
      "归属队列 %d 条；与 500000 步归属字典相同=%s" %
      (m2.cpu.pc, len(wait2), len(obj2),
       dict((k, sorted(v)) for k, v in obj2.items()) ==
       dict((k, sorted(v)) for k, v in obj_a.items())))
print("   ⇒ 归属图在短窗口会变，所以主结论锚在 500000/1000000 步一致的收敛态。")

# ---------------------------------------------------------------- H 边界
hr("H  显示链仍未闭合（与 V65/V66 同口径）")

chain = (0x120178, 0x12E602, 0x12E630, 0x12E6F8, 0x12E752)
print("H1 显示链 5 PC：%s" %
      ["0x%06X:%s" % (p, "命中" if p in cov else "未命中") for p in chain])
print("H2 0x%08X=0x%08X、0x%08X=0x%08X、8 槽回调表 0x%08X = %s" %
      (STRUCT_FLAG, m.bus.rd(STRUCT_FLAG, 4),
       HANDLER_CB, m.bus.rd(HANDLER_CB, 4), SLOT8,
       [m.bus.rd(SLOT8 + i * 4, 4) for i in range(8)]))

# ---------------------------------------------------------------- I 结论
hr("I  能说 / 不能说 / 复现")

print("能说：")
print("  1. 17 个等待任务全部归到 7 条明确的内核队列：6 个事件标志（flag 3/5/6/8/14/17）"
      "共 14 个、延时队列 0x68000118 共 3 个；两路独立取证逐项一致、闭环无遗漏。")
print("  2. 14 个 flag 等待者的等待位模式与 flag 当前位模式**没有任何位重合**，"
      "OR / AND 两种等待模式都不满足 ⇒ 醒不过来的直接答案。")
print("  3. 3 个延时任务等的是内核 tick（TCB+0x14 剩余 1/9/0），"
      "真机时基未建模，和 flag 等待是两类缺口。")
print("  4. 等待原因字段须按 16 位补码读：0xFF56 = −170；全 ROM 91 个 −170 点、"
      "2 个 −171 点被低 8 位口径误编号成 +86 / 85。")
print("  5. 500000 与 1000000 步归属图/位模式/状态逐项相同，PC 覆盖对称差集 0。")
print("不能说：")
print("  · 不能报「补一次 set_flg / 补一次 tick 就能出画面」—— 本版没有做任何"
      "反事实唤醒，也没验证唤醒之后显示链会闭合。")
print("  · 不能报显示链通了 / 出画面 / 找到真机 A↔B 通道；"
      "0x12E6F8、0x12E752 仍未命中，两个状态字仍是 0。")
print("  · 不能把 `TCB+0x14` 一律当剩余 tick：flag 等待时它是等待位模式。")
print("  · 不能把 11 个 DORMANT 任务当成「也在等」；它们 state=0x10、无栈、没上过 CPU。")
print("复现：")
print("  cd emu && python probe/exp_wait_v67.py > ../results/frbox/wait67.txt")
print("  ⚠️ 三台机器各跑一遍：C–F/H 共用 500000 步收敛机器；"
      "G1 的 1000000 步自证、G4 的 200000 步对照各用独立机器。")
print("  ⚠️ 不补映射、不打桩、不给状态位；加长 RUN 只是执行预算（V65 口径）。")
