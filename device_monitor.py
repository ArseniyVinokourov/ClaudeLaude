"""Telegram device monitoring via MTProto (Telethon).

Periodically checks active Telegram sessions and alerts on new devices.
Requires: API_ID, API_HASH in .env, one-time phone auth via setup.
"""
import asyncio
import json
import os
import time

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_SESSION_PATH = os.path.join(BOT_DIR, ".tg_monitor")
_KNOWN_PATH = os.path.join(BOT_DIR, ".known_devices.json")


def _available() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    api_id = os.environ.get("TG_API_ID", "")
    api_hash = os.environ.get("TG_API_HASH", "")
    if not api_id or not api_hash:
        return False
    return os.path.exists(_SESSION_PATH + ".session")


def _load_known() -> dict[str, dict]:
    if not os.path.exists(_KNOWN_PATH):
        return {}
    try:
        with open(_KNOWN_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_known(devices: dict[str, dict]):
    with open(_KNOWN_PATH, "w") as f:
        json.dump(devices, f, indent=2)


def _device_key(auth) -> str:
    return f"{auth.device_model}|{auth.platform}|{auth.app_name}"


async def _fetch_sessions():
    from telethon import TelegramClient
    from telethon.tl.functions.account import GetAuthorizationsRequest

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    client = TelegramClient(_SESSION_PATH, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return None

    result = await client(GetAuthorizationsRequest())
    sessions = []
    for auth in result.authorizations:
        sessions.append({
            "hash": str(auth.hash),
            "device_model": auth.device_model,
            "platform": auth.platform,
            "system_version": auth.system_version,
            "app_name": auth.app_name,
            "ip": auth.ip,
            "country": auth.country,
            "date_active": auth.date_active.isoformat() if auth.date_active else "",
            "key": _device_key(auth),
        })
    await client.disconnect()
    return sessions


def get_sessions() -> list[dict] | None:
    if not _available():
        return None
    try:
        return asyncio.run(_fetch_sessions())
    except Exception:
        return None


def check_new_devices() -> list[dict]:
    sessions = get_sessions()
    if sessions is None:
        return []

    known = _load_known()
    new_devices = []

    for s in sessions:
        key = s["key"]
        if key not in known:
            new_devices.append(s)

    if not known:
        for s in sessions:
            known[s["key"]] = {
                "device_model": s["device_model"],
                "platform": s["platform"],
                "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        _save_known(known)
        return []

    return new_devices


def trust_device(key: str):
    known = _load_known()
    known[key] = {
        "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_known(known)
