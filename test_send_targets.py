"""send_message target parsing for the Status plugin.

Run with the Hermes venv python (see README "Tests").
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.expanduser(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")))
import hermes_bootstrap  # noqa: E402,F401  activates Hermes' isolated runtime (core deps)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from status.adapter import parse_send_target  # noqa: E402

GROUP_ID = (
    "11111111-2222-4333-8444-555555555555-0x04aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "ccccccccccccccccc"
)
PUBKEY = "0x04" + "a" * 128


def test_group_chat_id_is_explicit():
    # Regression: a composite group id used to parse to nothing and fall back to the
    # home channel, delivering a group message into the operator's DM.
    assert parse_send_target(GROUP_ID) == (GROUP_ID, None)


def test_pubkey_is_explicit_and_stripped():
    assert parse_send_target(PUBKEY) == (PUBKEY, None)
    assert parse_send_target(" 0x00abcdef123456 ") == ("0x00abcdef123456", None)


def test_friendly_name_is_left_to_directory_resolution():
    assert parse_send_target("Jelly the housing assistant") is None
    assert parse_send_target("") is None
