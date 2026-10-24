package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.memory.listener.fr.ExpeedProxyIoListener;
import com.nikonhacker.emu.peripherials.interruptController.InterruptController;
import com.nikonhacker.emu.peripherials.programmableTimer.ProgrammableTimer;

import java.io.File;
import java.io.OutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 攻卡点：固件在 0x19EF40 有一段"等 EXPEED 就绪"的 8 次重试循环：
 *     ldi:32 #0x70000000, r10
 *     r0 = r10 + 0xA            ; 0x7000000A
 *     bandl #0xD, @r0           ; 清掩码 0xD 指定的位（bit0/bit2/bit3）
 *     call 延时
 *     borl  #0x2, @r0           ; 置 bit1
 *     btstl #0x4, @r0           ; 测掩码 0x4（bit2）
 *     beq   -> 没就绪就继续重试，共 8 次
 * 仿真器里 0x7000000A 读回来恒为 0，所以 8 次全失败。
 *
 * 本驱动补上这一位：每条指令之后把 0x7000000A 的 bit2 置 1，
 * 模拟"硬件过一会儿就就绪"。对比打桩前后的差别。
 *
 *   java com.nikonhacker.emu.D300Go <固件> <步数> <mode 0=基线 1=打桩>
 */
public class D300Go {

    static final int DISPATCH_IN = 0x00040668;
    static final int HALT_PC     = 0x000407DC;
    static final int COUNTER     = 0x5C003034;
    static final int EXPEED_RDY  = 0x7000000A;   // 就绪位所在寄存器（字节）

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

    static final TreeMap<Integer, int[]> CALLS = new TreeMap<Integer, int[]>();
    static final TreeMap<Integer, int[]> PCHIST = new TreeMap<Integer, int[]>();
    static final HashMap<Integer, int[]> CHANGED = new HashMap<Integer, int[]>();
    static final HashMap<Integer, Integer> PREVCS = new HashMap<Integer, Integer>();

    static int phase = 0;

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 60000000L;
        int  mode  = args.length > 2 ? Integer.parseInt(args[2]) : 1;
        // mode 2/3 = 启用修正版外设路由守卫（EXPEED 各外设模型真正生效）
        ExpeedProxyIoListener.routeAll = (mode >= 2);
        boolean stub = ((mode & 1) == 1);

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

        long i = 0, haltStreak = 0, rdySet = 0;
        int counter = 0;
        boolean inDisp = false;
        long t0 = System.currentTimeMillis();
        try {
            for (; i < steps; i++) {
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
                    if (pc != HALT_PC) {
                        int[] c = PCHIST.get(pc);
                        if (c == null) { c = new int[1]; PCHIST.put(pc, c); }
                        c[0]++;
                    }
                }

                emulator.onClockTick();

                // 打桩：让 EXPEED 的"就绪位"在第 2 拍之后就为 1
                if (stub && phase == 1) {
                    try {
                        int v = mem.loadUnsigned8(EXPEED_RDY);
                        mem.store8(EXPEED_RDY, v | 0x04);
                        rdySet++;
                    } catch (Throwable ignored) { }
                }

                if (i > 0 && i % 2000000 == 0) snapshot(mem);
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
        }
        snapshot(mem);

        System.out.printf("; 共 %d 步，mode=%d，打桩 %d 次，用时 %dms，最终 PC=0x%08X%n",
                i, mode, rdySet, System.currentTimeMillis() - t0, cpuState.pc);
        System.out.println(";");
        System.out.println("; === 系统调用 ===");
        dump(CALLS);
        System.out.println("; 合计 " + sum(CALLS));
        System.out.println(";");
        System.out.println("; === PC 热点 Top 25 ===");
        List<Map.Entry<Integer, int[]>> ph = new ArrayList<Map.Entry<Integer, int[]>>(PCHIST.entrySet());
        Collections.sort(ph, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        int k = 0;
        for (Map.Entry<Integer, int[]> e : ph) {
            if (k++ >= 25) break;
            System.out.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
        }
        System.out.println("; 不同 PC 共 " + PCHIST.size() + " 个");
        System.out.println(";");
        System.out.println("; === 被改写过的 4KB 块（Top 20）===");
        List<Map.Entry<Integer, int[]>> cl = new ArrayList<Map.Entry<Integer, int[]>>(CHANGED.entrySet());
        Collections.sort(cl, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        k = 0;
        for (Map.Entry<Integer, int[]> e : cl) {
            if (k++ >= 20) break;
            System.out.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
        }
        System.out.println("; 共 " + cl.size() + " 个");
        System.exit(0);
    }

    static void snapshot(DebuggableMemory mem) {
        int NPAGE = mem.getNumPages(), PSIZE = mem.getPageSize(), BLK = 4096;
        int per = PSIZE / BLK;
        for (int p = 0; p < NPAGE; p++) {
            byte[] page = mem.getPageForAddress(p << 16);
            if (page == null || page.length < PSIZE) continue;
            for (int b = 0; b < per; b++) {
                int addr = (p << 16) | (b * BLK), o = b * BLK, cs = 0;
                for (int x = 0; x < BLK; x += 4) {
                    cs = cs * 31 + (page[o+x] & 0xFF) + ((page[o+x+1] & 0xFF) << 8)
                       + ((page[o+x+2] & 0xFF) << 16) + ((page[o+x+3] & 0xFF) << 24);
                }
                Integer old = PREVCS.get(addr);
                if (old != null && old.intValue() != cs) {
                    int[] c = CHANGED.get(addr);
                    if (c == null) { c = new int[1]; CHANGED.put(addr, c); }
                    c[0]++;
                }
                PREVCS.put(addr, cs);
            }
        }
    }

    static int sum(TreeMap<Integer, int[]> m) {
        int s = 0;
        for (int[] v : m.values()) s += v[0];
        return s;
    }

    static void dump(TreeMap<Integer, int[]> m) {
        List<Map.Entry<Integer, int[]>> l = new ArrayList<Map.Entry<Integer, int[]>>(m.entrySet());
        Collections.sort(l, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<Integer, int[]> e : l) {
            int n = e.getKey();
            System.out.printf("; 0x%02X  %-22s %d%n",
                    n, NAMES.containsKey(n) ? NAMES.get(n) : "(未命名)", e.getValue()[0]);
        }
    }
}
