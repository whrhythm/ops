#!/bin/sh
set -eu
cd /tmp/starbridge-v1-01a-codeapi
test_key=${1:?unique test key required}
case "$test_key" in *[!a-zA-Z0-9_-]*) exit 2;; esac
probe="starbridge-v101a-kill-${test_key}"
runner=starbridge-v101a-runner-1
worker=starbridge-v101a-worker-1
dispatch=starbridge-v101a-dispatch-1
image=harbor.k3s.local/internal/platform-control:codeapi-20261105.1-api
run_probe() {
  docker run "$@" --network starbridge-v101a_default -v "$PWD:/work" -w /work/service \
    -e "OFFICE_TEST_KEY=$test_key" "$image" bun run src/office-recovery.acceptance.ts "$mode"
}
mode=submit
run_probe -d --name "$probe" >/dev/null
task_id=
for _ in $(seq 1 50); do
  task_id=$(docker logs "$probe" 2>&1 | awk '/^TEST_TASK_ID / {print $2}')
  [ -n "$task_id" ] && break
  sleep .1
done
test -n "$task_id"
sandbox_id=
for _ in $(seq 1 60); do
  sandbox_id=$(docker logs "$dispatch" 2>&1 | grep -F "\"jobId\":\"$task_id\"" | sed -nE 's/.*"sandboxSession":"(sbx_[a-zA-Z0-9_-]+)".*/\1/p')
  [ -n "$sandbox_id" ] && break
  sleep .1
done
test -n "$sandbox_id"
for _ in $(seq 1 80); do
  if docker logs "$runner" 2>&1 | grep -F "\"job\":\"$sandbox_id\"" | grep -q '"msg":"Running"'; then
    break
  fi
  sleep .1
done
docker logs "$runner" 2>&1 | grep -F "\"job\":\"$sandbox_id\"" | grep '"msg":"Running"'
sleep .5
docker kill "$worker" >/dev/null
test "$(docker inspect -f '{{.State.ExitCode}}' "$worker")" = 137
docker start "$worker" >/dev/null
echo "WORKER_SIGKILL_AND_RESTART $task_id"
mode=recover
run_probe --rm
count=$(docker logs "$runner" 2>&1 | grep -F "\"job\":\"$sandbox_id\"" | grep -c '"msg":"Running"')
test "$count" = 1
count=$(docker logs "$dispatch" 2>&1 | grep -F "\"jobId\":\"$task_id\"" | grep -c 'Office execution dispatched')
test "$count" = 1
echo "RUNNER_EXECUTIONS=1 DISPATCHES=1"
cp acceptance-result.json "acceptance-${test_key}.json"
