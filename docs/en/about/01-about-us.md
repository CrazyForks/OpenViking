# Team & Org

## Project Overview

OpenViking is an open-source context database for AI agents, initiated and maintained by the Viking team at ByteDance's Volcengine. It organizes resources, memories, and skills as files that agents can browse, search, and read on demand.

## Team Introduction

### Viking Team Background

The Viking team develops vector retrieval, knowledge base, and memory management products. OpenViking applies that engineering experience to a context database developed with the open-source community.

This work involves three related problems: extracting searchable information from unstructured content, finding relevant context among many candidates, and retaining interaction experience for later tasks. OpenViking addresses these through [resource parsing and extraction](../concepts/06-extraction.md), [context retrieval](../concepts/07-retrieval.md), and [session and memory management](../concepts/08-session.md). These pages explain the implementation and conditions for use.

### Development History and Technical Evolution

| Period | Work |
| --- | --- |
| 2019–2023 | VikingDB used for vector retrieval within ByteDance |
| 2024 | VikingDB, Viking Knowledge Base, and Viking Memory Base offered on Volcengine |
| 2025 | Expanded into AI search and knowledge assistants |
| Late 2025 | Open-sourced [MineContext](https://github.com/volcengine/MineContext) to explore proactive context applications |
| Early 2026 | Open-sourced OpenViking |

### Academic Collaboration and Industry–Academia Integration

We sincerely thank the following scholars for their contributions and guidance in launching OpenViking:

- Associate Professor Sun Yahui, School of Information, Renmin University of China
- Professor Gao Yunjun, School of Software, Zhejiang University; Researchers Zhu Yifan and Ge Congcong
- Associate Professor Dai Guohao, School of Artificial Intelligence, Shanghai Jiao Tong University; Co‑founder and Chief Scientist of Wuwen Xinqiong

## Open-Source Organization

### Project Development Stages

Development covers context storage and retrieval, agent integrations, and deployment. See the [roadmap](03-roadmap.md) for implemented capabilities and future directions, and the [changelog](02-changelog.md) for released changes.

### Governance Structure and Decision-Making

The governance committee oversees technical direction, release and feature priorities, architecture and compatibility reviews, engineering standards, contributor collaboration, and integrations with related projects. Members include Haojie Qin, Jiahui Zhou, Linggang Wang, Maojia Sheng, and Yaohui Sun.

See the [contribution guide](https://github.com/volcengine/OpenViking/blob/main/CONTRIBUTING.md) for module contacts and recently active reviewers.

Feature proposals and problems are discussed in [GitHub Issues](https://github.com/volcengine/OpenViking/issues). Code and documentation changes are reviewed through pull requests. Before implementing changes to public interfaces, persistence, permission boundaries, or architecture across modules, describe the current and intended behavior, request or configuration examples, and compatibility impact.

## Community Participation

### Join the Community

#### Lark Group

Scan the QR code to discuss usage questions and development plans:

![Join via Lark QR](../../images/lark-group-qrcode.png)

Install the [Lark client](https://www.feishu.cn/) first.

#### WeChat Group

Scan the QR code to add the assistant and mention "OpenViking" to request an invitation:

![Join via WeChat QR](../../images/wechat-group-qrcode.png)

You can also join [Discord](https://discord.com/invite/eHvx8E9XF3) or follow project updates on [X](https://x.com/openvikingai).

### Ways to Participate

- **Report a problem or suggest a feature:** include the use case, version, and reproduction steps in an [issue](https://github.com/volcengine/OpenViking/issues).
- **Improve code or documentation:** read the [contribution guide](https://github.com/volcengine/OpenViking/blob/main/CONTRIBUTING.md), then submit code, tests, documentation, or translations.
- **Build an integration:** add a plugin for an agent tool or framework using the [plugin development guide](../agent-integrations/18-plugin-development.md).
- **Share experience:** post usage examples and troubleshooting notes, or help other users in the community.

For what to include in issues and pull requests, see the [contribution guide](https://github.com/volcengine/OpenViking/blob/main/CONTRIBUTING.md).

## Discussion and Collaboration

The [GitHub repository](https://github.com/volcengine/OpenViking) holds code, documentation, and review records. Use [GitHub Discussions](https://github.com/volcengine/OpenViking/discussions) for design discussions and community exchange, chat for immediate discussion, and an issue or pull request for work that needs tracking.
