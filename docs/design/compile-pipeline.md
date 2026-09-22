# Compile collection pipeline

Compile 根据自然语言 Skill，将一组来源材料转换为目标目录中的文件。
Resource 与 Skill 命名空间共用可编排的 Map / Shuffle / Reduce / Finalize 算子；Memory 使用独立的页面提交路径。
用户通过现有 API/CLI 提供来源、目标、Skill 和可选指令，无需编写执行计划。通过 `--args '{"wiki_links":true}'` 开启 Wiki 链接与导航处理；省略该参数时默认关闭。

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

Planner 根据 Skill 和本次 instruction 确定交付物、输入依赖与阶段要求，选择独立转换、分组综合或全局综合；不按业务场景名称分派流程。省略计划时的默认流程为：

```python
records = p.map(sources, task=contract.extract)
groups = p.shuffle(records, by=contract.routing, against=target)
changes = p.reduce(groups, task=contract.reduce)
p.finalize(changes, into=target)
```

- **Map**：输出 records 或 files，并保留来源引用。`input_unit=range` 按来源片段组织批次；`input_unit=file` 将同一来源文件的所有片段放在一个任务中。中间 records 各自独立处理。
- **Shuffle**：routing 字符串或 `{"mode":"semantic","instructions":"…"}` 使用语义分组；`{"mode":"all"}` 将全部记录保留在一个组中，不进行向量分组。`against=target` 启用目标内历史召回。Shuffle 仅获得分组规则、字段含义和必要证据，不获得原始 Skill、附件或 instruction。
- **Reduce**：结合组内证据与历史正文，决定合并、拆分、保留或更新，生成最终路径和内容。
- **Finalize**：检查路径唯一性、交付完整性和版本，按 `args.wiki_links` 选择是否整理 Wiki 链接及导航，返回待发布操作，由服务执行写入。

工作组表示需要共同检查的材料，不表示事实等价，也不预定输出文件数量。
契约支持以 records 为中间产物的有限多级综合。独立文件输出可将 `extract.output` 设为 `files`，并显式使用 `files = p.map(sources, task=contract.extract)`、`p.finalize(files, into=target)`，无需 Shuffle/Reduce。
计划只允许上述算子、绑定引用和单次赋值，最多 12 个节点、8,000 字符；Python 语法仅解析，不执行。

## 契约与模型执行

契约提供 `extract`，其他转换和 routing 按计划引用要求提供；`distinguish` 描述业务适用范围。
`fields` 以字段名和可选描述定义业务 payload；附加保留要求、格式、必要路径和验证要求放入 `options`。
记录外层的业务字段归入 `payload`，同名异值拒收；业务 payload 与非空正文至少存在一个。
Planner、Map、Reduce 和文件冲突处理获得原始 Skill 与本次 instruction；转换阶段也获得已读取附件。无法表达或执行的要求必须明确报错。

简单任务直接调用 provider；复杂任务通过隔离 AgentLoop 子任务处理，支持草稿编辑和授权来源回读。
Skill 包内的 Python 脚本可通过现有 sandbox 执行；该能力不提供额外的操作系统隔离。
`overflow` 默认为 `direct`；仅显式声明 `combine` 与 `structured` 时启用分层结构化归纳。
超出输入预算的转换使用 agent：完整任务保存在私有 assignment.json 中，来源正文通过 read_evidence 分段读取，候选正文通过草稿文件读取，初始消息只提供任务入口。不截断输入；并发、超时、agent 轮数及输出 token 配置仍约束执行。

## 写入与失败处理

每个内容文件必须关联非空的支持输入，运行时校验引用范围和来源覆盖。Map/Reduce 先保存候选；路径唯一的成果直接接受，同路径候选由模型依据 Skill 和 instruction 判断合并、去重或分别命名。改名须避开其他输出路径，并保留全部候选的输入覆盖；失败候选不进入恢复发布清单。
Resource 新文件使用 create；更新绑定旧内容 hash，在服务端文件锁内核验版本。
版本变化、目标消失或路径占用的文件跳过并报告冲突，其余文件继续写入。
批量发布不承诺多文件原子性；索引刷新由现有写入链路异步执行。

声明 OKF 的 Resource 页面始终应用 Wiki 格式校验。`args.wiki_links` 是布尔值，默认 `false`：保留提交正文和已有链接，不自动补充链接或导航。设为 `true` 时，对 Wiki 页面整理链接、补充来源链接并生成祖先导航；普通文件和 Skill 包不受影响。来源追踪、覆盖率统计和版本检查始终执行。
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

同一工作目录重执行可复用经内容、依赖和模型配置校验的缓存；公开 API 不提供 resume 参数。本地恢复工具仅支持四阶段 Reduce 完成后的检查点，重新通过候选冲突处理及其缓存确定可发布文件。
进程重启将中断任务标为失败，不自动续跑；保留目录可在确认不再需要后手动删除。
当前实现存在阶段屏障，全局相似度计算为二次复杂度，不提供永久来源依赖索引或全库自动撤回。

## 实现与测试入口

- [契约与计划校验](../../bot/vikingbot/compile/plan.py)
- [流水线执行](../../bot/vikingbot/compile/pipeline.py)：持有任务运行时与依赖，准备来源、解析计划、调度算子和流转数据集，汇总跨阶段状态与覆盖率。
- [Map](../../bot/vikingbot/compile/ops/map.py)：`run` 分批调度提取，`map_job` 执行单个作业。
- [Shuffle](../../bot/vikingbot/compile/ops/shuffle.py)：`Shuffle.run` 组织工作组；模块内包含向量候选、历史召回与分组逻辑。同次计划内复用召回缓存。
- [Reduce](../../bot/vikingbot/compile/ops/reduce.py)：`run` 并发处理工作组；`reduce_group` 综合证据与历史，处理成品复用和结构化溢出聚合；`resolve_files` 接受唯一路径，按 Reduce 并发上限解决冲突；改名路径在保存前占用，遇到并发占用时允许重新规划一次。Map、Shuffle、Reduce 作业分别使用 `bot.compile.map_concurrency`、`shuffle_concurrency`、`reduce_concurrency`，未配置时继承 `vlm.max_concurrent`；模型请求仍受服务级共享并发限制。
- [Finalize](../../bot/vikingbot/compile/ops/finalize.py)：`run` 汇总已接受成果，处理导航、链接和版本检查，返回待发布操作；服务的部分成果恢复使用同一入口。
- [算子共享逻辑](../../bot/vikingbot/compile/ops/common.py)：Map 与 Reduce 共用 `transform`、`pack`、记录 prompt 和来源追踪；各算子共用 `job` 记录作业状态及重试。
- [文件编辑与草稿](../../bot/vikingbot/compile/file_ops.py)：补丁应用、历史正文读取、文件校验与成果存储；直接使用同一运行时状态，不承担流程级 Finalize 调度。
- [模型调用与 I/O](../../bot/vikingbot/compile/pipeline_io.py)：通用模型请求、缓存、校验修复、任务文件及并发工作队列。
- [任务与发布服务](../../bot/vikingbot/compile/service.py)
