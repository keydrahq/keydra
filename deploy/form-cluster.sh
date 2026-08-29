#!/usr/bin/env bash
# Joins the three nodes from keydra-cluster.yaml into one cluster.
#
# Separate from the manifest because forming a cluster is a one-off command against
# already-running servers, and `podman play kube` has nowhere to put one. Running it
# twice is harmless: the second run finds the slots already assigned and says so.
set -euo pipefail

NODES=(7001 7002 7003)
CONTAINER=keydra-cluster-node-1

for port in "${NODES[@]}"; do
    until podman exec "$CONTAINER" redis-cli -p "$port" ping >/dev/null 2>&1; do
        echo "waiting for node on $port…"
        sleep 1
    done
done

if podman exec "$CONTAINER" redis-cli -p 7001 cluster info | grep -q "cluster_state:ok"; then
    echo "Cluster already formed."
    podman exec "$CONTAINER" redis-cli -p 7001 cluster nodes
    exit 0
fi

# Three primaries and no replicas: the point here is slot distribution across nodes,
# which is what the topology view draws. Replicas would need three more servers to
# show one more row.
podman exec "$CONTAINER" redis-cli --cluster create \
    127.0.0.1:7001 127.0.0.1:7002 127.0.0.1:7003 \
    --cluster-yes

# The state is "fail" for a second or two after creation while the nodes gossip the
# slot assignment to each other; reporting that would say the script had not worked.
for _ in $(seq 1 30); do
    if podman exec "$CONTAINER" redis-cli -p 7001 cluster info | grep -q "cluster_state:ok"; then
        echo "cluster_state:ok"
        exit 0
    fi
    sleep 1
done

echo "Cluster did not reach cluster_state:ok" >&2
podman exec "$CONTAINER" redis-cli -p 7001 cluster info | grep cluster_state >&2
exit 1
