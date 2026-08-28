# 必学知识检查点

> 目的：只提醒会影响产品判断、实验可信度、系统可靠性或项目讲解能力的关键知识。  
> 规则：进入相关阶段或作出相关决策前提醒，不等开发完成后再补课。

## 提醒格式

每次提醒使用以下结构：

```text
【必学知识提醒】
当前知识：
为什么现在必须理解：
最低掌握范围：
一个具体例子：
验证证据（如已有）：
```

满足以下任一条件时，视为“必须学习”：

1. 不理解会导致错误选择产品或技术方案。
2. 不理解会误读指标、实验结果或 Bad Case。
3. 不理解会让系统出现不可发现的稳定性或安全风险。
4. 这是项目面试讲解中的核心能力，不能只说“代码是 AI 写的”。

以下内容通常不主动打断：框架样板代码、普通命令语法、依赖安装、
CSS 细节、重复性数据搬运和可以直接查文档的 API 参数。

## 阶段知识地图

| 阶段 | 必学知识 | 最低掌握标准 | 状态 |
|---|---|---|---|
| 0. 工程骨架 | 可复现环境、搜索索引、BM25 与向量搜索的区别 | 能解释为什么先做 Smoke Test，以及两种检索各自擅长什么 | **Completed** |
| 1. ESCI 数据 | ESCI 四类标签、Query 级切分、数据泄漏、不完整标注 | 能解释为什么同一 Query 不能跨数据集，以及为什么不能直接宣称全库 Recall | **Pending** |
| 2. Search Evaluation Harness | DCG/nDCG、MRR、Recall、Success@K、离线评测 | 能手算一个小例子，并判断不同指标分别奖励什么 | **Pending** |
| 3. 最小 Agent | Agent 与固定工作流、Tool Schema、观察后分支、证据约束 | 能解释系统为什么是 Agent，并指出一次基于观察的决策 | **Pending** |
| 4. Agent Runtime Harness | 状态机、权限、Trace、Replay、超时、重试、预算、停止条件 | 能解释一次失败如何定位、复现和安全恢复 | **Pending** |
| 5. Agent Evaluation Harness | 任务成功率、证据率、恢复率、裁判可靠性、Eval 污染 | 能说明如何证明 Agent 不是偶然成功或语言流畅 | **Pending** |
| 6. 搜索策略与诊断 | Multi-field BM25、Embedding、RRF、Cross-Encoder、Bad Case | 能说明各策略顺序、代价以及如何区分数据/召回/排序问题 | **Pending** |
| 7. 诊断与优化 Agent | 假设驱动实验、局部退化、自动化边界、人工审批 | 能判断 Agent 哪些实验可自动执行、哪些变更必须审批 | **Pending** |
| 8. 产品化 | API/前端边界、异步任务、指标披露、演示证据链 | 能独立完成三分钟项目讲解，并打开证据支撑结论 | **Pending** |

## 当前插入知识：全量索引与评测标签不是一回事

【必学知识提醒】

- 当前知识：Parquet 是离线商品数据文件；倒排索引是面向在线查询的检索结构。
- 为什么现在必须理解：否则会误以为网页每次搜索都扫描 181 万商品，或把“全库能搜”误说成“全库质量已评测”。
- 最低掌握范围：能解释建索引为什么把一次性离线成本换成低延迟在线查询；能解释 ESCI 对任意 Query 没有全库完整标签。
- 具体例子：扫描全部 1,814,924 行找 `wireless mouse`，与从倒排表直接读取
  `wireless`、`mouse` 的 posting list 并求交集，前者把全部工作留到每次
  查询，后者把主要工作提前到一次性建索引。
- 验证证据：2026-08-28 已随开发进度提供说明；Owner 要求后续不再以问答
  形式打断执行，因此不能把阅读或“继续”记作掌握证据。
- 状态：**Explanation delivered; independent verification pending**

## 当前插入知识：Agent 的观察后分支

【必学知识提醒】

- 当前知识：Agent 不是一次性答案生成器，也不是固定脚本；它必须根据工具
  观察结果决定下一步。
- 为什么现在必须理解：否则会把当前 deterministic scaffold 误认为完整 LLM
  Agent，或把固定流程误认为 Agent 能力。
- 最低掌握范围：能看懂 `任务 -> Planner -> Runtime -> 工具 -> Observation
  -> 再决策 -> 终态报告` 这个闭环；能指出一次分支条件。
- 具体例子：如果 `compare_runs` 发现候选 Run 有 Query 退化，当前 Planner
  会继续调用 `inspect_query` 下钻最严重的退化 Query；如果整体提升且没有
  退化，它可以直接 `accept`。
- 验证证据：2026-08-28 已新增 `docs/AGENT_FLOW.md` 作为视觉化学习地图；
  Owner 要求不再以问答中断执行，因此不能把阅读该图记作掌握证据。
- 状态：**Explanation delivered; independent verification pending**

## 检查点记录

完成一个检查点后，在这里追加简短记录：

| 日期 | 阶段 | 知识 | 验证方式 | 结果 |
|---|---|---|---|---|
| 2026-08-26 | 0. 工程骨架 | BM25 与向量搜索的区别 | Owner 口头解释精确型号 Query 的检索取舍 | **Completed** |
| 2026-08-28 | 协作方式 | 关键知识继续随进度讲解，但不再用问答中断开发 | Owner 明确要求“不要问答了。继续”；这是一项流程决定，不是知识掌握证明 | **Adopted; learning gates unchanged** |

状态只能在完成验证后从 `Pending` 更新为 `Completed`；仅仅阅读说明不算完成。

## 当前待补充的独立验证证据

### Stage 1：数据边界

- 阶段：1. ESCI 数据
- 提醒日期：2026-08-26
- 最低证据：Owner 日后能用自己的话解释 Query 泄漏为什么会夸大泛化能力，
  以及未标注商品为什么既不能自动视为 Irrelevant，也不能支撑全库 Recall。
- 状态：**Explanation delivered; independent verification pending**

### Stage 2：指标与局部退化

- 阶段：2. Search Evaluation Harness
- 提醒日期：2026-08-27
- 证据：`comparison-5c59968c1cd7` 中 BM25 相对 random 的平均
  nDCG@10 提升 `0.173312`，但 20 个 Query 中仍有 5 个退化；Query
  `15281` 的 nDCG@10 下降 `0.322992`。`comparison-dc727a4e03ca` 中
  BM25 相对 overlap 的 nDCG@10 更高，但 MRR@10 与 Success@1 更低。
- 最低证据：Owner 日后能说明平均 nDCG@10 提升为什么可能掩盖局部退化；
  能依据产品目标在 nDCG、MRR、Success 之间作选择；能解释一个在所有方案
  上恒定为 1.0 的指标为什么没有区分度。
- 状态：**Explanation delivered; independent verification pending**

执行保护：当前 Stage 2 共享正式评测入口、CLI 与 Agent 工具都只允许
smoke。500-Query dev 继续代码锁定；日后记录到独立理解证据并作出明确
解锁决定后才通过代码变更开放。“继续执行”、阅读说明或允许实现均不视为
知识验证。
