# 0.10安装验收补充审核

只读审查，不修改目标工程或发布默认。0.9通过证据保持原版本范围。

## 最小必须新增的安装验收

`verify_packaged.py`的原18流程覆盖真实旧业务2025复算、续算、导出、备份、恢复，但不能证明0.10新增评价writer或expectedReleaseId适配已在冻结程序中可用。

本轮新增`verify_v0100_installed_extra.py`，必须连接实际安装的0.10 EXE服务，传入明确app-root与隔离workspace。使用已有真实2025成功simulation，不插入合成分数或伪造候选。检查：

- health、release-info、source-manifest、release-manifest、config均为0.10.0；应用路径存在真实EXE。
- 安装源码`backend/evaluation_archive.py`真实SHA与两份manifest及归档report.implementationSHA256一致。frozen路径必须从安装ROOT/backend读取，不能依赖PyInstaller临时目录的__file__。
- expectedReleaseId只读计划为reuse且不生成计算，arbitraryRevisionRuntimeAvailable保持false。
- 后台评价真实成功，window/window仍为not-comparable，准入失败、严格续算/完整产物等为unknown，默认发布逐字段不变。
- 不可变方案修订与评价同事务持久化，冻结输入及身份指向真实已复算candidate，runtimeEvidence为空，不能因本次软件测试凭空标科学通过。
- 请求幂等、重复归档复用、compact tasks/列表/详情、3项完整产物下载大小及SHA、伪造scores/checks返回400。
- 可选`--verify-runtime-compute`只做一次通过expectedReleaseId派发的真实2025复算，与原真实标准路径逐日比较并核验实际安装源码模型身份。未启用则只声明计划适配通过，不声明该计算路径数值通过。

原18流程最后恢复成功会将当前进程置restart_required，正确拒绝后续写入。因此额外验收必须在恢复前执行，或退出并重新启动安装程序到明确的独立恢复/原验收workspace后执行。不得连接原8882业务库运行。

## 发行路径与证明边界

1. 新建0.10 runtime、资源、Verification安装及验收目录。不得把0.9 proof换名或用只改版号的旧EXE。
2. assemble必须包含backend/evaluation_archive.py、release_runtime.py、result_query.py实际源码与对应manifest；当前所有backend.py纳入legacy model_identity，增加模块会产生真实新身份，不能冒用旧checkpoint。
3. seed须继续干净并绑定同一默认发布、冻结输入及空间；研究任务、测试评价、临时状态不能进入发行种子。
4. 实际已安装manifest/proof必须同时写version0.10.0及candidateUpgradePassed=false；只有新安装流程通过后才能更新installationAcceptance。
5. portable验证恢复生成的`config/active-workspace.json`必须从发行目录移出保存证据，确认发行目录最终不存在该文件。旧v090 helper在retained已存在且相同时仅assert可能遗留active，0.10不能复用这一行为。
6. installer和release-manifest一致排除workspace、restored-workspaces、logs和active-workspace。仅排除manifest不会阻止安装器实际带入文件。
7. 完成证明后只允许同步文档/明确说明及manifest；若backend或EXE再变，应重新实际软件验收。

## 当前脚本CLI

`python verify_v0100_installed_extra.py --app-root <实际安装application> --workspace <独立验收workspace> --url http://127.0.0.1:<端口> --output <新证据JSON> [--candidate-id <真实复算任务>] [--verify-runtime-compute]`

脚本已语法检查，等待主代理提供实际安装服务参数执行；这份审核本身不是安装通过证明。

## final2实际安装验收记录（2026-10-27）

上节末尾的“等待”描述的是审核脚本准备时的历史状态。本节记录后续最终实测，不改写先前审核和失败证据。普通Setup正在组装，本节不将其提前写成已发行。

最终实际安装目录为`F:/色林错预警预报平台/build/installed-v0100-final2-remediation-verification-20261027/application`，恢复工作区为其`restored-workspaces/restored-6a5970d72b21`。实际EXE在PATH只保留Windows/System32环境下运行，SHA256为`355993549a01387a5748073c12aa3343eac432687682c671108a2982214508e2`。原18项真实业务流程与后续额外验收均通过；前两轮失败包与失败证据保持独立保存。

- [额外实际安装证明](installed-extra-verification.json)：passed=true、version=0.10.0，实际已安装源码、source-manifest及release-manifest摘要一致，writer实现摘要取自安装ROOT源码，三份下载产物大小与SHA匹配。
- 严格`expectedReleaseId`真实2025复算使用原始观测快照`6909ce0eb71804dca6ca9d317846016f3dae342f87a08de9cb6a771b2b9f088e`、33份来源；实际冻结请求与发行快照逐值一致。365日湖位、杜加及总来水相对真实基准最大差均为0，模型实现保留真实新身份。
- 评价归档明确排除2025-04-09窗口初始化日，两个结果的冻结窗口来源可核验；2025湖位共同样本255日，RMSE、MAE、偏差与默认冻结年度指标一致。下载配对CSV独立确认255条且不含该初始化日。修订关联、请求幂等、归档复用、compact任务与伪造分数/checks返回400均通过。
- [安装离线界面证明](installed-offline-ui.json)：passed=true，八页及单元过程完成；JavaScript错误、HTTP错误、外部请求均为0。此为本机独立安装及受限PATH/浏览器网络条件验收，不等于另一台无开发环境机器认证。
- 默认发布前后仍为`RELEASE-protected-rev2`与`BUILTIN-historical-rev2-39192cc713c5`。评价保留not-comparable、科学检查unknown和准入失败，不将软件测试转换为科学升级结论。

只读核查发行资源根`F:/色林错预警预报平台/build/release-v0100-final2-remediation-20261027/SelincoPlatform`时未发现`logs`目录。便携验收生成的备份/恢复目录及active-workspace指针由`prepare_delivery_v0100.py --verified`的排除或移出逻辑处理，最终发行清单与Setup仍由主流程收口，不能仅凭本段替代最终打包核验。
