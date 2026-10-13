"""로그인 정보 암호화 보관함.

- 사이트별 아이디·비밀번호를 내 PC의 파일 하나(credentials.enc)에 암호화해 둔다.
- 마스터 비밀번호 → scrypt(키 유도) → AES-256-GCM(암호화+위변조 검출).
- 사이트 이름을 AAD로 묶어, 암호문을 다른 사이트 항목으로 옮겨 붙이면 복호화가 실패한다.
- 마스터 비밀번호와 평문은 파일·로그에 남기지 않는다. 화면 표시는 mask()로 가린다.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = "exam_booker-vault-v1"
# scrypt 비용(OWASP 권장값 N=2^17, r=8, p=1): 일반 PC에서 0.3~0.5초, 메모리 128MB. 무차별 대입을 어렵게 한다.
KDF_N, KDF_R, KDF_P = 2**17, 8, 1
MIN_MASTER_LEN = 8


class VaultError(Exception):
    pass


@dataclass(frozen=True)
class Credential:
    username: str
    password: str

    def __repr__(self) -> str:  # 실수로 print/로그에 찍혀도 노출되지 않게
        return f"Credential(username={mask(self.username)!r}, password='****')"

    __str__ = __repr__


def mask(value: str) -> str:
    """'sample01' → 'sa****01' (짧으면 앞 1글자만)."""
    if len(value) <= 4:
        return value[:1] + "*" * max(len(value) - 1, 0)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _derive(master: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=KDF_N, r=KDF_R, p=KDF_P).derive(master.encode("utf-8"))


class Vault:
    """잠금 해제된 보관함. 키(bytearray)만 메모리에 두고, 항목은 필요할 때 복호화한다."""

    def __init__(self, path: Path, data: dict, key: bytearray):
        self.path = path
        self._data = data
        self._key = key

    # ---- 생성·열기 -------------------------------------------------------
    @classmethod
    def create(cls, path: str | Path, master: str) -> Vault:
        p = Path(path)
        if p.exists():
            raise VaultError(f"보관함이 이미 있습니다: {p}")
        if len(master) < MIN_MASTER_LEN:
            raise VaultError(f"마스터 비밀번호는 {MIN_MASTER_LEN}자 이상이어야 합니다.")
        salt = secrets.token_bytes(16)
        key = bytearray(_derive(master, salt))
        data = {
            "format": FORMAT,
            "kdf": {"name": "scrypt", "salt": _b64(salt), "n": KDF_N, "r": KDF_R, "p": KDF_P},
            # 마스터 비밀번호 확인용 (빈 평문을 암호화한 값)
            "check": cls._seal(key, b"", "__check__"),
            "entries": {},
        }
        v = cls(p, data, key)
        v._save()
        return v

    @classmethod
    def open(cls, path: str | Path, master: str) -> Vault:
        p = Path(path)
        if not p.exists():
            raise VaultError(f"보관함이 없습니다: {p} (먼저 'vault add'로 로그인 정보를 저장하세요)")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("format") != FORMAT:
                raise VaultError("보관함 형식이 올바르지 않습니다.")
            kdf = data["kdf"]
            key = bytearray(
                Scrypt(salt=_unb64(kdf["salt"]), length=32, n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(
                    master.encode("utf-8")
                )
            )
        except (ValueError, KeyError, TypeError) as e:
            raise VaultError("보관함 파일이 손상됐습니다.") from e
        v = cls(p, data, key)
        try:
            v._open(data["check"], "__check__")
        except VaultError:
            v.lock()
            raise VaultError("마스터 비밀번호가 틀렸습니다.") from None
        return v

    # ---- 암복호화 --------------------------------------------------------
    @staticmethod
    def _seal(key: bytearray, plaintext: bytes, site: str) -> dict:
        nonce = secrets.token_bytes(12)
        ct = AESGCM(bytes(key)).encrypt(nonce, plaintext, site.encode("utf-8"))
        return {"nonce": _b64(nonce), "ct": _b64(ct)}

    def _open(self, sealed: dict, site: str) -> bytes:
        if not self._key:
            raise VaultError("보관함이 잠겨 있습니다.")
        try:
            return AESGCM(bytes(self._key)).decrypt(_unb64(sealed["nonce"]), _unb64(sealed["ct"]), site.encode("utf-8"))
        except (InvalidTag, KeyError, ValueError) as e:
            raise VaultError("복호화 실패 — 비밀번호가 틀렸거나 파일이 변조됐습니다.") from e

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # 본인만 읽고 쓸 수 있게(0600) 만든 뒤 원자적으로 교체
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---- 항목 관리 -------------------------------------------------------
    def put(self, site: str, cred: Credential) -> None:
        if not cred.username or not cred.password:
            raise VaultError("아이디와 비밀번호를 모두 입력하세요.")
        payload = json.dumps({"u": cred.username, "p": cred.password}).encode("utf-8")
        self._data["entries"][site] = self._seal(self._key, payload, site)
        self._save()

    def get(self, site: str) -> Credential:
        sealed = self._data["entries"].get(site)
        if sealed is None:
            raise VaultError(f"'{site}' 로그인 정보가 없습니다. (저장된 사이트: {', '.join(self.sites()) or '없음'})")
        obj = json.loads(self._open(sealed, site))
        return Credential(obj["u"], obj["p"])

    def remove(self, site: str) -> bool:
        if self._data["entries"].pop(site, None) is None:
            return False
        self._save()
        return True

    def sites(self) -> list[str]:
        return sorted(self._data["entries"])

    def lock(self) -> None:
        """메모리의 키를 0으로 덮어쓴다."""
        for i in range(len(self._key)):
            self._key[i] = 0
        self._key = bytearray()
