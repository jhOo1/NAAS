# Need-Aware Active Sensing（NAAS）

基于[指定网页对话的最后一轮方向 B](https://chatgpt.com/c/6afd60fd-dfc8-83ec-a099-939aeaa60eeb)。方法沿用该轮的名称 Task-Dependency-Aware Active Sensing（TDAAS）。

**当前研究问题**　长期个人助理收到更新线索后，如何在有限预算下读到足以改变当前行动的证据？原多决策预算调度主张在 VibeLifeBench 的冻结覆盖审计中未达启动门槛。

- [Qwen3.5-27B 单次自由选读基线与逐例对照](docs/free-read-20261118.md)
- [Qwen3.5-27B 三臂模型级补证结果与复现](docs/model-pilot-20261118.md)
- [第三轮研究设计、三项原生评分复核与补证实验协议](docs/evidence-replay.md)
- [方向 B 的方法设计、论文定位与证伪实验](docs/research-design.md)
- [网页评审后冻结的首轮协议与覆盖审计](docs/round-01.md)
- [VibeLifeBench 256 个原生候选逐条核验与门槛判定](docs/native-coverage.md)
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

覆盖审计使用 PyYAML。数据版本、复现命令和原生场景核对记录见[首轮报告](docs/round-01.md#首次覆盖审计结果)。
