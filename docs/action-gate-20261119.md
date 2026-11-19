# PM-Bench v9 读后行动变更核验

沿用 `b21fbd2` 的 42 个配对及其原生历史、动作菜单、通道、实际回包和种子。保留原来的 B 无新增回包重答和 C 有回包重答。冻结提示和判据后，用 `distill/utils.py` 的 Qwen3.5-27B 对 B→C 的拟新增及拟移除动作各作核验，同一配对只调用一次，共同动作直接保留。模型只见原本可见的历史、实际回包和匿名动作编号。原生事件 ID、隐藏时间、到期集合和评分留在离线复核中。

核验逐项回答是否接受变更及可见理由。新增动作需要与具体对象、当前条件及最新指令相符。移除动作需要有失效、替代或冲突依据。解析失败时复用 C。评分沿用原生执行器，只重放到该步。附加零 API 对照取 B、C 动作并集，检查保留收益是否只靠合并即可得到。新模型调用只针对 B、C 动作集合不同的配对。

**预定通过条件**为相对 C 至少减少 3 次官方误报，非到期动作不增加，逐项保住 C 的 22 个目标命中和 30 个其他命中，并至少恢复 1 个 C 丢掉的 B 正确其他动作。此标准用于决定该机制是否值得复验。结果限同一个已暴露合成周，择时仍暂停。

## 复现

```sh
python3 run_pmbench_action_gate.py /path/to/PMBench \
  runs/pmbench-timing-20261119.json runs/pmbench-channel-lock-20261119.json \
  runs/pmbench-action-reanswer-20261119.json \
  --api-utils /path/to/distill/utils.py --output runs/pmbench-action-gate-20261119.json
```

中断时加 `--resume` 复用已保存的响应。用相同输入和输出路径、将模型参数换为 `--verify-only`，可在离线重建全部回包和计分。
