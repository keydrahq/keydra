#!/usr/bin/env bash
# Starts the backend for development, without the work a development loop does not need.
#
# `./mvnw quarkus:dev` on its own does four things every time that cost seconds and buy
# nothing between one edit and the next:
#
#   * it asks Maven Central for plugin metadata, which is a handful of network round trips
#     before anything local happens — `-o` keeps it offline, and the one time that is wrong
#     is the run after a dependency changes, which is what `--online` is for;
#   * it runs Spotless over four hundred files, which belongs in the build that produces a
#     commit rather than in the one that reloads a page;
#   * it starts continuous testing, which is a second JVM compiling and running the suite
#     beside the one serving requests;
#   * it JIT-compiles as though it were going to run for a week, when it is going to run
#     until the next edit.
#
# What it does not skip is the compiler. Code that does not compile should fail here rather
# than in a browser.
#
# Anything after the options is passed to Maven, so `dev-backend.sh -Dsomething=x` works.
set -euo pipefail

cd "$(dirname "$0")/.."/backend

OFFLINE="-o"
CONSOLE="true"
ARGUMENTS=()
for argument in "$@"; do
  case "$argument" in
    --online) OFFLINE="" ;;
    --quiet) CONSOLE="false" ;;
    *) ARGUMENTS+=("$argument") ;;
  esac
done

# TieredStopAtLevel=1 stops the JIT at the cheap tier: slower steady-state, faster start,
# and steady state is not what a dev server is for. SerialGC skips setting up a collector
# sized for a server nobody is loading.
export MAVEN_OPTS="${MAVEN_OPTS:-} -XX:TieredStopAtLevel=1 -XX:+UseSerialGC"

exec ./mvnw $OFFLINE quarkus:dev \
  -Dspotless.check.skip=true \
  -Dquarkus.test.continuous-testing=disabled \
  -Dquarkus.console.enabled="$CONSOLE" \
  ${ARGUMENTS[@]+"${ARGUMENTS[@]}"}
