# 方向 B · VibeLifeBench 原生覆盖核验

**启动门槛未达到。** 逐条核验冻结池的 256 个候选，覆盖 20 个 Terrarium 任务。确认了 1 个窗口有两个独立决策共享同一次读取。原生不加筛选的列表在同一次调用内也返回了该窗口的竞争证据，因此按读取次数口径，严格计入 **0 个窗口、0 个任务**。另外 4 个边界案例保留待复核。即使把共享读取案例和这 4 个边界案例全部计入，也只有 **5 个窗口、5 个任务**，低于 10 个窗口、5 个任务的启动门槛。同预算强基线对照尚未启动。

## 逐条核验

固定 [VibeLifeBench e557448](https://github.com/evolvent-ai/VibeLifeBench/tree/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set)，沿用[首轮协议](round-01.md)。从 263 条有后续同阶段声明信号的 mutation 出发，按任务、阶段和信号去重为 256 条。逐条对齐原生派发逻辑、唤醒时已见内容、mutation 对可读状态的影响，以及阶段和后续相关评分项。办公装修任务在下一条 mutation 或阶段结束前合并相邻的可见消息，审查时以合并后的可见内容为准。阶段里的多条评分要求可以对应同一个决定。复合评分也可能覆盖两个决定，但需要逐项检查它是否能判断两个结果。 [办公装修原生运行器](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/renovation/office_fitout_15d/task.py#L440)

每条候选只登记一个主要判定理由。[逐条台账](../audits/vibelife-native-256.jsonl)包含信号时间、原生可见批次、mutation、评分项和理由。

| 判定 | 候选数 |
| --- | ---: |
| 只支持一个决策或一个下游结果 | 134 |
| 关键证据已在可见消息中 | 68 |
| 证据分在不同读取，缺少一次读取同时改变两项决策的依据 | 40 |
| 设置、记录或通用提醒，缺少可核验的决策影响 | 9 |
| 有共享读取，原生不筛选的一次调用消除读取次数上的取舍 | 1 |
| 边界案例，现有证据不足以计入 | 4 |
| 严格通过 | 0 |
| 总计 | 256 |

## 决定门槛的案例

**#116 工厂参访 S20。** 07:50 写入现场健康异常、语言支持请求和签到人数。08:00 的原生唤醒只说签到开始、现场陆续有反馈。按 `source=onsite_desk` 读取同时返回健康异常与语言支持，分别用于安置升级和给出可阅读提示。签到与餐数信息可以通过 `source=onsite_checkin` 读取。原生阶段的 `s20_onsite_adjustment` 对安置、升级和语言支持均有内容要求，最终汇成一项分数。重放原生 SQL 和通知服务后，默认不传 `source` 的一次调用返回全部三条记录。在相同读取次数下，调度器没有必要为这三条证据二选一。更宽响应是否增加实际模型成本还有待测量。 [事件与写入](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/team_building/factory_visit_safety_day/event.yaml#L592) · [原生工具](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/servers/notification_hub_mock/src/notification_hub_mock/tools/notification_tools.py#L13) · [S20 评分](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/team_building/factory_visit_safety_day/rubrics/stage_20.py#L4)

| 候选 | 能确认的证据与缺口 |
| --- | --- |
| #078 跨城租房 S17 | 原生分别验候选池和看房前日历草稿。缺少新房源读数会改变日历选择的证据。 [S17 评分](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/rental/cross_city_remote_viewing_rental/rubrics/stage_17.py) |
| #139 Galápagos S8 | 火山灰可能同时影响线路和住宿。本阶段评分验证天气风险与路线复查，住宿另一次决策对同一证据的依赖未得到原生评分支持。 [S8 评分](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/travel/galapagos_no_us_transit/rubrics/stage_8.py) |
| #167 ADU 改造 S18 | 验收失败可能影响整改选择和是否出租。原生验故障持久化及禁止挂牌，缺少对可变整改方案的独立判据。 [S18 评分](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/renovation/garage_adu_rental_conversion_25d/rubrics/stage_18.py) |
| #187 办公装修 D14 | 实时状态可能同时改变入驻和尾款决定。提醒、承包商邮件、业主提问合并为同一次模型调用。实际判据接受仅写「暂缓尾款，punch list 尚未关闭。」的回答，因而无法靠这一判据核验独立的入驻建议。 [D14 事件](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/renovation/office_fitout_15d/event.yaml#L1190) · [D14 评分](https://github.com/evolvent-ai/VibeLifeBench/blob/e55744876a4928fa36772f64952bbe3581ed16ca/eval_set/renovation/office_fitout_15d/rubrics/stage_14.py#L48) |

办公装修 D13 的联合结果通知已给出消防通过、强电附条件、D+30 及费用，先于后台记录更新。Galápagos S12 需要两封邮件的正文，原生 `get_emails` 的列表不返回正文，`read_email` 按单封读取。这两类排除原因分别约束「提示后还有未知事实」和「一次读取能否共用」。

这是一轮单人源码核验。两项组件重放分别验证原生通知后端的读取结果、原生复合评分判据的行为。当前数据不提供模型失分频率、补证能否修复失分或调度策略收益，也不估计 256 条候选之外的覆盖率。

## 待联合评审的两条路径

1. **优先验证原生的跨来源核验问题。** 研究收到提示后，如何选中能确认关键事实的来源或具体记录。用 VibeLifeBench 中提示后仍需读取的原生窗口做小规模取样，先核对 Galápagos S19 航班更新等案例。用 SentinelBench 的同页等待、详情与 No-op 做能力对照。同模型全局规划器拥有相同等待、共享缓存和可读来源。先做同执行器的原轨迹、当时可读关键证据、同成本无关证据三路归因。如果关键证据不能稳定恢复独立评分项，停止扩大策略实验。待评审确定具体样本和停止标准。
2. **保留多决策主张，构建明确标注的扩展。** 从 #116 和 #187 派生配对情景。保持提示、时序、候选来源及预算可比，只改变证据共享、独立或过期条件，分别评分两个决定。全局规划器与一次宽读加共享缓存都是必测对照，所有组看到自己的读结果。只有在宽读无法用同预算满足所有目标、且人工依赖调度仍有增益时，才值得投入自动依赖抽取。扩展结果作为新实验单独报告。

## 复现

使用 Python 3.9+ 和 PyYAML，从仓库根目录执行。

```sh
python3 audit_coverage.py --vibelife /path/to/VibeLifeBench --sentinel /path/to/sentinel_environments --adjudications audits/vibelife-native-256.jsonl
python3 probe_native_coverage.py /path/to/VibeLifeBench
python3 -m unittest discover -s tests -v
```
