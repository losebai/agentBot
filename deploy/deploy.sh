#!/usr/bin/env bash
# Linux 服务器发布入口：固定项目和数据卷；不执行 SQL、不删除历史发布目录。
set -euo pipefail
base=${1:?需要部署根目录}
release_id=${2:?需要发布编号}
[[ "$base" =~ ^/[a-zA-Z0-9_/-]+$ && "$base" != / && "$base" != */ ]]
[[ "$release_id" =~ ^[0-9]+$ ]]
release="$base/releases/$release_id"
test -f "$base/runtime.env"
test -f "$release/compose.yaml"
test -f "$release/release.env"
grep -Eq '^AGENTBOT_IMAGE=[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}$' "$release/release.env"
grep -Fxq "AGENTBOT_RUNTIME_ENV_FILE=$base/runtime.env" "$release/release.env"

# GitLab resource_group 管理流水线并发，文件锁同时防止手动发布交叉。
exec 9>"$base/deploy.lock"
flock -n 9 || { printf '%s\n' '已有发布正在进行' >&2; exit 1; }
previous=''
if [[ -L "$base/current" ]]; then
    previous=$(readlink "$base/current")
    [[ "$previous" =~ ^releases/[0-9]+$ ]]
fi

# 参数数组保持文件路径边界；摘要已由 CI 固定，拉取失败不触碰正在运行的服务。
compose=(docker compose --project-name agentbot --env-file "$release/release.env"
         -f "$release/compose.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" pull agentbot
if ! "${compose[@]}" up -d --wait --wait-timeout 180 --remove-orphans agentbot; then
    printf '%s\n' '新版本未就绪，尝试恢复上一成功发布' >&2
    if [[ -n "$previous" && "$previous" != "releases/$release_id" ]]; then
        docker compose --project-name agentbot --env-file "$base/$previous/release.env" \
            -f "$base/$previous/compose.yaml" up -d --wait --wait-timeout 180 agentbot
    else
        "${compose[@]}" stop agentbot
    fi
    exit 1
fi

# 成功后才移动当前指针；旧目录和镜像仍可用于显式回滚。
if [[ -n "$previous" && "$previous" != "releases/$release_id" ]]; then
    ln -sfn "$previous" "$base/previous"
fi
ln -sfn "releases/$release_id" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
printf 'taobot 已部署发布 %s\n' "$release_id"
