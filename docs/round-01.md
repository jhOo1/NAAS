# 方向 B · 首轮协议与覆盖审计

## 两轮讨论后的决定

首轮评审包已发到[原 NAAS 对话](https://chatgpt.com/c/6afd60fd-dfc8-83ec-a099-939aeaa60eeb)。第一轮独立评审指出，全局 LLM 在看到全部开放决策、使用跨页等待和共享缓存时，也可能识别共享证据。第二轮确认以下协议，并明确强调各组只能在自己实际选择读取后获得相应结果。

**待证伪的论文故事**　当一条证据对不同待定决策的作用取决于约束、出处和时效，条件化依赖调度能否在同预算下，比具备相同观察工具的全局规划器更早取得改变决策的证据？以一次飞行状态读取为例，接送由航班触发和接送独立安排是两个不同的依赖条件。证据过期和重复计算同一后果均可能制造错误的调度收益。暂定标题为 *Which Evidence Should a Personal Agent Refresh Next?*

| 对照 | 输入与权限 | 用途 |
| --- | --- | --- |
| 全局联合规划 | 同一冻结模型，完整可见任务和候选来源，跨页等待、共享缓存、预算与时钟 | 主基线，可联立所有决策分配预算 |
| 人工依赖调度 | 人工依赖只从各组共同可见的历史整理，禁止使用未来事件、隐藏状态和评分器 | 机制上限诊断 |
| 自动依赖调度 | 同一冻结模型从可见证据抽取条件依赖，再由调度器选择读取 | 与主基线做正式比较 |

所有组从同一初始历史和环境快照出发。实际读数只在本组请求对应来源并抵达对应时间后由环境提供。缓存每组单独初始化和更新，规则一致。候选、系统权限、下游执行器、预算和可见时钟共享。匹配观察预算的同时记录端到端模型计算和页面检查等成本，避免让等待工具内部轮询在成本中消失。

- **主机制验证**使用 VibeLifeBench 的公开原生决策窗口。必须能在已定义的原生唤醒处获得证据，并证明至少两个独立后果竞争观察预算。首轮启动门槛为至少 10 个不重叠窗口且覆盖至少 5 个任务。门槛用于确定开发样本可用性，正式统计以独立任务为单位另做样本量规划。
- **观察能力对照**使用 SentinelBench 原生的同页等待、详情核验和 No-op。对照检查能否正确等待和验证读取，原生单条件任务对多决策竞争的外推需要额外数据。
- **失败归因**在同一历史、同一时刻和同一执行器下，分别运行原轨迹、补给当时可读的关键证据、补给同成本无关证据。仅依靠关键证据恢复的评分项构成观察可修复失分证据。原生覆盖不足时只保留探索结论。额外时钟或组合任务采用明确标注的扩展协议。

两项证据都需要才能提高论文主张。第一项是独立长程任务出现足量可复现、可修复的观察失分。第二项是自动依赖调度在共同预算下提高限时正确率，且依赖错标、误动作和计算成本可解释。若人工依赖都无法超过全局规划基线，应收窄到有原生证据支持的跨来源核验问题。任务图、等待和主动取证均有直接近邻。 [ProAct-75](https://arxiv.org/html/2602.03430) · [Pro-Memory](https://arxiv.org/html/2611.05731) · [ProAct 空闲时间取证](https://arxiv.org/html/2605.25971v1) · [SentinelBench 等待工具](https://arxiv.org/html/2606.05342#A4)

## 首次覆盖审计结果

以下记录保留首次结构盘点的状态。[后续报告](native-coverage.md)给出全部 256 个候选的逐条核验与最终启动判定。

两套公开源固定在 [VibeLifeBench e557448](https://github.com/evolvent-ai/VibeLifeBench/tree/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set) 和 [SentinelBench 3fa7634](https://github.com/microsoft/sentinel_environments/tree/3fa7634e114c8d223f8f2e54464be1fadbd8c7a6/scenarios)。源清单按相对路径和内容哈希固定。如需重跑，传入上述版本的源代码目录。审计脚本需要 PyYAML。

```sh
python3 audit_coverage.py --vibelife /path/to/VibeLifeBench --sentinel /path/to/sentinel_environments --windows
```

| 审查 | 结果 |
| --- | --- |
| SentinelBench | 100 个原生场景，10 个应用，20 个 No-op。阅读全部 100 条任务提示，当前未发现同时分配有限观察预算、具有不同成功条件的多决策任务。多个邮件、会话、岗位或文章匹配同一条件按一个监控目标计算。 |
| VibeLifeBench | 20 个 Terrarium 原生任务的事件清单，共 877 条事件，其中 280 条 mutation。263 条 mutation 在同阶段有更晚的声明信号，对应 256 个去重信号。信号为非 silent 的 `user_message`、`notification` 或 `world`，脚本另计 4 条 `cron_fire`。 |
| 主机制门槛 | 当前完成结构盘点及两个原生时点核对。尚无同时经过工具可读性、独立下游决策和评分恢复核查的窗口。256 是后续筛选池的结构计数。 |

源清单 SHA-256 分别为 `00e2a0487ca0ff2ba6136674a087a114089a3521e16dfd4a5c626611d5f19538` 和 `79380b0728cd117a673bdc82ed9d02adccd0d848aac8074838429ccd310b9b3f`。清单内有带时区时间、无时区本地时间和只给时分的时间。脚本只计算同阶段时差。各任务的真实唤醒条件、工具权限和可用读取在逐条审查时以该任务运行器为准。

| 已核对的原生控制 | 条件与评分 |
| --- | --- |
| [`micromail-junk-relative-passive`](https://github.com/microsoft/sentinel_environments/blob/3fa7634e114c8d223f8f2e54464be1fadbd8c7a6/scenarios/micromail/junk-relative-passive.json) | 297.75 秒时垃圾邮件相对初始值增加 3，评分检查数量与条件后报告。 |
| [`micromail-body-december-relative-active`](https://github.com/microsoft/sentinel_environments/blob/3fa7634e114c8d223f8f2e54464be1fadbd8c7a6/scenarios/micromail/body-december-relative-active.json) | 条件时刻 370.68 秒，评分检查具体邮件 `ext-21` 已被打开标为已读，并检查条件后报告。 |
| [`microchat-attachment-noop`](https://github.com/microsoft/sentinel_environments/blob/3fa7634e114c8d223f8f2e54464be1fadbd8c7a6/scenarios/microchat/attachment-noop.json) | 目标附件不出现。侧栏无法显示附件，要求进入会话核验。原生 No-op 评分要求没有报告触发。 |

VibeLifeBench 的两个已核对案例均适合检验收到提示后的读取。Galápagos S19 的航班更新发生在 14:20，17:35 通知明确要求复查航班，间隔 3 小时 15 分钟。旅行原生运行器不会因该 mutation 唤醒模型。陶艺团建 S5 的问答在 14:40 写入，15:00 有同主题 world 信号，间隔 20 分钟。两条均未计入独立多决策有效窗口。 [Galápagos 时间线](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/travel/galapagos_no_us_transit/event.yaml) · [原生运行器](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/travel/galapagos_no_us_transit/task.py) · [团建时间线](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/team_building/pottery_invoice_compliance_day/event.yaml)

本轮已实跑覆盖审计和现有 9 项测试。尚未运行 benchmark agent 重放、补证归因或策略效果对照。首轮的合成负结果已在网页中更正，预算 0.80 下的 −0.0049 是失配时双步相对单步的变化，双步低于不采集出现在预算 0.20 的失配条件。
