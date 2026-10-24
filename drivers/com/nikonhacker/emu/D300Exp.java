package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.memory.listener.fr.Expeed7000LogListener;
import com.nikonhacker.emu.memory.listener.fr.ExpeedProxyIoListener;
import com.nikonhacker.emu.peripherials.programmableTimer.ProgrammableTimer;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 把 0x7000xxxx 窗口（B 段 -> EXPEED）的全部读写录下来。
 *
 *   java com.nikonhacker.emu.D300Exp <固件> <步数> <trace 文件>
 *
 * 与 D300Go 的驱动骨架一致（确定性：忙等计数器按仿真步推进、定时器手动激活），
 * 唯一的差别是挂上了 Expeed7000LogListener 探针。
 */
public class D300Exp {

    static final int DISPATCH_IN = 0x00040668;
    static final int HALT_PC     = 0x000407DC;
    static final int COUNTER     = 0x5C003034;

    static final TreeMap<Integer, int[]> CALLS = new TreeMap<Integer, int[]>();
    static int phase = 0;

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 60000000L;
        String tracePath = args.length > 2 ? args[2] : "expeed7000.tsv";
        int  mode  = args.length > 3 ? Integer.parseInt(args[3]) : 0;  // 1 = 用修正版路由守卫
        ExpeedProxyIoListener.routeAll = (mode >= 1);

        // 把外设告警接到一个只保留前 20000 字符的缓冲里（原来直接丢弃）
        final StringBuilder warnBuf = new StringBuilder();
        System.setErr(new PrintStream(new OutputStream() {
            public void write(int b) { if (warnBuf.length() < 20000) warnBuf.append((char) b); }
        }));

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

        PrintWriter tw = new PrintWriter(new FileOutputStream(tracePath));
        Expeed7000LogListener.trace = tw;
        Expeed7000LogListener.enabled = true;

        long i = 0, haltStreak = 0;
        int counter = 0;
        boolean inDisp = false;
        long t0 = System.currentTimeMillis();
        long enterIdle = -1;
        try {
            for (; i < steps; i++) {
                Expeed7000LogListener.step = i;
                int pc = cpuState.pc;

                if (pc == DISPATCH_IN) {
                    if (!inDisp) {
                        int n = cpuState.getReg(12) & 0xFF;
                        int[] c = CALLS.get(n);
                        if (c == null) { c = new int[1]; CALLS.put(n, c); }
                        c[0]++;
                        inDisp = true;
                    }
                } else inDisp = false;

                if (i % 64 == 0) {
                    counter += 64;
                    try { mem.store32(COUNTER, counter); } catch (Throwable ignored) { }
                }

                if (phase == 0) {
                    if (pc == HALT_PC) {
                        if (++haltStreak > 200) {
                            phase = 1; haltStreak = 0; enterIdle = i;
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

                emulator.onClockTick();
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
            t.printStackTrace(System.out);
            Throwable c = t.getCause();
            while (c != null) { System.out.println("; caused by: " + c); c.printStackTrace(System.out); c = c.getCause(); }
        }
        Expeed7000LogListener.enabled = false;
        tw.flush(); tw.close();

        long nRead = 0, nWrite = 0, nAddr = 0;
        for (int k = 0; k < 0x10000; k++) {
            nRead  += Expeed7000LogListener.rc[k];
            nWrite += Expeed7000LogListener.wc[k];
            if (Expeed7000LogListener.rc[k] > 0 || Expeed7000LogListener.wc[k] > 0) nAddr++;
        }
        int nCall = 0;
        for (int[] v : CALLS.values()) nCall += v[0];

        System.out.printf("; 共 %d 步，用时 %dms，最终 PC=0x%08X，进入空闲=%d%n",
                i, System.currentTimeMillis() - t0, cpuState.pc, enterIdle);
        System.out.println("; 系统调用 " + nCall + " 次");
        System.out.println(";");
        System.out.println("; === 0x7000 窗口总量 ===");
        System.out.println("; 读 " + nRead + " 次 / 写 " + nWrite + " 次 / 涉及地址 " + nAddr + " 个");
        System.out.println("; 轨迹写入 " + tracePath + "（" + Expeed7000LogListener.nTrace + " 条）");
        System.out.println(";");
        System.out.println("; === 写最多的地址 Top 40 ===");
        Expeed7000LogListener.dumpStats(System.out, true);
        System.out.println(";");
        System.out.println("; === 读最多的地址 Top 40 ===");
        Expeed7000LogListener.dumpStats(System.out, false);
        System.out.println(";");
        System.out.println("; === 访问 0x7000 的 PC 热点 Top 25 ===");
        Expeed7000LogListener.dumpPc(System.out);
        System.out.println(";");
        System.out.println("; === 外设告警（前 2000 字符）===");
        System.out.println(warnBuf.length() == 0 ? "; （无）"
                : warnBuf.substring(0, Math.min(2000, warnBuf.length())));
        System.exit(0);
    }
}
