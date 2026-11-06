# -*- coding: utf-8 -*-
"""V43 探针：两次「取缓冲区」为什么都返回 0 —— 池地址在总线上根本没映射。

    cd emu && python probe/exp_bpool.py > ../results/frbox/bpool.txt

两档对照：`ram-sdram` 1MB（V42 及以前的口径）与 2.5MB（V43 修好的口径）。
每一档都**从 boot 重新跑一遍**（同一台机器跑第二遍会从停住的现场继续，
数字没有可比性 —— 本探针第一版就在这里栽过）。
"""
import sys
sys.path.insert(0, '.')

import fr_dis
from frbox import blive, bpool, machine

STEPS = 700000


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def measure(sdram_size, steps=STEPS):
    """按指定尺寸建机 → boot → 跑 → 收集所有观测量。返回一个 dict。"""
    with bpool.sdram_override(sdram_size):
        m = blive.boot()
        pw = bpool.PoolWatch(m).attach()
        nw = bpool.NullWatch(m).attach()
        c, st = m.cpu, m.cpu.step
        cnt = {a: 0 for a in bpool.WATCH}
        callers, last = [], [0]

        def spy():
            pc = c.pc
            if pc in cnt:
                cnt[pc] += 1
                if pc == bpool.ALLOC_BUF:
                    callers.append((m.step_count, last[0]))
            last[0] = pc
            return st()

        c.step = spy
        res = blive.run(m, steps=steps)
        c.step = st
        nw.detach()
        pw.detach()

        out = {"size": sdram_size, "m": m, "res": res, "cnt": cnt,
               "callers": callers, "step": m.step_count,
               "regions": list(m.bus.regions),
               "init": pw.init_at(), "nodes": pw.nodes_linked(),
               "track": pw.count_track(), "write_list": pw.writes,
               "n_writes": len(pw.writes), "n_reads": len(pw.reads),
               "null_hits": len(nw.hits), "nulls": nw.nulls(),
               "count": bpool.count_at(m),
               "pc_end": m.cpu.pc, "icr": m.bus.rd(blive.DISPATCH_ICR, 1) & 0xFF,
               "tasks": res["tasks"], "switches": res["switches"],
               "pool_mapped": bpool.pool_mapped(m), "sdram": bpool.sdram(m),
               "unmapped_w": len(m.bus.unmapped_writes),
               "unmapped_r": len(m.bus.unmapped_reads)}
        # 跑完再测「写得进 / 读得回」（哨兵会覆盖池头，所以放在跑完之后）
        out["rt"] = [(a,) + bpool.roundtrip(m, a) for a in
                     (bpool.POOL_COUNT, bpool.POOL_HEADS[0], bpool.NODES[0])]
    return out


def sec_a(d):
    hr("A. 池的地址在总线上是什么：find() / 写后读回")
    print("  池的地址（全部是固件里的立即数，不是我们编的）：")
    print("    计数      0x%08X" % bpool.POOL_COUNT)
    for a in bpool.POOL_HEADS:
        print("    链表头    0x%08X" % a)
    for a in bpool.NODES:
        print("    节点      0x%08X（三个，间隔 0x221C）" % a)
    print()
    for size in (0x00100000, 0x00280000):
        r = d[size]
        print("  ---- ram-sdram 0x%X（%s口径）----"
              % (size, "V42-" if size == 0x00100000 else "V43"))
        print("    总线区表 %d 段：" % len(r["regions"]))
        for reg in r["regions"]:
            mark = "  ←" if reg.name == "ram-sdram" else ""
            print("      0x%08X..0x%08X  %-11s %s%s"
                  % (reg.base, reg.end, reg.kind, reg.name, mark))
        print("    跑完之后写哨兵 0xA1B2C3D4 再读回：")
        for a, reg, back in r["rt"]:
            print("      0x%08X 区=%-11s 读回 = 0x%08X%s"
                  % (a, reg.name if reg else "**未映射**", back,
                     "" if back == 0xA1B2C3D4 else "   ← 丢了"))
        print("    池四个地址全在总线里？%s；ram-sdram 实测 = 0x%X + 0x%X"
              % ("是" if r["pool_mapped"] else "**不是**",
                 r["sdram"][0], r["sdram"][1]))
        print("    未映射写 %d 笔 / 未映射读 %d 个地址"
              % (r["unmapped_w"], r["unmapped_r"]))
        print()
    print("""  → 1MB 时 `0x9F69CE98` 落在 `0x9F680000` 之后：总线区表里根本没有它，
    写被丢进 `unmapped_writes`、读**恒返回 0**。分配器每次看到的都是「空池」。
    2.5MB 时池的四个地址全部落在 ram-sdram 里，哨兵写得进、读得回。""")


def sec_b(d):
    hr("B. 池的初始化与装填：两档一模一样（差别不在建池）")
    for size in (0x00100000, 0x00280000):
        r = d[size]
        print("  ---- ram-sdram 0x%X ----" % size)
        print("    池地址被写 %d 笔 / 被读 %d 笔" % (r["n_writes"], r["n_reads"]))
        print("    空链表初始化（head->next = head）%d 次，步 %s"
              % (len(r["init"]), r["init"]))
        print("    串进池里的节点 %d 个：%s"
              % (len(r["nodes"]), " ".join("0x%08X" % v for v in r["nodes"])))
        tr = r["track"]
        if tr:
            print("    计数被写 %d 次，首笔在步 %d、写成 %d；末笔写成 0x%08X"
                  % (len(tr), tr[0][0], tr[0][2], tr[-1][2]))
        print("    跑完读回计数 = 0x%08X（有符号 %d）"
              % (r["count"], bpool.as_signed(r["count"])))
        print("    前 8 笔写：")
        for s, pc, a, v, n, ra in r["write_list"][:8]:
            tag = "  ← 计数" if a == bpool.POOL_COUNT else (
                "  ← 自指" if v == a else "")
            print("      步 %7d pc=0x%06X 0x%08X ← 0x%08X n=%d%s"
                  % (s, pc, a, v, n, tag))
        print()
    print("""  → 两档的初始化、装填、计数写成 3 **逐笔相同**：池不是没建起来，是建在
    一块「写不进去」的地址上。1MB 那档多出来的 32 笔写，全在步 497,981 之后 ——
    那是分配器一次次尝试取节点、一次次写不进去。""")


def sec_c(d):
    hr("C. NULL 还发不发生：0x12216E `mov r4,r8` 的 r4 序列")
    for size in (0x00100000, 0x00280000):
        r = d[size]
        print("  ---- ram-sdram 0x%X：执行 %d 次，其中 r4 = 0 的 %d 次"
              % (size, r["null_hits"], len(r["nulls"])))
        for s, r4, r8, cnt in r["nulls"][:6]:
            print("      步 %7d r4=0x%08X → r8=0x%08X  当时池计数（读回）=0x%08X"
                  % (s, r4, r8, cnt))
    print("""  → 1MB：唯一一次执行就是 NULL（`r4 = 0`），此后调用方不判空就 `memmove`。
    2.5MB：这条指令**一次都不执行** —— 不是因为「取到了真节点」，而是因为这次
    分配根本没走到「判空 → 返回」那一步（D 段：进去 1 次、一次没回来）。
    能确定的只是：**「取到 0」这件事不再发生**。""")


def sec_d(d):
    hr("D. 整机对照：同一个任务体，两档走出两条完全不同的路")
    print("  %-9s %6s %-11s %-6s %6s %8s %8s %8s"
          % ("口径", "换手", "末 PC", "ICR", "入口", "取节点", "取缓冲", "memmove"))
    for size in (0x00100000, 0x00280000):
        r = d[size]
        print("  0x%-7X %6d 0x%08X 0x%02X   %6d %8d %8d %8d"
              % (size, len(r["switches"]), r["pc_end"], r["icr"],
                 len(r["tasks"]), r["cnt"][bpool.ALLOC_NODE],
                 r["cnt"][bpool.ALLOC_BUF], r["cnt"][bpool.MEM_MOVE_PC]))
    print()
    for size in (0x00100000, 0x00280000):
        r = d[size]
        print("  0x%X 跑到过的入口：%s" % (size, r["tasks"]))
        if r["callers"]:
            print("      0x122D32 的调用者（步 / 进入前 PC）：%s"
                  % ", ".join("%d/0x%X" % t for t in r["callers"][:4]))
        print("      最后一次换手在步 %s"
              % (r["switches"][-1][0] if r["switches"] else "（没有换手）"))
    print("""
  → 1MB：取缓冲区 11 次全部返回 0，调用方不判空就 `memmove`，把 `0x84..0x2208`
    整段（含 `0x46F` 这条 ICR）写成固件 RAM 内容 → 派发再不受理 → 卡在
    `0x137968` 的 16 位拷贝环里（V41/V42 记的那个「停摆」）。
  → 2.5MB：取缓冲区**一次都没被调到**，全程无 NULL，收尾**回到空闲任务 0x407DC**，
    `0x46F` 保持 0x10。但 `0x122064` 进去那一次也**没有返回**（`0x13531E` 0 次、
    池头一笔写都没发生）—— 任务 9 停在等待状态（TCB 状态字节 0x04），
    派发这次能正常切走，所以整机落到空闲而不是卡死。""")


def sec_e():
    hr("E. 任务体 0x1352EA 里的两条调用")
    with bpool.sdram_override(0x00280000):
        m = blive.boot()
    rom, base = m.bus.rom, m.bus.rom_base
    print("  取节点（0x135316 call 0x122064 → 0x13531E mov r4,r8）：")
    for t in fr_dis.disasm_addr(rom, 0x135316, 0x13532C, base):
        print("    " + t)
    print("\n  取缓冲区（0x135616 call 0x122D32 → 0x135624 mov r4,r9）：")
    for t in fr_dis.disasm_addr(rom, 0x135608, 0x13562A, base):
        print("    " + t)
    print("\n  取节点 0x122064 头部：")
    for t in fr_dis.disasm_addr(rom, bpool.ALLOC_NODE, bpool.ALLOC_NODE + 0x14, base):
        print("    " + t)
    print("\n  取缓冲区 0x122D32 头部：")
    for t in fr_dis.disasm_addr(rom, bpool.ALLOC_BUF, bpool.ALLOC_BUF + 0x14, base):
        print("    " + t)
    print("""
  → 两条是同一套骨架（等 `int #0x40` → 判空 → 取 → 挂链），只是链表不同；
    返回值一律走 r4。调用方 `0x13531E` / `0x13534E` 拿到 r4 之后**都不判空**。""")


def main():
    d = {s: measure(s) for s in (0x00100000, 0x00280000)}
    sec_a(d)
    sec_b(d)
    sec_c(d)
    sec_d(d)
    sec_e()

    hr("结论")
    print("""
1. **KI#171 的真因不是「池被取空」，是「池的地址没映射」**。`0x9F69CE98`
   （计数）/ `0x9F69CEB4` / `0x9F69CEE4` / `0x9F69CEFC`（链表头）落在
   `0x9F680000` 之后，而 V42 及以前 `ram-sdram` 只给了 1MB
   （`0x9F580000 + 0x00100000`）—— 写被丢、读恒 0，分配器**每次**都看到空池。
2. 池本身建得好好的：三个链表头各自自指（空链表语义）、3 个节点被串进去、
   计数写成 3，两档**逐笔相同**。V42 的「模型缺件」和 V43 中途的
   「池只有 3 个节点、只出不进」两个猜测都作废。
3. 把 `ram-sdram` 铺到任务栈区之前（`0x9F580000 + 0x00280000`，2.5MB）之后：
   四个池地址全部可读写（计数读回 3、链表头指向真节点），`0x12216E` 的 NULL
   从 1 次变 0 次，取缓冲区从 11 次变 0 次，整机从「卡在 0x137968」变成
   「回到空闲 0x407DC」。⚠️ 这不等于「分配成功」：`0x122064` 进去那一次没返回，
   卡在它自己的等待里（#172）。
4. 与 V42 的单点反事实 `fix_dst` **互相独立却收敛到同一结果**：那条路是把
   「搬错目的地」这个后果掐掉（换手 34→37、收尾回空闲、入口 17 个、
   `0x46F` 恢复 0x10），这条路是把「取到 NULL」这个起因掐掉。两条独立的路
   指向同一个终态，链条因此闭合：
   **地址未映射 → 取到 NULL → 搬 0 地址 → 踩 ICR → 派发不受理 → 停摆**。
5. 仍然 OPEN：①池之外还有别的未映射区（`0x9F55xxxx`、`0x5C/0x60/0x61/0x6F`、
   EXPEED 窗口外）—— 同一个病根，见 KNOWN_ISSUES #174；②`0x122D5A mov r8,r4`
   的方向与分支行为对不上，见 #173。
""")


if __name__ == "__main__":
    main()
