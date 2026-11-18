# VibeLifeBench 接入审查

VibeLifeBench 论文描述 200 个多周模拟任务和 22 个模拟服务。上游 README 声明可本地获取 100 个 Harbor 任务，其中 20 个另有 Terrarium 原生格式。首轮从原生任务的事件与评分契约查起，完整基准结果需要按覆盖的公开任务和运行格式分别报告。 [论文](https://arxiv.org/html/2608.10875) · [上游代码与公开范围](https://github.com/evolvent-ai/VibeLifeBench)

## 已核对的运行限制

- 原生 Galapagos 任务按阶段和时间排序分发事件，并在阶段末评分。后台变化会改变服务状态；可见的 user message、notification 或 world 事件才传给模型。要测试静默变化后、下一条可见事件前的主动检查，需要为所有策略提供相同的额外时钟，作为明确标注的评估扩展。 [原生任务入口](https://github.com/evolvent-ai/VibeLifeBench/blob/main/eval_set/travel/galapagos_no_us_transit/task.py) · [论文事件定义](https://arxiv.org/html/2608.10875#S2.SS2.SSS3)
- `galapagos_no_us_transit` 的首段航班风险于 S19 的 14:20 写入服务，17:35 的出发通知明确要求复查首段航班。原生 S19 可测试**通知后的读取选择**，对应 `s19_departure_delay_response` 检查。要测试有没有在 3 小时 15 分钟的通知前时段独立发现，需要公共时钟扩展和可读端点验证。陶艺团建的 S5 同样在静默写入供应商问答后发出同主题 `world` 事件。按事件类型统计 mutation 数量无法回答有多少变化最终需要独立发现。 [旅行时间线](https://github.com/evolvent-ai/VibeLifeBench/blob/main/eval_set/travel/galapagos_no_us_transit/event.yaml) · [S19 检查](https://github.com/evolvent-ai/VibeLifeBench/blob/main/eval_set/travel/galapagos_no_us_transit/rubrics/stage_19.py) · [团建时间线](https://github.com/evolvent-ai/VibeLifeBench/blob/main/eval_set/team_building/pottery_invoice_compliance_day/event.yaml)
- 原生任务入口保存阶段级工具轨迹，也有工具质量评分桶。结合评分项做失败归因前，要核对每个任务实际读取的证据、时钟和打分字段。这次为静态源码审查，运行器、候选端点和评分恢复实验尚未跑通。 [原生任务入口](https://github.com/evolvent-ai/VibeLifeBench/blob/main/eval_set/travel/galapagos_no_us_transit/task.py)

## 两个实验口径

**原生重放。** 只在原本可用的 agent turn 做读取选择，保持事件顺序、工具和评分器。第一步对原生旅行及团队活动任务固定模型与执行器，挑选同时包含多个候选只读来源的节点。单次可见事件里已经直接给出关键值的样本单独标记。

**公共时钟扩展。** 若原生重放的可观察窗口不足以检验主动监测，按全局预先固定的时间间隔插入空白时钟事件。它只暴露当前时间，所有策略得到同一套时钟、工具、总读取预算和执行权限。不能按 mutation 时刻或其数量生成唤醒信号。对恰好同时发生的 mutation、读操作和评分冻结优先级。扩展结果单列，原生轨迹用来核对差异。

## 失败归因与配对评估

1. 以完整任务族划分开发集和确认集。开发阶段审查原始轨迹与可用只读工具，登记 `任务、承诺、真实变化区间、已见提示、读操作、最晚有效决策时间、相关评分项`。评估标注对运行中的策略不可见。优先纳入变化仍可被读到且还有修订时间的案例，同时保留无变化、变化无关、晚于窗口和已被明确告知的对照。
2. 在同一虚拟时间从相同历史重放三组。第一组保留原模型轨迹。第二组在可行时间内提供正确工具调用的返回并使用同一修订器。第三组提供同成本但无关的读取结果。工具结果补给只用于判断**该失败是否可由更好的观察修复**。已读到仍未正确修订的案例单列为推理或执行失败。扩展时钟下同样评估有无可行窗口。
3. 对可归因案例进行方法比较。固定可见信息、同一 LLM、修订器、时间窗、权限与最大调用数。基线包括原生提示式工具选择、固定间隔、最旧优先、最接近截止优先、变化概率、单位成本信息增益。重复运行使用共享种子或共享已生成的用户事件。冻结模型及评分设置后，以任务为统计单位做配对比较。
4. 报告在有效窗口获取证据的比例、评分项的恢复幅度、任务级原生得分、每次发现的读取成本、错误通知次数与发现延迟。主对比同时给出实际读取量和相同平均成本区间内的收益。使用真实模拟时钟而非阶段编号度量延迟。对机会型样本和全部任务分别给分母，使用任务级汇总与置信区间。

第一轮先验证一个任务中至少一条闭环能重放：原始轨迹漏读或读晚，补给当时可读的关键结果后，同一修订器能修订计划并通过对应原生检查。若只能在修改后的时钟或通知消融里做到，按扩展协议解释收益。
