package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.gui.component.disassembly.DisassemblyLogger;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;

/**
 * 用完整 EmulationFramework（含主时钟、可编程定时器、中断控制器、IO 端口、
 * LCD/JPEG/SD 等外设）无头运行 D300 固件。
 *
 *   java com.nikonhacker.emu.D300Full <固件> <运行毫秒数> [日志上限条数]
 */
public class D300Full {

    public static void main(String[] args) throws Exception {
        File file = new File(args.length > 0 ? args[0] : "B3000111.bin");
        int runMs = args.length > 1 ? Integer.parseInt(args[1]) : 8000;
        int logMax = args.length > 2 ? Integer.parseInt(args[2]) : 0;

        Prefs prefs = Prefs.load();
        EmulationFramework framework = new EmulationFramework(prefs);

        System.out.println("; loading " + file.getName() + " into " + Constants.CHIP_LABEL[Constants.CHIP_FR]);
        framework.initialize(Constants.CHIP_FR, file);

        Platform platform = framework.getPlatform(Constants.CHIP_FR);
        Emulator emulator = framework.getEmulator(Constants.CHIP_FR);
        FrCPUState cpuState = (FrCPUState) platform.getCpuState();

        System.out.println("; loaded. reset PC=0x" + Integer.toHexString(cpuState.pc).toUpperCase());
        System.out.println("; TBR will be set by firmware itself (vector table)");
        System.out.println("; --------------------------------------------------");

        // 指令日志（可限量，避免刷屏）
        PrintWriter logWriter = null;
        if (logMax > 0) {
            FileOutputStream fos = new FileOutputStream("trace_head.txt");
            logWriter = new PrintWriter(new OutputStreamWriter(fos, "UTF-8"), true);
            final int[] counter = {0};
            final PrintWriter lw = logWriter;
            final int max = logMax;
            DisassemblyLogger logger = new DisassemblyLogger() {
                @Override
                public void println(String s) {
                    if (counter[0] < max) {
                        counter[0]++;
                        lw.println(s);
                    }
                }
            };
            logger.setRanges(null);
            logger.setLogging(true);
            emulator.setDisassemblyLogger(logger);
        }

        // 最小外设打桩：给 0x5C003034（固件忙等所用的自由运行计数器）持续递增
        final int[] tick = {0};
        Thread stub = new Thread(() -> {
            while (true) {
                try { Thread.sleep(1); } catch (InterruptedException e) { return; }
                tick[0] += 1000;                       // 每次 +1000，模拟计数前进
                try {
                    platform.getMemory().store32(0x5C003034, tick[0]);
                } catch (Throwable ignored) { }
            }
        });
        stub.setDaemon(true);
        stub.start();
        System.out.println("; stub: free-running counter at 0x5C003034 enabled");

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);
        framework.getMasterClock().start();

        // 采样：每 500ms 打一次 PC
        long t0 = System.currentTimeMillis();
        int lastPc = -1;
        int stall = 0;
        while (System.currentTimeMillis() - t0 < runMs) {
            Thread.sleep(500);
            int pc = cpuState.pc;
            long instr = emulator.getTotalCycles();
            System.out.printf("; t=%4dms  PC=0x%08X  cycles=%d%n",
                    System.currentTimeMillis() - t0, pc, instr);
            if (pc == lastPc) {
                if (++stall >= 4) {
                    System.out.println("; PC unchanged for 2s -> firmware is spinning");
                    break;
                }
            } else {
                stall = 0;
            }
            lastPc = pc;
        }

        System.out.println("; --------------------------------------------------");
        System.out.println("; final PC=0x" + Integer.toHexString(cpuState.pc).toUpperCase()
                + "   cycles=" + emulator.getTotalCycles());
        System.out.printf("; PS=0x%08X CCR=%d ILM=%d%n", cpuState.getPS(), cpuState.getCCR(), cpuState.getILM());
        StringBuilder sb = new StringBuilder("; regs: ");
        for (int i = 0; i < 16; i++) {
            sb.append(String.format("r%d=0x%08X ", i, cpuState.getReg(i)));
            if (i % 8 == 7) sb.append("\n;        ");
        }
        System.out.println(sb);
        if (logWriter != null) logWriter.flush();
        System.exit(0);
    }
}
