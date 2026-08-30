from __future__ import annotations

import pytest

from human_gate.xurl_adapter import (
    parse_terminal_xurl_write,
    terminal_xurl_write_is_unsupported,
    terminal_xurl_write_matches,
)


def test_simple_terminal_quote_is_safely_projectable() -> None:
    parsed = parse_terminal_xurl_write(
        {
            "command": (
                "xurl --app life --username DomAtSiteSage quote "
                "2093515564786540695 'plain approved text'"
            )
        }
    )

    assert parsed is not None
    assert parsed.action == "quote"
    assert parsed.account == "DomAtSiteSage"
    assert parsed.text == "plain approved text"
    assert parsed.quote_post_id == "2093515564786540695"


def test_raw_xurl_mutation_is_blocked_instead_of_projected() -> None:
    args = {"command": "xurl -X POST /2/tweets -d '{not persisted}'"}

    assert parse_terminal_xurl_write(args) is None
    assert terminal_xurl_write_is_unsupported(args) is True


def test_read_only_xurl_command_is_not_classified_as_a_write() -> None:
    args = {"command": "xurl read 2093515564786540695"}

    assert parse_terminal_xurl_write(args) is None
    assert terminal_xurl_write_is_unsupported(args) is False


def test_xurl_filename_in_unrelated_shell_command_is_not_classified_as_a_write() -> None:
    args = {
        "command": ("git add human_gate/xurl_adapter.py && git commit -m 'Test adapter changes'")
    }

    assert parse_terminal_xurl_write(args) is None
    assert terminal_xurl_write_matches(args) is False
    assert terminal_xurl_write_is_unsupported(args) is False


@pytest.mark.parametrize(
    "command",
    [
        "xurl.exe post 'blocked'",
        "./xurl.exe post 'blocked'",
        "C:/Tools/xurl.exe post 'blocked'",
    ],
)
def test_windows_xurl_executable_alias_is_gated(command: str) -> None:
    assert terminal_xurl_write_matches({"command": command}) is True
    assert terminal_xurl_write_is_unsupported({"command": command}) is False


@pytest.mark.parametrize(
    "command",
    [
        "env xurl post 'blocked'",
        "FOO=bar xurl post 'blocked'",
        "command xurl post 'blocked'",
    ],
)
def test_prefixed_xurl_write_commands_fail_closed(command: str) -> None:
    assert terminal_xurl_write_matches({"command": command}) is False
    assert terminal_xurl_write_is_unsupported({"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        'x"ur"l -X POST /2/tweets -d \'{"text":"blocked"}\'',
        "x\\url reply 2093515564786540695 'blocked'",
        "cmd=xurl; $cmd post 'blocked'",
        "$(printf xurl) post 'blocked'",
    ],
)
def test_shell_obfuscated_xurl_write_commands_fail_closed(command: str) -> None:
    assert terminal_xurl_write_matches({"command": command}) is False
    assert terminal_xurl_write_is_unsupported({"command": command}) is True
