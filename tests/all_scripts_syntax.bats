#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
}

@test "all root shell scripts pass syntax check" {
  mapfile -t scripts < <(find "$REPO_ROOT" -maxdepth 1 -type f -name '*.sh' -printf '%f\n' | sort)

  [ "${#scripts[@]}" -gt 0 ]

  for script in "${scripts[@]}"; do
    [ -f "$REPO_ROOT/$script" ]

    if head -n 1 "$REPO_ROOT/$script" | grep -q 'bash'; then
      run bash -n "$REPO_ROOT/$script"
    else
      run sh -n "$REPO_ROOT/$script"
    fi

    [ "$status" -eq 0 ]
  done
}
