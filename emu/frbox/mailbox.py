# -*- coding: utf-8 -*-
"""信箱：把「外部来了一条消息」做成可注入的事件（V17）。

## 为什么需要这一层

V16 结束时所有 flag 都已经能由外部注入，但任务 5 从场景 A 到 C 一路停在
`rcv_msg`（PC 0x0A6326，状态字 0x00400004）。flag 是「握手闸门」，消息是另一种
同步原语 —— 拿 flag 那套办法推不动它。

四步静态 + 动态侦察把这条路走通了：

    A 段全段 `rcv_msg` **只有 1 处**（0x0A6324，就在任务 5 的 PC 上一句）；
    `snd_msg` 15 处、R4 全是 1 —— 全固件只有一个信箱在收消息；
    任务 5 的 R4 实测是控制块 **0x466E4**（id 1 ↔ 0x466E4）；
    它在步 43622 等、任务 1 在 44433 发、51963 又等 —— **不是死锁，是没人再发**。

于是本模块只做一件事：**借固件自己的内核投递一条消息**。

    realos.kernel_call(m, -63, r4=1, r5=msg)   ← 走 0xFE3C0 陷阱入口 → 真实现

不改内核代码、不伪造队列指针，内核自己把消息挂链、自己唤醒任务 5。

## 消息不是数据，是「回调注册」

发送侧是一条固定流水线（0x081760）：

    clr_flg(3, 0xFFFFDFFF)     先清标志
    msg+0x08 = r3              ┐
    msg+0x0A = r2              │ 四个半字参数
    msg+0x0C = r6              │
    msg+0x0E = 0x2000          ┘
    snd_msg(1, msg)            0x08179C

而 msg+0x10 存的是一个**代码指针**（固件自己那条是 0x0008225E）。任务 5 收到后
把它抄进 0x3FE48 落地缓冲，再 `set_flg(2,0x4000)` / `set_flg(3,0x2000)` —— 也就是说
**消息是「把一段代码挂到某个事件上」的载体**，任务 5 是转发器，不是处理者。

## 边界

能报：投递之后任务 5 走完整处理链（11 次服务调用）、回到 rcv_msg 继续等；
      再投一次仍然如此 —— 这是个**常驻循环**，不是一次性初始化。
不能报：消息各字段的业务含义、真实硬件事件是什么、任务 5 之后那段代码由谁调用。
"""

MBX_ID = 1
MBX_CB = 0x000466E4        # 信箱 #1 控制块（+0x00 = 消息链表头）
MSG_SAMPLE = 0x000417DC    # 固件自己构造消息的常驻区（exp_msg_frame.py 抄下来的）
TASK5_BUF = 0x0003FE48     # 任务 5 的 rcv_msg 落地缓冲（+0x10 = 回调指针）
RCV_SITE = 0x000A6324      # 全段唯一的 rcv_msg
SND_SITE = 0x00008179C     # 任务 1 那处 snd_msg
MSG_SIZE = 0x20

SVC_SND_MSG = -63
SVC_RCV_MSG = -61

__all__ = ["Mailbox", "attach", "send", "MBX_ID", "MBX_CB", "MSG_SAMPLE",
           "TASK5_BUF", "RCV_SITE", "SND_SITE"]


class Mailbox:
    """一个可注入的信箱：`send()` 一次 = 外部设备来了一条消息。"""

    def __init__(self, machine=None, mbx=MBX_ID, buf=0x0003F900,
                 sample=MSG_SAMPLE, size=MSG_SIZE):
        self.machine = machine
        self.mbx = mbx
        self.buf = buf
        self.sample = sample
        self.size = size
        self.sent = 0
        self.history = []          # (步数, 返回码, 任务 5 之后的状态)
        self.loaded = False
        if machine is not None:
            self.load_sample()

    # ---------------------------------------------------------- 消息体
    def load_sample(self):
        """把固件自己那条消息抄到发送缓冲 —— 用它就不用猜格式。"""
        m = self.machine
        if m is None or not self.sample:
            return False
        for i in range(self.size):
            m.bus.wr(self.buf + i, 1, m.bus.rd(self.sample + i, 1))
        self.loaded = True
        return True

    def fill(self, words):
        """按 **16 位半字**覆写消息体（下标 0 就是 msg+0x00）。"""
        m = self.machine
        for i, w in enumerate(words):
            m.bus.wr(self.buf + i * 2, 2, w & 0xFFFF)
        return self.buf

    def poke(self, off, val, size=4):
        self.machine.bus.wr(self.buf + off, size, val)
        return self.buf

    def read_back(self):
        return [self.machine.bus.rd(self.buf + i, 4)
                for i in range(0, self.size, 4)]

    # ---------------------------------------------------------- 投递
    def send(self, words=None, max_steps=200000):
        """借固件内核的 `snd_msg` 投一条。返回 {'err':…, 'steps':…} 并顺带记账。

        ⚠️ 每次投递前都重新从样本区抄一遍：固件处理完消息会把缓冲清掉
        （实测 `read_back()` 全 0），不重抄第二发投的就是一条空消息。
        """
        from . import realos
        m = self.machine
        if words is not None:
            self.fill(words)
        elif self.sample:
            self.load_sample()
        r = realos.kernel_call(m, SVC_SND_MSG, r4=self.mbx, r5=self.buf,
                               max_steps=max_steps)
        self.sent += 1
        self.history.append((m.step_count, r.get("err"), self.task5_state()))
        return r

    def task5_state(self):
        from . import realos
        for t in realos.states(self.machine):
            if t["id"] == 5:
                return {"kind": t["wait_kind"], "pc": t["pc"],
                        "state": t["wait_state"]}
        return None

    # ---------------------------------------------------------- 观测
    def queue(self, limit=8):
        """沿控制块 +0x00 的消息链走一遍（空队列时是 0 或自指）。"""
        m = self.machine
        out = []
        p = m.bus.rd(MBX_CB, 4)
        seen = set()
        while len(out) < limit and 0x3F000 <= p < 0x47000 and p not in seen:
            seen.add(p)
            out.append(p)
            p = m.bus.rd(p, 4)
        return out, p

    def state(self):
        return {"sent": self.sent, "mbx": self.mbx,
                "buf": "0x%05X" % self.buf,
                "task5": (self.task5_state() or {}).get("kind", "?")}

    def info(self):
        return "msg: " + "  ".join("%s=%s" % (k, v) for k, v in self.state().items())


def attach(machine, **kw):
    """挂一个信箱源并返回它（`Machine.enable_msgs()` 就是调这个）。"""
    mb = Mailbox(machine, **kw)
    machine.msg = mb
    return mb


def send(machine, words=None, mbx=MBX_ID, buf=0x0003F900, max_steps=200000):
    """一次性投递，不建对象 —— 探针里最常用的入口。"""
    from . import realos
    if words is not None:
        for i, w in enumerate(words):
            machine.bus.wr(buf + i * 2, 2, w & 0xFFFF)
    return realos.kernel_call(machine, SVC_SND_MSG, r4=mbx, r5=buf,
                              max_steps=max_steps)
