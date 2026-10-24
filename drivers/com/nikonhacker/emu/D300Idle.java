package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.peripherials.interruptController.InterruptController;
import com.nikonhacker.emu.peripherials.programmableTimer.ProgrammableTimer;

import java.io.File;
import java.io.OutputStream;
import java.io.PrintStream;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * D300 固件"越过空闲任务"实验（确定性版）。
 *
 * 前一版用一个独立线程按真实时间给忙等计数器 0x5C003034 加值，
 * 结果机器负载不同 → 固件走的分支不同 → 两次跑出 55 / 161 次调用，不可重现。
 * 本版改成按仿真步推进（每 counterEvery 步 += counterStep），彻底确定。
 *
 * 阶段 A：跑到 0x407DC（空闲任务自转，其入口硬编码在 ROM 0x54F20）为止，作为基线
 * 阶段 B：从这里开始驱动外设，看固件能否被唤醒：
 *         - 每 interval 步给全部可编程定时器走一拍（它们是纯仿真计数，确定性）
 *         - 若 mode>=1，再额外手动申请一次轮换的中断，验证中断路径是否通
 *
 * 同时接管 System.err，把"未建模寄存器"消息按地址聚合，得到真实外设缺口清单。
 *
 *   java com.nikonhacker.emu.D300Idle <固件> <总步数> <喂中断间隔> <计数器每N步> <计数器增量> <mode>
 */
public class D300Idle {

    static final int DISPATCH_IN = 0x00040668;   // μITRON 分派器入口
    static final int HALT_PC     = 0x000407DC;   // 空闲任务自转
    static final int COUNTER     = 0x5C003034;   // 固件忙等所用的自由运行计数器

    /** 手动申请中断时轮换的中断号：重载定时器 0/1/2、32 位定时器、外部中断 0、串口 0 共享 */
    static final int[] TRY_IRQ = {0x18, 0x19, 0x1A, 0x2E, 0x10, 0x1B};

    static final Map<Integer, String> NAMES = new LinkedHashMap<Integer, String>();
    static {
        put(0xD0,"set_flg"); put(0xD1,"clr_flg"); put(0xD2,"wai_flg");
        put(0x96,"pol_flg"); put(0x56,"twai_flg");
        put(0xC9,"sig_sem"); put(0xCB,"wai_sem"); put(0x95,"preq_sem"); put(0xCC,"ref_sem");
        put(0xC1,"snd_msg");
        put(0xAB,"dly_tsk"); put(0xDA,"slp_tsk"); put(0xDB,"tslp_tsk");
        put(0xD8,"can_wup"); put(0xD9,"wup_tsk");
        put(0xE9,"sta_tsk"); put(0xEB,"ext_tsk"); put(0xE7,"ter_tsk");
        put(0xE4,"rot_rdq"); put(0xE5,"chg_pri"); put(0xE8,"get_tid"); put(0xEC,"ref_tsk");
        put(0xF8,"loc_cpu"); put(0xF9,"unl_cpu");
        put(0xBD,"chg_ilm"); put(0xBC,"ref_ilm");
        put(0xAD,"set_tim"); put(0xAE,"get_tim");
        put(0xF5,"unsupported_function");
    }
    static void put(int n, String s) { NAMES.put(n, s); }

    static final TreeMap<Integer, int[]> CALLS_A = new TreeMap<Integer, int[]>();
    static final TreeMap<Integer, int[]> CALLS_B = new TreeMap<Integer, int[]>();
    static final TreeMap<Integer, int[]> UNIMPL  = new TreeMap<Integer, int[]>();
    /** 阶段 B 里 PC 的直方图：看固件被唤醒后到底跑在哪段代码上 */
    static final TreeMap<Integer, int[]> PCHIST  = new TreeMap<Integer, int[]>();
    /** 各中断号被接受的次数 */
    static final TreeMap<Integer, int[]> IRQOK   = new TreeMap<Integer, int[]>();

    static int  phase = 0;
    static long irqAsked = 0, irqAccepted = 0;

    public static void main(String[] args) throws Exception {
        File   file        = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long   steps       = args.length > 1 ? Long.parseLong(args[1]) : 40000000L;
        long   interval    = args.length > 2 ? Long.parseLong(args[2]) : 2000L;
        long   counterEvery= args.length > 3 ? Long.parseLong(args[3]) : 64L;
        long   counterStep = args.length > 4 ? Long.parseLong(args[4]) : 64L;
        int    mode        = args.length > 5 ? Integer.parseInt(args[5]) : 1;

        // 接管 System.err，聚合"未建模寄存器"告警
        final PrintStream realErr = System.err;
        final Pattern pat = Pattern.compile("Register 0x([0-9A-Fa-f]+):\\s*(Load|Store)(\\d*)");
        final StringBuilder tail = new StringBuilder();
        System.setErr(new PrintStream(new OutputStream() {
            public void write(int b) {
                if (b == '\n') {
                    String line = tail.toString();
                    tail.setLength(0);
                    Matcher m = pat.matcher(line);
                    if (m.find()) {
                        int addr = (int) Long.parseLong(m.group(1), 16);
                        int[] c = UNIMPL.get(addr);
                        if (c == null) { c = new int[2]; UNIMPL.put(addr, c); }
                        if (m.group(2).equals("Load")) c[0]++; else c[1]++;
                    }
                } else if (b != '\r') {
                    tail.append((char) b);
                }
            }
        }));

        Prefs prefs = Prefs.load();
        EmulationFramework framework = new EmulationFramework(prefs);
        framework.initialize(Constants.CHIP_FR, file);
        Platform   platform   = framework.getPlatform(Constants.CHIP_FR);
        Emulator   emulator   = framework.getEmulator(Constants.CHIP_FR);
        FrCPUState cpuState   = (FrCPUState) platform.getCpuState();
        InterruptController irq = platform.getInterruptController();

        System.out.println("; loaded " + file.getName()
                + "   reset PC=0x" + Integer.toHexString(cpuState.pc).toUpperCase());
        System.out.printf("; 喂中断间隔=%d 步, 计数器每 %d 步 +=%d, mode=%d%n",
                interval, counterEvery, counterStep, mode);

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);

        long i = 0, haltStreak = 0, fed = 0;
        int  counter = 0, irqIdx = 0;
        boolean inDisp = false;
        long t0 = System.currentTimeMillis();
        try {
            for (; i < steps; i++) {
                int pc = cpuState.pc;

                if (pc == DISPATCH_IN) {
                    if (!inDisp) { record(cpuState); inDisp = true; }
                } else {
                    inDisp = false;
                }

                // 确定性计数器：按仿真步推进，不依赖真实时间
                if (i % counterEvery == 0) {
                    counter += counterStep;
                    try { platform.getMemory().store32(COUNTER, counter); } catch (Throwable ignored) { }
                }

                if (phase == 0) {
                    if (pc == HALT_PC) {
                        if (++haltStreak > 200) {
                            phase = 1; haltStreak = 0;
                            System.out.println("; === 阶段 A 结束：第 " + i
                                    + " 步进入 0x407DC（空闲任务）===");
                            System.out.println("; 阶段 A 系统调用合计 " + sum(CALLS_A) + " 次");
                            System.out.println("; --- 进入空闲任务时的定时器状态（固件自己配的）---");
                            for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                                if (t != null) System.out.println(";   " + t);
                            }
                            // 固件写 TMCSR 启动定时器时，仿真器只是把它注册到主时钟；
                            // 我们不启主时钟，active 就永远是 false、计数永不递减。
                            // 这里补上主时钟本该做的事：让已配置好的定时器真正跑起来。
                            for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                                if (t != null && !t.isActive()) t.setActive(true);
                            }
                            System.out.println("; --- 已手动激活全部定时器（补上未启用的主时钟）---");
                            System.out.println("; --- 中断控制器状态 ---");
                            System.out.println(";   " + irq.getStatus());
                            System.out.println("; === 阶段 B 开始：驱动外设，看能否唤醒 ===");
                        }
                    } else {
                        haltStreak = 0;
                    }
                } else {
                    // 阶段 B：定时器每步都走一拍，好让 OS 节拍真的溢出
                    // （阶段 A 里固件已把 timer0 配成 TMRLRA=33000 且开了中断）
                    fed++;
                    for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                        if (t != null) { try { t.onClockTick(); } catch (Throwable ignored) { } }
                    }
                    if (mode >= 1 && i % interval == 0) {
                        irqAsked++;
                        int no = TRY_IRQ[irqIdx];
                        if (irq.request(no)) {
                            irqAccepted++;
                            int[] c = IRQOK.get(no);
                            if (c == null) { c = new int[1]; IRQOK.put(no, c); }
                            c[0]++;
                        }
                        irqIdx = (irqIdx + 1) % TRY_IRQ.length;
                    }
                    // PC 直方图：只在离开空闲自转之后才统计，避免被自转淹没
                    if (pc != HALT_PC) {
                        int[] c = PCHIST.get(pc);
                        if (c == null) { c = new int[1]; PCHIST.put(pc, c); }
                        c[0]++;
                    }
                }

                emulator.onClockTick();

                if (i % 5000000 == 0 && i > 0) {
                    System.out.printf("; step=%d PC=0x%08X phase=%d A=%d B=%d irq %d/%d %dms%n",
                            i, cpuState.pc, phase, sum(CALLS_A), sum(CALLS_B),
                            irqAccepted, irqAsked, System.currentTimeMillis() - t0);
                }
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + " (phase " + phase + "): " + t);
        }

        System.setErr(realErr);
        report(i, fed, interval, System.currentTimeMillis() - t0, cpuState, mode);
        System.exit(0);
    }

    static void record(FrCPUState s) {
        int n = s.getReg(12) & 0xFF;
        TreeMap<Integer, int[]> dst = (phase == 0) ? CALLS_A : CALLS_B;
        int[] c = dst.get(n);
        if (c == null) { c = new int[1]; dst.put(n, c); }
        c[0]++;
    }

    static int sum(TreeMap<Integer, int[]> m) {
        int s = 0;
        for (int[] v : m.values()) s += v[0];
        return s;
    }

    static void report(long steps, long fed, long interval, long ms, FrCPUState s, int mode) {
        System.out.println("; ==================================================");
        System.out.printf("; 共 %d 步，喂外设 %d 次，用时 %dms，最终 PC=0x%08X%n", steps, fed, ms, s.pc);
        System.out.printf("; 手动申请中断 %d 次，被接受 %d 次（mode=%d）%n", irqAsked, irqAccepted, mode);
        System.out.println(";");
        System.out.println("; === 阶段 A（跑到空闲任务为止）===");
        dump(CALLS_A);
        System.out.println("; 合计 " + sum(CALLS_A));
        System.out.println(";");
        System.out.println("; === 阶段 B（喂外设之后新增的调用）===");
        if (sum(CALLS_B) == 0) {
            System.out.println("; （无 —— 固件在空闲任务里没有任何新动作）");
        } else {
            dump(CALLS_B);
            System.out.println("; 合计 " + sum(CALLS_B));
        }
        System.out.println(";");
        System.out.println("; === 各中断号被接受次数 ===");
        for (Map.Entry<Integer, int[]> e : IRQOK.entrySet()) {
            System.out.printf("; 中断 0x%02X   %d 次%n", e.getKey(), e.getValue()[0]);
        }
        if (IRQOK.isEmpty()) System.out.println("; （无）");
        System.out.println(";");
        System.out.println("; === 阶段 B 里 PC 落在哪里（离开空闲自转之后，Top 30）===");
        java.util.List<Map.Entry<Integer, int[]>> ph =
                new java.util.ArrayList<Map.Entry<Integer, int[]>>(PCHIST.entrySet());
        java.util.Collections.sort(ph, new java.util.Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        int shown = 0, distinct = PCHIST.size();
        for (Map.Entry<Integer, int[]> e : ph) {
            if (shown++ >= 30) break;
            System.out.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
        }
        System.out.println("; 阶段 B 出现过的不同 PC 共 " + distinct + " 个");
        System.out.println(";");
        System.out.println("; === 真实未建模外设（已排除 0x8000 起的片上 RAM）===");
        System.out.printf("; %-12s %-8s %-8s%n", "地址", "读", "写");
        for (Map.Entry<Integer, int[]> e : UNIMPL.entrySet()) {
            System.out.printf("; 0x%08X   %-8d %-8d%n", e.getKey(), e.getValue()[0], e.getValue()[1]);
        }
        System.out.println("; 未建模地址数 " + UNIMPL.size());
    }

    static void dump(TreeMap<Integer, int[]> m) {
        java.util.List<Map.Entry<Integer, int[]>> list =
                new java.util.ArrayList<Map.Entry<Integer, int[]>>(m.entrySet());
        java.util.Collections.sort(list, new java.util.Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<Integer, int[]> e : list) {
            int n = e.getKey();
            System.out.printf("; 0x%02X  %-22s %d%n",
                    n, NAMES.containsKey(n) ? NAMES.get(n) : "(未命名)", e.getValue()[0]);
        }
    }
}
