from __future__ import annotations

from takealot_ops.cli import build_parser


def test_help_lists_all_commands(capsys) -> None:
    parser = build_parser()
    parser.print_help()
    help_text = capsys.readouterr().out
    for command in (
        "collect",
        "collect-competitors",
        "export",
        "daily-run",
        "daily-report-run",
        "daily-report-capture",
        "daily-report-deadline",
        "dashboard",
        "migrate-to-mysql",
        "verify",
    ):
        assert command in help_text

