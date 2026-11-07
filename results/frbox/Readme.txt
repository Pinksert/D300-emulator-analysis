frbox 线的原始输出
==================

全部由 emu/ 下的代码在 1.11 固件上直接跑出来，没有手工编辑（只把本机绝对路径替换成 <repo>）。

文件                    生成命令
----------------------  ---------------------------------------------------------------
selftest.txt            cd emu && python frbox_selftest.py              （510 条断言）
cpu-selftest.txt        cd emu && python fr_selftest.py                 （63 条 CPU 断言）
realos-chain.txt        cd emu && python probe/exp_realos_chain.py      （启动链 + 13 条记录 + 初始上下文）
realos-tcb.txt          cd emu && python probe/exp_realos_tcb.py        （TCB 布局 + 队列归属）
realos-run.txt          cd emu && python probe/exp_realos_run.py        （A/B 两模式：任务 1 跑 30 万步 + 服务分布 + 切换）
realos-objects.txt      cd emu && python probe/exp_realos_flgcb.py      （服务分发表 / 对象登记表 / 标志控制块）
realos-sched.txt        cd emu && python probe/exp_realos_sched.py      （就绪位图/队列 + 优先级编码器 + ILM 约定 + 切换时间线）
boot.txt                cd emu && python frbox_boot.py --max 300000
coverage.txt            cd emu && python fr_cover.py                    （65536 个编码逐个喂）
isr-injection.txt       cd emu && python probe/exp_allisr.py            （48 个向量全量注入）
isr-fire.txt            cd emu && python probe/exp_isrfire.py           （以同一快照为基点，48 向量逐个注入）
autospin-run.txt        cd emu && python probe/exp_autospin_run.py      （自旋消除器 + 任务 1 跑到底）
boot-seq.txt            cd emu && python probe/exp_boot_seq.py          （任务表 / 启动序列 / 精简内核）
sp-rp.txt               cd emu && python probe/exp_sp_depth.py          （栈顶五种取值 × RP 有无）
svc-layers.txt          cd emu && python probe/exp_oldpath.py           （四种 μITRON 挂法对照）
sp-rp-sweep.txt         cd emu && python probe/exp_sp_rp.py             （栈顶/RP 四种组合）
boot-session.txt        cd emu && python probe/exp_boot_session.py    （任务表 / 真内核 / 自动送事件 / 手动送事件）
session.txt             cd emu && python probe/exp_monitor_session.py session
autospin-session.txt    cd emu && python probe/exp_monitor_session.py autospin
irq-session.txt         monitor 的 irq table / irq fire 实录（早期手工录，未脚本化）
all13.txt              cd emu && python probe/exp_all13.py             （V16 三场景总账 5→9→11 / 12）
unlock-chain.txt       cd emu && python probe/exp_unlock_chain.py      （V16 逐任务轨迹）
unlock-live.txt        cd emu && python probe/exp_unlock_chain.py --live
msg-static.txt         cd emu && python probe/exp_msg_static.py        （V17 服务号普查：1,008 处 int / 1 处 rcv_msg / 15 处 snd_msg）
mbx-live.txt           cd emu && python probe/exp_mbx_live.py          （V17 任务 5 的等待现场 + 三次消息调用）
mbx-probe.txt          cd emu && python probe/exp_mbx_probe.py         （V17 kernel_call 代投 + 信箱控制块结构）
msg-frame.txt          cd emu && python probe/exp_msg_frame.py         （V17 抄固件自己造的那条消息）
msg-slot.txt           cd emu && python probe/exp_msg_slot.py          （V17 投递后的 11 次服务调用链）
msg-matrix.txt         cd emu && python probe/exp_msg_matrix.py        （V17 内容矩阵 15 例）
msg-chain.txt          cd emu && python probe/exp_msg_chain.py         （V17 A–E 五场景）
msg-full.txt           cd emu && python probe/exp_msg_full.py          （V17 与 flag 通道叠加 P/Q/R）
cb-live.txt            cd emu && python probe/exp_cb_live.py             （V18 访存监视：回调段 32 地址命中 0 / 落地缓冲只写四个半字）
cb-dispatch.txt        cd emu && python probe/exp_dispatch.py            （V18 槽结构 + 手动调派发器）
cb-chain.txt           cd emu && python probe/exp_cb_chain.py            （V18 只 tick 240 次的门控节奏）
cb-full.txt            cd emu && python probe/exp_cb_full.py             （V18 A–E 五场景总账 +246 / +507）
isr2b.txt              cd emu && python probe/exp_isr2b.py              （V19 A–H 八段：向量表 / 61 开关 / +2053 / 第 9 次卡住）
tick.txt               cd emu && python probe/exp_tick.py               （V20 A–H 八段：0x174 归属 / 时基分水岭 / 605 步 / 只读 0x186 15 次）

关键读数
--------
A 段   43,446 步（跑到出界为止；在 0x000FE50C 下断点则是 43,445 步）/ 824 个不同 PC / 0 次中断
B 段  191,518 步（断点 0x000407D8 处是 191,517 步）/ 1,239 个不同 PC / 1 次中断
      —— 两个数差 1：断点数是「执行到该 PC 之前」，出界数是「含跳出去的那一步」
48 向量注入：39 个精确 reti 回注入点且 r15/SSP 逐位复原
编码空间：可执行 64,682 / 未定义 854 / 未实现 0 / 助记符 122

任务 1 的四种挂法（数字别混着比，见 svc-layers.txt）
---------------------------------------------------
同一快照、同一 200 万步上限，只换 int #0x40 那一层。「次数」按执行到的 int 指令条数数，
不用钩子计数 —— 不挂钩子时它真的会走 EIT 进向量表，挂上钩子就被截住当服务返回。

A 不挂钩子（走真向量表）        56 次 / 累计 42,442 步 / 停 0x081338（停机占位）
B --itron 一律立即返回          56 次 / 累计 38,639 步 / 停 0x081338
C --itron-block 真阻塞           9 次 / 累计  9,841 步 / 停 0x081492（第一个 wai_flg）
D --itron-block --autoevent     56 次 / 累计 35,573 步 / 停 0x0813F8（ext_tsk 之后）

⚠️ A 和 B 也是 56 次，和 D 一样多。所以「调用次数」根本不是判据 ——
判据是有没有真的在某一步停下来等。只有 C/D 会停。

D 的细节：13 个握手事件 / 56 次服务调用 / 10 次 sta_tsk / 4,210 个不同 PC，
最后 sys_ext_tsk 退出（PC=0x0813F6）——任务 1 是一次性初始化任务，不返回不循环。

栈顶与 RP（见 sp-rp.txt / sp-rp-sweep.txt）
------------------------------------------
⭐ 栈顶不影响结局，RP 决定结局。RP=0x81338 时，栈顶从 0x45000 到系统栈 0x7F000
   五种取值跑出来都是入口后 32,726 步、停 0x081338，一个字节不差
   （任务的压栈/弹栈是平衡的，栈顶只决定那 4 个字节压在哪儿）。
   RP 不给时：栈顶给真值 0x462EC → 跑满 200 万步停 0x0FE404；
              栈顶给表头字段 0x4616C → 累计 39,391 步就 PC=0。
   所以「掉到地址 0」的成因只有一个：boot_task 从没设过 RP，
   沿用了引导阶段最后一次 call 留下的 0x080500。

boot.txt 里 A 段有两种跑法（时钟自己锁 8 tick / 立刻锁定），两种都收敛到同一组 824 个 PC
—— 这是"时钟建模没有改变程序行为、只是让等待走完"的判据。

autospin-run.txt 里"没有一条硬件中断被受理"是刻意的判据：ICU 的累计请求是 0，
56 次都是 int #0x40 的 μITRON 陷阱。别把这两个数混着读。

任务表（0x000FE000）★ V10 更正：13 条不是 8 条
----------------------------------------------
0x000FE000 起 16 字节是头部，*(0xFE000) = 8 是用户任务数，不是记录总数。
记录实际 13 条，从 0x000FE010 起每 0x1C 字节一条，一路排到 0x000FE160：
id 1-12 加一条 id 0 的空闲任务（入口 0x000FE510 是一条 bra -1）。
字段顺序：TCB 指针 / (任务号<<16 | 属性) / 保留 / 状态 / 入口 / 栈顶 / 保留。

V9 留下的「tsk_id 9/10 在表里找不到」就此结案：任务 9 入口 0x000AD74E、
任务 10 入口 0x000BFAE6，都在表里。三条交叉验证见 realos-tcb.txt：
TCB+0x0C 高 16 位 == 表里的属性、TCB+0x18 == 表里的栈顶、TCB+0x24 == 自己的表记录地址，
13 条全部核对得上。

任务 1 入口 0x0008133A、栈顶 0x000462EC —— 现在不再是「从表里读出来的」，
而是固件内核亲手写在任务栈顶的事实：[0x08133A, 0x001F0030, 0x000FE250]。

V10 起的两件大事（详见 realos-*.txt）
-----------------------------------
一、固件自带的 REALOS 能原生跑起来。V2-V9 一直说「内核进不去」，真因只有一个 CPU 缺陷：
   andccr / orccr 改 CCR 的 S 位时不换 R15（self.s 被直接赋值，绕过了 ps 的 setter）。
   修掉以后复位链一路跑到固件自己的调度器：43,428 步到派发点 0x000FE4E6，
   派发点后面那二十几条 (0x0FE4DE-0x0FE514) 就是上下文恢复，靠翻 S 位在 SSP/USP 之间搬指针。
   此后不需要自己写调度器；花了八个版本做的 μITRON 半主机只剩对照用途。

二、realos-run.txt 里那段 300,000 步：1,888 次陷阱全部由固件内核处理
   （clr_flg 754 / wai_flg 751 / sta_tsk 376 / set_flg 6 / snd_msg 1），
   服务分发表 @0x0FE3B8、对象登记表 @0x0FE180、标志控制块 @0x045C38（见 realos-objects.txt）。
   ⚠️ 这组数字是【A 模式】（只开 xfer、不开 0x44）的读数。

V11 起的三件大事（详见 realos-sched.txt）
----------------------------------------
一、任务切换真的发生了 —— V10 那句「至今没有发生过一次真正的任务切换」作废。
   真因不在内核，是仿真台少建了三样东西：
     1. ILM 的优先级约定搞反了。正确的是「级别号越小优先级越高，受理条件 lvl < ILM」。
        反证很硬：任务上下文 PS 的 ILM=0x1F，而 48 个 ICR 的级别落在 0x12-0x1F；
        按旧约定任务运行时一条中断都进不来，固件等于全聋。
     2. 直接页 0x44 是「请求调度」寄存器：内核写 1，固件里没有任何一处读它
        （30 万步写 1016 次）→ 只写不读，只可能是硬件。写 1 = 挂 vec 0x3F。
     3. 0x3F4/0x3FC 是硬件优先级编码器：写就绪位图进 0x3F4，从 0x3FC 读回
        「最高优先级号」= 前导零个数（实测 0x50800000 → 1）。

   100 万步的切换时间线（*(0x45980) 每次变化）：
     43,429 任务 1 → 43,516 任务 5 → 43,806 任务 1 → 44,614 任务 5
     → 45,125 任务 1 → 45,341 空闲任务，最后停在 0x000FE510（空闲任务的 bra -1）。
   调度请求 7 次 / 优先级编码查询 7 次 / 位图为 0 的次数 0 / 中途没有停机。

二、⚠️ A/B 两个模式必须分开报，数字不能混着比：
     A = enable_xfer() + realos.prepare()       任务 1 一路跑到底，*(0x45980) 只写 1 次
     B = boot_realos()（= A + enable_dispatch()）*(0x45980) 写 6 次，真切换
   上面那组 1,888 次是 A；B 模式下 30 万步只有 14 次服务调用
   （clr_flg 5 / set_flg 3 / wai_flg 2 / rcv_msg 1 / snd_msg 1 / can_wup 1 / slp_tsk 1），
   因为任务 1 早就阻塞了，剩下的时间都在空闲任务的 bra -1 里打转。

三、别把「任务切换跑通了」读成「相机跑起来了」。13 个任务里只有 3 个上场
   （任务 1 / 任务 5 / 空闲任务）。其余 11 个的上下文要靠 sta_tsk 去建，
   而触发 sta_tsk 的硬件事件（按键 / 拨盘 / 存储卡 / 时钟）我们还没有。
autoirq.txt            cd emu && python probe/exp_autoirq.py           （V21 周期时钟：频率扫描 + 长跑）
bboot.txt              cd emu && python probe/exp_bboot.py           （V22 B 段 + V23 刷新：boot 191,520 步到空闲任务、两墙拆除后一次派发 +1349、全开 28 任务 2/3/4/5 上过场 +1910、42 条真 ISR 向量扫描）
                       ⚠️ 探针 boot() 多跑 2,000 步空闲（193,520 = 191,520+2,000，覆盖 1,241→1,242）
pendbits.txt           cd emu && python probe/exp_pendbits.py       （V22 A 段：位分派器 61 位逐位点亮 25/61、最强位 ×64 +2,822、位间状态耦合）
bwall.txt              cd emu && python probe/exp_bwall.py          （V23 B 段：两道墙的定性拆除 —— 0x1C2A98 指针表/擦除态 flash、0x5C003000 计数器；开机装配全程 11 次 sta_tsk；终点 flag 3 主事件循环）
pendbits2.txt          cd emu && python probe/exp_pendbits2.py      （V23 A 段：61 位干净机单点全定性 —— 18 位 A 类 / 48 位 C 类 / 0 位 B 类，一位一台新机器）
bflag.txt              cd emu && python probe/exp_bflag.py          （V24 B 段：13 条服务分发表、flag 对象布局、wfmode 语义（bit1=OR/bit0=清位）、主循环等待位 0x80、42 条 ISR 无一位能置 0x200000、计数器速率标定）
bgate.txt              cd emu && python probe/exp_bgate.py          （V25 B 段：门字节 0x9F58D08F 的 19 处引用与唯一写点 0x12F180、开门实验 +102 地址、内核 RAM 上界 0x6801DE84、任务停点表、服务 -170 未实现）
bdisp.txt              cd emu && python probe/exp_bdisp.py          （V26 B 段显示链路：描述符表 70 条、绘制函数不写像素、EXPEED 整场 238 次写、SDRAM 最长连续 2,126 B、文案池分段、重建图）
ui-rebuild.png         （V26 重建图：左侧模拟屏 + 右侧三条证据；像素为重建侧补绘，非固件渲染）
bexpeed.txt            cd emu && python probe/exp_bexpeed.py        （V27 B↔EXPEED 位串行解码：CS/CLK/DATA、LSB 先、整场 2 帧 = 读寄存器 6 + 40 位数据相位、8 次端口配置写、0 条写命令）
bexpeed2.txt           cd emu && python probe/exp_bexpeed2.py       （V28 EXPEED 器件模型：4 个 CS 窗口 / 读 6 + 48 位 + 写 0x76=1 / 寄存器 6 存 0x9F5A321C、bit7 改路径 2632→2692）
                       ⚠️ bexpeed.txt 的「2 帧 / 0 条写命令」是 V27 抓包器 bug，V28 已修正
bxstat.txt             cd emu && python probe/exp_bxstat.py         （V29：采样点 0x19EF5E / 7 组值全对 / 写 0x76 = [0x9F5A321E]+1 载荷 [1] / 整场只写 1 次 / 0x9F5A321E 被 0x120458 与 0x12DE08 读）
bgate2.txt             cd emu && python probe/exp_bgate2.py         （V30 第二道门：A 30 处=26 读/1 写、B 44 处=23 位写；字节 0x40 放行、多 7 个地址；槽位 0x9F58D508 + r4*0x0C）
bqueue.txt             cd emu && python probe/exp_bqueue.py         （V31 显示请求队列：0x12E6F8 312 处引用、签名 r4/r5、head 0x9F58D500 + 节点 0x9F58D508+n*0x0C + 头插 0x124F5A、补 r5 无增益、0x120458 静态无入口）
bdispchain.txt         cd emu && python probe/exp_bdispchain.py     （V32 队列两端：容量 70 槽位、生产 wai_sem(17)→头插→sig_sem(17)、消费 摘head→清70槽→set_flg(3,0x80)、sem17 八处四对）
bsem.txt               cd emu && python probe/exp_bsem.py          （V33：信号量表 0x54F2C 40 项 / 计数在对象 +8、三写者 / sem 17 = 1 / 0x124F46 init、0x124F5A 插到哨兵之后 / 节点 +0 next +4 prev / 槽位 +9 唯一写者 0x12E740）
blist.txt              cd emu && python probe/exp_blist.py         （V34：链表四件套 init/is_empty/insert/unlink、表头 [条数|项宽] 40|16 与 17|16 互证、sem 14/27/29 计数信号量上限 7/255/255、消费端清空/取首/重挂）
btable.txt             cd emu && python probe/exp_btable.py        （V35：对象表 = 4 字节表头 + 16 字节记录、sem 表止于 0x551B0 与 flag 表首尾相接、obj+0xC 背指记录 40/40、上限只在记录 +0xC、上限检查代码未执行、槽位 +8 载荷从没被写、游标块 0x9F58DA81..84、清空唯一调用 0x12E01E、rel12 假边界 KI#134）
bdesc.txt             cd emu && python probe/exp_bdesc.py        （V36：描述符表 70 条、索引 = 槽位号 69/69、表在 ROM、0x9F58D858 = 当前描述符、81 = 事件掩码、槽位 0 是空标记、取首返回值在 R4、is_empty 判据）
                       ⚠️ 「取首返回值在 R4」只在整段跑完时成立：中间 R4 = is_empty、R5 = 载荷（V38 更正）
bhandler.txt          cd emu && python probe/exp_bhandler.py      （V37：43 个非空处理函数=事件翻译器、flgid 只有 1/3/10/12、42 条 set_flg 全用立即数、空处理 5 函数 31 槽位、+8=2 是代码常量分派、掩码区 9 字节、提交不写 r5、显示模块 wai_flg(3)）
binit.txt             cd emu && python probe/exp_binit.py        （V38：槽位 +8 = 槽位编号 70/70（写入者 0x12E61C）、0x12E006 746 步跑通做五件事、调用者是任务 5 入口 0x12DFB8（更正「任务 4」）、sta_tsk(5) 改 TCB 但就绪位图不变、ret 读 DR1 不读栈（旧探针假返回）、wai_sem 非任务上下文卡死 / wai_flg 返回 -33、取首 R4=is_empty R5=载荷）
bready.txt            cd emu && python probe/exp_bready.py      （V39：0x45984 在 B 段是 ROM（写丢弃、读回 0x2E80A800）；真正的就绪结构在内核区头部 0x68000000（+0 当前 TCB / +4 位图 / +8 起 8B 队列哨兵）；队列号 = 优先数-1、位 = 31-队列号；sta_tsk(5) 入队 3 + 置位 28 + 写 0x44 请求派发（0x41886）；派发器 0x40D32 选优先数最小；换手 空闲→任务2→任务4→任务3→任务5，任务 5 上 CPU 后 0x12E006 自己跑完）
blive.txt             cd emu && python probe/exp_blive.py      （V40：0x44 = 锁存器（写 1 置位/写 0 撤销/读回锁存），boot 里两次写只挂 1 条；0x46F = vec 0x3F 的 ICR，48 条里唯一不被 init 循环写（init 写了 47 条 0x440..0x46E）；服务入口 0x4069C 抬到 0x1E、出口 0x406D4 写回 0x10 → 服务期间被 ILM=0x1E 挡住、reti 后 ILM=0x1F 才受理；整机自跑 40 万步 24 次换手 / 9 个任务 / 显示初始化 294,388 步自己跑完；51.5 万步被任务 9 宽写清零 0x46C..0x46F 后停住；J 段 A/B：锁存语义让 A 段首切 43,622→43,540、首次 set_flg 44,073→43,991（各 −82），切换 4/服务 14/总步 243,428 不变）
bfill.txt             cd emu && python probe/exp_bfill.py      （V41：0x1356A0 = memset 循环里的 `st r12,@(r13,r0)`（r0 基址 0x80 / r12 值 0 / r13 递减偏移），归属靠寄存器对总线实测（地址=r13+r0 与 值=r12 各 2,147/2,147）；70 万步 23,617 次 = 11 次调用、每次 2,147 笔×4B、起点 0x2208 终点 0x0080；落点 低区 RAM 12,342 / direct page 352 / ICR 352 / 其余 10,571；停摆真因 = 填充把 ICR 0x46F 写成 0x00（70 万步清 11 次，V40 记的 0x80 作废）；反事实改道影子 RAM → 换手 24→37、任务入口 9→17、收尾回到空闲 0x407DC → 停摆是模型副作用；「7 条指令死循环」实为 16 位拷贝循环；0x9F58DA85 1 写 0 读；41 笔非 0 写入判为模型问题（KI#169））
bmove.txt             cd emu && python probe/exp_bmove.py      （V42：`0x1356A0` 不是 memset 是 **memmove** 的存储端 —— `0x135690 add r9,r0` / `0x135694 add r8,r1` ⇒ dst=r9+0x80、src=r8+0x4C、len=0x2188、位移 +0x34；V41 的「dst=0x80、val=0」只在 r9=0 时数字上成立；证据 快照对拍 after[x+0x34]==before[x] 2,215/2,227 + 首次搬运逐笔 v==rd(dst-0x34) 2,147/2,147；11 次搬运全部 r8=r9=0，落点 ICR 块 176 / direct page 352；搬运者是任务体入口 0x1352EA（首次步 484,584、dr1=0x404F8）的主循环体，间隔 24,353 步 = 任务周期；判空链条 #0→r9 → cmp → bne → call 0x122D32 → mov r4,r9，**调用方不判空**；静态 写 r9 4 条 / 写 r8 3 条 + 动态执行次数 0/0/1/1 与 1/0/0；反事实 fix_dst 只改 0x135624 的 r4：换手 24→37、末次换手 501,944→660,398、收尾 0x1EDD80→**0x407DC 空闲**、0x46F 末值 0x00→0x10、入口 9→17，两个安全指针 0x2200/0x30000 结果逐项相同 → 停摆是模型副作用；41 笔非 0 = 搬来的源数据（KI#169 结案）；仍 OPEN：两次取缓冲区都返回 0（KI#171））
bpool.txt             cd emu && python probe/exp_bpool.py      （V43：**池的地址根本没映射** —— 计数 0x9F69CE98 / 链表头 0x9F69CEB4·CEE4·CEFC / 节点 0x9F696844·8A60·AC7C 全在 0x9F680000 之后，旧 ram-sdram 只到 0x9F680000，写被丢、读恒 0；判据 写 0xA1B2C3D4 后读回 1MB=0 / 2.5MB=原值；两档池初始化逐笔相同（自指 3 次 @415345·415366·419098、串 3 节点 @419147·181·215、计数=3 @419230）；铺到 0x9F580000+0x00280000 后 取缓冲区 11→0、NULL 1→0、换手 24→34、收尾 0x137968→0x407DC、0x46F 0x00→0x10、入口 9→17，与 V42 fix_dst 逐项吻合；仍 OPEN：分配器进 0x122064 后不返回（KI#172）、0x122D5A mov 方向（KI#173）、还剩 7,933 笔未映射写（KI#174））
lists.txt             cd emu && python probe/exp_lists.py       （V44：**固件的链表节点区从没建过存储器** —— 3003 个节点 × 0x14 住在 0x9F553620..0x9F5A0CDC；写点只有 0x124F5C/0x124F60、写值 100% 落在 0x9F5xxxx、步长 +4/+16 严格交替；建链循环 0x19ACF2（#0x3E8=1000 次、步长 0x14）；补 0x9F550000+0x30000 后 未映射写 7933→1933、读 3012→12、三条链各 1001 个节点且双向 1001/1001 配对，**而整机控制流数字一个都没变** ⇒ 纯数据区；同时更正 V43：0x124F62 被引用 0 次、是 0x124F5A 的第 4 条指令，且 0x124F5A 是头插（KI#175）；仍 OPEN：剩 1933 笔全是外设窗口，该建设备模型不该铺 RAM（KI#174-b））
bctrl.txt            cd emu && python probe/exp_bctrl.py       （V45：**0x50000000 是三通道位串行控制块** —— 三组基址 0x100/0x200/0x300 各 16 字节；+05 是串口：bit5=CLK、bit3=DATA、bit6-7=状态；+06 bit2=片选；0x125812 的函数按 r4=0x8000→lsr 走 16 圈逐位发，实测写序列 20 00 00 20 00 08 28 …（0x20/0x00 交替 16 次、0x08 只出现 1 次），通道 A 载荷 **0x2000**（17 时钟 = 1 起始 + 16 数据），通道 B/C 只打起始位无载荷；建器件后 未映射写 1933→**1853**、读 12 地址/69 次→**5 地址/9 次**、换手 34/入口 17/末 PC 0x407DC/ICR 0x10 **一个都没变**；反事实：+04 bit1=1 → 换手 13/入口 9（0x1204A0 的 btstl #0x2 跳过开机装配），这也是「铺 RAM 后 34→13」的真因；0x40/0x6F 建 1730/108 笔写只配 4/0 次读、写进去是 ROM 表（等差段 / 单调曲线 00 2D 40 4E 5A …）⇒ **只写数据汇**；踩坑：bus.io_value 打桩对未映射地址无效（KI#177）；F 段：全局 +01 的 bit0 是**状态位**（0x4014A btstl #0x1 → 0x4014C beq 跳过 → 0x4014E bandl #0xE 清位，两路在 0x40150 汇合），默认 0 与 V44 逐拍一致，置 1 只多一条指令 ⇒ boot 从 191,520 步变 191,521 步到空闲（**1 步相位，不是岔路**；逐拍 13/13 对齐验证）；另 +08 被 0x120CC0 的 orh 读改写、必须可读（读 0 会叠成 0x4000 而不是 0x6000），那笔 RMW 读在 access_log 记成 @PC 0x0 是 fr_emu.py:503 不带 pc 的记账噪声（KI#178））
dual40.txt            cd emu && python probe/exp_dual40.py     （V46：**0x40 不是只写数据汇、0x60/0x61 不是哑巴** —— 0x60 双通道 16 笔（11 写/5 读）跑同一段代码（0x1A36BC 初始化 / 0x1A36DA 查状态），基址来自 0x8400·0x8450 两张 0x50 字节描述符表（任务 2 步 291,872–292,063 填）；状态字段用 256 个值逐个直调定死在 **bit2–3 == 3**（64 命中，lsl2 #12 / lsr2 #14 是 u4+16 的 28/30 位，KI#181），但 8 个调用点都紧跟 ldi:8 r4,0 把返回值清掉 ⇒ 置成就绪只让覆盖 5,404→5,421、换手/入口/末PC 全不变（阴性结果）；0x40 的 1,650 笔写 / 54 个 PC = 寄存器实例（+0xF8 两次 RMW 叠出 0x3B3B / 0x1D1D / 0x0000，不回读会写成 0x003B）+ 批量下载口（6 张 16 位曲线表 1,541 项 3,082 字节，6/6 逐字节等于 ROM，占窗口 50%）（KI#179/#180）；建两块器件后 未映射写 1,853→**112**、未映射读 5 地址/9 次→**0/0**、换手 34/入口 17/末 PC 0x407DC/ICR 0x10/覆盖 5404 五项全不变）
bwait.txt             cd emu && python probe/exp_bwait.py      （V47：**池的分配器等的不是锁，是事件标志 6** —— 骨架是 lock(19) → clr_flg(6,~0x20) → 两次 is_empty → unlock(19) → wai_flg(6, 0x20, mode=2)，加锁那一步**成功**（sem 19 计数 1→0，内核步 495,923 写 0）；`0x122084 ldi:8 r4,#0x13` 是 call:d 的**延迟槽**= lock(19) 的实参（V43–V46 当它死代码）；`0x12590C`/`0x125992` = 优先数继承互斥量，持有者表 `0x9F58C4D0 + sem_id`，全机 6 个任务轮流拿 sem 19、取放成对、无泄漏；flag 6 是**整族的门铃**：set_flg 只碰 1/3/10/12/14，wai_flg(6) 被 7 个任务等过；反事实补一次 set_flg(6,0x20) → 任务 9 状态 4→0、走 0x122162 重新加锁、再走一遍、第二次等，调用方 0x13531E 仍 0 次（阴性结果）；另修 flow 的 TCB 槽：B 段该读 0x68000000 而非 A 段的 0x45980，修后 tcb 1→18 / switches 1→34 / 任务数 1→17，换手覆盖全不变）
