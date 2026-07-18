# 라이선스 관리 모듈
# 관리자 인증, 사용 기간이 서명된 빌드용 라이선스 파일 발급, 그리고
# 실행 시점에 빌드에 포함된 라이선스 기간 검증을 담당한다.
#
# PC 잠금(디바이스 코드 매칭)은 사용하지 않는다 - 관리자가 배포 전마다
# 사용 기간을 정해 license_build.json을 생성하고, 그 파일이 PyInstaller
# datas로 exe 안에 포함되어 배포된다. 즉 접근 제어는 "이 빌드를 누구에게
# 주느냐"로 관리자가 직접 하고, 프로그램은 그 exe에 박힌 기간만 확인한다.
# 개발 모드(python main.py 직접 실행)는 관리자 본인 환경이므로 항상 통과한다.

import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import date, datetime

from config.settings import (
    LICENSE_ADMIN_PASSWORD,
    LICENSE_BUILD_FILE_NAME,
    LICENSE_SECRET_KEY,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "CHANGE_ME_"


class LicenseConfigurationError(Exception):
    """LICENSE_SECRET_KEY가 설정되지 않았거나 플레이스홀더일 때 발급/검증을 차단한다."""


def _is_configured_value(value: str) -> bool:
    """빈 값이나 "CHANGE_ME_"로 시작하는 예제용 플레이스홀더는 실제 설정으로 인정하지 않는다."""
    value = (value or "").strip()
    if not value:
        return False
    if value.startswith(_PLACEHOLDER_PREFIX):
        return False
    return True


def _resource_dir() -> str:
    """빌드 라이선스 파일을 읽고 쓸 기준 디렉터리를 반환한다.

    - 개발 모드: 프로젝트 루트 (license_build.json을 생성해 여기 둔다)
    - frozen(exe) 모드: PyInstaller가 datas를 풀어놓는 임시 번들 경로(sys._MEIPASS)
      (빌드 시 datas로 포함시킨 읽기 전용 리소스를 읽는 용도)
    """
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LicenseManager:
    """관리자 인증 및 빌드용 라이선스(사용 기간) 발급/검증을 담당하는 클래스"""

    def __init__(self):
        self._build_license_path = os.path.join(_resource_dir(), LICENSE_BUILD_FILE_NAME)

    # ===== 설정 검증 (fail-closed 판단의 기준) =====

    def is_admin_password_configured(self) -> bool:
        """LICENSE_ADMIN_PASSWORD가 실제 값으로 설정되어 있는지."""
        return _is_configured_value(LICENSE_ADMIN_PASSWORD)

    def is_license_secret_configured(self) -> bool:
        """LICENSE_SECRET_KEY가 실제 값으로 설정되어 있는지."""
        return _is_configured_value(LICENSE_SECRET_KEY)

    def is_fully_configured(self) -> bool:
        """관리자 인증과 라이선스 서명 모두에 필요한 값이 갖춰져 있는지.

        MainWindow가 라이선스 관리자 버튼 노출 여부를 판단할 때 재사용한다.
        """
        return self.is_admin_password_configured() and self.is_license_secret_configured()

    # ===== 서명 =====

    def _sign(self, message: str) -> str:
        """LICENSE_SECRET_KEY가 설정되지 않았으면 빈 키/예제 키로 서명하지 않고
        즉시 실패한다(원칙: 설정 누락 상태로 라이선스를 발급/검증하지 않는다)."""
        if not self.is_license_secret_configured():
            raise LicenseConfigurationError(
                "LICENSE_SECRET_KEY가 설정되지 않았습니다 — .env에 실제 값을 설정하세요."
            )
        return hmac.new(LICENSE_SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()

    def _license_message(self, start_date: str, end_date: str) -> str:
        return f"BUILD|{start_date}|{end_date}"

    # ===== 관리자 인증 =====

    def verify_admin_password(self, password: str) -> bool:
        """입력한 비밀번호가 관리자 비밀번호와 일치하는지 확인한다.

        설정이 누락되어 있으면(빈 값/플레이스홀더) 어떤 입력에도 항상 실패한다
        (원칙: 설정 누락 시 기본 비밀번호를 제공하지 않는다 — 즉 인증 성공으로
        간주하지 않는다). 비밀번호 값 자체는 로그에 남기지 않는다.
        """
        if not isinstance(password, str) or not password:
            return False
        if not self.is_admin_password_configured():
            logger.warning("LICENSE_ADMIN_PASSWORD가 설정되지 않아 관리자 인증을 거부합니다.")
            return False
        # hmac.compare_digest()는 두 인자가 모두 bytes이거나 모두 ASCII-only str이어야
        # 하며, 그렇지 않으면 TypeError를 던진다 — 한글 등 비ASCII 비밀번호를 설정하면
        # (버그였음: 이전 코드 그대로도 동일하게 터졌을 것) 이 비교에서 그대로 예외가
        # 나 인증 로직 전체가 죽는다. UTF-8로 인코딩해 두 값 모두 bytes로 맞춰 비교하면
        # 어떤 문자열이든 안전하게(타이밍 공격 내성 유지) 비교할 수 있다.
        return hmac.compare_digest(password.encode("utf-8"), LICENSE_ADMIN_PASSWORD.encode("utf-8"))

    # ===== 라이선스 발급 (관리자 측, 빌드 전 실행) =====

    def generate_build_license(self, start_date: str, end_date: str) -> dict:
        """지정된 기간으로 서명된 빌드용 라이선스 데이터를 생성한다.

        호출 전 관리자 비밀번호 확인은 호출자의 책임이다. LICENSE_SECRET_KEY가
        설정되지 않았으면 LicenseConfigurationError를 던진다(발급 자체를 막는다).
        """
        return {
            "start_date": start_date,
            "end_date": end_date,
            "issued_at": datetime.now().isoformat(timespec="seconds"),
            "signature": self._sign(self._license_message(start_date, end_date)),
        }

    def save_build_license(self, license_data: dict) -> str:
        """빌드용 라이선스 데이터를 프로젝트 루트의 고정된 경로에 저장한다.

        이 파일은 이후 pyinstaller 빌드 시 datas로 exe에 포함되어야 한다.

        Returns:
            저장된 파일의 절대 경로
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        filepath = os.path.join(project_root, LICENSE_BUILD_FILE_NAME)
        self._atomic_write(license_data, filepath)
        logger.info(f"빌드용 라이선스 파일 저장 완료: {filepath}")
        return filepath

    # ===== 라이선스 검증 (실행 시점) =====

    def validate_license_dict(self, license_data: dict) -> tuple[bool, str]:
        """라이선스 데이터(기간 + 서명)의 유효성을 검사한다.

        Returns:
            (유효 여부, 실패 사유 — 유효하면 빈 문자열)
        """
        try:
            start_date_str = license_data["start_date"]
            end_date_str = license_data["end_date"]
            signature = license_data["signature"]
        except KeyError:
            return False, "라이선스 파일 형식이 올바르지 않습니다."

        try:
            expected_signature = self._sign(self._license_message(start_date_str, end_date_str))
        except LicenseConfigurationError:
            logger.error("LicenseConfigurationError: LICENSE_SECRET_KEY 설정 누락으로 라이선스 검증을 차단합니다.")
            return False, "라이선스 관리자 설정이 완료되지 않았습니다. 관리자에게 문의하세요."

        if not hmac.compare_digest(signature, expected_signature):
            return False, "라이선스 파일이 손상되었거나 위변조되었습니다."

        try:
            start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            return False, "라이선스 날짜 형식이 올바르지 않습니다."

        today = date.today()
        if today < start:
            return False, f"라이선스 시작일({start_date_str})이 아직 되지 않았습니다."
        if today > end:
            return False, f"라이선스가 만료되었습니다 (만료일: {end_date_str})."

        return True, ""

    def check_build_license(self) -> tuple[bool, str]:
        """실행 시점에 이 빌드에 포함된 라이선스 기간을 검증한다.

        개발 모드(python main.py 직접 실행)는 관리자 본인 환경이므로 항상 통과시킨다.
        frozen(exe) 모드에서만 실제로 번들된 license_build.json을 검사한다.
        """
        if not getattr(sys, "frozen", False):
            return True, ""

        if not os.path.exists(self._build_license_path):
            return False, "라이선스 파일이 없습니다. 관리자에게 문의하세요."

        try:
            with open(self._build_license_path, "r", encoding="utf-8") as f:
                license_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"빌드 라이선스 파일 읽기 오류: {e}")
            return False, "라이선스 파일이 손상되었습니다."

        return self.validate_license_dict(license_data)

    # ===== 내부 헬퍼 =====

    def _atomic_write(self, data: dict, filepath: str) -> None:
        """임시 파일에 먼저 쓴 후 교체하는 방식으로 저장 중 오류로 인한
        기존 파일 손상을 방지한다 (storage/data_manager.py의 패턴과 동일)."""
        tmp_path = filepath + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, filepath)
        except Exception as e:
            logger.error(f"저장 오류: {filepath} — {e}")
            raise
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
