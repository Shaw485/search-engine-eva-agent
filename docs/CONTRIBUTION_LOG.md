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

以下为截至 2026-08-29 的回填。对没有明确对话证据的事项，不推定为 Owner 决策。

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
| D-014 | Stage 2 `compare_runs` 只比较同一可信数据/Policy/Query/候选证据的两个 Run；统一采用 candidate-baseline delta，保留逐 Query 和逐商品排名 Diff，并把本地 Run store 与 Agent registry 信任边界分开 | 路线图需要让评测结论可验证、可下钻，并为 Agent 工具提供安全证据 | **Codex 设计**；Owner 的“继续”授权执行 | 当前采用为 smoke Harness；Owner 尚未对 random/BM25/overlap 的结果作质量决策 | Codex 实现严格校验、指标复算、CLI、原子不可变制品、结构化诊断和 205 项测试；commit `8df54b8`；smoke evidence 见 `docs/STAGE_2_SMOKE_REPORT.md` | Codex-designed/implemented; Owner operational authorization and pending review |
| D-015 | 先在现有作品集网站提供全量 ESCI 商品的“优化前”搜索体验，再根据 Owner 实际体验做优化；优化后通道暂不开放 | **Owner 主动提出**：“打造一个基础的搜索系统，然后亚马逊提供的所有商品都能搜索，我去体验一下，然后再进行优化”；并明确“载体是我的那个网站” | Owner 定义产品顺序与载体；**Codex 提出 SQLite FTS5、字段权重、只读 API 和部署方案** | **Owner 决定体验优先级和网站载体**；具体搜索架构没有冒充 Owner 原创 | Codex 实现全量索引构建器、搜索服务、API、网站接入、结构化诊断、测试和 ADR，并部署、验证 1,814,924 商品生产索引；Owner 授权部署但仍负责接下来的体验与 Bad Case 判断 | Owner-originated product decision + Codex technical design/implementation |
| D-016 | 在 dev 解锁前先实现 smoke-only、确定性的 Agent Runtime 垂直切片：四个白名单领域工具、受信 Run registry、基于观察分支的 Fake planner、有限状态/预算、Trace 与离线 Replay；真实模型和通用插件系统后置 | Owner 要求项目最终必须体现 Agent，并在 Codex 解释实施顺序后回复“可以”“继续” | **Codex 提出具体 Runtime 架构、安全边界和分阶段实现** | Owner 批准继续建设 Agent 方向；这不是 Owner 独立设计 Runtime 细节。当前受限脚手架已通过本地验收，但完整 Agent 阶段仍未完成 | Codex 实现代码、测试、日志和文档；外部语义/安全复审发现的问题由 Codex 修复；277 项测试、Ruff 与仓库策略检查通过 | Codex-proposed/implemented; Owner-approved direction |
| D-017 | 关键知识仍须随开发及时解释，但不再用问答或测验中断执行；未取得独立理解证据时不把学习状态标为完成，也不解锁 500-Query dev | **Owner 主动提出**：“不要问答了。继续” | Owner 定义协作节奏；Codex 将其落实为“进度内讲解 + 保持门禁” | **Owner** | Codex 更新项目协作指令与学习记录；Stage 1/2 学习状态仍为 pending | Owner-originated workflow decision + Codex operationalization |
| D-018 | 为当前 Agent Runtime 增加视觉化流程说明，帮助 Owner 区分搜索系统、搜索评测和 Agent 控制闭环 | **Owner 主动提出**：“把agent可视化，我不太懂目前的流程” | **Codex 提出文档落点和流程图结构** | Owner 提出学习需求；Codex 在不解锁 dev、不改变搜索结果的边界内执行 | Codex 新增 `docs/AGENT_FLOW.md`，并从 README、Runtime guide、Roadmap 链接；该文档是学习与讲解材料，不是代码能力声明 | Owner-originated learning/product communication need + Codex documentation implementation |
| D-019 | 最终 Agent 不能只是被动比较两个 Run；它必须主动搜索/抽样发现 Bad Case、提出候选优化、调用 Harness 比较，并通过审批面板让人点击更新或拒绝；批准后系统自动应用版本化策略并验证 | **Owner 主动提出并纠偏**：“你去判断或者构思或者自己去实际搜索，看看badcase，然后提出优化。然后用harness比较……人只需要点更新策略，或者拒绝策略” | Owner 定义目标产品行为；Codex 将其整理为 approval-gated optimization workflow 和阶段交付 | **Owner** 定义方向与审批交互；Codex 负责具体架构拆解和后续实现 | Codex 新增 `docs/AGENT_OPTIMIZATION_WORKFLOW.md`，更新 `ROADMAP.md`、`AGENT_FLOW.md`、README 与学习记录；该条记录的是当时的方向与规格，随后由 D-020/D-021 完成 smoke 后端与工作台的部分实现 | Owner-originated product decision + Codex specification implementation |
| D-020 | Agent 工作台必须接真实后端：点开始后后台跑 smoke Harness、找 Bad Case、提出候选策略、返回指标与样本证据；Owner 点击更新/拒绝，批准后策略进入策略平台 | **Owner 主动提出并明确反对“只是预览”** | Owner 定义交互和审批边界；**Codex 设计并实现** API-first smoke-only proposal/decision/catalog 后端闭环与首个 exact-boost 候选策略 | **Owner** 决定产品体验必须闭环；Codex 选择 `candidate-title-bm25-exact-boost-v1` 作为首个受控候选策略并实现 | Codex 实现后端 proposal/decision/catalog、策略目录、前端工作台 API 接入、策略平台读取、模块化诊断与测试；当前审批只允许服务器 loopback Owner 通道，浏览器按钮和线上搜索生效尚未实现，也不解锁 dev/test | Owner-originated product requirement + Codex technical design/implementation |
| D-021 | Agent 的分析、提案和对比必须升级为复杂但可控的优化策略，并允许后续用算法或模型增强；模型不得获得 Codex/平台私钥，必须由服务端 Owner 凭据或本地模型接入 | **Owner 主动提出**：“agent的分析，提案，对比。这应该是很复杂的策略”；并表示愿意提供模型额度方向 | Owner 定义“更强优化 Agent”目标；**Codex 设计**“诊断→有界候选→Harness→回归门禁→审批”架构、模型/密钥安全边界和首版工程默认门禁 | Owner 决定继续建设复杂优化 Agent；尚未独立选择模型、费用预算或正式门禁阈值。当前工程默认阈值不冒充 Owner 产品政策 | Codex 实现 smoke 根因诊断、参数化 exact-boost、多候选实验、透明相对选择分、七项可信门禁、Run/Comparison 重验、完整 active revision 与当前部署 revision 检查、候选完整 Ranker config 绑定、跨进程审批锁和工作台可视化；批准后下一轮以 active 策略为基线。未使用或提交任何 API Key | Owner-originated product direction + Codex technical design/implementation; model/provider policy pending Owner decision |
| D-022 | 工作台按钮要能自主判断是否需要多路召回或粗排等阶段能力，自己运行候选实验和 Harness，再把证据交给 Owner 批准；首个实现使用 query-scoped 多路词法召回、RRF、粗排和 12 项门禁 | **Owner 主动要求**按钮达到“分析需要增加多路召回/增加粗排然后优化”的水平，并最终明确说“执行” | Owner 定义自动诊断/实验/人工批准的产品方向；**Codex 提出**固定 20-Query fully judged pool、title/exact/multi-field 通道、RRF Top 20、title-BM25 coarse Top 10、三个权重候选和 12 项工程门禁 | **Owner 批准执行该产品方向**；没有证据表明 Owner 独立选择了算法、权重、pool、cutoff 或阈值，这些仍是 Codex 方案 | Codex 实现 stage contracts、严格重验、三候选 ablation、stage diagnosis、Harness comparison、API/工作台证据与测试；conservative 候选本地 smoke 通过 12 门禁。当前只请求 Owner review，不写 decision/catalog/active，不改变线上搜索，也未声明部署 | Owner-originated capability decision + Codex-proposed/implemented technical slice |
| D-023 | 将 stage-aware retrieval 真正放入统一 Agent Runtime：两个最小白名单工具、观察驱动 Planner、Grounding、预算、不可变 Trace/Replay 和网页只读时间线；仍不授予审批或激活权限 | 延续 Owner 对“按钮自行分析、实验、比较并交给人决策”的要求；本轮 Owner 说“继续”授权推进 | **Codex 设计** `RetrievalOptimizationTask`、两项 capability、uniform/conservative/aggressive 的有界观察分支、终态语义、Trace 网页契约和 ADR；Owner 没有独立选择这些 Runtime 参数或分支算法 | Owner 授权继续实现既定方向；未批准浏览器审批、自动激活、500-Query dev 或部署 | Codex 编写/整合 Runtime、工具、Planner、Grounding、Replay、API、网页时间线、日志、测试和文档；真实 smoke 路径为 uniform 失败七门禁、conservative 全通过、aggressive 失败两门禁，最终 `proposal_ready`；后端 376 项、前端 46 项及真实响应契约联调通过 | Owner-originated product direction + Codex-designed/implemented Runtime integration; no activation/deployment authority |
| D-024 | 为 stage-aware Runtime 建立第一版 Agent Evaluation Harness，并增加只读 committed-smoke 的 Query 构造器与工作台自检入口；仍不读取 locked dev/test、不审批或激活策略 | 延续既定路线图后 Owner 以“执行”授权推进 | **Codex 设计**静态独立 Oracle、12 个固定任务、Agent/固定工作流对照、相邻字母调换与词序反转变换、污染边界、API/网页摘要契约；Owner 没有独立选择任务、阈值、变换或指标 | Owner 授权执行既定阶段；未批准模型接入、500-Query dev、浏览器审批、策略激活或部署 | Codex 实现 Agent Eval contracts/catalog/scenarios/judge/artifacts/runner/CLI、Replay 安全错误与权限复核、独立 source-pin Query constructor、受 Basic Auth 保护的 API、模块日志、测试与文档；实现测试 12/12，其中 8 项归因生产 Planner、4 项归因 Harness stimulus，固定工作流只比较 3 个对称 branching 情境且成功率为 1.0；Query set 为 59=20 原始+39 未标注合成 | Owner operational authorization + Codex-designed/implemented; no activation/deployment authority |
| D-025 | 先执行全部 59 条来源受限开发 Query，让 Agent 工作台获得真实 Bad Case 行为诊断，再进入策略优化 | Owner 在理解 59=20 原始+39 变换样本后明确回复“行”“执行” | Owner 决定继续推进真实执行；**Codex 设计**59-call 单阶段执行器、四类纯行为判定、证据/执行双 ID、SQL deadline、跨进程锁、失败 attempt、隐私样本和 API 契约 | Owner 授权执行该步骤；没有独立选择分类谓词、Top 10、时限、存储或锁实现，也没有批准把无标签变化视为质量退化、解锁 dev/test、修改策略或部署 | Codex 实现 full-catalog batch search、完整性/来源/索引/策略权威校验、不可变哈希 artifact、owner-only 限量样本、CLI/API、模块日志、测试与文档；clean revision 上两次 59/59 执行复用 `bad-case-b2cbe225fea3`，得到 40 个唯一行为候选（`zero_result=40`、`spelling_sensitive=10`，分类重叠），0 operational failure / protected dispatch / strategy write；不读 relevance labels、不算 nDCG/MRR、不诊断 stage drop、不激活策略，且仍缺可强杀 worker deadline | Owner-originated execution priority + Codex-designed/implemented; no quality/activation/deployment decision |
| D-026 | 把 40 个行为候选升级为完整 20 来源簇的人类诊断 Oracle，并迁入可强杀 worker；Agent 首个诊断驱动候选为仅零结果触发、保护数字/型号词的 drop-one-token AND 回退，行为证据与质量证据严格分轨 | Owner 要求 Agent 自己找 Bad Case、构思并实验优化，人只负责更新或拒绝，并在当前阶段再次回复“执行” | **Codex 提出**两阶段盲化 Oracle、append-only/CAS 决策、POSIX 进程组 worker、严格 StrategySpec、行为/质量双证据轨和首个保守 AND 回退切片 | Owner 授权继续实现既定产品方向；没有证据表明 Owner 独立选择了 Oracle 标签枚举、进程信号协议、token 保护谓词、RRF 或证据门禁，也没有批准把行为恢复称为质量提升 | Codex 实现核心、7 个 owner-only API、Tool05、模块化安全日志、部署参考与 ADR；本地验收为后端 519 项、网站 98 项、Ruff/策略检查/JS 语法/smoke 全通过。clean revision `b7efb90e` 实跑得到 59/59、diagnostic `bad-case-3b5d1ff13a7c`、supervisor receipt `bad-case-supervisor-execution-750f5910a53e`、plan `diagnostic-experiment-plan-bae9602ea206` 和未写判断的 batch `oracle-batch-4b2533c217d3`。Owner 尚未提交 70 项判断，策略未运行、未批准、未激活，本阶段也未部署 | Owner-originated product direction + Codex-designed/implemented; no activation/deployment authority |
| D-027 | 尝试通过统一新 Basic Auth realm 让生产 Agent 工作台重新显示登录挑战 | **Owner 在部署验收中主动指出**“至少要打开登录页面” | Owner 定义最低可用验收标准；Codex 当时推断为旧 realm 的无效凭据缓存，并提出只更换 realm | Owner 要求修复入口；Codex 选择 `Search Agent Owner` 并实施 | Codex 更新 14 个受保护 location 并部署；Owner 随后再次截图证明 `ERR_INVALID_AUTH_CREDENTIALS` 仍存在，因此该诊断与修复被后续 D-028 证伪，不能写成已解决 | Owner-originated acceptance correction + Codex hypothesis/failed fix |
| D-028 | Agent 工作台改为“公开无证据登录壳 + 网页内内存凭据 + Nginx 每个 Owner API 逐次验证”，避免内置浏览器原生 Basic Auth 失败 | **Owner 用第二张生产截图继续指出入口仍不可用** | Owner 提供决定性验收证据；Codex 将根因修正为内置浏览器不支持当前原生 Basic Auth 导航，并设计内存会话、同源精确 API allowlist、无挑战 `403`、未知 Agent 路由 fail-closed 与独立 `owner-auth-ui` 日志 | Owner 决定必须在其网站内出现可用登录页面；没有独立选择协议或 Nginx/JS 细节 | Codex 实现登录表单、默认隐藏工作台、刷新/退出清除、静态认证探针、CSP、Owner API 逐次鉴权与 Authorization 剥离、测试和部署说明；不在仓库、存储或日志中保存账号密码 | Owner-originated acceptance requirement + Codex security design/implementation |
| D-029 | 为网页登录探针和 Owner API 增加独立限流与脱敏拒绝日志；已在非秘密渠道出现的生产密码必须交互式轮换 | Codex 安全复审提出，Owner 尚未独立选择具体阈值 | Codex 选择登录 `5r/m + burst 3`、Owner API `30r/m + burst 15`，仅对 `429` 写入不含账号、Authorization、请求体和参数的 JSON 日志；轮转复用主机已有 Nginx wildcard，避免重复规则 | Owner 授权继续修复和部署；新密码必须由 Owner 在服务器交互提示中输入，不能由 Codex 记录或冒充 Owner 决策 | Codex 实现 Nginx `limit_req_zone`、精确 location 限流、专用日志、测试和运维说明 | Codex security design; Owner retains credential decision |
| D-030 | 工作台底部只展示优化前后真实发生变化的搜索样本，并同时覆盖变好与变差 | **Owner 明确提出展示与验收要求** | Owner 定义需要看见双向结果差异；**Codex 设计**跨候选只读证据投影、epsilon 边界、按 Query+方向去重、最多 10 条、方向保留和来源标注 | Owner 决定页面必须同时呈现改善与退化；没有独立选择阈值、排序或去重算法 | Codex 保持最终候选的完整门禁与指标不变，从本轮消融实验中提取非持平样本；每张卡绑定自己的 Run/Comparison/策略/门禁来源，退化样本明确标为已淘汰候选，并增加严格 API/前端合同、脱敏计数日志和回归测试 | Owner-originated product requirement + Codex-designed/implemented evidence projection |

### 尚未拍板的提案

| ID | 提案 | 提案来源 | Owner 当前贡献 | 状态 |
|---|---|---|---|---|
| P-001 | 最终搜索实验室采用“多路召回 → 融合 → 粗排 → 精排 → 最终重排”，并把独立粗排层补入 Stage 6 | **Codex 提案** | Owner 先主动追问项目是否覆盖成熟多阶段链路，随后明确要求按钮能自主判断多路召回/粗排并说“执行” | **Partially adopted direction**：Owner 已批准执行 stage-aware 方向；当前只实现多路词法召回、RRF 与粗排 smoke slice，精排/最终重排仍未实现，具体架构仍属 Codex 方案 |

## 3. 当前里程碑贡献拆分

### Owner 的可核验贡献

- 定义最终产品方向：做能够展示计划、工具调用、观察分支和证据结论的搜索评测 Agent。
- 定义学习与协作方式：关键知识必须本人掌握，要亲手走一遍搜索系统，Codex 提供代码和反馈。
- 决定关键知识改为随进度直接讲解，不再用问答/测验中断执行；该流程决定
  不等于已经完成相应知识验证。
- 要求把 Agent 当前流程视觉化，因为文字流程仍不够直观；这是学习与产品
  表达需求，不等于 Owner 已独立设计 Runtime 结构。
- 将 Agent 产品方向从“被动比较 Run”纠偏为“主动发现 Bad Case、提出策略、
  Harness 验证、人工审批后自动更新”的优化 Agent。
- 明确要求 Agent 工作台不能只是预览，必须接后端并完成“开始分析—提案—审批—
  策略平台更新”的产品闭环。
- 要求把 Agent 的分析、提案和对比升级为更复杂的优化策略，并愿意在安全
  边界内引入算法或模型；这定义了能力方向，不等于 Owner 已选择具体模型、
  参数搜索算法、费用预算或发布门禁阈值。
- 明确要求工作台按钮能自主判断是否需要多路召回、粗排等阶段能力，自动实验
  和 Harness 比较后再由人批准，并以“执行”批准开始实现；Owner 没有据此
  认领具体 RRF 权重或门禁阈值的原创。
- 以“继续”授权把既定 stage-aware 方向推进到 Runtime/Trace 网页可视化；这
  是执行授权，不代表 Owner 独立设计了 Tool Schema、预算、Planner 或 Replay。
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
- 设计并实现受信 Run 比较契约、总体/逐 Query/逐商品 Diff、本地 Run store
  防线与未来 Agent registry 边界。
- 设计并实现 smoke-only Agent Runtime 脚手架：严格领域工具、观察驱动的
  确定性 Planner、状态/预算/权限、Trace/Replay、模块化日志与安全测试。
- 将 Owner 的主动优化 Agent 方向拆成 strategy proposal、Harness comparison、
  approval panel 和 versioned strategy update 的后续实施路线。
- 将固定单候选提案升级为确定性根因诊断、有界参数候选搜索、透明相对选择分、
  七项可信发布门禁、证据重验、active revision/CAS 和连续迭代基线，并设计
  模型 Provider 的密钥、预算与权限边界。
- 设计并实现 query-scoped stage-aware retrieval：显式 title/exact/multi-field
  召回通道、RRF、粗排、stage lineage、严格证据重验、三个候选 ablation 与
  12 项门禁，并记录 conservative 通过、uniform/aggressive 失败的 smoke 证据。
- 将 stage-aware retrieval 接入统一 Runtime：新增严格任务/工具契约、观察驱动
  Planner、动作 Grounding、失败预算、Trace/Replay 语义验证、API 摘要和网页
  只读时间线，并完成真实后端响应到前端契约的联调。
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
