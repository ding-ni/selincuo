# CDS网页可登录、数据/API下载停用：恢复说明

2026-10-22用户确认：CDS网站可以正常登录，但下载受限。当前API实际返回403和 `User has been disabled. Please contact the administrator.`。该信息不能用于确定停用原因；原因及解除方式需ECMWF/CDS支持核查。

## 恢复步骤

1. 使用同一账户登录 [CDS网站](https://cds.climate.copernicus.eu/)，保留下载错误及时间截图。不要将API令牌包含在截图中。
2. 从 [ECMWF支持门户](https://support.ecmwf.int/) 进入CDS/Copernicus数据支持；若支持门户当前页面无法加载，可从CDS网站的帮助/支持入口进入。门户具体表单名称可能调整，以实际页面为准。
3. 提交工单，请其核查并恢复数据/API访问。下面提供可直接复制的英文内容。账户邮箱只填在官方表单，API令牌和密码不填入工单正文。
4. 等待官方明确恢复。若仅提示许可未接受，则在ERA5-Land数据集页面按官方要求确认许可；这与“User disabled”是不同问题，接受许可不保证解除停用。
5. 官方恢复后，在本机检查CDS个人页面的API配置说明。若官方要求更新令牌，将最新配置写入当前Windows用户目录 `.cdsapirc`；不在聊天中发送令牌。平台下次任务重新读取配置，无须重建数据库。
6. 在色林错平台“模型与资料”核验本地文件，点击“继续补齐缺失月份”。下载逐月串行提交，并复用同条件请求与已校验断点。账户仍受限时会停止，不连续重试。

能登录网站表明登录功能仍可用，不等同于下载/API权限正常。不要为绕过停用批量注册账户；优先恢复当前账户并遵守支持方的请求限制。

## 英文工单模板

Subject: CDS website login works, but ERA5-Land API/download access returns “User has been disabled”

Hello ECMWF/Copernicus Support,

I can sign in to the CDS website with my account, but my ERA5-Land data/API requests return HTTP 403 with the message:

“User has been disabled. Please contact the administrator.”

Could you please investigate why data/API access is disabled and advise how to restore it? Please let me know if any action or change to my request pattern is required.

Dataset: reanalysis-era5-land (ERA5-Land hourly data)

Variables: 2m temperature, total precipitation, potential evaporation

Period needed: 2023–2025, with December 2022 as an initialization buffer

Area: North 33.5°, West 87.2°, South 29.9°, East 92.4°

Purpose: historical hydrological modelling and lake water-balance analysis for Selin Co, China.

My earlier requests encountered queue/cost limits. The downloader has now been changed to submit one monthly request at a time, preserve and reuse existing request IDs, and stop immediately when the service reports an access restriction. No new requests will be submitted while access remains disabled.

The issue was observed on 21–22 October 2026, China Standard Time (UTC+8).

I have not included any API token or password in this ticket. I can provide a relevant request ID through this official support channel if needed.

Thank you.

## 当前资料缺口

37个所需原始UTC月份中已有10个月完整：2022-12、2023-01至04、2024-01及12、2025-10至12；其余27个月未齐。平台保留原件、已登记输入和断点。账户恢复前仍可使用已登记连续片段完善大屏和历史计算；完整年度率定验证需真实驱动补齐后开展。

如果另有已合法下载的相同产品原始NetCDF，可直接上传或提供本机目录，不必重新下载已取得资料。
