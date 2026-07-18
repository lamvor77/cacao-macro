# 클라우드(Supabase) 메시지 동기화 서비스
#
# 동기화 정책 (PC 우선 단방향 push 아님):
#   - Supabase의 메시지를 "클라우드 기준 최신 데이터"로 취급한다.
#   - PC 로컬 JSON(storage/*.json, storage.data_manager.DataManager)은
#     오프라인 캐시이자 장애 대비 백업으로 계속 유지된다 — 이 서비스는
#     DataManager를 대체하지 않고, 그 위에 추가되는 별도 동기화 계층이다.
#   - 메시지마다 updated_at / updated_by / version을 두고, version 기반
#     낙관적 잠금으로 충돌을 감지한다. 충돌 시 자동으로 어느 한쪽을
#     덮어쓰지 않고 conflicts 목록으로 보고한다 — 실제 UI 알림/해결은
#     Phase 2B에서 이 서비스를 호출하는 쪽(MainWindow)의 몫이다.
#
# 이 모듈은 UI 컴포넌트를 import하거나 참조하지 않는다. 모든 입출력은
# dict/dataclass이며, 네트워크 오류는 예외로 전파하지 않고 SyncResult로
# 반환한다 — 실패해도 로컬 저장 결과에는 영향을 주지 않는다.

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config.cloud_settings import CloudConfig, get_cloud_config
from services.supabase_client import SupabaseClientManager

logger = logging.getLogger(__name__)

try:
    # postgrest-py의 APIError는 Postgres 오류 코드(.code, 예: RLS 위반 시 "42501")를
    # 담고 있다 — HTTP 401/403(권한 문제)과 순수 네트워크 오류를 구분하는 데 쓴다
    # (Phase 2D). supabase 패키지가 없을 때도 이 모듈을 import할 수 있어야 하므로
    # 다른 SDK import와 동일하게 가드한다.
    from postgrest import APIError as PostgrestAPIError
except ImportError:
    PostgrestAPIError = None  # type: ignore[assignment, misc]


def _is_permission_denied(exc: Exception) -> bool:
    """RLS 위반(Postgres 42501)으로 인한 실패인지 판별한다."""
    if PostgrestAPIError is None or not isinstance(exc, PostgrestAPIError):
        return False
    return getattr(exc, "code", None) == "42501"


# ===== 데이터 모델 =====

@dataclass
class MessageRecord:
    """클라우드에 저장된 메시지 1건 (메시지 번호 1~12 중 하나)"""

    number: int
    text: str
    updated_at: str
    updated_by: str
    version: int


@dataclass
class SyncResult:
    """pull/push/check_connection 공통 결과 객체.

    success=False라도 예외가 아니라 이 객체로 반환되므로 호출자(GUI,
    스케줄러)는 항상 안전하게 결과를 확인하고 로컬 동작을 이어갈 수 있다.
    """

    success: bool
    error: Optional[str] = None
    error_code: Optional[str] = None
    messages: Optional[dict] = None  # pull 결과: {번호: MessageRecord}
    updated: list = field(default_factory=list)     # push 성공한 메시지 번호 목록
    conflicts: list = field(default_factory=list)    # push 중 충돌난 메시지 번호 목록


@dataclass
class SyncStatus:
    """현재 동기화 상태 스냅샷 (네트워크 요청 없이 조회 가능)"""

    enabled: bool
    connected: Optional[bool]
    last_sync_at: Optional[str]
    last_error: Optional[str]
    device_id: str


def _cache_dir() -> str:
    """동기화 충돌 감지용 로컬 캐시 파일을 둘 디렉터리를 반환한다.

    storage/ 바로 아래가 아니라 storage/cloud_sync/ 하위에 둔다.
    DataManager.list_saved_files()나 "저장된 카톡방 목록 선택" 파일 열기
    대화상자는 storage/ 최상위만 조회하므로, 이 내부 캐시 파일이 사용자
    저장 목록 사이에 섞여 보이지 않도록 분리했다.
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "storage", "cloud_sync")
    os.makedirs(path, exist_ok=True)
    return path


class CloudSyncService:
    """메시지 클라우드 동기화 서비스.

    최소 메서드: is_enabled / check_connection / pull_messages /
    push_messages / get_sync_status.

    이 클래스는 스레드에서 호출되는 것을 전제로 설계되었다 — 네트워크
    요청 중 UI나 자동발송 스레드를 막지 않으려면 호출하는 쪽(Phase 2B의
    MainWindow/AutoScheduler 연동 코드)이 별도 스레드에서 호출해야 한다.
    이 서비스 자체는 스레드를 생성하지 않는다 (호출부가 이미 스레드
    관리 패턴 — MainWindow._run_in_thread — 을 갖고 있기 때문).
    """

    def __init__(
        self,
        config: Optional[CloudConfig] = None,
        client_manager: Optional[SupabaseClientManager] = None,
        cache_dir: Optional[str] = None,
    ):
        self._config = config or get_cloud_config()
        # client_manager 주입은 AuthService와 "같은" Supabase Client(및 그 안의
        # 인증 세션)를 공유하기 위한 것이다 (버그 수정, 아래 push_messages 주석 참고).
        # 주입하지 않으면 이 서비스가 자체적으로 별도 Client를 만드는데, 그 Client는
        # 로그인 세션이 전혀 적용되지 않은 anon 전용 상태로 남는다 — RLS의
        # auth.uid()가 항상 NULL이 되어 messages INSERT/UPDATE가 전부 42501로
        # 거부된다. 운영 코드(CloudSyncCoordinator)는 항상 공유 매니저를 넘긴다.
        self._client_mgr = client_manager or SupabaseClientManager(self._config)

        # cache_dir은 테스트에서 실제 프로젝트의 storage/cloud_sync/를 건드리지
        # 않도록 임시 디렉터리를 주입하기 위한 것이다(CloudSyncCoordinator의
        # state_dir과 동일한 목적) — 운영 코드는 항상 생략한다. 이 파라미터가
        # 없어서 테스트가 실제 storage/cloud_sync/message_cache.json을 덮어쓴
        # 사고가 있었다(수정 이력).
        cache_dir = cache_dir or _cache_dir()
        self._cache_path = os.path.join(cache_dir, "message_cache.json")
        self._cache: dict[int, MessageRecord] = self._load_cache()

        self._last_connected: Optional[bool] = None
        self._last_sync_at: Optional[str] = None
        self._last_error: Optional[str] = None

    @property
    def client_manager(self) -> SupabaseClientManager:
        """AuthService 등 다른 서비스와 Supabase Client를 공유하기 위해 노출한다."""
        return self._client_mgr

    # ===== 공개 메서드 =====

    def is_enabled(self) -> bool:
        return self._config.enabled

    def check_connection(self) -> SyncResult:
        """네트워크 요청으로 Supabase 연결 가능 여부를 확인한다."""
        if not self.is_enabled():
            self._last_connected = False
            return SyncResult(False, error="클라우드 동기화가 비활성화되어 있습니다.", error_code="disabled")

        result = self._client_mgr.check_connection()
        self._last_connected = result.success
        self._last_error = result.error
        return SyncResult(result.success, error=result.error, error_code=result.error_code)

    def pull_messages(self) -> SyncResult:
        """Supabase에서 메시지 1~12의 최신 상태를 가져온다.

        성공 시 내부 캐시(버전 정보)를 갱신한다 — 이후 push_messages() 호출
        시 이 캐시를 기준으로 충돌 여부를 판단한다.
        """
        guard = self._guard_enabled_and_connected()
        if guard is not None:
            return guard

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            return SyncResult(False, error=client_result.error, error_code=client_result.error_code)

        try:
            response = (
                client_result.client.table(self._config.messages_table)
                .select("message_number,text,updated_at,updated_by,version")
                .execute()
            )
        except Exception as e:
            logger.error(f"메시지 다운로드 오류: {e}")
            self._last_error = str(e)
            error_code = "permission_denied" if _is_permission_denied(e) else "connection_error"
            return SyncResult(False, error=str(e), error_code=error_code)

        records: dict[int, MessageRecord] = {}
        for row in response.data or []:
            try:
                number = int(row["message_number"])
            except (KeyError, TypeError, ValueError):
                logger.warning(f"메시지 행 형식 오류 — 건너뜀: {row!r}")
                continue

            record = MessageRecord(
                number=number,
                text=row.get("text", "") or "",
                updated_at=row.get("updated_at", "") or "",
                updated_by=row.get("updated_by", "") or "",
                version=int(row.get("version") or 1),
            )
            records[number] = record
            self._cache[number] = record

        self._save_cache()
        self._last_sync_at = _now_iso()
        self._last_error = None
        self._last_connected = True
        logger.info(f"클라우드 메시지 다운로드 완료 — {len(records)}건")
        return SyncResult(True, messages=records)

    def push_messages(
        self,
        messages: dict,
        updated_by: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> SyncResult:
        """로컬 메시지를 Supabase에 업로드한다.

        Args:
            messages: {메시지번호(1~12): 텍스트}
            updated_by: 이 변경을 수행한 인증 사용자의 UUID(auth.uid()와 일치해야
                함). 필수나 다름없다 — 비어 있으면 DB 호출 자체를 하지 않고
                즉시 실패를 반환한다(버그 수정: 과거에는 여기서 device_id 문자열로
                조용히 대체해, RLS의 updated_by=auth.uid() 검증에 항상 실패하는
                값을 보내고 있었다).
            device_id: 어느 PC에서 보냈는지 남기는 진단용 값(messages.device_id
                컬럼). 생략 시 CloudConfig.device_id 사용. updated_by와 다른
                컬럼이며 계정 식별자가 아니다.

        version 기반 낙관적 잠금을 사용한다 — 마지막 pull 시점 이후 클라우드
        쪽 데이터가 이미 바뀌었다면(예: 모바일에서 먼저 수정) 해당 메시지는
        업로드하지 않고 conflicts에 번호를 담아 반환한다. 호출부(Phase 2B)는
        이 목록을 사용자에게 알리고 재동기화를 유도해야 한다 — 이 서비스가
        임의로 어느 한쪽을 덮어쓰지 않는다.
        """
        guard = self._guard_enabled_and_connected()
        if guard is not None:
            return guard

        updated_by = (updated_by or "").strip()
        if not updated_by:
            msg = "updated_by(인증 사용자 UUID)가 없어 업로드를 거부합니다 — DB에 요청을 보내지 않았습니다."
            logger.error(msg)
            return SyncResult(False, error=msg, error_code="invalid_request")

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            return SyncResult(False, error=client_result.error, error_code=client_result.error_code)

        client = client_result.client
        device = (device_id or self._config.device_id or "").strip() or None
        now_iso = _now_iso()
        logger.info(f"클라우드 업로드 준비 — updated_by={updated_by[:8]}..., device_id={device}")

        updated: list[int] = []
        conflicts: list[int] = []
        errors: list[str] = []
        permission_denied = False

        for number, text in messages.items():
            known = self._cache.get(number)
            try:
                if known is None:
                    self._insert_new(client, number, text, now_iso, updated_by, device, updated, conflicts)
                else:
                    self._update_existing(client, number, text, now_iso, updated_by, device, known, updated, conflicts)
            except Exception as e:
                logger.error(f"메시지{number} 업로드 오류: {e}")
                errors.append(f"메시지{number}: {e}")
                if _is_permission_denied(e):
                    permission_denied = True

        self._save_cache()
        self._last_sync_at = _now_iso()

        if errors:
            self._last_error = "; ".join(errors)
            error_code = "permission_denied" if permission_denied else "connection_error"
            return SyncResult(False, error=self._last_error, error_code=error_code,
                               updated=updated, conflicts=conflicts)

        self._last_error = None
        if conflicts:
            logger.warning(f"메시지 업로드 충돌 발생 — 번호: {conflicts}")
        return SyncResult(True, updated=updated, conflicts=conflicts)

    def get_sync_status(self) -> SyncStatus:
        """네트워크 요청 없이 마지막으로 알려진 동기화 상태를 반환한다."""
        return SyncStatus(
            enabled=self.is_enabled(),
            connected=self._last_connected,
            last_sync_at=self._last_sync_at,
            last_error=self._last_error,
            device_id=self._config.device_id,
        )

    # ===== 내부 구현 =====

    def _guard_enabled_and_connected(self) -> Optional[SyncResult]:
        """비활성화 상태면 즉시 실패 결과를 반환한다 (네트워크 시도 자체를 막음)."""
        if not self.is_enabled():
            return SyncResult(False, error="클라우드 동기화가 비활성화되어 있습니다.", error_code="disabled")
        return None

    def _insert_new(self, client, number, text, now_iso, updated_by, device_id, updated, conflicts) -> None:
        """캐시에 없는(=한 번도 pull하지 않은) 메시지 번호를 최초 삽입 시도한다.

        클라우드에 이미 존재하는 행이면 고유키 충돌 오류가 발생하는데, 이는
        "로컬이 모르는 최신 클라우드 데이터가 있다"는 뜻이므로 덮어쓰지 않고
        충돌로 처리한다.

        payload는 messages_insert_editor 정책의 WITH CHECK
        (fn_can_edit() and updated_by = auth.uid())를 만족해야 한다 —
        updated_by는 반드시 이 요청을 보내는 인증 사용자의 UUID와 정확히
        같아야 한다(디바이스 문자열이 아님). source='pc'로 PC 데스크톱 앱에서
        온 변경임을 식별한다(스키마의 message_source enum: mobile/pc/restore).
        """
        try:
            response = (
                client.table(self._config.messages_table)
                .insert({
                    "message_number": number,
                    "text": text,
                    "updated_at": now_iso,
                    "updated_by": updated_by,
                    "version": 1,
                    "source": "pc",
                    "device_id": device_id,
                })
                .execute()
            )
        except Exception as e:
            if _looks_like_duplicate_key_error(e):
                logger.warning(f"메시지{number} 업로드 충돌 — 클라우드에 이미 존재(미동기화 상태)")
                conflicts.append(number)
                return
            raise

        if response.data:
            self._cache[number] = MessageRecord(number, text, now_iso, updated_by, 1)
            updated.append(number)
        else:
            conflicts.append(number)

    def _update_existing(self, client, number, text, now_iso, updated_by, device_id, known: MessageRecord, updated, conflicts) -> None:
        """캐시에 있는(=이전에 pull한 적 있는) 메시지 번호를 버전 확인 후 갱신한다."""
        expected_version = known.version
        response = (
            client.table(self._config.messages_table)
            .update({
                "text": text,
                "updated_at": now_iso,
                "updated_by": updated_by,
                "version": expected_version + 1,
                "source": "pc",
                "device_id": device_id,
            })
            .eq("message_number", number)
            .eq("version", expected_version)
            .execute()
        )

        if response.data:
            self._cache[number] = MessageRecord(number, text, now_iso, updated_by, expected_version + 1)
            updated.append(number)
        else:
            logger.warning(
                f"메시지{number} 업로드 충돌 — 클라우드 버전이 로컬 예상(version={expected_version})과 다릅니다."
            )
            conflicts.append(number)

    def _load_cache(self) -> dict:
        if not os.path.exists(self._cache_path):
            return {}
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {
                int(k): MessageRecord(
                    number=int(k),
                    text=v.get("text", ""),
                    updated_at=v.get("updated_at", ""),
                    updated_by=v.get("updated_by", ""),
                    version=int(v.get("version", 1)),
                )
                for k, v in raw.items()
            }
        except (json.JSONDecodeError, OSError, ValueError, AttributeError) as e:
            logger.warning(f"클라우드 동기화 캐시 읽기 오류 — 빈 캐시로 시작합니다: {e}")
            return {}

    def _save_cache(self) -> None:
        data = {
            str(num): {
                "text": rec.text,
                "updated_at": rec.updated_at,
                "updated_by": rec.updated_by,
                "version": rec.version,
            }
            for num, rec in self._cache.items()
        }
        tmp_path = self._cache_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._cache_path)
        except OSError as e:
            logger.warning(f"클라우드 동기화 캐시 저장 오류(무시하고 계속 진행): {e}")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _looks_like_duplicate_key_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "duplicate" in msg or "23505" in msg or "already exists" in msg
