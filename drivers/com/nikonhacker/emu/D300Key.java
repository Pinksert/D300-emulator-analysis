package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.memory.listener.fr.ExpeedProxyIoListener;
import com.nikonhacker.emu.memory.listener.fr.IoCensusListener;
import com.nikonhacker.emu.peripherials.programmableTimer.ProgrammableTimer;

import java.io.File;
import java.io.OutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 第 2 条路：找按键轮询地址 / 注入事件，把固件从待机推进菜单逻辑。
 *
 *   java com.nikonhacker.emu.D300Key <固件> <步数> <mode> [irq] [irqStep] [forceAddrHex] [forceValHex] [forceStep]
 *
 *   mode 0 = 纯普查（不改任何行为）
 *   mode 1 = 在 irqStep 步触发一次 irq 号中断
 *   mode 2 = 从 forceStep 步起，把 forceAddr 处读回的低字节改写为 forceVal
 *   mode 3 = 1 和 2 一起
 *
 * 确定性骨架与 D300Go/D300Exp 完全一致（忙等计数器按仿真步推进、定时器手动激活）。
 */
public class D300Key {

    static final int DISPATCH_IN = 0x00040668;
    static final int HALT_PC     = 0x000407DC;
    static final int COUNTER     = 0x5C003034;

    static final TreeMap<Integer, int[]> CALLS = new TreeMap<Integer, int[]>();
    static int phase = 0;

    // 4KB 块变化统计
    static final int PSIZE = 0x10000;
    static final int BLK   = 0x1000;
    static final HashMap<Integer, Integer> prevCs   = new HashMap<Integer, Integer>();
    static final HashMap<Integer, int[]>   changed  = new HashMap<Integer, int[]>();
    static final HashMap<Integer, int[]>   changedPost = new HashMap<Integer, int[]>();
    static int nBlockBefore = 0, nBlockPost = 0;

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 60000000L;
        int  mode  = args.length > 2 ? Integer.parseInt(args[2]) : 0;
        int  irq   = args.length > 3 ? Integer.parseInt(args[3]) : -1;
        long irqStep   = args.length > 4 ? Long.parseLong(args[4]) : -1;
        String fa = args.length > 5 ? args[5] : "-";
        int  fv   = args.length > 6 ? (int) Long.parseLong(args[6].replace("0x", ""), 16) : 0;
        long fs   = args.length > 7 ? Long.parseLong(args[7]) : -1;
        long period = args.length > 8 ? Long.parseLong(args[8]) : 0;   // >0 = 每 period 步重复点火
        int  nMax   = args.length > 9 ? Integer.parseInt(args[9]) : 1;

        ExpeedProxyIoListener.routeAll = true;

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
        DebuggableMemory mem = platform.getMemory();

        IoCensusListener probe = new IoCensusListener(platform, false);
        mem.addActivityListener(probe);

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);

        IoCensusListener.enabled = true;
        if (!"-".equals(fa)) {
            IoCensusListener.forceAddr = (int) Long.parseLong(fa.replace("0x", ""), 16);
            IoCensusListener.forceVal  = fv & 0xFF;
            IoCensusListener.forceFrom = fs;
        }

        long i = 0, haltStreak = 0;
        int counter = 0;
        boolean inDisp = false;
        long t0 = System.currentTimeMillis();
        long enterIdle = -1;
        int nCallAtInject = -1;
        boolean injected = false;
        int nFired = 0;
        try {
            for (; i < steps; i++) {
                IoCensusListener.forceStep = i;
                IoCensusListener.phase = phase;
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
                    if (mode == 4 && i == (irqStep < 0 ? 5000000L : irqStep)) {
                        dumpIrq(System.out, mem, platform);
                        System.exit(0);
                    }
                    if (!injected && (i == irqStep || (fs >= 0 && i == fs) || (irqStep < 0 && fs < 0 && i == 5000000L))) {
                        injected = true;
                        nCallAtInject = 0;
                        for (int[] v : CALLS.values()) nCallAtInject += v[0];
                        nBlockBefore = changed.size();
                        prevCs.clear();       // 重新取基线：之后的变化才算注入带来的
                        changed.clear();
                        IoCensusListener.injectPhase = 1;
                        if (irq >= 0 && (mode == 1 || mode == 3)) {
                            platform.getInterruptController().request(irq);
                            System.out.println("; 第 " + i + " 步触发中断 #" + irq);
                        }
                        if (IoCensusListener.forceAddr >= 0 && (mode == 2 || mode == 3)) {
                            System.out.println("; 第 " + i + " 步起把 0x"
                                    + Integer.toHexString(IoCensusListener.forceAddr)
                                    + " 读回值改写为 0x" + Integer.toHexString(IoCensusListener.forceVal));
                        }
                    }
                    if (period > 0 && irq >= 0 && nFired < nMax
                            && i >= irqStep && (i - irqStep) % period == 0) {
                        platform.getInterruptController().request(irq);
                        nFired++;
                    }
                    if (injected && i % 2000000 == 0) snapshot(mem, changedPost, prevCs);
                }

                emulator.onClockTick();
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
            t.printStackTrace(System.out);
        }
        snapshot(mem, injected ? changedPost : changed, prevCs);
        IoCensusListener.enabled = false;

        int nCall = 0;
        for (int[] v : CALLS.values()) nCall += v[0];

        System.out.printf("; 共 %d 步，用时 %dms，最终 PC=0x%08X，进入空闲=%d%n",
                i, System.currentTimeMillis() - t0, cpuState.pc, enterIdle);
        System.out.println("; 系统调用共 " + nCall + " 次，其中注入前 " + nCallAtInject + " 次");
        System.out.println("; 被改写的 4KB 块：注入前 " + nBlockBefore + " 个"
                + (injected ? "，注入后 " + changedPost.size() + " 个" : ""));
        if (injected) {
            System.out.println("; 界面文字区被读次数（注入前 -> 注入后）：");
            System.out.printf(";   0x1C0000-0x1DFFFF 短标签   %d -> %d%n",
                    IoCensusListener.wPre[0], IoCensusListener.wPost[0]);
            System.out.printf(";   0x3A0000-0x3AFFFF 菜单/帮助 %d -> %d%n",
                    IoCensusListener.wPre[1], IoCensusListener.wPost[1]);
            if (period > 0) System.out.println("; 重复点火 " + nFired + " 次");
        }
        if (IoCensusListener.forceAddr >= 0) {
            System.out.println("; 注入命中 " + IoCensusListener.forceHit + " 次");
        }
        System.out.println(";");
        System.out.println("; === 系统调用分布（服务号 -> 次数，次数降序）===");
        List<Map.Entry<Integer, int[]>> cl = new ArrayList<Map.Entry<Integer, int[]>>(CALLS.entrySet());
        Collections.sort(cl, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<Integer, int[]> e : cl) {
            int num = e.getKey().intValue();
            ps_call(System.out, num, e.getValue()[0]);
        }
        System.out.println(";");
        System.out.println("; === 按页统计（Top 25）===");
        IoCensusListener.dumpPages(System.out, 25);
        System.out.println(";");
        System.out.println("; === 片上 IO 区全程 Top 40 ===");
        IoCensusListener.dumpIo(System.out, 40, false);
        System.out.println(";");
        System.out.println("; === 片上 IO 区「进入空闲之后」Top 40 ===");
        IoCensusListener.dumpIo(System.out, 40, true);
        System.out.println(";");
        System.out.println("; === 访问片上 IO 的 PC 热点 Top 25 ===");
        IoCensusListener.dumpPc(System.out, 25);
        System.out.println(";");
        System.out.println("; === 注入后被改写最多的 4KB 块 Top 30 ===");
        dumpBlocks(System.out, injected ? changedPost : changed, 30);
        System.out.println(";");
        System.out.println("; === 中断控制器状态 ===");
        System.out.println(platform.getInterruptController().getStatus());
        System.out.println(";");
        System.out.println("; === 外设告警（前 2000 字符）===");
        System.out.println(warnBuf.length() == 0 ? "; （无）"
                : warnBuf.substring(0, Math.min(2000, warnBuf.length())));
        System.exit(0);
    }

    static void snapshot(DebuggableMemory mem, HashMap<Integer, int[]> tgt,
                         HashMap<Integer, Integer> prevCs) {
        int NPAGE = mem.getNumPages(), perPage = PSIZE / BLK;
        for (int p = 0; p < NPAGE; p++) {
            byte[] page = mem.getPageForAddress(p << 16);
            if (page == null || page.length < PSIZE) continue;
            for (int b = 0; b < perPage; b++) {
                int addr = (p << 16) | (b * BLK), o = b * BLK, cs = 0;
                for (int k = 0; k < BLK; k += 4) {
                    cs = cs * 31 + (page[o + k] & 0xFF) + ((page[o + k + 1] & 0xFF) << 8)
                            + ((page[o + k + 2] & 0xFF) << 16) + ((page[o + k + 3] & 0xFF) << 24);
                }
                Integer old = prevCs.get(addr);
                if (old != null && old.intValue() != cs) {
                    int[] c = tgt.get(addr);
                    if (c == null) { c = new int[1]; tgt.put(addr, c); }
                    c[0]++;
                }
                prevCs.put(addr, cs);
            }
        }
    }

    /** 服务号是 R12 的低 8 位（负数），打印成 -1..-176 */
    static void ps_call(PrintStream ps, int num, int cnt) {
        int signed = (num & 0x80) != 0 ? (num | 0xFFFFFF00) : num;
        ps.printf("; %5d (0x%02X)   %d%n", signed, num & 0xFF, cnt);
    }

    /** 直接读寄存器所在内存（accessSource=null 绕开监听器，不污染统计） */
    static void dumpIrq(PrintStream ps, DebuggableMemory mem, Platform platform) {
        ps.println("; === 中断寄存器现场 ===");
        ps.println("; " + platform.getInterruptController().getStatus());
        ps.printf("; EIRR0=0x%02X ENIR0=0x%02X ELVR0=0x%04X%n",
                mem.loadUnsigned8(0x40, null), mem.loadUnsigned8(0x41, null),
                mem.loadUnsigned16(0x42, null));
        ps.printf("; EIRR1=0x%02X ENIR1=0x%02X ELVR1=0x%04X%n",
                mem.loadUnsigned8(0xF0, null), mem.loadUnsigned8(0xF1, null),
                mem.loadUnsigned16(0xF2, null));
        ps.printf("; DICR(0x44)=0x%02X%n", mem.loadUnsigned8(0x44, null));
        StringBuilder en = new StringBuilder();
        StringBuilder icr = new StringBuilder();
        for (int i = 0; i < 48; i++) {
            int v = mem.loadUnsigned8(0x440 + i, null) & 0xFF;
            if (i < 16 && (mem.loadUnsigned8(0x41, null) & (1 << i)) != 0) {
                en.append(String.format(" IRQ%d(lvl%d)", i, v & 0x1F));
            }
            if (i >= 16 && (mem.loadUnsigned8(0xF1, null) & (1 << (i - 16))) != 0) {
                en.append(String.format(" IRQ%d(lvl%d)", i + 16, v & 0x1F));
            }
            icr.append(String.format("%d:%d ", i, v & 0x1F));
        }
        ps.println("; 已使能的中断（ENIR）:" + (en.length() == 0 ? " （无）" : en));
        ps.println("; ICR00-47 中断级别: " + icr);
    }

    static void dumpBlocks(PrintStream ps, HashMap<Integer, int[]> map, int topN) {
        List<Map.Entry<Integer, int[]>> l = new ArrayList<Map.Entry<Integer, int[]>>(map.entrySet());
        Collections.sort(l, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        ps.println("; 共 " + l.size() + " 个块被改写过");
        int n = Math.min(topN, l.size());
        for (int i = 0; i < n; i++) ps.printf("; 0x%08X   %d%n", l.get(i).getKey(), l.get(i).getValue()[0]);
    }
}
