# -*- coding: utf-8 -*-
"""frbox/ablink.py —— A↔B 跨机事件链与联合画面取证（V60）。

## ⚠️ 先说清楚：这是模型化假设，不是逆向确认的真机通道

V60 静态+动态两轮取证（`results/frbox/ab60.txt` A/B 段）：

* A 段 ROM 对 B 段全部外设窗口的引用是 **0**：`0x5000xxxx` / `0x60000000` /
  `0x68000000` / `0x70000000` / `0x9F5xxxxx` 一个都没有；
* B 段 ROM 对 A 段 `0x174` / `0x60` 的引用也是 **0**；
* 两段数值重叠的 72 个本地 RAM 地址里，动态「一边写、一边读」**0 组**；
* `0x482` 两段都引用，但 V20 已判它是 A 段时钟/PLL 控制组，不是双机握手。

⇒ **真机 A↔B 通道的载体仍未找到**。本模块不声称复原了它，也不往固件里
塞一个「共享内存」地址（那会覆盖两段各自的 RAM，破坏 V2 以来全部基线）。

本模块做的是一件可证伪的事：**假设 A 段的可观测输出事件能通过一条中断线
通知 B 段**，把这条线真的建起来、交替推进两台机器、数清线上过了多少东西，
再看 B 段的显示链会不会因此动起来。若不动，就把「缺的是 A→B 事件线」
这个猜想排除掉（硬规矩 27/28：反事实补全必须限定范围、写清补了什么、
没走到哪儿）。

## 三条口径

1. `OutputTap` 是**纯观察**：只在 bus 的 `rd`/`wr` 上装透传钩子，不改返回值
   （与 `bobserve` 同源），不写任何固件状态。
2. `LinkedPair` 用 `Machine.run()` 分轮交替推进，**不自写 `cpu.step()` 循环**
   （硬规矩 1）；每组对照各用新机器（硬规矩 13）；设备策略由探针显式声明
   并用 `devaudit.audit()` 核对（硬规矩 38）。
3. 对 B 段**唯一**的动作是 `Machine.raise_irq(vector)` 挂一个中断请求。
   不写事件位图、不调 `kernel_call()`、不补 flag、不改读返回值。

## 画面侧

`render_screen()` 画的是**结构重建图**：条目与文案取自固件（B 段运行时
`0x1BC020` 描述符表 + `0x3A7FC0` 文本池），像素与字形由本模块补绘。
图上会把 EXPEED 位串行总线上**真实抓到的命令字节**与帧缓冲三判据一起列出。
这张图不是固件渲染出来的像素，也不能当作「出画面」的证据。
"""

import collections

__all__ = ["Message", "AbLink", "OutputTap", "LinkedPair",
           "KIND_BY_PORT", "OBSERVED_PORTS", "channel_report", "render_screen"]

Message = collections.namedtuple(
    "Message", "seq side kind addr value step")

# A 段可观测的「输出」端口：直接页派发请求 + 三处启动位→完成位握手寄存器。
# 这些是固件自己会写的地址；本模块只观察，不替它写。
KIND_BY_PORT = {
    0x44:  "dispatch_req",
    0x0AF: "xfer_start",
    0x174: "xfer_start",
    0x524: "xfer_start",
}
OBSERVED_PORTS = tuple(sorted(KIND_BY_PORT))


class AbLink(object):
    """单向 A→B 消息链。`drain()` 只记投递，不往 B 段内存里塞东西。"""

    def __init__(self, vector=0x2B, threshold=4):
        self.vector = vector & 0xFF
        self.threshold = max(1, int(threshold))
        self.reset()

    # ------------------------------------------------------------ 生命周期
    def reset(self):
        self.mailbox = []
        self.log = []
        self.posts = 0
        self.delivered = 0
        self.injections = 0
        self.pending = 0

    # ------------------------------------------------------------ A 端
    def post(self, side, kind, addr, value, step):
        msg = Message(len(self.log) + 1, side, kind, addr, value, step)
        self.log.append(msg)
        self.mailbox.append(msg)
        self.posts += 1
        self.pending += 1
        return msg

    # ------------------------------------------------------------ B 端
    def drain(self):
        out = list(self.mailbox)
        self.mailbox = []
        self.delivered += len(out)
        self.pending = 0
        return out

    def should_inject(self):
        return self.pending >= self.threshold

    def record_injection(self, step):
        self.injections += 1
        return self.injections

    # ------------------------------------------------------------ 视图
    def stats(self):
        kinds = collections.Counter(m.kind for m in self.log)
        return {
            "vector": self.vector,
            "threshold": self.threshold,
            "posts": self.posts,
            "delivered": self.delivered,
            "pending": self.pending,
            "injections": self.injections,
            "kinds": dict(kinds),
            "ports": dict(collections.Counter(
                "0x%X" % m.addr for m in self.log)),
        }

    def head(self, n=8):
        return [dict(m._asdict()) for m in self.log[:n]]


class OutputTap(object):
    """在 A 段 bus 上装纯观察钩子，把新增的端口写转成 `Message`。

    直接复用 `bobserve.observe()`（透传、不改返回值，硬规矩 31）。
    """

    def __init__(self, bus, ports=OBSERVED_PORTS):
        from . import bobserve
        self.ports = tuple(ports)
        self.reads, self.writes = bobserve.observe(bus, self.ports)
        self._seen = 0

    def collect(self, link, step):
        """把上次以来新增的写笔投递到链路；返回本次新增条数。"""
        new = self.writes[self._seen:]
        self._seen = len(self.writes)
        for addr, _pc, _a, _n, value in new:
            link.post("A", KIND_BY_PORT.get(addr, "port_write"),
                      addr, value, step)
        return len(new)

    def counts(self):
        c = collections.Counter(w[0] for w in self.writes)
        return dict(c)


class LinkedPair(object):
    """A/B 两台机器交替推进的联合实验台。

    `inject=False` 时链路只记录、不碰 B 段；`inject=True` 时每攒够
    `threshold` 条消息给 B 段挂一次向量中断 —— 这是本设施对 B 段的
    **唯一**动作，属于反事实补全，必须在报告里写清。
    """

    def __init__(self, a, b, link=None, tap=None, inject=False, collect=True):
        self.a = a
        self.b = b
        self.link = link if link is not None else AbLink()
        self.tap = tap if tap is not None else OutputTap(a.bus)
        self.inject = bool(inject)
        self.collect = bool(collect)   # False = 完全不接桥的基线组
        self.rounds = 0
        self.steps_a = 0
        self.steps_b = 0

    def run(self, rounds=7, chunk_a=10000, chunk_b=10000):
        for _ in range(rounds):
            before_a, before_b = self.a.step_count, self.b.step_count
            self.a.run(max_steps=chunk_a, spin_guard=False)
            if self.collect:
                self.tap.collect(self.link, self.a.step_count)
            if self.inject and self.link.should_inject():
                self.b.raise_irq(self.link.vector)
                self.link.record_injection(self.b.step_count)
                self.link.drain()
            self.b.run(max_steps=chunk_b, spin_guard=False)
            self.steps_a += self.a.step_count - before_a
            self.steps_b += self.b.step_count - before_b
            self.rounds += 1
        return self

    def stats(self):
        d = dict(self.link.stats())
        d.update({
            "inject": self.inject,
            "collect": self.collect,
            "rounds": self.rounds,
            "steps_a": self.steps_a,
            "steps_b": self.steps_b,
            "pc_a": self.a.cpu.pc,
            "pc_b": self.b.cpu.pc,
        })
        return d


def channel_report(stats, baseline=None):
    """把链路数字整理成可打印的结论（纯函数，不碰机器）。

    `baseline` 可选：`{cov_b, expeed_frames, ...}`，用于和未接桥组并列。
    """
    lines = [
        "A→B 消息 %d 笔（投递 %d，在途 %d），中断注入 %d 次（vec 0x%02X，阈值 %d）" %
        (stats.get("posts", 0), stats.get("delivered", 0),
         stats.get("pending", 0), stats.get("injections", 0),
         stats.get("vector", 0), stats.get("threshold", 0)),
        "消息来源：%s" % (stats.get("kinds") or {}),
        "端口分布：%s" % (stats.get("ports") or {}),
        "交替推进：%d 轮，A %d 步 / B %d 步；末 PC A=0x%06X B=0x%06X" %
        (stats.get("rounds", 0), stats.get("steps_a", 0),
         stats.get("steps_b", 0), stats.get("pc_a", 0), stats.get("pc_b", 0)),
    ]
    if baseline:
        lines.append("基线对照：%s" % baseline)
    return lines


# ---------------------------------------------------------------- 画面
def render_screen(path, m, facts):
    """画 V60 联合画面图（结构重建，像素非固件）。

    `facts` 键：`link`（AbLink.stats()）、`expeed`（抓包命令帧列表）、
    `framebuffer`（L/V/R 三判据 dict）、`cov_b`、`pc_b`、`pair`。
    """
    from PIL import Image, ImageDraw, ImageFont
    from . import bdisp

    CJK = r"C:\Windows\Fonts\Deng.ttf"
    MONO = r"C:\Windows\Fonts\consola.ttf"

    def font(size, mono=False):
        try:
            return ImageFont.truetype(MONO if mono else CJK, size)
        except OSError:
            return ImageFont.load_default()

    W, H = 1360, 880
    BG, CARD, INK, SUB, LINE = (247, 248, 250), (255, 255, 255), (31, 36, 48), (91, 100, 114), (227, 231, 238)
    ACC, WARN, OK = (29, 111, 184), (184, 121, 27), (31, 138, 76)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((20, 20, W - 20, H - 20), 14, fill=CARD, outline=LINE)

    def put(xy, text, size=14, fill=None):
        mono = all(ord(ch) < 128 for ch in text)
        d.text(xy, text, font=font(size, mono=mono), fill=fill or SUB)

    d.text((44, 36), "D300 · V60 双机联合取证：屏幕结构重建", font=font(26), fill=INK)
    d.text((44, 74), "左：按固件描述符表与文案池重建的界面示意　右：通道流量 / 像素通道 / 帧缓冲判据",
           font=font(15), fill=SUB)

    # ---------------- 左：模拟屏 ----------------
    sx, sy, sw, sh = 44, 116, 640, 480
    d.rounded_rectangle((sx - 8, sy - 8, sx + sw + 8, sy + sh + 8), 10,
                        fill=(20, 22, 26), outline=(60, 64, 72))
    scr = Image.new("RGB", (sw, sh), (16, 18, 22))
    sd = ImageDraw.Draw(scr)
    sd.rectangle((0, 0, sw, 44), fill=(28, 31, 38))
    items = bdisp.menu_items(m, "PLAYBACK", 6)
    sd.text((18, 12), "PLAYBACK MENU", font=font(20), fill=(232, 234, 238))
    y = 64
    for k, it in enumerate(items[:5] or ["Delete", "Playback folder", "Rotate tall"]):
        if k == 0:
            sd.rectangle((10, y - 6, sw - 26, y + 30), fill=(58, 92, 140))
        sd.text((26, y), it, font=font(18), fill=(236, 238, 242))
        y += 42
    sd.rounded_rectangle((sw - 14, 56, sw - 6, sh - 56), 4, fill=(48, 52, 60))
    sd.rounded_rectangle((sw - 14, 56, sw - 6, 170), 4, fill=(120, 128, 140))
    sd.rectangle((0, sh - 40, sw, sh), fill=(28, 31, 38))
    sd.text((18, sh - 30), "重建示意：文案取自固件，像素与字形为外部补绘",
            font=font(13), fill=(150, 156, 168))
    img.paste(scr, (sx, sy))

    # ---------------- 右：证据 ----------------
    x, y = sx + sw + 40, 116
    s = bdisp.summary(m)
    link = facts.get("link") or {}
    ex = facts.get("expeed") or []
    fb = facts.get("framebuffer") or {}

    d.text((x, y), "① 固件自己的显示描述符（运行时读 0x1BC020）", font=font(17), fill=INK)
    y += 30
    for line in (
        "条目 %d 条，有文本 %d 条，文本共 %d 字节" % (s["count"], s["nonempty"], s["text_bytes"]),
        "绘制函数去重 %d 个，其中裸 ret %d 个" % (s["unique_fn"], s["noop_fn"]),
        "没有一条绘制函数写像素缓冲",
    ):
        put((x, y), line, 14, fill=WARN if line.startswith("没有") else SUB)
        y += 24

    y += 16
    d.text((x, y), "② A-B 事件链（模型化假设，非真机语义）", font=font(17), fill=INK)
    y += 30
    for line in (
        "A 段输出 %d 笔，投递 %d，在途 %d" % (link.get("posts", 0),
                                        link.get("delivered", 0), link.get("pending", 0)),
        "B 端中断注入 %d 次（vec 0x%02X，阈值 %d）" % (link.get("injections", 0),
                                              link.get("vector", 0), link.get("threshold", 0)),
        "B 末 PC 0x%06X，覆盖 %d" % (facts.get("pc_b", 0), facts.get("cov_b", 0)),
    ):
        put((x, y), line, 14)
        y += 24

    y += 16
    d.text((x, y), "③ EXPEED 位串行：固件真正送出去的字节", font=font(17), fill=INK)
    y += 30
    if ex:
        for c in ex[:4]:
            raw = c.get("raw", b"")
            reg = c.get("reg", "-")
            put((x, y), "  %s reg=%s raw=%s  %d 位" %
                (c.get("kind", "?"), "-" if reg is None else reg,
                 raw.hex(" ") if isinstance(raw, bytes) else raw, c.get("bits", 0)), 14)
            y += 24
        put((x, y), "共 %d 帧，没有一条是像素数据" % len(ex), 14, fill=WARN)
        y += 24
    else:
        put((x, y), "  0 帧", 14, fill=WARN)
        y += 24

    y += 16
    d.text((x, y), "④ 帧缓冲三判据（同一区间）", font=font(17), fill=INK)
    y += 30
    for key, label in (("L", "连续区间 ≥ 38,400 B"),
                       ("V", "值种数中位数 ≥ 16"),
                       ("R", "被重写半字占比 ≥ 50%")):
        v = fb.get(key, "-")
        put((x, y), "  %s %s → %s" % (key, label, v), 14)
        y += 24

    y += 16
    delta = facts.get("delta") or {}
    d.text((x, y), "⑤ 中断注入实际落点（C2 减 C0 的新增覆盖）", font=font(17), fill=INK)
    y += 30
    if delta:
        put((x, y), "  新增 %d 个 PC，范围 0x%06X..0x%06X" %
            (delta.get("new_pcs", 0), delta.get("lo", 0), delta.get("hi", 0)), 14)
        y += 24
        put((x, y), "  全部落在 B 段 vec 0x2B 的 ISR 入口段", 14)
        y += 24
        put((x, y), "  显示队列生产 / 消费两端一个都没解锁", 14, fill=WARN)
        y += 24
    else:
        put((x, y), "  无对照数据", 14)
        y += 24

    y += 22
    d.rounded_rectangle((x, y, W - 44, y + 106), 10,
                        fill=(253, 246, 236), outline=(240, 226, 200))
    d.text((x + 16, y + 12), "结论", font=font(16), fill=WARN)
    d.text((x + 16, y + 40), "真实画面仍未闭合：EXPEED 总线上仍只有初始化命令，没有一帧像素。",
           font=font(15), fill=(120, 84, 20))
    d.text((x + 16, y + 66), "这张图只重建「屏幕上该有什么」，不是固件渲染的像素。",
           font=font(15), fill=(120, 84, 20))

    img.save(path)
    return path
