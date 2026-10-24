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
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 第二十一轮：不打 flag 补丁，只补中断，看固件能不能自己走完显示管线。
 *
 * 静态解出的链路：
 *   IRQ 0x1B (Serial IF 0 共享) -> 向量槽 0x000EFF90 -> ISR 0x0012FDB4 (RETI)
 *        按 0x9F58DA86 索引函数指针表 0x001BC490（21 项）
 *   IRQ 0x20 (EXT9)             -> 向量槽 0x000EFF7C -> ISR 0x0012FE54 (RETI)
 *        按 0x9F58DA85 索引函数指针表 0x001BC480（4 项）
 *   表 0x1BC480 共 25 项，全部落在 0x12F778..0x12FD9E
 *   其中 0x12F77A / 0x12F952 / 0x12FAEA / 0x12FD9E 会 set_flg(3, 0x10)
 *   —— 正是显示管线 0x12E162 / 0x12E326 死等的那个 bit4
 *
 * mode: 0=基线  1=周期打 0x1B  2=周期打 0x20  3=两个都周期打
 *       5..8=状态驱动  9/10=IRQ20周期+IRQ1B状态驱动  11=IRQ1B 电平触发
 *
 *   java com.nikonhacker.emu.D300Isr <固件> <步数> <mode> <周期> <输出>
 */
public class D300Isr {

    static final int HALT_PC  = 0x000407DC;
    static final int COUNTER  = 0x5C003034;
    static final int FLG_BASE = 0x680008B8;
    static int flgBits(int id) { return FLG_BASE + (id - 1) * 0x10 + 8; }

    static final int IRQ_SERIAL = 0x1B;   // Serial IF 0 shared
    static final int IRQ_EXT9   = 0x20;   // EXT interrupt 9
    static final int ISR_SERIAL = 0x0012FDB4;
    static final int ISR_EXT9   = 0x0012FE54;

    /** 函数指针表 0x1BC480 的 25 项 */
    static final int[] HANDLERS = {
        0x0012FE12, 0x0012FE14, 0x0012FE20, 0x0012FE48,
        0x0012F778, 0x0012F77A, 0x0012F798, 0x0012F7DC,
        0x0012F82C, 0x0012F888, 0x0012F8EC, 0x0012F952,
        0x0012F968, 0x0012F9C0, 0x0012FA1C, 0x0012FA3E,
        0x0012FA90, 0x0012FAEA, 0x0012FB22, 0x0012FB76,
        0x0012FBC6, 0x0012FC52, 0x0012FCA6, 0x0012FCF6,
        0x0012FD9E,
    };

    /** 管线与状态监控点 */
    static final int[] WATCH = {
        0x0012DFAE, 0x0012E162, 0x0012E1EC, 0x0012E326, 0x0012E412, 0x0012E674, 0x0012E6F8,
        0x0012FDB4, 0x0012FE54,
        0x00198062, 0x00242DA0,
    };
    static final String[] WATCH_NAME = {
        "0x12DFAE 管线循环", "0x12E162 等flag3", "0x12E1EC 选对象", "0x12E326 取数据",
        "0x12E412 拷数据", "0x12E674 ★绘制", "0x12E6F8 插入链表",
        "0x12FDB4 ISR(IRQ1B)", "0x12FE54 ISR(IRQ20)",
        "0x198062 任务19", "0x242DA0 任务21",
    };

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 20000000L;
        int mode   = args.length > 2 ? Integer.parseInt(args[2]) : 1;
        long period= args.length > 3 ? Long.parseLong(args[3]) : 2000L;
        String out = args.length > 4 ? args[4] : "isr1.out";

        System.setErr(new PrintStream(new OutputStream() { public void write(int b) { } }));
        PrintStream realOut = System.out;
        System.setOut(new PrintStream(new OutputStream() { public void write(int b) { } }));

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
        w.println("mode=" + mode + "  period=" + period + "  steps=" + steps);

        final Map<Integer, int[]> hits = new LinkedHashMap<Integer, int[]>();
        for (int a : WATCH)    hits.put(a, new int[1]);
        for (int a : HANDLERS) hits.put(a, new int[1]);

        final Map<Integer, int[]> chkHist = new LinkedHashMap<Integer, int[]>();
        StringBuilder events = new StringBuilder();
        int prevPc = 0;
        long i = 0, nFire1B = 0, nFire20 = 0, nIsr1B = 0, nIsr20 = 0;
        int haltStreak = 0, counter = 0, phase = 0;
        boolean fired1B = false, fired20 = false;
        int lastFlg3 = -1, last86 = -1, last85 = -1;

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
                            events.append("# step ").append(i).append(" 启动定时器\n");
                        }
                    } else haltStreak = 0;
                } else {
                    for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                        if (t != null) { try { t.onClockTick(); } catch (Throwable ignored) { } }
                    }
                }

                boolean entered = (pc != prevPc);
                if (entered) {
                    int[] h = hits.get(pc);
                    if (h != null) h[0]++;
                }

                // ---- 两处「flag3 & 0x74」判定点：记录实际读到的值 ----
                if (pc == 0x0012E1E0 || pc == 0x0012E404) {
                    int v = cpuState.getReg(0) & 0xFF;
                    Integer key = Integer.valueOf((pc == 0x0012E1E0 ? 0 : 0x100) | v);
                    int[] c = chkHist.get(key);
                    if (c == null) { c = new int[1]; chkHist.put(key, c); }
                    c[0]++;
                }

                // ---- 中断被接受 → 撤销（边沿触发）----
                if (fired1B && pc == ISR_SERIAL && mode != 11) {
                    platform.getInterruptController().removeRequest(IRQ_SERIAL);
                    fired1B = false; nIsr1B++;
                }
                if (fired20 && pc == ISR_EXT9) {
                    platform.getInterruptController().removeRequest(IRQ_EXT9);
                    fired20 = false; nIsr20++;
                }
                if (mode == 11 && pc == ISR_SERIAL) nIsr1B++;

                // ---- 发中断 ----
                if (phase == 1) {
                    if (mode == 11) {
                        // 电平触发：状态字节非 0 就一直挂着 IRQ1B，让 CPU 反复进 ISR，
                        // 直到状态机跑完（DA86 归 0）才撤销 —— 中间不给任务插空的机会
                        int s86 = rd8(mem, 0x9F58DA86);
                        if (s86 != 0) {
                            if (!fired1B) {
                                platform.getInterruptController().request(IRQ_SERIAL);
                                fired1B = true; nFire1B++;
                            }
                        } else if (fired1B) {
                            platform.getInterruptController().removeRequest(IRQ_SERIAL);
                            fired1B = false;
                        }
                        if (period > 0 && (i % period) == 0 && !fired20) {
                            platform.getInterruptController().request(IRQ_EXT9);
                            fired20 = true; nFire20++;
                        }
                    } else if (mode >= 5 && mode <= 8) {
                        int s86 = rd8(mem, 0x9F58DA86);
                        int s85 = rd8(mem, 0x9F58DA85);
                        boolean want1B = (mode == 5 || mode == 6 || mode == 8) && s86 != 0;
                        boolean want20 = ((mode == 5 || mode == 7) && s85 != 0)
                                      || (mode == 8 && s85 != 0 && s86 == 0);
                        if (want1B && !fired1B && (i % 4) == 0) {
                            platform.getInterruptController().request(IRQ_SERIAL);
                            fired1B = true; nFire1B++;
                        }
                        if (want20 && !fired20 && (i % 4) == 0) {
                            platform.getInterruptController().request(IRQ_EXT9);
                            fired20 = true; nFire20++;
                        }
                    } else if (mode == 9 || mode == 10) {
                        long thr = (mode == 10) ? 1 : 8;
                        if (period > 0 && (i % period) == 0 && !fired20) {
                            platform.getInterruptController().request(IRQ_EXT9);
                            fired20 = true; nFire20++;
                        }
                        if ((i % thr) == 0 && !fired1B && rd8(mem, 0x9F58DA86) != 0) {
                            platform.getInterruptController().request(IRQ_SERIAL);
                            fired1B = true; nFire1B++;
                        }
                    } else if (period > 0 && (i % period) == 0) {
                        if ((mode == 1 || mode == 3) && !fired1B) {
                            platform.getInterruptController().request(IRQ_SERIAL);
                            fired1B = true; nFire1B++;
                        }
                        if ((mode == 2 || mode == 3) && !fired20) {
                            platform.getInterruptController().request(IRQ_EXT9);
                            fired20 = true; nFire20++;
                        }
                    }
                }

                // ---- 状态字节变化 ----
                if ((i % 1000) == 0) {
                    int f3 = rd32(mem, flgBits(3));
                    int s86 = rd8(mem, 0x9F58DA86);
                    int s85 = rd8(mem, 0x9F58DA85);
                    if (f3 != lastFlg3 || s86 != last86 || s85 != last85) {
                        events.append("# step ").append(i)
                              .append(" flag3=0x").append(Format.asHex(f3, 8))
                              .append(" DA85=").append(Format.asHex(s85, 2))
                              .append(" DA86=").append(Format.asHex(s86, 2)).append("\n");
                        lastFlg3 = f3; last86 = s86; last85 = s85;
                    }
                }

                prevPc = pc;
                emulator.onClockTick();
            }
        } catch (Throwable t) {
            w.println("; stopped at step " + i + ": " + t);
        }

        w.println("steps=" + i + "  finalPC=" + Format.asHex(cpuState.pc, 8));
        w.println("发出 IRQ 0x1B " + nFire1B + " 次，进入 ISR " + nIsr1B + " 次");
        w.println("发出 IRQ 0x20 " + nFire20 + " 次，进入 ISR " + nIsr20 + " 次");
        w.println();

        w.println("--- 管线 / ISR 命中 ---");
        for (int k = 0; k < WATCH.length; k++) {
            w.println("  " + WATCH_NAME[k] + "  x" + hits.get(WATCH[k])[0]);
        }

        w.println();
        w.println("--- 分发处理函数命中（表 0x1BC480 + idx*4）---");
        for (int k = 0; k < HANDLERS.length; k++) {
            int c = hits.get(HANDLERS[k])[0];
            if (c > 0) {
                w.println("  idx=" + k + "  0x" + Format.asHex(HANDLERS[k], 8) + "  x" + c);
            }
        }
        w.println("（未列出的 = 0 次）");

        w.println();
        w.println("--- 「flag3 & 0x74」判定点读到的值 ---");
        for (Map.Entry<Integer, int[]> e : chkHist.entrySet()) {
            int k = e.getKey().intValue();
            w.println("  " + ((k >= 0x100) ? "0x12E404 取数据后" : "0x12E1E0 等flag3后") + "  读到 0x"
                    + Format.asHex(k & 0xFF, 2) + "  x" + e.getValue()[0]
                    + (((k & 0xFF) == 0x10) ? "   <<< 通过" : ""));
        }

        w.println();
        w.println("--- 关键内存 ---");
        int[] keys = { 0x9F58DA85, 0x9F58DA86, 0x9F58DA82, 0x9F58D858, 0x9F58D500, 0x9F58D87B };
        String[] kn = { "DA85 状态(IRQ20用)", "DA86 状态(IRQ1B用)", "当前对象id",
                        "描述符指针", "链表头", "0x9F58D87B 模式开关" };
        for (int k = 0; k < keys.length; k++) {
            w.println("  " + kn[k] + " (0x" + Format.asHex(keys[k], 8) + ") = 0x"
                    + Format.asHex(rd32(mem, keys[k]), 8));
        }
        w.println("  flag3 位模式 = 0x" + Format.asHex(rd32(mem, flgBits(3)), 8));

        w.println();
        w.println("--- 文本缓冲 0x9F58D97C 起 48 字节 ---");
        StringBuilder hx = new StringBuilder(), as = new StringBuilder();
        for (int k = 0; k < 48; k++) {
            int b = rd8(mem, 0x9F58D97C + k);
            hx.append(String.format("%02X ", b));
            as.append((b >= 32 && b < 127) ? (char) b : '.');
        }
        w.println("  " + hx);
        w.println("  |" + as + "|");

        w.println();
        w.println("--- 直接页 I/O 寄存器 0x30-0x6F ---");
        for (int a = 0x30; a < 0x70; a++) {
            int b;
            try { b = mem.loadUnsigned8(a); } catch (Throwable t) { b = -1; }
            w.print((b < 0) ? " ?? " : String.format(" %02X ", b));
            if ((a & 0x0F) == 0x0F) w.println("   <- 0x" + Format.asHex(a & 0xF0, 2));
        }

        w.println();
        w.println("--- 事件时间线（前 200 条）---");
        String[] ls = events.toString().split("\n");
        for (int k = 0; k < Math.min(ls.length, 200); k++) w.println(ls[k]);
        w.println("（共 " + ls.length + " 条）");

        w.flush(); w.close();
        realOut.println("; mode=" + mode + " period=" + period + " isr1B=" + nIsr1B
                + " isr20=" + nIsr20 + " -> " + out);
        System.exit(0);
    }

    static int rd8(DebuggableMemory mem, int a) {
        try { return mem.loadUnsigned8(a); } catch (Throwable t) { return 0; }
    }

    static int rd32(DebuggableMemory mem, int a) {
        try { return mem.load32(a); } catch (Throwable t) { return -1; }
    }
}
