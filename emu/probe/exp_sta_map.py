#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_sta_map.py —— ISR → 回调 → sta_tsk → 任务 的静态可达图

背景（V11 结论边界）：固件自己的 REALOS 已经能在 frbox 上换任务了，但 13 个任务里
只有 3 个上场。其余 10 个的上下文要靠 `sys_sta_tsk` 去建，而「谁会调 sta_tsk」
一直没查清 —— V11 只数了个总数（A 段 108 处）。

本脚本不解「哪条中断线对应哪个硬件」，只回答一个能静态回答的问题：

    如果第 N 号向量响了，它这条链上有没有可能把某个任务建起来？

方法（四步，判据就写在代码里）：

  1. **带数据流的递归下降**：从复位入口 / 48 个向量入口 / 13 个任务入口出发。
     普通线性反汇编遇到数据会错位；这里顺着 `call`（含 `call @rN` 的已知常量目标）
     和条件分支扩散，并对每条路径做「寄存器常量」的前向合并（must-equal）。
  2. **服务号**：`int #0x40` 时 r12 的常量就是服务号（负数，补码）。
  3. **参数**：`sta_tsk` 的 tsk_id 在 r4（V11 从调用点指令坐实，见 KNOWN_ISSUES 40）。
  4. **回调注册**：任何 `st rX,@rY` 且地址落在 0x3F000–0x3F0FF、值已知 → 记一笔。
     这条把「ISR 只查槽、槽里存谁」接上，ISR 才连得到回调。

输出：每个向量 → 回调 → 能拉起的任务号；以及拉不起来的向量缺哪一环。

用法：cd emu && python probe/exp_sta_map.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)


def _fw(name):
    """固件路径：优先 FRBOX_FW_DIR，其次仓库 data/，最后退回 Java 仿真台目录。"""
    cands = []
    env = os.environ.get("FRBOX_FW_DIR")
    if env:
        cands.append(os.path.join(env, name))
    root = os.path.dirname(_EMU)
    cands.append(os.path.join(root, "data", name))
    cands.append(os.path.join(os.path.dirname(root), "nft", "Java", "Emulator", name))
    for c in cands:
        if os.path.exists(c):
            return c
    raise SystemExit("找不到固件 %s，试过：%s" % (name, cands))


sys.stdout.reconfigure(encoding="utf-8")

import fr_emu

TABLE = fr_emu.TABLE
ROM = open(_fw("A3000111.bin"), "rb").read()
BASE = 0x80000
END = BASE + len(ROM)
TBR = 0x000FFC00
FALLBACK = 0x000FE250

INS = {}          # addr -> (mnem, vals, size)
FUNC = {}         # entry -> set(addr)
EDGES = {}        # entry -> set(callee)
SVCS = []         # (pc, entry, svc, r4, r5, r6, r7)
STORES = []       # (pc, entry, addr, val)
NOTES = []        # 诊断

WRITE_MNEM = {"mov", "movn", "extsb", "extsh", "add", "add2", "addn", "addn2",
              "addc", "sub", "subc", "subn", "and", "or", "eor", "andn", "orn",
              "eorn", "mul", "mulu", "div0s", "div0u", "div1", "div2", "div3",
              "div4s", "lsl", "lsr", "asr", "lsl2", "lsr2", "asr2", "ld", "ldub",
              "ldsb", "lduh", "ldsh", "dmov", "dmovb", "dmovh", "dmovuh",
              "xchb", "bandl", "bandh", "borl", "borh", "beorl", "beorh",
              "btstl", "btsth", "bclr", "bset", "bnot", "cvtbl", "cvtsd",
              "cvtbs", "cvtbd", "cvtss", "cvtsd"}


def rd32(addr):
    off = addr - BASE
    if off < 0 or off + 4 > len(ROM):
        return None
    return int.from_bytes(ROM[off:off + 4], "big")


def vec_entry(n):
    return rd32(TBR + (255 - n) * 4)


def ivec(addr):
    off = addr - BASE
    if off < 0 or off + 2 > len(ROM):
        return None
    return TABLE.decode(ROM, off)


def jmp_rel(pc, v, key, bits):
    return (pc + 2 + fr_emu.sx(v[key], bits) * 2) & 0xFFFFFFFF


def transfer(st, mnem, v):
    """返回新状态（只保留常量寄存器）。不认识/算不出的写 → 该寄存器变未知。"""
    st = dict(st)
    d = v.get("ri")
    if mnem.startswith("ldi") and d is not None:
        st[d] = v.get("i8", v.get("i20", v.get("i32")))
    elif mnem in ("mov", "movn") and d is not None:
        s = v.get("rs1")
        if s in st:
            st[d] = st[s]
        else:
            st.pop(d, None)
    elif mnem == "extsb" and d is not None:
        if d in st:
            x = st[d] & 0xFF
            st[d] = (x - 0x100) if x & 0x80 else x
        else:
            st.pop(d, None)
    elif mnem == "extsh" and d is not None:
        if d in st:
            x = st[d] & 0xFFFF
            st[d] = (x - 0x10000) if x & 0x8000 else x
        else:
            st.pop(d, None)
    elif mnem in ("add", "add2") and d is not None:
        s = v.get("rs1", v.get("rs2"))
        if d in st and s in st:
            st[d] = (st[d] + st[s]) & 0xFFFFFFFF
        elif d in st and "i8" in v:
            st[d] = (st[d] + (FR_SX(v["i8"], 8) if False else v["i8"])) & 0xFFFFFFFF
        else:
            st.pop(d, None)
    elif mnem in WRITE_MNEM and d is not None:
        st.pop(d, None)
    return st


FR_SX = fr_emu.sx


def meet(a, b):
    """两条路径汇合：只保留「两边都有且相等」的常量。"""
    out = {}
    for k, v in a.items():
        if b.get(k) == v:
            out[k] = v
    return out


def analyze(entry, limit=6000):
    """对一个函数做 CFG 上的常量传播；顺带把新发现的函数塞进 work 列表。"""
    if entry in FUNC:
        return
    FUNC[entry] = set()
    EDGES.setdefault(entry, set())
    work = [(entry, {}, 0)]
    seen = {}          # addr -> state（用 meet 收敛）
    body = FUNC[entry]
    while work:
        pc, st, depth = work.pop()
        steps = 0
        while True:
            steps += 1
            if steps > limit:
                NOTES.append("函数 0x%08X 超过 %d 条，截断" % (entry, limit))
                break
            if pc in body:
                old = seen.get(pc)
                if old is not None:
                    new = meet(old, st)
                    if new == old:
                        break
                    seen[pc] = new
                    st = new
                else:
                    seen[pc] = st
            ins = ivec(pc)
            if ins is None:
                break
            mnem, _b, v, size, _w = ins
            body.add(pc)
            INS.setdefault(pc, ins)

            if mnem == "int" and v.get("u8") == 0x40:
                num = st.get(12)
                if num is not None:
                    num &= 0xFFFFFFFF          # extsb 会给出负数，先归一到 32 位
                    if num & 0x80000000:
                        num -= 0x100000000
                    SVCS.append((pc, entry, num, st.get(4), st.get(5),
                                 st.get(6), st.get(7)))
                # 系统调用返回后 r0/r4..r7/r12 都可能被改
                st = dict(st)
                for r in (0, 4, 5, 6, 7, 12):
                    st.pop(r, None)

            if mnem == "st" and "rj" in v and "ri" in v:
                addr = st.get(v["rj"])
                val = st.get(v["ri"])
                if addr is not None and val is not None:
                    STORES.append((pc, entry, addr, val))

            nxt = pc + size
            if mnem in ("ret", "ret:d", "reti", "jmp", "jmp:d"):
                if mnem in ("jmp", "jmp:d"):
                    tgt = st.get(v.get("ri", -1))
                    if tgt is not None and BASE <= tgt < END:
                        NOTES.append("函数 0x%08X 尾跳 0x%08X" % (entry, tgt))
                        EDGES[entry].add(tgt)
                        pending.append(tgt)
                break
            if mnem in ("call", "call:d"):
                if "rel12" in v:
                    tgt = jmp_rel(pc, v, "rel12", 11)
                    if BASE <= tgt < END:
                        EDGES[entry].add(tgt)
                        pending.append(tgt)
                else:
                    tgt = st.get(v.get("ri", -1))
                    if tgt is not None and BASE <= tgt < END:
                        EDGES[entry].add(tgt)
                        pending.append(tgt)
                    else:
                        NOTES.append("0x%08X 间接调用目标未知" % pc)
                # 调用返回后按 ABI 保守清掉易失寄存器
                st = dict(st)
                for r in (0, 1, 2, 3, 4, 5, 6, 7, 12, 13):
                    st.pop(r, None)
                pc = nxt
                continue
            if mnem in ("bra", "bra:d") and "rel9" in v:
                pc = jmp_rel(pc, v, "rel9", 8)
                continue
            if mnem.startswith("b") and "rel9" in v:
                work.append((jmp_rel(pc, v, "rel9", 8), dict(st), depth + 1))
                pc = nxt
                st = transfer(st, mnem, v)
                continue
            st = transfer(st, mnem, v)
            pc = nxt


pending = []


def build(seeds):
    for s in seeds:
        pending.append(s)
    while pending:
        e = pending.pop(0)
        if e in FUNC:
            continue
        analyze(e)


def closure(entry):
    seen, stack = set(), [entry]
    while stack:
        e = stack.pop()
        if e in seen:
            continue
        seen.add(e)
        stack.extend(EDGES.get(e, ()))
    return seen


def main():
    seeds = [0x000FC000, 0x00081336, 0x00081338]
    vecs = {}
    for n in range(0x10, 0x40):
        e = vec_entry(n)
        vecs[n] = e
        if e and BASE <= e < END:
            seeds.append(e)
    tasks = []
    for i in range(13):
        rec = 0x000FE010 + i * 0x1C
        ident = rd32(rec + 4) or 0
        entry = rd32(rec + 0x10) or 0
        tid = (ident >> 16) & 0xFFFF
        if entry:
            tasks.append((tid, entry))
            seeds.append(entry)
    build(seeds)

    print("=" * 78)
    print("A 段 1.11 · 带数据流的递归下降")
    print("  函数 %d 个 / 指令 %d 条 / 调用边 %d 条 / 已知目标存储 %d 笔"
          % (len(FUNC), len(INS), sum(len(v) for v in EDGES.values()), len(STORES)))
    print("  种子：复位 1 + 向量 %d + 任务 %d" % (len(vecs), len(tasks)))

    by = {}
    for pc, entry, num, a4, a5, a6, a7 in SVCS:
        by.setdefault(num, []).append((pc, a4, a5, a6, a7, entry))
    print("\n服务调用普查（可达代码里的 int #0x40，R12 已知）")
    for num in sorted(by, key=lambda x: -len(by[x]))[:14]:
        print("  %-5d ×%-4d 首处 0x%08X" % (num, len(by[num]), by[num][0][0]))

    sta = [(pc, a4, a5, a6, a7, e) for pc, e, num, a4, a5, a6, a7 in SVCS if num == -23]
    print("\n" + "=" * 78)
    print("sta_tsk（服务号 -23）调用点 %d 处" % len(sta))
    known, unknown = {}, []
    for pc, a4, a5, a6, a7, entry in sorted(sta, key=lambda t: t[0]):
        (known.setdefault(a4, []).append((pc, entry)) if a4 is not None
         else unknown.append((pc, entry)))
    print("  %-6s %-8s %s" % ("任务号", "处数", "首处（所在函数）"))
    for tid in sorted(known, key=lambda x: (x is None, x)):
        pcs = known[tid]
        print("  %-6s %-8d 0x%08X（0x%08X）"
              % (tid, len(pcs), pcs[0][0], pcs[0][1]))
    if unknown:
        print("  参数未定 %d 处：%s" % (len(unknown),
              " ".join("0x%08X" % p for p, _ in unknown[:10])))

    print("\n" + "=" * 78)
    print("回调槽写入（st rX,@rY，地址 0x3F000–0x3F0FF）")
    regs = {}
    for pc, entry, addr, val in STORES:
        if 0x3F000 <= addr < 0x3F100:
            regs.setdefault(addr, []).append((val, pc, entry))
    for slot in sorted(regs):
        vals = regs[slot]
        vs = sorted({v for v, _, _ in vals})
        print("  槽 0x%05X ← %-30s （%d 处，首处 0x%08X）"
              % (slot, "/".join("0x%08X" % v for v in vs[:4]), len(vals), vals[0][1]))

    print("\n" + "=" * 78)
    print("向量 → ISR → 回调槽 → 回调 → 能拉起的任务")
    rows = []
    for n in sorted(vecs):
        isr = vecs[n]
        if not isr or not (BASE <= isr < END):
            continue
        slots = []
        for pc in sorted(FUNC.get(isr, ())):
            mnem, _b, v, _s, _w = INS[pc]
            if mnem.startswith("ldi"):
                x = v.get("i20", v.get("i8", v.get("i32")))
                if x is not None and 0x3F000 <= x < 0x3F100:
                    slots.append(x)
        cbs = []
        for s in slots:
            for val, _pc, _e in regs.get(s, []):
                if val not in cbs:
                    cbs.append(val)
        reach = set()
        for cb in cbs:
            reach |= closure(cb)
        tids = sorted({a4 for pc, a4, a5, a6, a7, e in sta
                       if a4 is not None and e in reach})
        kind = "兜底" if isr == FALLBACK else ("stub" if slots else "特殊")
        rows.append((n, isr, kind, slots, cbs, tids))
        if cbs or tids:
            print("  vec 0x%02X  0x%08X  %-4s 槽 %-16s 回调 %-30s → %s"
                  % (n, isr, kind, ",".join("0x%X" % s for s in slots) or "-",
                     ",".join("0x%X" % c for c in cbs[:3]) or "-", tids or "-"))
    print("\n  有回调的向量 %d / 48；回调链上带 sta_tsk 的向量 %d"
          % (sum(1 for r in rows if r[4]), sum(1 for r in rows if r[5])))
    alltid = set()
    for r in rows:
        alltid |= set(r[5])
    print("  静态上限：中断链能拉起的任务 %s（%d 个）" % (sorted(alltid), len(alltid)))

    # 反向：每个任务的入口被谁调到（含 sta_tsk 之外的可能启动方式）
    print("\n" + "=" * 78)
    print("任务入口的调用者（call 边反查）")
    rev = {}
    for e, tgts in EDGES.items():
        for t in tgts:
            rev.setdefault(t, set()).add(e)
    for tid, entry in sorted(tasks):
        callers = sorted(rev.get(entry, ()))
        print("  任务 %-2d 入口 0x%08X ← %s"
              % (tid, entry, ", ".join("0x%08X" % c for c in callers) or "（无 call 边）"))

    if NOTES:
        print("\n诊断（前 12 条）")
        for s in NOTES[:12]:
            print("  " + s)

    # ------------------------------------------------ 6. 标志依赖：谁等 / 谁置
    print("\n" + "=" * 78)
    print("标志依赖：任务在等哪个 (flag,位)，中断在置哪个")
    # 每个任务入口的可达函数集；每个向量回调的可达函数集
    tset = {tid: closure(entry) for tid, entry in tasks}
    vset = {}
    for n, isr, kind, slots, cbs, tids in rows:
        s = set()
        for cb in cbs:
            s |= closure(cb)
        vset[n] = s
    # 反查：函数 → 属于哪个任务 / 哪个向量
    owner_task, owner_vec = {}, {}
    for tid, s in tset.items():
        for f in s:
            owner_task.setdefault(f, tid)
    for n, s in vset.items():
        for f in s:
            owner_vec.setdefault(f, n)

    waits, sets = [], []
    for pc, entry, num, a4, a5, a6, a7 in SVCS:
        if num == -46 and a5 is not None:                      # wai_flg（-21 是 ext_tsk）
            waits.append((owner_task.get(entry), entry, pc, a5, a6, a7))
        elif num in (-48, -47) and a4 is not None:             # set_flg / clr_flg
            kind = "set" if num == -48 else "clr"
            sets.append((kind, owner_task.get(entry), owner_vec.get(entry),
                         entry, pc, a4, a5))
    print("  wai_flg 站点 %d 个（有归属任务 %d）；set_flg/clr_flg 站点 %d 个"
          % (len(waits), sum(1 for w in waits if w[0]), len(sets)))
    bytask = {}
    for tid, entry, pc, flg, ptn, mode in waits:
        if tid is not None:
            bytask.setdefault(tid, []).append((pc, flg, ptn, mode))
    print("\n  任务的首个等待（按地址序，最多 4 个）：")
    for tid in sorted(bytask):
        ws = sorted(bytask[tid])[:4]
        print("    任务 %-2d: %s" % (tid, "  ".join(
            "0x%08X flg%d ptn=0x%X mode=%s" % (pc, f, (p or 0), m) for pc, f, p, m in ws)))

    print("\n  谁来满足这些等待（跨任务/跨中断反查 set_flg）：")
    for tid in sorted(bytask):
        for pc, flg, ptn, mode in sorted(bytask[tid])[:2]:
            cands = []
            for kind, otid, ovec, entry, spc, sflg, sbits in sets:
                if sflg != flg or kind != "set":
                    continue
                if ptn and sbits is not None and not (sbits & ptn):
                    continue
                who = ("I%02X" % ovec if ovec is not None else
                       ("T%d" % otid if otid is not None else "?"))
                cands.append("%s@0x%X:0x%X%s" % (who, spc, sbits if sbits is not None else 0,
                                                 kind == "clr" and "(清)" or ""))
            print("    任务 %-2d @0x%08X flg%d ptn=0x%X ← %s"
                  % (tid, pc, flg, ptn or 0, " ".join(cands[:6]) or "（无人置位）"))


def icr_scan():
    """第 7 节：ICR 级别分布 + 「0x1F 是不是禁用值」的交叉验证。

    静态只能看到「初始化的表里写了 0x1F」。要判它是不是「禁用」，得看两件事：
      ① 48 个 ICR 被写完之后（跑到 0x80506），各级别各有多少个向量；
      ② 级别 0x1F 的向量里，有几个的回调槽在整轮运行中被真正注册过。
    如果「有真 ISR 但回调槽从来没被注册」与「0x1F」完全重合，那 0x1F 就是禁用值
    —— 固件把不用的中断源一律写成最低级别，同时不会有任何代码去挂它的回调。
    """
    from frbox.machine import create
    m = create("d300-a")
    m.load()
    m.run(max_steps=60000, stop_pcs={0x00080506})
    rows = m.irq_table()
    from collections import Counter
    cnt = Counter(r["icr"] for r in rows)
    print("\n" + "=" * 78)
    print("ICR 级别分布（跑到 0x80506，初始化写完 48 个 ICR 之后）")
    for lvl in sorted(cnt):
        vs = [r for r in rows if r["icr"] == lvl]
        unused = [r["vec"] for r in vs if r["isr"] == m.fallback_vec]
        print("  ICR=0x%02X ×%2d  %s%s"
              % (lvl, len(vs), " ".join("%02X" % r["vec"] for r in vs),
                 ("   其中兜底：%s" % " ".join("%02X" % v for v in unused)) if unused else ""))

    # 跑到死锁，看回调槽到底注册了哪些
    m.boot_realos(1)
    n0 = m.cpu.int40
    idle = 0
    seen = {}                     # 整个运行期间注册过的槽（不是只看最后一眼）
    while m.step_count < 200000:
        m.run(max_steps=5000, spin_guard=False)
        for r in rows:
            for sl in r["slots"]:
                v = m.bus.rd(sl, 4)
                if v:
                    seen.setdefault((r["vec"], sl), v)
        if m.cpu.pc == 0x000FE510 and m.cpu.int40 == n0:
            idle += 1
            if idle >= 2:
                break
        else:
            idle = 0
            n0 = m.cpu.int40
    live = []
    for (v, sl), val in sorted(seen.items()):
        icr = next(r["icr"] for r in rows if r["vec"] == v)
        live.append((v, icr, [val]))
    print("\n运行到死锁后，回调槽被注册过的向量 %d 个：" % len(live))
    for v, icr, vals in live:
        print("  vec 0x%02X ICR=0x%02X 槽值 %s"
              % (v, icr, "/".join("0x%08X" % x for x in vals)))
    hi = [r for r in rows if r["icr"] == 0x1F]
    hi_real = [r["vec"] for r in hi if r["isr"] != m.fallback_vec]
    hi_live = [v for v, icr, _ in live if icr == 0x1F]
    print("\n  判定：ICR=0x1F 的向量 %d 个，其中 %d 个有真 ISR；"
          "这些有真 ISR 的里面，回调槽被注册过的 = %d 个"
          % (len(hi), len(hi_real), len(hi_live)))
    print("  → %s" % ("0x1F = 禁用值（有 ISR 但整套运行里从没人挂回调）"
                      if not hi_live else
                      "0x1F 不完全是禁用值，需要再看：%s" % hi_live))


if __name__ == "__main__":
    main()
    icr_scan()
