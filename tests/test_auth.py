from pathlib import Path

import pytest

from ha_insights_mcp.__main__ import main
from ha_insights_mcp.auth import (
    AuthConfigError,
    StaticTokenVerifier,
    TokenEntry,
    generate_token,
    hash_token,
    load_tokens,
)
from ha_insights_mcp.cli import token_create

pytestmark = pytest.mark.anyio


def entry(name: str, token: str, scopes: list[str] | None = None) -> TokenEntry:
    return TokenEntry(name=name, sha256=hash_token(token), scopes=scopes or ["ha:read"])


async def test_verifier_accepts_known_token_only() -> None:
    reader, admin = generate_token(), generate_token()
    verifier = StaticTokenVerifier([entry("reader", reader), entry("admin", admin, ["ha:actions"])])
    token = await verifier.verify_token(admin)
    assert (token.client_id, token.scopes) == ("admin", ["ha:actions", "ha:read"])
    assert token.token == hash_token(admin)  # the raw secret is not kept
    assert (await verifier.verify_token(reader)).scopes == ["ha:read"]
    assert await verifier.verify_token("hai_wrong") is None
    assert await verifier.verify_token("") is None


def test_generated_tokens_are_unique_and_long() -> None:
    tokens = {generate_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(t.startswith("hai_") and len(t) > 40 for t in tokens)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "tokens.toml"
    path.write_text(text)
    return path


def test_load_tokens(tmp_path: Path) -> None:
    sha = hash_token("x")
    (loaded,) = load_tokens(write(tmp_path, f'[[tokens]]\nname = "a"\nsha256 = "{sha}"\n'))
    assert (loaded.name, loaded.scopes) == ("a", ["ha:read"])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no tokens"),
        ('[[tokens]]\nname = "a"\nsha256 = "plain-text-token"\n', "sha256"),
        (f'[[tokens]]\nname = "a"\nsha256 = "{"0" * 64}"\nscopes = ["admin"]\n', "scopes"),
        (
            f'[[tokens]]\nname = "a"\nsha256 = "{"0" * 64}"\n'
            f'[[tokens]]\nname = "a"\nsha256 = "{"1" * 64}"\n',
            "unique",
        ),
        (f'[[tokens]]\nname = "a"\nsha256 = "{"0" * 64}"\ntoken = "x"\n', "token"),
    ],
)
def test_invalid_token_files_fail(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(AuthConfigError, match=message):
        load_tokens(write(tmp_path, text))


def test_missing_token_file(tmp_path: Path) -> None:
    with pytest.raises(AuthConfigError, match="not found"):
        load_tokens(tmp_path / "nope.toml")


async def test_token_create_output_round_trips(tmp_path: Path) -> None:
    text = token_create("laptop", ["ha:actions"])
    token = text.split("\n")[1].strip()
    toml = text.split("Add to tokens.toml:\n\n")[1]
    entries = load_tokens(write(tmp_path, toml))
    verified = await StaticTokenVerifier(entries).verify_token(token)
    assert (verified.client_id, verified.scopes) == ("laptop", ["ha:actions", "ha:read"])


def test_cli_token_create(capsys) -> None:
    main(["token", "create", "--name", "ci"])
    out = capsys.readouterr().out
    assert "hai_" in out
    assert 'scopes = ["ha:read"]' in out
