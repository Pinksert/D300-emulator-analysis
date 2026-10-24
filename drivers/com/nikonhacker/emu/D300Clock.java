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
 * 给 B 段 <-> EXPEED 位串行总线装一个「EXPEED 时钟/状态应答模型」，
 * 看看伪造的外设应答能不能把 UI 状态机推过去。
 *
 * 协议（由 0x19EC9C / 0x19EBBC / 0x19E8D6 的反汇编确定）：
 *   命令字节 = 寄存器号 | 读标志(0x80)
 *     0x86 = 读 状态寄存器 0x06 -> 回 1 字节；bit7 决定初始化走长/短分支
 *     0xA6 = 读 时钟寄存器 0x26 -> 回 7 字节：年 月 日 时 分 秒（BCD）+ 1 个未用字节
 *     0xF6 = 读 寄存器 0x76     -> 回 2 字节
 *     0x06/0x26/0x36/0x46/0x76 = 写（后面跟数据，不回读）
 *   两张 256 字节转换表：
 *     0x1CFCA8 = 二进制 -> BCD（发送前编码）
 *     0x1CFDA8 = BCD -> 二进制（收到后解码，饱和 99）
 *
 * 注入点：0x0019EF6E = 接收函数出口，此时 R4/R11 = 收到的字节；直接改它们。
 * 判据：recv 之前最后一次发送的字节就是命令（数据字节后面不跟 recv）。
 *
 *   java com.nikonhacker.emu.D300Clock <固件> <步数> <mode> <输出>
 *   mode 0 = 基线（不注入，数据线浮空读回 0xFF）
 *        1 = 0x86 -> 0x00（bit7=0，走「短」初始化分支）+ 真时钟
 *        2 = 0x86 -> 0x80（bit7=1，走「长」初始化分支）+ 真时钟
 *        3 = 0x86 -> 0x88（bit7|bit3）+ 真时钟
 */
public class D300Clock {

    static final int HALT_PC    = 0x000407DC;
    static final int COUNTER    = 0x5C003034;
    static final int SEND       = 0x0019EEF8;
    static final int RECV_IN    = 0x0019EF3C;
    static final int RECV_OUT   = 0x0019EF6E;
    static final int OBJ_SUBMIT = 0x0012E6F8;   // 提交一个显示对象（0x9F58D508 + id*12）
    static final int DRAW_PRIM  = 0x00124F5A;   // 绘制原语

    static final int A_STATE    = 0x9F5A321C;   // EXPEED 状态字节
    static final int A_LAST     = 0x9F5A321D;   // 上一次状态
    static final int A_BUF1     = 0x9F5A320C;   // 心跳 6 字节 + u16
    static final int A_BUF2     = 0x9F5A3214;
    static final int T_TEXT     = 0x9F58D45D;   // 当前显示的 6 字节时间文本
    static final int OBJ_TABLE  = 0x9F58D508;   // 显示对象表，每条 12 字节
    static final int OBJ_ARRAY  = 0x9F5A0F60;   // 显示对象数组（9 槽 x 18B）
    static final int CMD_QUEUE  = 0x9F5A10F0;   // 命令队列

    // 2026-10-17 12:34:56，全部 BCD；第 7 字节固件不读
    static final int[] CLOCK_BCD = {0x26, 0x10, 0x17, 0x12, 0x34, 0x56, 0x00};
    // mode>=4：用互不相同的标记字节，把「7 个字节分别落到缓冲哪一位」钉死
    static final int[] MARK_BCD  = {0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17};

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 20000000L;
        int   mode = args.length > 2 ? Integer.parseInt(args[2]) : 1;
        String out = args.length > 3 ? args[3] : ("clock" + mode + ".out");

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

        long i = 0, haltStreak = 0;
        int counter = 0, prevPc = 0;
        int nSend = 0, nRecv = 0, nInject = 0;
        int lastSent = -1, rxIdx = 0;
        long firstAct = -1, lastAct = -1;
        int phase = 0;
        int prevState = -1, prevLast = -1;
        int chgState = 0, chgLast = 0, chgText = 0;
        int[] prevText = new int[6];
        for (int k = 0; k < 6; k++) prevText[k] = -1;
        final Map<Integer, int[]> objIds = new LinkedHashMap<Integer, int[]>();
        int nObj = 0, nDraw = 0;

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

                boolean entered = (pc != prevPc);

                if (entered && pc == SEND) {
                    nSend++;
                    lastSent = cpuState.getReg(4) & 0xFF;
                    rxIdx = 0;                       // 每个新字节都重置读指针（数据字节后不跟 recv）
                    if (firstAct < 0) firstAct = i;
                    lastAct = i;
                } else if (entered && pc == RECV_OUT) {
                    nRecv++;
                    if (firstAct < 0) firstAct = i;
                    lastAct = i;
                    if (mode > 0) {
                        int b = reply(lastSent, rxIdx, mode);
                        cpuState.setReg(4, b);
                        cpuState.setReg(11, b);
                        nInject++;
                    }
                    rxIdx++;
                } else if (entered && pc == OBJ_SUBMIT) {
                    nObj++;
                    Integer id = cpuState.getReg(4);
                    int[] c = objIds.get(id);
                    if (c == null) { c = new int[1]; objIds.put(id, c); }
                    c[0]++;
                } else if (entered && pc == DRAW_PRIM) {
                    nDraw++;
                }

                // 关键状态字节变化检测（每 1000 步采样一次，够密了）
                if ((i & 0x3FF) == 0) {
                    int s = mem.loadUnsigned8(A_STATE);
                    if (prevState >= 0 && s != prevState) chgState++;
                    prevState = s;
                    int l = mem.loadUnsigned8(A_LAST);
                    if (prevLast >= 0 && l != prevLast) chgLast++;
                    prevLast = l;
                    boolean diff = false;
                    for (int k = 0; k < 6; k++) {
                        int v = mem.loadUnsigned8(T_TEXT + k);
                        if (prevText[k] >= 0 && v != prevText[k]) diff = true;
                        prevText[k] = v;
                    }
                    if (diff) chgText++;
                }

                prevPc = pc;
                emulator.onClockTick();
            }
        } catch (Throwable t) {
            System.out.println("; stopped at step " + i + ": " + t);
        }

        w.println("mode=" + mode + "  steps=" + i + "  finalPC=" + Format.asHex(cpuState.pc, 8));
        w.println("send=" + nSend + "  recv=" + nRecv + "  injected=" + nInject);
        w.println("firstAct=" + firstAct + "  lastAct=" + lastAct);
        w.println("objSubmit=" + nObj + "  drawPrim=" + nDraw);
        w.println("chgState(0x9F5A321C)=" + chgState + "  chgLast(0x9F5A321D)=" + chgLast
                + "  chgText(0x9F58D45D,6B)=" + chgText);
        w.println("stateByte=0x" + Format.asHex(mem.loadUnsigned8(A_STATE), 2)
                + "  lastByte=0x" + Format.asHex(mem.loadUnsigned8(A_LAST), 2));
        w.println();
        w.println("--- 显示对象 id 分布（0x12E6F8 的 R4 参数）---");
        for (Map.Entry<Integer, int[]> e : objIds.entrySet()) {
            w.println("  id=" + Format.asHex(e.getKey(), 8) + " (" + e.getKey() + ")  x" + e.getValue()[0]);
        }
        w.println("共 " + objIds.size() + " 个不同 id");
        w.println();
        dump(w, mem, "0x9F5A3200 时钟/状态区", 0x9F5A3200, 64);
        dump(w, mem, "0x9F58D45D 时间文本", 0x9F58D450, 48);
        dump(w, mem, "0x9F58D508 显示对象表", OBJ_TABLE, 192);
        dump(w, mem, "0x9F5A0F60 显示对象数组", OBJ_ARRAY, 192);
        dump(w, mem, "0x9F5A10F0 命令队列", CMD_QUEUE, 64);
        w.flush(); w.close();

        System.out.println("; mode=" + mode + " steps=" + i + " PC=" + Format.asHex(cpuState.pc, 8));
        System.out.println("; send=" + nSend + " recv=" + nRecv + " injected=" + nInject);
        System.out.println("; objSubmit=" + nObj + " drawPrim=" + nDraw
                + " chgState=" + chgState + " chgLast=" + chgLast + " chgText=" + chgText);
        System.out.println("; -> " + out);
        System.exit(0);
    }

    static int reply(int cmd, int idx, int mode) {
        switch (cmd) {
            case 0x86:
                if (idx != 0) return 0x00;
                return (mode == 1) ? 0x00 : (mode == 2 ? 0x80 : 0x88);
            case 0xA6: {
                int[] src = (mode >= 4) ? MARK_BCD : CLOCK_BCD;
                return (idx < src.length) ? src[idx] : 0x00;
            }
            case 0xF6:
                return (idx == 0) ? 0x00 : 0x00;
            default:
                return 0x00;
        }
    }

    static void dump(PrintWriter w, DebuggableMemory mem, String title, int addr, int len) {
        w.println("--- " + title + " ---");
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < len; k++) {
            if (k % 16 == 0) {
                if (k > 0) w.println(sb.toString());
                sb = new StringBuilder();
                sb.append(Format.asHex(addr + k, 8)).append("  ");
            }
            sb.append(Format.asHex(mem.loadUnsigned8(addr + k), 2)).append(' ');
        }
        w.println(sb.toString());
        w.println();
    }
}
