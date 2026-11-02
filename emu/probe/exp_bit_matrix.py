# -*- coding: utf-8 -*-
"""exp_bit_matrix.py —— 位图一个字一次只喂一位，看谁真的推动固件（V15 复算 V14）

V14 用的是「位图 bit4 + bit8 必须同时置」这句话，但两件事当时没坐实：
  1. 位图里的「bitN」是**字内位号**，而代码里 `btsth #4` 的 `#4` 是**掩码**（0x40 = bit6），
     两套编号混在了一张表里；
  2. `Flow` 记的 `c[1]` 是 `int #0x40` 的**调用点 PC**，不是任务的入口地址 ——
     V14 表里那两行「入口 0x0ACB2C / 0x0ACC16」其实是两个 `sta_tsk` 的调用点。

所以这里把口径全部换成可验证的：位图只喂**一位**（字内 bit0..bit15），输出
  · 任务 8/9/4 各被 `sta_tsk` 了几次、从哪些 PC 发出；
  · 任务 8 在**任务表里的真入口**；
  · 位图终值 / 有没有走到 0x0ACC22（固件自己置 0x80 的那句）。
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create          # noqa: E402
from frbox.flow import Flow               # noqa: E402
from frbox import realos                  # noqa: E402

STEPS = 200000
BATCH = 5000
BITMAP = 0x41A56
SELF_SET = 0x0ACC22                        # 固件自己给 0x41A56 置 0x80 的那一句


def run(bit, steps=STEPS):
    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    m.enable_events(period=4096)
    fl = Flow(m)
    hit_self = False
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        if bit is not None:
            m.bus.wr(BITMAP, 2, m.bus.rd(BITMAP, 2) | (1 << bit))
        for v in (0x13, 0x19):
            m.raise_irq(v)
        if m.cpu.pc == SELF_SET:
            hit_self = True
        if m.cpu.halted or m.fault:
            break
    sta = collections_count(fl)
    t8 = [t for t in realos.states(m) if t["id"] == 8]
    return {
        "bit": bit,
        "sta": sta,
        "bitmap": m.bus.rd(BITMAP, 2),
        "self": hit_self,
        "entry8": t8[0]["entry"] if t8 else 0,
        "pc8": t8[0]["pc"] if t8 else 0,
        "svc": len(fl.calls),
    }


def collections_count(fl):
    """sta_tsk 记录：任务号 → [调用点 PC]。"""
    out = {}
    for c in fl.calls:
        if c[2] == -23:
            out.setdefault(c[3], []).append(c[1])
    return out


def main():
    rows = []
    for bit in [None] + list(range(16)):
        r = run(bit)
        rows.append(r)
        name = "不喂" if bit is None else "bit%-2d" % bit
        sta = ", ".join("T%d×%d@%s" % (k, len(v), "/".join("0x%05X" % p for p in v[:2]))
                        for k, v in sorted(r["sta"].items()))
        print("%-6s 服务%-4d 位图 0x%04X  自置0x80:%-5s  %s"
              % (name, r["svc"], r["bitmap"], "是" if r["self"] else "否", sta or "无"))

    print("\n任务 8 在任务表里的真入口：0x%06X（各家一致才可信）"
          % rows[0]["entry8"])
    yes = [r["bit"] for r in rows if 8 in r["sta"] and r["bit"] is not None]
    base8 = 8 in rows[0]["sta"]
    print("不喂位图时任务 8 是否被 sta_tsk：%s" % ("是" if base8 else "否"))
    print("单喂一位就能拉起任务 8 的位：%s"
          % ("无" if not yes else " ".join("bit%d" % b for b in yes)))


if __name__ == "__main__":
    main()
