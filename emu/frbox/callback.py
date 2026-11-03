# -*- coding: utf-8 -*-
"""回调槽：A 段的两条「登记—倒计时—派发」链（V18）。

## V19 更正：那个「主循环」其实是 INT 0x2B

下面把 `0x80588` 叫主循环，是 V18 的叫法；V19 查了向量表，它是**中断 0x2B 的
处理函数**（`TBR=0xFFC00` 第 `0xD4` 项）。所以「找不到调用者」成立，「没接线」不成立 ——
接线的入口在 ICU 那边，用 `Machine.enable_isr2b()` 请求一次就能进来（见 isr2b.py）。
本文其余部分（槽布局、倒计时门控、四站链、`tick()` 的语义）不受影响，
`tick()` 仍然只是「手动替它转一圈」的旧工具。

## 为什么会有这一层

V17 把消息通道打通之后留了两条边界：消息里的回调指针**由谁在何时调用**，
以及 Q 场景里任务 5 为什么叫醒了却卡在闸门上。顺着 `0x417EC`（消息样本 `+0x10`）
反查，只有两处引用：

    0x81752  写：`0x81744` 的 r4==1 分支，把 r6 存进去 —— **只登记，不发送**
    0x82252  读：`0x8223A` 把这个值取出来，下一条就是 `call r0`

这就是**回调登记槽 + 派发器**。而且它是两套，结构完全对称：

| | 槽 0 | 槽 1 |
|---|---|---|
| 指针 | `0x417EC` | `0x417F4` |
| 计数 | `0x417F0` | `0x417F8` |
| 标志字节 | `0x3F404` bit1 | `0x3F40D` bit1 |
| 派发器 | `0x8223A` | `0x822CE` |
| 派发器唯一引用点 | `0x80606` | `0x808A4` |

两个引用点都在主循环 `0x80588` 那张「位 → 处理函数」的分派表里。

## 派发器是倒计时门控，不是「取一条就扔一条」

`0x8223A` 的语义（实测）：

    计数 -= 1
    计数 != 0  → 直接返回（这一 tick 什么也不做）
    计数 == 0  → 清标志位 → call [槽里的指针]

所以每个回调有自己的**节拍**：槽 0 登记时给 0xC，就是每 12 个 tick 执行一次。
回调执行完通常又登记下一站，链就这么一站站往前走。

## 关键事实：链是活的，派发器是死的

模拟器基线跑 24 万步之后：

    槽 0 = 0x0008225E，计数 0x000C，标志 0x3F404 = 0x02   ← 已经登记好了
    槽 1 = 0x000822F2，计数 0x0008，标志 0x3F40D = 0x04   ← 也登记好了
    主循环 0x80588 / 派发器 0x8223A / 0x822CE 覆盖率       ← 全 False

静态上也找不到主循环的调用者：相对 `call` 0 处、`jmp` 0 处、`ldi` 立即数 0 处、
ROM 里 4 字节常量 0 处、向量表里也没有。**它是一段没有被接线的后台调度器。**

## 边界

能报：两个单槽、倒计时门控、主循环未执行、手动接上之后链能推进到哪一站。
不能报：真机上这个主循环由谁进入（可能根本没进）、回调里那些动作的业务含义、
      以及**永远不能报「相机跑起来了」**。
"""

MAIN_LOOP = 0x00080588      # 主循环入口（无调用者）
REGISTER = 0x00081744       # 登记/发送复用函数（r4==1 = 只登记）
DISPATCH0 = 0x0008223A      # 槽 0 派发器
DISPATCH1 = 0x000822CE      # 槽 1 派发器

__all__ = ["Slot", "Callbacks", "SLOTS", "attach", "MAIN_LOOP", "REGISTER"]


class Slot:
    """一个「指针 + 计数 + 标志位」的回调槽。"""

    def __init__(self, machine, name, ptr, cnt, flag, bit, dispatch, caller):
        self.machine = machine
        self.name = name
        self.ptr = ptr
        self.cnt = cnt
        self.flag = flag
        self.bit = bit
        self.dispatch = dispatch
        self.caller = caller
        self.fired = []          # [(tick, 回调地址, 步数, 覆盖增量, 未返回)]

    # ------------------------------------------------------------ 观测
    def state(self):
        m = self.machine
        return {"name": self.name, "cb": m.bus.rd(self.ptr, 4),
                "count": m.bus.rd(self.cnt, 2),
                "flag": m.bus.rd(self.flag, 1),
                "flag_addr": self.flag,
                "pending": bool(m.bus.rd(self.flag, 1) & self.bit)}

    def armed(self):
        """槽里有指针 **且** 标志位还开着 —— 只有这种才值得 tick。"""
        return self.state()["cb"] != 0

    # ------------------------------------------------------------ 动作
    def dispatch_once(self, max_steps=20000):
        """走一次派发器 = 主循环扫到这一格。

        ⚠️ 返回值里 `cut=True` 表示函数没回到停机桩 —— 这不是出错，
        而是回调把控制权交给了调度器（例如 `set_flg` 唤醒了等待中的任务）。
        """
        m = self.machine
        before = m.cov.words() if getattr(m, "cov", None) else 0
        cb = m.bus.rd(self.ptr, 4)
        cnt_before = m.bus.rd(self.cnt, 2)
        r = m.call_function(self.dispatch, max_steps=max_steps)
        after = m.cov.words() if getattr(m, "cov", None) else 0
        out = {"cb": cb, "steps": r["steps"], "halted": r["halted"],
               "cut": r["steps"] >= max_steps, "delta": after - before,
               "next": m.bus.rd(self.ptr, 4),
               "count": m.bus.rd(self.cnt, 2),
               "count_before": cnt_before,
               "flag": m.bus.rd(self.flag, 1)}
        # 「回调被执行了」的判据是**门控**：调用前倒计时 ≤ 1（减完正好归零）。
        # 不能拿 delta 当判据 —— 派发器本身第一次跑也会带来十来个新地址，
        # 那是它的序言被覆盖，不是回调被执行。
        out["changed"] = out["next"] != cb
        out["fired"] = cnt_before <= 1
        if out["fired"]:
            self.fired.append((None, cb, out["steps"], out["delta"], out["cut"]))
        return out

    def info(self):
        s = self.state()
        return ("%s cb=0x%08X count=0x%04X flag[0x%05X]=0x%02X%s"
                % (self.name, s["cb"], s["count"], s["flag_addr"], s["flag"],
                   " *pending*" if s["pending"] else ""))


SLOTS = [
    # name, ptr, cnt, flag, bit, dispatch, caller（唯一的引用点）
    ("槽0", 0x000417EC, 0x000417F0, 0x0003F404, 0x02, DISPATCH0, 0x00080606),
    ("槽1", 0x000417F4, 0x000417F8, 0x0003F40D, 0x02, DISPATCH1, 0x000808A4),
]


class Callbacks:
    """两套回调槽的集合：可以 tick、可以看链走到哪、可以单点派发。"""

    def __init__(self, machine):
        self.machine = machine
        self.slots = [Slot(machine, *s) for s in SLOTS]
        self.ticks = 0

    # ------------------------------------------------------------ 观测
    def state(self):
        return [s.state() for s in self.slots]

    def pending(self):
        return [s for s in self.slots if s.armed()]

    def summary(self):
        return " | ".join(s.info() for s in self.slots)

    # ------------------------------------------------------------ 动作
    def tick(self, n=1, max_steps=20000):
        """模拟主循环扫 `n` 圈：每个还有指针的槽各走一次派发器。

        返回真正让覆盖增加的事件列表 —— 门控没到点的那些 tick 不会有记录。
        """
        out = []
        for _ in range(n):
            self.ticks += 1
            for s in self.slots:
                if not s.armed():
                    continue
                r = s.dispatch_once(max_steps=max_steps)
                if r["fired"]:
                    s.fired[-1] = (self.ticks, r["cb"], r["steps"],
                                   r["delta"], r["cut"])
                    out.append((self.ticks, s.name, r))
        return out

    def chain(self, max_ticks=240, max_steps=20000, on_event=None):
        """一直 tick 到「没有槽还装着指针」或者 tick 用完。

        `on_event(tick, slot_name, result)` 给探针做即时打印用。
        """
        events = []
        for _ in range(max_ticks):
            if not self.pending():
                break
            got = self.tick(1, max_steps=max_steps)
            events.extend(got)
            if on_event:
                for t, name, r in got:
                    on_event(t, name, r)
        return events

    def info(self):
        return "cb: ticks=%d  %s" % (self.ticks, self.summary())


def attach(machine):
    """挂上两个回调槽（`Machine.enable_callbacks()` 就是调这个）。"""
    cb = Callbacks(machine)
    machine.cb = cb
    return cb
