# -*- coding: utf-8 -*-
"""服务调用流水：被动记录固件自己的 `int #0x40`，读出「谁在什么时候等什么」。

V11 之前，frbox 观察内核只有两种办法：挂 μITRON 半主机（会截住陷阱、改变执行），
或者事后读内存（看不到顺序）。V12 给 CPU 加了一个**被动**钩子 —— 看一眼就放行，
`int` 照原样走固件自己的向量表 —— 于是可以一边跑真内核一边记账。

记的是四件事：

    calls     每次服务调用：步数 / PC / 服务号 / R4–R7 / R13 / 当时在跑哪个 TCB
    switches  当前 TCB 什么时候换的（槽按段取，见 `cur_tcb_slot()`）
    waits     每个任务最后一次 `wai_flg` 要的是哪个 flag 的哪些位
    sets      每次 `set_flg` / `clr_flg` 改了哪个 flag 的哪些位

这四样拼起来就能回答「任务 1 为什么停住」这类问题，而且不需要假设任何硬件行为。

台架跑法的判据在 `probe/exp_realos_flow.py` 的 docstring 里；本模块只负责记录与格式化。

⚠️ **V47 修的一个真 bug**：`cur_tcb` 原先写死 `realos.CUR_TCB`（`0x45980`，**A 段**的槽）。
B 段整机（`d300-b`）在 `0x45980` 上没有 TCB 指针，读到 `0xFAC7A424` 这种值 ——
于是每个服务调用都记成「同一个任务发的」。现在按机器名分段取槽（`cur_tcb_slot()`），
A 段行为不变（旧断言全绿），B 段的 `tcb` / `switches` 第一次是真的。
"""
from . import brealos, realos

#: A 段「当前任务」槽：`*(0x45980)` 是 TCB 指针
CUR_TCB_A = realos.CUR_TCB
#: B 段「当前任务」槽：派发序列把 r14 写进 `[0x68000000]`（见 `brealos.CUR_TCB_SLOT`）
CUR_TCB_B = brealos.CUR_TCB_SLOT


def cur_tcb_slot(machine):
    """按机器分段挑「当前任务 TCB」槽。

    ⚠️ **V47 更正**：V12 立这个模块时一律读 `realos.CUR_TCB`（`0x45980`），
    那是 **A 段**的槽。B 段（`d300-b`，整机跑的都是它）在 `0x45980` 上根本没有
    TCB 指针 —— 读到的是 `0xFAC7A424` 这类垃圾。后果是 `calls[*][8]`（tcb）、
    `switches`、以及 `waits()` / `sets()` / `unblocked()` 的键**全部塌成同一个假值**，
    「谁在等什么」这类问题看着有数，其实一直是把一台机器当一个任务。

    判据（`probe/exp_bwait.py` F 段）：修前 `calls` 里 tcb 只有一个取值
    `0xFAC7A424`；修后取值是 `0x680006xx` 一群，且 `switches` 条数与
    `blive.run()["switches"]`（同一口径 34 次）对得上。
    """
    name = getattr(machine, "name", "") or ""
    return CUR_TCB_A if name.endswith("a") else CUR_TCB_B


#: 兼容旧引用：A 段口径。B 段请用 `Flow.cur_tcb`（实例属性）。
CUR_TCB = CUR_TCB_A

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
        self.cur_tcb = cur_tcb_slot(machine) if machine is not None else CUR_TCB_A
        if machine is not None:
            self.attach(machine)

    # ---------------------------------------------------------------- 采集
    def attach(self, m):
        self.machine = m
        self.cur_tcb = cur_tcb_slot(m)
        m.attach_int_hook(self._hook)
        return self

    def _hook(self, cpu, vec, pc):
        svc = cpu.r[12] & 0xFF
        svc = svc - 0x100 if svc & 0x80 else svc
        m = self.machine
        tcb = 0
        if m is not None:
            d = m.fetch_bytes(self.cur_tcb, 4)
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
            d = m.fetch_bytes(self.cur_tcb, 4)
            cur = int.from_bytes(d, "big") if len(d) == 4 else 0
        # ⚠️ V47：「TCB 个数」与「任务个数」不是同一个数。槽还没填的那一段
        # （比如 boot 里第一次服务调用之前）会记成 `tcb = 0` —— 那不是一个任务，
        # 是「不知道是谁」。所以两个键都给出来，别让人拿 tcb 数当任务数用：
        #   整机 891,520 步：tcbs = 18、tasks = 17，差的那一个就是 tcb = 0（1 笔）。
        tcbs = {c[8] for c in self.calls}
        return {"calls": len(self.calls), "switches": len(self.switches),
                "cur_tcb": cur,
                "tcbs": len(tcbs),
                "tasks": len(tcbs - {0})}
