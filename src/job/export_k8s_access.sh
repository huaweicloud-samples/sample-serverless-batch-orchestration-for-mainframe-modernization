#!/usr/bin/env bash
# 读取编排身份的两项集群访问参数，打印成可直接粘进 terraform.tfvars 的三行。
#
# 使用时机：terraform apply 建出集群，并且 src/manifests 下的 00-namespace.yaml
# 与 10-rbac.yaml 已经执行之后。第一次 apply 之前集群还不存在，无需运行本脚本。
#
# 前置：kubectl 已安装，且已通过华为云「集群-连接信息」里下载的 kubeconfig 完成配置
# （KUBECONFIG 环境变量指向该文件，或已合并进 ~/.kube/config）。
#
# 用法：
#   src/job/export_k8s_access.sh                       # 使用清单里的默认名字
#   src/job/export_k8s_access.sh --namespace batch-jobs --secret batch-orchestrator-token
#   src/job/export_k8s_access.sh --api-server https://<内网地址>:5443
#
# 输出内容包含长期有效令牌，请勿提交到仓库，也不要粘贴进工单或聊天工具。
set -euo pipefail

NAMESPACE="batch-jobs"
SECRET="batch-orchestrator-token"
SERVICE_ACCOUNT="batch-orchestrator"
API_SERVER=""
WAIT_SECONDS=60

while [ $# -gt 0 ]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --secret) SECRET="$2"; shift 2 ;;
    --service-account) SERVICE_ACCOUNT="$2"; shift 2 ;;
    --api-server) API_SERVER="$2"; shift 2 ;;
    --wait) WAIT_SECONDS="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *)
      echo "未知参数：$1" >&2
      exit 2 ;;
  esac
done

fail() {
  echo "$*" >&2
  exit 1
}

command -v kubectl >/dev/null 2>&1 || fail "找不到 kubectl，请先安装并配置好集群访问。"

# 命名清单里的默认值，避免脚本与 10-rbac.yaml 各说一套
if [ -z "$API_SERVER" ]; then
  API_SERVER="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
  [ -n "$API_SERVER" ] || fail "无法从当前 kubeconfig 读到集群地址，请用 --api-server 显式传入集群内网地址。"
fi

TOKEN=""
CA=""
deadline=$(( $(date +%s) + WAIT_SECONDS ))
while :; do
  TOKEN="$(kubectl -n "$NAMESPACE" get secret "$SECRET" -o jsonpath='{.data.token}' 2>/dev/null || true)"
  CA="$(kubectl -n "$NAMESPACE" get secret "$SECRET" -o jsonpath='{.data.ca\.crt}' 2>/dev/null || true)"
  [ -n "$TOKEN" ] && [ -n "$CA" ] && break
  [ "$(date +%s)" -lt "$deadline" ] || fail "等待 ${WAIT_SECONDS}s 后仍未取到令牌：命名空间 ${NAMESPACE} 的 Secret ${SECRET} 的 data.token 为空。请确认 10-rbac.yaml 已应用，且其 annotations 指向 ${SERVICE_ACCOUNT}。"
  echo "令牌尚未签发，2s 后重试……" >&2
  sleep 2
done

# Secret 的 data 值一律是 base64，先还原成原始内容再校验
jwt_from_b64() {
  printf '%s' "$1" | base64 -d 2>/dev/null || fail "base64 解码失败：请确认系统里有 base64 命令（Git Bash / coreutils 自带）。"
}

pad_b64url() {
  local s="$1"
  case $(( ${#s} % 4 )) in
    2) printf '%s==' "$s" ;;
    3) printf '%s=' "$s" ;;
    *) printf '%s' "$s" ;;
  esac
}

JWT="$(jwt_from_b64 "$TOKEN")"
PAYLOAD_B64="$(printf '%s' "$JWT" | cut -d. -f2 | tr '_-' '/+')"
SUB="$(pad_b64url "$PAYLOAD_B64" | base64 -d 2>/dev/null | tr -d '\n')"

# 令牌必须属于预期的那个服务账户，否则拿到的是别的身份的权限
EXPECTED="system:serviceaccount:${NAMESPACE}:${SERVICE_ACCOUNT}"
case "$SUB" in
  *"$EXPECTED"*) : ;;
  *) fail "令牌里的主体与预期不一致（期望 ${EXPECTED}）。请检查 --namespace/--service-account/--secret 是否与清单一致。" ;;
esac

CA_PEM="$(printf '%s' "$CA" | base64 -d)"
printf 'k8s_api_server                = "%s"\n' "$API_SERVER"
printf 'k8s_service_account_token     = "%s"\n' "$JWT"
printf 'k8s_ca_cert = <<-EOT\n'
printf '%s\n' "$CA_PEM"
printf 'EOT\n'
