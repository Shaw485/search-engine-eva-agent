# 决策与贡献归因台账

> 建立日期：2026-08-27  
> 目的：如实区分项目 Owner 与 Codex 的贡献，供项目复盘、作品集披露和面试使用。  
> 原则：宁可少认领，也不把“批准方案”写成“原创方案”。

## 1. 归因口径

一项重要工作拆成五种角色，不能只写一个笼统的“完成者”：

| 角色 | 含义 | 例子 |
|---|---|---|
| 需求提出者 | 首先指出要解决的问题或约束的人 | Owner 要求最终产品必须体现 Agent |
| 方案提出者 | 给出具体可选方案或参数的人 | Codex 提出 ESCI 增益映射 |
| 决策者 | 选择、批准或否决方案的人 | Owner 回复“可以”并采用该映射 |
| 实现者 | 编写代码、测试、文档或部署的人 | Codex 实现指标与 BM25 基线 |
| 验证者 | 审查结果、运行验收或提供判断的人 | 自动测试由 Codex 运行；产品体验由 Owner 判断 |

补充规则：

- **Owner 主动提出并拍板**：可以称为 Owner-originated decision。
- **Codex 提案、Owner 批准**：属于 Owner 的最终决策权，但方案来源仍是 Codex。
- **共同迭代**：分别写清 Owner 给出的方向和 Codex 完成的拆解，不能只写“共同完成”。
- **允许安装、部署、提交**：这是授权，不是技术方案原创。
- **Git author/committer**：只表示提交身份，不证明谁构思或编写了内容。
- **提问和质疑**：属于 Review 贡献；只有确实改变了选择时才升级为决策。

## 2. 已发生的关键决策

以下为截至 2026-08-27 的回填。对没有明确对话证据的事项，不推定为 Owner 决策。

| ID | 结论 | 需求/问题来源 | 方案来源 | 最终决策 | 实现与验证 | 归因强度 |
|---|---|---|---|---|---|---|
| D-001 | 最终产品是可观察、可证明的“搜索评测 Agent”，而不只是搜索 Demo 或算法陈列 | **Owner 主动提出**：“最终想做的是一个搜索评测 agent，要能体现 agent” | Owner 定义产品方向；Codex 将其拆成 Agent-first 阶段 | **Owner** | Codex 重写路线图；见 `ROADMAP.md`、commit `33fbe00` | Owner-originated |
| D-002 | Owner 要亲手理解并走一遍搜索系统，Codex 提供可运行代码与引导 | **Owner 主动提出**：“我要手撕一遍搜索系统”“帮我写好代码，我去粘贴到终端看看” | Owner 定义协作方式 | **Owner** | Codex 编码、讲解和设置学习检查点；Owner 已完成 Stage 0 验证，Stage 1/2 仍待验证 | Owner-originated |
| D-003 | 遇到必须掌握的关键知识时必须及时提醒，并在误解会破坏实验时验证理解 | **Owner 主动提出** | Owner 定义原则；Codex 设计提醒格式和检查点 | **Owner** | Codex 写入 `AGENTS.md` 与 `docs/LEARNING_CHECKPOINTS.md` | Owner-originated + Codex operationalization |
| D-004 | 作品集增加搜索评测 Agent 入口和关键信息；搜索体验页只保留两套搜索框和结果区：优化前可用，优化后暂不可用，并删除原理介绍 | **Owner 主动提出**并根据实际页面给出明确 UX 反馈 | Owner | **Owner** | Codex 修改 API、页面并部署；Owner 负责产品体验验收 | Owner-originated |
| D-005 | 路线图提前交付最小 Agent：Search Evaluation Harness 之后先做 Agent MVP，再补完整 Runtime/Eval Harness 和更强 Ranker | Owner 要求重新分路线图并突出 Agent | **Codex 提出具体阶段顺序**，Owner 提供优先级方向 | Owner 决定产品优先级；具体阶段拆解为 Codex 设计，目前被项目采用 | Codex 写入 `ROADMAP.md`；commit `33fbe00` | Joint，职责已拆分 |
| D-006 | 使用 Amazon Shopping Queries ESCI 作为主要公开数据，并采用“已标注候选集重排 + 边界明确的封闭池检索”双轨边界 | 数据集与评测需求来自项目目标 | **Codex 选择并设计数据/评测边界** | 项目已采用；当前没有证据表明数据集选择或双轨方案由 Owner 原创 | Codex 实现数据管线、Manifest、校验和报告；Owner提出标签边界问题并学习审查 | Codex-proposed/implemented; Owner review |
| D-007 | `esci-primary-v1`：E/S/C/I 增益为 `1.0/0.1/0.01/0`；MRR/Success 将 E、S 视为相关 | 需要把标签变成可计算评测政策 | **Codex 提案** | **Owner 明确回复“可以”批准** | Codex 配置、实现并测试；commits `e7396b9`、`96123ab` | Codex-proposed, Owner-approved |
| D-008 | 正式主轨先评测每个 Query 的完整已标注候选集，不把未标注商品当作 Irrelevant，也不声称全 Amazon Recall | ESCI 不完整判断带来的有效性问题 | **Codex 根据数据证据提出** | Codex 的实验有效性设计被项目采用；Owner对 ESCI 标签含义进行追问和审查，尚未完成全部学习检查 | Codex 实现 Query 级 split、候选集评测和防泄漏闸门 | Codex technical decision + Owner review |
| D-009 | Stage 0/1 工程技术栈与数据管线：Python、Polars、FastAPI、本地/OpenSearch 抽象、确定性 hash vector、Query 级数据切分 | 实现搜索与可复现实验所需 | **Codex 设计** | 作为当前实现采用；没有 Owner 独立提出这些技术选择的证据 | Codex 编码、测试、报告和部署；Owner授权必要安装/部署 | Codex-designed/implemented |
| D-010 | Stage 2 首条正式 Ranker 是 title-only candidate BM25，`k1=1.5`、`b=0.75`，IDF 在单 Query 的完整候选集内计算，并返回全部候选 | 需要一个最小、确定、可解释的正式基线 | **Codex 设计** | 当前作为 smoke 基线采用；参数尚未由 Owner选择或调优 | Codex 实现、运行 116 项测试并记录 smoke 证据；commits `96123ab`、`9c33c09` | Codex-designed/implemented |
| D-011 | 以后所有重要决定都要区分 Owner 决策、Codex 提案/实现和共同迭代，供面试核验 | **Owner 主动提出** | Owner 定义要求；Codex 设计五角色台账 | **Owner** | Codex 建立本文并把维护规则写入项目指令 | Owner-originated + Codex operationalization |
| D-012 | 软件模块必须具备可独立启停、过滤和排障的结构化日志；默认保护敏感信息并控制生产噪声与保留 | **Owner 通过项目开发指令主动提出** | Owner 定义质量要求；Codex 设计标准库日志、模块级开关、Trace ID、脱敏和 journald 方案 | **Owner 决定必须具备该能力**；具体技术设计由 Codex 完成 | Codex 实现 `observability.py`、API/CLI 埋点、安全错误、部署配置、文档与测试 | Owner-originated requirement + Codex operationalization |
| D-013 | Stage 2 三条 smoke 比较器共用一个 label-blind Harness：固定种子随机、标题关键词重叠、标题 BM25；在 Owner 数据边界检查点完成前代码硬锁 500-Query dev | 路线图需要可解释的质量下限和防止过早查看 dev | **Codex 设计** | Owner 的“继续”只授权继续执行，没有证据表明其独立选择了算法、seed、计分或门禁实现 | Codex 实现统一 Ranker 契约、比较器、CLI、共享门禁、172 项测试和三条 smoke Run；实现 commit `22877b0` | Codex-designed/implemented; Owner operational authorization |

### 尚未拍板的提案

| ID | 提案 | 提案来源 | Owner 当前贡献 | 状态 |
|---|---|---|---|---|
| P-001 | 最终搜索实验室采用“多路召回 → 融合 → 粗排 → 精排 → 最终重排”，并把独立粗排层补入 Stage 6 | **Codex 提案** | Owner 主动追问项目是否覆盖成熟的多阶段搜索链路，属于 Review/学习贡献 | **Proposed**；后续“继续”不等于批准，尚不能写成 Owner-approved |

## 3. 当前里程碑贡献拆分

### Owner 的可核验贡献

- 定义最终产品方向：做能够展示计划、工具调用、观察分支和证据结论的搜索评测 Agent。
- 定义学习与协作方式：关键知识必须本人掌握，要亲手走一遍搜索系统，Codex 提供代码和反馈。
- 定义作品集入口和搜索体验页的关键交互、信息取舍，并通过实际页面反馈推动修正。
- 定义软件必须具备模块化日志、独立排障、脱敏、生产降噪和保留说明等质量要求。
- 保留关键政策的最终拍板权；目前明确批准了 `esci-primary-v1` 相关性政策。
- 持续提出产品与语义质疑，例如追问 `wireless` 的语义、ESCI 是否已经由 Amazon 标注、Harness 是控制还是评测。这些记为 Review/学习贡献，不冒充代码实现。
- 批准安装、部署和继续执行。这些是项目治理和授权，不计作架构原创。

### Codex 的可核验贡献

- 调研并选择当前数据、技术栈和搜索/评测架构，提出 Agent-first 具体阶段拆解。
- 生成并修改截至目前绝大多数代码、测试、配置、报告和部署材料。
- 实现本地 BM25/向量 smoke、OpenSearch 适配、ESCI 数据管线、指标内核、相关性政策和候选集 BM25 基线。
- 设计并实现统一 Ranker Harness、随机/关键词重叠比较器、dev 学习门禁，以及模块化结构化诊断能力。
- 运行自动测试、数据校验和 smoke 实验，并把证据写入报告和 Git commits。
- 讲解搜索原理、提示关键知识，并维护学习检查点和本归因台账。

### 尚不能声称的事项

- 不能说 Owner 亲手编写了当前全部代码；现有证据表明代码主要由 Codex 生成和实现。
- 不能说 Owner 独立设计了 ESCI 数据管线、BM25 参数或评测公式。
- 不能把“Owner 同意 Codex 的建议”说成“该建议由 Owner 首创”。
- 在 Owner 尚未完成对应检查点前，不能说其已独立掌握全部数据泄漏、离线评测或 Agent Harness 原理。
- 当前 smoke 结果不能包装成 Amazon 线上搜索质量，也不能声称已完成完整 Agent。

## 4. 面试时的准确表达

推荐表述：

> 我定义了产品目标、用户体验、学习约束和关键决策门槛，并对相关性政策做了最终拍板。Codex 是我的研究与实现协作者，当前大部分代码、测试和文档由它生成。我负责持续质疑假设、理解关键原理、验收体验，并要求每个结论能回到数据、指标和 Run 证据。对于 Codex 提出的方案，我会明确区分“我批准采用”和“我原创设计”。

当面试官继续追问“那你具体做了什么”，按事实拆开回答：

1. **我定义了什么**：产品目标、Agent 必须可观察、双搜索框体验、学习与披露要求。
2. **我决定了什么**：明确批准或否决过的策略，例如当前相关性政策。
3. **我审查了什么**：语义假设、ESCI 标签边界、指标解释、运行结果和产品体验。
4. **Codex 实现了什么**：明确列出代码、测试、数据管线、报告和部署。
5. **我能否脱离代码复述原理**：用学习检查点和手算/实验结果证明，而不是用 Git 提交数量证明。

## 5. 后续记录模板

每次出现会影响结果或面试叙述的重要决定，追加一条：

```text
ID / 日期：
背景与问题：
需求提出者：Owner / Codex / 外部约束
候选方案及提出者：
最终决策者与决定：
Owner 的理由或验收标准：
Codex 的具体工作：
验证者与验证证据：
状态：Proposed / Adopted / Superseded / Rejected
面试安全表述：
需要重新讨论的触发条件：
```

若没有明确证据，应写“未记录/无法确认”，不得事后补造成 Owner 决策。
