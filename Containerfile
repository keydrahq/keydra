# Keydra as one image: the frontend built and copied into the backend's static resources,
# so a single container serves both and nothing has to be told where the API lives.
#
#   podman build --ulimit nofile=16384:16384 -t keydra:dev -f Containerfile .
#   podman play kube deploy/keydra-prod.yaml
#
# The ulimit is not decoration. Rootless Podman passes the shell's own file-descriptor limit
# into the build, and javac compiling this many sources against this many jars runs out of
# descriptors long before it runs out of anything else — the error it gives is "Too many open
# files" against a random jar, which reads like a corrupt dependency and is not one.
#
# The COPY paths are the submodule directories, which are named after the repositories they
# are: this file only builds from the umbrella checkout, where all three sit side by side.
#
# Called a Containerfile rather than a Dockerfile because this project is built with
# Podman; `podman build` and `docker build` both read either name.

# --- Stage 1: the frontend ----------------------------------------------------
# The one stage that is not a Red Hat base image.
#
# ubi10/nodejs-24 does exist — an earlier version of this comment said it did not, and was
# wrong. What it ships is Node 24.18.0 against the 24.19.0 the frontend pins in .nvmrc and in
# `engines.node`, and it carries neither yarn nor corepack, so using it means a root step and
# an `npm install -g corepack` over the network before the build can start.
#
# Not worth it here, and the reason is what this stage is: everything it produces is a
# directory of static files that stage 2 copies out, and the image itself is thrown away. The
# errata feed that makes a Red Hat base the right answer for stage 3 — the image still
# running a month from now — buys nothing for a builder nobody runs.
#
# Revisit when UBI's stream catches up to the pin; it becomes a one-line change plus corepack.
FROM docker.io/library/node:24-alpine AS frontend

WORKDIR /build

# Dependencies first: they change far less often than the source, so this layer is
# reused across almost every rebuild.
COPY keydra-frontend/package.json keydra-frontend/yarn.lock keydra-frontend/.yarnrc.yml ./
RUN corepack enable && yarn install --immutable

COPY keydra-frontend/ ./
RUN yarn build

# --- Stage 2: the backend ------------------------------------------------------
# UBI 10's OpenJDK 21 image, which carries Maven 3.9 already — so this is a Maven image and
# a JDK image at once, and the version is pinned by the tag. That pin is the same guarantee
# the project's wrapper gives a developer, without a download at build time; and the
# download is what failed here, since this base image carries neither curl nor wget for the
# wrapper to use.
#
# root only because the image runs as uid 185 by default and /build would not be writable.
# Nothing survives this stage but target/, so the builder's user is not a security property.
FROM registry.access.redhat.com/ubi10/openjdk-21:1.24 AS backend

USER root
WORKDIR /build

# The descriptor first, then dependencies, then source: dependencies change far less
# often than code, so that layer survives almost every rebuild.
COPY keydra-backend/.mvn/ .mvn/
COPY keydra-backend/pom.xml ./
# `dependency:resolve` and not `go-offline`, which is a change the Red Hat platform forced.
# go-offline walks the raw dependency graph rather than the managed one, so it asks for the
# versions the Camel artifacts declare before dependencyManagement overrides them — and
# io.quarkus:quarkus-core:3.33.2.redhat-00005 is one the GA repository does not have. The
# build never wanted it; only that goal did. These two warm the same layer and respect the
# BOM, which was the whole point of the step.
# Which optional engines this image carries. Empty by default, and that default is the
# decision: the TiKV client is an uber-jar carrying forty-nine advisories in bundled copies
# nothing can upgrade, and an installation that manages no TiKV was shipping all of it.
#
#   podman build --build-arg MAVEN_PROFILES=-Ptikv ...
ARG MAVEN_PROFILES=""

RUN mvn -B -ntp ${MAVEN_PROFILES} dependency:resolve dependency:resolve-plugins

COPY keydra-backend/src/ src/
# The built frontend becomes part of the backend's static resources, which is what
# lets one server answer both the API and the page that calls it.
COPY --from=frontend /build/dist/ src/main/resources/META-INF/resources/
# Tests need containers, which a build container does not have; they run in CI.
RUN mvn -B -ntp ${MAVEN_PROFILES} package -DskipTests

# --- Stage 3: what actually ships ---------------------------------------------
# The runtime variant: a JRE without the compiler, which is a few hundred megabytes a
# running server has no use for and one more tool an attacker would find waiting.
#
# This is the stage where a Red Hat base earns its keep rather than merely matching a
# policy — it is the only image that is still running a month from now, so whose errata
# feed and patch cadence it is on is a real answer to a real question. The builder above
# is thrown away; this one is what somebody has to keep patched.
#
# UBI 10 rather than 9, and the difference is measurable rather than a preference for the
# newer number: 332 MB against 396, and neither python3 nor expat is installed — which
# between them carried sixty-one of the two hundred and seventy package vulnerabilities the
# registry's scanner found in the UBI 9 build. Everything the Containerfile depends on is
# the same: uid 185 in group 0, Java 21.0.12.1, curl and no wget.
FROM registry.access.redhat.com/ubi10/openjdk-21-runtime:1.24

# No user is created here: the image already runs as uid 185, and files are given to group
# 0 so the container still works when a platform assigns it some arbitrary uid instead —
# which is what OpenShift does, and the reason group 0 rather than 185:185.
USER root
WORKDIR /app

# The errata stream moves faster than the base tag does. `1.24` is `1.24-12`, rebuilt on Red
# Hat's own cadence, while UBI ships fixes in between — measured against this image, five
# packages are behind: sqlite-libs by two Highs (CVE-2026-11822 and CVE-2026-11824), and
# libattr, libxml2 and pam-libs by a Medium each. Without this the weekly rebuild republishes
# the same unpatched packages, which is the one thing the weekly rebuild exists not to do.
#
# Ahead of the COPY, so a rebuild that only changes the application reuses the layer.
RUN microdnf -y update && microdnf -y clean all && rm -rf /var/cache/yum

COPY --from=backend --chown=185:0 /build/target/quarkus-app/lib/ lib/
COPY --from=backend --chown=185:0 /build/target/quarkus-app/*.jar ./
COPY --from=backend --chown=185:0 /build/target/quarkus-app/app/ app/
COPY --from=backend --chown=185:0 /build/target/quarkus-app/quarkus/ quarkus/

USER 185
EXPOSE 8181 9001

# Reads the container's own memory limit rather than the host's, so a limited
# container does not size its heap for a machine it cannot use.
ENV JAVA_OPTS="-XX:MaxRAMPercentage=75 -XX:+UseContainerSupport"

# The readiness probe the manifests use, so an image run by hand behaves the same. On 9001
# rather than 8181: in production /q lives on its own port, so that the proxy publishing the
# interface publishes nothing that describes the installation.
#
# curl rather than wget: the runtime image is built on ubi9-minimal, which carries the
# first and not the second.
#
# Kept although Podman drops it: an image built in the OCI format has nowhere to put a
# healthcheck, and the build says so and carries on. It is here for `--format docker` and for
# anything that builds this with Docker, and the manifests probe the same endpoint themselves —
# so what this line adds is an image that behaves the same when somebody runs it by hand, on the
# builders that can express it.
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s \
    CMD ["sh", "-c", "curl -sf http://localhost:9001/q/health/ready || exit 1"]

ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar quarkus-run.jar"]
