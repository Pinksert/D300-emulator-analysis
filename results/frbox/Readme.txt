frbox 线的原始输出
==================

全部由 emu/ 下的代码在 1.11 固件上直接跑出来，没有手工编辑（只把本机绝对路径替换成 <repo>）。

文件                    生成命令
----------------------  ---------------------------------------------------------------
selftest.txt            cd emu && python frbox_selftest.py              （258 条断言）
cpu-selftest.txt        cd emu && python fr_selftest.py                 （63 条 CPU 断言）
realos-chain.txt        cd emu && python probe/exp_realos_chain.py      （启动链 + 13 条记录 + 初始上下文）
realos-tcb.txt          cd emu && python probe/exp_realos_tcb.py        （TCB 布局 + 队列归属）
realos-run.txt          cd emu && python probe/exp_realos_run.py        （任务 1 跑 30 万步 + 服务分布）
realos-objects.txt      cd emu && python probe/exp_realos_flgcb.py      （服务分发表 / 对象登记表 / 标志控制块）
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

注意：至今没有发生过一次真正的任务切换。*(0x45980)（当前 TCB）全程只被写过 1 次，
任务 1 的 USP 全程落在 0x0462xx。三条路都不成立（详见 KNOWN_ISSUES 第 39 条）：
硬件中断被 ILM 挡在门外；把 wai_flg 换成 slp_tsk / wai_sem / rcv_msg / dly_tsk
一律立刻返回 E_OK；把 clr_flg 改成 set_flg 只会改变任务 1 自己的走向。
别把这轮的结果读成「多任务跑通了」。
