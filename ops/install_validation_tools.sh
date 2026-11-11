#!/usr/bin/env bash
# Linux/amd64 only. Hashes checked against upstream release checksums/assets.
set -euo pipefail
destination=${1:?usage: install_validation_tools.sh DIRECTORY}
mkdir -p "$destination"
destination=$(realpath "$destination")
install_tool() {
  local name=$1 url=$2 expected=$3
  curl --fail --silent --show-error --location --retry 3 "$url" -o "$destination/$name.tar.gz"
  echo "$expected  $destination/$name.tar.gz" | sha256sum --check --status
  tar -xzf "$destination/$name.tar.gz" -C "$destination" "$name"
}
install_tool trivy https://github.com/aquasecurity/trivy/releases/download/v0.76.2/trivy_0.76.2_Linux-64bit.tar.gz bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf
install_tool actionlint https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
install_tool zizmor https://github.com/zizmorcore/zizmor/releases/download/v1.32.0/zizmor-x86_64-unknown-linux-gnu.tar.gz bb110cc765eca9cd7efb2e284a956be5974c60027b0970f16397d86208a9957b
