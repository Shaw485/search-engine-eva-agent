# ADR 008：用有界 Observation Inspector 展示 Agent 控制回路

- 状态：Accepted
- 日期：2026-08-29
- 决策者：Owner 决定必须把 Agent 架构与 Observation 可视化；Codex 设计展示契约与实现方案

## Context

当前 stage-aware Retrieval Agent 已经记录 Plan、Tool action、Tool Observation、运行内状态、Evidence、门禁、Trace 和 Replay，但网页只展示动作标题、触发原因、Evidence ID 与门禁结果。用户无法从现有时间线直接区分：

- Planner 的可审计原因与模型隐藏思维；
- 工具执行成功与候选质量门禁通过；
- 单次 Run 的 Observation working memory 与跨 Run 自学习；
- 搜索 Harness、Agent Runtime Safety 和 Owner 策略审批。

后端公开响应刻意只包含有界 `agent_run.actions` 摘要。完整 Tool payload 与哈希链 Trace 保留在服务器，不应为了可视化直接暴露给浏览器。

## Decision

采用“无连线步骤选择器 + 六格 Observation Inspector”：每个真实动作固定展示 `Plan → Tool → Observation → Memory → Safety → Next`。

- Plan 仅显示 `reason_code` 和代码内的人类可读映射，不显示或暗示隐藏思维链。
- Tool 只显示 allowlisted 工具、候选变体和输入边界。
- Observation 区分执行状态、Evidence 是否生成、门禁通过或拦截。
- Memory 只描述当前 Run 已观察动作数与 Trace 绑定的 Evidence 数；明确不存在对话、向量、用户画像记忆，也不会在下一 Run 自动学习旧 Trace。
- Safety 显示作用域、预算、重试、Evidence 契约、门禁和 Owner 审批边界。
- Next 使用响应中真实的下一条 action；最后一步使用真实终态和终态 reason code。
- 使用原生按钮、44px 以上触控目标、清晰焦点和简短 live status。详细六格不是 live region。
- 单独提供默认关闭的 `agent-observation-ui` 诊断模块，只记录 Trace ID、工具枚举、状态、步骤和计数，不记录 Query、商品、Evidence 值、原因码、门禁数组、响应或凭据。

## Options considered

### A. 展示模型“思考过程”

拒绝。当前 Planner 是确定性程序而非 LLM；虚构思考过程不可审计，也会误导用户把解释文本当成真实因果证据。

### B. 把完整 Trace/Tool payload 直接发到浏览器

拒绝。它会扩大隐私与契约攻击面，增加页面噪声，并破坏后端当前的最小披露边界。

### C. 只画静态架构图

拒绝作为唯一方案。静态图能讲概念，但不能证明某次 Run 实际执行了什么，也不能展示 Observation 如何改变下一步。

### D. 有界动作摘要驱动的交互式 Inspector

采用。它能从现有严格响应契约重建可审计状态转换，同时不扩大 Agent 权限或浏览器数据面。

## Consequences

### Positive

- 用户可以逐步检查真实控制回路，不再把工具成功、门禁通过和策略批准混为一谈。
- UI 与后端最小披露边界保持一致，无需修改 Runtime 权限或公开完整 Trace。
- Observation 可视化具有独立日志、复现和回归测试。

### Negative

- 页面是 Run 完成后的只读回放，不是流式执行监控。
- Memory 只能显示 Trace/Evidence 统计，不能提供跨 Run 学习能力。
- 新增工具或 Observation 字段时，需要同步更新严格 API Contract、展示映射和测试。

## Follow-up actions

1. 用真实 `agent_run.actions` 验证基线、门禁拦截、门禁通过和终态四类视图。
2. 保持 Runtime Planner 工具数与页面“独立诊断工具”文案分离，避免把五个手动入口称为 Planner 工具。
3. 若未来加入跨 Run Memory，先单独定义读写权限、保留策略、污染防护和可删除性 ADR；不得沿用当前 Trace 统计冒充长期记忆。
