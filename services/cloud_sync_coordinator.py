# 클라우드 동기화와 MainWindow/DataManager를 연결하는 조정자(coordinator)
#
# 이 모듈이 하는 일:
#   - 프로그램 시작 시: 로컬 우선 로드(동기, 즉시) → 로그인되어 있으면 백그라운드로
#     초기 동기화 시작. 프로그램 시작 자체를 절대 막지 않는다.
#   - 로컬 저장 직후: 로컬 JSON은 이미 저장된 뒤, 로그인되어 있으면 백그라운드로
#     클라우드 업로드를 시도한다. 업로드가 실패해도 로컬 저장은 취소되지 않는다.
#   - 주기적 폴링(기본 CloudConfig.sync_interval_seconds, 30초): 클라우드 변경을
#     감지해 ControlPanel과 로컬 JSON에 반영한다.
#   - 상태(CloudState)를 콜백으로 통지한다 — 콜백은 백그라운드 스레드에서
#     호출되므로, 호출부(MainWindow)가 self.after(0, ...)로 메인 스레드에
#     넘겨야 한다 (기존 _on_log 패턴과 동일).
#
# 이 모듈이 절대 하지 않는 일 (기존 구조 보존 원칙):
#   - core/scheduler.py, core/message_sender.py, core/kakao_controller.py를
#     import하거나 직접 다루지 않는다 — scheduler에는 주입받은
#     notify_scheduler_fn(보통 AutoScheduler.notify_cloud_update)만 호출한다.
#   - ControlPanel 위젯을 직접 참조하지 않는다 — get_messages_fn/apply_messages_fn
#     콜백을 통해서만 메시지에 접근한다 (ControlPanel이 메시지의 유일한 UI
#     소유자라는 원칙 유지).
#   - storage/data_manager.py의 메서드 시그니처를 바꾸지 않는다 — save_messages()/
#     load()를 있는 그대로 호출한다.
#   - services/cloud_sync_service.py, services/supabase_client.py의 공개
#     인터페이스를 바꾸지 않는다.

import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from config.cloud_settings import get_cloud_config, get_message_sync_config
from core.legacy_send_verification import MessageClassification, classify_message
from services.auth_service import AppUserProfile, AuthService
from services.cloud_state import CloudState, CloudStatusInfo
from services.cloud_sync_service import CloudSyncService, MessageRecord, SyncResult
from storage.data_manager import DataManager

logger = logging.getLogger(__name__)

# 상시 30초 pull polling 제거 이후, 이 값은 순수 내부 타이머 tick 간격일 뿐이다
# (15분 dirty push 스케줄 확인 + stop() 반응성 목적) — 네트워크를 트리거하지
# 않으므로 더 이상 .env로 설정할 이유가 없다(요구사항 5절).
_DEFAULT_TICK_INTERVAL_SECONDS = 30.0


@dataclass
class _LocalMessageState:
    """메시지 1건의 로컬 동기화 상태 (초기 동기화 정책 판단에 사용)."""

    version: int = 0        # 이 로컬 사본이 마지막으로 확인한 클라우드 버전 (0=한 번도 동기화 안 됨)
    updated_at: str = ""    # 로컬에서 마지막으로 수정된 시각
    dirty: bool = False     # version 확인 이후 로컬에서 다시 수정되었는지
    last_text: str = ""     # dirty 판단 기준이 되는 마지막으로 알려진 "로컬" 텍스트(자동저장 시점마다 갱신)
    # ===== Phase 2E 후속: LOCAL_PENDING vs 실제 CONFLICT 구분용 =====
    # last_synced_text는 last_text와 다른 개념이다 — "마지막으로 클라우드와
    # 실제로 일치한다고 확인된 텍스트"이며, 다음 3가지 경우에만 갱신된다:
    # 정상 pull 적용 완료 / 정상 push 완료 / 로컬·원격이 동일함을 확인. 로컬
    # 자동저장/입력변경/push 시작 전/conflict 발생 시에는 갱신하지 않는다.
    # 기존에 저장된 local_sync_state.json(이 필드가 없는 이전 버전 파일)을
    # 읽을 때는 dataclass 기본값(""/False)이 적용되어 하위 호환된다.
    last_synced_text: str = ""
    conflict: bool = False  # 실제(진짜) 충돌 상태 — 15분 자동 업로드에서 제외하는 데 사용


def _local_state_dir() -> str:
    """coordinator 전용 로컬 상태 저장 위치.

    storage/ 최상위가 아니라 storage/cloud_sync/ 하위 — Phase 2A의
    CloudSyncService 캐시(message_cache.json)와 동일한 관례로, 사용자가 수동
    저장한 storage/*.json 목록과 섞이지 않게 한다.
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "storage", "cloud_sync")
    os.makedirs(path, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(data: dict, filepath: str) -> None:
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
    except OSError as e:
        logger.warning(f"로컬 동기화 상태 저장 오류(무시하고 계속 진행): {e}")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


class CloudSyncCoordinator:
    """MainWindow/DataManager와 CloudSyncService/AuthService를 연결한다."""

    def __init__(
        self,
        get_messages_fn: Callable[[], dict],
        apply_messages_fn: Callable[[dict], None],
        log_fn: Callable[[str], None],
        status_fn: Callable[[CloudStatusInfo], None],
        notify_scheduler_fn: Callable[[], None],
        data_manager: Optional[DataManager] = None,
        cloud_service: Optional[CloudSyncService] = None,
        auth_service: Optional[AuthService] = None,
        poll_interval_seconds: Optional[float] = None,
        state_dir: Optional[str] = None,
        dirty_push_interval_seconds: Optional[float] = None,
    ):
        self._get_messages = get_messages_fn
        self._apply_messages = apply_messages_fn
        self._log = log_fn
        self._status_fn = status_fn
        self._notify_scheduler = notify_scheduler_fn

        self._data = data_manager or DataManager()

        # state_dir은 테스트에서 실제 프로젝트의 storage/cloud_sync/를 건드리지
        # 않도록 임시 디렉터리를 주입하기 위한 것이다 — 운영 코드(MainWindow)는
        # 항상 생략하며, 이 경우 기존과 동일한 storage/cloud_sync/를 사용한다.
        # self._cloud를 기본 생성하기 "전에" 정해야 한다 — 아래에서 그 CloudSyncService의
        # 캐시 디렉터리로도 같은 값을 넘겨, state_dir을 주입한 테스트가 실수로
        # 실제 storage/cloud_sync/message_cache.json을 건드리는 일을 막는다
        # (과거에 정확히 이 문제로 실제 캐시 파일을 덮어쓴 사고가 있었다).
        state_dir = state_dir or _local_state_dir()

        # 순서 중요: self._auth를 먼저 만든 뒤, self._cloud를 기본 생성할 때 그
        # auth_service의 client_manager를 공유시킨다. 공유하지 않으면 로그인
        # 세션이 CloudSyncService 쪽 Supabase Client에는 전혀 반영되지 않아
        # messages INSERT/UPDATE가 전부 RLS(42501)로 거부된다(실제로 있었던 버그).
        self._auth = auth_service or AuthService()
        self._cloud = cloud_service or CloudSyncService(
            client_manager=self._auth.client_manager, cache_dir=state_dir
        )

        config = get_cloud_config()
        # 상시 30초 pull polling을 제거하면서 이 값은 더 이상 "얼마나 자주
        # 서버를 조회할지"가 아니다 — 아래 15분 dirty push 타이머와 stop()
        # 반응성을 위한 내부 tick 간격일 뿐이며, 이 tick 자체는 네트워크를
        # 전혀 호출하지 않는다(요구사항 3/6절). 그래서 CloudConfig가 아니라
        # 이 모듈의 내부 상수를 기본값으로 쓴다 — 더 이상 사용자가 .env로
        # 설정할 값이 아니기 때문이다.
        self._poll_interval = poll_interval_seconds or _DEFAULT_TICK_INTERVAL_SECONDS
        self._device_id = config.device_id
        self._local_file = os.path.join(state_dir, "messages.json")
        self._local_state_file = os.path.join(state_dir, "local_sync_state.json")
        self._local_state: dict[int, _LocalMessageState] = self._load_local_state()
        # 요구사항 4절 — 메시지별 편집 중 여부(shared_messages의 SharedMessageCoordinator와
        # 동일한 목적, legacy 전용 별도 추적). REMOTE_APPLY 상황에서 이 메시지를
        # 사용자가 지금 편집 중이면 조용히 덮어쓰지 않고 발송 자체를 중단한다
        # (core/legacy_send_verification.py의 EDIT_IN_PROGRESS).
        self._editing_numbers: set = set()

        # Phase 2E: 로컬 자동 저장 + 클라우드 조건부(15분) 동기화 설정.
        msg_config = get_message_sync_config()
        self._dirty_push_interval = dirty_push_interval_seconds or msg_config.cloud_sync_interval_seconds

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 폴링(pull)이 동시에 겹치지 않도록 하는 락 (재진입 방지, non-blocking 시도)
        self._sync_busy = threading.Lock()
        # 클라우드 값을 UI/로컬에 반영하는 동안 True — 저장 콜백이 재귀적으로
        # 다시 클라우드에 업로드를 시도하는 루프를 막기 위한 억제 플래그.
        self._applying_remote = False
        # 프로그램 종료 처리 중 True — 새 자동저장/업로드 예약을 더 이상 하지 않는다.
        self._closing = False

        # 디바운스 저장(2초)이 아직 끝나지 않아 "화면 입력이 마지막 로컬 저장본과
        # 다를 수 있는" 구간인지 여부. True인 동안에는 폴링이 클라우드 값을
        # 조용히 UI에 덮어쓰지 않는다(원칙 8) — MainWindow가 디바운스 타이머를
        # 걸고 풀 때 set_input_in_progress()로 갱신해준다.
        self._input_in_progress = False

        # ===== Phase 2E: 업로드 직렬화(원칙 7) =====
        # request_push()로 들어오는 모든 업로드 요청은 이 락으로 직렬화된다.
        # 이미 업로드 중이면 새 스레드를 만들지 않고 "가장 최근 요청의 이유"만
        # 남겨두었다가, 현재 업로드가 끝나면 그 시점의 최신 메시지로 한 번 더
        # 업로드한다(스냅샷을 큐에 쌓지 않고 완료 시점에 다시 읽는 방식 — 그
        # 사이 또 바뀌었어도 항상 최신값이 최종적으로 반영된다).
        self._push_lock = threading.Lock()
        self._push_in_progress = False
        self._pending_push_reason: Optional[str] = None

        # 마지막으로 로컬 파일에 실제로 쓴 메시지 내용 — 동일 내용 반복 자동저장
        # 시 디스크 쓰기를 건너뛰기 위한 캐시(디바운스가 끝났지만 값이 그대로인 경우).
        self._last_autosaved_messages: dict = {}

        self._last_status = CloudStatusInfo(CloudState.NOT_CONFIGURED)

    # ===== 공개 API =====

    def load_local_and_apply(self) -> dict:
        """프로그램 시작 시 가장 먼저 호출한다.

        로컬 캐시 파일을 동기적으로 읽어 즉시 반환한다(네트워크 없음, 빠름).
        호출부(MainWindow)가 반환값을 ControlPanel에 반영해야 한다 — 여기서는
        UI를 직접 건드리지 않는다.
        """
        data = self._data.load(self._local_file)
        messages = (data or {}).get("messages", {}) or {}
        self._log(f"[INFO] 클라우드 동기화 — 로컬 캐시 로드 완료 (메시지 {len(messages)}개)")
        return messages

    def start(self) -> None:
        """초기 동기화 + 폴링 루프를 백그라운드 스레드에서 시작한다. 즉시 반환한다."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self, wait_seconds: float = 2.0) -> None:
        """폴링을 멈추고 백그라운드 스레드 종료를 짧게 기다린다.

        진행 중인 네트워크 요청이 있어도 wait_seconds 이상 프로그램 종료를
        지연시키지 않는다 (데몬 스레드이므로 프로세스 종료 시 어차피 정리된다).
        """
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=wait_seconds)
        self._log("[INFO] 클라우드 동기화 중지")

    def notify_local_save(self, messages: dict) -> None:
        """ControlPanel의 메시지가 로컬 JSON에 저장된 직후 호출한다 (저장 버튼 핸들러에서).

        1) 클라우드 반영을 로컬에 적용하는 도중이면(=_applying_remote) 아무 것도
           하지 않는다 — 클라우드 적용 때문에 다시 클라우드로 업로드하는 루프 방지.
        2) coordinator 전용 로컬 캐시 파일도 함께 갱신한다.
        3) dirty로 표시하고, 즉시 업로드를 요청한다(reason="manual_save") — 실제
           네트워크 시도 여부는 request_push() 내부 게이트(로그인/권한 등)가 결정한다.
        """
        if self._applying_remote:
            return

        self._data.save_messages(messages, self._local_file)
        self._last_autosaved_messages = dict(messages)
        self._mark_dirty(messages)
        self.request_push(messages=dict(messages), reason="manual_save")

    def notify_local_autosave(self, messages: dict) -> bool:
        """입력창 디바운스(기본 2초)가 끝난 뒤 호출한다 — 로컬 파일에만 저장하고
        Supabase 업로드는 하지 않는다(15분 조건부 동기화/명시적 이벤트 전용, 원칙 4/5).

        직전에 자동 저장한 내용과 동일하면 디스크에 쓰지 않는다. 저장 성공 시 True.
        """
        if self._applying_remote or self._closing:
            return False
        if messages == self._last_autosaved_messages:
            return False

        try:
            self._data.save_messages(messages, self._local_file)
        except Exception as e:
            logger.error(f"로컬 자동 저장 오류: {e}")
            self._log(f"[오류] 로컬 자동 저장 실패: {e}")
            return False

        self._last_autosaved_messages = dict(messages)
        self._mark_dirty(messages)
        self._log("[INFO] 로컬 자동 저장 완료")
        return True

    def set_input_in_progress(self, in_progress: bool) -> None:
        """MainWindow가 디바운스 타이머를 걸거나(True) 풀 때(False) 호출한다.

        True인 동안 폴링은 클라우드 값을 UI에 조용히 덮어쓰지 않는다 — 사용자가
        아직 로컬에 저장되지 않은 입력을 하고 있을 수 있기 때문이다(원칙 8).
        """
        self._input_in_progress = in_progress

    def has_cloud_dirty(self) -> bool:
        """업로드가 필요한(마지막으로 확인된 클라우드 버전과 다른) 메시지가
        하나라도 있는지 — DB 요청 없이 로컬 상태만으로 판단한다. 진짜 충돌
        상태인 메시지도 "아직 클라우드와 다르다"는 의미에서는 포함한다
        (자동 업로드 대상인지는 has_pushable_dirty()로 별도 판단한다)."""
        return any(s.dirty for s in self._local_state.values())

    def has_pushable_dirty(self) -> bool:
        """자동(15분 tick/종료) 업로드 대상이 하나라도 있는지 — dirty이면서
        진짜 충돌 상태는 아닌 메시지만 해당한다(원칙: 충돌 메시지는 자동
        업로드하지 않되, 충돌 없는 다른 dirty 메시지는 정상 업로드한다)."""
        return bool(self._pushable_dirty_numbers())

    def _pushable_dirty_numbers(self) -> list:
        return [n for n, s in self._local_state.items() if s.dirty and not s.conflict]

    def request_push(self, messages: Optional[dict] = None, reason: str = "interval") -> None:
        """비동기 업로드를 요청한다 (원칙 6/7).

        이미 업로드가 진행 중이면 새 스레드를 만들지 않고 "완료 후 최신 데이터로
        한 번 더 업로드해야 한다"는 표시만 남긴다 — 실제 업로드 시점의 최신
        메시지를 다시 읽으므로, 요청 시점과 실행 시점 사이의 변경도 항상 반영된다.

        이 메서드는 저장 버튼/발송 시작처럼 MainWindow 메인 스레드에서 직접
        호출될 수 있다 — is_logged_in()은 세션이 만료돼 있으면 내부적으로 갱신
        네트워크 요청을 할 수 있으므로, 그 판단(과 클라우드 활성화 여부)도
        스레드 생성 여부와 함께 전부 백그라운드 스레드 안에서 수행한다(UI
        스레드에서 네트워크 요청을 절대 하지 않는다는 원칙). 여기서 메인
        스레드 위에 남는 작업은 락 확인/설정과 스레드 생성뿐이다.
        """
        if self._closing:
            return

        with self._push_lock:
            if self._push_in_progress:
                self._pending_push_reason = reason
                self._log(f"[INFO] 업로드 진행 중 — 최신 변경 후속 업로드 예약 (reason={reason})")
                return
            self._push_in_progress = True

        payload = dict(messages) if messages is not None else None
        threading.Thread(target=self._push_entry, args=(payload, reason), daemon=True).start()

    def _push_entry(self, payload: Optional[dict], reason: str) -> None:
        """request_push()가 만든 스레드의 진입점 — 로그인/활성화 확인(네트워크 가능)과
        메시지 스냅샷 확보를 여기서(백그라운드 스레드) 수행한 뒤 _push_worker로 넘긴다."""
        if not (self._cloud.is_enabled() and self._auth.is_logged_in()):
            with self._push_lock:
                self._push_in_progress = False
                self._pending_push_reason = None
            return
        if payload is None:
            payload = dict(self._get_messages())
        self._push_worker(payload, reason)

    def shutdown_flush(self, timeout_seconds: float) -> None:
        """종료 직전 호출한다 — cloud_dirty이면 제한된 시간 안에서만 업로드를
        시도하고, 시간을 넘기면 기다리지 않고 반환한다(원칙: UI 스레드가 영구
        대기하면 안 됨). 새 자동저장/업로드 예약은 이후 막는다."""
        self._closing = True
        pushable = self._pushable_dirty_numbers()
        if not pushable:
            return
        if not (self._cloud.is_enabled() and self._auth.is_logged_in()):
            return

        # request_push()가 self._closing 체크로 막히므로, 여기서는 직접 push 워커를
        # 돌린다(이번 한 번은 예외적으로 종료 흐름 전용). 이미 다른 업로드가 진행
        # 중이면(예: 방금 저장 버튼으로 트리거된 업로드) 새로 시작하지 않고 "완료 후
        # 최신 데이터로 한 번 더"만 예약한다 — 그래야 그 사이의 최신 편집도 반영된다.
        # 진짜 충돌 상태인 메시지는 15분 tick과 동일하게 자동 업로드에서 제외한다.
        with self._push_lock:
            if not self._push_in_progress:
                self._push_in_progress = True
                current_messages = self._get_messages()
                payload = {n: current_messages.get(n, "") for n in pushable}
                threading.Thread(target=self._push_worker, args=(payload, "exit"), daemon=True).start()
            else:
                self._pending_push_reason = "exit"

        deadline = time.monotonic() + timeout_seconds
        while self._push_in_progress and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._push_in_progress:
            self._log("[경고] 종료 전 클라우드 업로드 대기시간 초과")

    def get_status(self) -> CloudStatusInfo:
        return self._last_status

    def on_login_success(self) -> None:
        """로그인 성공 직후(MainWindow) 호출한다 — 즉시 초기 동기화를 백그라운드로 재개한다."""
        if not self._cloud.is_enabled():
            self._set_status(CloudState.NOT_CONFIGURED)
            return
        threading.Thread(target=self._initial_sync, daemon=True).start()

    def on_logout(self) -> None:
        """로그아웃 직후(MainWindow) 호출한다 — 상태를 즉시 로그인 필요로 전환한다.

        백그라운드 타이머 스레드 자체는 멈추지 않는다 — 15분 dirty tick이나
        수동 새로고침/발송 직전 검증이 is_logged_in() 검사에서 자동으로
        건너뛰어지므로 실질적으로 멈춘 것과 같다(요구사항 6절 — 로그아웃
        상태에서는 어떤 경로로도 네트워크 요청이 0회여야 함). stop()은 프로그램
        종료 전용으로 남겨둔다(재로그인 시 스레드를 다시 만들 필요가 없어진다).
        """
        self._set_status(CloudState.LOGIN_REQUIRED)
        self._log("[INFO] 로그아웃 — 클라우드 동기화를 중지합니다 (로그인 필요 상태)")

    # ===== 내부: 백그라운드 루프 =====
    #
    # 상시 30초 pull polling을 제거했다(요구사항) — 이 루프는 더 이상 주기적으로
    # 서버를 조회하지 않는다. 유일하게 남은 주기 작업은 15분 dirty push
    # 검사뿐이며, 그것도 dirty가 없으면 네트워크를 전혀 호출하지 않는다
    # (_tick_dirty_cloud_sync, 요구사항 7/6). 로그인 직후 1회(_initial_sync),
    # 수동 새로고침(request_manual_refresh), 저장 재시도(기존 push CAS),
    # 발송 직전(core/legacy_send_verification.py)만 실제로 서버를 조회한다
    # (요구사항 2 — 충돌 재평가 트리거를 이 4곳으로 제한).

    def _run_loop(self) -> None:
        self._initial_sync()
        next_dirty_check = time.monotonic() + self._dirty_push_interval
        while self._running:
            if self._stop_event.wait(self._poll_interval):
                break  # stop()이 호출됨
            if time.monotonic() >= next_dirty_check:
                self._tick_dirty_cloud_sync()
                next_dirty_check = time.monotonic() + self._dirty_push_interval

    def _tick_dirty_cloud_sync(self) -> None:
        """15분(설정값)마다 호출된다 — cloud_dirty인 경우에만 업로드를 요청한다
        (원칙 5: 변경사항이 없으면 DB 요청 자체를 하지 않는다).

        진짜 CONFLICT 상태인 메시지는 자동 업로드 대상에서 제외한다(사용자
        해결이 필요한 상태를 자동으로 덮어쓰지 않기 위함) — 충돌 없는 다른
        dirty 메시지는 메시지별로 분리해 정상 업로드한다.
        """
        if not self._running or self._closing:
            return
        pushable = self._pushable_dirty_numbers()
        if not pushable:
            self._log("[INFO] 15분 동기화 검사 — 변경 없음, 요청 생략")
            return
        if not (self._cloud.is_enabled() and self._auth.is_logged_in()):
            return
        current_messages = self._get_messages()
        payload = {n: current_messages.get(n, "") for n in pushable}
        self.request_push(messages=payload, reason="interval")

    def _initial_sync(self) -> None:
        if not self._cloud.is_enabled():
            self._set_status(CloudState.NOT_CONFIGURED)
            return
        if not self._auth.is_logged_in():
            self._set_status(CloudState.LOGIN_REQUIRED)
            self._log("[INFO] 클라우드 동기화 — 로그인 필요 (로컬 모드로 계속 동작합니다)")
            return

        self._log("[INFO] 클라우드 초기 동기화 시작")
        self._set_status(CloudState.CONNECTING)
        profile = self._resolve_profile_or_halt()
        if profile is None:
            return
        self._sync_once(is_initial=True, can_write=profile.can_write, current_user_id=profile.id)

    def request_manual_refresh(self) -> None:
        """수동 새로고침 버튼에서 호출한다(legacy 쪽, 요구사항 2/4절) — 전체
        12건을 1회 pull해 정합성을 다시 확인한다. 이전 새로고침/초기 동기화가
        아직 끝나지 않았으면 건너뛴다(기존 30초 폴링과 동일한 busy 가드 재사용).
        즉시 반환한다 — 실제 조회는 백그라운드 스레드에서 수행한다.
        """
        threading.Thread(target=self._do_manual_refresh, daemon=True).start()

    def _do_manual_refresh(self) -> None:
        if not (self._cloud.is_enabled() and self._auth.is_logged_in()):
            self._log("[INFO] 레거시 메시지 수동 새로고침 건너뜀 — 클라우드 미설정 또는 로그인 필요")
            return
        if not self._sync_busy.acquire(blocking=False):
            self._log("[INFO] 레거시 메시지 수동 새로고침 건너뜀 — 이전 동기화가 아직 진행 중")
            return
        try:
            self._log("[INFO] 레거시 메시지 수동 새로고침 시작")
            profile = self._resolve_profile_or_halt()
            if profile is not None:
                # allow_reconcile_push=False: 새로고침은 "조회"가 목적이다 — 로컬
                # 미저장 변경을 함께 업로드하지 않는다(업로드는 저장 버튼/발송
                # 시작·종료/15분 조건부 tick 등 명시적 이벤트 전용, 요구사항 5절).
                self._sync_once(
                    is_initial=False, can_write=profile.can_write,
                    current_user_id=profile.id, allow_reconcile_push=False,
                )
            self._log("[INFO] 레거시 메시지 수동 새로고침 완료")
        finally:
            self._sync_busy.release()

    def push_single_message_sync(self, message_no: int, content: str, timeout_seconds: float = 10.0) -> bool:
        """발송 직전 검증 전용(core/legacy_send_verification.py의 LOCAL_PENDING
        케이스) — 동기적으로 즉시 push하고 성공 여부만 반환한다. 이미 스케줄러
        자신의 백그라운드 스레드에서 호출되므로 블로킹해도 안전하다.

        request_push()의 비동기 직렬화 큐와는 별개 경로이지만, 동시에 두
        업로드가 로컬 상태를 동시에 건드리지 않도록 같은 _push_lock/
        _push_in_progress로 상호 배제한다 — 이미 다른 업로드가 진행 중이면
        새로 큐잉하지 않고 바로 실패를 반환한다(발송 직전이라는 특성상 대기가
        길어지면 안 되므로, 실패 시 스케줄러가 그 메시지 하나만 이번 발송에서
        제외한다).
        """
        if not (self._cloud.is_enabled() and self._auth.is_logged_in()):
            return False

        acquired = self._push_lock.acquire(timeout=timeout_seconds)
        if not acquired:
            self._log(f"[경고] 발송 직전 push 대기 시간 초과 — message_no={message_no}")
            return False
        try:
            if self._push_in_progress:
                self._log(f"[경고] 다른 업로드가 진행 중이라 발송 직전 push를 건너뜁니다 — message_no={message_no}")
                return False
            self._push_in_progress = True
        finally:
            self._push_lock.release()

        try:
            profile = self._resolve_profile_or_halt()
            if profile is None or not profile.can_write:
                return False
            result = self._cloud.push_messages({message_no: content}, updated_by=profile.id, device_id=self._device_id)
            if not result.success or message_no not in result.updated:
                return False
            self._mark_synced_after_push(result, {message_no: content})
            return True
        finally:
            with self._push_lock:
                self._push_in_progress = False
                self._pending_push_reason = None

    def get_local_message_state(self, message_no: int) -> tuple:
        """발송 직전 검증(core/legacy_send_verification.py)이 필요로 하는
        (dirty, version, last_synced_text)만 읽기 전용으로 노출한다."""
        state = self._local_state.get(message_no)
        if state is None:
            return False, 0, ""
        return state.dirty, state.version, state.last_synced_text

    # --- 편집 중 상태(메시지별, 요구사항 4절) ---

    def begin_edit(self, message_no: int) -> None:
        self._editing_numbers.add(message_no)

    def end_edit(self, message_no: int) -> None:
        self._editing_numbers.discard(message_no)

    def is_editing(self, message_no: int) -> bool:
        return message_no in self._editing_numbers

    # --- 발송 직전 검증 지원(core/legacy_send_verification.py) ---

    def pull_message(self, message_no: int) -> Optional[MessageRecord]:
        """CloudSyncService.pull_message()에 대한 얇은 패스스루(단건 조회) —
        gui/main_window.py가 CloudSyncService 내부를 직접 참조하지 않도록
        coordinator를 통해서만 접근하게 한다(기존 설계 원칙과 동일)."""
        return self._cloud.pull_message(message_no)

    def record_remote_applied(self, message_no: int, version: int, text: str) -> None:
        """발송 직전 검증이 REMOTE_APPLY로 판정해 화면에 원격 내용을 반영한
        뒤 호출한다 — 로컬 동기화 상태를 그 반영과 일치시켜, 다음 조회에서
        같은 메시지가 다시 REMOTE_APPLY로 잘못 판정되지 않게 한다."""
        self._local_state[message_no] = _LocalMessageState(
            version=version, updated_at=_now_iso(), dirty=False,
            last_text=text, last_synced_text=text, conflict=False,
        )
        self._save_local_state()

    def _resolve_profile_or_halt(self) -> Optional[AppUserProfile]:
        """app_users 프로필을 조회해 pending/blocked/조회실패를 걸러낸다.

        None을 반환하면 호출부는 이번 동기화 시도를 여기서 멈춰야 한다(상태는
        이미 설정되어 있다). pending/blocked인 사용자는 messages를 pull/push
        시도조차 하지 않는다(원칙 13/14) — RLS로도 막히지만, 애초에 시도 자체를
        하지 않아야 한다는 요구사항이므로 여기서 먼저 걸러낸다.
        """
        profile = self._auth.get_app_user_profile()
        if profile is None:
            # "auth user 없음" 또는 "app_users 조회 실패" 둘 다 여기로 온다 —
            # 진단 로그(비밀정보 없음): 이 시점엔 UUID조차 확보하지 못한 상태다.
            self._log("[오류] 인증 사용자를 확인할 수 없습니다 (auth user 없음 또는 app_users 조회 실패).")
            self._set_status(CloudState.SYNC_FAILED, detail="app_users 프로필을 확인할 수 없습니다.")
            return None

        # 진단 로그 — UUID는 앞 8자리만 남기고, access/refresh token은 절대 남기지 않는다.
        self._log(
            f"[INFO] 인증 사용자 확인 — UUID {profile.id[:8]}..., "
            f"status={profile.status}, role={profile.role}"
        )

        if profile.is_pending:
            self._set_status(CloudState.APPROVAL_PENDING)
            self._log("[INFO] 관리자 승인 대기 중 — 메시지 동기화를 시도하지 않습니다.")
            return None
        if profile.is_blocked:
            self._set_status(CloudState.BLOCKED)
            self._log("[경고] 접근이 차단된 계정입니다 — 메시지 동기화를 시도하지 않습니다.")
            return None
        return profile

    # ===== 내부: 동기화 본체 =====

    def _sync_once(
        self, is_initial: bool, can_write: bool, current_user_id: str,
        allow_reconcile_push: bool = True,
    ) -> None:
        self._set_status(CloudState.SYNCING)

        pull_result = self._cloud.pull_messages()
        if not pull_result.success:
            self._handle_sync_failure(pull_result)
            return

        cloud_records: dict[int, MessageRecord] = pull_result.messages or {}
        self._log(f"[INFO] 클라우드 다운로드 완료 — {len(cloud_records)}건")

        local_messages = self._get_messages()
        to_apply, to_push, conflicts, pending, identical = self._reconcile(local_messages, cloud_records)

        if to_apply and self._input_in_progress:
            # 사용자가 아직 로컬 자동 저장 디바운스 중(=화면 입력이 마지막 로컬
            # 저장본과 다를 수 있음) — 클라우드 값을 조용히 덮어쓰지 않고 다음
            # 폴링으로 미룬다(원칙 8). 로컬 상태를 바꾸지 않으므로 다음 틱에
            # 동일한 판단을 다시 시도한다(자연 재시도, 별도 타이머 불필요).
            self._log(f"[INFO] 로컬 입력 진행 중 — 클라우드 변경 적용을 보류합니다 (메시지 {sorted(to_apply.keys())}번)")
            to_apply = {}

        if to_apply:
            self._apply_to_ui_and_local(to_apply, cloud_records)

        if identical:
            self._mark_identical(identical, local_messages, cloud_records)

        # 진짜 충돌/대기 상태를 영속화한다 — 15분 자동 업로드(_tick_dirty_cloud_sync)가
        # 네트워크 요청 없이 "이 메시지는 자동 업로드에서 제외해야 하는지"를 판단할 때 쓴다.
        self._update_conflict_flags(conflicts=conflicts, pending=pending)

        uploaded_now: list = []
        if not allow_reconcile_push:
            # 폴링(30초)에서는 여기서 업로드를 실행하지 않는다 — dirty 상태는
            # 그대로 유지되어 15분 조건부 tick 또는 명시적 이벤트가 처리한다.
            pass
        elif to_push and not can_write:
            # approved viewer — 업로드를 시도하지 않는다(원칙 15). RLS로도 막히지만
            # 불필요한 요청/오류 로그를 만들지 않도록 먼저 걸러낸다.
            self._log(f"[INFO] 읽기 전용 계정 — 업로드 대상 {len(to_push)}건을 건너뜁니다.")
        elif to_push:
            push_payload = {n: local_messages.get(n, "") for n in to_push}
            # updated_by는 방금 _resolve_profile_or_halt()가 검증한 current_user_id를
            # 그대로 쓴다 — 별도로 세션을 다시 조회하지 않으므로 "updated_by !=
            # current_user_id" 같은 불일치가 애초에 발생할 수 없다.
            self._log(f"[INFO] 업로드 준비 — updated_by {current_user_id[:8]}...")
            push_result = self._cloud.push_messages(
                push_payload, updated_by=current_user_id, device_id=self._device_id
            )
            if not push_result.success:
                self._handle_sync_failure(push_result)
                return
            self._log(f"[INFO] 클라우드 업로드 완료 — 메시지 {sorted(push_result.updated)}번")
            self._mark_synced_after_push(push_result, push_payload)
            uploaded_now = list(push_result.updated)
            if push_result.conflicts:
                conflicts = list(conflicts) + list(push_result.conflicts)
                self._update_conflict_flags(conflicts=push_result.conflicts, pending=[])

        unique_conflicts = sorted(set(conflicts))
        remaining_pending = sorted(set(pending) - set(uploaded_now))

        if unique_conflicts:
            self._set_status(CloudState.CONFLICT, detail=f"충돌 메시지: {unique_conflicts}", conflict_count=len(unique_conflicts))
            self._log(f"[경고] 실제 변경 충돌 감지 — 메시지 {unique_conflicts}번 (로컬값을 유지하고 자동 반영/업로드하지 않았습니다)")
        elif not can_write:
            self._set_status(CloudState.CONNECTED_READ_ONLY)
        else:
            self._set_status(CloudState.CONNECTED, pending_count=len(remaining_pending))

        if remaining_pending:
            self._log(f"[INFO] 로컬 변경 대기 — 메시지 {remaining_pending}번은 클라우드 업로드 전이므로 원격값을 적용하지 않았습니다.")

        self._log(
            f"[INFO] {'초기 ' if is_initial else ''}클라우드 동기화 완료 "
            f"(적용 {len(to_apply)}건, 업로드 {len(uploaded_now)}건, "
            f"대기 {len(remaining_pending)}건, 충돌 {len(unique_conflicts)}건)"
        )

    def _push_worker(self, payload: dict, reason: str) -> None:
        """request_push()/shutdown_flush()가 생성하는 유일한 업로드 스레드.

        _do_push() 완료 후 그 사이 새 요청이 들어왔으면(= _pending_push_reason이
        채워짐) 새 스레드를 만들지 않고 같은 스레드에서 최신 메시지를 다시 읽어
        한 번 더 업로드한다(원칙 7: "동시에 여러 push thread를 만들지 않는다").
        """
        while True:
            self._do_push(payload, reason)
            with self._push_lock:
                if self._pending_push_reason is not None:
                    reason = self._pending_push_reason
                    self._pending_push_reason = None
                else:
                    self._push_in_progress = False
                    return
            payload = dict(self._get_messages())
            self._log(f"[INFO] 최신 변경 후속 업로드 — reason={reason}")

    def _do_push(self, payload: dict, reason: str) -> None:
        """실제 업로드 1회 시도. 권한 확인 → push_messages() → 결과 반영."""
        profile = self._resolve_profile_or_halt()
        if profile is None:
            return
        if not profile.can_write:
            self._set_status(CloudState.CONNECTED_READ_ONLY)
            self._log("[INFO] 읽기 전용 계정 — 업로드를 시도하지 않습니다(로컬 저장은 유지됨).")
            return

        self._log(f"[INFO] 클라우드 업로드 시작 — reason={reason}, updated_by {profile.id[:8]}...")
        self._set_status(CloudState.SYNCING)
        result = self._cloud.push_messages(payload, updated_by=profile.id, device_id=self._device_id)
        if not result.success:
            self._handle_sync_failure(result)
            return

        self._mark_synced_after_push(result, payload)

        if result.conflicts:
            self._update_conflict_flags(conflicts=result.conflicts, pending=[])
            self._set_status(
                CloudState.CONFLICT, detail=f"충돌: {result.conflicts}",
                conflict_count=len(set(result.conflicts)),
            )
            self._log(f"[경고] 실제 변경 충돌 감지 — 메시지 {result.conflicts}번 (업로드 중 원격이 이미 달라져 있었습니다)")
        else:
            self._set_status(CloudState.CONNECTED)
            self._log(f"[INFO] 클라우드 업로드 완료 — reason={reason}, 메시지 {sorted(result.updated)}번")

    def _reconcile(
        self, local: dict, cloud: dict
    ) -> tuple[dict, list, list, list, list]:
        """메시지 번호별로 (적용 대상, 업로드 대상, 진짜 충돌, 대기, 동일) 5가지로 분류한다.

        판단 규칙:
          1. 클라우드만 존재                    → 로컬/UI에 적용 (REMOTE_APPLY)
          2. 로컬만 존재(비어있지 않음)          → 클라우드에 업로드
          3. 클라우드가 최신 + 로컬 dirty 아님    → 클라우드 적용 (REMOTE_APPLY)
          4. 로컬 dirty이지만 현재 로컬 텍스트가 원격과 이미 같음 → IDENTICAL
             (수렴 완료 — 충돌도 대기도 아님, 더 볼 것 없음)
          5. 로컬 dirty + (원격 버전이 앞서지 않음 OR 원격 텍스트가 last_synced_text와
             동일) → LOCAL_PENDING. 후자는 "원격 버전만 앞섰지 실제 내용은 내가
             마지막으로 확인한 그대로"인 경우 — 대개 내 자신의 이전 push가 아직
             로컬 상태에 반영되지 않았을 뿐인 정상 대기다. 업로드 후보(to_push)에도
             포함한다(실제 안전성은 CloudSyncService의 버전 CAS가 별도로 보장한다).
          6. 로컬 dirty + 원격 텍스트가 last_synced_text와도 다름 → 진짜 CONFLICT
             (자동 덮어쓰기/자동 업로드 금지)

        Phase 2E 후속: 예전에는 5/6을 "cloud_rec.version > state.version"만으로
        판정해, 내가 15분 대기 중 push한 내용이 아직 로컬에 반영 안 됐을 뿐인
        정상 상황도 무조건 충돌로 잘못 집계했다 — last_synced_text 비교를
        추가해 "원격이 실제로 다른 값으로 바뀌었는지"를 직접 확인한다.

        이 판정 로직 자체는 core/legacy_send_verification.py의 classify_message()로
        옮겨졌다(요구사항 1절 — 30초 재폴링을 전제로 한 부분을 이벤트 기반으로
        재설계) — 여기서는 이 메서드를 12개 메시지에 대해 반복 호출할 뿐이고,
        발송 직전 검증도 같은 함수를 메시지 1건에 대해 호출한다. 판정 규칙
        자체는 완전히 동일하다(로직 이동, 변경 없음).
        """
        to_apply: dict = {}
        to_push: list = []
        conflicts: list = []
        pending: list = []
        identical: list = []

        all_numbers = set(local.keys()) | set(cloud.keys()) | set(self._local_state.keys())
        for n in all_numbers:
            state = self._local_state.get(n, _LocalMessageState())
            cloud_rec = cloud.get(n)
            local_text = local.get(n, "")

            classification = classify_message(
                dirty=state.dirty, local_version=state.version, last_synced_text=state.last_synced_text,
                local_text=local_text,
                cloud_version=cloud_rec.version if cloud_rec is not None else None,
                cloud_text=cloud_rec.text if cloud_rec is not None else None,
            )

            if classification == MessageClassification.PUSH:
                to_push.append(n)
            elif classification == MessageClassification.REMOTE_APPLY:
                to_apply[n] = cloud_rec.text
            elif classification == MessageClassification.IDENTICAL:
                identical.append(n)
            elif classification == MessageClassification.LOCAL_PENDING:
                to_push.append(n)
                pending.append(n)
            elif classification == MessageClassification.CONFLICT:
                conflicts.append(n)
            # NOOP: 할 일 없음

        return to_apply, to_push, conflicts, pending, identical

    def _apply_to_ui_and_local(self, to_apply: dict, cloud_records: dict) -> None:
        """클라우드 값을 ControlPanel과 로컬 JSON에 반영한다."""
        self._applying_remote = True
        try:
            merged_messages = dict(self._get_messages())
            merged_messages.update(to_apply)

            self._apply_messages(to_apply)  # ControlPanel의 기존 setter/load 메서드(콜백)
            self._data.save_messages(merged_messages, self._local_file)

            for n, text in to_apply.items():
                rec = cloud_records.get(n)
                prev = self._local_state.get(n, _LocalMessageState())
                self._local_state[n] = _LocalMessageState(
                    version=rec.version if rec else prev.version,
                    updated_at=_now_iso(),
                    dirty=False,
                    last_text=text,
                    last_synced_text=text,  # 정상 pull 적용 완료 — last_synced 갱신 시점(원칙 3)
                    conflict=False,
                )
            self._save_local_state()

            self._notify_scheduler()  # AutoScheduler.notify_cloud_update() (콜백으로 주입됨)
            self._log(f"[INFO] 클라우드 변경 UI 적용 완료 — 메시지 {sorted(to_apply.keys())}번")
        finally:
            self._applying_remote = False

    def _mark_identical(self, numbers: list, local: dict, cloud_records: dict) -> None:
        """로컬과 원격이 결과적으로 같은 내용으로 수렴한 메시지의 상태를 정리한다.

        내용이 같으므로 어느 쪽으로도 적용/업로드할 것이 없다 — dirty/conflict를
        해제하고 last_synced를 현재 값으로 맞춰, 다음 폴링에서 불필요하게
        다시 검토되지 않도록 한다(원칙: 로컬과 remote가 완전히 동일함을 확인한
        경우 = last_synced 갱신 시점).
        """
        for n in numbers:
            rec = cloud_records.get(n)
            prev = self._local_state.get(n, _LocalMessageState())
            text = local.get(n, "")
            self._local_state[n] = _LocalMessageState(
                version=rec.version if rec else prev.version,
                updated_at=_now_iso(),
                dirty=False,
                last_text=text,
                last_synced_text=text,
                conflict=False,
            )
        if numbers:
            self._save_local_state()

    def _update_conflict_flags(self, conflicts: list, pending: list) -> None:
        """진짜 충돌/대기 상태를 영속화한다(15분 자동 업로드가 DB 요청 없이 참조).

        pending으로 재분류된 메시지는 이전에 conflict였더라도 해제한다 — 원격이
        내가 마지막으로 확인한 상태로 되돌아왔거나(드묾),애초에 내 자신의
        업로드 지연일 뿐이었던 경우를 자동으로 회복시킨다.
        """
        changed = False
        for n in conflicts:
            state = self._local_state.setdefault(n, _LocalMessageState())
            if not state.conflict:
                state.conflict = True
                changed = True
        for n in pending:
            state = self._local_state.setdefault(n, _LocalMessageState())
            if state.conflict:
                state.conflict = False
                changed = True
        if changed:
            self._save_local_state()

    def _mark_synced_after_push(self, push_result: SyncResult, pushed_messages: dict) -> None:
        """push 성공한 메시지의 dirty를 해제한다.

        CloudSyncService.push_messages()는 새 버전 숫자를 반환하지 않으므로(내부
        캐시에만 보관 — 공개 인터페이스를 바꾸지 않기 위해 그대로 둔다), 정확한
        버전 숫자는 다음 pull 때 확정한다. 그 사이 dirty=False로 표시해두면 다음
        polling에서 "클라우드가 최신"으로 판단해 방금 올린 값을 그대로 다시
        적용하는 무해한 자기 자신 재확인이 한 번 더 일어날 뿐, 데이터 손실은 없다.

        원칙 7(업로드 직렬화): 이 push가 시작된 뒤 완료되기 전에 같은 메시지가
        다시 로컬에서 수정됐다면(=_local_state[n].last_text가 이 push가 보낸
        값과 이미 달라져 있다면) dirty를 해제하지 않는다 — "오래된 업로드
        완료가 최신 dirty를 false로 만들면 안 된다"는 요구사항을 메시지별로
        정확히 만족시킨다(전역 revision 카운터 없이도 충분하다).

        last_synced_text는 still_current 여부와 무관하게 항상 "방금 성공적으로
        반영된 텍스트"로 갱신한다(원칙: 정상 push 완료 = last_synced 갱신 시점) —
        그 사이 로컬이 한 번 더 바뀌어 dirty가 유지되더라도, 다음 폴링에서
        "원격이 last_synced_text와 같다"를 정확히 판단할 수 있어야 LOCAL_PENDING과
        진짜 CONFLICT를 구분할 수 있다.
        """
        for n in push_result.updated:
            prev = self._local_state.get(n, _LocalMessageState())
            pushed_text = pushed_messages.get(n, "")
            still_current = prev.last_text == pushed_text
            self._local_state[n] = _LocalMessageState(
                version=prev.version,
                updated_at=_now_iso(),
                dirty=not still_current,
                last_text=prev.last_text,
                last_synced_text=pushed_text,
                conflict=False,
            )
        self._save_local_state()

    def _mark_dirty(self, messages: dict) -> None:
        """로컬 저장 시 실제로 값이 바뀐 메시지만 dirty로 표시한다."""
        now = _now_iso()
        changed = False
        for n, text in messages.items():
            state = self._local_state.setdefault(n, _LocalMessageState())
            if text != state.last_text:
                state.dirty = True
                state.updated_at = now
                state.last_text = text
                changed = True
        if changed:
            self._save_local_state()

    def _handle_sync_failure(self, result: SyncResult) -> None:
        if result.error_code == "connection_error":
            self._set_status(CloudState.OFFLINE, detail=result.error)
            self._log(f"[INFO] 클라우드 오프라인 — 로컬 모드로 계속 동작합니다 ({result.error})")
        elif result.error_code == "disabled":
            self._set_status(CloudState.NOT_CONFIGURED, detail=result.error)
        elif result.error_code == "permission_denied":
            # RLS(42501) 위반 — 순수 네트워크 문제와 다르다(원칙 D: 401/403을 네트워크
            # 오류와 구분). 대개는 _resolve_profile_or_halt()가 미리 걸러내지만,
            # 확인 시점과 실제 쓰기 시점 사이에 관리자가 권한을 바꾼 경쟁 상태 등
            # 드문 경우에 여기로 온다.
            self._set_status(CloudState.SYNC_FAILED, detail=result.error)
            self._log(f"[경고] 클라우드 권한 거부(네트워크 문제 아님) — 승인/역할 상태가 바뀌었을 수 있습니다 ({result.error})")
        else:
            self._set_status(CloudState.SYNC_FAILED, detail=result.error)
            self._log(f"[오류] 클라우드 동기화 실패 — 로컬 데이터는 그대로 유지됩니다 ({result.error})")

    def _set_status(
        self, state: CloudState, detail: Optional[str] = None,
        pending_count: int = 0, conflict_count: int = 0,
    ) -> None:
        info = CloudStatusInfo(state, detail, pending_count=pending_count, conflict_count=conflict_count)
        self._last_status = info
        self._status_fn(info)

    # ===== 내부: 로컬 동기화 상태 영속화 =====

    def _load_local_state(self) -> dict:
        if not os.path.exists(self._local_state_file):
            return {}
        try:
            with open(self._local_state_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {int(k): _LocalMessageState(**v) for k, v in raw.items()}
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            logger.warning(f"로컬 동기화 상태 파일 읽기 오류 — 빈 상태로 시작합니다: {e}")
            return {}

    def _save_local_state(self) -> None:
        data = {str(n): asdict(s) for n, s in self._local_state.items()}
        _atomic_write_json(data, self._local_state_file)
