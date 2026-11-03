#!/bin/sh
set -eu

repository=100space/salang
version=latest
prefix="${HOME}/.local"
update_path=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --version|--prefix)
            [ "$#" -ge 2 ] || { printf '%s\n' '옵션 값을 입력해 주세요.' >&2; exit 1; }
            case "$1" in --version) version=$2 ;; --prefix) prefix=$2 ;; esac
            shift 2 ;;
        --no-path) update_path=0; shift ;;
        --help|-h)
            printf '%s\n' 'Salang 설치: sh install.sh [--version v0.1.0] [--prefix 절대경로] [--no-path]'
            exit 0 ;;
        *) printf '%s\n' "알 수 없는 옵션: $1" >&2; exit 1 ;;
    esac
done

fail() { printf '%s\n' "$*" >&2; exit 1; }
case "$prefix" in /*) ;; *) fail '설치 경로는 절대경로여야 합니다.' ;; esac
case "$prefix" in *:*|*'
'*) fail '설치 경로에 콜론이나 줄바꿈을 사용할 수 없습니다.' ;; esac
if [ "$version" != latest ]; then
    printf '%s\n' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || fail '버전은 v0.1.0 형식으로 입력해 주세요.'
fi
case "$(uname -s)" in Darwin) operating_system=darwin ;; Linux) operating_system=linux ;; *) fail '현재 macOS와 Linux를 지원합니다.' ;; esac
case "$(uname -m)" in arm64|aarch64) architecture=arm64 ;; x86_64|amd64) architecture=amd64 ;; *) fail '현재 ARM64와 Intel/AMD 64비트 CPU를 지원합니다.' ;; esac
command -v curl >/dev/null 2>&1 || fail '다운로드에 필요한 curl을 찾지 못했습니다.'
if command -v sha256sum >/dev/null 2>&1; then
    checksum_tool=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    checksum_tool=shasum
else
    fail '다운로드 검증에 필요한 SHA-256 명령어를 찾지 못했습니다.'
fi

bin_dir="$prefix/bin"
app_dir="$prefix/share/salang"
executable="$app_dir/salang"
command_path="$bin_dir/salang"
if [ -e "$command_path" ] || [ -L "$command_path" ]; then
    [ -L "$command_path" ] || fail '다른 salang 명령어가 이미 있습니다. --prefix로 별도 경로를 지정해 주세요.'
    current_target=$(readlink "$command_path")
    case "$current_target" in "$executable"|*/salang/Salang.command) ;; *) fail '다른 salang 명령어와 충돌해 설치를 중단했습니다.' ;; esac
fi
if [ -e "$executable" ] || [ -L "$executable" ]; then
    [ ! -L "$executable" ] && [ -f "$app_dir/.managed-by-salang" ] &&
        [ "$(cat "$app_dir/.managed-by-salang")" = "$repository" ] || fail '설치 폴더에 기존 파일이 있어 덮어쓰지 않았습니다.'
fi

download_dir=$(mktemp -d "${TMPDIR:-/tmp}/salang-install.XXXXXX")
staged_executable=
staged_link=
cleanup() {
    rm -f "$download_dir/binary" "$download_dir/SHA256SUMS"
    [ -z "$staged_executable" ] || rm -f "$staged_executable"
    [ -z "$staged_link" ] || rm -f "$staged_link"
    rmdir "$download_dir" 2>/dev/null || :
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM
asset="salang-$operating_system-$architecture"
if [ "$version" = latest ]; then
    release_url="https://github.com/$repository/releases/latest/download"
else
    release_url="https://github.com/$repository/releases/download/$version"
fi
printf '%s\n' "Salang 다운로드 중: $operating_system / $architecture"
curl -fLsS --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 10 --max-time 120 "$release_url/SHA256SUMS" -o "$download_dir/SHA256SUMS" || fail '릴리스 정보를 받지 못했습니다. GitHub Releases가 공개되어 있는지 확인해 주세요.'
curl -fLsS --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 10 --max-time 120 "$release_url/$asset" -o "$download_dir/binary" || fail '이 운영체제용 실행 파일을 받지 못했습니다.'
expected=$(awk -v name="$asset" '$2 == name { count++; digest=$1 } END { if (count != 1) exit 1; print digest }' "$download_dir/SHA256SUMS") || fail '릴리스 검증 정보가 올바르지 않습니다.'
[ "${#expected}" -eq 64 ] || fail 'SHA-256 길이가 올바르지 않습니다.'
case "$expected" in *[!0-9a-fA-F]*) fail 'SHA-256 형식이 올바르지 않습니다.' ;; esac
if [ "$checksum_tool" = shasum ]; then
    actual=$(shasum -a 256 "$download_dir/binary" | awk '{print $1}')
else
    actual=$(sha256sum "$download_dir/binary" | awk '{print $1}')
fi
expected=$(printf '%s' "$expected" | tr 'A-F' 'a-f')
[ "$actual" = "$expected" ] || fail '다운로드 검증에 실패했습니다. 기존 설치는 유지했습니다. 다시 시도해 주세요.'
chmod 755 "$download_dir/binary"
installed_version=$("$download_dir/binary" --version) || fail '실행 파일이 이 컴퓨터에서 동작하지 않습니다.'
case "$installed_version" in 'salang '[0-9]*.[0-9]*.[0-9]*) ;; *) fail 'Salang 실행 파일인지 확인하지 못했습니다.' ;; esac
[ "$version" = latest ] || [ "$installed_version" = "salang ${version#v}" ] || fail '요청한 버전과 실행 파일 버전이 다릅니다.'

mkdir -p "$bin_dir" "$app_dir"
if [ -f "$executable" ]; then cp -p "$executable" "$app_dir/salang.previous"; fi
staged_executable=$(mktemp "$app_dir/.salang.XXXXXX")
cp "$download_dir/binary" "$staged_executable"
chmod 755 "$staged_executable"
mv -f "$staged_executable" "$executable"
staged_executable=
printf '%s\n' "$repository" > "$app_dir/.managed-by-salang"
staged_link="$bin_dir/.salang-link-$$"
ln -s "$executable" "$staged_link"
mv -f "$staged_link" "$command_path"
staged_link=

if [ "$update_path" -eq 1 ]; then
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *)
            case "${SHELL:-}" in */zsh) profile="$HOME/.zshrc" ;; */bash) profile="$HOME/.bashrc" ;; *) profile="$HOME/.profile" ;; esac
            quoted_bin=$(printf '%s' "$bin_dir" | sed "s/'/'\\\\''/g")
            path_line="case \":\$PATH:\" in *':$quoted_bin:'*) ;; *) export PATH='$quoted_bin':\"\$PATH\" ;; esac"
            if [ -L "$profile" ]; then
                printf '%s\n' "셸 설정이 연결 파일이므로 자동 수정하지 않았습니다. PATH에 $bin_dir 를 추가해 주세요."
            elif [ ! -f "$profile" ] || ! grep -Fqx "$path_line" "$profile"; then
                if [ -f "$profile" ]; then
                    profile_backup=$(mktemp "$profile.salang-backup.XXXXXX")
                    cp "$profile" "$profile_backup"
                fi
                printf '\n%s\n%s\n' '# Salang' "$path_line" >> "$profile"
            fi ;;
    esac
fi
printf '\n%s\n%s\n' "$installed_version 설치 완료" '새 터미널을 열고 salang을 입력하세요. 지역 설정: salang --setup'
if [ "$update_path" -eq 0 ]; then printf '%s\n' "실행 파일: $command_path"; fi
