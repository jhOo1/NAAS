# Need-Aware Active Sensing（NAAS）

基于[指定网页对话的最后一轮方向 B](https://chatgpt.com/c/6afd60fd-dfc8-83ec-a099-939aeaa60eeb)。方法沿用该轮的名称 Task-Dependency-Aware Active Sensing（TDAAS）。

**研究问题**　在有限观察预算下，个人助理如何根据未完成任务的证据依赖，决定下一次何时查、查哪一个来源，以及查完后哪些任务需要更新？

- [方向 B 的方法设计、论文定位与证伪实验](docs/research-design.md)
- [SentinelBench 首轮实验协议](docs/sentinelbench.md)
- [VibeLifeBench 长期任务评估协议](docs/vibelifebench.md)
- [早期局部采集诊断](docs/smoke-results.md)

## 本地复现

需要 Python 3.9+，只使用标准库。

```sh
python3 direction_b_demo.py
python3 -m unittest discover -s tests -v
python3 smoke.py
```

方向 B 的合成诊断比较一次读取应优先服务单个决策还是多个决策，并检查错误依赖能否反转收益。它使用人为设定的变化时刻和效用，正式 benchmark 实验尚待接入。
