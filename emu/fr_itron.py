# -*- coding: utf-8 -*-
"""μITRON（Softune REALOS/FR）服务调用层 —— 表驱动版。

与前一代的区别：不再「服务号→名字→直接返回 E_OK」，而是
  1) 用固件自己的分发表（A 段表尾 0x000FE3B8 / B 段 0x00040660）查出该服务的内核入口，
     从而知道它到底是「真实现」还是「指向桩」；
  2) 名字与参数签名取自社区那份 realos-systemcalls.properties（58 条），
     并用查表结果反证编号没错位——若错位，目标地址会散落到无关区域；
  3) 桩（ret:d + ldi:8 #errcode, r12）返回错误码，真实现按 E_OK 打桩，
     两者在日志里分开记，避免把「内核没这服务」误当成「服务成功了」。

实测（D300 1.11）：
  A 段 176 槽：真实现 18 条，桩 158 条
  B 段 176 槽：真实现 56 条，桩 120 条
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

from fr_svc import SEGS, read_table, load_props    # noqa: E402


# μITRON 标准错误码（仅用于把桩返回的负数翻成名字，属推断）
ERR = {-5: "E_SYS", -9: "E_NOSPT", -10: "E_RSFN", -11: "E_RSATR",
       -17: "E_PAR", -18: "E_ID", -20: "E_NOMEM", -23: "E_NOEXS",
       -24: "E_OBJ", -25: "E_CTX", -41: "E_RLWAI", -42: "E_DLT", -50: "E_TMOUT"}


def parse_sig(sig):
    """'R4 [IN flag_id], R12 [OUT err_code]' → [(4,'IN','flag_id'), (12,'OUT','err_code')]"""
    out = []
    for m in re.finditer(r"R(\d+)\s*\[(\w+)\](?:\s*([^,;]*))?", sig):
        out.append((int(m.group(1)), m.group(2), (m.group(3) or "").strip()))
    return out


class SvcTable:
    def __init__(self, seg):
        self.seg = seg
        self.tend = SEGS[seg]["tend"]
        self.stub = {self.tend, self.tend + 4}      # 两个失败桩
        rows, _ = read_table(seg)
        self.by_num = {n: t for n, _, _, t in rows}
        self.props = load_props()

    def lookup(self, cpu):
        """从 R12 取服务号（8 位负数），返回 (num, name, target, implemented, sig)"""
        raw = cpu.r[12] & 0xFF
        num = raw - 256 if raw >= 128 else raw
        tgt = self.by_num.get(num)
        name, sig = self.props.get(num, (f"svc_{num}", ""))
        implemented = tgt is not None and tgt not in self.stub
        err = None
        if tgt in self.stub:
            err = -17 if tgt == self.tend else -20
        return num, name, tgt, implemented, parse_sig(sig), err

    def stats(self):
        n_impl = sum(1 for t in self.by_num.values() if t not in self.stub)
        return len(self.by_num), n_impl, len(self.by_num) - n_impl


class ItronStub:
    """把 μITRON 服务调用当成立即返回的空实现，但每次调用都对着固件的表核一遍"""

    def __init__(self, runner=None, seg="A", verbose=True):
        self.runner = runner
        self.verbose = verbose
        self.tbl = SvcTable(seg)
        self.count = {}                       # 服务名 -> 次数
        self.log = []                         # (pc, num, name, target, impl, args)
        self.notimpl = {}                     # 未实现服务 -> 次数

    def __call__(self, cpu, num, pc):
        n, name, tgt, impl, sig, err = self.tbl.lookup(cpu)
        self.count[name] = self.count.get(name, 0) + 1
        # 只记签名里声明为输入的寄存器，避免把随机值当参数
        ins = [(r, p) for r, d, p in sig if d == "IN"]
        args = {f"R{r}": cpu.r[r] for r, _ in ins} if ins else \
               {"R4": cpu.r[4], "R5": cpu.r[5], "R6": cpu.r[6], "R7": cpu.r[7]}
        self.log.append((pc, n, name, tgt, impl, args))
        if not impl:
            self.notimpl[name] = self.notimpl.get(name, 0) + 1
        # 内核出口约定：R12 放错误码
        cpu.r[12] = 0 if impl else (err & 0xFFFFFFFF)
        return True

    def report(self, top=10):
        out = []
        for k, v in sorted(self.count.items(), key=lambda x: -x[1])[:top]:
            mark = "  ← 内核未实现" if k in self.notimpl else ""
            out.append(f"  {k:<22} {v}{mark}")
        return "\n".join(out)
