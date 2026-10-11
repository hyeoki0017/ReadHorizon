import json
import os
import stat
from pathlib import Path

import pytest

from exam_booker import vault as vault_mod
from exam_booker.config import ConfigError, parse_config
from exam_booker.vault import Credential, Vault, VaultError, mask

MASTER = "correct horse battery"
MOCK_LOGIN = (Path(__file__).resolve().parents[1] / "mock" / "mock_login.html").as_uri()


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(vault_mod, "KDF_N", 2**10)  # 테스트 속도용


def test_roundtrip_and_file_has_no_plaintext(tmp_path):
    p = tmp_path / "c.enc"
    v = Vault.create(p, MASTER)
    v.put("qnet", Credential("sample01", "S3cret!pw"))
    v.lock()
    raw = p.read_text(encoding="utf-8")
    assert "sample01" not in raw and "S3cret" not in raw and MASTER not in raw
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    v2 = Vault.open(p, MASTER)
    assert v2.get("qnet") == Credential("sample01", "S3cret!pw")
    assert v2.sites() == ["qnet"]


def test_wrong_master_and_short_master(tmp_path):
    p = tmp_path / "c.enc"
    Vault.create(p, MASTER)
    with pytest.raises(VaultError, match="틀렸"):
        Vault.open(p, "wrong password")
    with pytest.raises(VaultError, match="8자"):
        Vault.create(tmp_path / "d.enc", "short")


def test_tamper_and_entry_swap_detected(tmp_path):
    p = tmp_path / "c.enc"
    v = Vault.create(p, MASTER)
    v.put("qnet", Credential("a-user", "pw-a"))
    v.put("kca", Credential("b-user", "pw-b"))
    data = json.loads(p.read_text())
    data["entries"]["kca"] = data["entries"]["qnet"]  # 다른 사이트 항목으로 옮겨 붙이기
    p.write_text(json.dumps(data))
    v2 = Vault.open(p, MASTER)
    with pytest.raises(VaultError, match="복호화 실패"):
        v2.get("kca")
    assert v2.get("qnet").username == "a-user"


def test_lock_wipes_key(tmp_path):
    v = Vault.create(tmp_path / "c.enc", MASTER)
    key = v._key
    v.lock()
    assert not any(key)
    with pytest.raises(VaultError, match="잠겨"):
        v._open(v._data["check"], "__check__")


def test_credential_repr_is_masked():
    c = Credential("sample01", "S3cret!pw")
    assert "S3cret" not in repr(c) and "sample01" not in str(c)
    assert mask("sample01") == "sa****01"
    assert mask("abc") == "a**"


BASE = {
    "apply_url": "https://example.com/apply",
    "open_at": "2026-11-02 10:00:00",
    "preferences": [{"venue": "상암고"}],
    "selectors": {"apply_button": "#a", "slot_row": "tr", "slot_choose": "button"},
}


def test_site_preset_fills_login_and_user_overrides():
    cfg = parse_config({**BASE, "site": "qnet", "login": {"pw_field": "#myPw"}})
    assert cfg.login.credential == "qnet"
    assert cfg.login.pw_field == "#myPw"  # 사용자 값 우선
    assert "로그아웃" in cfg.login.logged_in_marker
    assert "#NetFunnel_Loading_Popup" in cfg.selectors.blockers
    assert cfg.selectors.apply_button == "#a"


def test_unknown_site_and_secrets_in_config_rejected():
    with pytest.raises(ConfigError, match="알 수 없는 site"):
        parse_config({**BASE, "site": "nope"})
    with pytest.raises(ConfigError, match="적지 마세요"):
        parse_config({**BASE, "site": "kca", "login": {"password": "1234"}})


def test_inspect_lists_fields_without_values(page_ctx):
    from exam_booker.inspect_page import describe

    _, page = page_ctx
    page.goto(MOCK_LOGIN)
    page.fill("input[name=userPw]", "secret-value")
    out = describe(page)
    assert 'input[name="userPw"]' in out and "로그인" in out
    assert "secret-value" not in out


def test_login_disabled():
    assert parse_config({**BASE, "site": "kisa", "login": {"enabled": False}}).login is None


# ---- 모의 로그인 화면으로 자동 로그인 확인 -------------------------------------
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page_ctx():
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium 실행 불가: {e}")
        ctx = browser.new_context()
        yield ctx, ctx.new_page()
        browser.close()


def _booker(page_ctx, cred):
    from exam_booker.booker import Booker

    ctx, page = page_ctx
    cfg = parse_config({**BASE, "site": "qnet", "login_url": MOCK_LOGIN})
    logs = []
    b = Booker(cfg, ctx, page, log=logs.append, ask=lambda _: "", credentials=lambda: cred)
    return b, page, logs


def test_auto_login_with_preset_selectors(page_ctx):
    b, page, logs = _booker(page_ctx, Credential("tester", "test-pw-1234"))
    assert b.auto_login()
    assert page.locator("#welcome").is_visible()
    assert page.input_value("#search") == ""  # 검색창에 잘못 입력하지 않음
    joined = "\n".join(logs)
    assert "test-pw-1234" not in joined and "tester" not in joined  # 로그에 평문 없음
    assert "te**er" in joined


def test_auto_login_failure_does_not_retry(page_ctx):
    b, page, logs = _booker(page_ctx, Credential("tester", "wrong-pw"))
    b.cfg = b.cfg.__class__(**{**b.cfg.__dict__, "login": b.cfg.login.__class__(**{**b.cfg.login.__dict__, "timeout_seconds": 1.5})})
    assert not b.auto_login()
    assert any("다시 시도하지 않습니다" in m for m in logs)
    assert page.inner_text("#error") == "아이디 또는 비밀번호가 틀렸습니다"


def test_vault_cli_add_list_remove(tmp_path, monkeypatch, capsys):
    from exam_booker import __main__ as cli

    secrets_in = iter([MASTER, MASTER, "pw-xyz-123"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _p="": next(secrets_in))
    monkeypatch.setattr("builtins.input", lambda _p="": "sample01")
    vp = str(tmp_path / "c.enc")
    assert cli.main(["vault", "add", "qnet", "--vault", vp]) == 0
    monkeypatch.setattr(cli.getpass, "getpass", lambda _p="": MASTER)
    assert cli.main(["vault", "list", "--vault", vp]) == 0
    assert cli.main(["vault", "remove", "qnet", "--vault", vp]) == 0
    out = capsys.readouterr().out
    assert "sa****01" in out and "sample01" not in out and "pw-xyz-123" not in out
    assert Vault.open(vp, MASTER).sites() == []


def test_cleanup_and_password_saving_disabled(tmp_path, monkeypatch):
    from exam_booker import __main__ as cli

    prof = tmp_path / "prof"
    cli._disable_password_saving(str(prof))
    prefs = json.loads((prof / "Default" / "Preferences").read_text())
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["password_manager_enabled"] is False
    vp = tmp_path / "c.enc"
    Vault.create(vp, MASTER)
    assert cli.main(["cleanup", "--profile-dir", str(prof), "--vault", str(vp), "--yes"]) == 0
    assert not prof.exists() and vp.exists()
    assert cli.main(["cleanup", "--profile-dir", str(prof), "--vault", str(vp), "--vault-too", "--yes"]) == 0
    assert not vp.exists()


def test_run_refuses_auto_login_with_pw_debug(tmp_path, monkeypatch):
    from exam_booker import __main__ as cli

    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(Path(__file__).resolve().parents[1].joinpath("config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("DEBUG", "pw:api")
    assert cli.main(["run", str(cfgp)]) == 2
