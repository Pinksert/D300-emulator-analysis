# -*- coding: utf-8 -*-
"""把 D300 固件里的 μITRON（Softune REALOS/FR）服务表整张倒出来。

依据（两段固件各自实读确认）：
  A 段 陷阱入口 0x000FE3C0 → 表尾 0x000FE3B8，向量 INT #0x40
  B 段 陷阱入口 0x00040668 → 表尾 0x00040660
分发器代码形如：
    lsl  #1, r12          ; 服务号×2 变字节偏移
    ldi:32 #<表尾>, r13
    lduh @(r12, r13), r12 ; 取 16 位相对偏移
    extsh r12             ; 符号扩展
    add  r13, r12         ; 目标 = 表尾 + 偏移
    call @r12
所以条目是「相对表尾的有符号 16 位偏移」，服务号为负数，从 -1 递减。
"""
import re
import sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")



sys.stdout.reconfigure(encoding="utf-8")

FW = FW_DIR
PROPS = os.environ.get("REALOS_PROPS", "")

SEGS = {
    "A": dict(file="A3000111.bin", base=0x00080000, lo=0x00080000, hi=0x00100000,
              trap=0x000FE3C0, tend=0x000FE3B8),
    "B": dict(file="B3000111.bin", base=0x00040000, lo=0x00040000, hi=0x00740000,
              trap=0x00040668, tend=0x00040660),
}


def sx16(v):
    return v - 0x10000 if v & 0x8000 else v


def load_props(path=PROPS):
    """社区那份 服务号→名称 映射；键是 32 位负数补码"""
    names = {}
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*(0x[0-9a-fA-F]+)\s*=\s*(\w+)\s*\((.*)\)\s*$", line)
            if not m:
                continue
            key = int(m.group(1), 16)
            num = key - 0x100000000 if key >= 0x80000000 else key   # 还原成负号
            names[num] = (m.group(2), m.group(3))
    except FileNotFoundError:
        pass
    return names


def read_table(seg, fw=FW, max_n=400):
    """从服务号 -1 往回读，遇到第一条越界就停（越界处已是代码/其它数据）"""
    s = SEGS[seg]
    d = open(os.path.join(fw, s["file"]), "rb").read()
    out = []
    for n in range(-1, -max_n, -1):
        a = s["tend"] + 2 * n
        off = a - s["base"]
        if not (0 <= off < len(d) - 2):
            break
        e = int.from_bytes(d[off:off + 2], "big")
        rel = sx16(e)
        # 真实条目是以表尾为基准、离内核码不太远的相对偏移；
        # 表外面紧接着就是代码，把代码字节当条目读会得到 ±0x970D 这类离谱值。
        if not (s["lo"] <= (s["tend"] + rel) < s["hi"]) or abs(rel) > 0x2000:
            break
        out.append((n, a, e, (s["tend"] + rel) & 0xFFFFFFFF))
    return out, d


def looks_like_code(d, seg, addr):
    """粗判目标是不是函数开头：看前 4 条指令里是否出现 stm/st RP/enter 这类序言"""
    s = SEGS[seg]
    off = addr - s["base"]
    if not (0 <= off < len(d) - 8):
        return False
    head = d[off:off + 8]
    w = [int.from_bytes(head[i:i + 2], "big") for i in range(0, 8, 2)]
    for x in w:
        if (x & 0xFF00) in (0x8E00, 0x8F00):     # stm0 / stm1
            return True
        if (x & 0xFF00) == 0x1700:               # st 类（st RP,@-R15）
            return True
        if (x & 0xFFFF) == 0x9FA0:               # nop 填充
            continue
        break
    return False


def main():
    names = load_props()
    print(f"社区映射表条目：{len(names)} 条（服务号 {min(names)} .. {max(names)}）\n")
    for seg in ("A", "B"):
        tbl, d = read_table(seg)
        s = SEGS[seg]
        print(f"===== {seg} 段 =====")
        print(f"  陷阱入口 0x{s['trap']:08X}   表尾 0x{s['tend']:08X}   条目 {len(tbl)} 条 "
              f"（服务 -1 .. {tbl[-1][0]}）")
        tgt = [t for _, _, _, t in tbl]
        code = sum(looks_like_code(d, seg, t) for t in tgt)
        named = sum(1 for n, *_ in tbl if n in names)
        print(f"  目标去重 {len(set(tgt))} 个；像函数开头 {code}/{len(tgt)}；"
              f"能查到名字 {named}/{len(tbl)}")
        # 多条服务指向同一目标 = 别名/未实现
        from collections import Counter
        c = Counter(tgt)
        alias = [(t, k) for t, k in c.items() if k > 1]
        print(f"  多条服务共用同一目标：{len(alias)} 组，涉及 {sum(k for _, k in alias)} 条")
        # 命名覆盖差的区段
        miss = [n for n, *_ in tbl if n not in names]
        if miss:
            print(f"  无名服务号示例：{miss[:12]}")
        # 内核窗口校验：若服务号编号错位，有名服务的目标会散落各处
        nt = sorted(t for n, _, _, t in tbl if n in names)
        if nt:
            print(f"  有名服务目标范围 0x{nt[0]:08X} .. 0x{nt[-1]:08X}（跨度 {nt[-1]-nt[0]:#x}）"
                  f"  中位数 0x{nt[len(nt)//2]:08X}")
        print()
        if seg == "A":
            print("  服务号 → 名称 → 目标地址（前 24 条 + 末 6 条）")
            for n, a, e, t in tbl[:24] + tbl[-6:]:
                nm = names.get(n, ("<未收录>", ""))[0]
                print(f"    {n:5d}  {nm:<18} 0x{t:08X}   条目@{a:08X}=0x{e:04X}")
            print()


if __name__ == "__main__":
    main()
