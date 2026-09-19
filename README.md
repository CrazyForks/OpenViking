<div align="center">

<a href="https://openviking.ai/"><img src="docs/images/ov-logo.png" alt="OpenViking" width="140"></a>

# OpenViking

**The context database for AI agents.**

Knowledge, memory, and skills that carry across sessions and agents.

[Quick start](#quick-start) · [Docs](https://docs.openviking.ai/en/getting-started/01-introduction) · [Try Studio](https://openviking.ai/studio) · [Website](https://openviking.ai/) · [Releases](https://github.com/volcengine/OpenViking/releases)

<p>
  <a href="https://discord.com/invite/eHvx8E9XF3"><img src="docs/images/community/discord.svg" width="18" height="18" alt="">&nbsp;Discord</a> &nbsp;·&nbsp;
  <a href="https://docs.openviking.ai/en/about/01-about-us#lark-group"><img src="docs/images/community/lark.svg" width="18" height="18" alt="">&nbsp;Lark</a> &nbsp;·&nbsp;
  <a href="https://docs.openviking.ai/en/about/01-about-us#wechat-group"><img src="docs/images/community/wechat.svg" width="18" height="18" alt="">&nbsp;WeChat</a> &nbsp;·&nbsp;
  <a href="https://x.com/openvikingai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/community/x-dark.svg"><img src="docs/images/community/x.svg" width="16" height="16" alt=""></picture>&nbsp;X</a>
</p>

English / [中文](README_CN.md) / [日本語](README_JA.md)

</div>

## Keep the context. Change the agent.

Your project knowledge, preferences, and past work should not be trapped in one agent or one conversation. OpenViking stores them outside the context window, ready for the next session and the next agent.

It is an open-source database that organizes **resources, memories, and skills as files and directories**. Connect your agents to the same service and user scope to reuse that context through MCP, plugins, the CLI, or an SDK.

<a href="https://openviking.ai/studio">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/studio-playground-dark.png">
    <img src="docs/images/studio-playground.png" alt="OpenViking Studio: browse context directories, inspect content, and search by meaning">
  </picture>
</a>

[Explore context in Studio](https://openviking.ai/studio), no installation required.

## Navigate like files. Retrieve by meaning.

- **One place for knowledge, memory, and skills.** Import documents, code, and web pages. Keep them alongside user memories and reusable skills, addressable by `viking://` URI.
- **Search a project, not everything.** Scope semantic search to a directory. Use `ls`, `tree`, `read`, `write`, and `grep` to inspect and manage context rather than only receive search results.
- **Read the summary before the source.** Generated directory abstracts (L0) and overviews (L1) help agents choose what to open. Full content (L2) stays available without putting it all in the prompt.
- **Carry useful history forward.** Session commits extract and update user memories. Enable Agent Evolution to derive reusable experience from task cases, rather than keeping only conversation logs.

```text
viking://
├── resources/                 # Documents, code, web pages
│   └── project/
│       ├── .abstract.md       # L0: Is this relevant?
│       ├── .overview.md       # L1: What is in this directory?
│       └── ...                # L2: Read the content you need
└── user/
    └── {user_id}/
        ├── memories/          # Preferences, events, experience
        └── skills/            # Reusable task instructions
```

Need to turn a collection into a knowledge base? [`ov compile`](https://docs.openviking.ai/en/context-compilation/01-overview) uses a skill to organize source directories into a wiki, knowledge graph, or report. Requires VikingBot.

[Architecture](https://docs.openviking.ai/en/concepts/01-architecture) · [Directory retrieval](https://docs.openviking.ai/en/concepts/07-retrieval) · [Memory extraction](https://docs.openviking.ai/en/concepts/06-extraction) · [Agent Evolution configuration](https://docs.openviking.ai/en/guides/01-configuration#server-section)

## Quick start

**Run a local server.** Requires Python 3.10+ and access to an embedding model and a vision-language model (VLM). Use [uv](https://docs.astral.sh/uv/getting-started/installation/) to install in an isolated environment:

```bash
uv tool install openviking --upgrade
openviking-server init      # Choose providers and models
openviking-server doctor    # Check configuration and connectivity
openviking-server
```

**Import and search.** In another terminal, install the CLI with npm and run `ov config`. Choose **Custom**, set `http://127.0.0.1:1933`, and leave the API key empty for the default local server.

```bash
npm install -g @openviking/cli
ov config
ov health

printf '# Project Atlas\nMaya owns the weekly backup. Keep backups for 30 days.\n' > quickstart.md
ov add-resource ./quickstart.md --to viking://resources/quickstart-demo --wait --timeout 120
ov overview viking://resources/quickstart-demo
ov find "Who owns the backup process?" --uri viking://resources/quickstart-demo
```

`find` returns matching context, not a generated answer. Read a result with `ov read "<returned-file-uri>"`. Use a new target directory if you repeat the import.

Already have a service? Skip the server install and connect the CLI to its URL with your user API key.

[Full walkthrough](https://docs.openviking.ai/en/getting-started/02-quickstart) · [Model configuration](https://docs.openviking.ai/en/guides/01-configuration) · [Docker](https://docs.openviking.ai/en/guides/03-deployment) · [Deploy on Railway](https://railway.com/deploy/openviking)

## Connect your agent

Use a native integration for automatic recall and session capture, or MCP for agent-invoked context tools. Lifecycle support differs by integration.

| Integration | Agents |
| --- | --- |
| Hooks + MCP | [Claude Code](https://docs.openviking.ai/en/agent-integrations/02-claude-code) · [Codex](https://docs.openviking.ai/en/agent-integrations/04-codex) · [Cursor](https://docs.openviking.ai/en/agent-integrations/12-cursor) · [TRAE](https://docs.openviking.ai/en/agent-integrations/13-trae) |
| Plugin + MCP | [OpenCode](https://docs.openviking.ai/en/agent-integrations/10-opencode) · [DeerFlow](docs/images/agents/en/deerflow-memory-manager.md) · [DSH](https://docs.openviking.ai/en/agent-integrations/17-dsh) |
| Context engine | [OpenClaw](https://docs.openviking.ai/en/agent-integrations/03-openclaw) |
| Built-in provider | [Hermes](https://docs.openviking.ai/en/agent-integrations/05-hermes) |
| Native extension | [pi](https://docs.openviking.ai/en/agent-integrations/11-pi) |
| Connector | [Doubao Work](docs/images/agents/en/doubao-work.md) |
| Tools + store | [LangChain / LangGraph](https://docs.openviking.ai/en/agent-integrations/07-langchain-langgraph) |

[MCP clients](https://docs.openviking.ai/en/agent-integrations/06-mcp-clients) · [Agent Plugins 1.0](https://docs.openviking.ai/en/agent-integrations/15-agent-plugins) · [Compare integration capabilities](https://docs.openviking.ai/en/agent-integrations/16-capability-reference)

**Prefer a desktop UI?** [OpenViking Helper](https://docs.openviking.ai/en/agent-integrations/14-openviking-helper) configures local integrations and lets you inspect sessions, memories, and skills. Available for macOS and Windows (beta).

**Building an application?** Use the [Python](sdk/python/README.md), [Go](sdk/go/README.md), or [TypeScript](sdk/typescript/README.md) SDK, or the [HTTP API](https://docs.openviking.ai/en/api/01-overview). [VikingBot](https://docs.openviking.ai/en/guides/17-vikingbot) is the included agent framework.

## Benchmarks

In the team's published May 2026 evaluation, memory improved long-conversation QA and multi-turn task success:

| Benchmark | Without OpenViking | With OpenViking |
| --- | ---: | ---: |
| LoCoMo, OpenClaw | 24.20% | **82.08%** |
| LoCoMo, Hermes | 33.38% | **82.86%** |
| LoCoMo, Claude Code | 57.21% | **80.32%** |
| tau2-bench, Retail | 70.94% | **77.81%** |
| tau2-bench, Airline | 54.38% | **66.25%** |

LoCoMo compares each agent's native memory with its OpenViking integration; input tokens fell by 34–91% in those runs. tau2-bench compares the same LLM with and without experience memory.

These are results for the reported setups, not guarantees for every model or workload. [Benchmark report](https://blog.openviking.ai/post/openviking-benchmark-results/) · [Evaluation code](benchmark)

## Deployment

- **Open source:** run the AGPLv3 server in your environment, without an activation key. [Deployment guide](https://docs.openviking.ai/en/guides/03-deployment).
- **Managed service:** use [Volcano Engine OpenViking](https://www.volcengine.com/product/openviking-service) without operating the server. [Plans and limits](https://docs.volcengine.com/docs/84313/2374478).
- **Commercial self-managed:** distributed deployment in your own cloud / VPC or an offline environment, with official support. [Contact the team](https://docs.google.com/forms/d/e/1FAIpQLScQqwsm7fvKdjtNiW5rWNXJjoHPtedVzLsKSMJgObtsj2_udA/viewform).

Configure [authentication](https://docs.openviking.ai/en/guides/04-authentication) before allowing remote access. See [user isolation](https://docs.openviking.ai/en/concepts/11-multi-tenant) and [resource permissions](https://docs.openviking.ai/en/concepts/15-acl) for shared deployments.

## Research

The work behind memory management, directory-aware retrieval, and structured-document RAG:

- [**VikingMem**](https://arxiv.org/abs/2605.29640): extracting, updating, and consolidating long-term memory.
- [**Directory-Aware Query and Maintenance in Vector Databases**](https://arxiv.org/abs/2606.16903): indexing and searching within directory scopes.
- [**VikingRAG**](https://arxiv.org/abs/2609.11390): retrieving evidence from structured documents with fewer tokens.

## Community & contact

Built by [Volcano Engine's Viking team](https://docs.openviking.ai/en/about/01-about-us) and open-source contributors. Ask questions and share what you build:

<p>
  <a href="https://discord.com/invite/eHvx8E9XF3"><img src="docs/images/community/discord.svg" width="18" height="18" alt="">&nbsp;Discord</a> ·
  <a href="https://docs.openviking.ai/en/about/01-about-us#lark-group"><img src="docs/images/community/lark.svg" width="18" height="18" alt="">&nbsp;Lark</a> ·
  <a href="https://docs.openviking.ai/en/about/01-about-us#wechat-group"><img src="docs/images/community/wechat.svg" width="18" height="18" alt="">&nbsp;WeChat</a>
</p>

| For | Where to go |
| --- | --- |
| Report a bug or request a feature | [GitHub Issues](https://github.com/volcengine/OpenViking/issues) |
| Follow releases and engineering notes | <a href="https://x.com/openvikingai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/community/x-dark.svg"><img src="docs/images/community/x.svg" width="16" height="16" alt=""></picture>&nbsp;@openvikingai</a> · [Blog](https://blog.openviking.ai/) · [Releases](https://github.com/volcengine/OpenViking/releases) |
| Discuss commercial deployment | [Contact form](https://docs.google.com/forms/d/e/1FAIpQLScQqwsm7fvKdjtNiW5rWNXJjoHPtedVzLsKSMJgObtsj2_udA/viewform) |
| Report a vulnerability privately | [Security policy](SECURITY.md), not a public issue |

Lark opens a group QR code. For WeChat, scan the assistant's QR code and mention “OpenViking” to receive an invitation.

**Contribute:** code, documentation, integrations, and reproducible bug reports all help. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

**Partner projects:** [DeerFlow](https://github.com/bytedance/deer-flow) · [NoKV](https://github.com/NoKV-Lab/NoKV) · [loopx](https://github.com/huangruiteng/loopx) · [Hermes Agent](https://github.com/NousResearch/hermes-agent)

## License

The server is [AGPLv3](LICENSE). The [CLI](crates/LICENSE) and [examples](examples/LICENSE) are Apache 2.0, except the [Hermes plugin](examples/hermes-plugin/LICENSE), which is MIT. Third-party components retain their original licenses.
