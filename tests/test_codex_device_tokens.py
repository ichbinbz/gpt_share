import json

import pytest

from scripts.codex_device_tokens import (
    create_token,
    find_device,
    load_registry,
    revoke_active_devices,
    token_hash,
)


def test_device_registry_stores_only_hash(tmp_path):
    registry = {"version": 1, "devices": []}
    token = create_token(registry, "研发-PC01")
    device = find_device(registry, "研发-PC01")
    assert token.startswith("cwsdt_")
    assert device["token_hash"] == token_hash(token)
    assert token not in json.dumps(registry)


def test_device_label_requires_rotation():
    registry = {"version": 1, "devices": []}
    create_token(registry, "研发-PC01")
    with pytest.raises(ValueError, match="use rotate"):
        create_token(registry, "研发-PC01")
    replacement = create_token(registry, "研发-PC01", rotate=True)
    assert replacement.startswith("cwsdt_")
    assert [device["enabled"] for device in registry["devices"]] == [False, True]


def test_missing_registry_is_initialized(tmp_path):
    assert load_registry(tmp_path / "missing.json") == {"version": 1, "devices": []}


def test_repeated_rotate_and_revoke_leave_no_old_token_enabled():
    registry = {"version": 1, "devices": []}
    create_token(registry, "employee-pc")
    create_token(registry, "employee-pc", rotate=True)
    create_token(registry, "employee-pc", rotate=True)

    assert [device["enabled"] for device in registry["devices"]] == [False, False, True]
    assert revoke_active_devices(registry, "employee-pc") == 1
    assert not any(device["enabled"] for device in registry["devices"])
