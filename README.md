# git-commit-review

提交前用 AI 按**你自己项目的规范**检查代码，不合规就不让提交。

```
git commit → hook 取暂存区 diff → 读 .ai-review/rules.md → 调 DeepSeek 判定
                ├─ 通过：正常提交
                └─ 违规：打印「规则 + 代码片段 + 建议」，exit 1，提交中止
```

## 三步开始用

### 1. 安装（只需一次）

```bash
cd /path/to/git-commit-review
python3 -m venv .venv && .venv/bin/pip install -e .

export DEEPSEEK_API_KEY=sk-xxxx        # 建议写进 ~/.zshrc
```

然后在**你的业务仓库**里执行：

```bash
cd /path/to/your-repo
/path/to/git-commit-review/.venv/bin/aicr install
```

### 2. 写规范

在业务仓库根目录建 `.ai-review/rules.md`，把团队规范写进去即可，**不需要任何配置文件**：

```markdown
<!-- rule: exception-swallow -->
### 禁止吞掉异常

捕获异常后必须抛出、补偿或记录 error 日志，禁止 catch 后什么也不做。

<!-- rule: magic-number -->
### 禁止魔法值

业务含义的数字必须定义为常量。
```

`<!-- rule: xxx -->` 是规则 ID，用来在报告里定位、也用来写忽略指令。没写 ID 的段落仍会参考，但只会产生警告，不会阻断。

### 3. 正常提交

```bash
git add .
git commit -m "feat: 订单改价"
```

违规时你会看到：

```
✗ AI Review 未通过：1 处阻断 / 1 处警告
规范: rules.md @d5f04da9 · 耗时 6.2s

────────────────────────────────────────────
[阻断 1/1]  src/main/java/com/demo/order/OrderProcess.java:21
  规则: exception-swallow  (.ai-review/rules.md)

      19 |         } catch (Exception e) {
      20 |             log.info("update order failed");
    > 21 |         }
    ↑ 捕获 Exception 后只打 info 日志，异常被吞掉
      建议: 改为 log.error 并抛出 BizException

共 1 处阻断 / 1 处警告
  修复后重新 git add 并提交；确需跳过时使用 git commit --no-verify
```

代码片段是从**暂存区**取的，就是你即将提交的那份内容。

## 常用操作

| 我想…… | 怎么做 |
|---|---|
| 放过某一行 | 该行加注释 `// ai-review-ignore`，或指定规则 `// ai-review-ignore: magic-number` |
| 放过整个文件 | 文件里加 `// ai-review-ignore-file: magic-number` |
| 临时跳过一次 | `git commit --no-verify` 或 `GIT_REVIEW_SKIP=1 git commit ...` |
| 看这次会发什么给模型 | `aicr run --dry-run`（不消耗 token） |
| 换模型 / 内网网关 | `.ai-review/config.toml` 里写 `[model] base_url` + `model`（见文末） |
| 不想要这个 hook 了 | `aicr uninstall`（会还原你原来的 hook） |

## 常见问题

**没配 API Key 会怎样？**
会打印警告并**放行**提交，不会卡住你。想改成"失败即拦截"，在配置里设 `[policy] on_api_error = "block"`。

**提交变慢了？**
正常 3～8 秒。`--dry-run` 不调模型所以是毫秒级。想更快可减少规范篇幅，或调小 `review.max_diff_lines`。

**误报太多？**
先确认规范写得够具体；其次用行内忽略指令局部放过；`[policy] min_confidence` 调高（如 0.8）可以让不确信的结论只出警告。

**公司不允许代码外传？**
把 `base_url` 指向内网部署的 OpenAI 兼容网关（vLLM / one-api 等），其余代码不用改。密钥、私钥类内容在发送前就会被本地拦截。

## 完整配置（不是必读）

不建 `.ai-review/config.toml` 也能用，所有项都有默认值。需要调整时复制 [.ai-review.example.toml](.ai-review.example.toml) 并只保留要改的部分。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `model.base_url` | `https://api.deepseek.com` | OpenAI 兼容接口 |
| `model.model` | `deepseek-chat` | 模型名 |
| `model.api_key_env` | `DEEPSEEK_API_KEY` | Key 只从环境变量读，配置里写明文会直接报错 |
| `model.timeout_seconds` / `retries` | `20` / `1` | 单次超时与重试 |
| `model.proxy` | 无 | 如 `http://127.0.0.1:7890` |
| `conventions.sources` | 自动用 `.ai-review/rules.md` | 多规范文件时显式列出 |
| `conventions.on_unavailable` | `warn` | 规范加载失败时 `warn` 放行 / `block` 拦截 |
| `conventions.max_tokens` | `4000` | 规范预算，超出截断并提示 |
| `review.include` / `exclude` | 常见代码后缀 / 排除 target、build 等 | 审查范围 |
| `review.max_diff_lines` | `400` | 超过后按 `on_large_diff` 处理 |
| `review.on_large_diff` | `new_files_only` | `new_files_only` / `split` / `skip` |
| `review.context_lines` | `3` | 报告片段上下文行数 |
| `policy.on_api_error` / `on_invalid_json` | `warn` | 模型故障时放行还是拦截 |
| `policy.min_confidence` | `0.6` | 低于该值的阻断降级为警告 |
| `report.show_snippet` | `true` | 是否展示代码片段 |
| `ignore.inline_directive` | `ai-review-ignore` | 忽略指令关键字 |

退出码：`0` 通过 / `1` 有阻断或命中密钥 / `2` 配置或模型故障且策略为拦截。

## 当前范围

已实现：本地 pre-commit 拦截、仓库内规范、密钥扫描、忽略指令、超长 diff 分批、模型故障策略。

尚未实现：GitLab 远程规范源与缓存、MR/CI 模式、飞书/钉钉告警、审查结果缓存、`ignore.toml` 存量豁免。
