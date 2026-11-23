# -*- coding: utf-8 -*-
"""task93.py —— V93：11 个休眠任务的创建点图谱与「表驱动启动」盲区。

## 这一版补的是什么

V51 列过 27 个 `sta_tsk` 调用点、跑过 16 个，剩下 11 个没跑，其中 **2 个 `r4`
读不出（记作 `None`）**，留了一句「这 11 个函数各自的前置条件是什么」没答。
V65 更正了「全部没被调用」的说法（任务 2 装配函数逐个调 12 个），
但**那 2 个 `r4=None` 从 V51 到 V92 一直挂着**。

V93 把它们解出来了：它们是**表驱动**的两条 `sta_tsk`，tid 从 ROM 表读，
所以 `bstatic.Sweep` 的 `r4` 字段恒 `None`（它只解析立即数）。

    clr_flg(6, ~TABLE[idx])       ; 清 flag 6 的对应位（"启动铃"）
    sta_tsk(TABLE[idx], mode)     ; 启动

两条点、各配两张表：

| 点 | tid 表 | 内容 |
|---|---|---|
| `0x130B5E` | `0x1BD070` | 12, 13 |
| `0x130B8E` | `0x1BD068` | 14, 15 |

于是 **11 个休眠任务 ↔ 11 个未命中的创建点** 才真正对得上：

    9 个静态点（8/18/19/22/23×3/24/25） + 2 个表驱动点（12·13 / 14·15）

V51 那句「11 个静态启动点未命中与 11 个休眠任务不是同一批（点 vs 任务号）」
是对的——**点数 11、任务数 11，但映射不是一对一的**：9 个点覆盖 7 个任务，
2 个点覆盖 4 个任务。本模块的 `BOOTSTRAP` 就是这张映射表。

## 任务分组表

`0x130B2C` 这个批量启动函数用到了 5 张 tid 表，纵向读出来是「按批次分组的任务清单」：

| 表 | 内容 | 当前状态 |
|---|---|---|
| `0x1BD040` | 6, 7, 9, 10 | 全跑过 |
| `0x1BD058` | 4, 8 | 4 跑过、**8 休眠** |
| `0x1BD060` | 1, 2 | 全跑过 |
| `0x1BD068` | 14, 15 | **全休眠** |
| `0x1BD070` | 12, 13 | **全休眠** |

11 个休眠任务里，8、12–15 这 5 个落在这张分组表上；另 6 个（18/19/22/23/24/25）
只有静态硬编码点。

## 补时基不改变这 11 个

V92 已证 `tick=256` 时任务 7 从 `tslp_tsk` 返回、换手 34→224。
V93 复核：**11 个创建点在带时基机器里仍然全部未命中，休眠任务数仍是 11**。

任务 7 的 `TCB+0x20` 高半变成 `0xFF56`（−170 `twai_flg`）——
它离开 `tslp_tsk` 之后转去等 flag 了，没有走得更远。
⇒ V92 的时基收益**不覆盖**这 11 个任务；它们的解锁条件是 flag 事件或
`0x50000107` 的 bit3（KI#199 的反事实路径），不是时间。

## 整片未覆盖

`0x131200..0x131600`（4KB 里取 1KB 采样）**0/16 段命中**，
一条指令都没跑过。`0x130B2C` 的两个直接调用者 `0x1313A6` / `0x131506`
都在这片里 ⇒ 表驱动这条链从**最上层就没进来过**，
不是「走到一半被条件挡住」。

## 边界

- 任务 12–15 的业务语义未查（只知道它们的 tid 与创建点）。
- `0x130B2C` 的实参语义（`r4` / `r5` / `r6` 各是什么）只做到「r6 是表索引」，
  其余没回查 `fr30-opc.c`，**不当定论**。
- `0x50000107` bit3 是 KI#199 的反事实路径，本版**没有重做**，也没证它是真机源。
- 不报「任务链跑通了 / 显示链通了 / 系统跑起来了」。
"""

from . import waitstat67 as _W

# ---------------------------------------------------------------- 分组表
#: (表地址, 内容, 说明) —— 内容由 `read_tables()` 从 ROM 实测核对
TID_TABLES = (
    (0x1BD040, (6, 7, 9, 10), "全跑过"),
    (0x1BD058, (4, 8), "4 跑过、8 休眠"),
    (0x1BD060, (1, 2), "全跑过"),
    (0x1BD068, (14, 15), "全休眠"),
    (0x1BD070, (12, 13), "全休眠"),
)

#: 每张表的长度（下一条表的起点 - 本表起点）/ 4
TABLE_LEN = {0x1BD040: 4, 0x1BD058: 2, 0x1BD060: 2, 0x1BD068: 2, 0x1BD070: 2}

#: 表驱动 sta_tsk：pc -> (tid 表, 覆盖的任务号)
DYNAMIC_BOOTSTRAP = (
    (0x130B5E, 0x1BD070, (12, 13)),
    (0x130B8E, 0x1BD068, (14, 15)),
)

#: 批量启动函数（按索引 clr_flg + sta_tsk 一组任务）
BATCH_FN = 0x130B2C
#: 它的两个直接调用者
BATCH_CALLERS = (0x1313A6, 0x131506)
#: 调用者所在函数的入口（序言起点，不是 enter —— 硬规矩 26）
BATCH_CALLER_FUNCS = (0x131390, 0x1314E4)

#: 静态硬编码创建点：任务号 -> (创建点, ...)
STATIC_BOOTSTRAP = {
    8: (0x170866,),
    18: (0x17306A,),
    19: (0x1963AE,),
    22: (0x242B92,),
    23: (0x1F4E32, 0x1F51FA, 0x1F5468),
    24: (0x1985B2,),
    25: (0x19600E,),
}

#: 11 个休眠任务（state=0x10、保存 SP=0、入口一条没跑）
DORMANT = (8, 12, 13, 14, 15, 18, 19, 22, 23, 24, 25)

#: 表驱动链所在的那片代码：实测 0/16 段命中
GROUND_ZERO = (0x131200, 0x131600)

#: 任务 7 体内那三个创建点（V51 记过，本版复核仍未命中）
T7_BOOTSTRAP = (0x175482, 0x17548E, 0x175630)


def bootstrap_map():
    """任务号 -> [(创建点, 类型)]，类型 `static` / `table`。"""
    out = {}
    for tid, pcs in STATIC_BOOTSTRAP.items():
        out.setdefault(tid, []).extend((pc, "static") for pc in pcs)
    for pc, _tab, tids in DYNAMIC_BOOTSTRAP:
        for tid in tids:
            out.setdefault(tid, []).append((pc, "table"))
    return out


def read_tables(m, window=0x10):
    """从 ROM 实测读 5 张表，返回 {表地址: [值...]}（长度由 `TABLE_LEN` 定）。"""
    out = {}
    for addr, _want, _note in TID_TABLES:
        n = TABLE_LEN[addr]
        out[addr] = [m.bus.rd(addr + i * 4, 4) for i in range(n)]
    return out


def tables_match(m):
    """实测值与常量是否逐格一致。"""
    real = read_tables(m)
    for addr, want, _note in TID_TABLES:
        if tuple(real[addr]) != tuple(want):
            return False, addr, real[addr], want
    return True, None, None, None


def dormant(m):
    """当前机器里 state=0x10 的任务号（升序）。"""
    from . import brealos
    out = []
    for rec in brealos.records(m):
        if not (0x68000000 <= rec["tcb"] < 0x68001000):
            continue
        fl = m.bus.rd(rec["tcb"] + _W.TCB_FLAGS, 4)
        if fl & 0xFF == _W.DORMANT:
            out.append(rec["idx"])
    return out


def dormant_detail(m):
    """休眠任务明细：任务号、TCB、入口、TCB 标志、保存 SP。"""
    from . import brealos
    out = []
    for rec in brealos.records(m):
        if not (0x68000000 <= rec["tcb"] < 0x68001000):
            continue
        fl = m.bus.rd(rec["tcb"] + _W.TCB_FLAGS, 4)
        if fl & 0xFF != _W.DORMANT:
            continue
        out.append({"idx": rec["idx"], "tcb": rec["tcb"], "entry": rec["entry"],
                    "flags": fl,
                    "saved_sp": m.bus.rd(rec["tcb"] + _W.TCB_SAVED_SP, 4)})
    return out


def bootstrap_pcs():
    """全部 11 个创建点的地址（静态 9 + 表驱动 2），升序。"""
    pcs = []
    for _tid, l in STATIC_BOOTSTRAP.items():
        pcs.extend(l)
    pcs.extend(pc for pc, _t, _ts in DYNAMIC_BOOTSTRAP)
    return sorted(pcs)


def unreached(m):
    """11 个创建点里当前机器**没跑到**的 → [(pc, 类型, 任务号)]。"""
    from . import waitstat67  # noqa: F401  (只为保持依赖显式)
    out = []
    for tid, l in sorted(STATIC_BOOTSTRAP.items()):
        for pc in l:
            if not _hit(m, pc):
                out.append((pc, "static", tid))
    for pc, _tab, tids in DYNAMIC_BOOTSTRAP:
        if not _hit(m, pc):
            out.append((pc, "table", tids))
    return out


def _hit(m, addr):
    try:
        return bool(m.cov and m.cov.visited(addr))
    except Exception:
        return False


def hit_map(m):
    """{任务号: 是否命中}——多点的任务要求**任一点**命中。"""
    out = {}
    for tid, l in STATIC_BOOTSTRAP.items():
        out[tid] = any(_hit(m, pc) for pc in l)
    for pc, _tab, tids in DYNAMIC_BOOTSTRAP:
        for tid in tids:
            out[tid] = _hit(m, pc)
    return out


def coverage_span(m, lo, hi, stride=0x40):
    """`[lo,hi)` 按 `stride` 分段的命中段数 → [(段首, 命中条数)]。"""
    out = []
    for base in range(lo, hi, stride):
        n = 0
        for a in range(base, base + stride, 2):
            if _hit(m, a):
                n += 1
        out.append((base, n))
    return out


def ground_zero_hit(m):
    """表驱动链那片代码一共命中了多少段（0 = 整片没进来）。"""
    return sum(1 for base, n in coverage_span(m, GROUND_ZERO[0], GROUND_ZERO[1]) if n)


def entry_prefix(m, entry, win=0x100):
    """入口起**连续**命中多少条（第一条未命中即停），返回 (条数, 停下的 PC)。"""
    import fr_dis
    txt = fr_dis.disasm_addr(m.bus.rom, entry, entry + win, m.bus.rom_base) or []
    n = 0
    stop = None
    for ln in txt:
        a = int(ln.split()[0], 16)
        if _hit(m, a):
            n += 1
        else:
            stop = a
            break
    return n, stop


def verdict_tables(ok, addr=None, real=None, want=None):
    if ok:
        return "5 张 tid 表与常量逐格一致（%d 格）" % sum(TABLE_LEN.values())
    return ("表 0x%06X 实测 %s 与常量 %s 不一致" % (addr, real, list(want)))


def verdict_dormant(dorm, dormant_const=DORMANT):
    if tuple(dorm) == tuple(dormant_const):
        return "休眠任务 %d 个，与 V41 普查逐项一致：%s" % (len(dorm), list(dorm))
    return ("休眠任务 %d 个：%s（与常量 %s 不同）"
            % (len(dorm), list(dorm), list(dormant_const)))


def verdict_bootstrap(unr, total=None):
    total = len(bootstrap_pcs()) if total is None else total
    return ("%d 个创建点未命中（%d 静态 + %d 表驱动）"
            % (len(unr),
               sum(1 for _pc, k, _t in unr if k == "static"),
               sum(1 for _pc, k, _t in unr if k == "table")))


def verdict_ground_zero(n):
    if n == 0:
        return ("表驱动链 0x%06X..0x%06X 整片 0 命中 ⇒ 从最上层就没进来，"
                "不是「走到一半被条件挡住」" % GROUND_ZERO)
    return "表驱动链命中 %d 段（不是整片未覆盖，需另查）" % n


def verdict_tick(dorm_none, dorm_tick):
    """补时基前后休眠任务数对比的短句。"""
    if tuple(dorm_none) == tuple(dorm_tick):
        return ("补时基前后休眠任务都是 %d 个、逐项相同 ⇒ 时基收益不覆盖这批任务"
                % len(dorm_none))
    gone = sorted(set(dorm_none) - set(dorm_tick))
    return ("补时基后休眠任务 %d→%d，减少 %s"
            % (len(dorm_none), len(dorm_tick), gone))
