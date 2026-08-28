# 搜索评测 Agent：Agent-first 建设路线图

> 文档版本：v0.2
>
> 更新日期：2026-08-28
>
> 当前状态：全量商品网站基线已部署；Runtime/Trace 脚手架、exact-boost
> 优化器与 query-scoped 阶段检索三条切片已实现但尚未整合；阶段检索新切片
> 尚未部署；Owner 体验与阶段 2 评测内核继续进行
>
> 使用方式：一次只跨越一个验收门槛；Agent 的任何结论必须能够回到确定性实验结果。

## 1. 最终产品是什么

最终产品不是一个搜索页面，也不是若干排序算法的展示集合，而是一个能够自主寻找
Bad Case、提出受控优化策略、运行评测、比较实验结果，并把证据化建议交给人类
审批的**搜索评测与优化 Agent**。

用户可以提出如下任务：

```text
为什么 “wireless mouse” 的前 10 名质量不好？
BM25 和 Hybrid 哪个更适合型号 Query？
这次字段权重调整是否值得上线？
找出本次实验退化最严重的 Query，并给出下一步实验。
自己搜索当前结果，找出 Bad Case，并提出一个可验证的策略更新。
```

Agent 必须完成：

1. 理解目标并形成有限步骤的计划。
2. 根据任务选择工具，而不是执行写死的固定流程。
3. 观察工具结果，并据此决定继续、改道或停止。
4. 比较 Run、指标、延迟和具体排名变化。
5. 主动构思一个受控候选策略，运行 Harness 验证，而不是只给口头建议。
6. 用面板展示希望增加的策略、样本前后对比、总体效果、局部退化和证据。
7. 等待人类点击更新或拒绝；批准后自动写入版本化策略配置并触发后续验证。
8. 在标签不足、工具失败或证据冲突时明确表示无法确认。

证明“这是 Agent”的最低标准：

- 下一次工具调用由前一次观察结果决定。
- 同一任务在不同工具结果下可以走不同分支。
- Agent 能自己发现 Bad Case 并选择一个受控候选优化，而不是等人指定两个 Run。
- Agent 能在预算内停止，并能处理至少一种工具失败。
- 最终结论不是语言模型猜测，每个关键判断都有实验引用。

## 2. 三种 Harness 必须分开

```text
Agent Evaluation Harness             给 Agent 出题并判定任务是否完成
              ↓
Agent Runtime Harness                控制循环、工具、权限、预算、Trace
              ↓
Search Evaluation Agent              找 Bad Case → 提策略 → 跑实验 → 比较 → 等审批
              ↓
Search Evaluation Harness            运行 Ranker、计算指标、保存 Run
              ↓
BM25 / Vector / Hybrid / Rerank       真正被测的搜索策略
              ↓
Amazon ESCI 数据                      Query、商品、E/S/C/I 标签
```

三者职责：

| 组件 | 作用 | 不负责什么 |
|---|---|---|
| Search Evaluation Harness | 用固定数据运行搜索并计算 nDCG、MRR、Success、延迟 | 不替 Agent 做诊断 |
| Agent Runtime Harness | 控制 Agent 状态、工具权限、重试、预算、Trace 和停止条件 | 不判断搜索质量 |
| Agent Evaluation Harness | 测试 Agent 的任务成功率、证据质量、恢复能力和成本 | 不代替运行时控制 |

评测内核必须独立于 LLM。没有模型 API Key 时，搜索、指标计算、Run 对比和 Replay 仍然必须可运行。

## 3. 数据与评测边界

ESCI 标签覆盖的是给定 Query 下的已判断候选商品，并不是完整 Amazon 商品库的相关性真值。因此项目分成两条轨道。

### 主轨道：已标注候选集重排

- 对每个 Query 只重排其已标注候选商品。
- 主要指标：nDCG@5/10、MRR@10、Success@1/5。
- 用途：可靠比较 BM25、Vector、Hybrid 和 Rerank。

### 次轨道：封闭商品池检索

- 从标签边界明确的商品集合中执行召回。
- 指标：Recall@K、nDCG@K、MRR@K。
- 报告必须写明商品池构造方式，不能声称是全 Amazon Recall。

目前已经能够对 482,105 个 ESCI 已判断商品建立标题 BM25 索引并搜索，但这只是可运行的探索原型，不代表 Amazon 线上搜索，也尚未成为正式基线报告。

## 4. Agent-first 阶段总览

### 当前插入里程碑：网站全量商品基线

Owner 决定先在现有 `shawspace.cn` 搜索体验页搜索全部 1,814,924 个 ESCI
商品，亲自感受优化前结果，再决定优化方向。Codex 负责设计和实现持久化
倒排索引、API、日志、测试与部署。这个里程碑不改变 Agent-first 总路线，
也不会提前打开 500-Query dev 或 frozen test。

验收顺序：

1. ✅ 用锁定的全量商品 Parquet 构建 SQLite FTS5 基线索引。
2. ✅ 左侧“优化前”调用全量 API；右侧“优化后”继续显示暂未支持。
3. ✅ 验证英文、西班牙语、日语、商品 ID 和零结果 Query。
4. ✅ 记录索引大小、构建时间、查询延迟与已知缺陷。
5. ✅ 部署到现有网站并完成本机、公网 API 与浏览器技术验收。
6. ⏳ 由 Owner 亲自体验并把 Bad Case 作为后续优化输入。

边界：这条体验轨能说明“商品可被检索”，不能说明“对所有 Query 的 Recall
是多少”，因为 ESCI 没有给任意 Query 标注全库相关商品。

| 阶段 | 核心交付物 | Agent 体现 | 状态 |
|---|---|---|---|
| 0. 工程骨架与搜索 Smoke | 可重复启动的本地搜索后端 | 为工具化提供稳定接口 | **Completed (OpenSearch live check pending)** |
| 1. ESCI 数据与实验边界 | 可复现 train/dev/test、Manifest、数据报告 | 给 Agent 提供可信数据边界 | **Technical gate passed; learning check pending** |
| 2. Search Evaluation Harness | 指标、BM25 基线、Run 与对比报告 | Agent 可依赖的确定性工具层 | **In Progress** |
| 3. 最小搜索评测 Agent | 首个计划—工具—观察—报告闭环 | 第一次可见的真实 Agent 行为 | **In Progress: deterministic scaffold only** |
| 4. Agent Runtime Harness | 状态机、权限、预算、Trace、Replay | Agent 可控、可恢复、可复现 | **In Progress: local scaffold only** |
| 5. Agent Evaluation Harness | 黄金任务集和 Agent 成绩单 | 证明 Agent 不是偶然成功 | **Not Started** |
| 6. 搜索策略与诊断实验室 | Multi-field BM25、Vector、Hybrid、Rerank、Bad Case | Agent 获得更多可组合实验工具 | **In Progress: query-scoped multi-recall + RRF + coarse-rank slice** |
| 7. 诊断与优化 Agent | 自动发现 Bad Case、提出策略、运行受控实验并请求审批 | 完整的搜索评测与优化 Agent | **In Progress: exact-boost optimizer + stage diagnosis/ablation; separate from Runtime** |
| 8. Web Agent 工作台与交付 | Agent 工作台、审批面板、搜索对比页、部署与作品集 | 用户可观察计划、工具、证据、Replay 和策略审批 | **In Progress: stage-aware UI implemented locally, not deployed** |

与 v0.1 相比，Agent MVP 从原阶段 5 前移到阶段 3；Trace、Replay 和 Agent Eval 也前移。向量与 Rerank 不再是 Agent 出现之前的前置条件。

## 5. 各阶段执行与验收

### 阶段 0：工程骨架与搜索 Smoke

已完成：本地 BM25、精确余弦后端、统一 Backend 接口、FastAPI、测试与 CI。OpenSearch 适配器已经实现，Docker 可用后补实机验证。

验收证据：

- `docs/STAGE_0_REPORT.md`
- `docs/adr/001-search-backend.md`
- 固定 10 商品 Smoke Test

### 阶段 1：ESCI 数据与实验边界

已完成技术工作：

- English-US Task 1 共 601,354 条判断记录。
- 20,388 个 train Query、500 个 dev Query、8,956 个 frozen test Query。
- 20 Query smoke 是 dev 内固定视图，不是第四个 split。
- 数据契约、字段完整性、重复判断、Query 泄漏和文件哈希均有自动检查。

进入阶段 2 的 Owner 门槛：

- 能解释为什么 Query 不能跨 train/test。
- 能解释为什么未标注商品不能自动视为 Irrelevant。
- 能区分商品语料、相关性判断和正式 split。

验收证据：

- `docs/STAGE_1_REPORT.md`
- `docs/DATA_DICTIONARY.md`
- `data/manifests/esci-stage1.json`

### 阶段 2：Search Evaluation Harness 与正式基线

目标：先建立可信的“尺子”，再让 Agent 使用它。

执行：

1. 手写并测试 nDCG@5/10、MRR@10、Success@1/5。
2. 配置化 E/S/C/I 增益映射和相关阈值。
3. 定义统一 `Ranker` 输入输出契约。
4. 实现随机、关键词重叠、标题 BM25 三条基线。
5. 将全量标题 BM25 探索代码沉淀为可测试模块和 CLI。
6. 对固定 smoke/dev 运行候选集重排。
7. 为每次实验保存 Run Manifest：数据、代码、配置、指标和随机种子；动态延迟只进入诊断日志。
8. 实现 `compare_runs`，输出总体变化和逐 Query 排名 Diff。
9. 为 data、evaluation、ranking、backend 和 API 提供可独立过滤的结构化诊断日志；动态 Trace/耗时不得改变确定性 Run 身份。

当前安全进度：三条 Ranker 已接入统一 Harness，但只允许在固定 smoke
profile 上执行。Owner 数据边界检查点记录完成前，共享正式评测入口与 CLI
都在读取数据前拒绝 500-Query dev；frozen test 始终不进入 routine CLI。
第 8 项 `compare_runs` 已在 smoke 范围实现：它对照受信 Stage 1 Manifest
校验两个 Run，复算指标，并输出总体变化、逐 Query 指标变化和逐商品排名
Diff。阶段 2 仍未完成，因为 Owner 学习检查点、500-Query dev 正式基线与
Bad Case 判断尚未完成。

验收：

- 指标代码与手算样例一致。
- 同一配置重复运行得到相同结果。
- BM25 在 dev 上优于随机基线。
- 任一汇总指标都能下钻到 Query 和商品排名。
- `make eval-baseline` 生成机器可读 Run，`make compare-runs` 生成机器可读
  Diff 和人类可读报告。

本阶段结束时，搜索已经可评测，但还不是 Agent。

### 阶段 3：最小搜索评测 Agent

目标：尽早完成一个窄但真实的 Agent 垂直闭环。

第一批受控领域工具（只读证据工具 + 受限的 Run/比较产物创建）：

```text
inspect_query       查看 Query、候选商品、标签和字段
run_ranker          使用指定基线创建一次 Run
evaluate_run        读取单 Query 或批量指标
compare_runs        比较两个 Run 和排名变化
```

Agent 工具只接收受控 registry 中已验证的 Run ID，不接收任意文件路径。
Stage 2 的本地 Run 内容哈希用于完整性检查，不是生成者身份认证。

Agent 循环：

```text
用户目标
  → 形成计划
  → 选择工具
  → 观察结果
  → 根据证据继续、换工具或停止
  → 输出带 Run ID 的报告
```

首批任务：

- 解释某个 Query 的 BM25 排名。
- 找出 smoke 中退化最明显的 Query。
- 比较随机、词重叠和 BM25，并判断基线是否有效。
- 遇到缺失 Query 时说明证据不足，不虚构结果。

验收：

- 至少 10 个固定 Agent 任务产生结构化终态。
- 至少一个任务会根据中间结果走不同工具分支。
- 关键质量结论全部引用 Query 或 Run 证据。
- Agent 只能调用白名单工具，不能执行任意 Shell/Python。
- 从第一版开始保留最小 Trace：计划、工具、观察和终态。

本阶段是作品第一次可以明确展示“Agent”的节点。

当前进度（2026-08-28）：四个 smoke-only 领域工具、受信 Run registry 和
观察后分支的 `FakeBranchingPlanner` 已形成第一条确定性垂直切片。它用于
验证 Runtime 与证据链，不是真实 LLM Planner，也尚未满足“至少 10 个固定
Agent 任务”的完整验收，因此不能对外声称阶段 3 已完成。

### 阶段 4：Agent Runtime Harness、Trace 与 Replay

目标：控制 Agent，而不是只让它成功演示一次。

执行：

1. 定义有限状态：`planning → acting → observing → deciding → completed/failed`。
2. 为工具定义 Pydantic Schema、权限、超时和错误码。
3. 加入最大步数、Token/金额预算、有限重试和明确停止条件。
4. 将阶段 3 的最小 Trace 扩展为完整 Trace ID、调用参数、观察、耗时、错误和终态。
5. 保存数据、代码、模型、Prompt、工具和 Ranker 版本。
6. 实现离线 Replay，默认读取历史快照，不重新调用模型。
7. 注入超时、空结果、损坏输出和中途失败。

验收：

- 不存在无限循环或无上限重试。
- 任一失败都能定位到具体状态和工具调用。
- Replay 能还原历史工具结果和报告引用。
- 相同 Trace 的证据不会因外部模型变化而消失。

当前进度（2026-08-28）：有限状态、白名单能力、步数/调用/失败/大小预算、
结构化错误、Trace 与离线 Replay 已有本地脚手架。Trace 的无密钥 SHA-256
链用于发现损坏，不是数字签名；elapsed budget 只在本地动作之间协作检查，
不能终止一个永不返回的外部调用。真实模型接入前必须补可强制终止的 worker
deadline、模型/Prompt/Token/成本版本与更强来源认证。因此阶段 4 仍为进行中。

### 阶段 5：Agent Evaluation Harness

目标：证明 Agent 的任务完成能力，而不只评测搜索分数。

任务集至少覆盖：

- 正常诊断与 Run 对比。
- 信息不足与冲突证据。
- 工具超时、错误参数和空结果。
- test 泄漏诱导。
- 预算不足和必须提前停止的任务。

Agent 指标：

| 指标 | 含义 |
|---|---|
| Task Success | 是否完成用户目标 |
| Grounded Claim Rate | 关键结论中有可验证证据的比例 |
| Tool Selection Accuracy | 是否选择了适合当前状态的工具 |
| Recovery Rate | 工具失败后能否安全恢复或正确停止 |
| Budget Compliance | 是否遵守步数、时间和 Token 预算 |
| Replay Fidelity | 历史任务能否还原 |

验收：

- 固定 Agent Eval 集可以一条命令运行。
- 确定性规则优先，LLM Judge 不能作为唯一裁判。
- 报告同时给出成功率、失败类型、成本和延迟。
- 与固定脚本工作流比较，说明 Agent 的收益和额外成本。
- 零越权工具调用，零 frozen-test 调参。

### 阶段 6：搜索策略与 Bad Case 实验室

目标：把更强的搜索能力作为 Agent 可组合的实验工具，而不是孤立 Demo。

执行顺序：

1. Multi-field BM25：标题、品牌、描述、bullet、型号精确字段。
2. 词法规则：型号、连字符、同义词和必要的字段加权。
3. 向量排序与 Embedding 缓存。
4. BM25 + Vector 的 RRF Hybrid。
5. Top-N Cross-Encoder Rerank。
6. Bad Case Schema：数据、召回、排序、标签和评测配置问题。
7. Query 分群：型号、品牌、属性、否定词、长短 Query。
8. 将所有策略和诊断能力暴露为版本化工具。

当前切片（2026-08-28）：已在固定 20-Query、416 judged pair 的封闭池中
显式实现 title BM25、exact title 与 multi-field BM25 三路召回，RRF Top 20
融合和 title-BM25 Top 10 粗排。Fine rank 与最终 rerank 仍明确标记为
`not_implemented`。统一、保守和激进三个 RRF 候选均由 Harness 运行；只有
`title=1.0 / exact=1.0 / multi-field=0.1` 的保守候选通过 12 项 smoke 门禁。
这证明了阶段证据链与受控改道，不等于完整 Stage 6，也不解锁 dev/test。

详细证据见 `docs/STAGE_AWARE_RETRIEVAL_REPORT.md`，架构决策见
`docs/adr/004-stage-aware-retrieval-agent.md`。

验收：

- 四种策略使用同一数据和标签规则公平比较。
- 每次提升都有改善 Case、退化 Case、延迟和成本。
- 至少 30 个 Bad Case 经过人工复核并形成黄金诊断集。
- Agent 可以调用工具获得证据，但不能直接修改正式配置。

### 阶段 7：诊断与优化 Agent

目标：让 Agent 完成完整的搜索评测和受控优化任务，而不是只解释一份已有报告。

完整循环：

```text
自己搜索或抽样发现问题
→ 定位 Query 分群和 Bad Case
→ 提出可证伪优化假设
→ 选择受控策略实验
→ 运行并比较 Run
→ 检查总体提升、局部退化、延迟和成本
→ 生成审批面板
→ 人类点击更新或拒绝
→ 批准后自动写入版本化策略配置并触发验证
```

Agent 可以：

- 选择预定义 Ranker、字段权重和安全参数空间。
- 创建隔离实验 Run。
- 比较质量、延迟和成本。
- 给出需要人工审批的配置建议。
- 在批准后自动应用版本化策略配置，并启动后续 validation。

Agent 不可以：

- 修改 frozen test、相关性标签或历史 Run。
- 任意执行代码或访问未授权数据。
- 在没有人工点击批准前修改活跃策略、部署线上索引或跳过审批。
- 用无法追溯的常识替代实验结果。

验收：

- 从自然语言任务到证据报告能够端到端完成。
- Agent 能主动发现至少一个 Bad Case，并提出一个受控候选策略。
- 至少一个任务包含假设失败后的改道。
- 最终建议同时考虑总体指标、Query 分群、延迟和成本。
- 审批面板展示策略、样本前后结果、效果变化、局部风险和证据引用。
- Agent Evaluation Harness 能稳定复现成功与失败结论。

### 阶段 8：Web Agent 工作台与作品集交付

页面分为两类，避免把搜索 Demo 当成 Agent：

1. **搜索对比页**：只保留优化前/后的搜索框和结果，供用户感受差异。
2. **Agent 工作台**：输入诊断或优化任务，展示计划、工具调用、观察、Run 对比和最终建议。
3. **实验页**：查看指标、延迟、排名 Diff 和 Query 分群。
4. **Trace / Replay 页**：检查历史 Agent 路径和失败位置。
5. **批量评测页**：运行 Search Harness 或 Agent Eval Harness。
6. **策略审批面板**：展示 Agent 希望增加的策略、样本前后结果、效果变化、
   局部退化和证据，提供更新策略、拒绝策略和继续实验按钮。

验收：

- 用户无需命令行即可完成一次 Agent 诊断。
- 用户只需要审批策略取舍，不需要手动跑每个实验。
- 页面展示的指标与离线 Run 完全一致。
- 三分钟演示中能看见 Agent 根据工具结果改变下一步行动。
- 任一结论都能打开对应 Run、Query 或 Trace。
- 点击更新策略后，系统自动写入版本化策略配置并触发后续验证。
- README、架构图、评测报告、演示脚本和部署说明齐全。

## 6. Now / Next / Later

### Now

- 保持 Runtime/Trace 脚手架与确定性优化器的能力边界清晰，不能把两条切片
  描述成一次已经可 Replay 的完整优化 Agent Run。
- 用固定 smoke 集持续验证“诊断 → 多候选实验 → Harness → 七项门禁 →
  提案 → 服务端决策 → 下一轮 active 基线”。
- 工作台展示根因、候选配置、三项核心指标、七项门禁和逐 Query 前后结果；
  浏览器仍无审批权，active 仍不影响 `/catalog/search`。
- 关键知识随进度提供说明但不进行问答；500-Query dev 继续代码锁定。
- 用 query-scoped 固定边界持续验证“多路召回 → RRF → 粗排 → 12 项门禁”；
  uniform/aggressive 候选失败也必须保留为证据，不能只展示被选结果。
- 当前阶段检索响应只能生成 Owner-reviewable 证据，不创建审批决定、不更新
  strategy catalog、不改变 active config，也没有部署到线上工作台。

### Next

- 把诊断、候选搜索、实验和提案动作接入 Agent Runtime，使一次优化可以生成
  完整 Trace 并在观察变化或工具失败时改道。
- 建立至少 10 个固定 Agent 任务和 Agent Eval 判定，区分 Runtime 正确与
  Planner 任务完成质量。
- 实现有来源边界的 Query 构造器、分桶与更大已标注验证；不解锁 frozen test。
- 实现认证 Owner 审批、CSRF、审计身份、验证后生效和可验证回滚。
- 把 recall/fusion/coarse 的诊断、三个 RRF 候选实验和门禁结果接入 Runtime
  工具与 Trace；让“uniform 失败 → conservative 通过”的改道可 Replay。

### Later

- 纠错、Vector、语义/词法 Hybrid、fine rank、Cross-Encoder rerank 和业务
  重排等更多白名单策略；multi-field BM25/RRF 的 smoke 切片已先行实现。
- 在 worker deadline、调用/Token/费用预算和严格 DSL 下接入可选模型 Planner；
  模型只提假设，不计算指标或批准发布。
- Bad Case 黄金集、流量分桶、置信区间和线上灰度指标。

## 7. 关键学习路线

| 阶段 | Owner 必须掌握 |
|---|---|
| 1 | 数据泄漏、不完整标签、商品语料与判断集的区别 |
| 2 | DCG/nDCG、MRR、Success、离线评测边界 |
| 3 | Agent 与固定工作流、工具调用和证据约束 |
| 4 | 状态机、权限、超时、重试、预算、Trace、Replay |
| 5 | Agent 任务成功率、裁判可靠性和 Eval 污染 |
| 6 | Multi-field BM25、向量、RRF、Rerank、召回与精排 |
| 7 | 假设驱动实验、局部退化、自动化与人工审批边界 |
| 8 | API/前端边界、异步任务、策略审批和三分钟证据化讲解 |

完成状态记录在 `docs/LEARNING_CHECKPOINTS.md`。不要求 Owner 手写所有工程代码，但必须能解释会影响产品和实验判断的概念。

## 8. 范围优先级

### Must Have

- 可复现 ESCI 数据和候选集重排评测。
- Search Evaluation Harness 与正式 BM25 基线。
- 最小 Agent 计划—工具—观察闭环。
- Agent Runtime Harness、Trace、Replay。
- Agent Evaluation Harness 和固定任务集。
- BM25、Vector、Hybrid、Rerank 对比。
- 主动 Bad Case 发现、策略提案、Harness 比较和人工审批后的自动更新。
- 能展示 Agent 行为和策略审批的 Web 工作台。

### Should Have

- 封闭语料检索评测。
- Query 分群、Bad Case 黄金集和回归门禁。
- 延迟、成本和稳定性指标。
- 人工审批后的实验配置提案与自动应用。

### Could Have

- 100–200 条自建中文测试集。
- 云端多人共享。
- 更多 Embedding/Rerank 模型适配器。
- 真实电商 API 的只读演示模式。

### 本周期明确不做

- 抓取 Amazon、淘宝、TikTok Shop 等商城网页。
- 声称复现或优化了 Amazon 线上搜索。
- 个性化、广告排序和推荐系统。
- 未经人工审批自动修改生产搜索配置或自动部署。
- 训练大型模型或让 Agent 任意执行 Shell/Python。

## 9. 风险与门禁

| 风险 | 应对 |
|---|---|
| 项目最后只剩搜索 Demo | 阶段 3 提前交付最小 Agent；Web 必须展示计划和工具 Trace |
| 固定工作流冒充 Agent | 验收条件要求基于观察分支、失败恢复和预算停止 |
| Agent 语言流畅但结论无证据 | 关键判断强制引用 Query、Run 或 Trace |
| 指标或标签边界错误 | 先完成确定性 Search Harness，再允许 Agent 使用 |
| LLM Judge 自说自话 | 确定性断言优先，人工黄金集校准，Judge 不能单独裁决 |
| test 被反复用于优化 | frozen test 权限隔离；Agent 工具默认只允许 smoke/dev |
| Agent 失控或循环 | 白名单工具、最大步数、预算、有限重试和终态 |
| 搜索策略扩张拖慢 Agent | Vector/Rerank 后置；BM25 足够支持 Agent MVP |

以下任一情况不进入下一阶段：

- 指标没有手算单元测试。
- Run 无法复现或没有版本记录。
- Agent 关键结论无法追溯到证据。
- Agent 可以越过权限、预算或 frozen test 门禁。
- 失败被吞掉，或 Trace 无法定位失败步骤。

## 10. 当前唯一下一步

把已经实现的 exact-boost 优化器与 stage-aware retrieval 动作统一封装为
Runtime 工具和观察驱动 Planner：一次 Agent Run 必须能在 Trace 中证明它
为何判断问题发生在 recall/fusion/coarse，为什么尝试 uniform、conservative
和 aggressive 候选，为什么因 12 项门禁失败而改道，以及为什么只生成
Owner-reviewable 提案而不自行激活。随后用固定 Agent 任务集评测这条完整链路。

该工作不依赖也不解锁 500-Query dev。更大验证、真实模型 Planner、浏览器
审批和线上生效仍分别需要数据学习证据、安全机制与 Owner 明确决策；“继续”
不会自动跨越这些门禁。
