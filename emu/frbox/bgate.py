# -*- coding: utf-8 -*-
"""bgate.py —— B 段主事件循环的「门字节」0x9F58D08F（V25 实测）。

V24 发现主循环是「清位 → 查门字节 → 等位 → 无条件跳回」，而且门字节
在 boot、装配、百万步主循环、42 条 ISR 下都没人写。V25 把它的写入者找到了：

    0x12F17A  ldi:32 r12, #0x9F58D08F
    0x12F180  stb   r0(=1), @(r12)      ← 写门字节 = 1
    0x12F182  bra   → 0x12F192
    …
    0x12F19C  ldi:8  r4, #3
    0x12F19E  ldi:32 r5, #0x200000
    0x12F1A8  int #0x40                 ← set_flg(3, 0x200000)

同一个函数里，先写门字节、再置 flag 3 的 0x200000 位 —— 两件事是配套的。
这个函数在 0x12F0xx 那一段（显示/UI 侧），我们的场景里从来没执行到，
所以「门字节没人写」的真实含义是「写它的那段代码没跑到」，不是「没有写者」。

主循环的分支：
    gate == 0  → 0x120302（跳到等待点）
    gate != 0  → 0x120184（干活分支：查状态字 0x9F58D450、call 0x12E6F8、
                  set_flg(6,0x20) / set_flg(12,0x800000) …）

V25 实验（`probe/exp_bgate.py` C 段）：手工把门字节置 1，主循环确实走进干活分支，
多踩 102 个指令地址、调进显示区 0x12E6F8，并置上 flag 6 / flag 12 / flag 3 的
位；但**只跑一次**就退回等待 —— 因为干活分支里还要看状态字 0x9F58D450 的位。

边界（写报告必须带上）：
  * `open_gate()` 是**替那段代码把字节写上**，不是真的执行了 0x12F17A 那段；
  * 状态字 0x9F58D450 的位含义未定（只知道 bit4/bit11 参与分支）；
  * 门字节写入者 0x12F180 所在函数的进入条件仍未定（见 KNOWN_ISSUES）。
"""

import io

GATE = 0x9F58D08F           # 主循环的门字节（0 = 空转，非 0 = 干活）
STATUS = 0x9F58D450         # 干活分支读的状态字（半字）
STATUS2 = 0x9F58D454        # 与 STATUS 一起参与分支的第二个字
WORK_ENTRY = 0x00120184     # 干活分支入口（gate != 0 时 bne 跳到这里）
IDLE_ENTRY = 0x00120302     # gate == 0 时跳到的等待点
GATE_WRITER = 0x0012F180    # 唯一已知写门字节的指令（stb 1）
SET_FLG_200000 = 0x0012F1A8  # 同一函数里紧随其后的 set_flg(3, 0x200000)


def gate(m):
    """读门字节（0 或非 0）。"""
    return m.bus.rd(GATE, 1)


def is_open(m):
    return gate(m) != 0


def open_gate(m, val=1):
    """把门字节置成 val（模拟 0x12F180 那一笔写）。返回旧值。"""
    old = gate(m)
    m.bus.wr(GATE, 1, val & 0xFF, m.cpu.pc if m.cpu else None)
    return old


def close_gate(m):
    """把门字节清 0（当前没有任何代码会清它）。"""
    return open_gate(m, 0)


def status(m):
    """干活分支读的状态字（16 位）。"""
    return m.bus.rd(STATUS, 2)


def status2(m):
    return m.bus.rd(STATUS2, 2)


def refs(m, addr, limit=40):
    """静态找「谁把 addr 当 32 位立即数用」——返回指令地址列表。

    只搜 `ldi:32`（立即数在指令后 2 字节，所以指令地址 = 命中偏移 − 2）。
    这是定位「谁读/谁写某个绝对地址」的廉价办法：固件里地址一律走立即数。
    """
    path = getattr(m, "fw_path", None)
    if not path:
        return []
    try:
        rom = io.open(path, "rb").read()
    except OSError:
        return []
    pat = (addr & 0xFFFFFFFF).to_bytes(4, "big")
    base = m.bus.rom_base
    out, i = [], rom.find(pat)
    while i >= 0 and len(out) < limit:
        out.append(base + i - 2)
        i = rom.find(pat, i + 1)
    return out


def describe(m):
    """一句话状态。"""
    return {"gate": gate(m), "open": is_open(m),
            "status": status(m), "status2": status2(m),
            "refs": len(refs(m, GATE))}
