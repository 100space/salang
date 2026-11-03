#!/bin/zsh
salang_dir=${0:A:h}
case "$(uname -sm)" in
  'Darwin arm64') salang_asset=darwin-arm64 ;;
  'Darwin x86_64') salang_asset=darwin-amd64 ;;
  'Linux aarch64'|'Linux arm64') salang_asset=linux-arm64 ;;
  'Linux x86_64') salang_asset=linux-amd64 ;;
esac
salang_binary="${salang_dir}/dist/salang-${salang_asset}"
if [[ -n "$salang_asset" && -x "$salang_binary" ]]; then
  salang_fresh=1
  for salang_source in salang.py seasonal.py terminal_ui.py regions.py korea-regions.json THIRD_PARTY_NOTICES.md licenses/BUILD_COMPONENTS.txt; do
    if [[ "${salang_dir}/${salang_source}" -nt "$salang_binary" ]]; then
      salang_fresh=0
      break
    fi
  done
  if (( salang_fresh )); then
    exec "$salang_binary" "$@"
  fi
fi
exec /usr/bin/python3 "${salang_dir}/salang.py" "$@"
