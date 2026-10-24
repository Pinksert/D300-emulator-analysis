package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;

import java.io.File;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;

/**
 * 无头运行 D300 固件，并拦截 μITRON 系统调用做运行时统计。
 *
 * 做法：用完整 EmulationFramework 建平台（外设齐全），但不启动主时钟，
 * 改成自己一步一步 onClockTick()，每步前判断 PC 是否等于分派器入口。
 *
 * 分派器入口 = 0x00040668（中断 #0x40 → 向量表 #191）
 * 分派器出口 = 0x000406E2（RETI）
 * 服务号    = R12（8 位有符号负数）；参数按 μITRON/FR 惯例在 R4 起
 *
 *   java com.nikonhacker.emu.D300Sys <固件> <步数>
 */
public class D300Sys {

    static final int DISPATCH_IN  = 0x00040668;
    static final int DISPATCH_OUT = 0x000406E2;
    static final int HALT_PC      = 0x000407DC;
    static final int TASK_TBL     = 0x00054BFC;   // 任务表基址常量（sta_tsk 里抠出）
    static final int FLAG_TBL     = 0x000551B0;   // 事件标志表基址常量

    static final Map<Integer, String> NAMES = new LinkedHashMap<Integer, String>();
    static {
        put(0xD0, "set_flg");  put(0xD1, "clr_flg");  put(0xD2, "wai_flg");
        put(0x96, "pol_flg");  put(0xC9, "sig_sem");  put(0xCB, "wai_sem");
        put(0x95, "preq_sem"); put(0xCC, "ref_sem");  put(0xC1, "snd_msg");
        put(0xAB, "dly_tsk");  put(0xDA, "slp_tsk");  put(0xDB, "tslp_tsk");
        put(0xD8, "can_wup");  put(0xD9, "wup_tsk");  put(0xE9, "sta_tsk");
        put(0xEB, "ext_tsk");  put(0xE7, "ter_tsk");  put(0xE4, "rot_rdq");
        put(0xE5, "chg_pri");  put(0xE8, "get_tid");  put(0xEC, "ref_tsk");
        put(0xF8, "loc_cpu");  put(0xF9, "unl_cpu");  put(0xBD, "chg_ilm");
        put(0xBC, "ref_ilm");  put(0xF5, "unsupported_function");
    }
    static void put(int n, String s) { NAMES.put(n, s); }

    static final TreeMap<Integer, int[]> CALLS = new TreeMap<Integer, int[]>();

    public static void main(String[] args) throws Exception {
        File file = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 20000000L;

        Prefs prefs = Prefs.load();
        EmulationFramework framework = new EmulationFramework(prefs);
        framework.initialize(Constants.CHIP_FR, file);

        Platform platform   = framework.getPlatform(Constants.CHIP_FR);
        Emulator emulator   = framework.getEmulator(Constants.CHIP_FR);
        FrCPUState cpuState = (FrCPUState) platform.getCpuState();

        System.out.println("; loaded " + file.getName() + " reset PC=0x" + Integer.toHexString(cpuState.pc).toUpperCase());
        System.out.println("; hook: dispatch in 0x" + Integer.toHexString(DISPATCH_IN).toUpperCase()
                + "   out 0x" + Integer.toHexString(DISPATCH_OUT).toUpperCase());
        System.out.println("; --------------------------------------------------");

        // 给固件忙等所用的自由运行计数器持续递增（否则卡在 0x125A0C）
        final int[] tick = {0};
        Thread stub = new Thread(new Runnable() {
            public void run() {
                while (true) {
                    try { Thread.sleep(1); } catch (InterruptedException e) { return; }
                    tick[0] += 1000;
                    try { platform.getMemory().store32(0x5C003034, tick[0]); } catch (Throwable ignored) { }
                }
            }
        });
        stub.setDaemon(true);
        stub.start();

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);

        long i = 0, haltStreak = 0, total = 0;
        long t0 = System.currentTimeMillis();
        boolean inDisp = false, inRet = false;
        try {
            for (; i < steps; i++) {
                int pc = cpuState.pc;
                if (pc == DISPATCH_IN) {
                    // 多周期指令会连续几拍不推进 PC，必须去抖，否则一次调用被记 6 遍
                    if (!inDisp) { onEnter(platform, cpuState, i); total++; inDisp = true; }
                } else {
                    inDisp = false;
                    if (pc == DISPATCH_OUT) {
                        if (!inRet) { onExit(cpuState); inRet = true; }
                    } else {
                        inRet = false;
                        if (pc == HALT_PC) {
                            if (++haltStreak > 200) {
                                System.out.println("; 停在自转 0x" + Integer.toHexString(HALT_PC).toUpperCase() + "，结束");
                                break;
                            }
                        } else {
                            haltStreak = 0;
                        }
                    }
                }
                emulator.onClockTick();
                if (i % 2000000 == 0 && i > 0) {
                    System.out.printf("; step=%d  PC=0x%08X  syscalls=%d  %dms%n",
                            i, cpuState.pc, total, System.currentTimeMillis() - t0);
                }
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
        }

        System.out.println("; --------------------------------------------------");
        System.out.printf("; 共执行 %d 步，捕获系统调用 %d 次，用时 %dms%n",
                i, total, System.currentTimeMillis() - t0);
        System.out.println("; final PC=0x" + Integer.toHexString(cpuState.pc).toUpperCase());
        System.out.println(";");
        System.out.println("; === 运行时系统调用统计（按次数降序） ===");
        java.util.List<Map.Entry<Integer, int[]>> list =
                new java.util.ArrayList<Map.Entry<Integer, int[]>>(CALLS.entrySet());
        java.util.Collections.sort(list, new java.util.Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        int sum = 0;
        for (Map.Entry<Integer, int[]> e : list) sum += e.getValue()[0];
        System.out.printf("; %-6s %-8s %-7s %s%n", "服务号", "次数", "占比", "服务名");
        for (Map.Entry<Integer, int[]> e : list) {
            int n = e.getKey(), c = e.getValue()[0];
            System.out.printf("; 0x%02X    %-8d %6.2f%%  %s%n",
                    n, c, 100.0 * c / Math.max(1, sum), NAMES.containsKey(n) ? NAMES.get(n) : "(未命名)");
        }
        System.out.println("; 合计 " + sum);
        System.out.println(";");
        System.out.println("; === 各服务用到的对象号（r4）与调用点（返回地址） ===");
        for (Map.Entry<Integer, int[]> e : list) {
            int n = e.getKey();
            StringBuilder a = new StringBuilder(), k = new StringBuilder();
            for (Integer v : ARGS4.get(n))   a.append(String.format("0x%X ", v));
            for (Integer v : CALLERS.get(n)) k.append(String.format("0x%06X ", v));
            System.out.printf("; 0x%02X %-22s r4: %s%n;       调用点: %s%n",
                    n, NAMES.containsKey(n) ? NAMES.get(n) : "(未命名)", a, k);
        }
        System.exit(0);
    }

    static void onEnter(Platform p, FrCPUState s, long step) {
        int r12 = s.getReg(12);
        int n = r12 & 0xFF;                    // 服务号是 8 位有符号负数
        int[] c = CALLS.get(n);
        if (c == null) { c = new int[1]; CALLS.put(n, c); }
        c[0]++;
        int sp = s.getReg(15);
        int top = 0;
        try { top = p.getMemory().load32(sp); } catch (Throwable ignored) { }
        java.util.Set<Integer> s4 = ARGS4.get(n);
        if (s4 == null) { s4 = new java.util.TreeSet<Integer>(); ARGS4.put(n, s4); }
        s4.add(s.getReg(4));
        java.util.Set<Integer> cs = CALLERS.get(n);
        if (cs == null) { cs = new java.util.TreeSet<Integer>(); CALLERS.put(n, cs); }
        cs.add(top);
        System.out.printf("SVC %6d  n=0x%02X %-22s r4=0x%08X r5=0x%08X r6=0x%08X r7=0x%08X ret=0x%08X%n",
                step, n, NAMES.containsKey(n) ? NAMES.get(n) : "(未命名)",
                s.getReg(4), s.getReg(5), s.getReg(6), s.getReg(7), top);
    }

    static final TreeMap<Integer, java.util.Set<Integer>> ARGS4   = new TreeMap<Integer, java.util.Set<Integer>>();
    static final TreeMap<Integer, java.util.Set<Integer>> CALLERS = new TreeMap<Integer, java.util.Set<Integer>>();

    static void onExit(FrCPUState s) {
        int err = (s.getReg(12) & 0xFF);
        System.out.printf("     -> err=%d (0x%02X)%n", (byte) err, err);
    }
}
