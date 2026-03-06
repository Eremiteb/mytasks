#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
}

@test "fails fast for non-root user" {
  [ ! -f "$REPO_ROOT/wireguard-install.sh" ] && skip "wireguard-install.sh is not present in this workspace"
  [ "$EUID" -eq 0 ] && skip "This test expects non-root execution"

  run bash "$REPO_ROOT/wireguard-install.sh"

  [ "$status" -eq 1 ]
  [[ "$output" == *"You need to run this script as root"* ]]
}
