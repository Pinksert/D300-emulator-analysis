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
import java.io.DataOutputStream;

/**
 * 第二十一轮：B 段代码覆盖率。
 *
 * 目的：判定「哪些代码在仿真里根本没跑」——尤其是那些静态上确实存在的
 *       set_flg(3,0x10)（0x12F794 / 0x12F964 / 0x12FB10 / 0x12FDB0）。
 *
 *   java com.nikonhacker.emu.D300Cover <固件> <步数> <mode> <输出.txt> <位图.bin>
 *   mode: 0=基线  10=每步补 flag3 bit4（D300Draw 的 mode10 注入）
 */
public class D300Cover {

    static final int HALT_PC = 0x000407DC;
    static final int COUNTER = 0x5C003034;
    static final int FLG_BASE = 0x680008B8;
    static int flgBits(int id) { return FLG_BASE + (id - 1) * 0x10 + 8; }

    /** B 段代码区间（CPU 地址）：0x40000 .. 0x40000+固件长度(7MB) */
    static final int CODE_LO = 0x00040000;
    static int codeHi = 0x000740000;

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 20000000L;
        int mode   = args.length > 2 ? Integer.parseInt(args[2]) : 0;
        String out = args.length > 3 ? args[3] : "cover0.txt";
        String bmp = args.length > 4 ? args[4] : "cover0.bin";

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

        codeHi = CODE_LO + (int) file.length();
        // 每条指令一个字节（0=没跑，1=跑过）
        final byte[] cov = new byte[(codeHi - CODE_LO) / 2 + 2];

        PrintWriter w = new PrintWriter(new FileOutputStream(out));

        int prevPc = 0;
        long i = 0;
        int haltStreak = 0, counter = 0, phase = 0;
        long firstExec = -1;

        try {
            for (; i < steps; i++) {
                int pc = cpuState.pc;

                if (pc >= CODE_LO && pc < codeHi) {
                    int k = (pc - CODE_LO) >> 1;
                    if (cov[k] == 0) { cov[k] = 1; }
                }

                if (i % 64 == 0) {
                    counter += 64;
                    try { mem.store32(COUNTER, counter); } catch (Throwable ignored) { }
                }

                if (phase == 0) {
                    if (pc == HALT_PC) {
                        if (++haltStreak > 200) {
                            phase = 1; haltStreak = 0; firstExec = i;
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

                if (phase == 1 && mode == 10) {
                    int addr = flgBits(3);
                    int cur;
                    try { cur = mem.load32(addr); } catch (Throwable t) { cur = 0; }
                    int nv = (cur & ~0x00000064) | 0x00000012;
                    if (nv != cur) { try { mem.store32(addr, nv); } catch (Throwable ignored) { } }
                }

                prevPc = pc;
                emulator.onClockTick();
            }
        } catch (Throwable t) {
            w.println("; stopped at step " + i + ": " + t);
        }

        // ---------- 统计 ----------
        int executed = 0;
        for (int k = 0; k < cov.length; k++) if (cov[k] != 0) executed++;

        w.println("steps=" + i + "  finalPC=" + Format.asHex(cpuState.pc, 8)
                + "  启动定时器于 step " + firstExec);
        w.println("代码区间 0x" + Format.asHex(CODE_LO, 8) + " - 0x" + Format.asHex(codeHi, 8)
                + "  覆盖指令(半字槽) " + executed + " / " + cov.length);
        w.println();

        // 按 0x1000 页统计
        w.println("--- 每 4KB 页覆盖率（只列有执行或显示相关的页）---");
        w.println("page      executed/total   pct");
        for (int p = CODE_LO; p < codeHi; p += 0x1000) {
            int n = 0, tot = 0x1000 / 2;
            for (int k = 0; k < tot; k++) {
                int idx = ((p - CODE_LO) >> 1) + k;
                if (idx < cov.length && cov[idx] != 0) n++;
            }
            if (n > 0) {
                w.println("0x" + Format.asHex(p, 8) + "  " + n + "/" + tot
                        + "   " + (n * 100 / tot) + "%");
            }
        }

        // 指定关注点
        w.println();
        w.println("--- 关注点是否执行 ---");
        int[] watch = {
            0x0012F794, 0x0012F964, 0x0012FB10, 0x0012FDB0, // set_flg(3,0x10)
            0x0012F77A, 0x0012F952, 0x0012FAEA, 0x0012FD9E, // 它们所在函数入口
            0x0012DFAE, 0x0012E162, 0x0012E674,             // 显示管线
            0x00198062, 0x00242DA0,
            0x001200AC, 0x00175192, 0x00198004, 0x0012E6F8,
        };
        String[] wn = {
            "set_flg(3,0x10) @12F794", "set_flg(3,0x10) @12F964",
            "set_flg(3,0x10) @12FB10", "set_flg(3,0x10) @12FDB0",
            "函数入口 12F77A", "函数入口 12F952", "函数入口 12FAEA", "函数入口 12FD9E",
            "显示管线 12DFAE", "等flag3 12E162", "★绘制 12E674",
            "任务19 198062", "任务21 242DA0",
            "任务1 1200AC", "任务6 175192", "显示更新 198004", "插入链表 12E6F8",
        };
        for (int k = 0; k < watch.length; k++) {
            int a = watch[k];
            boolean ran = (a >= CODE_LO && a < codeHi) && cov[(a - CODE_LO) >> 1] != 0;
            w.println("  " + wn[k] + " : " + (ran ? "执行过" : "未执行"));
        }

        w.flush(); w.close();

        DataOutputStream d = new DataOutputStream(new FileOutputStream(bmp));
        d.write(cov);
        d.flush(); d.close();

        System.out.println("; steps=" + i + " mode=" + mode + " cov=" + executed + " -> " + out);
        System.exit(0);
    }
}
