from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable


MAX_CUSTOM_TAGS = 20
MAX_CUSTOM_TAG_LENGTH = 64
PRIVATE_JOB_TAG_PREFIX = "ti_job_"


def normalize_qbt_tag(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("qBittorrent tags must be text")

    tag = value.strip()
    if not tag:
        raise ValueError("qBittorrent tags cannot be empty")
    if "," in tag:
        raise ValueError("qBittorrent tags cannot contain commas")
    return tag


def _validate_intake_tag_policy(tag: str, *, description: str) -> str:
    if len(tag) > MAX_CUSTOM_TAG_LENGTH:
        raise ValueError(
            f"Torrent Intake {description} must be {MAX_CUSTOM_TAG_LENGTH} characters or fewer"
        )
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in tag):
        raise ValueError(
            f"Torrent Intake {description} cannot contain control or surrogate characters"
        )
    return tag


def normalize_custom_tag(value: str) -> str:
    return _validate_intake_tag_policy(
        normalize_qbt_tag(value),
        description="custom tags",
    )


def normalize_managed_tag(value: str) -> str:
    tag = _validate_intake_tag_policy(
        normalize_qbt_tag(value),
        description="managed tag",
    )
    if tag.casefold().startswith(PRIVATE_JOB_TAG_PREFIX.casefold()):
        raise ValueError(
            f"Torrent Intake's managed tag cannot use the private {PRIVATE_JOB_TAG_PREFIX} namespace"
        )
    return tag


def is_reserved_custom_tag(tag: str, reserved_tags: Iterable[str] = ()) -> bool:
    folded = tag.casefold()
    if folded.startswith(PRIVATE_JOB_TAG_PREFIX.casefold()):
        return True

    reserved = {
        value.strip().casefold()
        for value in reserved_tags
        if isinstance(value, str) and value.strip()
    }
    return folded in reserved


def normalize_custom_tags(
    values: Iterable[str] | None,
    *,
    reserved_tags: Iterable[str] = (),
) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise ValueError("custom_tags must be a list of tag names")

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = normalize_custom_tag(value)
        if is_reserved_custom_tag(tag, reserved_tags):
            raise ValueError(f"qBittorrent tag '{tag}' is reserved for Torrent Intake")
        if tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)

    if len(normalized) > MAX_CUSTOM_TAGS:
        raise ValueError(f"at most {MAX_CUSTOM_TAGS} custom qBittorrent tags are allowed")
    return normalized


def filter_selectable_custom_tags(
    values: Iterable[str],
    *,
    reserved_tags: Iterable[str] = (),
) -> list[str]:
    selectable: list[str] = []
    seen: set[str] = set()
    for value in values:
        try:
            tag = normalize_custom_tag(value)
        except ValueError:
            continue
        if is_reserved_custom_tag(tag, reserved_tags) or tag in seen:
            continue
        seen.add(tag)
        selectable.append(tag)
    return sorted(selectable, key=lambda tag: (tag.casefold(), tag))


def encode_custom_tags(values: Iterable[str] | None) -> str:
    return json.dumps(normalize_custom_tags(values), ensure_ascii=False, separators=(",", ":"))


def decode_custom_tags(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored custom qBittorrent tags are not valid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError("stored custom qBittorrent tags must be a JSON list")
    return normalize_custom_tags(decoded)
