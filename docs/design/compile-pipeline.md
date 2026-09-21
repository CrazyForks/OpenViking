# Compile collection pipeline

Compile 根据自然语言 Skill，将一组来源材料转换为目标目录中的文件。
Resource 与 Skill 命名空间共用 Map → Shuffle → Reduce → Merge 流水线；Memory 使用独立的页面提交路径。
用户通过现有 API/CLI 提供来源、目标、Skill 和可选指令，无需编写执行计划。

## 使用

```bash
ov compile \
  --from viking://resources/source-docs \
  --to viking://resources/knowledge-base \
  --skill viking://agent/skills/llm-wiki
```

示例要求来源目录及 Skill 已存在。输出格式由 Skill 决定，可包含 Wiki、报告、JSON 或代码文本。
写入 Skill 命名空间时，一次任务生成一个包含 `SKILL.md` 和所需附件的 Skill 包。

## 执行流程

Planner 将 Skill 转换为结构化契约和受限计划；默认流程为：

```python
records = p.map(sources, task=contract.extract)
groups = p.shuffle(records, by=contract.routing, against=target)
changes = p.reduce(groups, task=contract.reduce)
p.merge(changes, into=target)
```

- **Map**：按来源范围提取证据，保留来源引用；独立成品可同时提交完整正文。
- **Shuffle**：通过全局向量候选和目标内检索组织工作组，不以路径或 scope 精确相等作为召回前提。
- **Reduce**：结合组内证据与历史正文，决定合并、拆分、保留或更新，生成最终路径和内容。
- **Merge**：检查来源、路径和版本，生成适用的导航，并通过目标命名空间的发布接口写入。

工作组表示需要共同检查的材料，不表示事实等价，也不预定输出文件数量。
契约支持以 records 为中间产物的有限多级综合；最终文件同路径时归并候选正文与来源。
计划只允许上述算子、绑定引用和单次赋值，最多 12 个节点、8,000 字符；Python 语法仅解析，不执行。

## 契约与模型执行

契约必需项为 `extract`、`reduce`、`routing`，可通过 `distinguish` 描述业务适用范围。
`fields` 以字段名和可选描述定义业务 payload；附加保留要求、格式、必要路径和验证要求放入 `options`。
记录外层的业务字段归入 `payload`，同名异值拒收；业务 payload 与非空正文至少存在一个。
完整 Skill 与已读取附件参与模型请求，无法表达或执行的要求必须明确报错。

简单任务直接调用 provider；复杂任务通过隔离 AgentLoop 子任务处理，支持草稿编辑和授权来源回读。
Skill 包内的 Python 脚本可通过现有 sandbox 执行；该能力不提供额外的操作系统隔离。
`overflow` 默认为 `direct`；仅显式声明 `combine` 与 `structured` 时启用分层结构化归纳。
分批字符预算是软目标，不用于截断完整输入；并发、超时及输出 token 配置约束实际调用。

## 写入与失败处理

每个内容文件必须关联非空的支持输入，运行时校验引用范围和来源覆盖。
Resource 新文件使用 create；更新绑定旧内容 hash，在服务端文件锁内核验版本。
版本变化、目标消失或路径占用的文件跳过并报告冲突，其余文件继续写入。
批量发布不承诺多文件原子性；索引刷新由现有写入链路异步执行。

声明 OKF 的 Resource 页面应用 Wiki 校验、来源链接和祖先导航；普通文件保留其格式。
Skill 包通过现有格式校验及 `add_skill` / `update_skill` 发布，保留未修改附件。
Skill 更新在发布前检查 hash，但检查与整包替换之间仍存在并发窗口。

独立作业失败后，其余作业继续处理；成功产物可部分发布，任务标记为 `failed/salvaged`。
未完成项、冲突和实际成果 URI 明确记录；部分交付不视为完整成功，取消不触发恢复发布。
运行时校验路径、JSON 语法、来源与写入回执；这些检查不证明自然语言语义完整或正文质量。

## 工作目录与恢复

任务数据保存在 `bot_data_path/compile_workspaces/<task_id>/__compile_staging__/pipeline/`。
目录包含契约、计划、来源分片、记录、缓存、路由、草稿、调用统计和最终产物。
`coverage.json` 记录来源分支状态、交付 URI 和文件 hash；仅确认写入或字节相同的既有文件计为 delivered。
成功、失败、取消及部分交付均保留工作目录；远端 sandbox 回收前尝试复制到 Bot 本机。

同一工作目录重执行可复用经内容、依赖和模型配置校验的缓存；公开 API 不提供 resume 参数。
进程重启将中断任务标为失败，不自动续跑；保留目录可在确认不再需要后手动删除。
当前实现存在阶段屏障，全局相似度计算为二次复杂度，不提供永久来源依赖索引或全库自动撤回。

## 实现与测试入口

- [契约与计划校验](../../bot/vikingbot/compile/plan.py)
- [流水线执行](../../bot/vikingbot/compile/pipeline.py)：持有任务运行时与依赖，准备来源、解析计划、调度算子和流转数据集，汇总跨阶段状态与覆盖率。
- [Map](../../bot/vikingbot/compile/ops/map.py)：`run` 分批调度提取，`map_job` 执行单个作业。
- [Shuffle](../../bot/vikingbot/compile/ops/shuffle.py)：`Shuffle.run` 组织工作组；模块内包含向量候选、历史召回与分组逻辑。同次计划内复用召回缓存。
- [Reduce](../../bot/vikingbot/compile/ops/reduce.py)：`run` 调度工作组，`reduce_group` 综合证据与历史，处理成品复用和结构化溢出聚合；`merge_candidates` 综合同路径候选正文。不同路径的候选合并按 `merge_concurrency` 有界并发，每个路径仅由一个任务处理，返回顺序保持稳定；服务默认从 `vlm.max_concurrent` 获取该上限，模型调用同时受全局并发限制。
- [Merge](../../bot/vikingbot/compile/ops/merge.py)：`run` 汇总已接受成果，处理导航、链接和版本检查，返回待发布操作；服务的部分成果恢复使用同一入口。
- [算子共享逻辑](../../bot/vikingbot/compile/ops/common.py)：Map 与 Reduce 共用 `transform`、`pack`、记录 prompt 和来源追踪；各算子共用 `job` 记录作业状态及重试。
- [文件编辑与草稿](../../bot/vikingbot/compile/file_ops.py)：补丁应用、历史正文读取、文件校验与成果存储；直接使用同一运行时状态，不承担流程级 Merge 调度。
- [模型调用与 I/O](../../bot/vikingbot/compile/pipeline_io.py)：通用模型请求、缓存、校验修复、任务文件及并发工作队列。
- [任务与发布服务](../../bot/vikingbot/compile/service.py)
- [增量编译测试](../../bot/tests/test_compile_incremental.py)与[分组测试](../../bot/tests/test_compile_shuffle.py)
