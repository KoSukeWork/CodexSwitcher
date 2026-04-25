#!/usr/bin/env python3
"""Shared utilities for Codex Switcher desktop UI."""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import time
import tomllib
import traceback
import zipfile
from datetime import datetime
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml


USERPROFILE_DIR = Path(os.environ.get("USERPROFILE") or Path.home())
CODEX_DIR = USERPROFILE_DIR / ".codex"
SWITCHER_DIR = USERPROFILE_DIR / ".codex-config-switch"
PROFILE_STORE = SWITCHER_DIR / "codex_profiles.json"
CONFIG_PATH = CODEX_DIR / "config.toml"
AUTH_PATH = CODEX_DIR / "auth.json"
LOG_PATH = SWITCHER_DIR / "codex_switcher.log"
_WIN_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_WIN_READONLY = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)

TEAM_PROFILE = {
    "name": "Team Official",
    "api_key": "sk-team-xxxx",
    "org_id": "org-xxxx",
    "base_url": "https://api.openai.com/v1",
}

PING_TIMEOUT_MS = 1000
HTTP_TIMEOUT = 3.0

PING_REGEX = re.compile(r"(?:time|时间)[=<]?\s*(\d+)\s*ms", re.IGNORECASE)


class _TomlEntry(NamedTuple):
    full_key: Tuple[str, ...]
    section: Tuple[str, ...]
    key_parts: Tuple[str, ...]
    start: int
    end: int
    equals_index: int
    lines: List[str]


class _TomlSectionHeader(NamedTuple):
    path: Tuple[str, ...]
    start: int
    line: str
    is_array: bool


class _TomlScan(NamedTuple):
    lines: List[str]
    entries: List[_TomlEntry]
    headers: List[_TomlSectionHeader]


def _detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_line_endings(text: str, line_ending: str) -> str:
    return re.sub(r"\r\n|\r|\n", line_ending, text)


def _parse_toml_key(key_text: str) -> Tuple[str, ...]:
    parts: List[str] = []
    idx = 0
    length = len(key_text)
    while idx < length:
        while idx < length and key_text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        if key_text[idx] in ("'", '"'):
            quote = key_text[idx]
            start = idx
            idx += 1
            while idx < length:
                if quote == '"' and key_text[idx] == "\\":
                    idx += 2
                    continue
                if key_text[idx] == quote:
                    idx += 1
                    break
                idx += 1
            raw = key_text[start:idx].strip()
        else:
            start = idx
            while idx < length and key_text[idx] != ".":
                idx += 1
            raw = key_text[start:idx].strip()
        if raw:
            parts.append(_decode_toml_key_part(raw))
        while idx < length and key_text[idx].isspace():
            idx += 1
        if idx < length and key_text[idx] == ".":
            idx += 1
            continue
        break
    return tuple(part for part in parts if part)


def _decode_toml_key_part(raw: str) -> str:
    if raw.startswith(("'", '"')):
        try:
            data = tomllib.loads(f"{raw} = 0")
            return str(next(iter(data.keys())))
        except Exception:
            return raw.strip("'\"")
    return raw


def _format_toml_key(parts: Tuple[str, ...]) -> str:
    return ".".join(_format_toml_key_part(part) for part in parts)


def _format_toml_key_part(part: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", part):
        return part
    escaped = part.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _find_toml_assignment_equals(line: str) -> int:
    quote: Optional[str] = None
    idx = 0
    while idx < len(line):
        char = line[idx]
        if quote is not None:
            if quote == '"' and char == "\\":
                idx += 2
                continue
            if char == quote:
                quote = None
        else:
            if char == "#":
                return -1
            if char in ("'", '"'):
                quote = char
            elif char == "=":
                return idx
        idx += 1
    return -1


def _parse_toml_table_header(line: str) -> Optional[Tuple[Tuple[str, ...], bool]]:
    stripped = line.lstrip()
    if stripped.startswith("[["):
        close = "]]"
        start = 2
        is_array = True
    elif stripped.startswith("["):
        close = "]"
        start = 1
        is_array = False
    else:
        return None

    quote: Optional[str] = None
    idx = start
    while idx < len(stripped):
        char = stripped[idx]
        if quote is not None:
            if quote == '"' and char == "\\":
                idx += 2
                continue
            if char == quote:
                quote = None
        else:
            if char in ("'", '"'):
                quote = char
            elif stripped.startswith(close, idx):
                after = stripped[idx + len(close) :].strip()
                if after and not after.startswith("#"):
                    return None
                key = stripped[start:idx].strip()
                parts = _parse_toml_key(key)
                return (parts, is_array) if parts else None
        idx += 1
    return None


def _toml_value_fragment_parses(fragment: str) -> bool:
    try:
        tomllib.loads(f"__codex_switcher_value = {fragment}")
        return True
    except tomllib.TOMLDecodeError:
        return False


def _find_toml_entry_end(lines: List[str], start: int, equals_index: int) -> int:
    fragment = lines[start][equals_index + 1 :]
    for end in range(start + 1, len(lines) + 1):
        if _toml_value_fragment_parses(fragment):
            return end
        if end < len(lines):
            fragment += lines[end]
    return start + 1


def _scan_toml(text: str) -> _TomlScan:
    lines = text.splitlines(keepends=True)
    entries: List[_TomlEntry] = []
    headers: List[_TomlSectionHeader] = []
    current_section: Tuple[str, ...] = ()
    idx = 0

    while idx < len(lines):
        header = _parse_toml_table_header(lines[idx])
        if header is not None:
            current_section, is_array = header
            headers.append(_TomlSectionHeader(current_section, idx, lines[idx], is_array))
            idx += 1
            continue

        equals_index = _find_toml_assignment_equals(lines[idx])
        if equals_index >= 0:
            key_text = lines[idx][:equals_index].strip()
            key_parts = _parse_toml_key(key_text)
            if key_parts:
                end = _find_toml_entry_end(lines, idx, equals_index)
                entries.append(
                    _TomlEntry(
                        current_section + key_parts,
                        current_section,
                        key_parts,
                        idx,
                        end,
                        equals_index,
                        lines[idx:end],
                    )
                )
                idx = end
                continue
        idx += 1

    return _TomlScan(lines, entries, headers)


def _toml_section_ranges(scan: _TomlScan) -> Dict[Tuple[str, ...], Tuple[int, int, str]]:
    ranges: Dict[Tuple[str, ...], Tuple[int, int, str]] = {}
    headers = scan.headers
    for idx, header in enumerate(headers):
        if header.is_array:
            continue
        end = headers[idx + 1].start if idx + 1 < len(headers) else len(scan.lines)
        ranges.setdefault(header.path, (header.start + 1, end, header.line))
    return ranges


def _toml_top_level_insert_at(scan: _TomlScan) -> int:
    return scan.headers[0].start if scan.headers else len(scan.lines)


def _toml_source_section_headers(scan: _TomlScan) -> Dict[Tuple[str, ...], str]:
    result: Dict[Tuple[str, ...], str] = {}
    for header in scan.headers:
        if not header.is_array:
            result.setdefault(header.path, header.line)
    return result


def _toml_value_fragment(entry: _TomlEntry) -> str:
    return entry.lines[0][entry.equals_index + 1 :] + "".join(entry.lines[1:])


def _entry_lines_with_key(entry: _TomlEntry, key_parts: Tuple[str, ...], line_ending: str) -> List[str]:
    fragment = _normalize_line_endings(_toml_value_fragment(entry), line_ending)
    if fragment and not fragment[0].isspace():
        fragment = f" {fragment}"
    text = f"{_format_toml_key(key_parts)} ={fragment}"
    if not text.endswith(("\n", "\r")):
        text += line_ending
    return text.splitlines(keepends=True)


def _replacement_lines(source: _TomlEntry, target: _TomlEntry, line_ending: str) -> List[str]:
    prefix = target.lines[0][: target.equals_index + 1]
    fragment = _normalize_line_endings(_toml_value_fragment(source), line_ending)
    if fragment and not fragment[0].isspace():
        fragment = f" {fragment}"
    text = prefix + fragment
    if not text.endswith(("\n", "\r")):
        text += line_ending
    return text.splitlines(keepends=True)


def _normalize_toml_lines(lines: List[str], line_ending: str) -> List[str]:
    text = _normalize_line_endings("".join(lines), line_ending)
    if not text.endswith(("\n", "\r")):
        text += line_ending
    return text.splitlines(keepends=True)


def _toml_insert_chunk(
    lines: List[str],
    insert_at: int,
    entries: List[Tuple[_TomlEntry, Tuple[str, ...]]],
    line_ending: str,
) -> List[str]:
    chunk: List[str] = []
    if insert_at > 0 and lines[insert_at - 1].strip():
        chunk.append(line_ending)
    for entry, key_parts in entries:
        chunk.extend(_entry_lines_with_key(entry, key_parts, line_ending))
    if insert_at < len(lines) and lines[insert_at].strip():
        chunk.append(line_ending)
    return chunk


def _longest_existing_section(
    full_key: Tuple[str, ...], sections: Dict[Tuple[str, ...], Tuple[int, int, str]]
) -> Tuple[str, ...]:
    for length in range(len(full_key) - 1, 0, -1):
        candidate = full_key[:length]
        if candidate in sections:
            return candidate
    return ()


def _validate_toml_text(text: str, label: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{label} 不是有效 TOML：{exc}") from exc


def merge_config_toml_overlay(current_text: str, overlay_text: str) -> str:
    """Apply keys from overlay_text to current_text without deleting other TOML keys."""
    _validate_toml_text(overlay_text, "选中的配置文件")
    if not current_text.strip():
        return overlay_text
    _validate_toml_text(current_text, "当前 config.toml")

    current_scan = _scan_toml(current_text)
    overlay_scan = _scan_toml(overlay_text)
    if any(header.is_array for header in current_scan.headers + overlay_scan.headers):
        raise ValueError("暂不支持合并包含 [[array-of-tables]] 的 TOML 配置")

    line_ending = _detect_line_ending(current_text)
    target_by_key = {entry.full_key: entry for entry in current_scan.entries}
    updated_keys = set()
    replacements: List[Tuple[int, int, List[str]]] = []

    for source_entry in overlay_scan.entries:
        target_entry = target_by_key.get(source_entry.full_key)
        if target_entry is None:
            continue
        updated_keys.add(source_entry.full_key)
        replacements.append(
            (target_entry.start, target_entry.end, _replacement_lines(source_entry, target_entry, line_ending))
        )

    lines = list(current_scan.lines)
    for start, end, new_lines in sorted(replacements, key=lambda item: item[0], reverse=True):
        lines[start:end] = new_lines

    merged_scan = _scan_toml("".join(lines))
    sections = _toml_section_ranges(merged_scan)
    source_headers = _toml_source_section_headers(overlay_scan)
    missing_groups: Dict[Tuple[str, ...], List[Tuple[_TomlEntry, Tuple[str, ...]]]] = {}
    section_order: List[Tuple[str, ...]] = []

    for source_entry in overlay_scan.entries:
        if source_entry.full_key in updated_keys:
            continue
        section = source_entry.section
        key_parts = source_entry.key_parts
        if not section:
            existing_section = _longest_existing_section(source_entry.full_key, sections)
            if existing_section:
                section = existing_section
                key_parts = source_entry.full_key[len(existing_section) :]
        if section not in missing_groups:
            missing_groups[section] = []
            section_order.append(section)
        missing_groups[section].append((source_entry, key_parts))

    insertions: List[Tuple[int, List[str]]] = []
    append_sections: List[Tuple[Tuple[str, ...], List[Tuple[_TomlEntry, Tuple[str, ...]]]]] = []
    for section in section_order:
        entries = missing_groups[section]
        if not section:
            insert_at = _toml_top_level_insert_at(merged_scan)
            insertions.append((insert_at, _toml_insert_chunk(lines, insert_at, entries, line_ending)))
        elif section in sections:
            insert_at = sections[section][1]
            insertions.append((insert_at, _toml_insert_chunk(lines, insert_at, entries, line_ending)))
        else:
            append_sections.append((section, entries))

    for insert_at, chunk in sorted(insertions, key=lambda item: item[0], reverse=True):
        lines[insert_at:insert_at] = chunk

    for section, entries in append_sections:
        if lines and lines[-1].strip():
            lines.append(line_ending)
        header = source_headers.get(section)
        if header is None:
            header = f"[{_format_toml_key(section)}]{line_ending}"
        lines.extend(_normalize_toml_lines([header], line_ending))
        for entry, key_parts in entries:
            lines.extend(_entry_lines_with_key(entry, key_parts, line_ending))

    merged_text = "".join(lines)
    _validate_toml_text(merged_text, "合并后的 config.toml")
    return merged_text

def load_store() -> Dict[str, object]:
    if PROFILE_STORE.exists():
        try:
            raw = json.loads(PROFILE_STORE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("检测到损坏的 codex_profiles.json，已使用空模板重新创建。")
            raw = {}
    else:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    teams = raw.get("teams")
    if not isinstance(teams, dict):
        teams = {}
    raw["profiles"] = profiles
    raw["teams"] = teams
    if "active" not in raw:
        raw["active"] = None
    return raw


def save_store(store: Dict[str, object]) -> None:
    PROFILE_STORE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_STORE.write_text(
        json.dumps(store, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def update_config_base_url(new_url: str) -> None:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
    else:
        text = ""
    line_ending = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    if not lines:
        lines = [
            'model_provider = "codexzh"',
            "",
            "[model_providers.codexzh]",
            f'base_url = "{new_url}"',
        ]
        CONFIG_PATH.write_text(line_ending.join(lines) + line_ending, encoding="utf-8")
        return

    section_start = None
    in_target_section = False
    updated = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section_name = stripped[1:-1].strip().strip("'\"")
            in_target_section = section_name == "model_providers.codexzh"
            if in_target_section:
                section_start = idx
            continue
        if in_target_section and stripped.startswith("base_url"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[idx] = f'{indent}base_url = "{new_url}"'
            updated = True
            break

    if not updated:
        if section_start is not None:
            insert_at = section_start + 1
            lines.insert(insert_at, f'base_url = "{new_url}"')
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["[model_providers.codexzh]", f'base_url = "{new_url}"'])
    text_out = line_ending.join(lines)
    if not text_out.endswith(line_ending):
        text_out += line_ending
    try:
        safe_write_text(CONFIG_PATH, text_out)
    except PermissionError as err:
        raise PermissionError(
            f"无法写入 {CONFIG_PATH}，请确认文件未被其他程序占用并具有写入权限。"
        ) from err


def update_auth_key(api_key: str) -> None:
    if AUTH_PATH.exists():
        try:
            data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("auth.json 内容无法解析，已重新生成。")
            data = {}
    else:
        data = {}
    data["OPENAI_API_KEY"] = api_key
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        safe_write_text(AUTH_PATH, json.dumps(data, indent=2) + "\n")
    except PermissionError as err:
        raise PermissionError(
            f"无法写入 {AUTH_PATH}，请确认文件未被其他程序占用并具有写入权限。"
        ) from err


def update_auth_org_id(org_id: str) -> None:
    if AUTH_PATH.exists():
        try:
            data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("auth.json 内容无法解析，已重新生成。")
            data = {}
    else:
        data = {}
    if org_id:
        data["OPENAI_ORG_ID"] = org_id
    else:
        data.pop("OPENAI_ORG_ID", None)
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        safe_write_text(AUTH_PATH, json.dumps(data, indent=2) + "\n")
    except PermissionError as err:
        raise PermissionError(
            f"无法写入 {AUTH_PATH}，请确认文件未被其他程序占用并具有写入权限。"
        ) from err


def apply_account_config(store: Dict[str, object], account: Dict[str, str]) -> None:
    update_config_base_url(account.get("base_url", ""))
    update_auth_key(account.get("api_key", ""))
    if account.get("is_team") == "1":
        update_auth_org_id(account.get("org_id", ""))
        name = account.get("name", "")
        store["active"] = f"team:{name}" if name else "team:unknown"
    else:
        update_auth_org_id("")
        name = account.get("name", "")
        store["active"] = name or None
    save_store(store)


def is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def parse_ping_time(output: str) -> Optional[int]:
    match = PING_REGEX.search(output)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _subprocess_hidden_kwargs() -> dict:
    if os.name != "nt":
        return {}
    kwargs: dict = {}
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    except Exception:
        pass
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def ping_once(host: str) -> Optional[int]:
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), host]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", host]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            **_subprocess_hidden_kwargs(),
        )
    except Exception:
        return None
    output = (proc.stdout or "") + (proc.stderr or "")
    return parse_ping_time(output)


def ping_average(host: str, attempts: int) -> Tuple[Optional[float], float]:
    times: List[int] = []
    failures = 0
    for _ in range(attempts):
        value = ping_once(host)
        if value is None:
            failures += 1
        else:
            times.append(value)
    loss_pct = failures / attempts * 100.0 if attempts > 0 else 100.0
    if not times:
        return None, loss_pct
    return sum(times) / len(times), loss_pct


def http_head_average(url: str, api_key: str, attempts: int) -> Optional[float]:
    try:
        import requests
        import urllib3
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("缺少 requests 依赖，请先执行：uv sync") from exc
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    headers = {"User-Agent": user_agent}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    verify = True
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host and is_ip_address(host):
        verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    times: List[float] = []
    for _ in range(attempts):
        start = time.perf_counter()
        try:
            resp = session.head(
                url,
                headers=headers,
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
                verify=verify,
            )
            resp.close()
        except requests.exceptions.SSLError:
            if verify:
                try:
                    resp = session.head(
                        url,
                        headers=headers,
                        timeout=HTTP_TIMEOUT,
                        allow_redirects=True,
                        verify=False,
                    )
                    resp.close()
                except requests.RequestException:
                    return None
            else:
                return None
        except requests.RequestException:
            return None
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    if not times:
        return None
    return sum(times) / len(times)


def is_placeholder_team_profile(profile: Dict[str, str]) -> bool:
    api_key = profile.get("api_key", "")
    org_id = profile.get("org_id", "")
    if not api_key or not org_id:
        return True
    if "xxxx" in api_key or "xxxx" in org_id:
        return True
    return False


def build_accounts(store: Dict[str, object]) -> List[Dict[str, str]]:
    profiles = store["profiles"]
    assert isinstance(profiles, dict)
    teams = store.get("teams")
    if not isinstance(teams, dict):
        teams = {}
    accounts: List[Dict[str, str]] = []
    if not is_placeholder_team_profile(TEAM_PROFILE):
        accounts.append(
            {
                "name": TEAM_PROFILE["name"],
                "api_key": TEAM_PROFILE["api_key"],
                "org_id": TEAM_PROFILE["org_id"],
                "base_url": TEAM_PROFILE["base_url"],
                "is_team": "1",
                "account_type": "team",
            }
        )
    for name in sorted(teams.keys()):
        profile = teams[name]
        accounts.append(
            {
                "name": name,
                "api_key": profile.get("api_key", ""),
                "org_id": profile.get("org_id", ""),
                "base_url": profile.get("base_url", ""),
                "is_team": "1",
                "account_type": "team",
            }
        )
    for name in sorted(profiles.keys()):
        profile = profiles[name]
        base_url = profile.get("base_url", "")
        account_type = profile.get("account_type")
        if not account_type:
            account_type = "official" if base_url == "https://api.openai.com/v1" else "proxy"
        accounts.append(
            {
                "name": name,
                "api_key": profile.get("api_key", ""),
                "base_url": base_url,
                "account_type": account_type,
                "is_team": "0",
            }
        )
    return accounts


def extract_host(base_url: str) -> str:
    if not base_url:
        return ""
    if base_url.startswith("http://") or base_url.startswith("https://"):
        parsed = urlparse(base_url)
        return parsed.hostname or ""
    return base_url


def apply_env_for_account(account: Dict[str, str]) -> None:
    os.environ["OPENAI_API_KEY"] = account.get("api_key", "")
    os.environ["OPENAI_BASE_URL"] = account.get("base_url", "")
    if account.get("is_team") == "1":
        org_id = account.get("org_id", "")
        if org_id:
            os.environ["OPENAI_ORG_ID"] = org_id
        else:
            os.environ.pop("OPENAI_ORG_ID", None)
            print("警告：Team 配置缺少 org_id，已忽略 OPENAI_ORG_ID。")
    else:
        os.environ.pop("OPENAI_ORG_ID", None)
    print(f"当前账号：{account.get('name', '')}")
    print(f"Base URL：{account.get('base_url', '')}")


def _build_codex_search_paths() -> List[str]:
    paths = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    appdata = os.environ.get("APPDATA")
    if appdata:
        npm_bin = Path(appdata) / "npm"
        if npm_bin.is_dir():
            paths.insert(0, str(npm_bin))
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        npm_global = Path(userprofile) / ".npm-global" / "bin"
        if npm_global.is_dir():
            paths.insert(0, str(npm_global))
    return paths


def _which_in_paths(cmd: str, paths: List[str]) -> Optional[str]:
    exts = [".exe", ".cmd", ".bat", ".ps1", ""]
    for base in paths:
        for ext in exts:
            name = cmd if cmd.lower().endswith(ext) else f"{cmd}{ext}"
            candidate = Path(base) / name
            if candidate.is_file():
                return str(candidate)
    return None


def pick_best_match(lines: List[str]) -> Optional[str]:
    items = [line.strip() for line in lines if line.strip()]
    if not items:
        return None
    priority = [".exe", ".cmd", ".bat", ".ps1", ""]
    for ext in priority:
        for item in items:
            if ext:
                if item.lower().endswith(ext):
                    return item
            else:
                if Path(item).suffix == "":
                    return item
    return items[0]


def get_where_exe() -> Optional[str]:
    exe = shutil.which("where") or shutil.which("where.exe")
    if exe:
        return exe
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        candidate = Path(system_root) / "System32" / "where.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def find_codex_exe() -> Optional[str]:
    exe = shutil.which("codex")
    if exe:
        return exe
    exe = _which_in_paths("codex", _build_codex_search_paths())
    if exe:
        return exe
    where_exe = get_where_exe()
    if where_exe:
        try:
            creationflags = 0x08000000 if os.name == "nt" else 0
            proc = subprocess.run([where_exe, "codex"], capture_output=True, text=True, timeout=2, creationflags=creationflags)
            if proc.returncode == 0:
                lines = (proc.stdout or "").splitlines()
                best = pick_best_match(lines)
                if best:
                    return best
        except Exception:
            return None
    return None


def run_codex_chat() -> None:
    exe = find_codex_exe()
    if not exe:
        raise FileNotFoundError("未找到 codex 命令，请确认已安装并加入 PATH。")
    if exe.lower().endswith(".ps1"):
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, "chat", "-m", "gpt-5.2-codex"], check=False)
    else:
        subprocess.run([exe, "chat", "-m", "gpt-5.2-codex"], check=False)




def check_codex_available() -> bool:
    return find_codex_exe() is not None


def get_active_account(store: Dict[str, object]) -> Dict[str, str]:
    active = store.get("active")
    if isinstance(active, str):
        if active.startswith("team:"):
            name = active[5:]
            teams = store.get("teams")
            if isinstance(teams, dict) and name in teams:
                data = dict(teams[name])
                data["name"] = name
                data["is_team"] = "1"
                return data
        else:
            profiles = store.get("profiles")
            if isinstance(profiles, dict) and active in profiles:
                data = dict(profiles[active])
                data["name"] = active
                data["is_team"] = "0"
                return data
    return {}


def set_active_account(store: Dict[str, object], account: Dict[str, str]) -> None:
    name = account.get("name", "")
    if not name:
        store["active"] = None
    elif account.get("is_team") == "1":
        store["active"] = f"team:{name}"
    else:
        store["active"] = name
    save_store(store)


def upsert_account(
    store: Dict[str, object],
    name: str,
    base_url: str,
    api_key: str,
    org_id: str,
    is_team: bool,
    account_type: Optional[str] = None,
) -> None:
    if is_team:
        teams = store.get("teams")
        if not isinstance(teams, dict):
            teams = {}
            store["teams"] = teams
        profiles = store.get("profiles")
        if isinstance(profiles, dict):
            profiles.pop(name, None)
        teams[name] = {"base_url": base_url, "api_key": api_key, "org_id": org_id}
    else:
        profiles = store.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
            store["profiles"] = profiles
        teams = store.get("teams")
        if isinstance(teams, dict):
            teams.pop(name, None)
        profile_data = {"base_url": base_url, "api_key": api_key}
        if account_type:
            profile_data["account_type"] = account_type
        profiles[name] = profile_data
    save_store(store)


def delete_account(store: Dict[str, object], account: Dict[str, str]) -> None:
    name = account.get("name", "")
    if not name:
        return
    if account.get("is_team") == "1":
        teams = store.get("teams")
        if isinstance(teams, dict):
            teams.pop(name, None)
    else:
        profiles = store.get("profiles")
        if isinstance(profiles, dict):
            profiles.pop(name, None)
    active = store.get("active")
    if active in (name, f"team:{name}"):
        store["active"] = None
    save_store(store)


def post_json(url: str, headers: Dict[str, str], payload: Dict[str, object], timeout: int = 90) -> Tuple[bool, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        return True, body
    except urllib_error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return False, f"HTTP {exc.code}: {body or exc.reason}"
    except Exception as exc:
        return False, str(exc)


def error_summary(message: str) -> str:
    msg = message.lower()
    if "model" in msg:
        return "model_not_found_or_not_allowed"
    if "401" in msg or "403" in msg:
        return "auth_failed"
    if "404" in msg:
        return "endpoint_not_supported"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "other_error"


def test_model(
    base: str,
    headers: Dict[str, str],
    model: str,
    retries: int = 3,
    wait_seconds: int = 2,
    timeout: int = 90,
) -> Dict[str, object]:
    payload = {"model": model, "input": "ping"}
    last_err = ""
    for i in range(1, retries + 1):
        ok, msg = post_json(f"{base}/responses", headers, payload, timeout=timeout)
        if ok:
            return {"model": model, "ok": True, "endpoint": "responses", "error": ""}
        last_err = msg
        if i < retries:
            time.sleep(wait_seconds)
    return {
        "model": model,
        "ok": False,
        "endpoint": "responses",
        "error": f"{error_summary(last_err)}: {last_err}",
    }


def log_exception(exc: Exception) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{timestamp}] {exc}\n")
        traceback.print_exc(file=fh)
        fh.write("\n")


def _clear_windows_attributes_temporarily(path: Path) -> Optional[int]:
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    get_attrs = kernel32.GetFileAttributesW
    get_attrs.argtypes = [ctypes.c_wchar_p]
    get_attrs.restype = ctypes.c_uint32
    attrs = get_attrs(str(path))
    if attrs == 0xFFFFFFFF:
        return None
    mask = _WIN_HIDDEN | _WIN_READONLY
    if not (attrs & mask):
        return None
    set_attrs = kernel32.SetFileAttributesW
    set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    cleared = attrs & ~mask
    if set_attrs(str(path), cleared):
        return attrs
    return None


def safe_write_text(path: Path, data: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_attrs = _clear_windows_attributes_temporarily(path)
    try:
        path.write_text(data, encoding=encoding)
    finally:
        if original_attrs is not None:
            kernel32 = ctypes.windll.kernel32
            set_attrs = kernel32.SetFileAttributesW
            set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            set_attrs(str(path), original_attrs)


def safe_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_attrs = _clear_windows_attributes_temporarily(path)
    try:
        path.write_bytes(data)
    finally:
        if original_attrs is not None:
            kernel32 = ctypes.windll.kernel32
            set_attrs = kernel32.SetFileAttributesW
            set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            set_attrs(str(path), original_attrs)


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
        return True
    except ValueError:
        return False


UPDATE_MANIFEST_NAMES = {
    "codex_update.yml",
    "codex_update.yaml",
    "update.yml",
    "update.yaml",
}


def _config_update_state_path(userprofile: Path) -> Path:
    return userprofile / ".codex-config-switch" / "package_update_state.json"


def _load_config_update_state(userprofile: Path) -> Dict[str, object]:
    path = _config_update_state_path(userprofile)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get_config_update_version_state(userprofile: Optional[Path] = None) -> Dict[str, object]:
    base = userprofile or USERPROFILE_DIR
    state = _load_config_update_state(base)
    version = state.get("latest_version")
    updated_at = state.get("updated_at")
    manifest = state.get("latest_manifest")
    zip_path = state.get("latest_zip_path")
    return {
        "version": str(version).strip() if isinstance(version, (str, int, float)) else "",
        "updated_at": str(updated_at).strip() if isinstance(updated_at, str) else "",
        "manifest": str(manifest).strip() if isinstance(manifest, str) else "",
        "zip_path": str(zip_path).strip() if isinstance(zip_path, str) else "",
        "state_path": str(_config_update_state_path(base)),
    }


def _save_config_update_state(userprofile: Path, data: Dict[str, object]) -> None:
    safe_write_text(_config_update_state_path(userprofile), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _manifest_version(manifest: Dict[str, object]) -> str:
    for key in ("version", "package_version", "update_version"):
        value = manifest.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    package = manifest.get("package")
    if isinstance(package, dict):
        value = package.get("version")
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _version_tokens(value: str) -> List[object]:
    tokens: List[object] = []
    for token in re.findall(r"\d+|[A-Za-z]+", value.lower()):
        if token.isdigit():
            tokens.append(int(token))
        else:
            tokens.append(token)
    return tokens


def _compare_update_versions(left: str, right: str) -> int:
    left_tokens = _version_tokens(left)
    right_tokens = _version_tokens(right)
    if not left_tokens or not right_tokens:
        left_norm = left.strip().lower()
        right_norm = right.strip().lower()
        return (left_norm > right_norm) - (left_norm < right_norm)

    for left_item, right_item in zip_longest(left_tokens, right_tokens, fillvalue=0):
        if left_item == right_item:
            continue
        if isinstance(left_item, int) and isinstance(right_item, int):
            return (left_item > right_item) - (left_item < right_item)
        left_text = str(left_item)
        right_text = str(right_item)
        return (left_text > right_text) - (left_text < right_text)
    return 0


def _config_update_version_warning(package_version: str, recorded_version: str) -> str:
    if not package_version or not recorded_version:
        return ""
    if _compare_update_versions(package_version, recorded_version) <= 0:
        return f"更新包版本 {package_version} 小于或等于已记录版本 {recorded_version}，可能是重复应用或回退更新。"
    return ""


def _record_config_update_version(userprofile: Path, version: str, manifest_path: str, zip_path: Path) -> None:
    state = _load_config_update_state(userprofile)
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    entry = {
        "version": version,
        "manifest": manifest_path,
        "zip_path": str(zip_path),
        "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    history.append(entry)
    state["latest_version"] = version
    state["latest_manifest"] = manifest_path
    state["latest_zip_path"] = str(zip_path)
    state["updated_at"] = entry["applied_at"]
    state["history"] = history[-20:]
    _save_config_update_state(userprofile, state)


def _normalize_posix_parts(value: object, allow_trailing_slash: bool = False) -> Optional[List[str]]:
    if not isinstance(value, str):
        return None
    raw = value.replace("\\", "/").strip()
    if not raw or "\x00" in raw:
        return None
    if raw.endswith("/") and allow_trailing_slash:
        raw = raw.rstrip("/")
    elif raw.endswith("/"):
        raw = raw.rstrip("/")
    if not raw:
        return None
    posix_path = PurePosixPath(raw)
    if posix_path.is_absolute():
        return None
    parts = [part for part in posix_path.parts if part not in ("", ".")]
    if not parts:
        return None
    for part in parts:
        if part == ".." or ":" in part:
            return None
    return parts


def _target_for_config_update_path(value: object, userprofile: Path) -> Tuple[Path, str]:
    parts = _normalize_posix_parts(value)
    if not parts:
        raise ValueError("目标路径为空或格式无效")
    codex_dir = userprofile / ".codex"
    switcher_dir = userprofile / ".codex-config-switch"
    root = parts[0].lower()
    rest = parts[1:]
    if root == ".codex":
        target = codex_dir / Path(*rest) if rest else codex_dir
    elif root == ".codex-config-switch":
        target = switcher_dir / Path(*rest) if rest else switcher_dir
    else:
        raise ValueError("目标路径必须位于 .codex 或 .codex-config-switch 下")

    if not (_is_relative_to(target, codex_dir) or _is_relative_to(target, switcher_dir)):
        raise ValueError("目标路径越界")
    backup_root = switcher_dir / "package_update_backups"
    if _is_relative_to(target, backup_root):
        raise ValueError("不允许直接修改配置更新备份目录")
    return target, str(PurePosixPath(*parts))


def _manifest_entry_path(info_name: str) -> Optional[str]:
    parts = _normalize_posix_parts(info_name, allow_trailing_slash=True)
    if not parts:
        return None
    return str(PurePosixPath(*parts))


def _find_config_update_manifest(archive: zipfile.ZipFile) -> Tuple[str, str]:
    candidates: List[Tuple[str, str]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        normalized = _manifest_entry_path(info.filename)
        if not normalized:
            continue
        if PurePosixPath(normalized).name.lower() in UPDATE_MANIFEST_NAMES:
            candidates.append((normalized, info.filename))
    if not candidates:
        raise ValueError("压缩包中未找到 codex_update.yml")
    candidates.sort(key=lambda item: (len(PurePosixPath(item[0]).parts), item[0].lower()))
    normalized_path, archive_path = candidates[0]
    manifest_dir = str(PurePosixPath(normalized_path).parent)
    if manifest_dir == ".":
        manifest_dir = ""
    return archive_path, manifest_dir


def _read_config_update_manifest(archive: zipfile.ZipFile, manifest_path: str) -> Dict[str, object]:
    try:
        raw = archive.read(manifest_path).decode("utf-8-sig")
    except Exception as exc:
        raise ValueError(f"无法读取更新清单：{exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except Exception as exc:
        raise ValueError(f"更新清单 YAML 无法解析：{exc}") from exc
    if data is None:
        data = {}
    if isinstance(data, list):
        return {"operations": data}
    if not isinstance(data, dict):
        raise ValueError("更新清单顶层必须是对象或操作列表")
    return data


def _manifest_operations(manifest: Dict[str, object]) -> List[object]:
    operations = manifest.get("operations")
    if operations is None:
        operations = manifest.get("actions")
    if operations is None:
        operations = manifest.get("ops")
    if not isinstance(operations, list):
        raise ValueError("更新清单缺少 operations 列表")
    return operations


def _normalize_update_action(value: object) -> str:
    if not isinstance(value, str):
        return ""
    action = value.strip().lower()
    if action in ("copy", "write", "add", "replace", "overwrite", "upsert"):
        return "copy"
    if action in ("delete", "remove", "rm"):
        return "delete"
    if action in ("mkdir", "create_dir", "create-directory"):
        return "mkdir"
    return action


def _operation_field(op: Dict[str, object], *names: str) -> object:
    for name in names:
        if name in op:
            return op[name]
    return None


def _manifest_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "y", "on"):
            return True
        if lowered in ("0", "false", "no", "n", "off"):
            return False
    if value is None:
        return default
    return bool(value)


def _source_path_from_manifest(value: object, manifest_dir: str) -> Optional[str]:
    parts = _normalize_posix_parts(value, allow_trailing_slash=True)
    if not parts:
        return None
    base_parts: List[str] = []
    if manifest_dir:
        base_parts = list(PurePosixPath(manifest_dir).parts)
    return str(PurePosixPath(*(base_parts + parts)))


def _zip_file_index(archive: zipfile.ZipFile) -> Dict[str, zipfile.ZipInfo]:
    result: Dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        normalized = _manifest_entry_path(info.filename)
        if normalized:
            result[normalized] = info
    return result


def _copy_plan_entries(
    op_index: int,
    op: Dict[str, object],
    manifest_dir: str,
    zip_files: Dict[str, zipfile.ZipInfo],
    userprofile: Path,
) -> Tuple[List[Dict[str, object]], Optional[str]]:
    source_value = _operation_field(op, "source", "src", "from")
    target_value = _operation_field(op, "target", "dest", "destination", "to", "path")
    source = _source_path_from_manifest(source_value, manifest_dir)
    if not source:
        return [], "copy 操作缺少有效 source"
    try:
        target_path, rel_target = _target_for_config_update_path(target_value, userprofile)
    except Exception as exc:
        return [], str(exc)

    overwrite = _manifest_bool(op.get("overwrite"), True)
    if source in zip_files:
        info = zip_files[source]
        return [
            {
                "action": "copy",
                "operation_index": op_index,
                "source": source,
                "entry": info.filename,
                "target": str(target_path),
                "relative_target": rel_target,
                "size": info.file_size,
                "exists": target_path.exists(),
                "overwrite": overwrite,
            }
        ], None

    prefix = source.rstrip("/") + "/"
    matched: List[Dict[str, object]] = []
    for normalized, info in sorted(zip_files.items()):
        if not normalized.startswith(prefix):
            continue
        rel_parts = PurePosixPath(normalized).relative_to(PurePosixPath(source)).parts
        child_target = target_path / Path(*rel_parts)
        child_rel = str(PurePosixPath(rel_target, *rel_parts))
        matched.append(
            {
                "action": "copy",
                "operation_index": op_index,
                "source": normalized,
                "entry": info.filename,
                "target": str(child_target),
                "relative_target": child_rel,
                "size": info.file_size,
                "exists": child_target.exists(),
                "overwrite": overwrite,
            }
        )
    if not matched:
        return [], f"source 不存在于压缩包：{source}"
    return matched, None


def _single_target_plan_entry(
    op_index: int,
    action: str,
    op: Dict[str, object],
    userprofile: Path,
) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    target_value = _operation_field(op, "target", "dest", "destination", "to", "path")
    try:
        target_path, rel_target = _target_for_config_update_path(target_value, userprofile)
    except Exception as exc:
        return None, str(exc)
    if action == "delete" and rel_target in (".codex", ".codex-config-switch"):
        return None, "不允许删除配置根目录"
    return {
        "action": action,
        "operation_index": op_index,
        "target": str(target_path),
        "relative_target": rel_target,
        "exists": target_path.exists(),
        "is_dir": target_path.is_dir(),
    }, None


def _build_config_update_plan(archive: zipfile.ZipFile, userprofile: Path) -> Dict[str, object]:
    manifest_path, manifest_dir = _find_config_update_manifest(archive)
    manifest = _read_config_update_manifest(archive, manifest_path)
    package_version = _manifest_version(manifest)
    update_state = _load_config_update_state(userprofile)
    recorded_raw = update_state.get("latest_version")
    recorded_version = str(recorded_raw).strip() if isinstance(recorded_raw, (str, int, float)) else ""
    version_warning = _config_update_version_warning(package_version, recorded_version)
    operations_raw = _manifest_operations(manifest)
    zip_files = _zip_file_index(archive)
    operations: List[Dict[str, object]] = []
    skipped: List[Dict[str, str]] = []

    for idx, raw_op in enumerate(operations_raw, start=1):
        if not isinstance(raw_op, dict):
            skipped.append({"operation": str(idx), "reason": "操作必须是对象"})
            continue
        action = _normalize_update_action(_operation_field(raw_op, "action", "op"))
        if action == "copy":
            entries, error = _copy_plan_entries(idx, raw_op, manifest_dir, zip_files, userprofile)
            if error:
                skipped.append({"operation": str(idx), "action": "copy", "reason": error})
                continue
            operations.extend(entries)
        elif action in ("delete", "mkdir"):
            entry, error = _single_target_plan_entry(idx, action, raw_op, userprofile)
            if error:
                skipped.append({"operation": str(idx), "action": action, "reason": error})
                continue
            if entry:
                operations.append(entry)
        else:
            skipped.append({"operation": str(idx), "reason": f"不支持的 action：{action or '-'}"})

    return {
        "manifest": manifest_path,
        "package_version": package_version,
        "recorded_version": recorded_version,
        "version_warning": version_warning,
        "operations": operations,
        "files": [op for op in operations if op.get("action") == "copy"],
        "skipped": skipped,
        "backup_root": str((userprofile / ".codex-config-switch") / "package_update_backups"),
        "version_state_path": str(_config_update_state_path(userprofile)),
    }


def _backup_existing_path(path: Path, rel_target: str, backup_dir: Path, backed_up: set[str]) -> Optional[Path]:
    key = str(path.resolve(strict=False)).lower()
    if key in backed_up or not path.exists():
        return None
    backup_path = backup_dir / Path(rel_target)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, backup_path, dirs_exist_ok=True)
    else:
        shutil.copy2(path, backup_path)
    backed_up.add(key)
    return backup_path


def _delete_existing_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        original_attrs = _clear_windows_attributes_temporarily(path)
        try:
            path.unlink()
        finally:
            if original_attrs is not None and path.exists():
                kernel32 = ctypes.windll.kernel32
                set_attrs = kernel32.SetFileAttributesW
                set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
                set_attrs(str(path), original_attrs)


def plan_config_update_zip(zip_path: Path, userprofile: Optional[Path] = None) -> Dict[str, object]:
    base = userprofile or USERPROFILE_DIR
    path = Path(zip_path)
    if not path.is_file():
        raise FileNotFoundError(f"压缩包不存在：{path}")

    try:
        with zipfile.ZipFile(path, "r") as archive:
            plan = _build_config_update_plan(archive, base)
    except zipfile.BadZipFile as exc:
        raise ValueError("压缩包无法读取或格式无效") from exc
    plan["zip_path"] = str(path)
    return plan


def apply_config_update_zip(zip_path: Path, userprofile: Optional[Path] = None) -> Dict[str, object]:
    base = userprofile or USERPROFILE_DIR
    path = Path(zip_path)
    if not path.is_file():
        raise FileNotFoundError(f"压缩包不存在：{path}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = (base / ".codex-config-switch") / "package_update_backups" / f"config_update_{stamp}"
    created: List[str] = []
    updated: List[str] = []
    deleted: List[str] = []
    dirs_created: List[str] = []
    unchanged: List[str] = []
    failed: List[Dict[str, str]] = []
    backups: List[str] = []
    backed_up: set[str] = set()
    version_recorded = False
    version_record_error = ""

    try:
        with zipfile.ZipFile(path, "r") as archive:
            plan = _build_config_update_plan(archive, base)
            operations = plan.get("operations")
            if not isinstance(operations, list) or not operations:
                raise ValueError("更新清单中没有可应用的操作")

            for op in operations:
                if not isinstance(op, dict):
                    continue
                action = str(op.get("action", ""))
                rel_target = str(op.get("relative_target", ""))
                target = Path(str(op.get("target", "")))
                try:
                    if action == "copy":
                        entry = str(op.get("entry", ""))
                        data = archive.read(entry)
                        overwrite = _manifest_bool(op.get("overwrite"), True)

                        if target.exists() and target.is_dir():
                            failed.append({"action": action, "target": rel_target, "error": "目标位置已存在同名目录"})
                            continue

                        current = target.read_bytes() if target.exists() else None
                        if current == data:
                            unchanged.append(rel_target)
                            continue
                        if current is not None and not overwrite:
                            failed.append({"action": action, "target": rel_target, "error": "目标已存在且 overwrite=false"})
                            continue
                        backup_path = _backup_existing_path(target, rel_target, backup_dir, backed_up)
                        if backup_path is not None:
                            backups.append(str(backup_path))
                        safe_write_bytes(target, data)
                        if current is None:
                            created.append(rel_target)
                        else:
                            updated.append(rel_target)
                    elif action == "delete":
                        if not target.exists():
                            unchanged.append(rel_target)
                            continue
                        backup_path = _backup_existing_path(target, rel_target, backup_dir, backed_up)
                        if backup_path is not None:
                            backups.append(str(backup_path))
                        _delete_existing_path(target)
                        deleted.append(rel_target)
                    elif action == "mkdir":
                        if target.exists() and not target.is_dir():
                            failed.append({"action": action, "target": rel_target, "error": "目标位置已存在同名文件"})
                            continue
                        if target.is_dir():
                            unchanged.append(rel_target)
                            continue
                        target.mkdir(parents=True, exist_ok=True)
                        dirs_created.append(rel_target)
                except Exception as exc:
                    failed.append({"action": action, "target": rel_target, "error": str(exc)})
    except zipfile.BadZipFile as exc:
        raise ValueError("压缩包无法读取或格式无效") from exc

    package_version = str(plan.get("package_version") or "").strip()
    if package_version and not failed:
        try:
            _record_config_update_version(base, package_version, str(plan.get("manifest") or ""), path)
            version_recorded = True
        except Exception as exc:
            version_record_error = str(exc)

    result = dict(plan)
    result["zip_path"] = str(path)
    result.update(
        {
            "backup_dir": str(backup_dir) if backups else "",
            "backups": backups,
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "dirs_created": dirs_created,
            "unchanged": unchanged,
            "failed": failed,
            "version_recorded": version_recorded,
            "version_record_error": version_record_error,
        }
    )
    return result


