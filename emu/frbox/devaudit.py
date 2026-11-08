# -*- coding: utf-8 -*-
"""V57：探针设备策略审计（纯观察 / 纯函数，不改机器状态）。

## 这一层解决的问题

V56 的 C2 组（A 段挂 vec 0x2B）报「末 PC 进应用区 `0x09C97A`、覆盖 825→2269」，
读起来像 A 段往前推进了。V57 复核发现那个 PC 是 `0x174` 完成位的**忙等轮询点**：
6119 次读全是 `0x02`（只剩启动位），循环永不退出。原因是探针用
`create("d300-a")` 自建机器，而 `xfer` 默认关闭 ——

    Machine.__init__   只有 xfer=True / dispatch=True 才 attach（默认全 False）
    blive.boot()       create("d300-b", dispatch=True)  → 只有 dispatch + prio
    boot_realos()      无条件 enable_xfer() + enable_dispatch()

同一套模拟器，三种常用入口挂的设备并不一样。挂上 `xfer` 之后，同一次实验的
`0x174` 读值出现 `0x42`（启动位 `0x02` + 完成位 `0x40`），轮询 6119 次 → 30 次，
正常 `reti` 回空闲任务，覆盖反而更高（2269 → 2512）。所以 V56 那个
「进入应用区」不是推进，是**假墙**。

⚠️ `bobserve` 的读值字典按**十进制**打印（`{0: 4, 2: 28, 66: 4}`），
   那个 `66` 是 `0x42` 不是 `0x66`；掩码 / 地址仍按十六进制写。混用会把
   完成位读成没置上、或把 `0x42` 说成别的寄存器状态。

这和 V20 是同一类坑的两种形态：

    V20  cpu.step() 绕过 Machine.step() 的设备时基 ⇒ 已挂的设备拿不到 tick（硬规矩 1）
    V57  create() 漏挂默认关闭的设备     ⇒ 压根没有那个设备（硬规矩 38）

## 边界

- 本模块**不挂任何设备**、不写内存、不改 CPU 时基，只回答「当前挂了什么」并
  把「忙等假墙」算成一个可复算的布尔判据。
- `XferHandshake` 是模型化假设，不是逆向确认的寄存器语义（见 `dev_xfer.py`）。
  本模块说「挂上之后轮询能退出」，不等于说真机上那个器件是什么。
- 不改 `create()` 的默认值：V2–V9 / V2–V10 的历史基线依赖 `xfer` / `dispatch`
  默认关闭，探针要显式声明自己用哪一套，而不是偷偷替所有人改口径。
"""

import collections

__all__ = ["POLICY", "ENTRIES", "HANDSHAKE", "Site", "audit", "missing",
           "read_values", "stall_verdict", "handshake_table"]

#: `Machine.__init__` 的两个开关及其历史基线原因。
#: create_arg  传给 create() 的关键字参数名；enable  等价的显式启用方法名。
POLICY = {
    "xfer": {
        "create_arg": "xfer",
        "default": False,
        "enable": "enable_xfer",
        "baseline": "V2–V9",
        "why": "0x0AF/0x174/0x524/0x534 的忙等不建模才会一直等，"
               "那批基线观察的就是「卡住」本身",
    },
    "dispatch": {
        "create_arg": "dispatch",
        "default": False,
        "enable": "enable_dispatch",
        "baseline": "V2–V10",
        "why": "直接页 0x44 不建模才没有任务切换，V11 之前观察的就是「没切换」",
    },
    "prio": {
        "create_arg": None,          # 由 dispatch=True 连带挂上
        "default": False,
        "enable": None,
        "baseline": "V2–V10",
        "why": "0x3F4/0x3FC 优先级编码器，__init__ 里与 dispatch 同步",
    },
}

#: 三种常用入口实际挂上的设备（与 machine.py 代码逐项对照）。
#: 这张表是**代码事实的镜像**，不是配置；改代码时单测会把它钉住。
ENTRIES = {
    "create(name)": {"xfer": False, "dispatch": False, "prio": False},
    "blive.boot()": {"xfer": False, "dispatch": True, "prio": True},
    "boot_realos()": {"xfer": True, "dispatch": True, "prio": True},
}

#: A 段四处「置启动位 → 轮询完成位」的实证指纹。
#: 地址与掩码来自 dev_xfer.D300_A_TABLE；轮询 PC 来自 dev_xfer.py 的注释
#: （0x0DCD2C / 0x09C97A / 0x0CE134 / 0x0CA89E 的 btsth + beq 回跳）。
Site = collections.namedtuple("Site", "off start done poll_pc start_pc note")

HANDSHAKE = (
    Site(0x0AF, 0x02, 0x80, 0x000DCD2C, 0x000DCD2A,
         "0x0DCD2A borl #2,@r0 置启动 / 0x0DCD30 btsth #8 等 bit7"),
    Site(0x174, 0x02, 0x40, 0x0009C97A, 0x0009C974,
         "INT 0x2B 派发链里那道；V20 用 XferHandshake.tick() 解过一次"),
    Site(0x524, 0x02, 0x40, 0x000CE134, 0x000CE132,
         "寄存器在 r8，启动位不在紧邻的 borl"),
    Site(0x534, 0x02, 0x40, 0x000CA89C, 0x000CA89C,
         "V12 新增：任务 10 被拉起后卡在这里"),
)


def handshake_table():
    """`{off: (start_mask, done_mask)}`，用来和 dev_xfer.D300_A_TABLE 对账。"""
    return dict((s.off, (s.start, s.done)) for s in HANDSHAKE)


def audit(machine):
    """只读返回当前机器的设备策略快照。

    返回 `{"xfer": bool, "dispatch": bool, "prio": bool,
           "cov": bool, "devices": int, "name": str}`。
    属性不存在一律按 False 处理，方便拿假机器做单测。
    """
    return {
        "xfer": getattr(machine, "xfer", None) is not None,
        "dispatch": getattr(machine, "dispatch", None) is not None,
        "prio": getattr(machine, "prio", None) is not None,
        "cov": getattr(machine, "cov", None) is not None,
        "devices": len(getattr(machine, "devices", ()) or ()),
        "name": getattr(machine, "name", ""),
    }


def missing(machine, required=("xfer", "dispatch")):
    """列出 `required` 里没挂上的设备名；空列表表示齐了。"""
    st = audit(machine)
    return [k for k in required if not st.get(k, False)]


def read_values(reads, addr, pc=None):
    """把 `bobserve.observe()` 的 reads 汇总成 `{值: 次数}`。

    `reads` 结构是 `{(addr, pc): Counter({value: n})}`；`pc=None` 表示
    所有读点合并，传 PC 则只看那一个读点。
    """
    out = collections.Counter()
    for (a, p), cnt in reads.items():
        if a == addr and (pc is None or p == pc):
            out.update(cnt)
    return out


def stall_verdict(reads, addr, done_mask, poll_pc):
    """忙等判据 → `(总读次数, 见到完成位的次数, 是否假墙)`。

    「假墙」= 在轮询 PC 上读过、且**一次都没读到完成位**。这是纯函数：
    只吃观察日志，不看机器状态，也不猜器件语义。没读返回 `(0, 0, False)`。
    """
    vals = read_values(reads, addr, poll_pc)
    total = sum(vals.values())
    done = sum(n for v, n in vals.items() if v & done_mask)
    return total, done, (total > 0 and done == 0)
