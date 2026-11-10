import pytest

from indico_mcp.config import Config


def test_single_instance(monkeypatch):
    monkeypatch.setenv("INDICO_BASE_URL", "https://indico.example/")
    monkeypatch.setenv("INDICO_TOKEN", "tok")
    monkeypatch.setenv("INDICO_ROOM_LOCATIONS", " Main ,, Annex ")
    cfg = Config()
    assert cfg.instance_names == ["default"]
    assert cfg.default_name == "default"
    inst = cfg.get()
    assert inst.base_url == "https://indico.example"
    assert inst.token == "tok"
    assert inst.room_locations == ["Main", "Annex"]


def test_single_instance_token_is_optional(monkeypatch):
    monkeypatch.setenv("INDICO_BASE_URL", "https://indico.example")
    assert Config().get().token is None


def test_missing_configuration_is_an_error():
    with pytest.raises(ValueError, match="INDICO_BASE_URL"):
        Config()


def test_multi_instance(monkeypatch):
    monkeypatch.setenv("INDICO_INSTANCES", "cern, su")
    monkeypatch.setenv("INDICO_DEFAULT", "su")
    monkeypatch.setenv("INDICO_CERN_URL", "https://indico.cern.ch")
    monkeypatch.setenv("INDICO_SU_URL", "https://indico.fysik.su.se/")
    monkeypatch.setenv("INDICO_SU_TOKEN", "tok")
    cfg = Config()
    assert cfg.instance_names == ["cern", "su"]
    assert cfg.default_name == "su"
    assert cfg.get().name == "su"
    assert cfg.get("cern").token is None
    assert cfg.get("su").base_url == "https://indico.fysik.su.se"


def test_multi_instance_default_is_first_listed(monkeypatch):
    monkeypatch.setenv("INDICO_INSTANCES", "cern,su")
    monkeypatch.setenv("INDICO_CERN_URL", "https://indico.cern.ch")
    monkeypatch.setenv("INDICO_SU_URL", "https://indico.fysik.su.se")
    assert Config().default_name == "cern"


def test_multi_instance_missing_url(monkeypatch):
    monkeypatch.setenv("INDICO_INSTANCES", "cern")
    with pytest.raises(ValueError, match="INDICO_CERN_URL"):
        Config()


def test_multi_instance_unknown_default(monkeypatch):
    monkeypatch.setenv("INDICO_INSTANCES", "cern")
    monkeypatch.setenv("INDICO_CERN_URL", "https://indico.cern.ch")
    monkeypatch.setenv("INDICO_DEFAULT", "su")
    with pytest.raises(ValueError, match="INDICO_DEFAULT"):
        Config()


@pytest.mark.parametrize("name", ["bad name", "../x", "a.b"])
def test_multi_instance_rejects_unsafe_names(monkeypatch, name):
    monkeypatch.setenv("INDICO_INSTANCES", name)
    with pytest.raises(ValueError, match="invalid"):
        Config()


def test_unknown_instance_lists_available(monkeypatch):
    monkeypatch.setenv("INDICO_BASE_URL", "https://indico.example")
    with pytest.raises(ValueError, match="Available: default"):
        Config().get("cern")
