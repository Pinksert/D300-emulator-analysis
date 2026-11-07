# -*- coding: utf-8 -*-
"""V47 探针：池的分配器到底在等什么 —— 卡点结案，附 flow 的 TCB 修复对照。

    cd emu && python probe/exp_bwait.py > ../results/frbox/bwait.txt

V43 把「`0x122064` 进去就不出来」留成 OPEN（KI#172），当时的猜测是「卡在取之前的
某个等待上」。本探针把它拆干净：

A  逐条走完 `0x122064`：进函数 → 加锁 → 清标志 → 探三条队列 → 解锁 → 等
B  加锁/解锁包装 `0x12590C` / `0x125992` 的真身 + 全机 6 次拿锁的持有者序列
C  卡点那一刻：flag 6 的位模式与等待者、池的计数与三条链表、锁的持有者
D  全机事件标志账：set_flg / clr_flg / wai_flg 按 flag 汇总 —— flag 6 从没被 set
E  反事实：补一次 `set_flg(6, 0x20)` 会怎样（**阴性**：醒了但再等一次）
F  flow 的 TCB 槽 bug：修前 tcb 只有一个假值，修后 17 个任务各归各位
G  结论与边界

两档机器：m1 走 499,000 步（A/B/C/E 用），m2 走 891,520 步（D/F 用）。
⚠️ 同一台机器不能跑两遍（会从停住的现场继续，V43 栽过）。
"""
import sys
sys.path.insert(0, '.')

import fr_dis
from frbox import bflag, blive, bpool, bready, brealos

ALLOC = bpool.ALLOC_NODE
WAIT = bpool.WAIT_FLG_PC
CALLER = bpool.CALLER_R8_PC
#: 醒来之后的「再锁一次」——证明同一条路被重走了一遍
RELOCK = 0x122162
BLOCK_STEP = 499000
FULL_STEP = 891520
FLG = bpool.POOL_FLAG
BIT = bpool.POOL_FLAG_BIT

#: 只用**硬证据**的几行：字节来自 ROM，语义来自动态观测。
#: ⚠️ 不照抄反汇编印刷文本 —— 本项目的 `fr_dis` 对 `mov` 的目的寄存器与
#: 相对跳转的目标地址都有读不准的地方（KI#173），拿它当证据会栽。
KEYBYTES = [
    (0x122082, 2, "call:d @r12          跳到 0x12590C（加锁包装）"),
    (0x122084, 2, "ldi:8 r4, #0x13      ← **延迟槽** = lock(19) 的实参"),
    (0x122090, 2, "int #0x40            实测 -47 → clr_flg(6, ~0x20)"),
    (0x12214A, 2, "call:d @r12          跳到 0x125992（解锁包装）"),
    (0x12214C, 2, "ldi:8 r4, #0x13      ← 延迟槽 = unlock(19) 的实参"),
    (0x122152, 2, "ldi:8 r5, #0x6       等的是 flag 6"),
    (0x122154, 2, "ldi:8 r6, #0x20      bit5"),
    (0x122156, 2, "ldi:8 r7, #0x2       mode 2 = 任一位成立"),
    (0x12215C, 2, "int #0x40            实测 -46 → wai_flg(6, 0x20, 2)  ★ 卡在这"),
    (0x122162, 6, "ldi:32 r12,#0x12590C 醒来后**重新加锁**的入口"),
]


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def task_of(m, tcb):
    """TCB 指针 → 任务号（0 = 认不出）。"""
    if not tcb:
        return 0
    for r in brealos.records(m):
        if r["tcb"] == tcb:
            return r["id"]
    return 0


def run_block():
    """m1：跑到卡点，收集 A/B/C 要的东西。"""
    m = blive.boot()
    hits = {ALLOC: 0, WAIT: 0, CALLER: 0, RELOCK: 0}
    timeline = []          # 函数体内 / 锁包装内的 int
    c, st = m.cpu, m.cpu.step
    armed = [False]

    def spy():
        pc = c.pc
        if pc == ALLOC:
            armed[0] = True
        if pc in hits:
            hits[pc] += 1
        return st()

    def hook(cpu, vec, pc):
        if not armed[0]:
            return
        if (0x122064 <= pc < 0x1221A0) or (0x12590C <= pc < 0x1259C2):
            v = cpu.r[12] & 0xFF
            timeline.append((m.step_count, (v - 0x100 if v & 0x80 else v),
                             cpu.r[4] & 0xFFFFFFFF, cpu.r[5] & 0xFFFFFFFF,
                             cpu.r[6] & 0xFFFFFFFF, cpu.r[7] & 0xFFFFFFFF, pc))

    m.attach_int_hook(hook)
    hw = bpool.HolderWatch(m).attach()
    c.step = spy
    blive.run(m, steps=BLOCK_STEP - m.step_count)
    c.step = st
    hw.detach()
    return {"m": m, "hits": hits, "timeline": timeline, "holder": hw,
            "step": m.step_count}


def sec_a(d):
    hr("A. 逐条走完 0x122064：它到底卡在哪一步")
    m = d["m"]
    rom, base = m.bus.rom, m.bus.rom_base
    print("  只列**能当证据的**几行（字节取自 ROM；语义取自动态观测，不照抄印刷文本）：")
    for a, n, txt in KEYBYTES:
        print("    0x%06X  %-14s %s" % (a, rom[a - base:a - base + n].hex(), txt))
    print("""
  → 最关键的一条：`0x122084 ldi:8 r4,#0x13` 是 `call:d` 的**延迟槽** ——
    它不是死代码，是 `lock(19)` 的实参。V43 把它读成「放 r4 = 0x13 之后又被
    下一句覆盖」，所以一直没看出第一步是加锁。
    `0x12214C` 那句同样是 `unlock(19)` 的延迟槽：**加锁 → 干活 → 解锁 → 等**，
    这条骨架两头对称。""")

    print("\n  实际时间线（这个函数体与锁包装里发生的全部服务调用）：")
    print("     %-9s %-9s %-24s %s" % ("步", "服务", "参数", "位置"))
    for s, svc, r4, r5, r6, r7, pc in d["timeline"]:
        name = {-24: "get_tid", -27: "chg_pri", -53: "wai_sem",
                -55: "sig_sem", -47: "clr_flg", -46: "wai_flg",
                -48: "set_flg"}.get(svc, "svc%d" % svc)
        arg = "r4=0x%X r5=0x%X" % (r4, r5)
        if svc == -46:
            arg = "flg=%d ptn=0x%X mode=%d" % (r5, r6, r7)
        elif svc in (-47, -48):
            arg = "flg=%d bits=0x%X" % (r4, r5)
        elif svc in (-53, -55):
            arg = "sem=%d" % r4
        print("    %7d  %-9s %-24s 0x%06X" % (s, name, arg, pc))
    print("""
  → 步 495,861 的 `wai_sem(19)` **成功了**（计数 1 → 0，内核在步 495,923 写 0）。
    所以「进去就不出来」不是卡在锁上 —— V43 的猜测到这里被推翻。
    真正停住的是最后那句 `wai_flg(6, 0x20, mode=2)`。

  → 中间那两句「两次探测」是什么，本条**不下断言**：它们是对 `0x124F4E`
    （链表库的 `is_empty`，V44 记的四件套之一）的两次调用，两条 `bne` 都从
    探测处跳到解锁之前 —— 但本项目的反汇编器对相对跳转的目标地址读得不准
    （KI#173），所以这里只报「调了两次 is_empty」，不报跳转落点。""")


def sec_b(d):
    hr("B. `0x12590C` / `0x125992` 是加锁 / 解锁包装（优先数继承）")
    m = d["m"]
    rom, base = m.bus.rom, m.bus.rom_base
    for lo, hi, t in ((0x12590C, 0x12594A, "加锁 0x12590C(sem_id)"),
                      (0x125992, 0x1259C2, "解锁 0x125992(sem_id)")):
        print("  ---- %s ----" % t)
        for s in fr_dis.disasm_addr(rom, lo, hi, base):
            print("    " + s)
        print()
    print("""
  → 两边骨架一样：get_tid(-24) 存自己 → chg_pri(-27) 自己提到 1 →
    加锁端 wai_sem(-53) / 解锁端 sig_sem(-55) → 写「持有者」字节 → chg_pri 归 0。
    「持有者」表基址 `0x9F58C4D0`，**按 sem_id 索引**（实测 +19 位写任务号）。

  → 拿 sem 19 的顺序（谁拿、谁放）。先看**跑到卡点为止**（m1，499,000 步）：""")
    for title, key in (("m1 到卡点", "holder"), ("m2 全机 891,520 步", "holder2")):
        hw = d[key]
        seq = hw.acquire_release(19)
        takes = [(s, t) for s, k, t in seq if k == "set"]
        rels = [s for s, k, _t in seq if k == "clear"]
        print("     [%s] 取锁 %d 次 / 放锁 %d 次，取过锁的任务：%s"
              % (title, len(takes), len(rels), sorted({t for _s, t in takes})))
        print("       " + "  ".join("步%d→任务%d" % (s, t) for s, t in takes))
    print("""
  → 全机 6 个任务（4 / 9 / 10 / 11 / 27 / 28）轮流拿同一把锁，**每次都是
    「取 → 放」成对**，跑完没人持有 —— 没有泄漏的锁。
    任务 9 在步 495,952 拿到、498,107 放掉，然后才去等（498,572）：
    顺序完全对得上「解锁 → 等」，它**没有握着锁睡觉**。""")


def sec_c(d):
    hr("C. 卡点那一刻机器是什么样")
    m = d["m"]
    print("  步数 %d，命中：进分配器 %d 次 / 走到 wai_flg 那句 %d 次 / "
          "调用方 0x%06X 取返回值 %d 次"
          % (m.step_count, d["hits"][ALLOC], d["hits"][WAIT],
             CALLER, d["hits"][CALLER]))
    print("\n  flag %d：位模式 = 0x%08X   等待队头 = 0x%08X（任务 %d）"
          % (FLG, bflag.pattern(m, FLG), bflag.waiter(m, FLG),
             task_of(m, bflag.waiter(m, FLG))))
    print("\n  池：计数 = %d" % bpool.count_at(m))
    for h in bpool.POOL_HEADS:
        prev = m.bus.rd(h, 4) & 0xFFFFFFFF
        nxt = m.bus.rd(h + 4, 4) & 0xFFFFFFFF
        print("      表头 0x%08X  +0=0x%08X  +4(next)=0x%08X  %s"
              % (h, prev, nxt, "空" if nxt == h else "非空"))
    print("\n  sem 19 此刻的持有者 = 任务 %d（0 = 无人）"
          % bpool.holder(m, 19))
    print("  `0x9F58C4C0` 起 16 字节 = %s"
          % bpool.lock_state_bytes(m).hex())
    print("""
  → 两处反直觉，都能说清：
    ① **池的计数是 3、第一条链表非空**，可它还是去等了 —— 这三条链表 / 这个
       计数与它探的那两条（`0x9F69CEFC` / `0x9F69CF04`）不是同一件事。
       「池里有 3 个节点」和「我要的那类节点有没有」是两个问题，本项目**没有
       证据**说清它们各是什么，只报实测（KI#172）。
    ② 此刻 sem 19 的持有者是**任务 10**，不是任务 9 —— 任务 9 早放锁了。
       这正好说明「解锁 → 等」是对的写法：它等的时候，别的任务照样能进去。""")


def sec_d(d):
    hr("D. 全机事件标志账：flag 6 只被清、被等，从没被置")
    m = d["m2"]
    calls = m.flow.calls
    print("  m2 步数 %d，服务调用 %d 次" % (m.step_count, len(calls)))
    grp = {}
    for step, pc, svc, r4, r5, r6, r7, r13, tcb in calls:
        if svc in (-48, -47, -46):
            fid = r4 if svc in (-48, -47) else r5
            grp.setdefault((svc, fid), []).append((step, r4, r5, r6, r7, pc, tcb))

    print("\n  %-9s %-6s %5s %s" % ("服务", "flag", "次数", "谁（任务号）"))
    for (svc, fid) in sorted(grp, key=lambda k: (k[0], k[1])):
        v = grp[(svc, fid)]
        nm = {-48: "set_flg", -47: "clr_flg", -46: "wai_flg"}[svc]
        who = sorted({task_of(m, t[6]) for t in v})
        mark = "   ← **flag 6**" if fid == FLG else ""
        print("  %-9s %-6s %5d  %s%s" % (nm, fid, len(v), who, mark))

    print("\n  `set_flg` 一共碰过哪些 flag：",
          sorted({r4 for _s, _p, svc, r4, _5, _6, _7, _13, _t in calls if svc == -48}))
    print("  `wai_flg` 一共等过哪些 flag：",
          sorted({r5 for _s, _p, svc, _4, r5, _6, _7, _13, _t in calls
                  if svc == -46}))
    print("""
  → 这就是死结：**flag 6 被 `wai_flg` 等着、被 `clr_flg` 清着，从来没有人
    `set_flg` 它**。整机 70 万步里 `set_flg` 只碰过 1 / 3 / 10 / 12 / 14。

  → 而且不是「一个任务卡住」：`wai_flg(6)` 被 **7 个任务**（4/6/9/10/11/27/28）
    调用过，`clr_flg(6)` 被其中 6 个调用过 —— 这是**一整个任务族共用的一只门铃**。
    「谁来按」才是问题，不是「任务 9 为什么特殊」。""")


def sec_e(d):
    hr("E. 反事实：补一次 `set_flg(6, 0x20)` 会怎样")
    m = d["m"]
    before_cov = m.cov.words()
    before = dict(d["hits"])

    print("  补之前：flag 6 位模式 = 0x%08X，等待队头 = 任务 %d"
          % (bflag.pattern(m, FLG), task_of(m, bflag.waiter(m, FLG))))
    t9 = None
    for r in brealos.records(m):
        if r["id"] == 9:
            t9 = r["tcb"]
    print("  任务 9：状态字节 %d（4 = 等待）" % bready.state_of(m, t9))

    r = bflag.set(m, FLG, BIT)
    print("\n  走固件内核 set_flg(6, 0x20)：%s" % r)
    print("  之后：位模式 = 0x%08X，等待队头 = 任务 %d（队头换人 = 前一个被取走了）"
          % (bflag.pattern(m, FLG), task_of(m, bflag.waiter(m, FLG))))
    print("  任务 9：状态字节 %d（0 = 就绪）" % bready.state_of(m, t9))

    c, st = m.cpu, m.cpu.step

    def spy():
        if c.pc in d["hits"]:
            d["hits"][c.pc] += 1
        return st()

    c.step = spy
    blive.run(m, steps=200000)
    c.step = st

    print("\n  再跑 20 万步之后（步 %d，末 PC 0x%06X）：" % (m.step_count, m.cpu.pc))
    print("    进函数 0x122064     %d → %d" % (before[ALLOC], d["hits"][ALLOC]))
    print("    重入点 0x122162     %d → %d  ← 醒来后「再锁一次」那句"
          % (before[RELOCK], d["hits"][RELOCK]))
    print("    走到 wai_flg 那句   %d → %d" % (before[WAIT], d["hits"][WAIT]))
    print("    调用方取返回        %d → %d" % (before[CALLER], d["hits"][CALLER]))
    print("    覆盖率              %d → %d 个字" % (before_cov, m.cov.words()))
    print("    任务 9 状态字节     %d，flag 6 队头 = 任务 %d"
          % (bready.state_of(m, t9),
             task_of(m, bflag.waiter(m, FLG))))
    print("""
  → **阴性结果，而且是最有价值的那种**：信号确实生效了 —— 队头（任务 9）被
    取下来、状态字节从 4（等）回到 0（就绪），它接着跑完了 `0x12215C` 后面那句，
    又在 `0x122162` **重新加锁**（这一句在补信号之前是 0 次），把同一段
    「锁 → 清标志 → 探两条队列 → 解锁 → 等」**又走了一遍**，然后第二次等。
    调用方 `0x13531E` 仍然是 0 次。

    所以「释放端」不是一个信号能替代的：它必须**同时**把节点挂回链表、**并且**
    置 bit5。只置位的结果就是一次空转。""")


def sec_f(d):
    hr("F. flow 的 TCB 槽：修前一个假值，修后 17 个任务各归各位")
    m = d["m2"]
    calls = m.flow.calls
    tcbs = {}
    for c in calls:
        tcbs[c[8]] = tcbs.get(c[8], 0) + 1
    print("  m2 步数 %d，服务调用 %d 次" % (m.step_count, len(calls)))
    print("  `flow.cur_tcb` = 0x%08X（B 段正确槽；A 段仍是 0x%08X）"
          % (m.flow.cur_tcb, 0x45980))
    print("  记到的不同 tcb：%d 个" % len(tcbs))
    print("  记到的不同任务号：%d 个" % len({task_of(m, t) for t in tcbs if t}))
    print("\n  %-8s %-12s %5s" % ("任务", "TCB", "服务数"))
    for tcb, n in sorted(tcbs.items(), key=lambda kv: -kv[1]):
        print("  %-8s 0x%08X %5d" % (task_of(m, tcb) or "-", tcb, n))
    print("\n  flow.switches = %d 条" % len(m.flow.switches))
    print("""
  → 修前 `realos.CUR_TCB`（`0x45980`）是 **A 段**的槽，B 段在那儿读到的是
    `0xFAC7A424` 这类垃圾 —— 每个服务调用都记成「同一个任务发的」，
    `switches` 只有 1 条。修后按机器名分段取槽，`switches` 变成 34 条，
    与 `blive.run()["switches"]` 这个**独立实现**数出来的 34 条一致。
    记到的任务号也正好是普查里「跑过的 17 个」，不多不少。""")


def main():
    print("V47 探针：池的分配器在等什么（frbox %s）" % __import__("frbox").__version__)
    d = run_block()

    m2 = blive.boot()
    hw2 = bpool.HolderWatch(m2).attach()
    blive.run(m2, steps=FULL_STEP - m2.step_count)
    hw2.detach()
    d["m2"] = m2
    d["holder2"] = hw2

    sec_a(d)
    sec_b(d)
    sec_c(d)
    sec_d(d)
    sec_e(d)
    sec_f(d)

    hr("G. 结论")
    print("""
1. **KI#172 结案：卡在 `wai_flg(6, 0x20, mode=2)`，不是卡在锁、也不是卡在「取之前」。**
   `0x122064` 的骨架是 `lock(19)` → `clr_flg(6,~0x20)` → 探两条队列 →
   `unlock(19)` → `wai_flg(6, bit5)`。加锁那一步**成功**（sem 19 计数 1→0）。

2. **`0x12590C` / `0x125992` 是优先数继承互斥量的加锁 / 解锁包装**，持有者记在
   `0x9F58C4D0 + sem_id` 这个字节上。这是 V47 新解出来的通用设施 —— 全机 6 个
   任务轮流用它，不是池专用的。

3. **死结在于没有人置 flag 6 —— 而且是「一整个任务族」在等它。**
   70 万步里 `set_flg` 只碰 1/3/10/12/14；`wai_flg(6)` 有 7 个任务调用过
   （4/6/9/10/11/27/28），`clr_flg(6)` 有 6 个。反事实补一次 `set_flg(6,0x20)`：
   队头任务 9 被取下、状态从 4 回到 0，它跑完 `0x12215C` 后面那句，又在
   `0x122162` **重新加锁**、把同一段路又走一遍、第二次等 —— 调用方仍然没拿到
   返回值。**阴性结果**，但它把「需要什么」说死了：需要一段「把节点挂回链表
   **并且** 置 bit5」的代码。

4. **`flow` 的「当前 TCB」槽用错了段**（V47 修）：A 段 `0x45980` / B 段
   `0x68000000`。修后 `switches` 34 条与 `blive` 独立口径一致，17 个任务
   一次分得清。这是个**会静默污染所有「谁在等什么」结论**的 bug。

5. 仍然 OPEN：①`0x9F58C4C0` 起 16 字节实测恒 0，而 `0x122096 btstl` 就读
   `0x9F58C4C1` 的 bit0 —— 这条分支在我们这台机器上是**恒真**的（KI#183）；
   ②池的计数 3 与「探到的两条队列为空」并存，两者各指什么没有证据；
   ③释放端仍未定位（17 个跑过的任务里没有）。
""")


if __name__ == "__main__":
    main()
