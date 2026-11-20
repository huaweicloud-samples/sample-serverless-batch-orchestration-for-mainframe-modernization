# 部署指导

本文从零开始，把整套栈部署到可验证状态。所有命令与参数名均取自本仓库现有 `infra/*.tf`、`src/` 与 `scripts/`。

> **部署形态是两次 apply**：第一次建出集群与其余资源；中间用 kubectl 建命名空间、RBAC 与配置项，并取回三项集群访问参数；第二次 apply 把它们写进编排函数。顺序不可调换。

---

## 0. 准备

### 0.1 工具与版本

| 工具 | 要求 | 出处 |
|-|-|-|
| Terraform | `>= 1.5.0` | `infra/versions.tf` 的 `required_version` |
| huaweicloud Provider | `1.96.1` | `infra/versions.tf` 的 `required_providers` |
| Python | 本地 3.9 及以上 | 函数 runtime 固定 `Python3.9` |
| kubectl | 与集群版本匹配 | 应用 `src/manifests/` |
| Docker（或等效容器构建工具） | 构建并推送作业镜像 | `src/job/Dockerfile` |
| obsutil（可选） | 上传落地文件、列举检查点 | 验证章节使用 |

自检：

```bash
terraform version        # Terraform v1.5.0 或更高
python --version         # Python 3.9+
kubectl version --client
docker version --format '{{.Server.Version}}'
```

### 0.2 账号与凭据

在华为云「我的凭证」页面获取 Access Key ID 与 Secret Access Key，导出到部署环境；不要把凭据写入代码或提交到仓库。

```bash
export HW_ACCESS_KEY=<HW_ACCESS_KEY>
export HW_SECRET_KEY=<HW_SECRET_KEY>
```

`infra/providers.tf` 优先读环境变量，`access_key` / `secret_key` 变量留空时不传值；`security_token` 用于临时凭据场景，同样留空即可。区域固定 `cn-north-4`（写在 `infra/locals.tf`），不需要另外设置区域变量。

需要开通的服务：FunctionGraph、CCE、CCI、OBS、LTS、SMN、CES、SWR、VPC、IAM。两个 IAM 委托由栈创建（`batch-dev-functions-agency` 承担给 `op_svc_cff`，`batch-dev-cluster-agency` 承担给 `op_svc_cce`），无需手工建。

### 0.3 区域与可用区约束

- 区域：`cn-north-4`，不可改；端点后缀取 `cloud`（默认 `myhuaweicloud.com`）。
- 可用区：`subnet_a_az = cn-north-4a`、`subnet_b_az = cn-north-4b`，CCI 每 Pod 网络与节点池都落在对应可用区。
- 集群时区 `UTC+08:00`；营业日判定用 `schedule_timezone`（默认 `Asia/Shanghai`）。

### 0.4 网络前提

kubectl 与 `export_k8s_access.sh` 必须在 **VPC 内的主机**上运行——默认拓扑不给集群 API Server 挂公网地址（`enable_api_server_public_access = false`）。若确实需要从 VPC 外访问，把该开关置 `true` 后重新 apply。

---

## 1. 离线校验（不触碰云端）

在仓库根目录执行。三条校验都应在申请任何资源之前跑通。

### 1.1 函数用例

```bash
python -m pytest src/functions -q
```

预期末行输出：

```text
292 passed in 0.64s
```

这 292 条用例覆盖编排流、状态读写、计划构建、事件解析、两个校验判定、OBS 存储层与签名/HTTP 层。用例全部走替身，不访问云端。

### 1.2 构建函数代码包

```bash
python scripts/package_functions.py
```

预期输出：

```text
函数代码包构建完成：
  orchestrator  infra/.build/batch-orchestrator.zip      20 个条目   42.4 KiB  sha256 fa81bffe1468
  checks        infra/.build/batch-checks.zip            20 个条目   42.4 KiB  sha256 52b683323878
根目录入口：index.py（处理器名见 infra/functions.tf）
```

`sha256` 前缀随源码内容变化，产物路径不会变。两个包的路径与 `infra/functions.tf` 里 `huaweicloud_obs_bucket_object.*.source`（`${local.build_dir}/batch-orchestrator.zip`、`${local.build_dir}/batch-checks.zip`，`build_dir` 在 `infra/locals.tf` 定义为 `${path.module}/.build`）逐字符一致——脚本会主动比对，不一致直接报 `构建失败：产物路径 ... 与部署声明 ... 不一致`。

只构建一个包：

```bash
python scripts/package_functions.py --only orchestrator
python scripts/package_functions.py --only checks
```

包内布局的硬性要求（不满足即构建失败）：根目录必须有 `index.py`（只做转发），包内必须完整收录 `functions/orchestrator`、`functions/checks`、`functions/shared` 三个子包，不收录测试、缓存、字节码与原生扩展。

### 1.3 配置校验

```bash
cd infra
terraform validate
```

`terraform validate` 是本地校验，需要 Provider 插件已在本地（先执行 `terraform init`，或指向本地插件缓存）。预期输出：

```text
Success! The configuration is valid, but there were some validation warnings as shown above.
```

---

## 2. 手工前置：构建并推送作业镜像

**这一步必须在 apply 之前完成。** `infra/swr.tf` 只创建 SWR 组织与私有仓库，不构建也不推送镜像；`infra/locals.tf` 的 `batch_image` 由 `huaweicloud_swr_organization.main.login_server` 与 `swr_organization` / `swr_repository` / `swr_image_tag` 拼出，直接写进每个 Job 的 `image` 字段。仓库里没有对应标签时，作业停在 `ImagePullBackOff`，而 apply 本身不会报错。

镜像上下文是 `src/job/`，`Dockerfile` 基于 `python:3.11-slim`，依赖只有 `esdk-obs-python==3.26.6`，以 UID/GID `10001` 非特权运行。入口 `entrypoint.sh` 把 Job 传入的步名转成 `python3 -m batch_job <STEP>`（缺省为 `JOB1`），**容器的退出状态码就是批处理程序的退出状态码**，编排层据此路由下一步。

```bash
# <registry host> 与 SWR 登录口令在 SWR 控制台获取；
# 第一次 apply 之后也可以用 terraform output image_registry_host 读取
docker build -t <registry host>/batch-modernization/batch-stub:1.0.0 src/job
docker login -u <用户名> -p <SWR 访问密钥> <registry host>
docker push <registry host>/batch-modernization/batch-stub:1.0.0
```

预期输出（推送阶段末行）：

```text
1.0.0: digest: sha256:<摘要> size: <N>
```

三点必须核对：

1. 地址三段与 `terraform.tfvars` 的 `swr_organization` / `swr_repository` / `swr_image_tag` 完全一致，否则 Job 拉到的是另一个标签。
2. `<registry host>` 必须与 `terraform output image_registry_host` 相同——组织名与登录服务器由平台分配。
3. 节点侧经 `batch-dev-cluster-agency` 的 `SWR Admin` 角色拉镜像，**不需要创建 imagePullSecret**。

---

## 3. 准备部署参数

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars` 已被根 `.gitignore` 与 `infra/.gitignore` 忽略，不会被提交。

### 3.1 必填：节点登录方式

`huaweicloud_cce_node_pool.workers` 有一条 `precondition`：

```text
Set node_pool_key_pair or node_password: worker nodes require one of the two.
```

两者都为空时，plan 阶段就会失败（这个校验只在建节点池时才暴露）。二选一：

```hcl
node_pool_key_pair = "<已导入的密钥对名称>"   # 取的是名称，不是公钥字符串
```

或者走环境，避免把口令写进文件：

```bash
export TF_VAR_node_password="<节点登录密码>"
```

### 3.2 必填：三项集群参数留空

```hcl
k8s_api_server             = ""
k8s_ca_cert                = ""
k8s_service_account_token  = ""
```

第一次 apply 时集群还不存在，这三项无法填写。

### 3.3 建议确认的取值

| 变量 | 默认值 | 说明 |
|-|-|-|
| `environment` / `name_prefix` | `dev` / `batch` | 拼出 `full_name = batch-dev`，进入所有资源名；`environment` 受正则 `^[a-z][a-z0-9-]{1,15}$` 约束 |
| `notification_email` | `""` | 填运维邮箱（例如 `"<EMAIL>"`）才会创建 email 订阅；留空时主题仍创建、告警仍可投递 |
| `cluster_spec` | `cce.s1.small` | 支持至 50 个节点 |
| `node_flavor` / `node_count` | `c7.large.2` / `2` | 2 vCPU / 4 GiB × 2 台，对应两个并发作业槽位 |
| 节点盘 | 40 GB SAS + 100 GB SAS | `node_root_volume_*` / `node_data_volume_*`，数据盘容纳镜像缓存 |
| `daily_cron` | `0 0 2 * * ?` | 六段：秒 分 时 日 月 周 |
| `resume_interval_seconds` | `180` | 检查点续跑间隔 |
| `max_input_age_days` | `2` | 落地文件最大龄期（UTC 日历天） |
| `stage_budget_seconds` | `900` | 单趟时间预算；函数超时 = 该值 + 300 |
| `job_deadline_seconds` | `7200` | 单容器硬截止 |
| `job_cpu_*` / `job_memory_*` | `500m` / `1`、`1Gi` / `2Gi` | 一槽一个核，半核请求使两槽能落同一节点 |
| `cci_enabled` | `true` | 关掉则整条溢出通道不创建，相关输出为空串 |
| `enable_api_server_public_access` | `false` | 建议保持默认 |
| `enable_ces_alarms` | `false` | 自定义指标告警规则属选装项，见第 8 节 |
| `enable_custom_metrics` | `true` | 大盘数据点来自这里，与上一条无关 |
| `agency_extra_roles` | `[]` | 只追加已确认名称的项目级角色 |

> **`infra/terraform.tfvars.example` 与 `infra/variables.tf` 一致，两处都是 `false`。** 自定义指标命名空间上的告警规则需要在云监控侧创建，沿用样例的 `true` 会让 apply 在创建 `ces_alarmrule` 时受阻。第 8 节给出两条路径，需要告警规则时再显式改成 `true`。

### 3.4 检查 plan 是否可预测

```bash
terraform init
terraform plan
```

预期以 `Plan: <N> to add, 0 to change, 0 to destroy.` 结束，且没有 `precondition` 失败。

---

## 4. 第一次 apply：建出集群

```bash
terraform apply
```

输入 `yes` 确认后，预期结束于：

```text
Apply complete! Resources: <N> added, 0 changed, 0 destroyed.
```

产出内容：VPC 与两个私有子网、安全组与 7 条规则、CCE 集群与节点池、CCI 命名空间与每 Pod 网络、OBS 桶（含策略与阻止公共访问）、SWR 组织与仓库、三个函数与两个定时触发器、LTS 日志组与两条日志流、SMN 主题、CES 大盘与两个组件、两个 IAM 委托。两个代码包在这一步上传到 OBS 的 `functions/` 前缀下。

集群与节点池创建需要若干分钟。此期间若编排函数被触发，会在启动阶段报：

```text
the batch containers cannot be started: K8S_API_SERVER, K8S_SA_TOKEN was never named to this function. Fill it in after the cluster exists and re-run the deployment.
```

这是预期行为，第 7 节回填后消失。

### 4.1 记下后续要用的输出

```bash
terraform output data_bucket
terraform output cluster_api_server
terraform output image_registry_host
terraform output job_namespace
terraform output inbound_prefix
```

预期形态：

```text
data_bucket         = "batch-data-dev-<账号 ID>"
cluster_api_server  = "https://<内网地址>:5443"
image_registry_host = "swr.cn-north-4.myhuaweicloud.com"
job_namespace       = "batch-jobs"
inbound_prefix      = "inbound/transactions/"
```

`next_step_inputs` 这个输出把第 7 节要填的三项列齐：

```bash
terraform output next_step_inputs
```

```text
next_step_inputs = "k8s_api_server                = output.cluster_api_server
k8s_ca_cert                   = export_k8s_access.sh -> CA
k8s_service_account_token     = export_k8s_access.sh -> token"
```

---

## 5. 配置 kubectl

在华为云 CCE 控制台的集群「连接信息」中下载 kubeconfig，然后：

```bash
export KUBECONFIG=<kubeconfig 路径>
kubectl get nodes
```

预期看到两个 `c7.large.2` 节点，状态 `Ready`（节点操作系统 `Huawei Cloud EulerOS 2.0`）。

如果 `kubectl` 报连不上：确认执行主机在这套 VPC 内（`10.20.0.0/16`），或按需把 `enable_api_server_public_access` 置 `true` 后重新 apply。

---

## 6. 应用集群内对象

```bash
cd infra
kubectl apply -f ../src/manifests/00-namespace.yaml
kubectl apply -f ../src/manifests/10-rbac.yaml
kubectl apply -f ../src/manifests/20-batch-config-configmap.yaml
```

预期输出：

```text
namespace/batch-jobs created
serviceaccount/batch-orchestrator created
serviceaccount/batch-job-runner created
role.rbac.authorization.k8s.io/batch-orchestrator created
rolebinding.rbac.authorization.k8s.io/batch-orchestrator created
secret/batch-orchestrator-token created
configmap/batch-config created
```

> **`src/manifests/30-batch-job-template.yaml` 是逐次运行的模板，永远不要直接 apply。** 其中的 `__JOB_NAME__`、`__NAMESPACE__`、`__RUN_ID__`、`__TASK__`、`__IMAGE__`、`__PREFIX__`、`__CPU_REQUEST__`、`__DEADLINE__`、`__TTL__` 等占位符由编排层在创建 Job 时逐字段填好（`src/functions/orchestrator/index.py` 的 `Engine.job_body()` 就是这份结构）。手改或提交该文件不会得到可运行的作业。

### 6.1 把真实桶名写进 ConfigMap

`20-batch-config-configmap.yaml` 出厂时 `OBS_BUCKET: "REPLACE_ME"`。容器在启动自检里会因此停住：

```text
OBS_BUCKET is empty: the batch-config map has no bucket
```

把 `REPLACE_ME` 换成 `terraform output data_bucket` 的值后重新应用：

```bash
kubectl apply -f ../src/manifests/20-batch-config-configmap.yaml
kubectl -n batch-jobs get configmap batch-config -o yaml
```

`batch-config` 必须覆盖容器读取的七项（`src/job/batch_job/settings.py` 的 `CONFIG_MAP_KEYS`）：`OBS_BUCKET`、`OBS_ENDPOINT`、`OBS_REGION`、`INBOUND_PREFIX`、`PROCESSING_PREFIX`、`OUTBOUND_PREFIX`、`LOG_LEVEL`。其余端点与前缀出厂值即为本区域正确值，一般只需改桶名。

### 6.2 创建容器访问数据湖的凭据对象

作业 Pod 通过 `batch-obs-credentials` 取本次运行的 OBS 访问密钥。它被声明为 `optional: true`，因此不存在时 Pod 仍能启动，容器会自己报出缺失：

```text
no access key pair: create the batch-obs-credentials secret with the literals HW_ACCESS_KEY and HW_SECRET_KEY
```

```bash
kubectl -n batch-jobs create secret generic batch-obs-credentials \
  --from-literal=HW_ACCESS_KEY=<HW_ACCESS_KEY> \
  --from-literal=HW_SECRET_KEY=<HW_SECRET_KEY>
```

预期输出：

```text
secret/batch-obs-credentials created
```

字面量名称必须正是 `HW_ACCESS_KEY` 与 `HW_SECRET_KEY`（临时凭据场景另有 `HW_SECURITY_TOKEN`）。

### 6.3 确认编排身份可用

```bash
kubectl -n batch-jobs auth can-i create jobs.batch --as=system:serviceaccount:batch-jobs:batch-orchestrator
kubectl -n batch-jobs auth can-i get pods --as=system:serviceaccount:batch-jobs:batch-job-runner
```

预期分别是 `yes` 与 `no`——`batch-job-runner` 按设计一条 Kubernetes API 权限都不持有，容器经集群委托访问数据湖。

---

## 7. 取回三项集群访问参数

```bash
cd infra
../src/job/export_k8s_access.sh
```

脚本行为：从当前 kubeconfig 读 API Server 地址 → 读 `batch-jobs/batch-orchestrator-token` 的 `data.token` 与 `data.ca.crt`（尚未签发时每 2 秒重试，最长等 60 秒）→ base64 还原 → 校验 JWT 主体是 `system:serviceaccount:batch-jobs:batch-orchestrator` → 打印三行可直接粘贴的内容。

预期输出：

```text
k8s_api_server                = "https://<内网地址>:5443"
k8s_service_account_token     = "<JWT>"
k8s_ca_cert = <<-EOT
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
EOT
```

把这三行完整写入 `infra/terraform.tfvars`（`k8s_ca_cert` 用的是 Terraform 的 heredoc 语法）。**令牌长期有效**：不要提交、不要粘贴进工单或聊天工具。

可覆盖的参数：

```bash
../src/job/export_k8s_access.sh --namespace batch-jobs --secret batch-orchestrator-token
../src/job/export_k8s_access.sh --api-server https://<内网地址>:5443
../src/job/export_k8s_access.sh --wait 120
```

常见失败与含义：

```text
找不到 kubectl，请先安装并配置好集群访问。
无法从当前 kubeconfig 读到集群地址，请用 --api-server 显式传入集群内网地址。
等待 60s 后仍未取到令牌：命名空间 batch-jobs 的 Secret batch-orchestrator-token 的 data.token 为空。请确认 10-rbac.yaml 已应用，且其 annotations 指向 batch-orchestrator。
令牌里的主体与预期不一致（期望 system:serviceaccount:batch-jobs:batch-orchestrator）。
```

第三条说明第 6 节的 `10-rbac.yaml` 没应用成功；第四条说明 `--namespace` / `--secret` / `--service-account` 与清单不一致。

---

## 8. 第二次 apply 与观测项收尾

```bash
cd infra
terraform apply
```

预期结束于：

```text
Apply complete! Resources: 0 added, <N> changed, 0 destroyed.
```

这一趟把 `K8S_API_SERVER`、`K8S_NAMESPACE`、`K8S_CA_CERT`、`K8S_SA_TOKEN` 写进编排函数的运行配置。

### 8.1 告警规则（二选一）

- **保持默认（推荐）**：`enable_ces_alarms = false`。栈不创建告警规则，只保留大盘与两个组件；在云监控控制台按 `terraform output metric_namespace`、维度 `run_environment = dev` 与下列阈值手工建同样两条规则：
  - `batch-dev-failed-runs`：指标 `batch_run_failed`，`sum` ≥ 1，周期 300 秒，级别 2，抑制窗口取 `failure_alarm_suppression_minutes × 60`。
  - `batch-dev-run-duration`：指标 `batch_run_duration`，`max` > `duration_alarm_threshold_seconds`（默认 1800），周期 300 秒，级别 3，抑制 900 秒。
  - 两条规则的通知都指向 `terraform output notification_topic_urn`。
- **让栈创建**：把 `enable_ces_alarms = true`。绑定自定义指标命名空间的告警规则需要在云监控侧确认接受后再启用，并确保 `agency_extra_roles` 含自定义指标写入所需的项目级角色。

无论哪种方式，`enable_custom_metrics = true`（默认）都必须保持，否则指标序列本身不会产生。

### 8.2 定时触发

`daily_launch` 与 `resume_pass` 由栈创建为 `ACTIVE`。**修改 `daily_cron` 或 `resume_interval_seconds` 后重新 apply 不会改变调度**——两个触发器都对 `timer` 与 `status` 设了 `ignore_changes`，活动中的定时触发无法原地更新。要改调度：在 FunctionGraph 控制台删除该触发器，再重新 apply。

---

## 9. 部署后验证

### 9.1 资源就绪

```bash
terraform output region
terraform output data_bucket
terraform output object_storage_endpoint
terraform output metric_namespace
terraform output daily_schedule
terraform output resume_interval_seconds
terraform output orchestrator_function_urn
terraform output file_check_function_urn
terraform output calendar_check_function_urn
terraform output batch_image
terraform output burst_namespace
terraform output burst_network_id
terraform output dashboard_id
terraform output notification_topic_urn
```

预期要点：`region = "cn-north-4"`；`object_storage_endpoint = "https://obs.cn-north-4.myhuaweicloud.com"`；`metric_namespace = "BATCH_ORCHESTRATOR"`；三个 URN 以 `urn:fgs:` 开头；`batch_image` 与第 2 节推送的地址逐段一致；`cci_enabled = false` 时 `burst_namespace` / `burst_network_id` 为空串。

FunctionGraph 控制台确认：三个函数状态正常、`batch-dev-orchestrator` 的两个定时器都在 `ACTIVE`、运行时配置里能看到 `K8S_API_SERVER` 与 `K8S_CA_CERT` 已有值。

### 9.2 校验函数（最简示例）

`batch-dev-file-check` → 函数测试，输入：

```json
{ "jobName": "JOB1" }
```

预期：

```json
{
  "step": "JOB1", "jobName": "JOB1", "allFilesPresent": false,
  "errorType": "INPUT_FILES_MISSING", "bucket": "batch-data-dev-<账号 ID>",
  "prefix": "(production)", "filesChecked": 1, "missingCount": 1, "maxAgeDays": 2,
  "message": "1 input check(s) of JOB1 did not pass."
}
```

换成 `{"jobName": "NoSuchStep"}` 应得到 `errorType: "INPUT_FILES_INVALID"` 与 `knownSteps: ["JOB1","JOB2A","JOB2B","JOB3"]`。

`batch-dev-calendar-check` → 函数测试，输入：

```json
{ "runDate": "2026-11-16" }
```

预期：

```json
{
  "isBusinessDay": true, "date": "2026-11-16", "reason": "business day",
  "timezone": "Asia/Shanghai", "calendar": "no calendar object"
}
```

`{"runDate": "2026-11-15"}`（周日）→ `isBusinessDay: false`、`reason: "weekend"`。

### 9.3 上传一份落地文件

输入文件名必须匹配 `^txns\.\d{8}\.csv$`，且龄期在 `max_input_age_days`（默认 2 个 UTC 日历天）之内。

```bash
obsutil cp txns.20261116.csv obs://batch-data-dev-<账号 ID>/inbound/transactions/txns.20261116.csv
```

预期 `obsutil` 报告上传成功；再问一次 `{"jobName": "JOB1"}`，应变成 `"allFilesPresent": true`、`"message": "Every input of JOB1 is present."`。

### 9.4 开启一次运行并观察作业

`batch-dev-orchestrator` → 函数测试，输入：

```json
{ "action": "start_run", "runDate": "2026-11-16", "budgetSeconds": 300 }
```

返回体形如：

```json
{
  "action": "start_run", "kind": "manual",
  "passBudgetSeconds": 300, "passElapsedSeconds": 41.2, "budgetSpent": false,
  "runs": [
    {
      "runId": "batch-2026-11-16", "status": "RUNNING",
      "phases": { "JOB1": "running", "JOB2A": "pending", "JOB2B": "pending", "JOB3": "pending" },
      "unfinished": ["JOB1", "JOB2A", "JOB2B", "JOB3"],
      "checkpoint": "runs/batch-2026-11-16.json"
    }
  ]
}
```

（若第 9.3 节没做，`JOB1` 会是 `skipped`、`errorType` 为 `INPUT_FILES_MISSING`，四条步全部收敛为 `skipped`，运行状态 `FAILED`——这是正确行为。）

集群侧观察：

```bash
kubectl -n batch-jobs get jobs -l batch.job/run-id=batch-2026-11-16
kubectl -n batch-jobs get pods -l batch.job/run-id=batch-2026-11-16
kubectl -n batch-jobs logs -l batch.job/run-id=batch-2026-11-16 --tail=40
```

预期作业名形如 `job-job1-batch-2026-11-16`，Pod 以 UID `10001` 运行、`restartPolicy` 为 `Never`。

运行没在一趟里跑完属正常：`resume_pass` 每 180 秒继续推进，也可以手工再发一次 `{"action":"resume_runs"}`。

### 9.5 检查点与结果

```bash
obsutil ls obs://batch-data-dev-<账号 ID>/runs/
obsutil cp obs://batch-data-dev-<账号 ID>/runs/batch-2026-11-16.json ./
```

每个运行一个 `<run-id>.json`，下载到本地后即为可读的 JSON，内含运行级 `status` / `errorType` / `message` / `plan` 与每步的 `phase`、`job`、`attempts`、`exitCode`、`startedAt`、`finishedAt`、`detail`。跑完后 `outbound/` 前缀下应有本轮结果，`processing/JOB1/output/normalized.csv` 等中间数据集应在 `processing/` 下。

### 9.6 日志、通告与大盘

- LTS 日志组 `batch-dev`：流 `batch-dev-orchestrator` 出现 `pass opened: action=start_run kind=manual run=... date=... steps=[]`；流 `batch-dev-validation` 出现 `file check answered: step=JOB1 present=... errorType=...` 与 `calendar check answered: date=... businessDay=... reason=...`。
- SMN：一次失败运行的邮件主题形如 `batch-dev-orchestrator: run batch-2026-11-16 failed (BATCH_JOB_FAILED)`，正文带 `runId`、`errorType`、`phases` 与每步详情。
- CES 大盘 `batch-dev`：`Failed runs`（柱状）与 `Run duration in seconds`（折线）按 `run_environment = dev` 出现数据点。

### 9.7 隔离回归运行

```json
{ "action": "run_regression", "runId": "night-7", "modules": ["JOB1", "JOB3"] }
```

预期该运行的读写全部落在 `runs/night-7/` 之下，不套营业日闸门、模块顺序不限，并发上限取 `regression_max_concurrency`（默认 3）。故意不给 `modules` 会抛错并列出已知模块；给出拼错的名字时返回体里出现 `rejected` 字段而不是静默成功。

### 9.8 部署后请自行验证

**部署完成后请按本节（第 9 节）步骤在真实环境逐项验证**：资源输出齐全、两个校验函数给出预期答案、作业能创建并跑完、检查点能落 OBS、失败通告能到达订阅邮箱、大盘有数据点。全部确认后方可视为交付完成。

---

## 10. 清理资源

```bash
cd infra
terraform destroy
```

销毁前须知：

- 集群创建时带 `delete_all = true`，节点池随集群一并释放。
- OBS 桶创建时带 `force_destroy = true`，**桶内对象连同全部版本会被一并删除**。需要保留的结果数据集与检查点请先导出：
  ```bash
  obsutil cp -r obs://batch-data-dev-<账号 ID>/outbound/ ./outbound-backup/
  obsutil cp -r obs://batch-data-dev-<账号 ID>/runs/ ./runs-backup/
  ```
- 命名空间、RBAC 与 ConfigMap 随集群消失，无需单独删除。
- `batch-obs-credentials` 与 `batch-orchestrator-token` 里的凭据，建议在控制台侧另行吊销；令牌轮换的方式是删除并重建该 Secret，然后重跑第 7、8 节。
- 若开启过 `enable_api_server_public_access`，弹性公网 IP 与带宽会随 destroy 释放。
- `infra/.build/` 下的两个 zip 是构建产物，可随时用 `python scripts/package_functions.py` 重建。

及时释放资源，避免残留。
