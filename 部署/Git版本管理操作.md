# Git版本管理操作

## 已有配置

| 项目 | 当前值 |
|---|---|
| 本地目录 | E:\Project\色林错系统 |
| Git版本 | 2.52.0.windows.1 |
| GitHub认证账号 | ding-ni，SSH验证已通过 |
| 提交署名 | adiang，邮箱沿用本仓库已配置的用户邮箱 |
| 远程origin | git@github.com:ding-ni/selincuo.git |
| 网页 | https://github.com/ding-ni/selincuo |
| 分支与上游 | main → origin/main |

提交署名与GitHub登录账号是两个概念。配置仅保存在本仓库，不需要再运行初始化、创建远程、重命名分支或设置全局身份。不得记录或提交SSH私钥、令牌、真实环境密钥。

## 新对话开始时

在上述工作目录依次检查：

```powershell
git status --short --branch
git remote -v
git log -5 --oneline
```

已有未提交改动应先读取并理解，保留用户工作。需要确认远程新提交时运行 `git fetch origin`，再比较 `git log --oneline --left-right main...origin/main`。仅在工作区干净、确认本地仅落后且无分叉时使用 `git merge --ff-only origin/main`；存在分叉时先分析差异，不强制推送、不自动重写历史。fetch可能更新Git引用，但不应清理本地文件，也不附加清理选项。

## 阶段完成时

1. 更新 `文档/阶段操作记录.md`，注明实际完成内容、验证、遗留问题、下一步及提交状态。提交哈希可在下一次记录中引用；不能把尚未成功的推送写为成功。
2. 运行与修改有关的检查。只做文档更新时检查链接与差异即可；应用和模型实施时补充对应验证。
3. 用 `git diff` 和 `git status --short` 核对范围。新文件还需读取内容；原始数据、签章件和核查全文继续排除在提交之外。
4. 明确列出本阶段文件再暂存，检查暂存差异，然后提交。以下命令以本阶段只修改操作记录为例，实际文件清单必须根据差异调整，不盲目暂存整个目录。

```powershell
git add -- '文档/阶段操作记录.md'
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m '记录本阶段实现与验证结果'
git push origin main
```

5. 推送成功后检查同步情况：

```powershell
git status --short --branch
git rev-parse HEAD
git ls-remote --heads origin main
```

本地HEAD应与远程main哈希一致，工作区状态应符合预期。上游已配置，日常也可直接 `git push`。用户已指定本项目GitHub仓库并要求记录接续操作，按阶段同步相关代码和文档；不因此推断对其他仓库、公开发布科研原始数据、删除远程分支或强制覆盖历史的授权。

## 异常处理

- SSH排查可用 `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -T git@github.com`。GitHub返回认证成功但不提供Shell时通常退出码为1，应结合消息判断，不以退出码独断认证失败。
- 若认证失败，不打印私钥，不把用户发来的孤立SHA256字符串当成登录凭据或直接替换主机校验信息。
- 若推送被拒绝，保留本地提交，读取拒绝原因并检查远程；不使用强制推送，不删除或重建仓库。
- Git没有待提交改动时无需制造空提交。网络失败不等于工作未保存；在记录和交付说明中区分“本地已提交”与“远程已同步”。

## 本地资料与恢复边界

`.gitignore`已排除原始资料、外部衍生成果、标准数据实体、运行成果、签章PDF、核查全文/图页、外部参考代码、依赖与本地密钥。被忽略的文件仍在磁盘上，Git不是这些资料的备份。不要用强制暂存绕过规则。

当前同一工作区包含完整本轮复制资料和核查清单，新对话直接使用即可。仅从GitHub克隆到另一台电脑时不会自动获得这些数据或空目录，应根据项目导读恢复必要目录并另行取得资料，不能把缺失资料误判为已完成导入。任何备份或恢复均遵守禁止删除和原始资料只读的规则。
