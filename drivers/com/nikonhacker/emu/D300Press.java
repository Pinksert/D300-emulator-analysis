package com.nikonhacker.emu;

import com.nikonhacker.Constants;
import com.nikonhacker.Format;
import com.nikonhacker.Prefs;
import com.nikonhacker.disassembly.fr.FrCPUState;
import com.nikonhacker.emu.memory.DebuggableMemory;
import com.nikonhacker.emu.memory.listener.fr.ExpeedProxyIoListener;
import com.nikonhacker.emu.memory.listener.fr.IoCensusListener;
import com.nikonhacker.emu.peripherials.interruptController.fr.FrInterruptController;
import com.nikonhacker.emu.peripherials.ioPort.IoPort;
import com.nikonhacker.emu.peripherials.ioPort.Pin;
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
 * 第 2 条路的收尾：按真机方式模拟一次「按键」。
 *
 *   java com.nikonhacker.emu.D300Press <固件> <步数> <引脚位掩码> [按下步] [按下持续步数] [次数]
 *
 * 真机的按键链路（本轮反汇编坐实）：
 *   外部中断通道 3（请求号 0x13）→ ISR 0x042A2C → 0x17584C
 *       关掉通道 0/1/2，然后 set_flg(12, 0x80000000) 通知「有按键」
 *   随后某任务跑 0x175876：读引脚 0x50000107 消抖、按电平重设触发边沿、重新开通道 3
 *
 * 所以要完整地模拟一次按键，必须三件事一起做（少了任何一件都只会造成
 * 「中断撤不掉→ISR 无限重入」这种假象）：
 *   1) 把引脚端口 0x50000107 的相应位拉高
 *   2) 把外部中断通道 3 的输入电平 externalChannelValue[0][3] 置 1
 *   3) 触发请求号 0x13；松开时再把 1)、2) 归零
 */
public class D300Press {

    static final int DISPATCH_IN = 0x00040668;
    static final int HALT_PC     = 0x000407DC;
    static final int COUNTER     = 0x5C003034;
    static final int PIN_PORT    = 0x50000107;
    static final int IRQ_KEY     = 0x13;
    static final int ISR_ENTRY   = 0x00042A2C;   // 中断 #0x13 的向量入口
    static final int EIRR0       = 0x00000040;
    static final int ENIR0       = 0x00000041;

    static final TreeMap<Integer, int[]> CALLS  = new TreeMap<Integer, int[]>();
    static final HashMap<Integer, int[]> pcHist = new HashMap<Integer, int[]>();
    static final HashMap<Integer, Integer> prevCs = new HashMap<Integer, Integer>();
    static final HashMap<Integer, int[]> changedPost = new HashMap<Integer, int[]>();
    static final int PSIZE = 0x10000, BLK = 0x1000;

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 20000000L;
        int  mask  = args.length > 2 ? (int) Long.parseLong(args[2].replace("0x", ""), 16) : 0x08;
        long pressStep = args.length > 3 ? Long.parseLong(args[3]) : 5000000L;
        long hold  = args.length > 4 ? Long.parseLong(args[4]) : 200000L;
        int  nPres = args.length > 5 ? Integer.parseInt(args[5]) : 1;

        ExpeedProxyIoListener.routeAll = true;

        final StringBuilder warnBuf = new StringBuilder();
        System.setErr(new PrintStream(new OutputStream() {
            public void write(int b) { if (warnBuf.length() < 8000) warnBuf.append((char) b); }
        }));

        Prefs prefs = Prefs.load();
        EmulationFramework framework = new EmulationFramework(prefs);
        framework.initialize(Constants.CHIP_FR, file);
        Platform platform = framework.getPlatform(Constants.CHIP_FR);
        Emulator emulator = framework.getEmulator(Constants.CHIP_FR);
        FrCPUState cpuState = (FrCPUState) platform.getCpuState();
        DebuggableMemory mem = platform.getMemory();
        FrInterruptController ic = (FrInterruptController) platform.getInterruptController();

        mem.addActivityListener(new IoCensusListener(platform, false));

        // 引脚必须从「端口模型」注入：ExpeedPinIoListener.onLoadData8 直接返回
        // IoPort.getValue()，而 getValue() 由每个 Pin 的 input/output 值合成，
        // 写内存（即使绕过监听器）固件也看不到。
        IoPort port7 = platform.getIoPorts()[PIN_PORT & 0xFF];
        Pin[] pins = new Pin[8];
        StringBuilder pinDesc = new StringBuilder();
        for (int b = 0; b < 8; b++) {
            pins[b] = port7.getPin(b);
            if (b > 0) pinDesc.append(' ');
            pinDesc.append("P").append(b).append(pins[b].isInput() ? "=IN" : "=OUT")
                   .append(pins[b].isInputEnabled() ? "" : "(禁)");
            pins[b].setInputValue(0);
        }
        int bit = 0, mm = mask;
        while (mm > 1) { mm >>= 1; bit++; }
        System.out.println("; 端口 P" + (PIN_PORT & 0xFF) + " 引脚方向: " + pinDesc);
        System.out.println("; 注入引脚 bit" + bit + "（掩码 0x" + Integer.toHexString(mask) + "）"
                + "  端口初始读回=0x" + Integer.toHexString(port7.getValue() & 0xFF));

        framework.prepareBreakTriggers(Constants.CHIP_FR, EmulationFramework.ExecutionMode.RUN, null);
        framework.prepareEmulation(Constants.CHIP_FR);
        IoCensusListener.enabled = true;
        IoCensusListener.recordWatch = true;

        long i = 0, haltStreak = 0;
        int counter = 0, phase = 0, nPress = 0;
        boolean inDisp = false, pressed = false, started = false;
        boolean fired = false;                       // 请求已发出、等待 CPU 接受
        int nIsr = 0, nEirrW = 0, nEnirW = 0, lastEirr = -1, lastEnir = -1;
        long t0 = System.currentTimeMillis(), enterIdle = -1;
        int nCallPre = 0, uiPre0 = 0, uiPre1 = 0;
        try {
            for (; i < steps; i++) {
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

                int[] h = pcHist.get(pc);
                if (h == null) { h = new int[1]; pcHist.put(pc, h); }
                h[0]++;

                if (i % 64 == 0) {
                    counter += 64;
                    try { mem.store32(COUNTER, counter); } catch (Throwable ignored) { }
                }

                // 请求被 CPU 接受 → 立刻撤销（模拟边沿触发；模拟器默认是电平触发且从不撤销）
                if (fired && pc == ISR_ENTRY) {
                    platform.getInterruptController().removeRequest(IRQ_KEY);
                    fired = false; nIsr++;
                }
                if (i % 256 == 0) {
                    int e, n;
                    try { e = mem.loadUnsigned8(EIRR0, null); } catch (Throwable t) { e = -1; }
                    try { n = mem.loadUnsigned8(ENIR0, null); } catch (Throwable t) { n = -1; }
                    if (lastEirr >= 0 && e != lastEirr) nEirrW++;
                    if (lastEnir >= 0 && n != lastEnir) nEnirW++;
                    lastEirr = e; lastEnir = n;
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
                    if (!started && i >= pressStep) {
                        started = true;
                        nCallPre = 0;
                        for (int[] v : CALLS.values()) nCallPre += v[0];
                        uiPre0 = IoCensusListener.wPre[0];
                        uiPre1 = IoCensusListener.wPre[1];
                        IoCensusListener.injectPhase = 1;
                        prevCs.clear();
                        snapshot(mem, changedPost, prevCs);
                    }
                    if (started && nPress < nPres) {
                        long rel = (i - pressStep) % (hold * 2);
                        if (!pressed && rel == 0) {
                            // 按下：拉引脚 + 拉外部中断输入电平 + 触发请求
                            pins[bit].setInputValue(1);          // 真正被固件看到的按键电平
                            mem.store8(PIN_PORT, (byte) mask, null);
                            ic.externalChannelValue[0][3] = 1;
                            platform.getInterruptController().request(IRQ_KEY);
                            fired = true;
                            pressed = true; nPress++;
                        } else if (pressed && rel == hold) {
                            // 松开
                            pins[bit].setInputValue(0);
                            mem.store8(PIN_PORT, (byte) 0, null);
                            ic.externalChannelValue[0][3] = 0;
                            pressed = false;
                        }
                    }
                    if (started && i % 1000000 == 0) snapshot(mem, changedPost, prevCs);
                }

                emulator.onClockTick();
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
            t.printStackTrace(System.out);
        }
        snapshot(mem, changedPost, prevCs);
        IoCensusListener.enabled = false;

        // dump 显示相关 RAM，离线分析
        dumpRange(mem, 0x9F5A0000, 0x00008000, "ram_9F5A.bin");
        dumpRange(mem, 0x9F590000, 0x00010000, "ram_9F59.bin");
        dumpRange(mem, 0x9F580000, 0x00010000, "ram_9F58.bin");

        int nCall = 0;
        for (int[] v : CALLS.values()) nCall += v[0];
        System.out.printf("; 引脚掩码=0x%02X  按下 %d 次  共 %d 步  用时 %dms  最终 PC=0x%08X%n",
                mask, nPress, i, System.currentTimeMillis() - t0, cpuState.pc);
        System.out.println("; 系统调用 " + nCall + " 次（按下前 " + nCallPre + " 次，按下后 "
                + (nCall - nCallPre) + " 次）");
        System.out.printf("; 界面文字读次数  短标签 %d -> %d   菜单/帮助 %d -> %d%n",
                uiPre0, IoCensusListener.wPost[0], uiPre1, IoCensusListener.wPost[1]);
        System.out.println("; 按下后被改写的 4KB 块 " + changedPost.size() + " 个");
        System.out.println("; 中断被接受 " + nIsr + " 次    EIRR0 变化 " + nEirrW
                + " 次    ENIR0 变化 " + nEnirW + " 次");
        System.out.println(";");
        System.out.println("; === 按键后 PC 热点 Top 30 ===");
        dumpMap(System.out, pcHist, 30, true);
        System.out.println(";");
        System.out.println("; === 系统调用分布 ===");
        dumpCall(System.out);
        System.out.println(";");
        System.out.println("; === 按下后被改写最多的 4KB 块 Top 25 ===");
        dumpMap(System.out, changedPost, 25, false);
        System.out.println(";");
        System.out.println("; === 按键后被读取的界面文字地址 ===");
        for (Map.Entry<Integer, int[]> e : IoCensusListener.watchAddr.entrySet()) {
            System.out.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
        }
        System.out.println("; 共 " + IoCensusListener.watchAddr.size() + " 个地址");
        System.out.println("; === 读界面文字的 PC 热点 ===");
        dumpMap(System.out, IoCensusListener.watchPc, 15, true);
        System.out.println(";");
        System.out.println("; === 中断现场 ===");
        System.out.println("; " + platform.getInterruptController().getStatus());
        System.out.printf("; EIRR0=0x%02X ENIR0=0x%02X ELVR0=0x%04X%n",
                mem.loadUnsigned8(0x40, null), mem.loadUnsigned8(0x41, null),
                mem.loadUnsigned16(0x42, null));
        System.exit(0);
    }

    static void dumpRange(DebuggableMemory mem, int start, int len, String fn) {
        try {
            java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
            for (int a = start; a < start + len; a++) {
                int v;
                try { v = mem.loadUnsigned8(a, null); } catch (Throwable t) { v = 0; }
                bo.write(v);
            }
            java.io.FileOutputStream fo = new java.io.FileOutputStream(fn);
            bo.writeTo(fo);
            fo.close();
            System.out.println("; 已写出 " + fn + "  " + len + " 字节");
        } catch (Throwable t) {
            System.out.println("; dump " + fn + " 失败: " + t);
        }
    }

    static void snapshot(DebuggableMemory mem, HashMap<Integer, int[]> tgt,
                         HashMap<Integer, Integer> prev) {
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
                Integer old = prev.get(addr);
                if (old != null && old.intValue() != cs) {
                    int[] c = tgt.get(addr);
                    if (c == null) { c = new int[1]; tgt.put(addr, c); }
                    c[0]++;
                }
                prev.put(addr, cs);
            }
        }
    }

    static void dumpMap(PrintStream ps, HashMap<Integer, int[]> map, int topN, boolean hexPc) {
        List<Map.Entry<Integer, int[]>> l = new ArrayList<Map.Entry<Integer, int[]>>(map.entrySet());
        Collections.sort(l, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        int n = Math.min(topN, l.size());
        for (int i = 0; i < n; i++) {
            Map.Entry<Integer, int[]> e = l.get(i);
            if (hexPc) ps.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
            else ps.printf("; 0x%08X   %d%n", e.getKey(), e.getValue()[0]);
        }
        ps.println("; 共 " + l.size() + " 项");
    }

    static void dumpCall(PrintStream ps) {
        List<Map.Entry<Integer, int[]>> l = new ArrayList<Map.Entry<Integer, int[]>>(CALLS.entrySet());
        Collections.sort(l, new Comparator<Map.Entry<Integer, int[]>>() {
            public int compare(Map.Entry<Integer, int[]> a, Map.Entry<Integer, int[]> b) {
                return b.getValue()[0] - a.getValue()[0];
            }
        });
        for (Map.Entry<Integer, int[]> e : l) {
            int num = e.getKey().intValue();
            int signed = (num & 0x80) != 0 ? (num | 0xFFFFFF00) : num;
            ps.printf("; %5d (0x%02X)   %d%n", signed, num & 0xFF, e.getValue()[0]);
        }
    }
}
