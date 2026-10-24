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
 * 录制 B 段 -> EXPEED 的位串行字节流。
 *
 * 总线（由 0x19EEF8 / 0x19EF3C 的反汇编确定）：
 *   0x70000002 bit2 = 片选 CS（BORL #0x4 选中 / BANDL #0xB 释放）
 *   0x7000000A bit1 = 时钟 CLK（BANDL #0xD 拉低 / BORL #0x2 拉高）
 *   0x7000000A bit2 = 双向数据 DATA（发送 BORL #0x4 / BANDL #0xB；接收 BTSTL #0x4）
 *   0x7000000A bit0 = 另一条控制线（BANDL #0xE 拉低 / BORL #0x1 拉高）
 *
 * 钩子：
 *   0x0019EEF8 = 发送函数入口（此时 R4 = 待发字节）
 *   0x0019EF3C = 接收函数入口 / 0x0019EF6E = 出口（此时 R11 = 收到字节）
 *
 *   java com.nikonhacker.emu.D300Stream <固件> <步数> <输出 tsv>
 */
public class D300Stream {

    static final int HALT_PC  = 0x000407DC;
    static final int COUNTER  = 0x5C003034;
    static final int SEND     = 0x0019EEF8;
    static final int RECV_IN  = 0x0019EF3C;
    static final int RECV_OUT = 0x0019EF6E;
    static final int REG_CS   = 0x70000002;
    static final int REG_PORT = 0x7000000A;

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 60000000L;
        String out  = args.length > 2 ? args[2] : "stream.tsv";
        int   mode  = args.length > 3 ? Integer.parseInt(args[3]) : 0;  // 1 = 让 EXPEED 应答 0x00

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
        w.println("step\tcaller\tdir\tbyte\tcs\ta0");

        final Map<String, int[]> seq2 = new LinkedHashMap<String, int[]>();
        long i = 0, haltStreak = 0;
        int counter = 0, prevPc = 0, recvCaller = 0;
        int nSend = 0, nRecv = 0, nCsOn = 0;
        long firstStep = -1, lastStep = -1;
        int phase = 0;
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

                int cs = 0, a0 = 0;
                if (pc == SEND || pc == RECV_IN || pc == RECV_OUT) {
                    int p = mem.loadUnsigned8(REG_CS), q = mem.loadUnsigned8(REG_PORT);
                    cs = (p >> 2) & 1; a0 = q & 1;
                }

                // 去抖：一条多周期指令会连续几拍 PC 不变，只在「PC 刚跳进来」时记一次
                boolean entered = (pc != prevPc);
                if (entered && pc == SEND) {
                    nSend++; if (firstStep < 0) firstStep = i; lastStep = i;
                    if (cs == 1) nCsOn++;
                    w.printf("%d\t%s\tS\t0x%02X\t%d\t%d%n", i, Format.asHex(prevPc, 8),
                            cpuState.getReg(4) & 0xFF, cs, a0);
                    if (nSend < 400000) {
                        String k = Format.asHex(prevPc, 8);
                        int[] c = seq2.get(k);
                        if (c == null) { c = new int[1]; seq2.put(k, c); }
                        c[0]++;
                    }
                } else if (entered && pc == RECV_IN) {
                    recvCaller = prevPc;
                } else if (entered && pc == RECV_OUT) {
                    nRecv++; if (firstStep < 0) firstStep = i; lastStep = i;
                    w.printf("%d\t%s\tR\t0x%02X\t%d\t%d%n", i, Format.asHex(recvCaller, 8),
                            cpuState.getReg(11) & 0xFF, cs, a0);
                }

                prevPc = pc;
                emulator.onClockTick();

                // 桩：只在接收函数体内把数据线拉低，让每次读回 0x00（模拟 EXPEED 有应答）。
                // 选函数体内是因为发送函数也用同一条数据线，不能干扰它。
                if (mode >= 1 && pc >= RECV_IN && pc <= 0x0019EF76) {
                    try {
                        mem.store8(REG_PORT, mem.loadUnsigned8(REG_PORT) & ~0x04);
                    } catch (Throwable ignored) { }
                }
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
        }
        w.flush(); w.close();

        System.out.printf("; 共 %d 步，最终 PC=0x%08X%n", i, cpuState.pc);
        System.out.println("; 发送 " + nSend + " 字节 / 接收 " + nRecv + " 字节");
        System.out.println("; 发送时 CS=1 的有 " + nCsOn + " 次");
        System.out.println("; 首次总线活动 step=" + firstStep + "，最后一次 step=" + lastStep);
        System.out.println(";");
        System.out.println("; === 发送调用点 Top 20 ===");
        java.util.List<Map.Entry<String, int[]>> l =
                new java.util.ArrayList<Map.Entry<String, int[]>>(seq2.entrySet());
        java.util.Collections.sort(l, new java.util.Comparator<Map.Entry<String, int[]>>() {
            public int compare(Map.Entry<String, int[]> a, Map.Entry<String, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        int k = 0;
        for (Map.Entry<String, int[]> e : l) {
            if (k++ >= 20) break;
            System.out.printf("; 0x%s   %d%n", e.getKey(), e.getValue()[0]);
        }
        System.out.println("; 调用点共 " + seq2.size() + " 个");
        System.exit(0);
    }
}
