# -*- coding: utf-8 -*-
"""μITRON 服务分发表解析（V56，两段通用）。

两段固件都用 `int #0x40` 进内核。陷阱 wrapper 用「服务号 × 2」做带符号偏移
查一张 16 位偏移表，再加回表基址 `call` 过去。A 段 @0x0FE3B8 的 wrapper
（实测见 `results/frbox/realos-objects.txt` 第 1 节）：

    0x0FE3EC  lsl    #1, r12          ; extsb 后的服务号 × 2
    0x0FE3EE  ldi:32 #0xFE3B8, r13
    0x0FE3F4  lduh   @(r12,r13), r12
    0x0FE3F6  extsh  r12
    0x0FE3F8  add    r13, r12
    0x0FE3FA  call   @r12

所以每一项是相对**表基址**的带符号 16 位偏移，handler = table_addr + delta。

V56 实证（见 `results/frbox/ab56.txt`）：A 段 128 槽里 56 槽非零、19 个不同
handler，其中 38 槽统一指向 0x0FE3BC（表基址 +4）—— 那是「未实现」默认桩；
`dly_tsk(0xAB)` / `tslp_tsk(0xDB)` 表项为 0、静态调用点也各 0 处。
B 段静态调用点里这两个服务分别是 121 / 137 处。

⚠️ 这里解析的是 ROM 里的**数据**，不是执行结果：表项非零只说明「编译进去了」，
不等于「跑过」。有没有执行要看 cov（硬规矩 5）。
⚠️ 两段的 `int #0x40` 调用点总数口径来自 `bstatic.Sweep.calls`（全 ROM 线性扫），
与 V11 时代 `msg-static.txt` 的 1000/1870 不是同一把尺子，不要混比（硬规矩 2）。
"""

import collections

#: 陷阱向量（与 `bstatic.SVC_VEC` 一致，单独写一份便于纯函数单测不依赖 bstatic）
SVC_VEC = 0x40

#: A 段 1.11 的服务分发表基址（ROM 绝对地址）
A_TABLE = 0x000FE3B8

#: 已实证的服务名；没实证的一律留空，不按 B 段的服务号表硬套
SVC_NAMES = {
    0xD0: "set_flg", 0xD1: "clr_flg", 0xD2: "wai_flg", 0xD4: "ref_flg",
    0xDA: "slp_tsk", 0xE9: "sta_tsk", 0xE7: "ter_tsk", 0xEB: "ext_tsk",
    0xC1: "snd_msg", 0xC3: "rcv_msg", 0xAB: "dly_tsk", 0xDB: "tslp_tsk",
}

#: 时间驱动类服务（V56 用来判「这段内核有没有编进 tick 需求」）
DELAY_SERVICES = (0xAB, 0xDB)

Entry = collections.namedtuple("Entry", "svc raw delta handler implemented")


def sx8(v):
    """8 位补码 → 有符号整数（服务号必须按 8 位补码读，硬规矩 23）。"""
    v &= 0xFF
    return v - 256 if v & 0x80 else v


def parse_table(rom, rom_base, table_addr=A_TABLE, first=0x80, last=0x100):
    """解析服务分发表 → `{svc(有符号): Entry}`。

    `rom` 是整段固件字节串，`rom_base` 是它被映射到的绝对地址，
    `table_addr` 是绝对地址。只做纯读，不建机器、不改任何状态。
    """
    out = {}
    for b in range(first, last):
        svc = sx8(b)
        off = table_addr - rom_base + svc * 2
        if not (0 <= off <= len(rom) - 2):
            continue
        raw = int.from_bytes(rom[off:off + 2], "big")
        delta = raw - 0x10000 if raw & 0x8000 else raw
        out[svc] = Entry(svc, raw, delta, table_addr + delta, delta != 0)
    return out


def default_stub(entries):
    """返回 (被最多服务号共用的 handler, 共用它的服务号列表)。

    编译期内核把没实现的服务统一指到同一个短桩；V56 A 段实测是
    0x0FE3BC（表基址 +4），38 个槽共用它。
    """
    by_handler = collections.defaultdict(list)
    for e in entries.values():
        if e.implemented:
            by_handler[e.handler].append(e.svc)
    if not by_handler:
        return None, []
    handler, svcs = max(by_handler.items(), key=lambda kv: len(kv[1]))
    return handler, sorted(svcs)


def implemented_services(entries, stub=None):
    """真正有独立 handler 的服务号列表（剔除 delta=0 与默认桩）。"""
    if stub is None:
        stub, _svcs = default_stub(entries)
    return sorted(s for s, e in entries.items()
                  if e.implemented and e.handler != stub)


def call_profile(sweep):
    """`bstatic.Sweep` 的 `int #0x40` 调用点 → `{svc: 次数}`（`None` 单列）。"""
    return collections.Counter(c.svc for c in sweep.calls)


def delay_report(entries, calls, services=DELAY_SERVICES):
    """时间驱动服务专查 → `[(svc字节, 名字, 调用点数, 表项raw, handler)]`。

    调用点数与表项两条证据并列，避免只凭「表里没有」就下结论。
    """
    out = []
    for b in services:
        e = entries.get(sx8(b))
        out.append((b, SVC_NAMES.get(b, ""), calls.get(sx8(b), 0),
                    e.raw if e else None, e.handler if e else None))
    return out


def name_of(svc):
    """服务号（有符号或 0..255）→ 名字；没实证的返回空串。"""
    return SVC_NAMES.get(svc & 0xFF, "")
