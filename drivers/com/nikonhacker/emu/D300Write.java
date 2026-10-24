package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.peripherials.interruptController.InterruptController;
import com.nikonhacker.emu.peripherials.programmableTimer.ProgrammableTimer;

import java.io.File;
import java.io.OutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 决定性问题：固件在运行时究竟往哪些内存地址写数据？
 *
 * 做法：每 checkEvery 步，对所有"已映射页"按 4KB 块算一次校验和，
 *      和上一轮比较，统计每个 4KB 块被改写的次数。
 *      改写最频繁的区域 = 固件的活动工作区；若存在"屏幕缓冲"，
 *      它必然出现在榜首（每帧都被整屏改写）。
 *
 *   java com.nikonhacker.emu.D300Write <固件> <总步数> <检查间隔步数>
 */
public class D300Write {

    static final int DISPATCH_IN = 0x00040668;
    static final int HALT_PC     = 0x000407DC;
    static final int COUNTER     = 0x5C003034;

    static int phase = 0;

    public static void main(String[] args) throws Exception {
        File file   = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps  = args.length > 1 ? Long.parseLong(args[1]) : 60000000L;
        long every  = args.length > 2 ? Long.parseLong(args[2]) : 2000000L;

        System.setErr(new PrintStream(new OutputStream() { public void write(int b) { } }));

        Prefs prefs = Prefs.load();
        EmulationFramework framework = new EmulationFramework(prefs);
        framework.initialize(Constants.CHIP_FR, file);
        Platform platform = framework.getPlatform(Constants.CHIP_FR);
        Emulator emulator = framework.getEmulator(Constants.CHIP_FR);
        FrCPUState cpuState = (FrCPUState) platform.getCpuState();
        InterruptController irq = platform.getInterruptController();
        DebuggableMemory mem = platform.getMemory();

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);

        final int PSIZE = mem.getPageSize();
        final int BLK   = 4096;

        // 4KB 块地址 -> {改写次数, 最后一次校验和, 不同字节峰值}
        final HashMap<Integer, int[]> changed = new HashMap<Integer, int[]>();
        final HashMap<Integer, Integer> prevCs = new HashMap<Integer, Integer>();

        long i = 0, haltStreak = 0;
        int counter = 0;
        long t0 = System.currentTimeMillis();
        try {
            for (; i < steps; i++) {
                int pc = cpuState.pc;

                if (i % 64 == 0) {
                    counter += 64;
                    try { mem.store32(COUNTER, counter); } catch (Throwable ignored) { }
                }

                if (phase == 0) {
                    if (pc == HALT_PC) {
                        if (++haltStreak > 200) {
                            phase = 1; haltStreak = 0;
                            System.out.println("; 第 " + i + " 步进入空闲任务，激活定时器");
                            for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                                if (t != null && !t.isActive()) t.setActive(true);
                            }
                        }
                    } else haltStreak = 0;
                } else {
                    for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                        if (t != null) { try { t.onClockTick(); } catch (Throwable ignored) { } }
                    }
                }

                if (i > 0 && i % every == 0) {
                    snapshot(mem, changed, prevCs, PSIZE, BLK);
                }

                emulator.onClockTick();
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
        }
        snapshot(mem, changed, prevCs, PSIZE, BLK);

        System.out.printf("; 共 %d 步，用时 %dms%n", i, System.currentTimeMillis() - t0);
        System.out.println(";");
        System.out.println("; === 运行中被改写过的 4KB 块（按改写次数排序）===");
        System.out.printf("; %-14s %-10s%n", "地址", "改写次数");
        List<Map.Entry<Integer, int[]>> list = new ArrayList<Map.Entry<Integer, int[]>>(changed.entrySet());
        Collections.sort(list, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<Integer, int[]> e : list) {
            System.out.printf("; 0x%08X     %d%n", e.getKey(), e.getValue()[0]);
        }
        System.out.println("; 被改写过的块共 " + list.size() + " 个");
        System.exit(0);
    }

    /** 对已映射页按 4KB 块算校验和，和上一轮比对 */
    static void snapshot(DebuggableMemory mem, HashMap<Integer, int[]> changed,
                         HashMap<Integer, Integer> prevCs, int PSIZE, int BLK) {
        int NPAGE = mem.getNumPages();
        int perPage = PSIZE / BLK;
        for (int p = 0; p < NPAGE; p++) {
            byte[] page = mem.getPageForAddress(p << 16);
            if (page == null || page.length < PSIZE) continue;
            for (int b = 0; b < perPage; b++) {
                int addr = (p << 16) | (b * BLK);
                int o = b * BLK;
                int cs = 0;
                for (int k = 0; k < BLK; k += 4) {
                    cs = cs * 31 + (page[o+k] & 0xFF)
                       + ((page[o+k+1] & 0xFF) << 8)
                       + ((page[o+k+2] & 0xFF) << 16)
                       + ((page[o+k+3] & 0xFF) << 24);
                }
                Integer old = prevCs.get(addr);
                if (old != null && old.intValue() != cs) {
                    int[] c = changed.get(addr);
                    if (c == null) { c = new int[1]; changed.put(addr, c); }
                    c[0]++;
                }
                prevCs.put(addr, cs);
            }
        }
    }
}
