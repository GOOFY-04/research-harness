from pathlib import Path

import deploy


def test_ensure_env_file_creates_once_and_preserves_user_values(tmp_path: Path):
    (tmp_path / ".env.example").write_text(
        "AGNES_API_KEY=your_agnes_api_key\n", encoding="utf-8"
    )

    assert deploy.ensure_env_file(tmp_path) == "created"
    (tmp_path / ".env").write_text("AGNES_API_KEY=secret\n", encoding="utf-8")
    assert deploy.ensure_env_file(tmp_path) == "preserved"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AGNES_API_KEY=secret\n"


def test_env_has_api_key_rejects_template_and_accepts_environment(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "AGNES_API_KEY=your_agnes_api_key\n", encoding="utf-8"
    )
    assert not deploy.env_has_api_key(tmp_path)

    monkeypatch.setenv("AGNES_API_KEY", "configured")
    assert deploy.env_has_api_key(tmp_path)


def test_remote_matches_https_and_ssh_forms():
    expected = "https://github.com/GOOFY-04/opencode.git"

    assert deploy.remote_matches(expected, expected)
    assert deploy.remote_matches("git@github.com:GOOFY-04/opencode.git", expected)
    assert not deploy.remote_matches("https://github.com/other/opencode.git", expected)


def test_parser_defaults_to_safe_deploy_without_starting():
    args = deploy.build_parser().parse_args([])

    assert not args.start
    assert not args.skip_checks
    assert not args.no_update
    assert args.opencode_branch == "research-harness"
