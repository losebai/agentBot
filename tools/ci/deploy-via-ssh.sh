#!/usr/bin/env bash
# 从 GitLab 传输同一提交的部署描述，凭据和数据库操作不进入发布包。
set -euo pipefail

: "${DEPLOY_HOST:?请设置 DEPLOY_HOST}"
: "${DEPLOY_USER:?请设置 DEPLOY_USER}"
: "${SSH_PRIVATE_KEY:?请设置文件类型 SSH_PRIVATE_KEY}"
: "${SSH_KNOWN_HOSTS:?请设置文件类型 SSH_KNOWN_HOSTS}"
: "${CI_PIPELINE_ID:?缺少流水线编号}"
: "${AGENTBOT_IMAGE:?缺少已构建镜像摘要}"
deploy_port=${DEPLOY_PORT:-22}
deploy_path=${DEPLOY_PATH:-/opt/agentbot}
bind_address=${AGENTBOT_BIND_ADDRESS:-$DEPLOY_HOST}
http_port=${AGENTBOT_HTTP_PORT:-8080}

# 所有进入远端命令的字段先限制字符；不执行 CI 变量中的 shell 文本。
[[ "$DEPLOY_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]
[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ ]]
if ! [[ "$deploy_port" =~ ^[0-9]{1,5}$ ]] || (( 10#$deploy_port < 1 || 10#$deploy_port > 65535 )); then
    printf '%s\n' 'SSH 端口必须为 1-65535 的整数' >&2
    exit 1
fi
[[ "$deploy_path" =~ ^/[a-zA-Z0-9_/-]+$ && "$deploy_path" != / && "$deploy_path" != */ ]]
[[ "$CI_PIPELINE_ID" =~ ^[0-9]+$ ]]
[[ "$bind_address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]
# 显式退出：set -e 不会因 && 左侧校验失败而终止脚本。
if ! [[ "$http_port" =~ ^[0-9]{1,5}$ ]] || (( 10#$http_port < 1 || 10#$http_port > 65535 )); then
    printf '%s\n' 'Web 端口必须为 1-65535 的整数' >&2
    exit 1
fi
[[ "$AGENTBOT_IMAGE" =~ ^[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}$ ]]
test -f "$SSH_PRIVATE_KEY" && test -f "$SSH_KNOWN_HOSTS"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
install -m 600 "$SSH_PRIVATE_KEY" "$HOME/.ssh/agentbot_deploy"
install -m 600 "$SSH_KNOWN_HOSTS" "$HOME/.ssh/known_hosts"
target="$DEPLOY_USER@$DEPLOY_HOST"
release="$deploy_path/releases/$CI_PIPELINE_ID"
ssh_options=(-i "$HOME/.ssh/agentbot_deploy" -o BatchMode=yes -o IdentitiesOnly=yes
             -o StrictHostKeyChecking=yes -o ConnectTimeout=15)
# shellcheck disable=SC2029 # 路径和编号已限制字符，明确在本机展开。
ssh "${ssh_options[@]}" -p "$deploy_port" "$target" "mkdir -p -- '$release'"

umask 077
manifest=$(mktemp)
trap 'rm -f -- "$manifest"' EXIT
printf 'AGENTBOT_IMAGE=%s\nAGENTBOT_RUNTIME_ENV_FILE=%s/runtime.env\nAGENTBOT_BIND_ADDRESS=%s\nAGENTBOT_HTTP_PORT=%s\n' \
    "$AGENTBOT_IMAGE" "$deploy_path" "$bind_address" "$http_port" > "$manifest"
scp "${ssh_options[@]}" -P "$deploy_port" deploy/compose.yaml deploy/deploy.sh "$target:$release/"
scp "${ssh_options[@]}" -P "$deploy_port" "$manifest" "$target:$release/release.env"
# shellcheck disable=SC2029 # 远端只执行固定脚本及已经验证的路径参数。
ssh "${ssh_options[@]}" -p "$deploy_port" "$target" \
    "bash '$release/deploy.sh' '$deploy_path' '$CI_PIPELINE_ID'"
