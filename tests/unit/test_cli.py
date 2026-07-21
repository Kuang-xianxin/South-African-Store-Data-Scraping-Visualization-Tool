from __future__ import annotations

from takealot_ops.cli import build_parser


def test_help_lists_all_five_commands(capsys) -> None:
    parser = build_parser()
    parser.print_help()
    help_text = capsys.readouterr().out
    for command in ("collect", "export", "daily-run", "dashboard", "verify"):
        assert command in help_text

