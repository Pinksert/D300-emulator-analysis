package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.peripherials.interruptController.InterruptController;
import com.nikonhacker.emu.peripherials.programmableTimer.ProgrammableTimer;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * D300 固件 "UI 画面" 实验。
 *
 * 目的：找出固件到底把屏幕画在哪块内存里，并把它渲染成图片。
 *
 * 做法：沿用 D300Idle 的确定性驱动（按步推进计数器 + 手动激活定时器），
 *      跑够步数后扫描整个 32 位地址空间的页表：
 *        - 哪些页被映射过
 *        - 每页有多少字节既不是 0x00 也不是 0xFF（"真有内容"的量）
 *        - 把连续有内容的区域合并成"块"
 *      然后对最大的若干块，按几种可能的像素格式各渲染一张 PNG，
 *      由人眼判断哪一张像画面。
 *
 *   java com.nikonhacker.emu.D300UI <固件> <总步数> <输出目录> [计数器每N步] [计数器增量]
 */
public class D300UI {

    static final int DISPATCH_IN = 0x00040668;
    static final int HALT_PC     = 0x000407DC;
    static final int COUNTER     = 0x5C003034;

    static final Map<Integer, String> NAMES = new LinkedHashMap<Integer, String>();
    static {
        put(0xD0,"set_flg"); put(0xD1,"clr_flg"); put(0xD2,"wai_flg");
        put(0x96,"pol_flg"); put(0x56,"twai_flg");
        put(0xC9,"sig_sem"); put(0xCB,"wai_sem"); put(0x95,"preq_sem"); put(0xCC,"ref_sem");
        put(0xC1,"snd_msg");
        put(0xAB,"dly_tsk"); put(0xDA,"slp_tsk"); put(0xDB,"tslp_tsk");
        put(0xD8,"can_wup"); put(0xD9,"wup_tsk");
        put(0xE9,"sta_tsk"); put(0xEB,"ext_tsk"); put(0xE7,"ter_tsk");
        put(0xE4,"rot_rdq"); put(0xE5,"chg_pri"); put(0xE8,"get_tid"); put(0xEC,"ref_tsk");
        put(0xF8,"loc_cpu"); put(0xF9,"unl_cpu");
        put(0xBD,"chg_ilm"); put(0xBC,"ref_ilm");
        put(0xAD,"set_tim"); put(0xAE,"get_tim");
        put(0xF5,"unsupported_function");
    }
    static void put(int n, String s) { NAMES.put(n, s); }

    static final TreeMap<Integer, int[]> CALLS = new TreeMap<Integer, int[]>();

    static int  phase = 0;

    /** 一页的"有内容"统计 */
    static class PageStat {
        int pte; int nonZeroFF; int distinct; int ffRunMax;
        PageStat(int pte, int nz, int ds, int run) { this.pte=pte; this.nonZeroFF=nz; this.distinct=ds; this.ffRunMax=run; }
    }

    public static void main(String[] args) throws Exception {
        File   file        = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long   steps       = args.length > 1 ? Long.parseLong(args[1]) : 40000000L;
        String outDir      = args.length > 2 ? args[2] : "uishot";
        long   counterEvery= args.length > 3 ? Long.parseLong(args[3]) : 64L;
        long   counterStep = args.length > 4 ? Long.parseLong(args[4]) : 64L;

        new File(outDir).mkdirs();

        // 静音"未建模寄存器"噪声
        System.setErr(new PrintStream(new OutputStream() { public void write(int b) { } }));

        Prefs prefs = Prefs.load();
        EmulationFramework framework = new EmulationFramework(prefs);
        framework.initialize(Constants.CHIP_FR, file);
        Platform   platform   = framework.getPlatform(Constants.CHIP_FR);
        Emulator   emulator   = framework.getEmulator(Constants.CHIP_FR);
        FrCPUState cpuState   = (FrCPUState) platform.getCpuState();
        InterruptController irq = platform.getInterruptController();

        System.out.println("; loaded " + file.getName() + "  reset PC=0x" + Integer.toHexString(cpuState.pc).toUpperCase());

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);

        long i = 0, haltStreak = 0;
        int  counter = 0;
        boolean inDisp = false;
        long t0 = System.currentTimeMillis();
        try {
            for (; i < steps; i++) {
                int pc = cpuState.pc;

                if (pc == DISPATCH_IN) {
                    if (!inDisp) {
                        int n = cpuState.getReg(12) & 0xFF;
                        int[] c = CALLS.get(n);
                        if (c == null) { c = new int[1]; CALLS.put(n, c); }
                        c[0]++;
                        inDisp = true;
                    }
                } else {
                    inDisp = false;
                }

                if (i % counterEvery == 0) {
                    counter += counterStep;
                    try { platform.getMemory().store32(COUNTER, counter); } catch (Throwable ignored) { }
                }

                if (phase == 0) {
                    if (pc == HALT_PC) {
                        if (++haltStreak > 200) {
                            phase = 1; haltStreak = 0;
                            System.out.println("; 阶段 A 结束：第 " + i + " 步进入空闲任务 0x407DC");
                            for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                                if (t != null && !t.isActive()) t.setActive(true);
                            }
                            System.out.println("; 已手动激活全部定时器");
                        }
                    } else {
                        haltStreak = 0;
                    }
                } else {
                    for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                        if (t != null) { try { t.onClockTick(); } catch (Throwable ignored) { } }
                    }
                }

                emulator.onClockTick();

                if (i % 10000000 == 0 && i > 0) {
                    System.out.printf("; step=%d PC=0x%08X svc=%d %dms%n",
                            i, cpuState.pc, sum(CALLS), System.currentTimeMillis() - t0);
                }
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
        }
        System.out.printf("; 跑完 %d 步，系统调用 %d 次，最终 PC=0x%08X%n", i, sum(CALLS), cpuState.pc);

        // ---------- 扫描内存 ----------
        DebuggableMemory mem = platform.getMemory();
        final int NPAGE = mem.getNumPages();          // 0x10000
        final int PSIZE = mem.getPageSize();          // 0x10000

        List<PageStat> stats = new ArrayList<PageStat>();
        long mappedPages = 0;
        for (int p = 0; p < NPAGE; p++) {
            byte[] page = mem.getPageForAddress(p << 16);
            if (page == null || page.length < PSIZE) continue;
            mappedPages++;
            int nz = 0;
            boolean[] seen = new boolean[256];
            int ds = 0;
            int run = 0, runMax = 0;
            for (int k = 0; k < PSIZE; k++) {
                int v = page[k] & 0xFF;
                if (v != 0x00 && v != 0xFF) nz++;
                if (!seen[v]) { seen[v] = true; ds++; }
                if (v == 0xFF) { if (++run > runMax) runMax = run; } else run = 0;
            }
            if (nz > 0) stats.add(new PageStat(p, nz, ds, runMax));
        }
        System.out.printf("; 已映射页 %d / %d，其中有内容（含非 00/FF 字节）的页 %d 个%n",
                mappedPages, NPAGE, stats.size());

        // 合并成连续块
        List<int[]> blocks = new ArrayList<int[]>();   // {startPte, endPte(含), totalNZ}
        int idx = 0;
        while (idx < stats.size()) {
            int s = stats.get(idx).pte;
            long tot = stats.get(idx).nonZeroFF;
            int e = s;
            int j = idx + 1;
            while (j < stats.size() && stats.get(j).pte <= e + 4) {   // 允许最多 4 页空洞
                tot += stats.get(j).nonZeroFF;
                e = stats.get(j).pte;
                j++;
            }
            blocks.add(new int[]{s, e, (int) Math.min(tot, Integer.MAX_VALUE)});
            idx = j;
        }
        // 按总字节数排序
        java.util.Collections.sort(blocks, new java.util.Comparator<int[]>() {
            public int compare(int[] a, int[] b) { return b[2] - a[2]; }
        });

        System.out.println(";");
        System.out.println("; === 有内容的连续内存块（按非 00/FF 字节数排序，Top 25）===");
        System.out.printf("; %-14s %-14s %-10s %-8s%n", "起始地址", "结束地址", "页数", "有效字节");
        for (int b = 0; b < Math.min(25, blocks.size()); b++) {
            int[] bl = blocks.get(b);
            System.out.printf("; 0x%08X     0x%08X     %-10d %-8d%n",
                    bl[0] << 16, ((bl[1] + 1) << 16) - 1, bl[1] - bl[0] + 1, bl[2]);
        }

        // ---------- 对最大的块渲染候选画面 ----------
        System.out.println(";");
        System.out.println("; === 渲染候选 ===");
        int[] WIDTHS = {640, 480, 320, 800, 1024};
        int rendered = 0;
        for (int b = 0; b < Math.min(6, blocks.size()) && rendered < 12; b++) {
            int[] bl = blocks.get(b);
            int start = bl[0] << 16;
            int size  = (bl[1] - bl[0] + 1) << 16;
            if (size < 64 * 1024) continue;
            // 只渲染块的后半段（帧缓冲通常在区域靠后位置），避免全是代码
            for (int w : WIDTHS) {
                if (size < w * 100) continue;
                int h = Math.min(size / w, 480);
                if (h < 100) continue;
                int off = Math.max(0, size - w * h);
                try {
                    BufferedImage img = new BufferedImage(w, h, BufferedImage.TYPE_INT_RGB);
                    for (int y = 0; y < h; y++) {
                        for (int x = 0; x < w; x++) {
                            int v = mem.loadUnsigned8(start + off + y * w + x) & 0xFF;
                            int g = (v << 16) | (v << 8) | v;
                            img.setRGB(x, y, g);
                        }
                    }
                    String name = String.format("%s/blk%02d_0x%08X_g8_%dx%d.png",
                            outDir, b, start + off, w, h);
                    ImageIO.write(img, "png", new File(name));
                    rendered++;
                } catch (Throwable ignored) { }
                if (w != 640) continue;
            }
        }
        System.out.println("; 已渲染 " + rendered + " 张候选（灰度 8bpp，多种宽度）到 " + outDir + "/");

        // 把最大的 8 个块原样存盘，供离线细看
        for (int b = 0; b < Math.min(8, blocks.size()); b++) {
            int[] bl = blocks.get(b);
            int start = bl[0] << 16;
            int size  = (bl[1] - bl[0] + 1) << 16;
            if (size > 32 * 1024 * 1024) size = 32 * 1024 * 1024;
            File f = new File(String.format("%s/blk%02d_0x%08X.bin", outDir, b, start));
            mem.saveToFile(f, start, size);
            System.out.printf("; 已保存 blk%02d 0x%08X 长度 %d 字节 -> %s%n", b, start, size, f.getName());
        }

        System.out.println(";");
        System.out.println("; === 系统调用统计 ===");
        dump(CALLS);
        System.exit(0);
    }

    static int sum(TreeMap<Integer, int[]> m) {
        int s = 0;
        for (int[] v : m.values()) s += v[0];
        return s;
    }

    static void dump(TreeMap<Integer, int[]> m) {
        List<Map.Entry<Integer, int[]>> list = new ArrayList<Map.Entry<Integer, int[]>>(m.entrySet());
        java.util.Collections.sort(list, new java.util.Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<Integer, int[]> e : list) {
            int n = e.getKey();
            System.out.printf("; 0x%02X  %-22s %d%n",
                    n, NAMES.containsKey(n) ? NAMES.get(n) : "(未命名)", e.getValue()[0]);
        }
    }
}
