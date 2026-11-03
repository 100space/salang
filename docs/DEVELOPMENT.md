# 개발과 검증

소스 실행은 macOS/Linux의 Python 3.9 이상을 사용하며, 실행에 필요한 외부 Python 패키지는 없습니다. 저장소 폴더에서 다음 명령으로 점검합니다.

```sh
python3 "$PWD/check_salang.py"
python3 "$PWD/check_install.py"
```

독립 실행 파일은 Python 3.12와 해시로 고정한 빌드 도구로 만듭니다. 로컬 빌드용 가상환경을 만든 뒤 실행하세요.

```sh
python3 -m pip install --require-hashes --only-binary=:all: -r "$PWD/requirements-build.txt"
python3 "$PWD/tools/build_release.py"
```

`Salang.command`는 현재 운영체제·CPU에 맞는 최신 로컬 빌드가 있으면 이를 사용합니다. 소스나 데이터가 빌드보다 새로우면 소스로 실행합니다.

성능 재측정은 `tools/benchmark.py`에서 지원합니다. `--project`로 소스 폴더, `--executable`로 독립 실행 파일을 지정할 수 있습니다.

`v0.1.0` 형식의 태그를 게시하면 네 플랫폼의 빌드와 검증을 수행하고 실행 파일·설치 스크립트·SHA-256 목록을 GitHub Releases에 게시합니다. 태그와 앱 버전이 다르면 게시하지 않습니다.


## 관련 문서

새 소스나 문서를 공개할 때는 루트 `.gitignore`의 허용 목록도 함께 갱신합니다.

- [20개 장면 미리보기](previews/README.md)
- [데이터 출처와 라이선스](../THIRD_PARTY_NOTICES.md)
