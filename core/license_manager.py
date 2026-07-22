# 라이선스 관리 모듈
# 관리자 인증, 그리고 관리자가 발급하는 빌드용 라이선스 파일(license_build.json)
# 생성/서명 검증을 담당한다.
#
# 이 파일이 검증하는 것은 "이 exe가 관리자가 정식으로 발급한 빌드인지"뿐이다
# (license_build.json 존재 여부 + JSON 형식 + 서명 무결성). 프로그램의 실제
# 사용 권한(누가 쓸 수 있는가)은 Supabase 계정 인증/승인 상태로 관리한다
# (services/auth_service.py, app_users.status) — 즉 실행 순서는
#   1) verify_build_signature() — 이 빌드가 변조되지 않았는지
#   2) Supabase 로그인 — 이 사람이 누구인지
#   3) app_users.status == approved — 이 사람이 승인됐는지
# 날짜 기반 사용기간(시작일/만료일) 판단은 어디에도 없다 — 서명이 유효하면
# license_build.json에 어떤 날짜가 들어있든(과거/미래/부재) 통과시킨다.
# 개발 모드(python main.py 직접 실행)는 관리자 본인 환경이므로 항상 통과한다.

import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime

from config.settings import (
    LICENSE_ADMIN_PASSWORD,
    LICENSE_BUILD_FILE_NAME,
    LICENSE_SECRET_KEY,
    get_runtime_base_dir,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "CHANGE_ME_"


class LicenseConfigurationError(Exception):
    """LICENSE_SECRET_KEY가 설정되지 않았거나 플레이스홀더일 때 발급/검증을 차단한다."""


# ===== 오류 메시지(원칙 9) — UI가 사유별로 구분해 보여줄 수 있도록 상수로 고정한다.
# 실패 이유는 5가지로만 분류한다: 파일 없음 / 형식 오류 / 서명 불일치 / 만료 /
# 검증 설정 누락. 경로 전체·비밀키·signature·내부 스택트레이스는 절대 포함하지 않는다.
MSG_LICENSE_FILE_MISSING = "라이선스 파일이 없습니다."
MSG_LICENSE_FORMAT_INVALID = "라이선스 파일 형식이 올바르지 않습니다."
MSG_LICENSE_SIGNATURE_INVALID = "라이선스 서명이 올바르지 않습니다."
MSG_LICENSE_CONFIG_MISSING = "라이선스 검증 설정이 누락되었습니다."


def _is_configured_value(value: str) -> bool:
    """빈 값이나 "CHANGE_ME_"로 시작하는 예제용 플레이스홀더는 실제 설정으로 인정하지 않는다."""
    value = (value or "").strip()
    if not value:
        return False
    if value.startswith(_PLACEHOLDER_PREFIX):
        return False
    return True


def get_external_license_path() -> str:
    """라이선스 파일의 유일한 조회 경로 — 항상 exe(또는 개발 스크립트)와 같은
    폴더의 license_build.json이다.

    frozen(PyInstaller onefile) 모드에서 sys._MEIPASS(부트로더가 실행할 때마다
    새로 압축 해제하는 임시 폴더 — exe 파일 위치와 무관하고, 프로세스 종료 시
    사라진다)에 있는 내부 번들 파일로는 절대 폴백하지 않는다. 폴백을 허용하면
    사용자가 exe 옆 파일을 교체해도 조용히 무시되고, 빌드 시점에 박제된
    오래된(또는 존재하지 않는) 내부 사본이 계속 쓰이는 문제가 생긴다 — 바로
    이 문제 때문에 과거 라이선스를 재빌드 없이 교체할 수 없었다. get_runtime_base_dir()는
    frozen 모드에서 sys.executable의 부모 디렉터리를 반환하므로(config/settings.py
    참고), 이 함수는 항상 "exe가 실제로 있는 폴더"를 가리키며 현재 작업
    디렉터리(CWD)에 의존하지 않는다.
    """
    return os.path.join(get_runtime_base_dir(), LICENSE_BUILD_FILE_NAME)


class LicenseManager:
    """관리자 인증 및 빌드용 라이선스(license_build.json) 발급/서명 검증을
    담당하는 클래스 — 날짜 기반 사용기간은 다루지 않는다."""

    def __init__(self):
        self._build_license_path = get_external_license_path()

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
        """라이선스 데이터의 형식과 서명 유효성을 검사한다.

        사용기간(start_date/end_date) 비교는 하지 않는다 — 그 값들은 서명
        메시지를 구성하는 문자열로만 쓰이고(과거 형식과의 서명 호환성을
        위해 유지), 유효/만료 판단에는 전혀 쓰이지 않는다. 프로그램 실행
        권한은 Supabase 계정 인증/승인 상태로 관리한다(services/auth_service.py).
        이 함수의 결과는 verify_build_signature()를 통해 main.py 시작을
        막는 데(변조/미발급 빌드 차단) 쓰이고, services/diagnostics_service.py는
        진단 화면 표시에 재사용한다.

        Returns:
            (유효 여부, 실패 사유 — 유효하면 빈 문자열)
        """
        try:
            start_date_str = license_data["start_date"]
            end_date_str = license_data["end_date"]
            signature = license_data["signature"]
        except KeyError:
            return False, MSG_LICENSE_FORMAT_INVALID

        try:
            expected_signature = self._sign(self._license_message(start_date_str, end_date_str))
        except LicenseConfigurationError:
            logger.error("LicenseConfigurationError: LICENSE_SECRET_KEY 설정 누락으로 라이선스 검증을 차단합니다.")
            return False, MSG_LICENSE_CONFIG_MISSING

        if not hmac.compare_digest(signature, expected_signature):
            return False, MSG_LICENSE_SIGNATURE_INVALID

        return True, ""

    def verify_build_signature(self) -> tuple[bool, str]:
        """exe 옆(개발 모드는 프로젝트 루트) license_build.json이 관리자가
        발급한 빌드가 맞는지(존재/형식/서명 무결성) 검증한다.

        main.py 시작 경로에서 호출되며, 실패하면 실행을 중단한다(변조되거나
        관리자가 발급하지 않은 빌드는 실행시키지 않는다). 날짜(시작일/만료일)는
        검사하지 않는다 — 검사 대상이 아니다. services/diagnostics_service.py도
        진단 화면 표시를 위해 이 함수를 재사용한다.

        개발 모드(python main.py 직접 실행)는 관리자 본인 환경이므로 항상 통과시킨다.
        frozen(exe) 모드에서만 실제로 외부 license_build.json을 검사한다 — exe
        내부에 번들된 사본을 참조하지 않는다(get_external_license_path() 참고).
        """
        if not getattr(sys, "frozen", False):
            return True, ""

        if not os.path.exists(self._build_license_path):
            logger.error(f"라이선스 파일을 찾을 수 없습니다: {os.path.basename(self._build_license_path)}")
            return False, MSG_LICENSE_FILE_MISSING

        try:
            with open(self._build_license_path, "r", encoding="utf-8") as f:
                license_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"빌드 라이선스 파일 읽기 오류: {type(e).__name__}")
            return False, MSG_LICENSE_FORMAT_INVALID

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
