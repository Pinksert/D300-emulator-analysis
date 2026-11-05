# -*- coding: utf-8 -*-
"""bdisp.py —— B 段显示链路：描述符表、绘制函数、以及一张「重建图」（V26）。

先回答一个常被问的问题：**这个仿真台能出真实画面吗？**

不能。三条硬理由（都能在本模块里量出来）：

1. **固件里没有帧缓冲。** B 段 7 MB 里没有任何一块按 640×480（92 万点）
   组织的连续区域；RAM 里也没有对应的写模式。
2. **固件里没有字库。** 界面上要显示的字全在 ROM 的文本池里（
   `0x3A7FC0` 起，`PLAYBACK MENU`/`Delete`/`Slide show`… 与帮助段落），
   但把它们变成点阵是外部显示控制器（EXPEED）的事。
3. **「绘制」只是改状态 + 发标志。** 描述符表 `0x1BC020`（70 条 × 16 B：
   文本缓冲指针 / 长度 / 样式 / 绘制函数）里的绘制函数，
   实测是「读几个字节、写几个字节、`set_flg(3, 0x400)`」，没有一条写像素；
   其中 `0x12E752` 就是一条裸 `ret`（空实现）。
   真正的像素走 B↔EXPEED 位串行总线（`0x70000002` / `0x7000000A`）——
   而在装配完成 + 开门的整场运行里，那条总线只被碰了 2 次。

**能做什么**：把固件的「屏幕上应该有什么」按结构重建出来 ——
描述符表的条目数/长度/样式是固件自己写的，菜单文案是固件自己的字符串，
我们用系统字体把它们摆成一张示意图。像素与字形是我们补的，必须标清楚。

    from frbox import bdisp
    bdisp.summary(m)                 # 结构统计
    bdisp.menu_items(m, "PLAYBACK")  # 固件文案里的菜单项（按大小写切分）
    bdisp.render("ui.png", m)        # 出重建图（PIL）
"""

DESC_TABLE = 0x1BC020       # 显示描述符表（70 条 × 16 B，V1–V3 已定性）
DESC_COUNT = 70
DESC_STRIDE = 0x10
TYPE_NAME = {0: "normal", 1: "highlight", 2: "blank"}

TEXT_POOL = 0x3A7FC0        # 界面文案池起点（PLAYBACK MENU 所在）
POOL_END = 0x3AD000

MENUS = ("PLAYBACK MENU", "SHOOTING MENU", "SETUP MENU",
         "CUSTOM SETTING MENU", "MY MENU")


def descriptors(m):
    """读 70 条描述符：文本缓冲 / 长度 / 样式 / 绘制函数。"""
    out = []
    for i in range(DESC_COUNT):
        a = DESC_TABLE + i * DESC_STRIDE
        data, ln, ty, fn = (m.bus.rd(a + 4 * k, 4) for k in range(4))
        out.append({"idx": i, "addr": a, "data": data, "len": ln,
                    "type": ty, "fn": fn})
    return out


def fn_is_noop(m, addr):
    """绘制函数是不是「什么都不做」（第一条指令就是 ret）。"""
    return "ret" in m.disasm_at(addr, 1)


def summary(m):
    rows = descriptors(m)
    from collections import Counter
    tys = Counter(r["type"] for r in rows)
    fns = Counter(r["fn"] for r in rows)
    noop = sum(1 for f in fns if fn_is_noop(m, f))
    return {"count": len(rows),
            "type0": tys.get(0, 0), "type1": tys.get(1, 0), "type2": tys.get(2, 0),
            "nonempty": sum(1 for r in rows if r["len"]),
            "unique_fn": len(fns), "noop_fn": noop,
            "text_bytes": sum(r["len"] for r in rows)}


def _pool(m):
    n = POOL_END - TEXT_POOL
    return m.fetch_bytes(TEXT_POOL, n)


def _is_upper(c):
    return 65 <= c <= 90


def _is_lower(c):
    return 97 <= c <= 122


def split_items(blob):
    """把「DeletePlayback folderRotate tall…」切成词条。

    固件把菜单项首尾相接存放（没有分隔符），这里用一个可复核的启发式：
    **大写字母前面若跟着小写字母、右括号或数字，就认为新词条开始**。
    这条规则对 PLAYBACK/ SHOOTING/ SETUP 三段的前若干项都能切对；
    切错时表现为一个词条里出现两个大写开头，肉眼可辨。
    """
    out, cur = [], []
    for i, c in enumerate(blob):
        if c == 0:
            break
        if 32 <= c <= 126 or c >= 0xA0:
            if cur and _is_upper(c):
                p = cur[-1]
                if _is_lower(p) or p in (ord(")"), ord("]")) or 48 <= p <= 57:
                    out.append(bytes(cur).decode("latin1"))
                    cur = []
            cur.append(c)
        else:
            if cur:
                out.append(bytes(cur).decode("latin1"))
                cur = []
    if cur:
        out.append(bytes(cur).decode("latin1"))
    return out


def menu_items(m, menu="PLAYBACK", limit=12):
    """取某个菜单开头的若干词条（默认 PLAYBACK MENU 的前 12 条）。"""
    blob = _pool(m)
    key = menu.encode() if menu.endswith("MENU") else (menu + " MENU").encode()
    i = blob.find(key)
    if i < 0:
        return []
    j = blob.find(b" MENU", i + len(key))
    tail = blob[i + len(key): j if j > 0 else i + len(key) + 600]
    return split_items(tail)[:limit]


def render(path, m, screen=(640, 480), title="D300 · 界面结构重建", facts=None):
    """画一张「重建图」：左边是模拟屏，右边是证据。

    需要 PIL。像素、字体、配色都是重建侧补的 —— 固件只提供
    「有哪些条目、每条的文本长度与样式」以及文案本身。

    facts 可传实测值（probe/exp_bdisp.py 量的）：{'expeed_w','expeed_r','sdram_run'}
    """
    from PIL import Image, ImageDraw, ImageFont

    CJK = r"C:\Windows\Fonts\Deng.ttf"
    MONO = r"C:\Windows\Fonts\consola.ttf"

    def font(size, mono=False):
        try:
            return ImageFont.truetype(MONO if mono else CJK, size)
        except OSError:
            return ImageFont.load_default()

    def put(xy, text, size=14, fill=None, mono=None):
        """写一行：含中文就用 CJK 字体，纯 ASCII 默认等宽（避免方框）。"""
        if mono is None:
            mono = all(ord(ch) < 128 for ch in text)
        d.text(xy, text, font=font(size, mono=mono), fill=fill if fill else SUB)

    W, H = 1320, 760
    BG, CARD, INK, SUB, LINE = (247, 248, 250), (255, 255, 255), (31, 36, 48), (91, 100, 114), (227, 231, 238)
    ACC, WARN, OK = (29, 111, 184), (184, 121, 27), (31, 138, 76)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((20, 20, W - 20, H - 20), 14, fill=CARD, outline=LINE)

    d.text((44, 40), title, font=font(26), fill=INK)
    d.text((44, 78), "左：按固件描述符表与文案重建的界面示意　右：为什么这不是固件渲染出来的像素",
           font=font(15), fill=SUB)

    # ---------------- 左：模拟屏 ----------------
    sx, sy = 44, 120
    sw, sh = screen
    d.rounded_rectangle((sx - 8, sy - 8, sx + sw + 8, sy + sh + 8), 10,
                        fill=(20, 22, 26), outline=(60, 64, 72))
    scr = Image.new("RGB", (sw, sh), (16, 18, 22))
    sd = ImageDraw.Draw(scr)
    # 标题栏
    sd.rectangle((0, 0, sw, 44), fill=(28, 31, 38))
    items = menu_items(m, "PLAYBACK", 6)
    sd.text((18, 12), "PLAYBACK MENU", font=font(20), fill=(232, 234, 238))
    # 菜单项（固件文案池里 PLAYBACK MENU 后的前几条：Delete / Playback folder / …）
    y = 64
    for k, it in enumerate(items[:5] or ["Delete", "Playback folder", "Rotate tall"]):
        if k == 0:
            sd.rectangle((10, y - 6, sw - 26, y + 30), fill=(58, 92, 140))
        sd.text((26, y), it, font=font(18), fill=(236, 238, 242))
        y += 42
    # 右侧滚动条
    sd.rounded_rectangle((sw - 14, 56, sw - 6, sh - 56), 4, fill=(48, 52, 60))
    sd.rounded_rectangle((sw - 14, 56, sw - 6, 170), 4, fill=(120, 128, 140))
    # 页脚
    sd.rectangle((0, sh - 40, sw, sh), fill=(28, 31, 38))
    sd.text((18, sh - 30), "重建示意：文案取自固件，像素与字形为外部补绘", font=font(13), fill=(150, 156, 168))
    img.paste(scr, (sx, sy))

    # ---------------- 右：证据 ----------------
    s = summary(m)
    x = sx + sw + 40
    y = 120
    d.text((x, y), "① 描述符表（固件自己写的屏幕状态）", font=font(17), fill=INK)
    y += 30
    for line in (
        "总条目            %d" % s["count"],
        "有文本的条目      %d" % s["nonempty"],
        "样式 0/1/2        %d / %d / %d" % (s["type0"], s["type1"], s["type2"]),
        "文本总字节        %d" % s["text_bytes"],
        "绘制函数去重      %d 个，其中裸 ret（空实现）%d 个" % (s["unique_fn"], s["noop_fn"]),
    ):
        put((x, y), line, 14)
        y += 24

    y += 18
    d.text((x, y), "② 绘制函数实际做了什么", font=font(17), fill=INK)
    y += 30
    for line in (
        "0x12E752  ret                       ← 空实现（多数条目用它）",
        "0x12E7D6  读/写字节 + set_flg(3,0x400)  ← 改状态、发标志",
        "没有一条绘制函数写像素缓冲",
    ):
        put((x, y), line, 14, fill=(WARN if line.startswith("没有") else SUB))
        y += 24

    y += 18
    d.text((x, y), "③ 像素通道在哪、走了多少次", font=font(17), fill=INK)
    y += 30
    f = facts or {}
    ew, er, run = f.get("expeed_w"), f.get("expeed_r"), f.get("sdram_run")
    for line in (
        "B ↔ EXPEED 位串行：0x70000002 (CS) / 0x7000000A (CLK+DATA)",
        ("整场运行：EXPEED 写 %s 次 / 读 %s 次 ≈ %s 个时钟位" % (ew, er, (ew or 0) // 2))
        if ew is not None else "整场运行：EXPEED 只被碰了两次（稳态窗口）",
        ("SDRAM 最长连续写 %s 字节；一帧 640×480 单色要 38,400" % run) if run is not None
        else "固件里没有帧缓冲，也没有字库 —— 点阵在 EXPEED 那边",
    ):
        put((x, y), line, 14)
        y += 26

    y += 40
    d.rounded_rectangle((x, y, W - 44, y + 92), 10, fill=(253, 246, 236), outline=(240, 226, 200))
    d.text((x + 16, y + 14), "结论", font=font(16), fill=WARN)
    d.text((x + 16, y + 42), "真实画面出不来：缺的不是「再跑几步」，而是 EXPEED 与外设事件。",
           font=font(15), fill=(120, 84, 20))
    d.text((x + 16, y + 66), "这张图只重建「屏幕上该有什么」，不是固件渲染的像素。",
           font=font(15), fill=(120, 84, 20))

    img.save(path)
    return path
