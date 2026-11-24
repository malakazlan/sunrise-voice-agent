# Bounded waits for the CI docker job, sourced by its steps. Each gives up after its bound and
# says what it was waiting for, so a hang fails the step instead of stalling the job.

# bounded SECONDS WHAT COMMAND...: run COMMAND for at most SECONDS.
bounded() {
  seconds=$1
  what=$2
  shift 2
  code=0
  timeout "$seconds" "$@" || code=$?
  if [ "$code" = 124 ]; then
    echo "timed out after ${seconds}s waiting for $what" >&2
  fi
  return "$code"
}

# bounded_run SECONDS WHAT NAME DOCKER-RUN-ARGS...: `docker run --name NAME ...` for at most
# SECONDS. Stopping the client does not stop the container, so on timeout it is removed by name.
bounded_run() {
  seconds=$1
  what=$2
  name=$3
  shift 3
  code=0
  timeout "$seconds" docker run --name "$name" "$@" || code=$?
  if [ "$code" = 124 ]; then
    docker rm -f "$name" > /dev/null 2>&1 || true
    echo "timed out after ${seconds}s waiting for $what (container $name removed)" >&2
  fi
  return "$code"
}

# wait_for_status SECONDS URL STATUS WHAT: poll URL once a second until it answers STATUS.
wait_for_status() {
  seconds=$1
  url=$2
  expected=$3
  what=$4
  status=000
  for _ in $(seq 1 "$seconds"); do
    status=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$url" || true)
    if [ "$status" = "$expected" ]; then
      echo "GET $url -> $status"
      return 0
    fi
    sleep 1
  done
  echo "gave up after ${seconds}s waiting for $what (GET $url -> $status)" >&2
  return 1
}
