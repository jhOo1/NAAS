# Need-Aware Active Sensing（NAAS）

面向主动个人助理的研究仓库。用户提供的截图确认了 [网页原案](https://chatgpt.com/c/6afd60fd-dfc8-83ec-a099-939aeaa60eeb) 的主线为 **Active Sensing × VibeLifeBench**，候选方案可以完全 training-free。原聊正文和后续讨论仍待读取。以下是围绕截图与公开资料独立提出的 v0。

**研究问题**　在长期任务中，世界会悄悄变化。固定的观察预算下，能否用当前承诺、已知时限和证据的过期风险决定何时重查、重查什么，从而在合适的时间修订计划？

- [研究设计和近邻工作](docs/research-design.md)
- [VibeLifeBench 接入审查与评估协议](docs/vibelifebench.md)
- [合成诊断结果与运行限制](docs/smoke-results.md)

## 本地复现

需要 Python 3.9+，只使用标准库。在仓库根目录运行：

```sh
python3 -m unittest discover -s tests -v
python3 smoke.py
python3 smoke.py --budget 0.2 0.45 0.8 1.2 --json
```

程序精确枚举构造情境和可观察结果，比较相同证据预算上限下的停止、三种启发式采集、群体平均偏好的采集策略和两种前瞻深度。`cost` 是模拟采集支出，`net` 是建议效用减去采集支出。结果用于核对机制与计算，外部效度需在独立交互任务中验证。
