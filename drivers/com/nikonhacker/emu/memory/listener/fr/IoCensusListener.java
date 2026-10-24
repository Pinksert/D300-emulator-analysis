package com.nikonhacker.emu.memory.listener.fr;

import com.nikonhacker.emu.Platform;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.memory.listener.IoActivityListener;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 全地址空间访问普查探针。
 *
 * 两级统计：
 *   1) 按 64K 页统计读/写次数  —— 回答「固件到底用了哪些外设页」
 *   2) 对片上 IO 区（地址 < 0x1000）逐字节统计 + PC 热点 —— 回答「谁在被轮询」
 *
 * 默认完全透传（onLoad* 返回 null、onStore* 不动 pageData），不改变固件行为。
 * 只有显式设置 forceAddr/forceVal 时才改写读回值，用于按键注入实验。
 */
public class IoCensusListener extends IoActivityListener {

    public static boolean enabled = false;

    /** 按页（地址高 16 位）统计 */
    public static final int[] pageRc = new int[0x10000];
    public static final int[] pageWc = new int[0x10000];

    /** 片上 IO 区（< 0x1000）逐字节统计 */
    public static final int IO_LIMIT = 0x1000;
    public static final int[] ioRc   = new int[IO_LIMIT];
    public static final int[] ioWc   = new int[IO_LIMIT];
    public static final int[] ioLast = new int[IO_LIMIT];

    /** 进入空闲任务之后（phase>=1）的统计，用于区分「初始化」和「轮询」 */
    public static final int[] ioRc2  = new int[IO_LIMIT];
    public static final int[] ioWc2  = new int[IO_LIMIT];

    /** 触发片上 IO 访问的 PC 热点 */
    public static final HashMap<Integer, int[]> pcHot = new HashMap<Integer, int[]>();

    /** 注入：把 forceAddr 处读回的低字节改写成 forceVal */
    public static int  forceAddr = -1;
    public static int  forceVal  = 0;
    public static long forceFrom = -1;
    public static long forceStep = 0;
    public static long forceHit  = 0;

    public static int  phase = 0;

    /** 关注区：界面文字（注入前 / 注入后分别计数） */
    public static final int[] watchLo = {0x001C0000, 0x003A0000};
    public static final int[] watchHi = {0x001E0000, 0x003B0000};
    public static final int[] wPre    = new int[2];
    public static final int[] wPost   = new int[2];
    public static int injectPhase = 0;   // 0 = 注入前，1 = 注入后

    /** 注入后被读取的界面文字地址（地址 -> 次数），以及读它的 PC（PC -> 次数） */
    public static final java.util.TreeMap<Integer, int[]> watchAddr =
            new java.util.TreeMap<Integer, int[]>();
    public static final java.util.HashMap<Integer, int[]> watchPc =
            new java.util.HashMap<Integer, int[]>();
    public static boolean recordWatch = false;

    public IoCensusListener(Platform platform, boolean logRegisterMessages) {
        super(platform, logRegisterMessages);
    }

    private void rec(char op, int address, int value) {
        int page = (address >>> 16) & 0xFFFF;
        if (op == 'R') {
            pageRc[page]++;
            for (int k = 0; k < 2; k++) {
                if (address >= watchLo[k] && address < watchHi[k]) {
                    if (injectPhase == 0) wPre[k]++; else wPost[k]++;
                    if (recordWatch && injectPhase == 1) {
                        int[] c = watchAddr.get(address);
                        if (c == null) { c = new int[1]; watchAddr.put(address, c); }
                        c[0]++;
                        Integer pc = platform.getCpuState().pc;
                        int[] p = watchPc.get(pc);
                        if (p == null) { p = new int[1]; watchPc.put(pc, p); }
                        p[0]++;
                    }
                }
            }
        } else pageWc[page]++;

        if ((address & 0xFFFF) < IO_LIMIT && page == 0x0000) {
            int a = address & 0xFFFF;
            if (op == 'R') {
                ioRc[a]++;
                if (phase >= 1) ioRc2[a]++;
            } else {
                ioWc[a]++; ioLast[a] = value;
                if (phase >= 1) ioWc2[a]++;
            }
            Integer pc = platform.getCpuState().pc;
            int[] c = pcHot.get(pc);
            if (c == null) { c = new int[1]; pcHot.put(pc, c); }
            c[0]++;
        }
    }

    /** 注入改写：只在 forceAddr 命中且已到 forceFrom 步之后生效 */
    private int patch(int address, int value) {
        if (forceAddr >= 0 && address == forceAddr && forceStep >= forceFrom) { forceHit++; return forceVal; }
        return value;
    }

    /**
     * 声明为「只读」→ 会被排在监听器队列末尾，也就是在外设模型改写之后才看到值。
     * 这样统计到的是 CPU 真正拿到的值；注入时也不会被外设模型覆盖回去。
     */
    @Override public boolean isReadOnly() { return true; }

    @Override public boolean matches(int address) { return true; }

    @Override public Byte onLoadData8(byte[] pageData, int address, byte value, DebuggableMemory.AccessSource src) {
        if (!enabled) return null;
        rec('R', address, value & 0xFF);
        if (forceAddr >= 0 && address == forceAddr && forceStep >= forceFrom) {
            forceHit++;
            return (byte) forceVal;
        }
        return null;
    }
    @Override public Integer onLoadData16(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        if (!enabled) return null;
        rec('R', address, value & 0xFFFF);
        if (forceAddr >= 0 && address == forceAddr && forceStep >= forceFrom) {
            forceHit++;
            return (value & 0xFF00) | (forceVal & 0xFF);
        }
        return null;
    }
    @Override public Integer onLoadData32(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        if (!enabled) return null;
        rec('R', address, value);
        if (forceAddr >= 0 && address == forceAddr && forceStep >= forceFrom) {
            forceHit++;
            return (value & 0xFFFFFF00) | (forceVal & 0xFF);
        }
        return null;
    }
    @Override public void onStore8(byte[] pageData, int address, byte value, DebuggableMemory.AccessSource src) {
        if (enabled) rec('W', address, value & 0xFF);
    }
    @Override public void onStore16(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        if (enabled) rec('W', address, value & 0xFFFF);
    }
    @Override public void onStore32(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        if (enabled) rec('W', address, value);
    }

    /* ---------------- 输出 ---------------- */

    public static void dumpPages(java.io.PrintStream ps, int topN) {
        List<int[]> rows = new ArrayList<int[]>();
        for (int i = 0; i < 0x10000; i++) {
            if (pageRc[i] > 0 || pageWc[i] > 0) rows.add(new int[]{i, pageRc[i], pageWc[i]});
        }
        Collections.sort(rows, new Comparator<int[]>() {
            public int compare(int[] a, int[] b) { return (b[1] + b[2]) - (a[1] + a[2]); }
        });
        ps.println("; 被访问过的页共 " + rows.size() + " 个（按读+写排序）");
        int n = Math.min(topN, rows.size());
        for (int i = 0; i < n; i++) {
            int[] r = rows.get(i);
            ps.printf("; 0x%04Xxxxx   读 %10d   写 %10d%n", r[0], r[1], r[2]);
        }
    }

    public static void dumpIo(java.io.PrintStream ps, int topN, boolean phase1Only) {
        int[] r = phase1Only ? ioRc2 : ioRc;
        int[] w = phase1Only ? ioWc2 : ioWc;
        List<int[]> rows = new ArrayList<int[]>();
        for (int i = 0; i < IO_LIMIT; i++) if (r[i] > 0 || w[i] > 0) rows.add(new int[]{i, r[i], w[i]});
        Collections.sort(rows, new Comparator<int[]>() {
            public int compare(int[] a, int[] b) { return (b[1] + b[2]) - (a[1] + a[2]); }
        });
        ps.println("; 片上 IO 区（<0x1000）有记录的地址共 " + rows.size() + " 个"
                + (phase1Only ? "（只统计进入空闲任务之后）" : "（全程）"));
        int n = Math.min(topN, rows.size());
        for (int i = 0; i < n; i++) {
            int[] x = rows.get(i);
            ps.printf("; 0x%04X   读 %10d   写 %10d   最后写入=0x%02X%n", x[0], x[1], x[2], ioLast[x[0]]);
        }
    }

    public static void dumpPc(java.io.PrintStream ps, int topN) {
        List<Map.Entry<Integer, int[]>> l = new ArrayList<Map.Entry<Integer, int[]>>(pcHot.entrySet());
        Collections.sort(l, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        int n = Math.min(topN, l.size());
        for (int i = 0; i < n; i++) {
            Map.Entry<Integer, int[]> e = l.get(i);
            ps.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
        }
        ps.println("; 访问过片上 IO 的 PC 共 " + pcHot.size() + " 个");
    }
}
