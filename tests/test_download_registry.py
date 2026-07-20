from pathlib import Path

from app.download_registry import DownloadLinkRegistry


def test_registry_persists_and_reuses_links_with_enough_time(tmp_path: Path):
    path = tmp_path / "download_links.json"
    registry = DownloadLinkRegistry(path)
    registry.remember(
        ukey="U1",
        dkey="D1",
        link="https://files.example/D1",
        expires_at=100_000,
    )

    reloaded = DownloadLinkRegistry(path)

    assert reloaded.active_for("U1", now=90_000).dkey == "D1"
    assert reloaded.active_for("U1", now=97_000) is None
    assert reloaded.hidden_dkeys() == {"D1"}


def test_registry_keeps_historical_dkeys_and_can_forget_a_source(tmp_path: Path):
    registry = DownloadLinkRegistry(tmp_path / "download_links.json")
    registry.remember("U1", "D1", "https://files.example/D1", expires_at=100)
    registry.remember("U1", "D2", "https://files.example/D2", expires_at=200)
    registry.remember("U2", "D3", "https://files.example/D3", expires_at=300)

    assert registry.hidden_dkeys() == {"D1", "D2", "D3"}

    registry.forget_ukey("U1")

    assert registry.hidden_dkeys() == {"D3"}
