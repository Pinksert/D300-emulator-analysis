package com.nikonhacker.emu;

import com.nikonhacker.disassembly.OutputOption;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.peripherials.interruptController.fr.FrInterruptController;
import com.nikonhacker.emu.trigger.condition.AlwaysBreakCondition;
import com.nikonhacker.gui.component.disassembly.DisassemblyLogger;

import java.io.File;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;

/**
 * 无头（不带 GUI）运行 D300 固件的驱动：
 *   java com.nikonhacker.emu.D300Run <固件文件> <装载地址> <执行步数> [起始PC]
 * 例：
 *   java com.nikonhacker.emu.D300Run B3000111.bin 0x40000 200
 */
public class D300Run {

    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.out.println("usage: D300Run <file> <loadAddress> <steps> [startPc]");
            return;
        }
        File file = new File(args[0]);
        int loadAddress = (int) Long.parseLong(args[1].replace("0x", "").replace("0X", ""), 16);
        int steps = Integer.parseInt(args[2]);
        int startPc = (args.length > 3)
                ? (int) Long.parseLong(args[3].replace("0x", "").replace("0X", ""), 16)
                : loadAddress;

        // ---- 搭建最小平台（照抄 FrEmulatorTest 的初始化方式）----
        MasterClock masterClock = new MasterClock();
        Platform platform = new Platform(masterClock);

        FrCPUState cpuState = new FrCPUState(loadAddress);
        platform.setCpuState(cpuState);

        DebuggableMemory memory = new DebuggableMemory(false);
        memory.setLogMemoryMessages(false);
        platform.setMemory(memory);

        FrInterruptController interruptController = new FrInterruptController(platform);
        platform.setInterruptController(interruptController);

        FrEmulator emulator = new FrEmulator(platform);
        emulator.clearBreakConditions();
        emulator.addBreakCondition(new AlwaysBreakCondition());
        emulator.setContext(memory, cpuState, interruptController);

        // ---- 指令级执行日志 ----
        PrintWriter pw = new PrintWriter(new OutputStreamWriter(System.out, "UTF-8"), true);
        DisassemblyLogger logger = new DisassemblyLogger();
        logger.setRanges(null);                 // null = no filtering, log everything
        logger.getWriters().add(pw);
        logger.setLogging(true);
        emulator.setDisassemblyLogger(logger);
        emulator.setOutputOptions(java.util.EnumSet.of(OutputOption.ADDRESS, OutputOption.HEXCODE));

        // ---- load firmware ----
        memory.loadFile(file, loadAddress, false);
        long size = file.length();
        System.out.println("; loaded " + file.getName() + "  " + size + " bytes -> 0x"
                + Integer.toHexString(loadAddress).toUpperCase());
        System.out.println("; start PC = 0x" + Integer.toHexString(startPc).toUpperCase()
                + "   steps = " + steps);
        System.out.println("; --------------------------------------------------");

        cpuState.reset();
        cpuState.pc = startPc;

        int executed = 0;
        try {
            for (int i = 0; i < steps; i++) {
                emulator.onClockTick();
                executed++;
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + (executed + 1) + ": " + t);
        }

        System.out.println("; --------------------------------------------------");
        System.out.println("; executed " + executed + " steps, PC=0x"
                + Integer.toHexString(cpuState.pc).toUpperCase());
        dumpRegs(cpuState);
        pw.flush();
    }

    private static void dumpRegs(FrCPUState s) {
        StringBuilder sb = new StringBuilder("; regs: ");
        for (int i = 0; i < 16; i++) {
            sb.append(String.format("r%d=0x%08X ", i, s.getReg(i)));
            if (i % 8 == 7) sb.append("\n;        ");
        }
        System.out.println(sb);
        System.out.println(String.format("; PC=0x%08X  PS=0x%08X  CCR=%d  ILM=%d",
                s.pc, s.getPS(), s.getCCR(), s.getILM()));
    }
}
