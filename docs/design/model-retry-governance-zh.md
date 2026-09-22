# OpenViking 模型重试治理：add_resource 与 session_commit

状态：首版实现与回归评审（开发分支，未部署）。代码基线：`origin/main@611f5c469b2bb8dc6d072b215251379e780d3f23`，2026-09-22。文中次数来自确定性故障注入，不代表线上实测放大率或已节省 Token。统一重试入口已接入主要非流式路径；实际覆盖与限制见第 8 节。

## 1. 建议与收益证据

模型调用层应成为唯一自动重试负责人。Provider/SDK 每次只发送一个请求；workflow 和 queue 收到模型终止结果后记录失败或按现有规则降级，不再重新获得一份重试预算。在线调用最多一次，离线调用只对可恢复错误做有限重试；credential failover 同样消耗总次数。

一期先覆盖 `add_resource` 的 VLM/Embedding 和 `session_commit` Phase 2。必须同时调整这些链路的失败出口，否则只把 SDK 重试关掉，仍会被外层步骤或消息重入放大。这是模型重试的接入改造，不要求建设通用任务重试、Checkpoint 或集群流控平台。

| 本地故障场景 | 基线 HTTP 请求次数 | 单负责人原型 | 证据范围 |
| --- | ---: | ---: | --- |
| Embedding 持续 429，单次出队处理 | 12 | 4 | 真实 OV adapter + 本机 Ark SDK + mock HTTP |
| Volcengine 文本异步 VLM 持续 401，单凭证 | 4 | 1 | 真实 VLM adapter + mock HTTP |
| Phase-2 步骤重试组合 VLM 持续 429 | 16 | 4 | 组合实际 retry helper/常量和 adapter，未执行完整 commit |
| 上一场景配置两个 credential | 32 | 总计 4 | 实际 MultiCredentialVLM，非线上成功率测量 |

Embedding 的 12 次来自 OV 的 4 次 SDK 调用乘以 SDK 的 3 次 HTTP 尝试。本机 `volcengine-python-sdk==5.0.14` 默认 `max_retries=2`；当前 Embedding 的 Ark 构造没有显式关闭它。VLM 的 Ark SDK 已关闭重试，但文本异步路径自己对所有 Exception 循环，sync/vision 路径则不同。因此历史上的“最多 4 个物理 attempts”只在 SDK 确实单次请求时成立。

Embedding 耗尽内部重试后，handler 仍把同一消息重新入队。除了 transient/unknown，`content_safety` 和 `quota_exceeded` 也落入重入分支；auth 已有明确失败出口。真实 handler 的三轮有界回放复现了这些重入。额度耗尽会打开熔断器，后续轮次可能只等待和重入；熔断只能减速，没有终止预算。

这些证据足以证明结构性重复请求，支持先做收敛。文件摘要、目录 overview、分批与 merge、不同节点向量化属于正常业务扇出，不能以“模型调用数 / add_resource 任务数”衡量重试。历史截图中的 429、积压和累计 Token 也不足以估算重试成本或归因到特定操作。

## 2. 哪一层负责什么

| 层次 | 保留的职责 | 自动模型重试规则 |
| --- | --- | --- |
| 业务流程 | 划分文件摘要、目录概览、记忆抽取等 logical call，决定失败/降级 | 不因同一个模型失败重跑整个步骤 |
| 模型调用层 | 分类错误、退避、credential 选择、总次数与 deadline、事件记录 | 唯一负责人，统一 VLM/Embedding 的策略契约 |
| Provider adapter / SDK | 组装请求、单次 I/O、规范化错误及 usage | 一次调用对应一次 HTTP 请求；关闭 SDK/transport 隐式重试 |
| QueueFS consumer | 投递、前置条件等待、终态写回、既有恢复 | 模型永久失败/耗尽后终止；不把模型异常当作重新发放预算的理由 |

同一输入、模型目标和输出目的构成一个 logical call。换 credential 或网络重试仍属于同一 call；工具调用后的下一轮生成、格式修复、patch repair 属于新的 call，需要保留各自业务循环上限。不能把预算耗尽后的相同请求改名为“fallback”来绕过限制。

建议延续现有 `max_retries=3` 的配置意图，迁移为离线单个 logical call 总计 `max_attempts=4`，跨所有 credential 共用。在线显式为 1，失败后也不再切备用请求；可在第一次请求前选择可用 route。策略按调用上下文传入，不在共享模型实例上临时修改 `max_retries`，避免在线与离线并发串扰。

Operation 是归属与截止时间的上层边界，不能让整棵资源树的所有首次请求争抢“4 次”。若增加 operation quota，优先限制额外尝试，并保留业务生成循环的硬上限；跨 worker 的全局次数/Token 硬预算需要共享持久化，不能靠复制一个整数实现。一期不把这一分布式能力作为已有功能。

## 3. 两条链路如何接入

**add_resource。** 从入口保留 root task 与 operation 归属；parse、文件摘要和 overview 每次生成建立独立 call，Embedding 消息继承归属与绝对 deadline。收到模型永久错误或预算耗尽后，Embedding consumer 返回 FAILED、通知 request wait tracker 并走现有任务终态/ACK 链路，禁止再次 enqueue。Semantic consumer 同样必须识别该终止类型。首版熔断开启时直接返回失败，不再等待并重入；这会降低故障窗口内完成率，作为显式行为变化评审；`queue_enqueued_at` 会在入队时刷新，不能把它当作任务原始 deadline。

文件摘要当前有捕获异常并降级为空摘要的行为。接入时可以保留既有局部降级，但必须记录该 logical call 的失败，避免被外围重新生成；operation 预算耗尽或取消则应停止后续模型调度。向量写入失败属于存储问题，不能为了重试写入再次调用 Embedding，后续恢复应复用已生成向量或明确标失败。

**session_commit。** Phase 1 负责归档、状态和投递，不直接调用模型。Phase 2 的 archive_summary、长期记忆和技能抽取接入同一个模型 owner，去掉对模型失败的整步骤重试。存储类重试只留在已确认幂等的具体 I/O 边界，不能继续包住整个抽取函数。最终失败沿用 `.failed.json` 与已完成步骤记录，已成功步骤不重复执行。等待前序 archive 的 requeue 仍是调度等待，不记为模型 attempt。

ExtractLoop 的工具轮次、格式/patch repair 仍由业务层控制，每次真正的新生成记作新 logical call。working-memory update 失败后的 creation fallback 必须区分业务不适用与模型终止：模型耗尽、auth、内容安全等不能再次触发生成。已确认安全拒绝也不能通过换 credential 或格式修复绕过。

**错误策略。** 429 限流、连接/超时和可恢复 5xx 可在离线策略下退避，使用 jitter、可用的 Retry-After，并将排队/退避/请求耗时纳入同一 deadline。401/403、额度耗尽不在同一 credential 上重试；为兼容已配置的多凭证容灾，可显式允许离线切换其他 credential，但消耗同一总次数，且不回到已失败凭证。400/过长输入/内容安全/unknown 直接终止。额度耗尽与短期限流同为 429 时，按结构化错误码区别处理。取消立即传播，流式响应已经输出内容后不自动重放。

## 4. RetryContext 与恢复的真实边界

上下文只携带执行所需的最小信息：operation/stage/workload、root task/logical call 标识、已用次数/上限、绝对 deadline；reason/owner 随决策事件记录。每个并发 call 有独立预算，credential 切换共享同一个对象。正常交接或主动 requeue 时保持 identity、已用次数与 deadline，不重新初始化。

**仅给 SessionCommitMsg 或 EmbeddingMsg 加字段不能保证崩溃安全。** QueueFS 在 dequeue 后、ACK 前崩溃会恢复旧消息；若新计数只在内存或下一条消息中，恢复仍可拿到旧预算。当前 TaskWorkIndex 也是从队列重建的运行时索引，不能当持久预算账本。

一期原型证明单次执行内次数上限和正常序列化交接。首版实现已消除迁移路径中“模型失败 → 主动重新入队/整步骤重跑”；EmbeddingMsg / SemanticMsg 保留 operation 与可选绝对 deadline。默认未配置 operation deadline；SessionCommitMsg 的总 deadline 和持久 attempt 预占尚未接入。QueueFS 崩溃重投保留既有语义，不承诺跨重启恰好一次或总次数硬上限。

若验收要求“同一个工作跨任意重启也最多 N 次”，还必须在现有独占处理边界内，先持久化 attempt 预占再发送请求，恢复时对结果未知的预占不返还；持久化失败不发送。预算与任务代次要绑定，旧快照不能覆盖新值。这个窄持久化接入需要真实 QueueFS 故障测试，不能用新增字段或本原型替代证明；未完成时不得对外承诺该保证，也不扩展成通用 durable scheduler。

## 5. Metrics：需要补，但先固定计数口径

保留现有 VLM/Embedding/operation Token 指标，新增四个 family 即可。当前旧 calls 指标按 SDK 调用周围打点；SDK 内部再发请求可能不可见，因此旧 calls 不能直接当作统一的物理 HTTP 计数。

| 指标 | labels | 何时计一次 |
| --- | --- | --- |
| `openviking_model_logical_calls_total` | model_type, operation, stage, result | 一个 logical call 最终结束；重试中间失败不计新 call |
| `openviking_model_attempts_total` | model_type, operation, stage, result, error_class | 单次 adapter 请求结束；需先验证 adapter 与 transport 1:1 |
| `openviking_model_retry_decisions_total` | model_type, operation, stage, decision, reason, owner | 决定 retry/failover/stop；决定不等于请求已发送 |
| `openviking_model_retry_exhausted_total` | model_type, operation, stage, reason | call 因次数/deadline/quota 耗尽结束，仅一次 |

operation 统一为 `add_resource` / `session_commit` / `find` / `search` 等固定枚举。当前源码已有 `resources.add_resource`、`add_resource_job`、`session_commit_phase2` 等名字，需要明确映射；队列不能只依赖进程内 telemetry 对象，恢复时从消息继承。stage 同样固定枚举。result、error_class、reason、owner 用受控值；task/session/logical_call/credential ID 以及原始异常内容只进日志或 trace，不进 label。

Dashboard 先展示 attempts / logical calls 与 exhausted / logical calls，按 operation 和 stage 拆分。两类计数都按结束记录；短窗口因跨窗/在途会有偏差，精确收益使用同一批已完成 logical calls 的回放数据，不将分钟级比率当账单或严格不变量。进程崩溃可能丢失进程内事件，Prometheus 也不是预算账本。

若展示重试 Token 占比，再补 `openviking_model_attempt_tokens_total{model_type,operation,stage,attempt_kind,token_type}`，attempt_kind 至少区分 initial/retry/failover。只累加 provider 真实返回的 usage，并展示 usage 覆盖情况；没有 usage 的失败请求标为未知，不能记作零成本。未接入该指标前不展示“已节省 Token”。不依赖客户部署的私有 Metrics，也不新增遥测回传。

## 6. 验证、实施与回滚

最初的独立原型位于 `benchmark/model_retry/`，14 个契约测试及基线 HTTP 回放已保留。后续首版已接入实际 adapter、队列与 Phase 2，并接入四个 Metrics；当前验证结果、环境阻塞和未覆盖路径见第 8 节。代码尚未部署，完整 E2E、跨进程恢复和线上灰度仍未完成。

| 顺序 | 交付与验收 |
| --- | --- |
| A：观测与基线 | 补四个 family；完成已支持 provider 的 sync/async 单次请求契约清点；保留本地故障回放 |
| B：两个场景接入 | 统一 owner，关闭迁移路径 SDK 重试，同时去掉 workflow/queue 的模型重试；永久错误 1 次、在线 1 次、离线总计不超过 N |
| C：真实流程验证 | 故障注入覆盖 429 后成功、持续失败、混合 credential、取消、breaker/deadline；核查失败终态、锁/等待释放、成功步骤和向量不重复生成 |
| D：恢复保证与灰度 | 如需跨重启次数硬上限，完成预占持久化和各崩溃窗口测试后再承诺；自有环境比对成功率/产物、额外请求、延迟和已知 usage |

先小范围按 provider 与操作灰度。回滚必须成组处理 SDK、模型 owner 与外围失败出口，不能只打开旧 SDK retry 又保留新 owner，也不能恢复无限模型失败 requeue。次数上限可沿用原配置意图；deadline 与可选 operation quota 的生产数值按自有环境完成率和延迟确定，原型的 60 秒仅为测试值。

## 7. 代码依据

以下链接固定到本次检查的 commit，避免后续 main 漂移影响评审。

- [Embedding 重试 helper 调用](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/models/embedder/base.py#L343)；[Ark Embedding client 构造](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/models/embedder/volcengine_embedders.py#L82)。
- [Embedding 的终止与重新入队分支](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/storage/collection_schemas.py#L735)；[Semantic 错误重新入队](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/storage/queuefs/semantic_processor.py#L691)。
- [Volcengine 文本异步重试](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/models/vlm/backends/volcengine_vlm.py#L271)；[多凭证切换](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/models/vlm/base.py#L874)。
- [Phase-2 外层步骤重试](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/session/session.py#L2650)；[已有终态恢复](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/session/session.py#L2237)；[ExtractLoop 业务再生成](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/session/memory/extract_loop.py#L292)。
- [摘要局部降级](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/storage/queuefs/semantic_executor.py#L1087)；[QueueFS dequeue/ACK 恢复语义](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/storage/queuefs/named_queue.py#L223)；[TaskWorkIndex 的运行时边界](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/service/task_work_index.py#L3)。
- [现有 Embedding Metrics](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/metrics/collectors/embedding.py#L121)；[现有 VLM Metrics](https://github.com/volcengine/OpenViking/blob/611f5c469b2bb8dc6d072b215251379e780d3f23/openviking/metrics/collectors/vlm.py#L93)。


## 8. 首版实现、回归结论与后续准入

维护约定：以本方案作为策略评审依据；后续改变错误分类、次数、切换、失败出口或指标口径时，同一变更同步修改仓库文档和飞书方案，更新验证证据。

### 已实现的边界

开发分支为 `feat/model-retry-governance`。`openviking/utils/model_call.py` 提供 sync/async 唯一 owner；工作上下文与单次调用状态分开保存，不修改共享模型实例。未绑定上下文时按在线处理，最多 1 次；两个后台入口明确绑定 offline，使用原配置 `max_retries + 1`，默认共 4 次。

当前覆盖 OpenAI / Volcengine / LiteLLM 的非流式 text / vision、Embedding 公共调用入口，以及实际配置使用的 MultiCredentialVLM / FailoverEmbedder。多凭证成功路由仍保持 sticky；短暂错误在候选凭证间切换，auth / quota 错误禁用当前 call 内的失败凭证，均消费统一总次数。原始 SDK 异常类型、status 与 body 保留，通过附加 `model_call_error` 终止信息阻止外层重新获得预算；无上游异常的 deadline / breaker 拒绝使用 ModelCallError。

Ark、OpenAI-compatible Embedding、Gemini、MiniMax 和 LiteLLM 的可见隐式重试已显式关闭。HTTP 请求次数目前实测验证的是 Ark Embedding、Volcengine VLM 和 OpenAI VLM；其他 provider 不能仅凭配置修改就声称通过 transport 一对一验证。流式、音视频上传/轮询/生成流程、Codex 401 刷新重发、旧 FailoverVLM 直接调用，以及第三方 adapter 仍需单独迁移/验证；配置中的旧 backup 语法已由工厂转换到 MultiCredentialVLM，不等同于直接使用旧 wrapper。

Embedding 的模型失败和 breaker 拒绝返回 FAILED，通知 wait tracker；Semantic 模型终止不重入，breaker 提前拒绝时先释放移交锁。文件摘要原有空摘要降级仍保留。Session Phase 2 去掉整步骤重试，working-memory creation/update 不再吞掉终止的模型错误；失败写 `.failed.json`，保留已完成步骤，恢复看到终态后直接结束。

四个指标已通过现有 datasource / collector 路由导出，operation / stage 为固定枚举，call ID 留在异常及日志。当前 attempts 是进入 adapter 的尝试数：本地准备失败、等待 semaphore 时取消，或特殊 SDK 内部认证重发，都可能使它与 HTTP 请求数不完全相等。只在已验证 transport 契约的路径上用它近似物理调用放大，不将其当计费账本。Retry-After 在 30 秒等待上限内被尊重，超过则以 backoff_limit 终止，避免无限等待或提前重试；该上限是首版策略值，后续需结合离线完成率评估。

### 验证结果

| 验证 | 结果 | 限制 |
| --- | --- | --- |
| 首版聚焦回归 | 276 passed，2 skipped | 覆盖 owner、HTTP 请求数、队列终态、等待/锁、Phase 2、异常类型与指标；跳过项为既有环境条件 |
| 真实 SDK + mock HTTP | 19 个场景通过；离线持续 429 共 4 次、在线共 1 次、双 credential 共 4 次、单 credential 401 为 1 次；429 后恢复成功 | 无真实模型、账单或客户线上流量 |
| Phase 2 独立流程 | 模型失败 4 次、抽取步骤执行 1 次；写失败标记并保留完成记录；恢复不重跑；WM creation/update 不触发额外 fallback | 内存文件系统和 TaskStore，未覆盖 native engine / 真 QueueFS 崩溃恢复 |
| 扩展回归 | 879 个测试：856 passed、20 failed、3 skipped；20 个失败已在干净基线复现 | 既有 Ollama 参数 / max_tokens 和日志捕获断言问题，本次不改动其功能 |
| Gemini 扩展测试 | 当前环境缺少 google-genai，未纳入上述 879 项 | Gemini transport 行为尚未验证；相关旧配置断言也需独立清理 |
| 完整 session 集成 | 3 个目标场景在初始化阶段被 PersistStore 缺失阻塞 | 不能声称完整 E2E 通过或已无生产回归 |

### 会改变什么，以及如何判断能否合入

| 回归风险 | 原因与首版行为 | 合入前重点观察 |
| --- | --- | --- |
| 短暂故障下成功率下降 | 在线不再多试；离线全局 4 次比过去 12/16/32 次更少；超过 4 个坏 credential 不会继续遍历到末尾 | 同一批固定输入对比产物、成功率、额外请求和尾延迟；允许调整有上限的配置，不恢复叠乘 |
| 熔断期间积压变失败 | 原先消息等待/重入，首版直接失败；可能集中出现未完成索引 | 这是最需要产品接受的行为变化；上线前用故障窗口验证恢复体验；若需要延后执行，应新增有终止预算的调度等待，并与模型 retry 分开 |
| Session 错误暴露更明确 | 模型终止不再静默变成简短占位摘要；去掉整步骤重试后存储临时错误也可能直接失败 | 确认失败状态和重新提交体验；只在具体幂等存储 I/O 处补重试，不能恢复整个抽取函数重跑 |
| 错误类型/锁/正常链路兼容 | 回归中已修复 SDK 异常类型被覆盖、breaker 提前失败未释放移交锁两项；正常成功与凭证切换测试保留 | 仍需 native 环境 E2E，覆盖取消、并发 add_resource、commit 前序等待和向量写入失败 |
| 覆盖不全与跨重启预算 | 部分 provider/媒体/流式路径尚未验证；无 durable attempt 预占，无默认总 deadline，无 operation 全局 retry quota | 不对所有 SDK 或跨任意重启承诺物理次数上限；下一步按实际流量补齐，不新增通用调度平台 |

这版可供代码评审和受控环境验证。完成 native 环境 E2E、确认熔断失败策略并核查未覆盖 provider 前，不以本地 mock 成功作为直接上线依据。
