# -*- coding: utf-8 -*-
"""disp_preview.py —— B 段结构化显示预览入口。

整合三项基础设施：
  1. Tick18Clock（INT 0x18 周期节拍，可选）；
  2. SerialIf 波特率级 tick（直接页 0x60–0x67）；
  3. ExpeedLink 写寄存器回写状态机（0x70000000 位串行）；
  4. bdisp.render() 生成结构化界面重建图，并可选打开 tkinter 预览窗口。

用法：
  cd emu
  python -m frbox.disp_preview --no-gui --out preview.png
  python -m frbox.disp_preview --steps 500000 --tick 256
"""

import argparse
import os
import tempfile

from . import bdisp, blive


def run_preview_session(steps=500000, tick_period=256, out_png=None, gui=False):
    """启动 D300 B 段，跑完指定步数后生成结构化显示预览。"""
    m = blive.boot(0)
    if tick_period and tick_period > 0:
        m.enable_auto_tick18(period=tick_period)
    m.run(steps, spin_guard=False)

    if out_png is None:
        out_png = os.path.join(tempfile.gettempdir(), "d300_disp_preview.png")

    ex = m.expeed.summary() if m.expeed is not None else {"cmds": [], "bits_in": 0, "bits_out": 0}
    facts = {
        "expeed_w": ex["bits_in"],
        "expeed_r": ex["bits_out"],
    }
    bdisp.render(out_png, m, facts=facts)

    info = {
        "steps": m.step_count,
        "pc": m.cpu.pc,
        "tick_period": tick_period,
        "disp_summary": bdisp.summary(m),
        "expeed_cmds": len(ex["cmds"]),
        "expeed_regs": {hex(k): v.hex() if isinstance(v, (bytes, bytearray)) else hex(v)
                        for k, v in (m.expeed.regs.items() if m.expeed else ())},
        "serial": m.serial.state(),
        "out_png": out_png,
    }

    if gui:
        _show_tk(out_png, info)
    return info


def _show_tk(png_path, info):
    import tkinter as tk
    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title("D300 Emulator — 结构化显示预览")
    img = Image.open(png_path)
    photo = ImageTk.PhotoImage(img)

    lbl = tk.Label(root, image=photo)
    lbl.image = photo
    lbl.pack()

    status = (
        f"步数: {info['steps']}  |  末 PC: 0x{info['pc']:06X}  |  "
        f"EXPEED 命令: {info['expeed_cmds']} (regs={info['expeed_regs']})  |  "
        f"串口: {info['serial']}"
    )
    tk.Label(root, text=status, anchor="w", padx=10, pady=6).pack(fill="x")
    root.mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser(description="D300 B 段结构化显示预览")
    ap.add_argument("--steps", type=int, default=500000, help="运行步数（默认 500000）")
    ap.add_argument("--tick", type=int, default=256, help="INT 0x18 节拍周期，0 表示关闭（默认 256）")
    ap.add_argument("--out", default=None, help="输出 PNG 路径")
    ap.add_argument("--no-gui", action="store_true", help="仅生成 PNG，不弹窗")
    args = ap.parse_args(argv)

    info = run_preview_session(
        steps=args.steps,
        tick_period=args.tick,
        out_png=args.out,
        gui=not args.no_gui,
    )
    print(f"预览已生成: {info['out_png']}")
    print(f"步数={info['steps']}  末PC=0x{info['pc']:06X}  "
          f"EXPEED命令={info['expeed_cmds']}  EXPEED寄存器={info['expeed_regs']}")


if __name__ == "__main__":
    main()
