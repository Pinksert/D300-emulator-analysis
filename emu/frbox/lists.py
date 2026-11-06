# -*- coding: utf-8 -*-
"""V44：固件的**双向链表库** —— 机制、两套实例、以及那块没映射的节点区。

V43 在查 KI#171（取缓冲区返回 0）时碰到了这套库的边角：三个头
`0x9F69CEB4/CEE4/CEFC`、三个节点、节点之间隔 `0x221C`。V43 把它当成
「池」，并把 `0x124F62` 叫成「联链」。这一版把整个库拆开了，顺手修掉
V43 的两个说法。

## 一、这套库长什么样

固件里有一套**通用双向链表库**，四个入口（都在 `0x124Fxx`）：

| 入口 | 动作 | 全固件被引用次数 |
|---|---|---|
| `0x124F46` | `head->next = head`（建空链表） | 44 |
| `0x124F4E` | `is_empty`：读 `head->next`，与 head 相比 | 87 |
| `0x124F5A` | **挂链**（三步，见下） | 88 |
| `0x124F7A` | 取下一个节点 | 43 |
| `0x124FA0` | 取/新建一个节点 | 60 |

`0x124F5A` 的三步（`fr30-opc.c` 语义：**最后一个操作数是目的**）：

```
0x124F5A  ldi:8  r13, #0x4
0x124F5C  st     (r5), r4      ; ① node->prev = r4
0x124F5E  ld     (r4), r0      ; ② r0 = head->next
0x124F60  st     4(r5), r0     ; ③ node->next = head->next
0x124F62  st     (r4), r5      ; ④ head->next = r5   ← 被 V43 误当成独立函数
0x124F64  ld     (r5), r0
0x124F66  ret:d
```

⚠️ **V43 更正**：`0x124F62` 在整个 B 段 ROM 里被引用 **0 次** —— 它不是
一个函数，是 `0x124F5A` 的第 4 条指令。V43 记的「步 419,147 由 `0x124F62`
串进 3 个节点」，实际是 `0x124F5A` 整体在跑，其中第 ③ 步（`st 4(r5),r0`）
才是把 `head->next` 存进 `node->next`。

**节点结构**：+0 = prev、+4 = next（双向）。有些链表头额外用 +8/+0xC 把自己
串成一条「头链」。

## 二、两套实例

同一套库，固件里用了**不止一次**，规模完全不同：

| | 套 A（V43 找到的） | 套 B（V44 找到的） |
|---|---|---|
| 链表头 | `0x9F69CEB4`（另有 9 个空头 `CED4..CF14`） | `0x9F5A0CC0` / `CC8` / `CD0` |
| 节点数 | 3 | 3 × **1001** |
| 节点大小 | **0x221C**（8732 字节） | **0x14**（20 字节） |
| 节点地址 | `0x9F696844` / `0x9F698A60` / `0x9F69AC7C` | `0x9F553620` .. `0x9F5A0CDC` |
| 节点间隔 | 0x221C（等距） | 0x14（等距） |
| 头链 | `0x9F69CF14` → 13 个头 | `0x9F5A0CD8` → 6 个头 |
| 计数 | `0x9F69CE98` = 3 | 无（套 B 不计��） |
| 建它的函数 | `0x121122`（初始化 10 个空头） | `0x19ACF2`（**循环建 1000 个**） |

套 A 那 3 个节点是**大对象**（每个 8732 字节），链长 4（3 个节点 + 回到头）。
套 B 是**小对象**（每个 20 字节），链长 1001 —— 1000 个数据项 + 1 个头。

## 三、套 B 的节点区没映射 —— 这才是 KI#174 的主因

套 B 的 3003 个节点住在 `0x9F553620..0x9F5A0CDC`。V42/V43 的 `ram-sdram`
只到 `0x9F680000`，这一整段**在断口之下**，从 V42 起就没人给它建过存储器：

* 写被静默丢弃、读恒返回 0（KI#171 的同一个病根）；
* 固件建链时写了 **6,000 笔**（写点只有 `0x124F5C` / `0x124F60` 两个），
  全部落空 → 链表是断的；
* 补上映映后：未映射写 7,933 → 1,933、未映射读 3,012 → **12**，
  三条链各数出 1001 个节点，双向一致性 1001/1001 配对。

`0x9F5B65E8` 那块是**链表头注册表**：

```
0x9F5B65E8 = 0x9F564F60   （节点区末端）
0x9F5B65EC = 0x9F562080   （尾哨兵）
0x9F5B65F0 = 0x9F55D260   （CC0 链首节点）
0x9F5B65F4 = 0x9F558440   （CC8 链首节点）
0x9F5B65F8 = 0x9F553620   （CD0 链首节点）
```

⚠️ `0x19AD18` 那句 `ldi:32 r12, #0x9F5B65F0` 取的是**表项**，不是表首 ——
注册表的起点是 `0x9F5B65E8`，`0x9F5B65F0` 是它的第 3 项。

⚠️ **`0x124F5A` 是头插**，所以从链头往下遍历的顺序是
「**尾节点 → 头 → 首节点 → …**」，不是「首 → 尾」。链长 1001 里包含头自己。

## 四、建套 B 的那个循环（`0x19ACF2`）

```
0x19AD18  ldi:32 r12, #0x9F5B65F0
0x19AD1E  ld    (r12), r5      ; r5 = 源表项
0x19AD22  call  0x124F5A       ; 挂链
0x19AD2A  add   r5, r8         ; 源 += 偏移
0x19AD2C  add   r9, #1         ; 计数 +1
0x19AD2E  add   r8, #0x14      ; 步长 20 字节
0x19AD32  ldi:20 r0, #0x3E8    ; 1000
0x19AD36  cmp   r0, r9
0x19AD38  bc    *-34            ; 回 0x19AD22
```

**1000 次**循环，每次步进 **0x14** 字节 —— 这就是「1000 项定长数组」的出处。

节点区总账：**1000 × 3 + 3 个头 = 3003 个节点**，从 `0x9F553620` 排到
`0x9F5A0CDC`（3003 × 0x14 = 0x9EBC 字节）。三个链头 `0x9F5A0CC0/CC8/CD0`
本身也在这个区的**尾部**。

## 五、还剩下的（KI#174 的后半截）

补上节点区之后，仍有 1,933 笔未映射写，全部落在**外设窗口**，不是 RAM：

| 地址段 | 笔数 | 访问宽度 | 写值形状 | 性质 |
|---|---|---|---|---|
| `0x40xxxxxx` | 1,730 | 2 字节为主 | `0x3FFF` / `0xFFFF` / `0x0` | 外设（`0x137968` 的 16 位拷贝环占 1,541） |
| `0x6Fxxxxxx` | 108 | 2/4 字节 | `0x118`/`0x120`/`0x80` | 外设 |
| `0x50xxxxxx` | 80 | 1 字节为主 | `0x0`/`0x20` | 外设（读 `0x50000105` 最频繁） |
| `0x60`/`0x61xxxxxx` | 11 | 1 字节 | `0x1`/`0xFF`/`0x80` | 位操作寄存器 |
| `0x5C0020xx` | 4 | 1 字节 | `0x26`/`0x72`/`0x52` | 位操作寄存器 |

这五块**不该用 RAM 铺**（铺了会掩盖「这是外设」这件事，行为也不对），
该做的是给它们建**设备模型**。见 KNOWN_ISSUES #174。
"""
from __future__ import unicode_literals

# ---------------------------------------------------------------- 库入口
#: 建空链表：`head->next = head`
LIST_INIT = 0x124F46
#: is_empty：读 `head->next` 与 head 比
LIST_EMPTY = 0x124F4E
#: 挂链（三步 + 第 4 条指令 `0x124F62`）
LIST_PUSH = 0x124F5A
#: `0x124F62` —— `st (r4), r5`，`0x124F5A` 的第 4 条指令，**不是独立函数**
LIST_PUSH_TAIL = 0x124F62
#: 取下一个节点
LIST_NEXT = 0x124F7A
#: 取/新建一个节点
LIST_NEWNODE = 0x124FA0

#: 挂链三步各自的指令地址（`0x124F5A` 内部）
PUSH_PREV = 0x124F5C      # st (r5), r4   → node->prev = r4
PUSH_NEXT = 0x124F60      # st 4(r5), r0 → node->next = head->next
PUSH_HEAD = 0x124F62      # st (r4), r5  → head->next = r5

# ---------------------------------------------------------------- 套 A
#: 套 A 的计数
POOL_COUNT = 0x9F69CE98
#: 套 A 的链表头
POOL_HEADS = (0x9F69CEB4, 0x9F69CEE4, 0x9F69CEFC)
#: 套 A 的另 9 个头（`0x121122` 初始化，`0x124F46` 各调一次）
POOL_EXTRA_HEADS = (0x9F69CED4, 0x9F69CEDC, 0x9F69CEE4, 0x9F69CEEC,
                    0x9F69CEF4, 0x9F69CEFC, 0x9F69CF04, 0x9F69CF0C,
                    0x9F69CF14)
#: 套 A 的三个节点（等距 0x221C）
POOL_NODES = (0x9F696844, 0x9F698A60, 0x9F69AC7C)
#: 套 A 的节点大小
POOL_NODE_SIZE = 0x221C
#: 套 A 的头链起点（链长 13）
POOL_HEADCHAIN = 0x9F69CF14

# ---------------------------------------------------------------- 套 B
#: 套 B 的三个链表头
LIST_HEADS_B = (0x9F5A0CC0, 0x9F5A0CC8, 0x9F5A0CD0)
#: 套 B 的头链起点（链长 6）
LIST_HEADCHAIN_B = 0x9F5A0CD8
#: 套 B 的节点区首地址
NODES_B_BASE = 0x9F553620
#: 套 B 的节点大小
NODE_SIZE_B = 0x14
#: 套 B 每条链的项数（1000 个数据项 + 1 个头 = 1001）
CHAIN_LEN_B = 1001
#: 建套 B 的循环里的项数（`0x19AD32 ldi:20 #0x3E8`）
BUILD_COUNT_B = 0x3E8
#: 建套 B 的那个函数
BUILD_B_FN = 0x19ACF2
#: 链表头注册表（**末端、哨兵、三条链的首节点**）
REGISTRY_B = 0x9F5B65E8
#: 注册表项数
REGISTRY_LEN = 5

#: 套 B 节点区的上界（末节点 + 0x14）
NODES_B_END = 0x9F5A0CDC

# ---------------------------------------------------------------- 地址映射
#: 套 B 节点区要铺的那块 RAM 的基址
NODES_RAM_BASE = 0x9F550000
#: 铺多大（到 `0x9F580000`，正好接上 `ram-sdram`）
NODES_RAM_SIZE = 0x00030000
#: 补上映映后仍未映射的外设窗口（不该用 RAM 铺）
PERIPHERAL_WINDOWS = (
    # V46：0x40xxxxxx 已建成器件（dev_ctrl40）—— 寄存器实例可回读、
    # +0xF8 的两次 RMW 能叠出 0x3B3B、+0x10200 的批量流留档。
    # 详见 KNOWN_ISSUES #179 / emu/probe/exp_dual40.py。
    ("0x40xxxxxx", 0x40000000, 0x41000000, 0),
    ("0x6Fxxxxxx", 0x6F000000, 0x70000000, 108),
    # V45：0x50xxxxxx 已建成器件（dev_ctrl50），那 80 笔不再算未映射写。
    # 详见 KNOWN_ISSUES #176 / emu/probe/exp_bctrl.py。
    ("0x50xxxxxx", 0x50000000, 0x51000000, 0),
    # V46：0x60000000 / 0x61000000 已建成器件（dev_dual60）。
    ("0x60/0x61",   0x60000000, 0x62000000, 0),
    # 只有 4 笔写、0 次读，且是 0x5C00_2000 里 3 个孤立偏移 —— 明显是某个
    # 更大窗口里的一小片命令口，证据不足以单独建器件，老实留着。
    ("0x5C0020xx",  0x5C002000, 0x5C002100, 4),
)

#: V46：`0x40000000` 窗口（与 `dev_ctrl40.CtrlWindow40` 一致）。
#: 寄存器实例在 +0x40000 / +0x40800 / +0x40900，`+0xF8` 是必须可读的 16 位
#: 位域寄存器（两次 RMW → 0x3B3B）；`+0x10200` 是批量下载口。
CTRL40_BASE = 0x40000000
CTRL40_SIZE = 0x1000000
CTRL40_RMW_OFF = 0xF8
CTRL40_RMW_FINAL = 0x3B3B
CTRL40_STREAM_LO = 0x10200
CTRL40_STREAM_LEN_REG = 0x10000

#: V46：`0x60000000` / `0x61000000` 双通道控制器（与 `dev_dual60.DualCtrl60` 一致）。
DUAL60_BASES = (0x60000000, 0x61000000)
DUAL60_SIZE = 0x4000
DUAL60_CTRL = 0x02
DUAL60_STATUS = 0x100
DUAL60_STATUS_FIELD = 0x0C     # +0x100 的 bit2–3（`lsl2 #12` + `lsr2 #14` 取出来）
DUAL60_READY = 0x03            # 该字段 == 3 才算就绪（256 个值里 64 个命中）

#: V45：`0x50000000` 三通道位串行控制块（与 `dev_ctrl50.CtrlSerial50` 一致）。
#: 三组各 16 字节，步长 0x100；`+05` 是串口（bit5 CLK / bit3 DATA / bit6-7 状态），
#: `+06` bit2 是片选。固件在这块地址上做了 80 笔写、58 次读、0 次整块读。
CTRL50_BASE = 0x50000000
CTRL50_SIZE = 0x400
CTRL50_CH_BASE = 0x100
CTRL50_CH_STRIDE = 0x100
CTRL50_CH_COUNT = 3


def nodes_ram_extra():
    """`machine.D300B.extra_ram` 要追加的那一项（补上映映用）。"""
    return (NODES_RAM_BASE, NODES_RAM_SIZE, "ram-nodes")


def chain_len(m, head, limit=20000):
    """从 `head` 出发走一遍 `next` 链，返回 `(节点数, 末端地址)`。

    节点结构：+0 = prev、+4 = next。
    """
    seen = set()
    cur = m.bus.rd(head, 4) & 0xFFFFFFFF
    n = 0
    while cur and cur not in seen and n < limit:
        seen.add(cur)
        n += 1
        cur = m.bus.rd(cur + 4, 4) & 0xFFFFFFFF
    return n, cur


def chain_nodes(m, head, limit=20000):
    """走一遍链，返回节点地址列表。"""
    seen = set()
    cur = m.bus.rd(head, 4) & 0xFFFFFFFF
    out = []
    while cur and cur not in seen and len(out) < limit:
        seen.add(cur)
        out.append(cur)
        cur = m.bus.rd(cur + 4, 4) & 0xFFFFFFFF
    return out


def is_bidirectional(m, nodes):
    """检查 `node->next->prev == node` 是否对每个节点成立，返回不配对数。"""
    bad = 0
    for nd in nodes:
        nxt = m.bus.rd(nd + 4, 4) & 0xFFFFFFFF
        if nxt and (m.bus.rd(nxt, 4) & 0xFFFFFFFF) != nd:
            bad += 1
    return bad


def registry(m):
    """链表头注册表 `0x9F5B65E8` 的内容：`[末端, 哨兵, 首×3, 0]`。

    ⚠️ 别把 `0x9F5B65F0` 当成注册表起点 —— 那是它的**第 3 项**
    （第一条链的首节点）。`0x19AD18` 那句 `ldi:32 r12, #0x9F5B65F0`
    取的是**表项**，不是表首。
    """
    return [m.bus.rd(REGISTRY_B + i * 4, 4) & 0xFFFFFFFF
            for i in range(REGISTRY_LEN)]


def nodes_b_region():
    """套 B 节点区覆盖的地址区间 `(首, 末)`。"""
    return NODES_B_BASE, NODES_B_END


class nodes_override(object):
    """`with lists.nodes_override(True): ...` —— 临时把节点区铺上映射表。

    与 `bpool.sdram_override` 同款，但改的是**追加**一项而不是改 `ram-sdram`。
    """

    def __init__(self, enable=True):
        self.enable = enable
        self._base = None

    def __enter__(self):
        from .machine import D300B
        self._base = list(D300B.extra_ram)
        if self.enable:
            D300B.extra_ram = self._base + [nodes_ram_extra()]
        return self._base

    def __exit__(self, *exc):
        from .machine import D300B
        D300B.extra_ram = self._base
        return False
