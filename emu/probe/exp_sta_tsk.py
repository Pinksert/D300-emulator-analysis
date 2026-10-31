#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_sta_tsk.py —— 找 ROM 里所有 μITRON 服务调用点

判据（对齐 fr_svc.py 已验证的分发协议）：
    ldi:8 #<负数>, r12 / ldi:20 ... -> extsb r12 -> int #0x40

服务号取自 realos-systemcalls.properties（R12 低 8 位为负补码）。
重点看 sys_sta_tsk(-17) / sys_ext_tsk(-21) / sys_chg_ilm(-67)。
"""
import sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")



sys.stdout.reconfigure(encoding="utf-8")

import fr_emu
import fr_svc
from frbox.machine import create

T = fr_emu.TABLE
NAMES = fr_svc.load_props()

WATCH = {-17: "sys_sta_tsk", -21: "sys_ext_tsk", -19: "sys_ter_tsk",
         -29: "sys_ena_dsp", -30: "sys_dis_dsp", -27: "sys_chg_pri",
         -28: "sys_rot_rdq", -67: "sys_chg_ilm"}


def scan(machine_name, seg):
    m = create(machine_name)
    m.load()
    rom = bytes(m.bus.rom)
    base = m.bus.rom_base
    reg = {}
    calls = []

    off = 0
    while off < len(rom) - 1:
        ins = T.decode(rom, off)
        if ins is None:
            off += 2
            reg.clear()
            continue
        mn, _ops, vals, size, w = ins
        pc = base + off

        if mn.startswith("ldi"):
            dst = vals.get("ri")
            imm = vals.get("i8", vals.get("i20", vals.get("i32")))
            if dst is not None and imm is not None:
                reg[dst] = imm & 0xFFFFFFFF
        elif mn in ("mov", "movn"):
            rs, rd = vals.get("rs1"), vals.get("ri")
            if rs is not None and rd is not None:
                if rs in reg:
                    reg[rd] = reg[rs]
                else:
                    reg.pop(rd, None)
        elif mn == "extsb":
            r = vals.get("ri")
            if r in reg:
                v = reg[r] & 0xFF
                reg[r] = (v - 0x100) & 0xFFFFFFFF if v & 0x80 else v
        elif mn == "extsh":
            r = vals.get("ri")
            if r in reg:
                v = reg[r] & 0xFFFF
                reg[r] = (v - 0x10000) & 0xFFFFFFFF if v & 0x8000 else v
        elif mn.startswith("ld"):
            rd = vals.get("ri")
            if rd is not None:
                reg.pop(rd, None)

        if mn == "int" and vals.get("u8") == 0x40:
            v = reg.get(12)
            if v is not None:
                svc = v - 0x100000000 if v & 0x80000000 else v
                calls.append((pc, svc))
        off += max(2, size)
    return calls


for name, seg in (("d300-a", "A"), ("d300-b", "B")):
    print("=" * 74)
    print("机器 %s" % name)
    calls = scan(name, seg)
    print("  int #0x40 且 R12 已知的服务调用 %d 处" % len(calls))
    cnt = {}
    for pc, svc in calls:
        cnt.setdefault(svc, []).append(pc)
    for svc in sorted(cnt):
        pcs = cnt[svc]
        nm = WATCH.get(svc)
        if nm is None:
            info = NAMES.get(svc)
            nm = info[0] if info else "?"
        flag = " ★" if svc in WATCH else ""
        print("    %-16s (%-4d) ×%-3d  首处 0x%08X%s"
              % (nm, svc, len(pcs), pcs[0], flag))
    print()
