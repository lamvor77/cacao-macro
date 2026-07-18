# Supabase 클라이언트 초기화 및 연결 상태 관리
# 이 모듈은 오직 "연결"만 책임진다 — 실제 메시지 pull/push 로직은
# services/cloud_sync_service.py 에 있다.
#
# 설계 원칙:
#   - 어떤 경우에도 예외를 호출자에게 그대로 던지지 않는다 (ClientResult로 반환).
#   - supabase 패키지가 설치되어 있지 않아도 import 시점에 죽지 않는다.
#   - 클라우드 동기화가 비활성화되어 있으면 네트워크 요청을 전혀 시도하지 않는다.
#   - 모든 요청에 타임아웃을 둔다 (기본 CloudConfig.timeout_seconds).

import logging
from dataclasses import dataclass
from typing import Any, Optional

from config.cloud_settings import CloudConfig, get_cloud_config

logger = logging.getLogger(__name__)

try:
    from supabase import Client, create_client
    # NOTE: supabase.lib.client_options.ClientOptions는 storage(세션 저장소) 필드가
    # 없는 베이스 클래스라 동기(Sync) Client 생성 시 내부에서 AttributeError가 난다.
    # 동기 클라이언트에는 반드시 SyncClientOptions를 사용해야 한다.
    from supabase.lib.client_options import SyncClientOptions as ClientOptions

    _SDK_AVAILABLE = True
except ImportError:
    Client = Any  # type: ignore[assignment, misc]
    create_client = None  # type: ignore[assignment]
    ClientOptions = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False


@dataclass
class ClientResult:
    """클라이언트 획득/연결 확인 결과 (예외 대신 반환하는 구조화된 결과 객체)"""

    success: bool
    client: Optional["Client"] = None
    error: Optional[str] = None
    # 호출자가 실패 원인을 구분해서 처리할 수 있도록 하는 분류 코드.
    # disabled / missing_config / sdk_unavailable / init_error / connection_error
    error_code: Optional[str] = None


class SupabaseClientManager:
    """Supabase 클라이언트를 생성하고 연결 가능 여부를 확인하는 클래스.

    실패해도(설정 없음/패키지 없음/네트워크 오류) 예외를 던지지 않고
    ClientResult를 반환하므로, 이 클래스를 사용하는 상위 코드는 앱 실행이나
    기존 로컬 기능에 영향을 주지 않고 실패를 처리할 수 있다.
    """

    def __init__(self, config: Optional[CloudConfig] = None):
        self._config = config or get_cloud_config()
        self._client: Optional["Client"] = None

    @property
    def config(self) -> CloudConfig:
        return self._config

    def get_client(self) -> ClientResult:
        """캐시된 클라이언트를 반환하거나 필요 시 새로 생성한다.

        네트워크 요청은 하지 않는다 (supabase-py의 create_client는 지연 연결
        방식). 실제 연결 가능 여부 확인은 check_connection()에서 수행한다.
        """
        if not self._config.enabled:
            return ClientResult(False, error="클라우드 동기화가 비활성화되어 있습니다.", error_code="disabled")

        if not _SDK_AVAILABLE:
            msg = "supabase 패키지가 설치되어 있지 않습니다 (requirements.txt 참고)."
            logger.warning(msg)
            return ClientResult(False, error=msg, error_code="sdk_unavailable")

        if not self._config.url or not self._config.anon_key:
            msg = "SUPABASE_URL 또는 SUPABASE_ANON_KEY가 설정되지 않았습니다."
            logger.warning(msg)
            return ClientResult(False, error=msg, error_code="missing_config")

        if self._client is not None:
            return ClientResult(True, client=self._client)

        try:
            options = self._build_client_options()
            self._client = create_client(self._config.url, self._config.anon_key, options=options)
            logger.info("Supabase 클라이언트 초기화 완료")
            return ClientResult(True, client=self._client)
        except Exception as e:
            logger.error(f"Supabase 클라이언트 초기화 실패: {e}")
            return ClientResult(False, error=str(e), error_code="init_error")

    def check_connection(self) -> ClientResult:
        """실제 네트워크 요청을 보내 Supabase에 연결 가능한지 확인한다.

        가벼운 조회(messages 테이블 1건 제한)를 사용하며, 클라이언트 생성
        시 지정한 타임아웃 내에 응답이 없거나 오류가 발생하면 실패로 처리한다.
        """
        client_result = self.get_client()
        if not client_result.success:
            return client_result

        try:
            client_result.client.table(self._config.messages_table).select("message_number").limit(1).execute()
            return ClientResult(True, client=client_result.client)
        except Exception as e:
            logger.warning(f"Supabase 연결 확인 실패: {e}")
            return ClientResult(False, error=str(e), error_code="connection_error")

    def _build_client_options(self) -> Optional["ClientOptions"]:
        """타임아웃이 적용된 ClientOptions를 만든다.

        supabase-py 버전에 따라 ClientOptions 필드가 달라질 수 있으므로,
        구성에 실패하면 None을 반환해 라이브러리 기본 타임아웃으로 폴백한다
        (연결 자체가 막히는 것보다는 낫다).
        """
        try:
            return ClientOptions(
                postgrest_client_timeout=self._config.timeout_seconds,
                storage_client_timeout=self._config.timeout_seconds,
            )
        except Exception as e:
            logger.debug(f"ClientOptions 구성 실패 — 기본 옵션 사용: {e}")
            return None
