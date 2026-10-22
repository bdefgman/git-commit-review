# git-commit-review

提交前用 AI 按项目自己的规范检查代码，不合规直接阻止 `git commit`。

## 功能

- **提交前拦截**：作为 `pre-commit` hook 运行，有违规就 `exit 1`，提交不产生
- **只看暂存区**：审查 `git diff --cached`，不受工作区未暂存改动影响
- **规则可追溯**：每条问题带规则 ID 和规范文件出处
- **代码片段可信**：片段从暂存区内容截取，不是模型复述的内容
- **防模型幻觉**：编造的规则会被丢弃，越界行号标记「位置待人工确认」
- **零配置可用**：仓库里有 `.ai-review/rules.md` 即可，不需要配置文件
- **密钥扫描**：命中 AWS Key、`sk-*`、私钥等直接阻断，且不发送给模型
- **误报可控**：支持行内/文件级忽略指令，低置信度结论自动降级为警告
- **故障不阻塞**：模型超时或断网默认放行并提示（可配置为强制拦截）
- **兼容已有 hook**：自动备份并串联原有的 `pre-commit`，卸载时还原

## 用法

### 1. 安装工具

```bash
cd /path/to/git-commit-review
python3 -m venv .venv && .venv/bin/pip install -e .
export DEEPSEEK_API_KEY=sk-xxxx     # 建议写进 ~/.zshrc
```

### 2. 在业务仓库安装 hook

```bash
cd /path/to/your-repo
/path/to/git-commit-review/.venv/bin/aicr install
```

### 3. 写规范

在业务仓库根目录建 `.ai-review/rules.md`：

```markdown
<!-- rule: exception-swallow -->
### 禁止吞掉异常

捕获异常后必须抛出、补偿或记录 error 日志，禁止 catch 后什么也不做。

<!-- rule: magic-number -->
### 禁止魔法值

业务含义的数字必须定义为常量。
```

`<!-- rule: xxx -->` 是规则 ID，用于报告定位和忽略指令；没有 ID 的段落只产生警告，不会阻断。

### 4. 正常提交

```bash
git add .
git commit -m "feat: 订单改价"
```

违规时输出：

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

## 常用命令

| 命令 | 说明 |
|---|---|
| `aicr install` | 安装 pre-commit hook |
| `aicr uninstall` | 卸载 hook 并还原原有 hook |
| `aicr run` | 手动审查当前暂存区 |
| `aicr run --dry-run` | 只打印将要发送的内容，不调用模型、不消耗 token |
| `aicr run --debug` | 把 prompt 和模型原始输出写入 `~/.cache/git-commit-review/debug/` |
| `aicr version` | 查看版本 |

## 忽略与跳过

```java
// ai-review-ignore                     忽略该行（及下一行）的全部问题
// ai-review-ignore: exception-swallow  只忽略指定规则
// ai-review-ignore-file: magic-number  忽略整个文件
```

```bash
GIT_REVIEW_SKIP=1 git commit -m "..."   # 临时跳过一次
git commit --no-verify -m "..."         # 同上
```

## 可选配置

不建配置文件也能用。需要改模型或调整策略时，复制 [.ai-review.example.toml](.ai-review.example.toml) 到业务仓库的 `.ai-review/config.toml`，只保留要改的项：

```toml
[model]
base_url = "https://api.deepseek.com"   # 换内网网关改这里
api_key_env = "DEEPSEEK_API_KEY"

[policy]
on_api_error = "block"                  # 模型故障时拦截而不是放行
```

退出码：`0` 通过 / `1` 有阻断或命中密钥 / `2` 配置或模型故障且策略为拦截。
