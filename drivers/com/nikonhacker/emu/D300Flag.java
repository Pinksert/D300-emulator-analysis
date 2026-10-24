package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Format;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.peripherials.programmableTimer.ProgrammableTimer;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 在 uITRON 分派器上抓「flag 号 = 3」的每一次系统调用，
 * 目的是找到到底有没有任务在等 set_flg(3, 0x08)（0x12E6F8 发出的重绘通知）。
 *
 * 钩子：0x00040668 = 分派器入口；服务号 = R12 的低 8 位（负数）；参数从 R4 起。
 *
 *   java com.nikonhacker.emu.D300Flag <固件> <步数> <输出>
 */
public class D300Flag {

    static final int HALT_PC  = 0x000407DC;
    static final int COUNTER  = 0x5C003034;
    static final int DISPATCH = 0x00040668;

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 20000000L;
        String out = args.length > 2 ? args[2] : "flag3.out";

        System.setErr(new PrintStream(new OutputStream() { public void write(int b) { } }));

        Prefs prefs = Prefs.load();
        EmulationFramework framework = new EmulationFramework(prefs);
        framework.initialize(Constants.CHIP_FR, file);
        Platform platform = framework.getPlatform(Constants.CHIP_FR);
        Emulator emulator = framework.getEmulator(Constants.CHIP_FR);
        FrCPUState cpuState = (FrCPUState) platform.getCpuState();
        platform.getInterruptController();
        DebuggableMemory mem = platform.getMemory();

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);

        PrintWriter w = new PrintWriter(new FileOutputStream(out));

        long i = 0, haltStreak = 0;
        int counter = 0, prevPc = 0, phase = 0;
        int nSvc = 0;
        final Map<Integer, int[]> bySvc = new LinkedHashMap<Integer, int[]>();
        final Map<String, int[]> flag3 = new LinkedHashMap<String, int[]>();

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

                boolean entered = (pc != prevPc);
                if (entered && pc == DISPATCH) {
                    nSvc++;
                    int svc = cpuState.getReg(12) & 0xFF;
                    if (svc >= 128) svc -= 256;                 // 负数服务号
                    if (svc < -176 || svc > -1) svc = 0;        // 越界的不算
                    int r4 = cpuState.getReg(4);
                    int r5 = cpuState.getReg(5);

                    int[] c = bySvc.get(svc);
                    if (c == null) { c = new int[1]; bySvc.put(svc, c); }
                    c[0]++;

                    w.printf("%d\t%d\t%s\t%s\t%s%n", i, svc,
                            Format.asHex(prevPc, 8), Format.asHex(r4, 8), Format.asHex(r5, 8));

                    if (r4 == 3) {
                        String k = String.format("svc=%d caller=%s r5=%s", svc,
                                Format.asHex(prevPc, 8), Format.asHex(r5, 8));
                        int[] f = flag3.get(k);
                        if (f == null) { f = new int[1]; flag3.put(k, f); }
                        f[0]++;
                    }
                }

                prevPc = pc;
                emulator.onClockTick();
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
        }

        w.println("# step\tsvc\tcaller\tr4\tr5");
        w.println("steps=" + i + "  finalPC=" + Format.asHex(cpuState.pc, 8) + "  syscall=" + nSvc);
        w.println();
        w.println("--- 服务号分布（含全部，正数=越界/未识别）---");
        List<Map.Entry<Integer, int[]>> l = new ArrayList<Map.Entry<Integer, int[]>>(bySvc.entrySet());
        Collections.sort(l, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<Integer, int[]> e : l) {
            w.printf("  svc=%-5d  x%d%n", e.getKey(), e.getValue()[0]);
        }
        w.println();
        w.println("--- 所有 R4==3（flag 号 3）的系统调用 ---");
        List<Map.Entry<String, int[]>> l2 = new ArrayList<Map.Entry<String, int[]>>(flag3.entrySet());
        Collections.sort(l2, new Comparator<Map.Entry<String, int[]>>() {
            public int compare(Map.Entry<String, int[]> a, Map.Entry<String, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<String, int[]> e : l2) {
            w.println("  " + e.getKey() + "  x" + e.getValue()[0]);
        }
        w.println("共 " + flag3.size() + " 种");
        w.flush(); w.close();

        System.out.println("; steps=" + i + " syscall=" + nSvc + " -> " + out);
        System.exit(0);
    }
}
