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

## 当前插入知识：Runtime Harness 与 Search Harness 分工不同

【必学知识提醒】

- 当前知识：Agent Runtime Harness 控制“Agent 能做什么、做几步、失败如何
  重试、怎样留下 Trace”；Search Evaluation Harness 负责“搜索方案的指标
  是多少、候选是否通过相关性门禁”。Trace/Replay 证明行为链可追溯，不会
  自动证明搜索质量更好。
- 为什么现在必须理解：工作台现在同时展示 Agent 动作和 12 项搜索门禁；
  如果把两者混成同一个 Harness，就会把“Agent 按流程运行成功”误说成
  “搜索策略已经可上线”，或把“指标通过”误说成“Agent 没有越权”。
- 最低掌握范围：能把一次运行拆成两层：Runtime 调度并约束工具，搜索 Harness
  生成 Run/Comparison/指标；能说明 Replay 只读复核历史行为，不重新搜索。
- 具体例子：本轮 Runtime 先调用基线诊断工具，再根据 uniform 的七项失败
  门禁调用 conservative；conservative 通过后仍执行一次 bounded aggressive
  probe，后者失败两项门禁，因此终态选择 conservative。12 项门禁由 Search
  Harness 计算；调用顺序、预算、权限和 Trace 由 Runtime Harness 保证。
- 失败边界：正常路径创建 4 个 Run，预算最多 5 个，因此整次任务只有 1 次
  全局重试额度；第二个独立工具故障会安全停止，而不是继续消耗资源。
- 验证证据：`docs/adr/005-stage-retrieval-runtime-trace.md`、本地真实 Trace
  与网页只读时间线；2026-08-28 已随实现进度直接说明，不能把“继续”记作
  Owner 已独立掌握。
- 状态：**Explanation delivered; independent verification pending**

## 当前插入知识：优化 Agent 与人工审批边界

【必学知识提醒】

- 当前知识：Agent 可以自动发现 Bad Case、提出候选策略并跑 Harness，但不能
  在没有人类批准时修改活跃搜索策略。
- 为什么现在必须理解：这是产品可信度和安全边界。否则 Agent 可能把“平均
  指标提升但局部退化”的策略自动上线，破坏用户体验。
- 最低掌握范围：能区分三步：候选实验自动化、证据面板人工审批、批准后
  自动写入版本化策略配置。
- 具体例子：Agent 发现 `wireless mouse` 一类 Query 的品牌或属性匹配不好，
  提出提高 `brand/title` 权重，Harness 显示 nDCG@10 提升但某些 Query
  退化；面板必须把提升和退化都展示出来，由 Owner 点击更新或拒绝。
- 验证证据：2026-08-28 已新增 `docs/AGENT_OPTIMIZATION_WORKFLOW.md`；这
  是产品方向说明，不等于 Owner 已完成全部自动化边界学习验证。
- 状态：**Explanation delivered; independent verification pending**

## 当前插入知识：模型不是 Harness，也不能拿走发布权

【必学知识提醒】

- 当前知识：模型适合归纳 Bad Case 和提出可证伪假设；真正的指标必须由
  确定性 Harness 重算，发布仍受固定门禁与人工审批控制。
- 为什么现在必须理解：否则容易把“模型说这个策略更好”误当实验结果，或
  把 API Key、评测标签和生产修改权限都交给一个不可验证的输出端。
- 最低掌握范围：能区分三层：模型/规则提出候选，Harness 产生事实，Owner
  决定业务取舍；能说明密钥为什么只能保存在服务端 Secret 中。
- 具体例子：本轮 coverage 候选的平均 nDCG@10 增益更高，但 Query 回归比例
  超过工程默认门禁，因此优化器选择增益较小的 conservative 候选。这个选择
  来自可复算指标和门禁，不是模型偏好。
- 验证证据：2026-08-28 已实现 `strategy_search.py` 并新增
  `docs/AGENT_OPTIMIZATION_STRATEGY.md`；Owner 尚未独立选择正式门禁阈值或
  模型预算，因此不能把这些工程默认值写成 Owner 原创政策。
- 状态：**Explanation delivered; independent verification pending**

## 当前插入知识：Recall 提升不等于最终排序提升

【必学知识提醒】

- 当前知识：多路召回、融合和粗排是三个独立阶段。Recall union 回答“至少
  一条路有没有找到”，RRF 回答“合并后是否保留”，粗排回答“Top 10 是否
  仍把相关商品排在合适位置”。
- 为什么现在必须理解：否则会把“multi-field 找到了更多相关商品”直接说成
  “用户结果变好了”。新通道也可能把噪声带入 RRF，挤掉原有高质量结果。
- 最低掌握范围：能区分 recall miss、fusion drop 和 coarse-rank drop；能说明
  为什么每增加一条召回路都必须继续检查下游 nDCG/MRR 和 Query 退化。
- 具体例子：本次 conservative 候选把 recall-union coverage 从
  `0.8114716964` 提到 `0.8487412085`，但 coarse Recall@10 仍是
  `0.5296930653`。这表示新增相关商品进入过召回集合，却不保证都进入最终
  Top 10。uniform 候选具有同样的 union coverage，却因下游退化失败七项门禁。
- 验证证据：`docs/STAGE_AWARE_RETRIEVAL_REPORT.md`；当前只是说明已提供，
  不能记作 Owner 已独立掌握。
- 状态：**Explanation delivered; independent verification pending**

## 当前插入知识：宏平均与微观商品贡献回答不同问题

【必学知识提醒】

- 当前知识：mean Query metric 先对每个 Query 算分再平均，让每个 Query 权重
  相同；unique relevant contribution 是 Query-product 层面的微观计数，商品
  多的 Query 可能贡献更多项。
- 为什么现在必须理解：Agent 面板同时展示“平均覆盖率”和“新通道独有相关
  商品”时，两者不能混成同一百分比。一个策略可能多找回许多商品，但只集中
  在少量 Query；也可能每个 Query 小幅改善而微观计数不大。
- 最低掌握范围：能说明宏平均适合看 Query 体验是否广泛改善，微观贡献适合
  证明新通道是否真的提供增量；发布判断必须同时下钻 Query 分布。
- 具体例子：本次 `+3.73` percentage points 是 20 个 Query 的 mean judged
  recall-union coverage 变化；新通道独有相关项则是逐 Query-product 的计数。
  两者的分母不同，不能写成同一个“提升率”。
- 验证证据：stage-aware comparison 记录两类统计及逐 Query Diff；学习验证
  仍待 Owner 日后用自己的话复述。
- 状态：**Explanation delivered; independent verification pending**

## 当前插入知识：Agent Eval 必须有独立 Oracle

【必学知识提醒】

- 当前知识：Search Evaluation Harness 判“搜索结果好不好”；Agent Evaluation
  Harness 判“Agent 有没有正确完成任务”。Agent Eval 的 Oracle 必须独立于被测
  Planner，不能调用 Planner 自己的决策函数生成标准答案。
- 为什么现在必须理解：如果 Agent 既作答又出答案，即使 12/12 也可能只是同一
  错误被复制两次。相反，在没有安全提升时正确停止，虽然没有产出优化策略，仍可
  算 Agent 任务成功；碰巧取得高搜索分但越权、伪造证据或超预算则应判失败。
- 最低掌握范围：能区分“搜索质量分数”和“Agent 任务分数”；能说明静态 Oracle
  为什么要检查终态、工具顺序、证据、预算、Replay 和副作用，而不是只看最终文案。
- 具体例子：`eval-no-safe-candidate` 的正确答案是停止并报告
  `no_safe_improvement`，不是为了让指标看起来上涨而强行选择候选；
  `eval-trace-tamper-rejected` 即使原搜索结果不变，也必须拒绝被改写的 Trace。
- Query 构造边界：相邻字母调换和词序反转可以发现拼写/顺序敏感 Bad Case，
  但它们没有 Amazon ESCI 新标签，不能继承源 Query 的判断并计算正式 nDCG/MRR。
- 验证证据：2026-08-29 已实现 12 个固定任务、静态 Oracle、固定工作流对照和
  smoke-only Query 构造器；实现测试 12/12，其中 8 项使用生产 Planner，4 项是
  Harness stimulus 对 Runtime 的围栏测试。Owner 本轮“执行”是推进授权，不是
  独立理解证据，也不解锁 500-Query dev 或 frozen test。
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
