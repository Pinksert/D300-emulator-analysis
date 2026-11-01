# -*- coding: utf-8 -*-
"""服务调用流水：被动记录固件自己的 `int #0x40`，读出「谁在什么时候等什么」。

V11 之前，frbox 观察内核只有两种办法：挂 μITRON 半主机（会截住陷阱、改变执行），
或者事后读内存（看不到顺序）。V12 给 CPU 加了一个**被动**钩子 —— 看一眼就放行，
`int` 照原样走固件自己的向量表 —— 于是可以一边跑真内核一边记账。

记的是四件事：

    calls     每次服务调用：步数 / PC / 服务号 / R4–R7 / R13 / 当时在跑哪个 TCB
    switches  当前 TCB（`*(0x45980)`）什么时候换的
    waits     每个任务最后一次 `wai_flg` 要的是哪个 flag 的哪些位
    sets      每次 `set_flg` / `clr_flg` 改了哪个 flag 的哪些位

这四样拼起来就能回答「任务 1 为什么停住」这类问题，而且不需要假设任何硬件行为。

台架跑法的判据在 `probe/exp_realos_flow.py` 的 docstring 里；本模块只负责记录与格式化。
"""
from . import realos

CUR_TCB = realos.CUR_TCB

SVC_NAMES = {
    -17: "sta_tsk", -19: "ter_tsk", -20: "ref_tsk", -21: "ext_tsk", -22: "twai_flg",
    -23: "sta_tsk", -25: "ter_tsk", -27: "chg_pri", -28: "rot_rdq",
    -38: "?", -39: "wup_tsk", -40: "can_wup", -44: "ref_flg", -46: "wai_flg",
    -47: "clr_flg", -48: "set_flg", -52: "ref_sem", -53: "wai_sem",
    -55: "sig_sem", -60: "ref_mbx", -61: "rcv_msg", -63: "snd_msg",
    -67: "chg_ilm", -68: "ref_ilm", -85: "dly_tsk",
}


def svc_name(num):
    return SVC_NAMES.get(num, "svc%d" % num)


def load_names():
    """有社区那份服务号→名称表就用它（data/realos-systemcalls.properties）。"""
    try:
        import fr_svc
        props = fr_svc.load_props()
    except Exception:
        return dict(SVC_NAMES)
    out = dict(SVC_NAMES)
    for k, v in props.items():
        out[k] = v[0].replace("sys_", "")
    return out


class Flow:
    """被动记录器。`attach(machine)` 之后不要挂 kernel/itron，否则陷阱被截住就记不到真路径。"""

    def __init__(self, machine=None, names=None):
        self.machine = None
        self.calls = []          # (step, pc, svc, r4, r5, r6, r7, r13, tcb)
        self.switches = []       # (step, tcb)
        self.names = names or load_names()
        if machine is not None:
            self.attach(machine)

    # ---------------------------------------------------------------- 采集
    def attach(self, m):
        self.machine = m
        m.attach_int_hook(self._hook)
        return self

    def _hook(self, cpu, vec, pc):
        svc = cpu.r[12] & 0xFF
        svc = svc - 0x100 if svc & 0x80 else svc
        m = self.machine
        tcb = 0
        if m is not None:
            d = m.fetch_bytes(CUR_TCB, 4)
            if len(d) == 4:
                tcb = int.from_bytes(d, "big")
        self.calls.append((m.step_count if m else 0, pc, svc,
                           cpu.r[4], cpu.r[5], cpu.r[6], cpu.r[7], cpu.r[13], tcb))
        if not self.switches or self.switches[-1][1] != tcb:
            self.switches.append((m.step_count if m else 0, tcb))

    def clear(self):
        self.calls.clear()
        self.switches.clear()

    # ---------------------------------------------------------------- 视图
    def of(self, svc):
        return [c for c in self.calls if c[2] == svc]

    def waits(self):
        """每个任务最后一次 wai_flg：{tcb: (step, pc, flag, pattern, mode)}"""
        out = {}
        for step, pc, svc, a4, a5, a6, a7, a13, tcb in self.calls:
            if svc == -46:
                out[tcb] = (step, pc, a5, a6, a7)
        return out

    def sets(self):
        """所有 set_flg / clr_flg：(step, kind, tcb, flag, bits)"""
        out = []
        for step, pc, svc, a4, a5, a6, a7, a13, tcb in self.calls:
            if svc in (-48, -47):
                out.append((step, "set" if svc == -48 else "clr", tcb, a4, a5))
        return out

    def unblocked(self, flgid, bits):
        """谁置过 (flgid, bits) —— 用来回答「这个等待该由谁满足」。"""
        return [(step, tcb) for step, kind, tcb, f, b in self.sets()
                if kind == "set" and f == flgid and b is not None and (b & bits)]

    def format_call(self, c):
        step, pc, svc, a4, a5, a6, a7, a13, tcb = c
        nm = self.names.get(svc, "svc%d" % svc)
        if nm == "wai_flg":
            arg = "flg%s ptn=0x%X mode=%s" % (a5, a6 or 0, a7)
        elif nm in ("set_flg", "clr_flg"):
            arg = "flg%s bits=0x%X" % (a4, a5 or 0)
        elif nm in ("sta_tsk", "ter_tsk", "wup_tsk", "can_wup"):
            arg = "tsk=%s" % a4
        elif nm in ("snd_msg", "rcv_msg"):
            arg = "mbx=%s" % a4
        else:
            arg = "a4=%s a5=%s" % (a4, a5)
        return "%8d 0x%08X tcb=0x%05X %-8s %s" % (step, pc, tcb, nm, arg)

    def dump(self, limit=None):
        rows = self.calls if limit is None else self.calls[-limit:]
        return "\n".join("  " + self.format_call(c) for c in rows)

    def summary(self, m=None):
        m = m or self.machine
        cur = 0
        if m is not None:
            d = m.fetch_bytes(CUR_TCB, 4)
            cur = int.from_bytes(d, "big") if len(d) == 4 else 0
        return {"calls": len(self.calls), "switches": len(self.switches),
                "cur_tcb": cur,
                "tasks": len({c[8] for c in self.calls})}
