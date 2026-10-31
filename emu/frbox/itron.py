# -*- coding: utf-8 -*-
"""μITRON 半主机：只建模「会让任务停下来」的那几个服务。

为什么需要这个模块
------------------
frbox 默认把 ``int #0x40`` 当成立即返回的空实现（``--itron`` 的旧行为）。
对 ``sys_get_ver`` 这类查询没问题，但对 ``sys_wai_flg`` 就致命：等待立刻
「成功」返回，于是任务变成 ``wai_flg → clr_flg → sta_tsk`` 的死循环。

V9 的实测：任务 1 用正确的栈顶跑 200 万步不报错、调用 16,450 次服务，
看起来像「跑得很好」，实际上其中 440 次是 ``wai_flg(1, 0x8000)`` 在空转。
**所以「没停机」不等于「跑对了」。**

本模块的做法
------------
- 事件标志（``set_flg`` / ``clr_flg`` / ``wai_flg``）按 μITRON 语义真实建模；
- ``wai_flg`` 条件不满足时**不让任务往下走**，而是把机器置为「阻塞」状态，
  记下是哪个标志、等哪几位、在哪个 PC；
- ``sys_slp_tsk`` / ``sys_dly_tsk`` / ``sys_ext_tsk`` 也按「停下来」处理；
- 外部事件用 ``machine.flag_set(fid, bits)`` 注入，``machine.resume()`` 从
  被阻塞的那条 ``int`` 指令重新执行 —— 这次条件满足了，服务正常返回。

这样就能回答一个此前答不出来的问题：**A 段启动链到底跑完了没有？**
答案是跑完了，停在「等第一个外部事件」上。
"""

M32 = 0xFFFFFFFF

# 服务号（R12 里的 8 位负数）
SVC = {
    -46: "sys_wai_flg",
    -47: "sys_clr_flg",
    -48: "sys_set_flg",
    -44: "sys_ref_flg",
    -38: "sys_slp_tsk",
    -37: "sys_tslp_tsk",
    -85: "sys_dly_tsk",
    -21: "sys_ext_tsk",
    -23: "sys_sta_tsk",
    -25: "sys_ter_tsk",
    -39: "sys_wup_tsk",
    -40: "sys_can_wup",
    -53: "sys_wai_sem",
    -55: "sys_sig_sem",
    -61: "sys_rcv_msg",
    -63: "sys_snd_msg",
}

# wai_flg 的等待模式位
TWF_ANDW = 0x00
TWF_ORW = 0x02
TWF_CLR = 0x01

E_OK = 0
E_TMOUT = -3


def svc_name(num):
    if num >= 128:
        num -= 256
    return SVC.get(num, f"svc_{num}")


class ItronKernel:
    """事件标志 + 阻塞语义的最小内核。

    只做「会让任务停下来」的部分；其余服务一律立刻返回 E_OK。
    """

    def __init__(self, nflags=17, trace=None, machine=None, auto_event=False):
        self.nflags = nflags
        self.machine = machine
        self.auto_event = auto_event     # True = 等不到就把事件补上（记录在 events）
        self.pattern = {}                  # flag_id -> 位模式
        self.calls = {}                    # 服务名 -> 次数
        self.blocks = []                   # 历史阻塞记录
        self.events = []                   # 自动补上的事件（握手序列）
        self.task_ops = []                 # sta_tsk / ter_tsk 的记录
        self.trace = trace
        self.pending = None                # 当前阻塞记录

    def _now(self, cpu):
        """步数口径统一用机器计数；没有机器对象才退回 CPU 自己的计数。"""
        if self.machine is not None:
            return self.machine.step_count
        return getattr(cpu, "steps", 0)

    # ---------------------------------------------------------- 标志操作
    def flag_get(self, fid):
        return self.pattern.get(fid, 0)

    def flag_set(self, fid, bits):
        """外部事件注入：把标志 fid 的 bits 位置 1（等价于 sys_set_flg）"""
        self.pattern[fid] = (self.pattern.get(fid, 0) | bits) & M32
        return self.pattern[fid]

    def flag_clr(self, fid, bits=M32):
        self.pattern[fid] = (self.pattern.get(fid, 0) & ~bits) & M32
        return self.pattern[fid]

    def flag_table(self):
        return [(i, self.pattern.get(i, 0)) for i in range(1, self.nflags + 1)]

    def stats(self):
        return sorted(self.calls.items(), key=lambda kv: -kv[1])

    # ---------------------------------------------------------- 服务钩子
    def __call__(self, cpu, num, pc):
        raw = cpu.r[12] & 0xFF
        n = raw - 256 if raw >= 128 else raw
        name = SVC.get(n, f"svc_{n}")
        self.calls[name] = self.calls.get(name, 0) + 1

        if n == -46:                                   # sys_wai_flg
            return self._wai_flg(cpu, pc)
        if n in (-23, -25):                            # sys_sta_tsk / sys_ter_tsk
            self.task_ops.append({"svc": name, "tsk_id": cpu.r[4],
                                  "param": cpu.r[5],
                                  "step": self._now(cpu), "pc": pc})
            cpu.r[12] = E_OK
            return
        if n == -48:                                   # sys_set_flg
            fid, bits = cpu.r[4], cpu.r[5]
            self.flag_set(fid, bits)
            cpu.r[12] = E_OK
            cpu.r[13] = self.flag_get(fid)
            self._log(f"set_flg({fid}, 0x{bits:X}) → 0x{self.flag_get(fid):X}")
            return
        if n == -47:                                   # sys_clr_flg
            fid, bits = cpu.r[4], cpu.r[5]
            if bits == 0:
                bits = M32
            self.flag_clr(fid, bits)
            cpu.r[12] = E_OK
            self._log(f"clr_flg({fid}, 0x{bits:X}) → 0x{self.flag_get(fid):X}")
            return
        if n in (-38, -37, -85, -21):                   # 会睡/会退出的服务
            self._block(cpu, pc, name, n, {})
            return

        cpu.r[12] = E_OK
        return

    # ---------------------------------------------------------- 内部
    def _wai_flg(self, cpu, pc):
        # 注意参数顺序：wai_flg 用 R5 标志号 / R6 等待位 / R7 模式 / R13 输出
        fid, ptn, mode = cpu.r[5], cpu.r[6], cpu.r[7]
        cur = self.flag_get(fid)
        if mode & TWF_ORW:
            ok = (cur & ptn) != 0
        else:
            ok = (cur & ptn) == ptn
        if not ok and self.auto_event:
            self.flag_set(fid, ptn)
            self.events.append({"flag": fid, "pattern": ptn, "mode": mode,
                                "step": self._now(cpu), "pc": pc,
                                "was": cur})
            self._log(f"自动送事件 flag {fid} |= 0x{ptn:X}（原值 0x{cur:X}）")
            cur = self.flag_get(fid)
            ok = True
        if ok:
            if mode & TWF_CLR:
                self.flag_clr(fid, ptn)
            cpu.r[13] = cur
            cpu.r[12] = E_OK
            self._log(f"wai_flg({fid}, 0x{ptn:X}, mode={mode}) 满足，"
                      f"当前 0x{cur:X}")
            return
        self._block(cpu, pc, "sys_wai_flg", -46,
                    {"flag": fid, "pattern": ptn, "mode": mode, "current": cur})

    def _block(self, cpu, pc, name, n, extra):
        rec = {"svc": name, "num": n, "pc": pc, "step": self._now(cpu),
               "regs": list(cpu.r), "ps": cpu.ps, "dr": list(cpu.dr)}
        rec.update(extra)
        self.pending = rec
        self.blocks.append(rec)
        if self.machine is not None:
            self.machine.blocked = rec
            if name == "sys_ext_tsk":
                self.machine.exited = rec
        self._log(self.describe(rec))

    @staticmethod
    def describe(rec):
        if rec["svc"] == "sys_ext_tsk":
            return "任务调用 sys_ext_tsk 正常结束（不再运行）"
        if rec["svc"] == "sys_wai_flg":
            return (f"任务阻塞于 wai_flg(flag {rec['flag']}, "
                    f"0x{rec['pattern']:X}, mode={rec['mode']})，"
                    f"当前值 0x{rec['current']:X}"
                    + ("（或等待：任一位）" if rec["mode"] & TWF_ORW else "（与等待：全部位）"))
        return f"任务阻塞于 {rec['svc']}"

    def resume(self, cpu):
        """从被阻塞的那条 int 指令重来一遍；此时标志应已满足。"""
        rec = self.pending
        if rec is None:
            return None
        cpu.pc = rec["pc"]
        cpu.r[:] = rec["regs"]
        cpu.ps = rec["ps"]
        cpu.dr[:] = rec["dr"]
        self.pending = None
        if self.machine is not None:
            self.machine.blocked = None
        return rec

    def _log(self, text):
        if self.trace is not None:
            self.trace.log("exec", "[itron] " + text)
