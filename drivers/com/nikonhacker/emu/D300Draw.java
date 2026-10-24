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
 * 第十轮：驱动 D300 的显示管线。
 *
 * 已知（静态解出）：
 *   flag 对象表 0x551B0：17 条，元素 16B；表项 +0 = 控制块指针(0x680008B8+(id-1)*0x10)，+8 = 位模式
 *   flag 3 的控制块 = 0x680008D8，位模式 = 0x680008E0
 *   显示管线任务 = 任务 4，入口 0x12DFAE
 *     0x12DFAE 循环 → 0x12E006(重置) → 0x12E0F8(取对象) → 0x12E162(等 flag 3 bit4)
 *                   → 0x12E1EC(选对象/填描述符) → 0x12E326 → 0x12E412 → 0x12E674(★绘制)
 *   显示对象描述符表 = 0x1BC020 + id*16；+0 数据指针 +4 长度 +8 类型 +C 绘制函数
 *   0x9F58D500 = 待绘制链表头（12B 节点，id 在 +8，0x80 = 脏）
 *
 * 实验：mode 0 = 不干预基线；mode 1..n = 周期性往 flag 位模式里或位，看管线能否推进。
 *
 *   java com.nikonhacker.emu.D300Draw <固件> <步数> <mode> <输出>
 */
public class D300Draw {

    static final int HALT_PC = 0x000407DC;
    static final int COUNTER = 0x5C003034;

    /** flag 控制块基址：id N -> 0x680008B8 + (N-1)*0x10 ；位模式在 +8 */
    static final int FLG_BASE = 0x680008B8;
    static int flgBits(int id) { return FLG_BASE + (id - 1) * 0x10 + 8; }

    /** 显示对象描述符表 */
    static final int DESC_TABLE = 0x001BC020;

    // 监控点：地址 -> 名字
    static final int[] WATCH = {
        0x0012DFAE, // 显示管线任务 4 循环体
        0x0012E006, // 重置/清空
        0x0012E0F8, // 取下一个待绘制对象
        0x0012E162, // 等 flag 3 bit4
        0x0012E1EC, // 选对象，填 0x9F58DA82 / 0x9F58D858
        0x0012E326, // 取数据
        0x0012E412, // 拷数据到缓冲
        0x0012E674, // ★ 消费者：绘制
        0x0012E602, // 清空全部节点
        0x0012E636, // 从链表头取 id
        0x0012E6F8, // 插入链表 + set_flg(3,0x08)
        0x0012E48E, // 错误/等待处理
        0x00198062, // 任务 19
        0x001984F0,
        0x0012E7D6, 0x0012E806, 0x0012E856, 0x0012E8E2, 0x0012EA12, // 绘制函数
        0x0012EC8A, 0x0012EC98, 0x0012ED06, 0x0012EE6A, 0x0012EEF2,
        0x0012EF88, 0x0012EF92, 0x0012F06E, 0x0012F0EA, 0x0012F0F4,
        0x0012F0F6, 0x0012F1AC, 0x0012F212, 0x0012F260, 0x0012F276,
        0x0012F286, 0x0012F296, 0x0012F2A6, 0x0012F2A8, 0x0012F40A,
        0x0012F448, 0x0012F452, 0x0012F460, 0x0012F4BA, 0x0012F4C8,
        0x0012F528, 0x0012F53C, 0x0012ECE2, 0x0012F15A, 0x0012F5AC,
        0x0012F5AE, 0x0012F648, 0x0012F668, 0x0012F740, 0x0012F6BA,
        0x0012F76C, 0x0012E754,
    };
    static final String[] WATCH_NAME = {
        "0x12DFAE 显示管线循环", "0x12E006 重置", "0x12E0F8 取对象", "0x12E162 等flag3",
        "0x12E1EC 选对象", "0x12E326 取数据", "0x12E412 拷数据", "0x12E674 ★绘制",
        "0x12E602 清空节点", "0x12E636 链表取id", "0x12E6F8 插入链表", "0x12E48E 等待/错误",
        "0x198062 任务19", "0x1984F0",
        "0x12E7D6", "0x12E806", "0x12E856", "0x12E8E2", "0x12EA12",
        "0x12EC8A", "0x12EC98", "0x12ED06", "0x12EE6A", "0x12EEF2",
        "0x12EF88", "0x12EF92", "0x12F06E", "0x12F0EA", "0x12F0F4",
        "0x12F0F6", "0x12F1AC", "0x12F212", "0x12F260", "0x12F276",
        "0x12F286", "0x12F296", "0x12F2A6", "0x12F2A8", "0x12F40A",
        "0x12F448", "0x12F452", "0x12F460", "0x12F4BA", "0x12F4C8",
        "0x12F528", "0x12F53C", "0x12ECE2", "0x12F15A", "0x12F5AC",
        "0x12F5AE", "0x12F648", "0x12F668", "0x12F740", "0x12F6BA",
        "0x12F76C", "0x12E754",
    };

    // 关键内存：地址 -> 名字
    static final int[] MEM32 = {
        0x9F58D500, // 链表头 next
        0x9F58D504, // 链表头 prev
        0x9F58D87B, // 0/非0 决定走"直接指定"还是"取链表"
        0x9F58DA82, // 当前显示对象 id
        0x9F58D858, // 描述符指针
        0x9F58D854,
        0x9F58D850,
        0x9F5A0F60, // 显示对象数组
        0x9F5A10F0, // 命令队列
        0x9F5A0CC0, // 图层表
    };
    static final String[] MEM32_NAME = {
        "链表头.next", "链表头.prev", "0x9F58D87B 模式开关", "当前对象 id",
        "描述符指针", "0x9F58D854", "0x9F58D850",
        "显示对象数组[0]", "命令队列[0]", "图层表[0]",
    };

    public static void main(String[] args) throws Exception {
        File file  = new File(args.length > 0 ? args[0] : "B3000111.bin");
        long steps = args.length > 1 ? Long.parseLong(args[1]) : 20000000L;
        int mode   = args.length > 2 ? Integer.parseInt(args[2]) : 0;
        String out = args.length > 3 ? args[3] : "draw0.out";

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
        w.println("mode=" + mode + "  steps=" + steps);

        final Map<Integer, int[]> hits = new LinkedHashMap<Integer, int[]>();
        for (int a : WATCH) hits.put(a, new int[1]);

        // 记录被监控内存的所有不同取值
        final Map<Integer, StringBuilder> hist = new LinkedHashMap<Integer, StringBuilder>();
        for (int a : MEM32) hist.put(a, new StringBuilder());

        int prevPc = 0;
        long i = 0;
        int haltStreak = 0, counter = 0, phase = 0;
        int lastFlg3 = -1;
        StringBuilder events = new StringBuilder();
        StringBuilder svcLog = new StringBuilder();

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
                            events.append("# step ").append(i).append(" 启动定时器\n");
                        }
                    } else haltStreak = 0;
                } else {
                    for (ProgrammableTimer t : platform.getProgrammableTimers()) {
                        if (t != null) { try { t.onClockTick(); } catch (Throwable ignored) { } }
                    }
                }

                boolean entered = (pc != prevPc);
                if (entered) {
                    int[] h = hits.get(pc);
                    if (h != null) {
                        h[0]++;
                        // 绘制函数（WATCH 索引 >= 14）以及管线后三段：记录一次现场
                        int idx = -1;
                        for (int k = 0; k < WATCH.length; k++) if (WATCH[k] == pc) { idx = k; break; }
                        if (idx >= 14 || pc == 0x0012E674 || pc == 0x0012E412 || pc == 0x0012E326) {
                            int idv, desc, buf0, buf1;
                            try { idv = mem.loadUnsigned8(0x9F58DA82); } catch (Throwable t) { idv = -1; }
                            try { desc = mem.load32(0x9F58D858); } catch (Throwable t) { desc = -1; }
                            try { buf0 = mem.load32(0x9F58D850); } catch (Throwable t) { buf0 = -1; }
                            try { buf1 = mem.load32(0x9F58D854); } catch (Throwable t) { buf1 = -1; }
                            events.append("# step ").append(i).append(" 进入 ").append(WATCH_NAME[idx])
                                  .append("  当前id=0x").append(Format.asHex(idv, 2))
                                  .append(" 描述符=0x").append(Format.asHex(desc, 8))
                                  .append(" src=0x").append(Format.asHex(buf0, 8))
                                  .append(" dst=0x").append(Format.asHex(buf1, 8)).append("\n");
                            if (buf1 > 0) {
                                StringBuilder hx = new StringBuilder(), as = new StringBuilder();
                                for (int k = 0; k < 32; k++) {
                                    int b;
                                    try { b = mem.loadUnsigned8(buf1 + k); } catch (Throwable t) { b = 0; }
                                    hx.append(String.format("%02X", b));
                                    as.append((b >= 32 && b < 127) ? (char) b : '.');
                                }
                                events.append("        dst缓冲: ").append(hx).append("  |").append(as).append("|\n");
                            }
                        }
                    }
                }

                // ---- 注入：周期性给 flag 置位 ----
                // 注入周期：mode>=7 为 1 步（每步），否则 20000 步
                long period = (mode >= 7) ? 1 : 20000;
                if (phase == 1 && mode != 0 && (i % period) == 0) {
                    int addr = flgBits(3);
                    int cur;
                    try { cur = mem.load32(addr); } catch (Throwable t) { cur = 0; }
                    int nv = cur;
                    switch (mode) {
                        case 1: nv = cur | 0xFFFFFFFF; break;   // 全部置位
                        case 2: nv = cur | 0x00000010; break;   // bit4（0x12E162 要的）
                        case 3: nv = cur | 0x00000002; break;   // bit1（0x12E48E 等的是 waiptn 2）
                        case 4: nv = cur | 0x00000008; break;   // bit3（插入链表时置的）
                        case 5: nv = cur | 0x00000012; break;   // bit4 + bit1
                        case 6: nv = cur | 0x00000001; break;   // bit0
                        // 高阶模式：每步注入，精确维持 (flag3 & 0x74) == 0x10
                        case 7: nv = (cur & ~0x00000064) | 0x00000010; break; // 只留 bit4，清 bit2/5/6，保留其余
                        case 8: nv = (cur & ~0x00000074) | 0x00000010; break; // 更狠：清 bit2/4/5/6 再置 bit4
                        case 9: nv = cur | 0x00000010; break;                 // 每步只置 bit4
                        case 10: nv = (cur & ~0x00000064) | 0x00000012; break;// bit4+bit1，清 bit2/5/6
                        default: break;
                    }
                    if (nv != cur) {
                        try { mem.store32(addr, nv); } catch (Throwable ignored) { }
                    }
                }

                // ---- 记录 flag 3 位模式变化 ----
                if ((i % 5000) == 0) {
                    int v;
                    try { v = mem.load32(flgBits(3)); } catch (Throwable t) { v = -1; }
                    if (v != lastFlg3) {
                        events.append("# step ").append(i).append(" flag3=0x")
                              .append(Format.asHex(v, 8)).append("\n");
                        lastFlg3 = v;
                    }
                }

                // ---- 记录被监控内存 ----
                if ((i % 20000) == 0) {
                    for (Map.Entry<Integer, StringBuilder> e : hist.entrySet()) {
                        int v;
                        try { v = mem.load32(e.getKey()); } catch (Throwable t) { v = -1; }
                        e.getValue().append(i).append(":").append(Format.asHex(v, 8)).append(" ");
                    }
                }

                // ---- 记录所有 event flag 系统调用（谁写过/读过 flag 3）----
                if (entered && pc == 0x00040668) {
                    int svc = cpuState.getReg(12) & 0xFF;
                    if (svc >= 128) svc -= 256;
                    if (svc == -48 || svc == -47 || svc == -46 || svc == -170) {
                        int id = cpuState.getReg(5);      // 内核 wai_flg/set_flg 用 R5 作 id
                        int ptn = cpuState.getReg(6);
                        int mode_ = cpuState.getReg(7);
                        if (svc == -48 || svc == -47) { ptn = cpuState.getReg(5); id = cpuState.getReg(4); }
                        if (id == 3) {
                            String nm = (svc == -48) ? "set_flg" : (svc == -47) ? "clr_flg" : (svc == -46) ? "wai_flg" : "twai_flg";
                            svcLog.append("# step ").append(i).append(" ").append(nm)
                                  .append("(3, 0x").append(Format.asHex(ptn, 8))
                                  .append(") caller=").append(Format.asHex(prevPc, 8))
                                  .append(" wfmode=").append(Format.asHex(mode_, 2)).append("\n");
                        }
                    }
                }

                prevPc = pc;
                emulator.onClockTick();
            }
        } catch (Throwable t) {
            w.println("; stopped at step " + i + ": " + t);
        }

        // ---------- 输出 ----------
        w.println("steps=" + i + "  finalPC=" + Format.asHex(cpuState.pc, 8));
        w.println();
        w.println("--- 关键函数命中次数 ---");
        for (int k = 0; k < WATCH.length; k++) {
            int c = hits.get(WATCH[k])[0];
            if (c > 0) w.println("  " + WATCH_NAME[k] + "  x" + c);
        }
        w.println("（未列出的 = 0 次）");

        w.println();
        w.println("--- 关键内存最终值 ---");
        for (int k = 0; k < MEM32.length; k++) {
            int v;
            try { v = mem.load32(MEM32[k]); } catch (Throwable t) { v = -1; }
            w.println("  " + MEM32_NAME[k] + " (0x" + Format.asHex(MEM32[k], 8) + ") = 0x" + Format.asHex(v, 8));
        }

        w.println();
        w.println("--- flag 位模式（id 1..17）---");
        for (int id = 1; id <= 17; id++) {
            int v;
            try { v = mem.load32(flgBits(id)); } catch (Throwable t) { v = -1; }
            if (v != 0) w.println("  flag " + id + " = 0x" + Format.asHex(v, 8));
        }

        w.println();
        w.println("--- 事件时间线 ---");
        w.print(events);
        w.println();
        w.println("--- flag 3 上的系统调用（前 120 条）---");
        {
            String[] ls = svcLog.toString().split("\n");
            for (int k = 0; k < Math.min(ls.length, 120); k++) w.println(ls[k]);
            w.println("（共 " + ls.length + " 条）");
        }

        w.println();
        w.println("--- 文本缓冲 0x9F58D97C 起 64 字节 ---");
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < 64; k++) {
            int b;
            try { b = mem.loadUnsigned8(0x9F58D97C + k); } catch (Throwable t) { b = 0; }
            sb.append(String.format("%02X ", b));
        }
        w.println("  " + sb);
        StringBuilder asc = new StringBuilder();
        for (int k = 0; k < 64; k++) {
            int b;
            try { b = mem.loadUnsigned8(0x9F58D97C + k); } catch (Throwable t) { b = 0; }
            asc.append((b >= 32 && b < 127) ? (char) b : '.');
        }
        w.println("  |" + asc + "|");

        w.println();
        w.println("--- 显示对象数组 0x9F5A0F60 起 9*18 字节 ---");
        for (int s = 0; s < 9; s++) {
            StringBuilder o = new StringBuilder();
            for (int k = 0; k < 18; k++) {
                int b;
                try { b = mem.loadUnsigned8(0x9F5A0F60 + s * 18 + k); } catch (Throwable t) { b = 0; }
                o.append(String.format("%02X", b));
            }
            w.println("  [" + s + "] " + o);
        }

        w.println();
        w.println("--- 链表 0x9F58D500 起 24 个节点(12B) ---");
        for (int s = 0; s < 24; s++) {
            int base = 0x9F58D500 + s * 12;
            int nx, pv, idv;
            try {
                nx = mem.load32(base); pv = mem.load32(base + 4); idv = mem.loadUnsigned8(base + 8);
            } catch (Throwable t) { break; }
            if (nx != 0 || pv != 0 || idv != 0) {
                w.println("  [" + s + "] next=0x" + Format.asHex(nx, 8) + " prev=0x" + Format.asHex(pv, 8)
                        + " id=0x" + Format.asHex(idv, 2));
            }
        }

        w.println();
        w.println("--- 被监控内存的历史取值（去重后的不同值）---");
        for (Map.Entry<Integer, StringBuilder> e : hist.entrySet()) {
            String[] toks = e.getValue().toString().trim().split(" ");
            StringBuilder uniq = new StringBuilder();
            String last = null;
            for (String tok : toks) {
                String v = tok.substring(tok.indexOf(':') + 1);
                if (!v.equals(last)) { uniq.append(tok).append(" "); last = v; }
            }
            w.println("  0x" + Format.asHex(e.getKey(), 8) + " : " + uniq);
        }

        w.flush(); w.close();
        System.out.println("; steps=" + i + " mode=" + mode + " -> " + out);
        System.exit(0);
    }
}
