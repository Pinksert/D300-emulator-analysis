# -*- coding: utf-8 -*-
"""V44 探针：双向链表库 —— 两套实例、以及那块没映射的节点区。

    cd emu && python probe/exp_lists.py > ../results/frbox/lists.txt

四段：
  A. 库本身：五个入口的指令、全固件被引用次数
  B. 套 A（V43 找到的）：头、节点、节点大小、头链
  C. 套 B（V44 找到的）：1000 项节点数组、3003 个节点、三条链各 1001
  D. 补上映映的前后对照：未映射写/读、链表完整性、整机行为

每一段都**从 boot 重新跑一遍**（同一台机器跑第二遍会从停住的现场继续）。
"""
import sys
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8')

import struct
from collections import Counter

import fr_dis
from frbox import blive, bpool, lists, machine

STEPS = 700000


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def rom_refs(rom, target):
    """ROM 里有多少处把这个 32 位常量当作立即数。"""
    hits = []
    for i in range(len(rom) - 3):
        if struct.unpack_from(">I", rom, i)[0] == target:
            hits.append(i)
    return hits


def build(extra=None):
    """按指定 extra_ram 建机并跑。"""
    base = list(machine.D300B.extra_ram)
    if extra is not None:
        machine.D300B.extra_ram = extra
    try:
        m = blive.boot()
        res = blive.run(m, steps=STEPS)
        return m, res
    finally:
        machine.D300B.extra_ram = base


BASE = list(machine.D300B.extra_ram)
NO_NODES = [x for x in BASE if x[2] != "ram-nodes"]


def sec_a():
    hr("A. 这套库：五个入口 + 全固件引用次数")
    m, res = build()
    rom, base = m.bus.rom, m.bus.rom_base
    print("  五个入口的指令（`fr30-opc.c` 语义：**最后一个操作数是目的**）：")
    for a, n, tag in [
        (lists.LIST_INIT, 0x08, "建空链表 head->next = head"),
        (lists.LIST_EMPTY, 0x0C, "is_empty：读 head->next 与 head 比"),
        (lists.LIST_PUSH, 0x0E, "挂链（三步 + 第 4 条指令）"),
        (lists.LIST_NEXT, 0x08, "取下一个节点"),
        (lists.LIST_NEWNODE, 0x0E, "取/新建一个节点"),
    ]:
        print("    ---- 0x%08X  %s ----" % (a, tag))
        for t in fr_dis.disasm_addr(rom, a, a + n, base):
            print("      " + t)
        print()

    print("  全固件引用次数（扫 ROM 里所有 `ldi:32` 形态的立即数）：")
    for a, name in [(lists.LIST_INIT, "0x124F46 建空链表"),
                    (lists.LIST_EMPTY, "0x124F4E is_empty"),
                    (lists.LIST_PUSH, "0x124F5A 挂链"),
                    (lists.LIST_PUSH_TAIL, "0x124F62 ★V43 误当成独立函数"),
                    (lists.LIST_NEXT, "0x124F7A 取下一个"),
                    (lists.LIST_NEWNODE, "0x124FA0 取/新建节点")]:
        hits = rom_refs(rom, a)
        mark = "  ← 0 次！不是独立函数" if not hits and a == lists.LIST_PUSH_TAIL else ""
        print("    %-28s %3d 次%s" % (name, len(hits), mark))
        if hits and len(hits) <= 12:
            print("        %s" % " ".join("0x%X" % (base + h) for h in hits))
    print("""
  → 结论：`0x124F62` 在整个 B 段 ROM 里被引用 **0 次** —— 它不是函数，
    是 `0x124F5A` 的第 4 条指令。**V43 记的「联链」要更正。**""")


def sec_b():
    hr("B. 套 A（V43 找到的）：3 个大节点")
    m, res = build()
    print("  计数 0x9F69CE98 = %d" % bpool.count_at(m, lists.POOL_COUNT))
    print("  三个链表头：")
    for h in lists.POOL_HEADS:
        print("    0x%08X  +0=0x%08X +4=0x%08X +8=0x%08X"
              % (h, m.bus.rd(h, 4), m.bus.rd(h + 4, 4), m.bus.rd(h + 8, 4)))
    print("  三个节点（等距 0x%X = %d 字节）："
          % (lists.POOL_NODE_SIZE, lists.POOL_NODE_SIZE))
    for a in lists.POOL_NODES:
        print("    0x%08X  +0=0x%08X +4=0x%08X +8=0x%08X"
              % (a, m.bus.rd(a, 4), m.bus.rd(a + 4, 4), m.bus.rd(a + 8, 4)))
    n, end = lists.chain_len(m, lists.POOL_HEADS[0])
    print("  链长 %d（3 个节点 + 回到头）" % n)
    n2, _ = lists.chain_len(m, lists.POOL_HEADCHAIN)
    print("  头链 0x%08X → %d 个头" % (lists.POOL_HEADCHAIN, n2))
    print("  另有 %d 个空头（`0x121122` 初始化，各自 head->next = head）"
          % len(lists.POOL_EXTRA_HEADS))


def sec_c():
    hr("C. 套 B（V44 找到的）：1000 项 × 3 条链 = 3003 个节点")
    m, res = build(NO_NODES)
    print("  ⚠️ 这一段用**没有节点区**的口径跑 —— 链是断的，数不出长度。")
    print("  节点区地址（固件自己的立即数，不是我们编的）：")
    print("    三个链表头：0x9F5A0CC0 / 0x9F5A0CC8 / 0x9F5A0CD0")
    print("    头链起点：  0x9F5A0CD8")
    print("    节点区：    0x%08X .. 0x%08X" % lists.nodes_b_region())
    print("    节点大小：  0x%X（%d 字节）" % (lists.NODE_SIZE_B, lists.NODE_SIZE_B))

    # 未映射写里落在节点区的
    recs = [r for r in m.bus.unmapped_writes
            if lists.NODES_B_BASE <= r[0] <= lists.NODES_B_END]
    print("  落在节点区的未映射写：%d 笔" % len(recs))
    pcs = Counter(r[3] for r in recs)
    print("    写点只有：%s"
          % ", ".join("0x%X×%d" % (p, c) for p, c in pcs.most_common(6)))
    addrs = sorted(set(r[0] for r in recs))
    diffs = Counter(addrs[i] - addrs[i - 1] for i in range(1, len(addrs)))
    print("    不同地址 %d，步长分布 %s" % (len(addrs), dict(diffs.most_common(4))))
    vals = Counter(r[2] for r in recs)
    inblk = sum(c for v, c in vals.items() if lists.NODES_B_BASE <= v <= lists.NODES_B_END)
    print("    写值里指向节点区自身的：%d / %d" % (inblk, len(recs)))

    print("""
  → 写点只有 `0x124F5C`（st (r5),r4）与 `0x124F60`（st 4(r5),r0）——
    正是挂链那三步里的前两步。写的是**链表指针**（prev / next）。
    步长 +4 / +16 交替 = 节点 20 字节（+0 prev、+4 next、+8..+0x13 是别的字段）。""")

    # 建链的循环
    rom, base = m.bus.rom, m.bus.rom_base
    print("  建链的循环（0x%08X）：" % lists.BUILD_B_FN)
    for t in fr_dis.disasm_addr(rom, lists.BUILD_B_FN + 0x28,
                                lists.BUILD_B_FN + 0x48, base):
        print("    " + t)
    print("""
  → `ldi:20 #0x3E8` = **1000**，步长 `add r8,#0x14` = **20 字节** ——
    这就是「1000 项定长数组」的出处。""")

    # 注册表
    print("  链表头注册表 0x%08X（补上映映后读回）：" % lists.REGISTRY_B)


def sec_d():
    hr("D. 补上映映：前后对照")
    print("  ---- 口径一：没有节点区（V43 默认）----")
    m1, r1 = build(NO_NODES)
    print("    未映射写 %d 笔 / 未映射读 %d 个地址"
          % (len(m1.bus.unmapped_writes), len(m1.bus.unmapped_reads)))
    for h in lists.LIST_HEADS_B:
        n, end = lists.chain_len(m1, h)
        print("    链头 0x%08X → %d 个节点（末端 0x%08X）" % (h, n, end))
    print("    换手 %d / 末 PC 0x%08X / ICR 0x%02X / 入口 %d"
          % (len(r1["switches"]), m1.cpu.pc,
             m1.bus.rd(blive.DISPATCH_ICR, 1) & 0xFF, len(r1["tasks"])))

    print("  ---- 口径二：补上节点区（V44 默认）----")
    m2, r2 = build()
    print("    未映射写 %d 笔 / 未映射读 %d 个地址"
          % (len(m2.bus.unmapped_writes), len(m2.bus.unmapped_reads)))
    for h in lists.LIST_HEADS_B:
        n, end = lists.chain_len(m2, h)
        nodes = lists.chain_nodes(m2, h)
        bad = lists.is_bidirectional(m2, nodes)
        print("    链头 0x%08X → %d 个节点，双向不配对 %d，末端 0x%08X"
              % (h, n, bad, end))
    print("    换手 %d / 末 PC 0x%08X / ICR 0x%02X / 入口 %d"
          % (len(r2["switches"]), m2.cpu.pc,
             m2.bus.rd(blive.DISPATCH_ICR, 1) & 0xFF, len(r2["tasks"])))
    print("    注册表 %s"
          % " ".join("0x%08X" % v for v in lists.registry(m2)))
    print("""
  → **控制流数字一个都没变**（换手 34 / 末 PC 0x407DC / ICR 0x10 / 入口 17）：
    节点区是**纯数据区**，补对它不会改变调度，只把链表补完整。
    这正是理想结果 —— 若是控制流岔路，换手数会变。""")

    print("  ---- 剩下的未映射写（V44 默认口径）：全是外设窗口 ----")
    per = Counter()
    for a, sz, v, pc in m2.bus.unmapped_writes:
        for name, lo, hi, _ in lists.PERIPHERAL_WINDOWS:
            if lo <= a < hi:
                per[name] += 1
                break
        else:
            per["其它"] += 1
    for name, lo, hi, expect in lists.PERIPHERAL_WINDOWS:
        print("    %-12s %5d 笔  (0x%08X..0x%08X)" % (name, per[name], lo, hi))
    print("    合计 %d" % sum(per.values()))
    print("""
  → 这五块**不该用 RAM 铺** —— 访问宽度（2 字节 / 1 字节为主）、写值形状
    （0x3FFF / 0xFFFF / 0x1 / 0x80 / 0xFF）都是**寄存器**特征，
    铺成 RAM 会掩盖「这是外设」并给出错误行为。该做的是建**设备模型**。""")


def main():
    sec_a()
    sec_b()
    sec_c()
    sec_d()
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    print("""
1. 固件有一套**通用双向链表库**（`0x124F46` 建空链 / `0x124F4E` is_empty /
   `0x124F5A` 挂链 / `0x124F7A` 取下一个 / `0x124FA0` 取建节点），
   全固件被引用 44 / 87 / 88 / 43 / 60 次。

2. **V43 更正**：`0x124F62` 在 ROM 里被引用 **0 次** —— 它不是「联链」函数，
   是 `0x124F5A` 的第 4 条指令（`st (r4),r5`，把 head->next 换成新节点）。

3. 这套库在固件里至少用了**两遍**：
   * 套 A：3 个节点、每个 **0x221C**（8732 字节）、头 `0x9F69CEB4`；
   * 套 B：**3003** 个节点、每个 **0x14**（20 字节）、三条链各 1001 个、
     头 `0x9F5A0CC0/CC8/CD0`，节点区 `0x9F553620..0x9F5A0CDC`。
   V43 只看到套 A，把套 B 整个漏了。

4. 套 B 的节点区**从来没建过存储器**（V42 起 `ram-sdram` 只到 `0x9F680000`，
   节点区在断口之下）→ 建链那 6,000 笔写全被丢弃、读恒 0 → 链表是断的。
   与 KI#171 **同一个病根**。

5. 补上 `0x9F550000 + 0x30000` 之后：未映射写 7,933 → **1,933**、
   未映射读 3,012 → **12**；三条链各数出 **1001** 个节点、双向 1001/1001 配对；
   **整机控制流数字一个都没变**（换手 34 / 末 PC 0x407DC / ICR 0x10 / 入口 17）
   ⇒ 这块是纯数据区，补对它是纯收益。

6. 剩下的 1,933 笔未映射写全在**外设窗口**（0x40 / 0x6F / 0x50 / 0x60 / 0x61 /
   0x5C0020xx），访问宽度与写值形状都是寄存器特征，**不该用 RAM 铺**，
   该建设备模型。见 KNOWN_ISSUES #174。
""")


if __name__ == "__main__":
    main()
