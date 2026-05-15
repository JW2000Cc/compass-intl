# Compass Skills · Claude Code Skill 生态

Compass 的"扩展能力"系统——通过 [Claude Code Skill](https://docs.claude.com/) 实现。
做核心反思引擎之外的小工具：ATS 关键词审计 / 公司简报 / 薪资谈判 / 转行决策 / 月度反思生成。

## 设计选择

**只支持 Claude API 用户**——这是有意识的产品聚焦：

| 维度 | 理由 |
|---|---|
| Claude Code Skill 是真元认知层 | LLM 自己根据用户请求决定何时激活——这是 Anthropic 生态的真实价值差，自己造的内置引擎复刻不了 |
| 集中投入做透一条路 | 与其做二流的内置引擎 + 一流的 Claude 路径，不如做透 Claude 路径 |
| 用户可以自由选 provider | 但 skill 这一层是 Claude-only。Compass 核心（matcher / claim grounder / 简历改写）所有 provider 都能用 |

**其它 provider 用户**：可以正常用 Compass 核心，但看不到 skill 那一层。Compass 启动时会显示提示。

---

## 一次性配置（5 分钟）

### Step 1: 启动 Compass MCP server

Compass 通过 MCP 暴露 6 个核心 tool 给 Claude Code Skill 使用：

```bash
cd ~/Desktop/我的程序/求职工具/Compass
pip install mcp     # 一次性
python -m tools.mcp_server   # 长跑进程，不要关
```

server 挂在 stdio 上，等 Claude Code 连接。

### Step 2: 配置 Claude Code

编辑 `~/.claude.json`（如不存在则新建），加 mcpServers 段：

```json
{
  "mcpServers": {
    "compass": {
      "command": "python",
      "args": ["-m", "tools.mcp_server"],
      "cwd": "/Users/<你的用户名>/Desktop/我的程序/求职工具/Compass"
    }
  }
}
```

### Step 3: 安装一个 reference skill

复制 Compass 提供的示例 skill 到你的 Claude Code skill 目录：

```bash
mkdir -p ~/.claude/skills/ats_keyword_audit
cp ~/Desktop/我的程序/求职工具/Compass/compass/skills/ats_keyword_audit/SKILL.md \
   ~/.claude/skills/ats_keyword_audit/
```

### Step 4: 重启 Claude Code

新会话里说："帮我审核 job_id=xxxx 的 ATS 关键词覆盖" — Claude Code 会自动激活 ats_keyword_audit skill，调 compass.* tools 完成审核。

---

## Compass 暴露的 MCP Tools

skill 可以调用：

| Tool | 描述 |
|---|---|
| `compass.list_jobs(tier, status, limit)` | 列岗位（按 tier 1-5 / 状态过滤） |
| `compass.get_job(job_id)` | 取一个 job 的完整信息（含 JD） |
| `compass.list_facts(active_only)` | 你的 resume facts（不可变事实） |
| `compass.list_variants(job_id)` | 某 job 的所有 variant（含 decision） |
| `compass.get_calibration()` | 你的当前 calibration 边界 |
| `compass.update_status(job_id, status)` | 修改 job 状态（applied/interview 等） |

---

## Reference Skills

`compass/skills/` 下有开箱即用的 skill 模板：

| Skill | 做什么 | 状态 |
|---|---|---|
| **ats_keyword_audit** | 评估 approved 简历对 JD 关键词的覆盖率，区分"可加 / 不能加（无 facts 支持）" | ✅ Ready |
| `cover_letter_gen` | 给 job + facts 生成 cover letter | 📋 Backlog |
| `salary_negotiation_sim` | 模拟谈薪对话 | 📋 Backlog |
| `company_brief` | 面试前 5 分钟公司情报 | 📋 Backlog |
| `transition_decision` | 转方向 / 转行决策辅助 | 📋 Backlog |
| `monthly_reflection_report` | 月度反思自动生成 | 📋 Backlog |

每个 skill 是一个 `SKILL.md` 文件 + 可选辅助资源。复制到 `~/.claude/skills/<name>/` 即可启用。

---

## 写新 skill

格式（Claude Code Skill 标准）：

```markdown
---
name: my_skill
description: 一句话——Claude Code 据此决定何时激活这个 skill
---

<这里写指令——Claude Code Skill 在激活时把整个 markdown body 加载到 context>

可在指令里调用 compass.* tools。
```

把文件放到 `~/.claude/skills/my_skill/SKILL.md`，重启 Claude Code 即可。

---

## 设计哲学链接

skill 系统是 Compass 路径三（开放生态）的工程实现。
完整设计讨论见 [`docs/discussions/04-paths-2-and-3.md`](discussions/04-paths-2-and-3.md)。
为什么放弃内置 skill 引擎、专注 Claude 路径——见 ADR-0011（待补）。
