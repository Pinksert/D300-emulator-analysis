# -*- coding: utf-8 -*-
"""bhandler.py —— 描述符 +0xC 那 44 个「处理函数」到底做什么（V37 实测）。

V36 只报了两件事：44 个不同地址、27 个指向 0x12E752（那一条就一个 `ret`）。
这一版把 43 个非空的逐个走了一遍控制流，用**轻量常量传播**把每条副作用还原成
「对谁做什么」：

    寄存器常量：ldi / mov / add / extsb 能在直线代码里算出常数就记住
    副作用：int #0x40（r12 = 服务号，r4/r5 = 参数）、call（rel12 / 间接）、
           borl/borh/bandl/bandh（内存位操作，见 KI#114）、st（绝对地址）

一、结论先说：处理函数是「事件翻译器」

    43 个非空处理函数里，**29 个槽位只做一件事**：把界面事件翻译成某条
    `set_flg(flgid, 位模式)`，两个参数全是代码里的立即数 —— 没有一个是运行期算的。
    用到的 flag 只有 4 个：1（1 条）/ 3（13 条）/ 10（12 条）/ 12（16 条）。

    剩下的：23 个槽位做 `borl / borh / bandh`（置位全局字）、7 个回调提交
    `0x12E6F8`（状态链）、2 个用信号量包夹（槽 17 / 48）、31 个槽位挂着单条 `ret`。

    这不是「画界面」，是「通知」。真正的绘制在显示任务里，这里只负责把
    「某个槽位的事件发生了」变成「叫醒等在这组标志上的任务」。接收端也找到了：
    显示模块 0x12E11E / 0x12E4C2 两处 `wai_flg(flgid=3, wfmode=2)`。

二、取值方式（为什么说「翻译」不说过「计算」）

    set_flg 的 flgid 与 pattern 都是 `ldi:8` / `ldi:20` / `ldi:32` 立即数：

        0x12EC8A  ldi:8  r4,#0x0A        ; flgid = 10
        0x12EC8C  ldi:20 r5,#0x2000      ; pattern = 0x2000
        0x12EC90  ldi:8  r12,#0xD0       ; 0xD0 = -48 = set_flg
        0x12EC92  extsb  r12
        0x12EC94  int #0x40
        0x12EC96  ret

    整段没有一条读内存的指令 —— 槽位 9 的「事件」就是这个固定组合。

三、+8 = 2 那五条：掩码是分派代码里写死的常量

    V36 记「+8=2 五条无规律」。规律其实在代码里，不在 +4 里（0x12E2AC 起）：

        0x12E2AC  cmp slot,#0x0F   → beq 0x12E2C4   ; 15 号 → 0x80
        0x12E2B2  cmp slot,#0x44   → beq 0x12E2CA   ; 68 号 → 0x8A
        0x12E2BA  cmp slot,#0x1F   → beq 0x12E2D0   ; 31 号 → 另一条路径（实测 0）
        其余                        → 0x12E2EA     ; 32 号 → 0x00

    实测 5/5 对：15→0x80、68→0x8A、31→0x00、32→0x00、0→不进入（空槽位）。
    所以 +8=2 不是第三种「极性」，是**按槽位号特判**：这条路上掩码不从描述符来。

四、掩码区是 9 字节，不是 4 字节（更正 V36）

    V36 说 0x9F58DA81..84 是「四字节游标」。实际写日志里还有 0x7C / 0x7D / 0x7F：

        0x9F58DA7C  槽位号（副本）      0x12E2F6
        0x9F58DA7D  恒 0               0x12E300
        0x9F58DA7E  没被写              -
        0x9F58DA7F  掩码（副本）        0x12E30A
        0x9F58DA80  没被写              -
        0x9F58DA81  掩码（主）          0x12E28C / 0x12E296 / 0x12E2xx
        0x9F58DA82  槽位号（主）        0x12E244
        0x9F58DA83  计数（上限 70）      -
        0x9F58DA84  下一个槽位号        -

    (7C,82) 与 (7F,81) 是两对同值，像是「上一次」与「当前」两份记录。

五、提交函数（0x12E6F8）不存 r5

    两次不同 r5（0x11223344 / 0x55667788）调同一个槽位，内存写入**逐笔相同**：
    只有 +0 / +4 / +9 三格。已挂链的那条分支也只写 +9（0xC0）。所以载荷 +8
    不是从这条路的 r5 来的 —— 「+8 是谁填的」仍然是 OPEN。

六、能报 / 不能报

    能报：每个处理函数的副作用清单、flag id 只有 1/3/10/12、模式 2 的常量分派、
          掩码区 9 字节布局、提交不写 r5
    不能报：flag 的接收者是谁（要追 wai_flg 的调用者）、0x9F58D08D 那一路的位搬运算、
          载荷 +8 的业务含义、任何画面
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fr_emu

from . import bdesc

BASE = 0x40000
ROM_PATH = os.path.join(os.path.dirname(_ROOT), "data", "B3000111.bin")

SVC_NAMES = {
    -23: "sta_tsk", -25: "ter_tsk", -27: "chg_pri",
    -39: "wup_tsk", -41: "slp_tsk", -45: "ext_tsk",
    -46: "wai_flg", -47: "clr_flg", -48: "set_flg",
    -53: "wai_sem", -55: "sig_sem",
    -61: "rcv_msg", -63: "snd_msg", -83: "set_tim", -170: "twai_flg",
}

SVC_SET_FLG = -48
SUBMIT = 0x0012E6F8
NOOP = bdesc.NOOP_HANDLER
MASK_BLOCK = 0x9F58DA7C          # 9 字节：7C..84
MODE2_CONST = {15: 0x80, 68: 0x8A, 31: 0x00, 32: 0x00}
RET_BYTES = b"\x97\x20"          # `ret` 的机器码（V36 用它验 0x12E752）
MODE2_CMP = {15: 0x12E2AC, 68: 0x12E2B2, 31: 0x12E2BA, 32: 0x12E2C0}
LIVE_RAM = 0x9F000000            # 只报这以上的绝对地址（RAM），滤掉误算

__all__ = ["SVC_NAMES", "SUBMIT", "MASK_BLOCK", "MODE2_CONST", "MODE2_CMP",
           "RET_BYTES", "decode_at", "actions", "dispatch", "flag_map", "families",
           "is_ret", "mode2_check", "audit", "dump", "describe"]


def _rom():
    if not os.path.exists(ROM_PATH):
        return b""
    return open(ROM_PATH, "rb").read()


_ROM = _rom()


def decode_at(addr):
    """按 B 段基址 0x40000 取一条指令。"""
    if not _ROM:
        return None
    return fr_emu.TABLE.decode(_ROM, addr - BASE)


def _sx(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def actions(handler, max_ins=400):
    """走控制流 + 常量传播，返回 [(种类, pc, ...)]。

    种类：
        ("svc",  pc, 服务名, r4, r5)
        ("call", pc, 目标地址或 None)
        ("bit",  pc, 助记符, 地址, 位号)
        ("wr",   pc, 地址, 值)
    """
    acts, seen, work = [], set(), [(handler, {})]
    n = 0
    while work and n < max_ins:
        pc, st0 = work.pop(0)
        st = dict(st0)
        while pc not in seen and n < max_ins:
            d = decode_at(pc)
            if d is None:
                break
            mn, _b, v, size, _w = d
            seen.add(pc)
            n += 1
            nxt = pc + size
            ri = v.get("ri")
            if mn.startswith("ldi"):
                keys = [k for k in v if k.startswith("i")]
                if ri is not None and keys:
                    st[ri] = v[keys[0]]
            elif mn == "mov" and "rs1" in v and v["rs1"] in st and ri is not None:
                st[ri] = st[v["rs1"]]
            elif mn.startswith("add") and "rj" in v and v.get("rj") in st \
                    and v.get("ri") in st:
                st[v["ri"]] = (st[v["rj"]] + st[v["ri"]] + v.get("u4", 0)) & 0xFFFFFFFF
            elif mn == "extsb" and ri in st:
                b = st[ri] & 0xFF
                st[ri] = b - 0x100 if b & 0x80 else b
            elif mn == "int":
                r12 = st.get(12)
                name = SVC_NAMES.get(r12, "svc%s" % r12) if isinstance(r12, int) else "?"
                acts.append(("svc", pc, name, st.get(4), st.get(5)))
                for r in range(8):
                    st.pop(r, None)
            elif mn in ("borl", "borh", "bandh", "bandl"):
                a = st.get(ri)
                if isinstance(a, int) and a >= LIVE_RAM:
                    acts.append(("bit", pc, mn, a, v.get("u4")))
            elif mn.startswith("st") and "rj" in v and ri in st:
                a, val = st[ri], st.get(v["rj"])
                if a >= LIVE_RAM:
                    acts.append(("wr", pc, a, val))
            elif mn in ("call", "call:d"):
                if "rel12" in v:
                    t = (pc + 2 + _sx(v["rel12"], 11) * 2) & 0xFFFFFFFF
                else:
                    t = st.get(v.get("ri", -1))
                acts.append(("call", pc, t if isinstance(t, int) else None))
                for r in (0, 1, 2, 3, 4, 5, 6, 7, 12, 13):
                    st.pop(r, None)
            if mn.startswith("ret") or mn in ("jmp", "jmp:d"):
                break
            if mn in ("bra", "bra:d") and "rel9" in v:
                pc = (pc + 2 + _sx(v["rel9"], 8) * 2) & 0xFFFFFFFF
                continue
            if mn.startswith("b") and "rel9" in v:
                work.append(((pc + 2 + _sx(v["rel9"], 8) * 2) & 0xFFFFFFFF, dict(st)))
            pc = nxt
    return acts


def is_ret(m, handler):
    """这个处理函数是不是「开头就是 ret」—— 什么也不做。"""
    return bytes(m.fetch_bytes(handler, 2)) == RET_BYTES


def dispatch(m):
    """处理函数 → 槽位号列表（按地址排序）。"""
    g = {}
    for e in bdesc.entries(m):
        g.setdefault(e["handler"], []).append(e["index"])
    return dict(sorted(g.items()))


def flag_map(m):
    """set_flg 的统计：{flgid: [(槽位, 位模式, 处理函数, pc)]}。"""
    out = {}
    for h, slots in dispatch(m).items():
        for a in actions(h):
            if a[0] != "svc" or a[2] != "set_flg":
                continue
            fid, pat = a[3], a[4]
            if not isinstance(fid, int) or not isinstance(pat, int):
                continue
            for s in slots:
                out.setdefault(fid, []).append((s, pat, h, a[1]))
    for k in out:
        out[k].sort()
    return dict(sorted(out.items()))


def families(m):
    """按副作用给 44 个处理函数分类。"""
    fam = {"noop": [], "flag": [], "bit": [], "requeue": [], "sem": [], "call": []}
    for h, slots in dispatch(m).items():
        if h == NOOP:
            fam["noop"].extend(slots)
            continue
        acts = actions(h)
        if any(a[0] == "svc" and a[2] == "set_flg" for a in acts):
            fam["flag"].extend(slots)
        if any(a[0] == "bit" for a in acts):
            fam["bit"].extend(slots)
        if any(a[0] == "call" and a[2] == SUBMIT for a in acts):
            fam["requeue"].extend(slots)
        if any(a[0] == "svc" and a[2] in ("wai_sem", "sig_sem") for a in acts):
            fam["sem"].extend(slots)
        if any(a[0] == "call" and a[2] != SUBMIT for a in acts):
            fam["call"].extend(slots)
    for k in fam:
        fam[k].sort()
    return fam


def mode2_check(m):
    """+8=2 的五条：槽位 → (掩码常量, 判等的 cmp 地址, 描述符地址)。"""
    out = {}
    for i in (0, 15, 31, 32, 68):
        e = bdesc.entry(m, i)
        out[i] = {"mode": e["mode"], "mask": MODE2_CONST.get(i),
                  "cmp": MODE2_CMP.get(i), "addr": bdesc.entry_addr(i)}
    return out


def audit(m):
    """体检：覆盖 / 空处理 / flag 种类 / 状态链 / 模式 2。"""
    dp = dispatch(m)
    fam = families(m)
    flags = flag_map(m)
    slots_total = sum(len(s) for s in dp.values())
    return {
        "handlers": len(dp),
        "slots_covered": slots_total,
        "noop_slots": fam["noop"],
        "noop_count": len(fam["noop"]),
        "real": len(dp) - 1,
        "flag_ids": sorted(flags),
        "flag_slots": len(fam["flag"]),
        "bit_slots": len(fam["bit"]),
        "requeue_slots": fam["requeue"],
        "sem_slots": fam["sem"],
        "child_calls": sorted({a[2] for h in dp for a in actions(h)
                               if a[0] == "call" and a[2] and a[2] != SUBMIT}),
        "mode2": mode2_check(m),
        "ret_handlers": sorted(h for h in dp if is_ret(m, h)),
        "ret_only_slots": sorted(s for h in dp if is_ret(m, h) for s in dp[h]),
        "mode2": mode2_check(m),
        "mask_block": bytes(m.fetch_bytes(MASK_BLOCK, 9)),
    }


def dump(m):
    """每个处理函数一行：槽位 + 副作用。"""
    out = []
    for h, slots in dispatch(m).items():
        parts = []
        for a in actions(h):
            if a[0] == "svc":
                parts.append("%s(%s,%s)" % (a[2], a[3], a[4]))
            elif a[0] == "call":
                parts.append("call %s" % ("0x%X" % a[2] if a[2] else "?"))
            elif a[0] == "bit":
                parts.append("%s[0x%X]&%s" % (a[2], a[3], a[4]))
            elif a[0] == "wr":
                parts.append("wr 0x%X=%s" % (a[2], a[3]))
        out.append((h, slots, parts))
    return out


def describe(m):
    a = audit(m)
    lines = [
        "处理函数 %d 个；其中 %d 个是单条 ret（占 %d 个槽位），实做 %d 个"
        % (a["handlers"], len(a["ret_handlers"]), len(a["ret_only_slots"]),
           a["handlers"] - len(a["ret_handlers"])),
        "槽位覆盖 %d/70" % a["slots_covered"],
        "事件标志族：flag %s —— 覆盖槽位 %d 个"
        % ("/".join(str(x) for x in a["flag_ids"]), a["flag_slots"]),
        "位操作族：%d 个槽位；状态链（回调提交）：%s；信号量族：%s"
        % (a["bit_slots"], a["requeue_slots"] or "-", a["sem_slots"] or "-"),
        "子调用：%s" % " ".join("0x%X" % x for x in a["child_calls"]),
        "掩码区 0x%08X 九字节：%s" % (MASK_BLOCK, a["mask_block"].hex(" ")),
    ]
    return "\n".join(lines)
