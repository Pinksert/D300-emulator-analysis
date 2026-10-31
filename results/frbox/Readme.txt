frbox 线的原始输出
==================

全部由 emu/ 下的代码在 1.11 固件上直接跑出来，没有手工编辑（只把本机绝对路径替换成 <repo>）。

文件                    生成命令
----------------------  ---------------------------------------------------------------
selftest.txt            cd emu && python frbox_selftest.py              （212 条断言）
cpu-selftest.txt        cd emu && python fr_selftest.py                 （54 条 CPU 断言）
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

任务表（0x000FE000）
--------------------
0x000FE000 起 16 字节是头部，8 条记录各 0x1C 字节从 0x000FE010 开始。
字段顺序：TCB 指针 / (任务号<<16 | 属性) / 保留 / 状态 / 入口 / 栈顶 / 保留。
复位后任务 1 和任务 5 状态为 0x80000000（激活），其余 6 条为 0。
任务 1 入口 0x0008133A、栈顶 0x000462EC；前 2 字节 0x00081338 是 `bra -1` 停机占位。

注意：`sys_sta_tsk` 传进来的 tsk_id 出现过 9 和 10，而表里只有 8 条 —— 这一处未定，
别把"启动了 10 次"读成"启动了 10 个任务"（KNOWN_ISSUES 第 34 条）。
