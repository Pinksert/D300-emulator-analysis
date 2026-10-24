package com.nikonhacker.emu.memory.listener.fr;

import com.nikonhacker.Format;
import com.nikonhacker.emu.Platform;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.memory.listener.IoActivityListener;

import java.io.PrintWriter;

/**
 * 0x7000xxxx 窗口（B 段 -> EXPEED）的透传记录器。
 *
 * 设计约束：所有 onLoad* 一律返回 null（不改写读回值），onStore* 不动 pageData，
 * 因此接入前后固件行为完全不变 —— 它只是一个探针，不是外设模型。
 *
 * 记录三类信息：
 *   1) 每个字节地址的读次数 / 写次数 / 最后一次写入值
 *   2) 顺序事件流（前 MAX_TRACE 条写盘，含步号、PC、方向、宽度、值）
 *   3) 触发访问的 PC 热点
 */
public class Expeed7000LogListener extends IoActivityListener {

    public static final int PAGE = 0x7000;

    public static boolean   enabled = false;
    public static PrintWriter trace = null;
    public static long      step    = 0;
    public static int       maxTrace = 300000;
    public static int       nTrace  = 0;

    /** 每字节地址统计，下标 = 地址低 16 位 */
    public static final int[] rc = new int[0x10000];
    public static final int[] wc = new int[0x10000];
    public static final int[] lastW = new int[0x10000];
    /** 按 PC 统计访问次数 */
    public static final java.util.HashMap<Integer, int[]> pcHot =
            new java.util.HashMap<Integer, int[]>();

    public Expeed7000LogListener(Platform platform, boolean logRegisterMessages) {
        super(platform, logRegisterMessages);
    }

    private void rec(char op, int width, int address, int value) {
        if (!enabled) return;
        int off = address & 0xFFFF;
        if (op == 'R') rc[off]++; else { wc[off]++; lastW[off] = value; }

        int pc = platform.getCpuState().pc;
        int[] c = pcHot.get(pc);
        if (c == null) { c = new int[1]; pcHot.put(pc, c); }
        c[0]++;

        if (trace != null && nTrace < maxTrace) {
            nTrace++;
            trace.printf("%d\t%s\t%d\t0x%08X\t%c%d\t0x%08X%n",
                    step, Format.asHex(pc, 8), width, address, op, width, value);
        }
    }

    @Override public boolean matches(int address) { return true; }

    @Override public Byte onLoadData8(byte[] pageData, int address, byte value, DebuggableMemory.AccessSource src) {
        rec('R', 1, address, value & 0xFF);
        return null;
    }
    @Override public Integer onLoadData16(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        rec('R', 2, address, value & 0xFFFF);
        return null;
    }
    @Override public Integer onLoadData32(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        rec('R', 4, address, value);
        return null;
    }
    @Override public void onStore8(byte[] pageData, int address, byte value, DebuggableMemory.AccessSource src) {
        rec('W', 1, address, value & 0xFF);
    }
    @Override public void onStore16(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        rec('W', 2, address, value & 0xFFFF);
    }
    @Override public void onStore32(byte[] pageData, int address, int value, DebuggableMemory.AccessSource src) {
        rec('W', 4, address, value);
    }

    /** 排序输出访问最多的地址 */
    public static void dumpStats(java.io.PrintStream ps, boolean byWrite) {
        int[] arr = byWrite ? wc : rc;
        java.util.List<int[]> rows = new java.util.ArrayList<int[]>();
        for (int i = 0; i < 0x10000; i++) if (arr[i] > 0) rows.add(new int[]{i, arr[i]});
        java.util.Collections.sort(rows, new java.util.Comparator<int[]>() {
            public int compare(int[] a, int[] b) { return b[1] - a[1]; }
        });
        int n = Math.min(40, rows.size());
        for (int i = 0; i < n; i++) {
            int[] r = rows.get(i);
            ps.printf("; 0x7000%04X   %s %6d   最后写入=0x%08X%n",
                    r[0], byWrite ? "写" : "读", r[1], lastW[r[0]]);
        }
        ps.println("; 有记录的地址共 " + rows.size() + " 个（按" + (byWrite ? "写" : "读") + "排序）");
    }

    public static void dumpPc(java.io.PrintStream ps) {
        java.util.List<java.util.Map.Entry<Integer, int[]>> l =
                new java.util.ArrayList<java.util.Map.Entry<Integer, int[]>>(pcHot.entrySet());
        java.util.Collections.sort(l, new java.util.Comparator<java.util.Map.Entry<Integer, int[]>>() {
            public int compare(java.util.Map.Entry<Integer, int[]> a, java.util.Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        int n = Math.min(25, l.size());
        for (int i = 0; i < n; i++) {
            java.util.Map.Entry<Integer, int[]> e = l.get(i);
            ps.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
        }
        ps.println("; 访问过 0x7000 窗口的 PC 共 " + pcHot.size() + " 个");
    }
}
