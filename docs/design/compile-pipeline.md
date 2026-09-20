# Compile collection pipeline

Resource 和 Skill 命名空间目标的现有 Compile API/CLI 默认执行同一集合 pipeline，无新增开关或请求参数。
自然语言 Skill 仍为输入；任务内 planner 将其转换为带 hash 的契约和受限 Python 计划。
memory 与受保护 skills namespace 保持原有写入/校验路径和合法目标范围。
这里的通用性是“由 Skill 定义提取、路由、聚合和文件产物”，不要求实体、slug 或 Wiki。

## 执行与使用

Wiki 的最小计划：

```python
records = p.map(sources, task=contract.extract)
groups = p.shuffle(records, by=contract.routing, against=target)
changes = p.reduce(groups, task=contract.reduce, overflow=contract.overflow)
p.merge(changes, into=target)
```

写作 Skill 的有限多级计划：

```python
records = p.map(sources, task=contract.extract)
dimensions = p.shuffle(records, by=contract.routing)
rules = p.reduce(dimensions, task=contract.reduce, overflow=contract.overflow)
outputs = p.shuffle(rules, by=contract.final_routing)
changes = p.reduce(outputs, task=contract.synthesize, overflow=contract.overflow)
p.merge(changes, into=target)
```

此处 `reduce.output=records`，`synthesize.output=files`；最终路径可以是 Resource 下的
`SKILL.md` 和契约要求的 references。报告、JSON、代码文本等使用相同算子和文件提交路径，
不为普通文件套用 Wiki 格式；实际声明 OKF 的 Markdown 页启用原始来源链接与局部导航处理，
混合文件集合中的普通文件保持原格式。
用户无需编写 DSL，正常提供 from、to、Skill 和 instruction 即可。

Planner 的公共契约仅要求 `extract`、`reduce`、`routing`，可指定 `distinguish`。
`distinguish` 用“简短字段名 → 字段含义”的映射描述 scope，例如 `subject: 产品主体及适用范围`。
名称与解释随 Map、Shuffle、Reduce 传递；记录 schema 展示说明，但业务字段可按证据增补或省略，
不按字段名长度、与规划键名完全一致或 scope 字段数拒收。保存的字段名列表可读作空解释映射。
`fields` 使用“字段名 → 一句简单描述”的映射，名称和描述随 `record_fields` 传入模型及 payload 工具 schema。
描述可选，不检查长度、句数或内容；空字符串和 null 均可用作缺省描述，null 归一化为空字符串。
字段不限制数量；旧名称列表按首次出现顺序去重并转为空描述映射，代码移除记录外层的保留字段名，清理后允许为空。
实际记录的 `payload` 顶层同样移除这些保留字段；外层值及业务内容中的嵌套字段保持原样。
省略 `plan` 使用四步默认流程，省略 `reduce.output` 生成文件；中间 Reduce 显式选择 records。
版本由运行时补齐；附加保留/验证要求、多级综合、overflow、输出格式、必要路径及未支持能力
集中到可选的 `contract.options`，按需填写。运行时展开为现有契约属性并执行全部校验；
旧的平铺契约仍可读取，重复、未知或越界选项拒绝。完整 Skill 和附件始终提供，无需在契约重抄。
ready 正文承担完整事实，其 payload 保留充分的路由与关系；已经由正文承载的证据字段可为 null。
需要联合综合的分片保留充分结构化证据，不通过空字段省略必要原文信息。

契约包含任务指令、有界 JSON 形式的记录字段、需要保留的 scope 信息、
必须保留的信息、overflow 策略、输出格式、必要路径及验证要求。原 Skill 全文进入每个任务，
未能表达的要求使计划失败。静态格式检查包括 Wiki frontmatter、JSON 语法和合法输出路径；
自然语言语义约束由模型执行，不能靠引用覆盖证明语义无损。
各算子只接收当前阶段的指令与字段，公共上下文保留原 Skill、保留/验证要求和运行元信息，
不重复携带其他阶段的完整契约。实际文件重名时，Reduce 收尾按路径归并候选正文与来源。

计划只允许 map/shuffle/reduce/merge、绑定引用和单次赋值：最多 8,000 字符、12 个节点，
无 import、循环、任意调用、执行 Python 或隐式数据集 fan-out。Shuffle 将每条记录放入一个候选工作组。
工作组表示需要共同检查的证据，不预定输出文件数量、路径或事实等价关系。路径、scope 措辞和版本差异
不阻断候选召回；补充材料、潜在重复和矛盾可共同进入 Reduce，由其保留适用边界、合并或拆成多个文件。
Reduce 决定最终路径；同一组可以生成多份新文件、更新多份已召回历史文件，并保留无须修改的历史内容。
实际产物同路径时，Reduce 收尾选择不同来源 URI 数最多的候选作为主稿，调用模型归并其他候选。
模型超时或接口报错时，仅保留主稿正文和其自身来源，不追加补充内容；其他路径保留。Merge 检查最终路径唯一性；资源发布时在锁内检查历史版本和 create 冲突，并按文件跳过冲突项。
每个内容文件必须关联非空的纳入记录支持子集，省略子集则继承全部纳入记录；运行时核对范围与总体覆盖，并按页传播原始来源。
新增路径继续执行唯一写入者与 create 检查。历史文件整体交给 Reduce 判断，更新绑定原文件 hash。

简单任务直接结构化调用已有 provider；`execution=agent` 使用现有 AgentLoop 的隔离子任务，
复用 `subagent_iterations`（默认 70 轮）、自己的草稿文件 read/write/edit、受限来源回读与结果提交。
大文件通过 `content_ref` 提交，运行时读取子目录内文件、核对 hash 和来源覆盖；每文件至多 8 MiB，
文件数量不设固定上限，引用文件每次提交共 16 MiB。子任务完整保留工具返回，禁止会隐藏原文中段的头尾预览及自动摘要；
merge_input_chars 仅用于软分批和原文内联，不按初始提示或累计上下文字符数拒绝请求。复杂 Map 每个子任务只接收一个来源范围或记录，跨输入判断交给 Reduce。
小记录仍合批，Map 额外按输出预留空间限制批次载荷，防止只按输入上下文装满任务。
独立成品可由 Map 用 ready_path 与 ready_content/ref 提交，正文只写一次，payload 只保留路由与关系信息。
仅新建、单记录且无需历史比较的工作组复用完整草稿；草稿路径已存在时进入 Reduce 核对历史。
联合工作组与历史更新执行 Reduce，输入包含完整成品正文，不能只用简短路由字段重建内容。
分片或联合判断任务继续传递充分结构化证据；不统一退化为逐来源摘要。
记录集合可以通过 `result_ref` 提交 JSON；提交 schema 提供字段含义，运行时核对来源覆盖。主 agent 不派发单条任务。
子任务首次校验失败的未截断提交原文保存在其私有草稿目录，包括语法错误的 JSON，可通过既有 edit_file 局部修改并用 result_ref 重交；仍执行全部来源、权限和文件检查，两次失败即终止。截断响应不执行、不保存为完整候选；草稿保存失败不覆盖原校验错误。
明确引用的 Skill 附件由运行时预读，并随每次完整模型请求提供；额外引用使用 `read_skill_resource`
经任务的授权 client 按需读取，支持明确行范围，
不静默截断。单附件 256 KiB，最多 32 个、合计 1 MiB；内容 hash 写入任务依赖清单和缓存，
缓存重用前校验依赖版本。模型标识和实际 ISO 时间由运行时提供。来源回读仅接受当前分配的来源范围 ID。最终生成按分批目标内联完整原文，其余范围可通过工具按需读取；该目标不用于拒绝模型请求。
原文优先于派生字段；提示模型在细节缺失、歧义或冲突时回读核对，不要求读齐全部原文才能提交。来源 ID 与文件支持关系仍需有效；这些校验不证明语义无损。
子任务可通过 `run_skill_script` 调用所选 Skill 包内的 Python 脚本。脚本与已读取附件按 hash
落入本次调用目录，以输入/输出 JSON 文件名为参数，复用任务现有 sandbox.execute；单次 60 秒，
退出成功且输出 JSON 完整才返回。模型自行写入的 scratch 脚本不会执行，脚本结果仍须通过正常 emit
验收；没有目录扫描式发布。direct 后端继承已有宿主机执行权限，不是新增 OS 隔离层。
二进制产物及超出当前 backend 能力的要求仍须明确报告，不能静默跳过。

## 检索、预算与写入

- Bot 经当前身份的 `VikingClient.compile_embeddings` 调用
  `POST /api/v1/search/compile-embeddings`；服务端验证 target 身份/ACL，复用配置的 embedder
  和其并发限制。每批最多 32 条、每条 1,024 字符，批次复用有界 worker 池；模型指纹变化会拒绝混用。
  不跨进程读取凭证，不把临时文本写入用户索引。
- 临时 embedding 和内容缓存只在任务目录。所有记录参与全局 top-5 候选比较，无 scope/path 精确匹配前置过滤。
  向量以紧凑 float32 保存，相似度按 64×256 块计算，不建 N×N 矩阵；计算后释放向量，仅保留候选 ID。
  复用 NumPy，缺少时使用标准库 top-k，不按集合大小拒绝任务。
  每批最多 4 条记录，每条召回 top-5 候选；模型可以关联本次请求展示的任意同批记录或候选，不限定为该记录自己的 top-5，也不限制选中关联数量。历史目标同样可从本批展示的召回结果中选择；ID 和 URI 必须属于请求中展示的范围。候选描述在请求内去重。
  Compile 不按总字符数拒绝模型请求；完整 Skill、附件、记录和工具结果按实际内容发送。
  关联的连通分量形成工作组，只表示共同处理，不证明内容相同，也不预分配输出路径。
  Shuffle 输出的工作组最多 50 条记录；更大的连通分量复用本阶段 embedding，按固定种子与余弦近邻
  拆为互斥组，每条记录恰好分配一次，目标历史 URI 随成员保留，不增加 embedding 或模型调用。
  每组只执行一次 Reduce，收尾仅对同路径候选增加一次内容归并，不因重名合并工作组。
  路由可以按分配的 source_range 回读原文。不确定的潜在关联交给 Reduce 判断，不因低置信度拒绝输入。
- 明确出现在输入证据中的目标 URI 优先成为候选；其余使用 target 限定 find，最多 4 个，
  目录结果只检查最多 4 个直接子项。选中相同历史文件的记录进入同一工作组，避免不同组独立更新同一版本。
  历史读取先短描述，Reduce 再读取候选正文并校验版本。搜索异常不是空结果，越界候选拒绝。
- 来源按实际字符切片并分批读取，不设来源文件数、总字符数、Shuffle/累计记录数或临时向量总量上限。
  来源正文最多保留一个读取批次，随后落入任务分片；来源元数据、记录元数据和向量仍累计驻留内存。
  Map/Shuffle/Reduce 仍有阶段屏障，全局相似度仍为二次计算量；没有宣称端到端流式或千篇吞吐已验证。
- worker 默认复用配置的模型并发数，显式 CompileLimits 仍生效；所有任务共享模型槽位，队列最多并发数的两倍。
  merge_input_chars 仅作为分批和原文内联的软目标；单条超过目标仍完整提交，不设初始提示或累计上下文字符上限。直接调用最多一次结构修复，提供被拒候选和具体节点错误；planner 同时报告契约字段和 AST 数据流错误，复用通用完整结果修复，不维护字段路径替换协议。模型文本、raw 工具参数和结果草稿文件先用已有 json_repair 修复 JSON 语法，再校验结构与来源，不增加模型调用。明确截断的模型响应不作为完整结果接收。完整失败候选可用于一次模型修复，不按字符数截断或拒绝修复，不记录思维内容。
  reduce 省略 overflow 时唯一绑定 contract.overflow；非法参数与任意 Python 仍拒绝。
  直接调用不设固定读取次数或总轮数上限；planner 600 秒，单次请求 600 秒；直接或子 agent 任务 1,200 秒，
  整个 pipeline 不设总时限。单次调用/算子时间限制包含 provider 内部重试和排队；I/O 失败任务重试一次。
  所有阶段（包括规划、路由、直接调用与 Agent 子任务）的输出 token 上限优先使用 VLM 的 max_tokens 配置；未配置时显式传入 32,000。实际值进入缓存键与调用日志。
  Compile 全阶段显式关闭 thinking，保留完整 Skill 与原文；有效配置进入缓存键。
  每次请求保存输出 token 设置、完整消息/工具/附件字符数、工具名、耗时、finish_reason、整数 usage 与错误类型；
  子 agent 工作数与真实请求数分开统计，阶段 usage 汇总包含输入/输出 tokens。adapter 统一归一化 SDK usage，
  缓存命中与 reasoning token 计数保留，SDK 对象不进入 AgentLoop 或任务 JSON。
- 契约显式声明 combine 与 structured overflow 时，超过分批目标的组可进行最多三层结构化归纳。
  未声明 combine 或归纳后仍超过分批目标时，完整输入继续交给 Reduce，不以字符数或组内记录数拒绝。
  大文件不反复携带增长的完整检查点；唯一锚点 patch 绑定完整旧文件 hash，并保留未命中字节。
- 最终 batch-write 增加可选 `expected_sha256`：在原有文件锁内、任何本批写入前检查旧字节版本。
  新文件使用 create；资源 Compile 显式启用 `skip_conflicts`，在锁内跳过版本变化、目标消失或路径被占用的文件，其他文件继续写入。
  返回的 `conflicts` 包含 URI、错误码和原因；冲突不计入 delivered 或 page_count，任务标记为 failed/salvaged，暂存产物保留。
  内容与缓存相同的文件也在服务端锁内核验版本；批量写接口默认仍在冲突时拒绝整批。不承诺多文件原子性或 exactly-once。
  索引刷新继续由既有批量写入链路排队执行（wait=false）。
- 导航只读取受影响文件的祖先 index，保留旧链接；不扫描全部历史文件。
  实际 OKF 导航由运行时独占，内容子任务不能重复提交祖先索引；普通非 Wiki index.md 不受此限制。
  Merge 直接用既有 Markdown 渲染器生成导航，不调用模型或 Skill 脚本，也不按模型预算分批。
  每个 index 只列直属内容页和下一级目录入口，使用已有页面标题与描述；新目录以目录名作为标题。
  导航不汇总后代页面的来源；内容页的来源链接由运行时记录补齐。
  已接受成品的路径变换由程序重定位链接；其余未解析链接仅在 target 内 stat 精确目标，确认不存在且
  当前成果标题唯一时才允许修复。未检索不等于链接无效，不扫描整个历史库，也不代表全库链接审计。
  Reduce 获得至多 12 个同来源的已接受成品标题、说明和最终路径，避免依赖尚未存在的路由建议路径。
  并行组还获得至多 12 个同来源、同结构记录的主题范围和说明；这些是主题提示，不是已存在文件。
  最终只链接精确且唯一的实际标题；先筛掉正文未提及的候选，避免对每个无关页面重复解析已有链接。跨来源关系发现尚不完整。
- 普通 Markdown 的 type: report 等元数据不触发 Wiki 适配；files 契约仅对声明 OKF 类型的页面应用 Wiki
  校验与导航。JSON 检查语法，Skill 中的专门 JSON schema 仍由模型履行，不承诺通用 schema 执行器。

### Skill 命名空间发布

`to=viking://agent/skills`（以及已支持的用户 Skill 命名空间）使用同一 Map→Shuffle→Reduce→Merge 执行器。
一次任务生成一个 Skill 包；planner 在 `required_paths` 声明 `<skill-name>/SKILL.md`，各组生成该目录内的文件。
Shuffle 使用全部记录的向量候选和目标内的 `skills` 搜索结果；包目录命中读取其 `SKILL.md`，不扫描全部历史包。
Reduce 可输出多个附件与入口文件，重复输出路径由共同 Reduce 处理。Skill 包不执行 OKF 导航或引用改写。
Merge 检查来源、路径、修订及必要文件；即使部分恢复也不能省略契约规定的 Skill 文件。
服务复用现有格式校验及 `add_skill`/`update_skill` 发布，更新时保留未修改的附件，结果 URI 指向包根目录。
发布前再次检查修改文件的 hash；现有 Skill 更新 API 不提供原子 CAS，检查与整包替换之间仍存在并发窗口。
Memory 保留页面提交和子任务编排；旧 Skill 提交分支及其失去用途的通用文件适配已删除。

## 临时文件与恢复边界

文件位于 `bot_data_path/compile_workspaces/<task_id>/__compile_staging__/pipeline/`：
`contract.json`、`plan.json`、`inputs.json`、`sources/`、`records/`、`payloads/`、`embeddings/`、
`cache/`、`routes/`、`groups/`、`jobs/`、`artifacts/`、`merge.json`、`summary.json`，
以及 `calls/`、`candidates/`、`skill-dependencies.json`、`runtime.json`、`coverage.json`。
来源保留原文、字符/行位置、片段 hash；记录保存完整证据引用和逐层 lineage。
来源计数按 URI 去重。`coverage.json` 逐来源记录原文范围/hash、各分支状态、实际成品 URI 和最终字节 hash；只有 batch-write 确认或字节相同的既有成品才标记 delivered。失败作业保留输入 ID，已接受草稿不冒充已交付成品。引用、排除和路径所有权检查不等同于事实保留保证。

各 shard 通过 sandbox 文件 API 和同目录 rename 发布，不假设远端 sandbox 暴露本机路径。
本机可访问的 sandbox 使用原生文件替换，不为每个 shard 启动命令或打印命令日志；远端使用 exec/mv。
只更新受影响组/任务；不维护全量 merge-state 控制面。任务 meta 保留有界阶段统计和错误预览，
包括调用次数、provider usage、缓存命中、候选/正文读取、未决/失败、队列峰值和阶段耗时。

独立 Map/Reduce 作业失败时，队列继续处理其他输入，成功结果继续进入后续阶段，未完成项明确标记。
Shuffle 的候选判断按批次并发。单批路由失败记录到任务失败清单，不阻止其他批次生成工作组。
仅路由失败的记录标记未完成，不进入 Reduce；分组前移除失败记录及其关联边，成功记录按彼此已确认的关联和历史目标继续分组、拆组，并按部分交付发布。
未经确认的候选不作为关联，已保存的记录和草稿保留。历史候选读取失败按所属批次记录，不伪造历史为空。
取消传播至所有工作任务。普通失败/超时可复用完整接受的成品 artifact，继续通过同一 merge 和目标命名空间发布入口保存部分成果；不扫描或发布内部 JSON、来源和任意 scratch。恢复不调用模型；Skill 的导航脚本仍可运行，缺少规定产物记 warnings。导航格式化失败按批次保留确定性索引，不丢弃其他批次的格式化结果，明确哪些路径未完成 Skill 格式化。任务返回 `failed/salvaged`、实际成果 URI 和 warnings，不能视为完整成功；取消不触发恢复发布。
实际产物路径、唯一 owner 和 artifact hash 仍是写入约束；资源目标的历史版本和 create 冲突按文件跳过并报告，Skill 包保留整包校验。部分交付保留未完成状态和工作目录。
Resource/Skill Compile 的成功、失败、取消和部分交付均保留工作目录供核对与复用；远端 sandbox 停止前尝试复制
到 Bot 本机，本地文件不复制到自身。恢复拷贝受既有 cleanup grace 限制，失败会记录日志，不承诺拷贝一定完成。
保留目录确认不再需要后可手动删除；任务状态记录的过期清理不会自动删除失败工作目录。
同一工作目录重新执行可复用已验证结果和 embedding；缓存键包含处理版本、Skill/契约、
输入内容/来源 hash、模型配置，更新任务还包含旧目标 hash。公开 API 不新增 resume 参数。
进程重启沿用现有 BOT_RESTARTED 失败标记，不自动恢复执行；临时目录清理后无跨任务缓存。
没有永久来源依赖索引，不提供全库自动撤回、删除、影响分析或自动合并历史文件。

## 验证

原始整批命令覆盖 121 个文件（两个目录共有 121 文件，并非 120）：

```bash
ov compile \
  --from viking://resources/shunfeng_half/part2 \
  --from viking://resources/shunfeng_half/part1 \
  --to viking://resources/shunfeng/shunfeng-wiki-incremental-8 \
  --skill viking://agent/skills/llm-wiki
```

千篇输入位于 `viking://resources/input`，由 120 份原文副本与 880 份带显式合成标记、
独立产品/园区/合同范围的模板变体构成，共 29,068,888 字节。原始整批中的补充产品简称表
不复制进千篇集；模板及其余来源目录保持原样。模板来源、合成标记与逐文件 SHA-256
位于 `/tmp/ov-compile-scale-20260918/input-manifest.json`。合成数据不是真实新增业务材料。其中 22 个原文副本无正文，154 个变体来自无正文模板，仅有合成场景信息；824 个输入来自有正文模板，明细见 `input-shape.json`。

```bash
ov compile \
  --from viking://resources/input \
  --to viking://resources/compile-1000-20260918 \
  --skill viking://agent/skills/llm-wiki
```

验证目录为 `/tmp/ov-compile-scale-20260918`，保留进程 RSS 采样、提交响应和回归日志。
任务工作目录为 `/Users/bytedance/.openviking/llm-wiki/bot/compile_workspaces/<task_id>`。
模型沿用配置的 VolcEngine `ep-20260918100503-9n4tz`，共享并发 20；Skill 与附件未裁剪。

| 任务 | 范围与状态 | Pipeline 耗时 | 已返回 input / output tokens |
| --- | --- | ---: | ---: |
| `cmp_763b01f0dbde4284be9af182288ed6b9` | 121 文件基线；228 分片，2 Map 作业完成、5 ready 记录；为应用实测优化而取消，未发布 | 449.3s | 247,171 / 96,592 |
| `cmp_70980c381c6243d797893e3cf85de2e5` | 121 文件；规划在单次修复后仍有非法 files→Map 数据流，未发布 | 52.7s | 34,425 / 2,039 |
| `cmp_c202e6d0c7544868aac09b48b6a32a21` | 121 文件；为修复中间格式约束取消并保留有效缓存，未发布 | 1054.6s | 2,380,840 / 587,648 |
| `cmp_6ec1a9605913423b83f4b32195b24a84` | 1,000 文件、1,969 分片；为修复中间格式约束取消并保留有效缓存，未发布 | 933.4s | 2,391,214 / 442,810 |
| `cmp_c7d7985ef80d95a1f2bab410f4ad83ec4e2864ff25264a946a0e49dd39d4e6dd` | 121 文件；Map 整批结束后发现路由分区放大失败，取消并保留缓存与 11 个已接受 artifact，未发布 | 4309.5s | 11,624,875 / 2,519,767 |
| `cmp_f53cdfb5c439e3074d41f2de9edcf8b55eb2334f05501bae5a63c89a36aec9e6` | 1,000 文件；为应用路由失败隔离修复取消，未发布 | 4499.3s | 15,253,647 / 2,688,255 |
| `cmp_5adb69e6a557fc101cd5b3937d75e1553ca6314ea8051885fd706ea6d5df9e92` | 1,000 文件；为应用内容文件来源非空校验取消并保留缓存，未发布 | 8285.2s | 46,244,696 / 7,831,405 |

当前恢复任务：

- 121 文件：`cmp_60a06fd2eb4941f9a2eabc326be8c891126753560d074077cb266e71b9cf3c76`，携带 244 个缓存条目，读取时重新核对依赖/hash。
- 1,000 文件：`cmp_7e437142f668b59f599688e33b91c3daf4ea42698a8fb8c7451100b480caee14`，携带 783 个缓存条目，读取时重新核对依赖/hash。

恢复使用既有幂等提交 API，完整来源集合未缩减；没有新增恢复参数。两批共享模型槽位，不作为单批独占吞吐基准。

121 篇恢复任务终态为 `failed/salvaged`，条件写入确认 986 个文件；867 个内容文件的实际 SHA-256 全部匹配，零未确认写入。其中 865 个具备来源分片关系，另 2 个虽有正文来源引用但支持输入为空，单列 `outputs_missing_lineage`，分配证据与排除原话见 `lineage-exceptions-121.json`，不计入来源覆盖；内容文件的非空支持输入校验拒绝此类提交。完整交付 **29/121（23.97%）**，至少有一个核对通过产物 **96/121（79.34%）**，70 篇有失败分支、22 篇全部排除。22 篇经原文件核对均只有 PDF 转换元数据、正文为空；排除不计作交付。来源账本在任务工作目录 `pipeline/coverage.json`，平铺对照在验证目录 `coverage-121.csv`；任务完整错误、调用和分阶段统计在 `report.json`。

该次 Pipeline 用时 5,058.4s（Map 693.6s、Shuffle 664.9s、Reduce 3,676.8s、merge 22.7s），使用 35,980,849 tokens；121 篇所有尝试累计 53,474,206 tokens，含 30,057,101 缓存输入 tokens，41 个调用没有 usage，成本统计为已返回用量的下界。当前 Map 8 个、路由批次 4 个、Reduce 5 个作业失败；另有 79 条不确定归属和 105 个跨 scope 路径冲突。导航脚本对部分未匹配配置的目录触发 `StopIteration`，保存基础导航并报告格式化未完成；103 条已确认缺失目标链接保留告警。覆盖证明来源分支处置和实际文件落盘，不证明正文质量。

千篇第 4 轮于 2026-09-19 01:50:58（北京时间）结束，终态为 `failed/salvaged`；从本轮提交到终态约 **5 小时 24 分钟**。整批 1,000 个输入均有处置记录，**395/1,000（39.5%）完整交付**、557 篇有失败分支、48 篇全部排除；**865/1,000（86.5%）至少关联一个已核验产物**。条件写入确认 4,730 个文件，其中 4,517 个内容文件、213 个导航文件；内容文件全部核对实际 SHA-256、非空来源关系和写入回执，零 hash 不符、零未确认写入、零无来源内容文件。千篇全量实测已结束，但未达到千篇全部完整交付的目标。

本轮 Pipeline 19,409.0s：Map 14,758.4s、Shuffle 1,496.6s、Reduce 2,526.6s、merge 624.6s；另有终态写入和收尾耗时。Map/路由/Reduce 失败作业分别为 239/44/10，另有 369 条不确定路由、805 个不兼容 scope 的输出路径冲突，错误类别相互重叠。导航脚本 74 次调用中 26 个批次未完成，日志含来源元数据缺失和未知 `pages` 分类；保留基础导航并报告失败。704 条确认缺失目标的链接仍有告警。失败作业到原输入的对照见 `failed-jobs-1000.csv`，逐篇交付见 `coverage-1000.csv`，完整分支账本见任务工作目录 `pipeline/coverage.json`。

本轮已返回 157,416,958 tokens。千篇全部 4 次尝试累计 **232,268,985 tokens**（输入 204,566,419、输出 27,702,566；缓存输入 136,993,700 已包含在输入中），7,956 次模型请求中 71 次无 usage，此值为用量下界；所有尝试累计 Pipeline 33,126.8s。两批验证及写入后异步索引观测期间，采样进程 RSS 峰值 752.5 MiB。服务端索引/检索用量另列于 `report.json`，异步索引可能继续增长；没有将 token 数换算成未经核实的货币费用。耗时和调用量仍偏高。

千篇终态工作目录保留，并在 `/tmp/ov-compile-scale-20260918/terminal-workspace-cmp_7e437142f668b59f599688e33b91c3daf4ea42698a8fb8c7451100b480caee14` 留存完整副本。121 篇最终工作目录在 2026-09-19 01:00 检查时已不存在，原因未确认；上述 121 篇数字来自 2026-09-18 21:25 导出的历史核验，不能视为再次核验。其 CSV、报告、来源审计与异常证据保存在验证目录的 `retained-evidence-20260919-0100/`；千篇结果来自本次终态重新核验。

逐区间核验 121/1,000 文件的 228/1,969 个来源分片，原文（含 CRLF）、hash 与首尾覆盖全部匹配。404 页实际接受草稿的内链处理计时为 5.61s → 0.52s，输出正文和链接计数逐项相同；链接回归 68 项、核心链路回归 41 项通过。
867 页整批落盘成品的内链预筛选使用批次内编译正则，避免超过 Python 正则缓存容量后逐页重复编译；无采样计时为 6.58s → 0.41s，逐页正文 SHA-256 与链接数完全一致，见 `links-full-121-before.json` / `links-full-121-after.json`。这项改动及局部修复草稿暂存尚未加载到进行中的千篇进程，未计作该轮收益。
千篇终态的 4,517 个真实内容产物在本地首轮合并中触发约 2,048 万次标题正则搜索。批次目标缓存增加无大小写变体的中文片段作为必要条件，未命中片段时跳过正则；最终匹配仍由原正则判断。相同 cProfile 采样下首轮合并 95.76s → 28.69s，4,730 个输出的 SHA-256、链接数和完整链接报告完全相同，8 组 Unicode 边界对照及 68 项回归通过。证据见 `full-finalize-1000.json` / `full-finalize-1000-after.json`；该局部核验不包含第二次缺失链接处理、Skill 导航脚本和发布，不是完整千篇复跑。

基线 6 个 Map 并发时 Bot RSS 约 312 MiB、CPU 接近空闲；首批请求 249.7s 和 283.3s，
输出 12,088 和 10,807 tokens 后仍需继续工具往返。取消中的 6 个请求没有 usage，不能计作零成本。
当前机制不设集合总时限，也不设来源总量上限。上述终态来自 2026-09-18 20:26 进程加载的实现；局部修复草稿、内链优化、死代码清理和候选工作组的当前磁盘代码尚无完整千篇复跑结果。

相关回归扩大集合为 89 通过、5 失败；5 个 content-write 失败全部在原始 HEAD
`0242878237e7274432c9306984c1c0a3e22e68e1` 隔离工作区复现。基线分别涉及共享资源 AGFS
刷新、Memory overview、刷新状态和缺失文件的 replace/append mode，不修改这些无关行为。
新增交付分支/写入确认与 planner 单次修复错误汇总检查通过。小样本历史质量验收没有通过，
历史缓存已清理；本轮正文质量不作为链路验证阻塞项，来源与交付覆盖不证明语义无损。

服务的 Resource/Skill 执行入口直接运行 Pipeline；Memory 使用页面结构化提交与子任务工具。清理审计见 `dead-code-review.json`：相对审查快照新增 40 行、删除 2054 行（净减 2014），移除不可达的 Resource agent/merge/目录恢复链及其专用参数。最终相关回归 48 项通过；扩展 AgentLoop 回归中的 3 项失败在原始 HEAD 同样复现，命名空间提交、子任务收集门禁、隔离文件读取与取消行为的本地核验通过。运行中的千篇任务未重启，清理不计作该轮速度收益。

## 候选工作组验证

回归命令 `.venv/bin/pytest bot/tests/test_compile_incremental.py bot/tests/test_compile_shuffle.py tests/server/test_compile_studio.py tests/unit/test_vikingbot_vlm_adapter_retry.py --no-cov -q` 通过 56 项，其中 Skill 新建/更新复用一个参数化用例，验证附件保留、历史召回、修订检查和必要入口约束。
覆盖跨路径/跨 scope 的全局候选、10 条输入生成 3 个不同路径文件、路由结构失败拒绝未确认关联、历史候选共同处理、
多历史文件版本更新、实际输出重名的联合归并、无关产物的部分恢复、取消与 create 防覆盖。
模型响应由测试控制，这些检查证明算子行为，不证明真实模型的语义质量或千篇交付率。

2026-09-19 本机性能样本：2,048 维合成向量的全局 top-6，1,000 条用时 0.031s，8,397 条用时 2.047s；
后者单份 float32 数据 65.6 MiB，独立测试进程峰值 535 MiB，包含 Python 与依赖；不含 embedding 服务耗时。
当前配置的 embedding 服务对 160 条实际记录（平均 165.6 字符）测得：单批 32 条 1.817s，4 批并发 128 条 1.965s。
按样本吞吐外推 8,397 条约 129s，非全量测量；服务负载、限流和文本长度会影响结果。
证据位于 `/tmp/ov-shuffle-rewrite-20260919/` 的 `tests-trimmed.log`、`similarity-benchmark-compact.json`、`embedding-benchmark.json`。
候选工作组方案尚无完整千篇模型实测；已在运行的任务沿用其进程加载的实现。

## Added-lines 审计

统计全部未提交 tracked/untracked 文件的新增行；删除行不抵扣新增。生成的输入、运行缓存、日志、快照副本和报告数据不计作实现代码。

| 文件 | Added lines |
| --- | ---: |
| `bot/tests/test_compile_incremental.py` | 295 |
| `bot/tests/test_compile_shuffle.py` | 299 |
| `bot/vikingbot/agent/loop.py` | 3 |
| `bot/vikingbot/agent/tools/compile.py` | 22 |
| `bot/vikingbot/agent/tools/compile_merge.py` | 0 |
| `bot/vikingbot/agent/tools/spawn.py` | 2 |
| `bot/vikingbot/compile/models.py` | 5 |
| `bot/vikingbot/compile/pipeline.py` | 1477 |
| `bot/vikingbot/compile/pipeline_agent.py` | 253 |
| `bot/vikingbot/compile/pipeline_io.py` | 472 |
| `bot/vikingbot/compile/plan.py` | 425 |
| `bot/vikingbot/compile/renderer.py` | 88 |
| `bot/vikingbot/compile/service.py` | 331 |
| `bot/vikingbot/compile/shuffle.py` | 364 |
| `bot/vikingbot/compile/skill_resources.py` | 246 |
| `bot/vikingbot/openviking_mount/ov_server.py` | 11 |
| `bot/vikingbot/providers/base.py` | 2 |
| `bot/vikingbot/providers/vlm_adapter.py` | 19 |
| `crates/ov_cli/src/help_ui.rs` | 1 |
| `crates/ov_cli/src/main.rs` | 1 |
| `docs/design/compile-pipeline.md` | 293 |
| `openviking/server/routers/content.py` | 3 |
| `openviking/server/routers/search.py` | 82 |
| `openviking/storage/content_write.py` | 17 |
| `tests/unit/test_vikingbot_vlm_adapter_retry.py` | 18 |

仓库合计 **4729** 行。仓库外既有 Skill 改造按交接文档计 **131** 行，本轮验证 Python 脚本 **420** 行；保守合计 **5280** 行。
