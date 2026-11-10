"""Cloud speech requires a provider; puppet mode never auto-enables."""

import pytest

from app.config import Settings
from app.core.service import AgentService
from app.services.transport.mqtt import MqttService


@pytest.mark.parametrize("key", ["", "test-key"])
async def test_startup_enables_configured_speech_and_disables_puppet(key, tmp_path, monkeypatch):
    monkeypatch.setattr(MqttService, "connect", lambda self: None)
    service = AgentService(Settings(
        _env_file=None,
        OPENAI_API_KEY=key,
        CONTROLLER_LOCK_FILE=str(tmp_path / "controller.lock"),
        MOXIE_FACE_FILE=str(tmp_path / "face.json"),
    ))
    with service.scope.activate():
        # Startup must establish the desired mode even with injected state.
        await service.state.set_puppet_mode(True)
        await service.state.set_cloud_tts(not bool(key))
    try:
        await service.start()
        assert await service.state.get_cloud_tts() is bool(key)
        assert await service.state.get_puppet_mode() is False
    finally:
        await service.stop()
