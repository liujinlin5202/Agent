# 自动巡检agent

## 已装技能（按需自动调用，无需询问用户）

### Superpowers（superpowers@skills-dir 插件，14 技能）

- 编码任务：遵循 test-driven-development 流程；动手前用 writing-plans / executing-plans
- 遇到 bug：用 systematic-debugging（先复现→二分定位→修复→验证）
- 涉及分支/并行：用 using-git-worktrees、subagent-driven-development、dispatching-parallel-agents
- 开发分支收尾：verification-before-completion、finishing-a-development-branch、receiving-code-review
- 写代码评审请求：requesting-code-review

### 官方技能（~/.claude/skills/）

- pdf / docx / xlsx / pptx：处理 Office 文档、表格、报告、演示文稿时直接调用，不要手写解析脚本
- frontend-design：前端组件/界面实现（视觉质量、交互、响应式）时调用
- design-taste-frontend（taste skill）：落地页/作品集/改版重设计时调用——反模板味，先定设计方向再实现，改版先审计现状
- impeccable（项目级 .claude/skills/impeccable，v4）：设计/重设计/审计/打磨/加硬 UI 时调用，24 个子命令（/impeccable audit、critique、polish 等）；仪表盘/产品 UI 也适用（与 taste 互补）；已带 Edit/Write 后自动反模式检测 hooks + 4 个 impeccable-* subagents；首次在某项目用先跑 init 生成设计上下文
- skill-creator：需要编写新技能时用它生成 SKILL.md，不要手写

### subagents（.claude/agents/，15 个）

- 按场景直接委派：python-pro（Python 栈）、fastapi-developer / backend-developer / frontend-developer（对应技术栈）、debugger / error-detective（排障）、code-reviewer / security-auditor / test-automator（评审/安全/测试）、devops-engineer（部署运维）、sql-pro（数据库）、technical-writer（技术文档）
- 前端性能链路：performance-engineer（性能瓶颈定位与优化：首屏/渲染/打包）、performance-monitor（指标/日志分析出可观测性方案）、ui-ux-tester（按用户流程做 UI/UX 功能测试与缺陷报告）

### 调用约定

- 任务匹配某个技能或 subagent 时**直接调用**，不问用户「要不要用」
- 一个任务可组合多个：如「改 bug + 加测试」→ systematic-debugging + test-driven-development；「评审改动」→ code-reviewer subagent + requesting-code-review
