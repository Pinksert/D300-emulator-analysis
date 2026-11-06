# -*- coding: utf-8 -*-
"""V43：`0x122064` / `0x122D32` 取的是什么 —— 缓冲池，以及它**根本没被映射**。

V42 把「停摆」定成一次空指针搬运，把「两次取缓冲区都返回 0」留成 KI#171，
当时猜是「模型缺件：链表 / 缓冲池没建起来」。V43 的结论是三段：

**第一段：池建得好好的。** 同一份固件、同样 70 万步，池头被初始化、3 个节点被
串进去、计数写成 3：

```
0x122064  取节点：等(int #0x40) → 判空 → 取/新建 → 挂链 → r4 返回
  ├ 0x124F46  head->next = head     （空链表语义）
  ├ 0x124F4E  is_empty(链表)
  ├ 0x124FA0  取/新建一个节点
  ├ 0x124F5A  挂链
  └ 0x124F62  联链（把节点串进池）
0x12216E  mov r4,r8      ; r8 ← r4：**「取到 NULL」在这里落地**
0x122190  mov r8,r4      ; r4 ← r8：返回值走 r4
```

池的地址全是固件里的立即数，不是我们编的：

| 用途 | 地址 |
|---|---|
| 计数 | `0x9F69CE98` |
| 三条链表头 | `0x9F69CEB4` / `0x9F69CEE4` / `0x9F69CEFC` |
| 三个节点（各 0x221C 字节） | `0x9F696844` / `0x9F698A60` / `0x9F69AC7C` |

**第二段：这些地址在总线上没映射。** V42 及以前 `ram-sdram` 只给了
`0x9F580000 + 0x00100000`，到 `0x9F680000` 就断了；池整段落在断口之后。
于是 `bus.find()` 返回 `None`，写被丢进 `unmapped_writes`、读**恒返回 0**
—— 分配器每次看到的都是「空池」，每次都给 `r4 = 0`。

判据（`probe/exp_bpool.py` A 段）：往 `0x9F69CE98` 写 `0xA1B2C3D4` 再读回，
1MB 口径得到 `0x00000000`，2.5MB 口径得到 `0xA1B2C3D4`。

**第三段：修好映射，整条链就自洽了。** 把 `ram-sdram` 铺到任务栈区之前
（`0x9F580000 + 0x00280000`，2.5MB，V43 起为默认）：

| 口径 | 取节点 | 取缓冲区 | `0x12216E` NULL | 换手 | 末 PC | ICR `0x46F` |
|---|---|---|---|---|---|---|
| 1MB（V42-） | 1 | 11 | 1 | 24 | `0x137968`（卡拷贝环） | `0x00` |
| 2.5MB（V43） | 1 | 0 | 0 | 34 | `0x407DC`（空闲任务） | `0x10` |

⚠️ 2.5MB 那一列**不是「修好了相机」**：取缓冲区 0 次只是说明任务不再走那条
「取不到也照搬」的分支；整机收尾回空闲、任务 9 状态停在 `0x04`（等信号量），
就是我们这台没有真实中断源、没有按键、没有传感器的机器该有的样子。

⚠️ 计数「只减不增 / 下溢成 0xFFFFFFFF」这个 V43 中途的观察**已被推翻**：那是
1MB 口径下「读回恒 0」的副产品。这条留在这里当反面记录。
"""
from __future__ import unicode_literals

#: 池计数（32 位）
POOL_COUNT = 0x9F69CE98
#: 池里三条链表的头（初始化时各自指向自己）
POOL_HEADS = (0x9F69CEB4, 0x9F69CEE4, 0x9F69CEFC)
POOL_ALL = (POOL_COUNT,) + POOL_HEADS
#: 装填时串进去的 3 个节点
NODES = (0x9F696844, 0x9F698A60, 0x9F69AC7C)

#: 取节点（V42 里那个 0x1352EA 任务体调的）
ALLOC_NODE = 0x122064
#: 取缓冲区（同一任务体在 0x135616 调的另一条）
ALLOC_BUF = 0x122D32
#: `mov r4,r8` —— 把取到的指针存进 r8（NULL 就是在这里落地的）
NULL_ASSIGN_PC = 0x12216E
#: `mov r8,r4` —— 返回值走 r4
RET_MOV_PC = 0x122190
#: 调用方取返回值：`0x13531E` 存 r8（节点）、`0x13534E` 存 r9（缓冲区）
CALLER_R8_PC = 0x13531E
CALLER_R9_PC = 0x13534E
#: 任务体里那段 memmove 的起点（V42 的 11 次「搬 0 地址」）
MEM_MOVE_PC = 0x13568E
#: 空链表初始化 `head->next = head`
LIST_INIT = 0x124F46
#: 挂链 / 联链
LIST_PUSH = 0x124F5A
LIST_LINK = 0x124F62
#: is_empty
LIST_EMPTY = 0x124F4E
#: 取/新建节点
LIST_NEWNODE = 0x124FA0
#: 计数自减的写点
COUNT_DEC_PC = 0x122D70

#: 池结构在 SDRAM 里的最高地址（三个节点之后）
POOL_HI = 0x9F69CF04

#: SDRAM 窗口基址；上界是任务栈区（`ram-taskstack` 0x9F800000）
SD_BASE = 0x9F580000
TASKSTACK_BASE = 0x9F800000
#: V42 及以前给 1MB（到 0x9F680000 截断 —— 池正好在外面）
SD_SIZE_V42 = 0x00100000
#: V43 起铺到任务栈区之前
SD_SIZE_V43 = 0x00280000

#: 一次整机跑里值得数的 PC —— 探针与自检共用同一张表
WATCH = {
    ALLOC_NODE: "取节点",
    ALLOC_BUF: "取缓冲区",
    NULL_ASSIGN_PC: "NULL 落地",
    CALLER_R8_PC: "调用方存 r8",
    CALLER_R9_PC: "调用方存 r9",
    MEM_MOVE_PC: "memmove 起点",
    COUNT_DEC_PC: "计数自减",
}

#: 三链表在「初始化前」的初值：全 0（BSS）
BSS = 0x00000000


class PoolWatch(object):
    """记录池头与计数的每一次读写，带 pc 与当时的 r4/r8。

    ⚠️ 判定用的是**重叠**而不是相等：一条 `stb` 写 `0x9F69CE9A` 也会改到计数的
    高字节，用 `a == POOL_COUNT` 会漏（V43 先漏过一次：写日志里始终没有 0，
    可 NULL 那一刻活读却是 0）。
    """

    def __init__(self, m, addrs=POOL_ALL):
        self.m = m
        self.addrs = tuple(addrs)
        self.writes = []          # (step, pc, addr, val, size, 命中的池地址)
        self.reads = []           # (step, pc, addr, val)
        self._wr = None
        self._rd = None

    def _hit(self, a, n):
        for t in self.addrs:
            if a <= t < a + max(n, 1):
                return t
        return None

    def attach(self):
        m, base_w, base_r = self.m, self.m.bus.wr, self.m.bus.rd

        def wr(a, n, v, pc=0):
            t = self._hit(a, n)
            if t is not None:
                self.writes.append((m.step_count, pc, t, v & 0xFFFFFFFF, n, a))
            return base_w(a, n, v, pc)

        def rd(a, n, pc=0):
            t = self._hit(a, n)
            if t is not None:
                v = base_r(a, n, pc)
                self.reads.append((m.step_count, pc, t, v & 0xFFFFFFFF))
                return v
            return base_r(a, n, pc)

        self._wr, self._rd = base_w, base_r
        m.bus.wr, m.bus.rd = wr, rd
        return self

    def detach(self):
        if self._wr is not None:
            self.m.bus.wr, self.m.bus.rd = self._wr, self._rd
            self._wr = self._rd = None
        return self

    def __enter__(self):
        return self.attach()

    def __exit__(self, *a):
        self.detach()
        return False

    # ---- 视图 ----
    def count_track(self):
        """[(步, pc, 值)]，只看计数那一个地址。"""
        return [(s, pc, v) for s, pc, a, v, _n, _ra in self.writes if a == POOL_COUNT]

    def empties(self):
        """计数被写成 `0xFFFFFFFF`（-1）的那些笔。

        ⚠️ 只在 `ram-sdram` 没铺到池地址（1MB 口径）时出现，而且**不是**「空」的
        语义证据：那时的计数读回恒 0，固件怎么算都是错，写 -1 只是它自己的中间
        状态。V43 修好映射之后这条路径不再发生（`probe/exp_bpool.py` B 段两档对照）。
        """
        return [(s, pc, v) for s, pc, v in self.count_track() if v == 0xFFFFFFFF]

    def init_at(self):
        """空链表初始化（head 指向自己）的步数列表。"""
        return [s for s, _pc, a, v, _n, _ra in self.writes
                if a in POOL_HEADS and v == a]

    def nodes_linked(self):
        """被串进池里的节点地址（去掉自指与置 0）。"""
        return [v for _s, _pc, a, v, _n, _ra in self.writes
                if a in POOL_HEADS and v not in (0, a)]

    def by_pc(self):
        """{pc: 次数}，看是谁在动池子。"""
        out = {}
        for _s, pc, _a, _v, _n, _ra in self.writes:
            out[pc] = out.get(pc, 0) + 1
        return out


class NullWatch(object):
    """盯 `0x12216E mov r4,r8`：每次执行时 r4 是多少 —— NULL 就从这里落地。"""

    def __init__(self, m, pc=NULL_ASSIGN_PC):
        self.m = m
        self.pc = pc
        self.hits = []            # (step, r4, r8, 当时池计数)
        self._st = None

    def attach(self):
        cpu, st = self.m.cpu, self.m.cpu.step
        self._st = st

        def step():
            if cpu.pc == self.pc:
                self.hits.append((self.m.step_count, cpu.r[4] & 0xFFFFFFFF,
                                  cpu.r[8] & 0xFFFFFFFF,
                                  self.m.bus.rd(POOL_COUNT, 4) & 0xFFFFFFFF))
            return st()

        cpu.step = step
        return self

    def detach(self):
        if self._st is not None:
            self.m.cpu.step = self._st
            self._st = None
        return self

    def __enter__(self):
        return self.attach()

    def __exit__(self, *a):
        self.detach()
        return False

    def nulls(self):
        return [h for h in self.hits if h[1] == 0]

    def first_null(self):
        n = self.nulls()
        return n[0] if n else None


def count_at(m, addr=POOL_COUNT):
    return m.bus.rd(addr, 4) & 0xFFFFFFFF


def as_signed(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


# ---- 地址映射：V43 的真因就藏在这里 -------------------------------------

def mapped(m, addr):
    """这块地址在总线区表里有没有落点（None = 写被丢、读恒 0）。"""
    return m.bus.find(addr) is not None


def region_of(m, addr):
    """命中的区；未映射返回 None。"""
    return m.bus.find(addr)


def sdram(m):
    """当前机器的 (ram-sdram 基址, 大小)；没有这一段返回 (None, 0)。"""
    for r in m.bus.regions:
        if r.name == "ram-sdram":
            return r.base, r.end - r.base
    return None, 0


def pool_mapped(m):
    """池的四个地址是否**全部**落在已映射的区里。"""
    return all(mapped(m, a) for a in POOL_ALL)


def roundtrip(m, addr, val=0xA1B2C3D4):
    """写一个哨兵再读回：`(命中的区, 读回值)`。

    比对「读回 == 写进」是判「这块地址到底有没有存储器」最直接的判据 ——
    比看 `unmapped_writes` 更硬，因为后者只说明「有一笔写没落地」。
    """
    r = m.bus.find(addr)
    m.bus.wr(addr, 4, val, 0)
    return r, m.bus.rd(addr, 4) & 0xFFFFFFFF


def boot_with(sdram_size, steps=None):
    """按指定的 `ram-sdram` 尺寸建一台 d300-b 并 boot（反事实用）。

    直接改 `machine.D300B.extra_ram` 再建机 —— 改完记得还原，或者用
    `sdram_override()` 上下文。
    """
    from .machine import D300B
    base = list(D300B.extra_ram)
    D300B.extra_ram = [(SD_BASE, sdram_size, "ram-sdram")] + base[1:]
    from . import blive
    return blive.boot() if steps is None else blive.boot(steps)


class sdram_override(object):
    """`with bpool.sdram_override(0x00100000): ...` —— 临时换 SDRAM 尺寸。"""

    def __init__(self, size):
        self.size = size
        self._base = None

    def __enter__(self):
        from .machine import D300B
        self._base = list(D300B.extra_ram)
        D300B.extra_ram = [(SD_BASE, self.size, "ram-sdram")] + self._base[1:]
        return self

    def __exit__(self, *a):
        from .machine import D300B
        D300B.extra_ram = self._base
        return False
