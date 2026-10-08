from __future__ import annotations

import pytest

from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    NetworkMode,
    NetworkPolicy,
    ProcessPolicy,
    normalize_relative_path,
)


@pytest.mark.parametrize(
    "invalid",
    ("deny", "approved_hosts", "DENY", "", None, 0, True, False, object()),
)
def test_network_policy_requires_actual_mode_enum(invalid: object) -> None:
    with pytest.raises(ValueError, match="must be a NetworkMode"):
        NetworkPolicy(mode=invalid)


def test_spoofed_deny_cannot_carry_approved_hosts() -> None:
    with pytest.raises(ValueError, match="must be a NetworkMode"):
        NetworkPolicy(mode="deny", approved_hosts=("unapproved.example",))


@pytest.mark.parametrize(
    "invalid",
    (["example.com"], "example.com", ("",), (" example.com",), ("example.com ",),
     (None,), (123,), []),
)
def test_network_policy_rejects_malformed_approved_host_carriers(
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="tuple of nonempty canonical text"):
        NetworkPolicy(mode=NetworkMode.APPROVED_HOSTS, approved_hosts=invalid)


def test_network_policy_preserves_explicit_deny_and_approved_hosts() -> None:
    assert NetworkPolicy().mode is NetworkMode.DENY
    assert NetworkPolicy(mode=NetworkMode.APPROVED_HOSTS, approved_hosts=("example.com",)).mode is (
        NetworkMode.APPROVED_HOSTS
    )
    with pytest.raises(ValueError, match="DENY"):
        NetworkPolicy(mode=NetworkMode.DENY, approved_hosts=("example.com",))
    with pytest.raises(ValueError, match="at least one host"):
        NetworkPolicy(mode=NetworkMode.APPROVED_HOSTS)


@pytest.mark.parametrize("invalid", (True, 0, 1, None, "", 0.0, (), []))
def test_process_policy_requires_exact_disabled_shell_boolean(invalid: object) -> None:
    with pytest.raises(ValueError, match="generic shell execution is not allowed"):
        ProcessPolicy(("python",), shell_allowed=invalid)


def test_process_policy_preserves_explicit_disabled_shell() -> None:
    assert ProcessPolicy(("python",), shell_allowed=False).shell_allowed is False


@pytest.mark.parametrize(
    "invalid",
    ([], "python", (), ("",), ("   ",), (None,), (123,), ("python\x00",)),
)
def test_process_policy_rejects_malformed_executable_allowlist(invalid: object) -> None:
    with pytest.raises(ValueError, match="executable"):
        ProcessPolicy(invalid)


@pytest.mark.parametrize(
    "invalid",
    ([], "python", (), ("",), (None,), (123,), ("python\x00",), ("python", 1)),
)
def test_acceptance_command_rejects_malformed_argument_carriers(
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="argv must be nonempty text arguments"):
        AcceptanceCommand(invalid)


def test_acceptance_command_rejects_nontext_cwd() -> None:
    with pytest.raises(ValueError, match="cwd must be text"):
        AcceptanceCommand(("python",), cwd=None)


def test_valid_unicode_command_and_allowlist_are_preserved() -> None:
    assert ProcessPolicy(("C:\\Program Files\\Nika\\python.exe",)).shell_allowed is False
    command = AcceptanceCommand(("python", "-m", "pytest", "тести з пробілами"))
    assert command.argv[-1] == "тести з пробілами"

@pytest.mark.parametrize(
    "invalid",
    (
        "src",
        [],
        (),
        ("",),
        (" src",),
        ("src ",),
        ("src\nsecrets",),
        ("src\u0085secrets",),
        (None,),
        (123,),
    ),
)
def test_allowed_path_policy_rejects_ambiguous_root_carriers(invalid: object) -> None:
    with pytest.raises(ValueError, match="path|tuple|control"):
        AllowedPathPolicy(invalid)  # type: ignore[arg-type]


def test_allowed_path_policy_rejects_behavioral_root_text() -> None:
    class Root(str):
        pass

    with pytest.raises(ValueError, match="exact text"):
        AllowedPathPolicy((Root("src"),))


def test_allowed_path_policy_snapshots_canonical_root_identity() -> None:
    policy = AllowedPathPolicy(("src/./nika_core", "tests\\unit"))

    assert policy.roots == ("src/nika_core", "tests/unit")
    assert policy.allows("src/nika_core/module.py")
    assert policy.allows("tests/unit/test_module.py")
    assert not policy.allows("src2/module.py")


@pytest.mark.parametrize(
    "value",
    (
        " src/module.py",
        "src/module.py ",
        "src/\tmodule.py",
        "src/\x7fmodule.py",
        "src/\u2028module.py",
        "src/\u2029module.py",
    ),
)
def test_relative_path_rejects_ambiguous_text_identity(value: str) -> None:
    with pytest.raises(ValueError, match="canonical text|control"):
        normalize_relative_path(value)


def test_relative_path_rejects_behavioral_text_identity() -> None:
    class PathText(str):
        def strip(self) -> str:
            return "src/module.py"

    with pytest.raises(ValueError, match="exact text"):
        normalize_relative_path(PathText(" attacker"))

