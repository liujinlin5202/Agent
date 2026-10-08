# INC-20261008-QD02：agent-run hollow exec（复合 run 命令被静默截断）

> 2026-10-08 发现并当日热修。本文是复盘留底：时间线、根因、为什么层层观测
> 全部失明、修复与验证、遗留加固项。发现者：编辑部 M1 部署的验收环节
> （M1 计划文档 §5 部署步骤 3 的「服务器侧验收」触发）。

## 1. 时间线

| 时刻（北京） | 事件 |
|---|---|
| 10-08 上午 | tech-digest 割接秋坞 job 模式（v-2610080401），门禁「15s 通过」后武装、systemd 停用 |
| 14:40 | 编辑部 M1 部署（v-2610080640），门禁 2/2「通过」、CronJob×4 武装 |
| 14:43 | 手动触发 ingest 验收：pod 23s exit 0、零 python 输出、数据盘无 pool_items——假成功暴露 |
| 14:50 | debug pod 用 `echo A; python --version; echo B` 复现：只有 A 执行，实锤根因 |
| 15:10 | 镜像热修 v1（去内层 exec）；复测撞上 clone 网络抖动（129s 超时，即上午试跑挂 3h 的同款） |
| 15:30 | 镜像热修 v2（+clone 3 次退避重试 / --depth 1）；ingest 班全链路 62s 跑通，池 64+13 入库 |

## 2. 根因

镜像 `qdock-runtime:python3.11` 的 `/usr/local/bin/agent-run` 末行原为：

```sh
exec sh -c "exec $QDOCK_REPO_RUN"
```

POSIX shell 语义：`exec` 以目标命令**替换当前进程**。对复合命令
`pip install ... && cd tech-digest && python main.py ingest`，sh 在执行第一段前
就把自己整个替换成 pip——`&&` 链的后续段从未被解析执行。结果：

- 实际只跑 `pip install -q requests beautifulsoup4`（约 15-20s，正常退出）
- exit 0，k8s Job 状态 Complete，平台 cron 台账「成功」，告警脚本无异常
- **python 主程序在平台 job 模式下从未运行过**（今早割接的 daily/star/weekly
  同样空心；首个真实 CronJob 触发在次日 09:15，故当日无人察觉）

## 3. 为什么四层观测全部失明

| 观测层 | 为何没拦住 |
|---|---|
| 门禁（evals dry-run） | assert 只校验触发 API 的 HTTP 200（`kind: status`），不校验 pod 内进程行为；且 pod 的 exit 0 来自 pip |
| k8s Job 状态 | pip 退出码 0 = Complete，语义上「成功」 |
| crontab 失败告警（qdock_cron_alert.sh） | 查的是 cron 台账的成败字段——台账记的就是假成功 |
| run_log（业务自检） | 由 python 写入；python 没跑，run_log 自然空白，但没人对「run_log 缺席」设防 |

**教训（已固化认知）**：门禁校验「触发成功」≠「业务成功」；唯一可信的业务
信号是 run_log 尾条时间戳。见 §6 加固项。

## 4. 修复

两处，均已入镜像（digest `f94f7cfe`）并收编入仓（`qdockd/deploy/agent-run.sh`，
此前该脚本只存在于镜像内、无 tracked source）：

```sh
# 修复 1（根因）：去内层 exec，复合命令恢复执行、退出码正常传播
exec sh -c "$QDOCK_REPO_RUN"

# 修复 2（防御，同次实锤的另一故障面）：clone 浅化 + 3 次退避重试
# GitHub 直连在 pod 内偶发 443 超时 129s（上午试跑挂 3h、下午复测各中一次）
git clone --quiet --depth 1 ...   # 无 REF 路径；REF 分支/标签浅克隆，SHA 回退全量
# 3 次失败 → exit 1（可见失败，替代无限挂起）
```

修复操作走既有播放簿：原始镜像从 containerd 导出还原（一次失误的 docker
commit 曾把临时容器 ENTRYPOINT 带进 tag，靠 ctr 侧留存的原镜像救回）→
Dockerfile COPY 方式重建（保配置）→ `docker save | k3s ctr -n k8s.io images import`。
未重启 k3s/docker，未触碰在跑工作负载。

## 5. 影响面与验证

- 受影响：**repo 模式且 run 为复合命令的 job 租户**。当前唯一此类租户 =
  tech-digest（4 任务全部空心）。image 模式（argv 直跑）与单命令 repo 模式
  （如 demo-hello）不受累；xiaoqiu/ops/sandbox 走独立镜像，无关。
- 修复后实测（生产，真实 pod）：
  - ingest 班：全链路 62.2s，10 源 97 news + 13 trending，池 64+13 入库
    （33 条撞近 14 天已发历史被正确拦截），run_log `ingest ok`，WAL 生效；
  - clone 失败场景（修复 v1 复测时自然发生）：job 正确 Failed、退出码可见——
    可见失败替代静默假成功，告警链路从此有效。
  - daily `--dry-run` 真实 pod 试跑：见 M1 文档验证记录。

## 6. 遗留加固项（不在本次范围内，记账待做）

1. **告警加固**：qdock_cron_alert.sh 增加一类校验——对 job 租户抽查
   `run_log` 尾条时间戳（如 >26h 无该任务的 run_log 条目即告警），把
   「job 成功但业务没跑」类故障纳入告警视野。
2. **门禁加固**：job_run eval 可选 `assert: run_log`（要求门禁试跑结束时
   run_log 出现本次试跑条目），让门禁真正覆盖业务行为。
3. agent-run 的 pip 段同样是网络依赖（镜像内未加重试）——若线上出现 pip 偶发
   失败，按同款退避模式补。
