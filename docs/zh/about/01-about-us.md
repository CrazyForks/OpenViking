# 团队与组织

## 项目概述

OpenViking 是面向 AI Agent 的开源上下文数据库，由字节跳动火山引擎 Viking 团队发起并维护。它用文件系统组织资源、记忆和技能，供 Agent 浏览、检索和按需读取。

## 团队介绍

### Viking 团队背景

Viking 团队主要开发向量检索、知识库和记忆管理产品。OpenViking 将这些领域的工程经验用于开源上下文数据库，与社区共同开发。

这些工作涉及三个相互关联的问题：如何从非结构化内容中提取可检索的信息，如何在大量候选内容中找到相关上下文，以及如何保留对后续任务有用的交互经验。OpenViking 对应提供[资源解析与提取](../concepts/06-extraction.md)、[上下文检索](../concepts/07-retrieval.md)和[会话与记忆管理](../concepts/08-session.md)。可沿这些入口了解实现和使用条件。

### 发展历程与技术演进

| 时间 | 主要工作 |
| --- | --- |
| 2019–2023 | VikingDB 在字节跳动内部用于向量检索 |
| 2024 | 在火山引擎提供 VikingDB、Viking 知识库和 Viking 记忆库 |
| 2025 | 扩展 AI 搜索、知识助手等应用 |
| 2025 年末 | 开源 [MineContext](https://github.com/volcengine/MineContext)，探索主动式上下文应用 |
| 2026 年初 | 开源 OpenViking |

### 学术合作与产学研结合

以下学者参与了 OpenViking 的发起、研究和技术指导：

- 中国人民大学：孙亚辉
- 浙江大学：高云君、朱轶凡、葛丛丛
- 上海交通大学、无问芯穹：戴国浩

## 开源组织建设

### 项目发展阶段

项目围绕上下文存储与检索、Agent 集成和部署能力持续迭代。已实现的能力与后续方向见[路线图](03-roadmap.md)，已发布的变更见[更新日志](02-changelog.md)。

### 治理架构与决策机制

开源治理委员会负责技术路线、版本与功能优先级、核心架构和兼容性评审、工程规范、贡献者协作，以及相关项目的集成。成员包括 Haojie Qin、Jiahui Zhou、Linggang Wang、Maojia Sheng、Yaohui Sun。

具体模块的协作入口和近期活跃评审者见[贡献指南](https://github.com/volcengine/OpenViking/blob/main/CONTRIBUTING_CN.md)。

功能建议和问题在 [GitHub Issues](https://github.com/volcengine/OpenViking/issues) 讨论，代码与文档变更通过 Pull Request 评审。涉及公开接口、数据存储、权限边界或跨模块架构的改动，请先说明当前行为、目标行为、请求或配置示例，以及兼容性影响，再开始实现。

## 社区参与

### 加入社区

#### 飞书群

扫描二维码加入飞书群，交流使用问题和开发方案：

![飞书扫码加群](../../images/lark-group-qrcode.png)

需要先安装[飞书客户端](https://www.feishu.cn/)。

#### 微信群

扫描二维码添加小助手，备注“OpenViking”，申请加入交流群：

![微信扫码加群](../../images/wechat-group-qrcode.png)

也可以加入 [Discord](https://discord.com/invite/eHvx8E9XF3)，或在 [X](https://x.com/openvikingai) 查看项目动态。

### 参与方式

- **报告问题或提建议**：在 [Issues](https://github.com/volcengine/OpenViking/issues) 提供场景、版本和复现步骤。
- **改代码或文档**：阅读[贡献指南](https://github.com/volcengine/OpenViking/blob/main/CONTRIBUTING_CN.md)，提交实现、测试、文档或翻译。
- **开发集成**：为 Agent 工具或框架添加插件，参考[插件开发指南](../agent-integrations/18-plugin-development.md)。
- **分享经验**：在社区分享使用案例、排障过程，或帮助其他用户解决问题。

### 提交什么信息

为了让维护者能复现问题、评估方案或复用经验，提交时可以按下面的内容组织：

| 参与事项 | 建议提供的材料 |
| --- | --- |
| 使用问题或 Bug | 版本、部署方式、复现步骤、预期与实际结果，以及去除密钥和个人信息后的日志或配置 |
| 功能或研究提案 | 要解决的场景、现有方法的限制、方案与取舍；涉及效果或性能时，补充数据范围、评估方法和运行条件 |
| 文档或翻译 | 页面链接、读者在哪一步遇到困难、建议文案；示例改动附验证方法和结果 |
| Agent 集成 | 目标工具和版本、接入方式、验证步骤，以及已知限制 |
| 使用案例 | 任务和数据类型、接入过程、实际结果，以及尚未解决的问题 |

提交代码前请查看已有 Issue 和 PR，避免重复工作。改动应围绕一个完整问题，并附必要的测试和文档。大型改动先讨论范围；可独立理解和验证的部分可以拆开提交。

## 讨论与协作机制

[GitHub 仓库](https://github.com/volcengine/OpenViking) 保存代码、文档和评审记录。群聊适合即时交流；需要跟踪的问题和方案请同步到 Issue 或 Pull Request，方便后续查阅和协作。
