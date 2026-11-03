"""Offline Korean region selection using the attributed KMA catalog."""

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import termios
import tty

from terminal_ui import FOREGROUND, MUTED, color_mode, hint_line, read_key, screen_frame, styled_line


LOCATION_ALIASES = {
    "울릉도": "경상북도 울릉군", "gangnam": "서울 강남구",
    "seoul": "서울", "busan": "부산", "incheon": "인천", "daegu": "대구",
    "daejeon": "대전", "ulsan": "울산",
}


@dataclass(frozen=True)
class Region:
    code: str
    province: str
    district: str
    town: str
    latitude: float
    longitude: float

    @property
    def parts(self):
        district = re.fullmatch(r"(.+시)(.+구)", self.district)
        names = [self.province]
        names.extend(district.groups() if district else ([self.district] if self.district else []))
        if self.town:
            names.append(self.town)
        return tuple(names)

    @property
    def label(self):
        return " ".join(dict.fromkeys(self.parts))


@lru_cache(maxsize=1)
def all_regions():
    payload = json.loads(Path(__file__).with_name("korea-regions.json").read_text(encoding="utf-8"))
    return tuple(Region(**row) for row in payload["records"])


def province_aliases(province):
    base = re.sub(r"(특별자치도|특별자치시|통합특별시|특별시|광역시|도)$", "", province)
    aliases = {province, base}
    if base.endswith(("북", "남")):
        aliases.add(base[0] + base[-1])
    if province in ("강원특별자치도", "전북특별자치도"):
        aliases.add("강원도" if base == "강원" else "전라북도")
    if base == "제주":
        aliases.add("제주도")
    return aliases


def normalized(name):
    return re.sub(r"[\s,]+", "", name).casefold()


def find_region(name):
    name = LOCATION_ALIASES.get(name.strip().casefold(), name)
    target, matches = normalized(name), []
    for region in all_regions():
        if region.district == region.province and not region.town:
            continue
        tail = region.parts[1:]
        if tail and tail[0] == region.province:
            tail = tail[1:]
        tails = {tail}
        if len(tail) > 1 and tail[0].endswith("시") and tail[1].endswith("구"):
            tails.add((tail[0][:-1],) + tail[1:])
        keys = {normalized(alias + "".join(variant)) for alias in province_aliases(region.province)
                for variant in tails}
        if tail:
            keys.update(normalized("".join(variant)) for variant in tails)
            keys.add(normalized(tail[-1]))
        if target in keys:
            matches.append(region)
    if len(matches) > 1:
        raise ValueError("같은 이름의 지역이 여러 곳입니다. 시·도 이름을 함께 적거나 salang --setup으로 선택해 주세요.")
    return matches[0] if matches else None


def region_options(prefix):
    records, level = all_regions(), len(prefix)
    children = sorted({region.parts[level] for region in records
                       if region.parts[:level] == prefix and len(region.parts) > level})
    exact = [region for region in records if region.parts == prefix]
    return children, exact


def menu_view(prefix, labels, cursor, columns, rows, mode):
    width, count = max(1, columns - 2), max(1, rows - 9)
    start = max(0, min(cursor - count // 2, len(labels) - count))
    heading = "지역 선택" if not prefix else "지역 선택  /  " + " → ".join(dict.fromkeys(prefix))
    lines = [styled_line((("  " + heading, FOREGROUND),), width, mode),
             styled_line((("  방향키로 이동하고 Enter로 선택하세요", MUTED),), width, mode), ""]
    for index in range(start, min(len(labels), start + count)):
        selected = index == cursor
        lines.append(styled_line(((("  › " if selected else "    ") + labels[index],
                                   FOREGROUND if selected else MUTED),), width, mode))
    lines.extend(("", styled_line(((f"  {cursor + 1} / {len(labels)}", MUTED),), width, mode),
                  hint_line((("↑↓", "이동"), ("Enter", "선택"), ("Esc", "이전"), ("q", "취소")), width, mode)))
    return "\r\n".join(lines)


def select_region(no_color=False):
    prefix, cursor, history = (), 0, []
    previous_frame, previous_size = None, None
    fd = sys.stdin.fileno()
    settings = termios.tcgetattr(fd)
    mode = "none" if no_color else color_mode()
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033[?1049h\033[?25l")
        while True:
            children, exact = region_options(prefix)
            if not children:
                if len(exact) != 1:
                    raise ValueError("지역 목록을 확인할 수 없습니다.")
                return exact[0]
            if len(children) == 1 and prefix and children[0] == prefix[-1]:
                prefix += (children[0],)
                continue
            labels = (["이 지역 전체 · 대표 위치"] if exact else []) + children
            cursor = min(cursor, len(labels) - 1)
            size = shutil.get_terminal_size((80, 35))
            current_frame = menu_view(prefix, labels, cursor, size.columns, size.lines, mode)
            sys.stdout.write(screen_frame(current_frame, previous_frame if size == previous_size else None))
            sys.stdout.flush()
            previous_frame, previous_size = current_frame, size
            key = read_key(fd)
            if key == "q":
                raise KeyboardInterrupt
            if key == "up":
                cursor = (cursor - 1) % len(labels)
            elif key == "down":
                cursor = (cursor + 1) % len(labels)
            elif key in ("home", "end"):
                cursor = 0 if key == "home" else len(labels) - 1
            elif key in ("page-up", "page-down"):
                cursor = max(0, min(len(labels) - 1, cursor + (-1 if key == "page-up" else 1) * max(1, size.lines - 9)))
            elif key in ("back", "b"):
                if not history:
                    raise KeyboardInterrupt
                prefix, cursor = history.pop()
            elif key == "enter":
                if exact and cursor == 0:
                    return exact[0]
                history.append((prefix, cursor))
                prefix += (children[cursor - bool(exact)],)
                cursor = 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()


def settings_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "salang" / "settings.json"


def saved_region():
    path = settings_path()
    try:
        if not path.exists():
            path = path.parent.parent / "terminal-garden" / "settings.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("region_code"), str):
            raise ValueError("잘못된 지역 설정 형식")
        selected = next((region for region in all_regions() if region.code == value["region_code"]), None)
        if selected is None:
            raise ValueError("지역 코드가 목록에 없습니다")
        return selected
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        print(f"저장된 지역 설정을 읽지 못해 기본 강남구를 사용합니다: {error}", file=sys.stderr)
        return None


def save_region(region):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump({"version": 1, "region_code": region.code}, output)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
