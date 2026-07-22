# Supabase Realtime protocol 2.0 배열 메시지 호환 계층
# ([join_ref, ref, topic, event, payload] 형식)
#
# 문제: realtime==2.31.0(현재 최신 안정판, requirements.txt에 고정됨)의
# ServerMessageAdapter(realtime/message.py)는 객체 형식 메시지만 검증하는
# pydantic 모델만 정의돼 있다. 이 프로젝트가 접속하는 Supabase Realtime 서버는
# 접속 시 프로토콜 버전을 명시하지 않는데도(realtime 라이브러리의
# AsyncRealtimeClient.endpoint_url()이 실제로는 connect()에서 호출되지 않는
# 죽은 코드라 vsn 파라미터 자체가 전송되지 않음 — 소스로 직접 확인함) 서버가
# postgres_changes UPDATE 이벤트를 배열 형식으로 보내, realtime/_async/client.py의
# _listen()이 매번 ValidationError를 잡아 로그만 남기고 continue한다 — 즉
# 이벤트가 우리 콜백(on_postgres_changes)까지 절대 도달하지 못한다(연결 자체는
# 끊기지 않아 "실시간 연결됨" 상태 표시는 정상으로 보이는 조용한 실패).
#
# 해결: realtime 패키지(site-packages)는 전혀 수정하지 않는다. 대신
# AsyncRealtimeClient를 상속한 CompatAsyncRealtimeClient가 _listen()만
# 오버라이드해서, 원본 파서에 넘기기 전에 배열 형식이면 동등한 객체로
# 정규화한다. connect/reconnect/heartbeat/channel 관리는 전부 상속으로
# 그대로 재사용한다(오버라이드하지 않음).
#
# 주입 방법: supabase-py의 create_async_client()는 내부적으로
# AsyncClient._init_realtime_client()가 AsyncRealtimeClient(...)를 직접
# 생성하며, 이를 교체할 파라미터가 없다. 그렇다고 다른 모듈의 전역 이름을
# 잠깐 바꿔치기하는 방식은 쓰지 않는다(요구사항) — 대신 create_async_client()
# 호출이 끝난 뒤 AsyncClient가 이미 공개 속성으로 들고 있는 realtime_url/
# supabase_key/options.realtime을 그대로 읽어 우리 팩토리로 새 인스턴스를
# 만들고, self._client.realtime 속성 자체를 교체한다(services/
# realtime_message_sync_service.py 참고). AsyncClient.channel()/set_auth()
# 전파(_listen_to_auth_events)는 모두 self.realtime을 호출 시점에 속성으로
# 읽으므로(소스로 확인함), 교체 이후에는 항상 우리 인스턴스가 쓰인다 —
# create_async_client()가 만들었다가 버려지는 원래 AsyncRealtimeClient
# 인스턴스는 한 번도 connect()되지 않으므로 아무 부작용이 없다.
#
# 안전장치: REALTIME_PROTOCOL_COMPAT=false(.env)로 즉시 비활성화할 수 있다 —
# 이 경우 create_compat_realtime_client()가 원본 AsyncRealtimeClient를 그대로
# 반환해 이 파일이 존재하기 전과 완전히 동일하게 동작한다(코드 되돌리기 없이도
# 즉시 원상복구 가능).

import json
import logging
from typing import Any, Optional

import websockets
from pydantic import ValidationError
from realtime import AsyncRealtimeClient
from realtime.exceptions import NotConnectedError
from realtime.message import ServerMessageAdapter

from config.settings import IS_TEST_ENVIRONMENT

logger = logging.getLogger(__name__)

# 이 호환 계층이 실제로 소스를 읽고 복제한 realtime 버전. requirements.txt도
# 이 버전으로 고정돼 있다 — 설치된 버전이 다르면 _listen() 내부 구조가
# 달라졌을 수 있으므로 경고만 남기고 계속 진행한다(안전 실패: 호환 계층이
# 안 맞아도 최악의 경우 교체 전과 동일한 동작으로 남을 뿐, 새로운 오류를
# 만들지 않는다).
_TESTED_REALTIME_VERSION = "2.31.0"

_ARRAY_MESSAGE_LENGTH = 5


def _installed_realtime_version() -> Optional[str]:
    try:
        from realtime.version import __version__

        return __version__
    except Exception:
        return None


def normalize_realtime_message(raw: str) -> str:
    """raw가 [join_ref, ref, topic, event, payload] 형식의 JSON 배열이면 동등한
    JSON 객체 문자열로 변환한다. 그 외(이미 객체, JSON 파싱 실패, 배열이지만
    길이/타입이 예상과 다름)에는 raw를 그대로 반환한다 — 호출부가 원본
    ServerMessageAdapter.validate_json()에 그대로 넘기므로, 기존과 동일하게
    ValidationError로 처리되고 축약 로그만 남는다(요구사항 3절)."""
    try:
        parsed: Any = json.loads(raw)
    except (TypeError, ValueError):
        return raw

    if not isinstance(parsed, list):
        return raw
    if len(parsed) != _ARRAY_MESSAGE_LENGTH:
        return raw

    join_ref, ref, topic, event, payload = parsed
    if not isinstance(topic, str) or not isinstance(event, str):
        return raw
    if not isinstance(payload, dict):
        return raw
    if join_ref is not None and not isinstance(join_ref, str):
        return raw
    if ref is not None and not isinstance(ref, str):
        return raw

    return json.dumps(
        {"join_ref": join_ref, "ref": ref, "topic": topic, "event": event, "payload": payload}
    )


def _log_normalized_message(normalized_json: str) -> None:
    """APP_ENV=test에서만 호출된다(요구사항 10절). event/topic/message_no/revision만
    남기고 payload 본문·토큰·키는 절대 남기지 않는다(요구사항 4/5절)."""
    try:
        obj = json.loads(normalized_json)
        event = obj.get("event", "")
        topic = obj.get("topic", "")
        record = ((obj.get("payload") or {}).get("data") or {}).get("record") or {}
        message_no = record.get("message_no")
        revision = record.get("revision")
        logger.info(
            f"[Realtime 호환] 배열 메시지 정규화됨: event={event} topic={topic} "
            f"message_no={message_no} revision={revision}"
        )
    except Exception:
        logger.info("[Realtime 호환] 배열 메시지 정규화됨(요약 로그 생성 실패)")


class CompatAsyncRealtimeClient(AsyncRealtimeClient):
    """AsyncRealtimeClient의 명시적 서브클래스 — _listen()만 오버라이드한다.

    connect()/_reconnect()/_heartbeat()/channel()/close() 등 나머지는 전부
    상속으로 그대로 재사용한다(요구사항 9절 — 재연결/하트비트/채널 join/정리
    로직을 다시 구현하지 않음)."""

    async def _listen(self) -> None:
        if not self._ws_connection:
            raise NotConnectedError("_listen")

        try:
            async for msg in self._ws_connection:
                logger.debug(f"receive: {msg!r}")

                normalized = normalize_realtime_message(msg)
                if IS_TEST_ENVIRONMENT and normalized != msg:
                    _log_normalized_message(normalized)

                try:
                    message = ServerMessageAdapter.validate_json(normalized)
                except ValidationError as e:
                    logger.error(f"Unrecognized message format {normalized!r}\n{e}")
                    continue
                logger.debug(f"parsed message as {message!r}")
                if channel := self.channels.get(message.topic):
                    channel._handle_message(message)
        except websockets.exceptions.ConnectionClosedError as e:
            await self._on_connect_error(e)


def create_compat_realtime_client(
    url: str,
    token: Optional[str] = None,
    compat_enabled: bool = True,
    **options: Any,
) -> AsyncRealtimeClient:
    """RealtimeMessageSyncService가 create_async_client() 호출 직후 이 팩토리로
    만든 인스턴스를 self._client.realtime에 대입해 교체한다.

    compat_enabled=False(REALTIME_PROTOCOL_COMPAT=false)이면 원본
    AsyncRealtimeClient를 그대로 반환한다 — 이 파일이 없던 상태와 완전히
    동일하게 동작하는 즉시 비활성화 안전장치(요구사항)."""
    if not compat_enabled:
        logger.info("REALTIME_PROTOCOL_COMPAT=false — 프로토콜 호환 계층을 비활성화하고 원본 AsyncRealtimeClient를 사용합니다.")
        return AsyncRealtimeClient(url, token=token, **options)

    installed = _installed_realtime_version()
    if installed and installed != _TESTED_REALTIME_VERSION:
        logger.warning(
            f"realtime 패키지 버전({installed})이 이 호환 계층이 검증된 버전"
            f"({_TESTED_REALTIME_VERSION})과 다릅니다 — _listen() 내부 구조가 달라졌을 수 있으니 "
            "동작을 확인하세요."
        )

    return CompatAsyncRealtimeClient(url, token=token, **options)
