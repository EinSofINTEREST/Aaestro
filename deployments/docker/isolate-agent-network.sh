#!/bin/sh
# Block an isolated agent network from reaching the host through its bridge gateway (#303).
#
# `--internal` 네트워크는 외부로 나가지 못하지만, 브리지 게이트웨이는 호스트 자신이다 — 모든
# 인터페이스에 바인드한 호스트 서비스는 에이전트가 부를 수 있다. 이 스크립트는 그 브리지에서 호스트로
# **새로 여는** 연결을 막는다. 호스트가 먼저 연 연결의 응답(control plane → 에이전트 Control API)은
# 통과한다 (ESTABLISHED,RELATED).
#
# 컨테이너 → 게이트웨이 트래픽은 호스트 자신에게 오므로 FORWARD(DOCKER-USER)가 아니라 INPUT 을 지난다.
#
#   usage: isolate-agent-network.sh apply|remove|status <network>
#   env:   IPTABLES   iptables 실행 방법 (기본 iptables). IPv6 는 IP6TABLES (기본 ip6tables, 없으면 건너뜀)
#
# root(또는 CAP_NET_ADMIN)가 필요하다. 호스트가 재부팅되거나 방화벽을 다시 읽으면 다시 적용한다.
set -eu

action=${1:?usage: $0 apply|remove|status <network>}
network=${2:?usage: $0 apply|remove|status <network>}

id=$(docker network inspect -f '{{.Id}}' "$network")
internal=$(docker network inspect -f '{{.Internal}}' "$network")
bridge=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$network")
[ -n "$bridge" ] && [ "$bridge" != "<no value>" ] || bridge="br-$(printf %s "$id" | cut -c1-12)"
chain="AAESTRO-$(printf %s "$id" | cut -c1-12)"

if [ "$internal" != "true" ] && [ "$action" = "apply" ]; then
  echo "refusing: $network is not an internal network — isolate agents first (runtime.egress_proxy)" >&2
  exit 2
fi

run() { # $1 = iptables command, rest = arguments
  cmd=$1; shift
  # shellcheck disable=SC2086 — IPTABLES 는 "docker run ... iptables" 같은 명령일 수 있다
  $cmd "$@"
}

apply_with() {
  cmd=$1
  run "$cmd" -N "$chain" 2>/dev/null || run "$cmd" -F "$chain"
  run "$cmd" -A "$chain" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  run "$cmd" -A "$chain" -j DROP
  run "$cmd" -C INPUT -i "$bridge" -j "$chain" 2>/dev/null || run "$cmd" -I INPUT 1 -i "$bridge" -j "$chain"
}

remove_with() {
  cmd=$1
  while run "$cmd" -D INPUT -i "$bridge" -j "$chain" 2>/dev/null; do :; done
  run "$cmd" -F "$chain" 2>/dev/null || true
  run "$cmd" -X "$chain" 2>/dev/null || true
}

status_with() {
  cmd=$1
  if run "$cmd" -C INPUT -i "$bridge" -j "$chain" 2>/dev/null; then echo "applied ($cmd)"; else echo "not applied ($cmd)"; fi
}

ipv4=${IPTABLES:-iptables}
ipv6=${IP6TABLES:-ip6tables}

case "$action" in
  apply) apply_with "$ipv4" ;;
  remove) remove_with "$ipv4" ;;
  status) status_with "$ipv4" ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
if command -v "${ipv6%% *}" >/dev/null 2>&1; then
  case "$action" in
    apply) apply_with "$ipv6" ;;
    remove) remove_with "$ipv6" ;;
    status) status_with "$ipv6" ;;
  esac
fi
echo "$action: $network (bridge $bridge, chain $chain)"
