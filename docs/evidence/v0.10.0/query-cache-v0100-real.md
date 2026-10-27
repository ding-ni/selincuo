# 0.10真实1597成果只读查询验收

结果：metadata、1597单元空间帧和731日日末单元过程，与冻结0.9查询模块的同请求结果逐值相同。冻结request/result/checkpoint/hourly-manifest的大小、mtime和SHA前后不变。SQLite以URI `mode=ro`及`PRAGMA query_only=ON`读取，Zarr由既有`open_arrays`以`mode=r`打开，没有运行模型或写原工作区。

## 时延实测

| 0.10步骤 | 首次调用/秒 | 后续5次中位数/秒 | 后续5次p95最近秩/秒 |
|---|---:|---:|---:|
| metadata | 6.9110 | 0.3439 | 0.3762 |
| 空间帧 | 1.1255 | 0.3417 | 0.3788 |
| 单元0、swe、2024—2025日过程 | 5.4635 | 0.5358 | 0.5518 |

首次metadata前显式清空应用source缓存；随后frame与series共享该source/time轴，但仍需首次读取各自数值块。**单元首次过程5.46秒仍未达到约2秒目标。** 不以热查询p95抵消该缺项，也不宣称冷启动性能通过。

旧0.9参考模块在相同进程后执行6轮，其metadata首次15.74秒、后续中位0.581秒，frame后续中位0.399秒、series后续中位0.597秒。这两个序列顺序执行、操作系统缓存未清理或测量，不能用首值相除得出性能提升倍数或认为公平冷对照。

cacheInfo为1个Zarr句柄、1条source entry（容量8）、266696字节时间轴、0份legacy数组。50毫秒间隔取得698次RSS样本，进程最大采样154.26MiB；该进程含新旧模块与验证开销，采样不是连续峰值，也不证明单查询500MiB上限。两年单元查询731行，空间帧1597值均精确一致。

本次没有清理OS缓存、没有并发大备份，也没有测HTTP排队及浏览器。后续仍需在可重复条件下测应用miss、OS缓存状态、重任务并发与更细粒度内存峰值。

复现脚本：`verify_query_cache_v0100.py`。完整JSON：`query-cache-v0100-real.json`；原始逐次输出：`query-cache-v0100-real.log`。源码SHA、所有36次调用时长、冻结文件SHA与数值等价断言均已保存。当前模块源码冻结路径为本轮`next-stage/backend/result_query.py`，参考为本轮`stage/backend/result_query.py`，不依赖部署中的目标同名文件。
