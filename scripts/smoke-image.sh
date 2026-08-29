#!/usr/bin/env bash
# Builds the image and starts it, which is the only check that finds what phase 65 found.
#
# The test suites answer whether the code is right. This answers a different question, and one
# nothing else asks: whether the thing that ships starts. Phase 65's defect lived entirely between
# the manifest and the container runtime — every test passed, the image built, and the pod restarted
# itself every thirty seconds for ever — and the only way to see it was to start it and look.
#
# Podman on purpose, and this is the point rather than a preference. The failure was Podman's
# translation of a Kubernetes probe into a healthcheck; the same manifest under Docker, or under a
# hand-written `docker run`, would have passed. A smoke test that used a different runtime would be
# testing something other than the command written at the top of the file it is testing.
#
#   scripts/smoke-image.sh            # build, start, check, tear down
#   scripts/smoke-image.sh --keep     # leave it running to poke at
#
# KEYDRA_SMOKE_IMAGE names an image that already exists and skips the build. That exists for
# CI, which publishes what this script started: without it CI would build twice and push the
# copy this never ran, which is the one arrangement that makes a smoke test decorative.
set -euo pipefail

cd "$(dirname "$0")/.."

KEEP=false
[[ "${1:-}" == "--keep" ]] && KEEP=true

IMAGE=${KEYDRA_SMOKE_IMAGE:-localhost/keydra:smoke}
MANIFEST=$(mktemp /tmp/keydra-smoke-XXXXXX.yaml)
BASE=http://localhost:8181
# /q is on its own port in a packaged run, which is the arrangement being checked.
MANAGEMENT=http://localhost:9001

cleanup() {
  if [[ "$KEEP" == false ]]; then
    podman play kube --down "$MANIFEST" >/dev/null 2>&1 || true
    podman volume rm smoke-db smoke-backups >/dev/null 2>&1 || true
    rm -f "$MANIFEST"
  fi
}
trap cleanup EXIT

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
# A failure here is usually read on a CI page by somebody who cannot reach the pod, so the
# message alone is not enough: "the first administrator could not be created" was a whole
# round trip away from "the readiness probe names a program the image does not carry, so
# Podman had already killed the JVM". Whatever is known gets printed with the failure.
fail() {
  printf '\033[31mFAILED: %s\033[0m\n' "$*" >&2
  if podman pod exists keydra-prod 2>/dev/null; then
    printf '\n\033[1m-- containers --\033[0m\n' >&2
    podman ps -a --filter pod=keydra-prod --format '{{.Names}}\t{{.Status}}\t{{.RestartCount}} restarts' >&2 || true
    for c in keydra-prod-keydra keydra-prod-postgres; do
      if podman container exists "$c" 2>/dev/null; then
        printf '\n\033[1m-- %s (last 60) --\033[0m\n' "$c" >&2
        podman logs --tail 60 "$c" >&2 2>&1 || true
        # What Podman made of the manifest's probe, which is the thing that is not in the log
        # when the probe is what went wrong.
        printf '\n\033[1m-- %s healthcheck --\033[0m\n' "$c" >&2
        podman inspect "$c" --format '{{json .Config.Healthcheck}}' >&2 || true
        podman healthcheck run "$c" >&2 2>&1 || true
      fi
    done
  fi
  exit 1
}

say "Secrets"
# Throwaway, and named so nobody mistakes them for the real thing. A real deployment's key
# encrypts every stored credential; losing it means losing them, which is why the manifest keeps
# them out of the repository and why these are created here and only here.
# A Kubernetes Secret's data, which is what `podman play kube` reads a Podman secret as: a JSON
# object whose keys secretKeyRef.key selects between, with base64 values. Both halves are easy to
# get wrong and neither says so plainly — a bare value is "not valid JSON: invalid character 'k'",
# and unencoded JSON is "illegal base64 data at input byte 4".
podman secret rm smoke-db-password smoke-secret-key >/dev/null 2>&1 || true
printf '{"smoke-db-password":"%s"}' "$(printf keydra | base64 -w0)" \
  | podman secret create smoke-db-password - >/dev/null
printf '{"smoke-secret-key":"%s"}' "$(openssl rand -base64 32 | base64 -w0)" \
  | podman secret create smoke-secret-key - >/dev/null

# Storage of its own, thrown away first. The manifest keeps its database now, which is the
# point of it — and a check that shared that storage would be reading yesterday's run: the
# second one finds an administrator already there and cannot make the first.
podman volume rm smoke-db smoke-backups >/dev/null 2>&1 || true

if [[ -n "${KEYDRA_SMOKE_IMAGE:-}" ]]; then
  say "Build (skipped: using $IMAGE)"
  podman image exists "$IMAGE" || fail "KEYDRA_SMOKE_IMAGE names $IMAGE, which does not exist"
else
  say "Build"
  # The ulimit is not decoration; see the Containerfile. javac runs out of file descriptors long
  # before it runs out of anything else, and says so against a random jar.
  podman build --ulimit nofile=16384:16384 -t "$IMAGE" -f Containerfile .
fi

say "Start"
# The manifest as it is, with two substitutions: the image tag, and the key's secret name so that
# a machine running this does not need the real one to exist.
# The manifest as it is, with the image tag and the two secret names replaced — so a machine
# running this needs neither the real key nor the real password to exist, and running it cannot
# overwrite them.
sed -e "s|image: localhost/keydra:dev|image: $IMAGE|" \
    -e "s|keydra-secret-key|smoke-secret-key|g" \
    -e "s|keydra-db-password|smoke-db-password|g" \
    -e "s|\(name: \)keydra-db$|\1smoke-db|" \
    -e "s|claimName: keydra-db$|claimName: smoke-db|" \
    -e "s|\(name: \)keydra-backups$|\1smoke-backups|" \
    -e "s|claimName: keydra-backups$|claimName: smoke-backups|" \
    deploy/keydra-prod.yaml > "$MANIFEST"
podman play kube "$MANIFEST"

say "Waiting for it to answer"
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "$MANAGEMENT/q/health/ready" 2>/dev/null; then
    break
  fi
  sleep 5
done
curl -fsS -o /dev/null "$MANAGEMENT/q/health/ready" || fail "it never became ready"

say "What only a running installation can answer"

# Every migration applied to an empty database, in order, which no test of a single migration
# checks and which is what a first release does.
applied=$(podman exec keydra-prod-postgres psql -U keydra -d keydra -tAc \
  "select count(*) from flyway_schema_history where success" | tr -d '[:space:]')
[[ "$applied" -gt 0 ]] || fail "no migrations were applied"
echo "migrations applied: $applied"

# The interface, and a deep link — which the single-page fallback serves for a browser and refuses
# for a fetch. Its own javadoc says it only matters in the packaged image, and until this script
# existed it had never run there.
curl -fsS -o /dev/null "$BASE/" || fail "the interface is not served"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Accept: text/html' "$BASE/connections/1/keys")
[[ "$code" == "200" ]] || fail "a deep link answered $code for a browser"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/nope")
[[ "$code" == "404" ]] || fail "a missing endpoint answered $code instead of 404"

# And nothing that describes the installation on the port people are given. This is the check
# the separate management port exists for: "put a proxy in front and do not publish /q" is a
# rule somebody has to remember, and a port with nothing on it is a thing that is true.
for hidden in /q/metrics /q/health /q/health/ready /api/openapi; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$hidden")
  [[ "$code" == "404" ]] || fail "$hidden answered $code on the port the proxy publishes"
done
curl -fsS -o /dev/null "$MANAGEMENT/q/metrics" || fail "metrics are not on the management port"

# An instance with accounts of its own, which is what the manifest now ships. Until the identity
# provider stopped following the enforcement switch, this was a container that refused to start
# with a message about an OIDC property nobody had set.
say "The first administrator, and signing in as them"
curl -fsS -X POST "$BASE/api/v1/auth/setup" -H 'Content-Type: application/json' \
  -d '{"username":"smoke-admin","password":"a-password-nobody-guesses"}' -o /dev/null \
  || fail "the first administrator could not be created"

# Once, and only once: an endpoint that makes an administrator is an endpoint that must stop
# working the moment there is one.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/auth/setup" \
  -H 'Content-Type: application/json' \
  -d '{"username":"second-admin","password":"a-password-nobody-guesses"}')
[[ "$code" != "201" ]] || fail "a second administrator could be created through setup"

cookie=$(curl -s -i -X POST "$BASE/api/v1/auth/login" \
  --data-urlencode 'username=smoke-admin' --data-urlencode 'password=a-password-nobody-guesses' \
  | grep -io 'keydra_session=[^;]*' | head -1)
[[ -n "$cookie" ]] || fail "signing in returned no session"
curl -fsS -H "Cookie: $cookie" "$BASE/api/v1/security/me" | grep -q 'smoke-admin' \
  || fail "the session does not answer for the account that took it"

# And that it is a door rather than a formality.
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/connections")
[[ "$code" == "401" || "$code" == "403" ]] || fail "an unauthenticated request answered $code"

# A credential written and read back: the answer never carries it, and the database never holds it
# in the clear. Tested here as well as in the suite because here it is the production profile, the
# real key, and the schema Flyway built.
curl -fsS -X POST "$BASE/api/v1/connections" -H "Cookie: $cookie" -H 'Content-Type: application/json' \
  -d '{"name":"smoke","host":"127.0.0.1","port":6399,"tls":false,"database":0,"type":"STANDALONE","password":"a-secret-nobody-should-see"}' \
  -o /dev/null || fail "a connection profile could not be created"
curl -fsS -H "Cookie: $cookie" "$BASE/api/v1/connections" | grep -q '"hasPassword":true' \
  || fail "the profile came back without a password on it"
curl -fsS -H "Cookie: $cookie" "$BASE/api/v1/connections" | grep -q 'a-secret-nobody-should-see' \
  && fail "the API answered with the password"
stored=$(podman exec keydra-prod-postgres psql -U keydra -d keydra -tAc \
  "select password from connection_profile limit 1" | tr -d '[:space:]')
[[ "$stored" == enc:* ]] || fail "the password is not encrypted at rest: ${stored:0:8}"
echo "stored as: ${stored:0:8}…"

# It knows itself: the roster, the lease, and what this deployment says twice differently.
curl -fsS -H "Cookie: $cookie" "$BASE/api/v1/instances" | grep -q '"leader":true' \
  || fail "no instance claims the chores"

# And that check, proved rather than assumed: a request through a proxy, on an instance told there
# is none. Nothing else in the repository can exercise it, because it is a fact about traffic.
curl -fsS -o /dev/null -H 'X-Forwarded-For: 203.0.113.9' "$BASE/api/v1/about"
curl -fsS -H "Cookie: $cookie" "$BASE/api/v1/instances" | grep -q 'KEYDRA_BEHIND_PROXY' \
  || fail "a request through a proxy did not raise the setting it contradicts"

# Still up. The failure phase 65 found looks like success until you look twice: the application
# starts perfectly and is stopped a few seconds later, for ever.
say "Still running after the probes have had their say"
sleep 45
curl -fsS -o /dev/null "$MANAGEMENT/q/health/ready" || fail "it stopped answering after starting"
starts=$(podman logs keydra-prod-keydra 2>&1 | grep -c 'Listening on' || true)
[[ "$starts" == "1" ]] || fail "it started $starts times, so something is stopping it"

printf '\n\033[32mThe image starts, serves and stays up.\033[0m\n'
[[ "$KEEP" == true ]] && echo "Left running at $BASE — tear down with: podman play kube --down $MANIFEST"
