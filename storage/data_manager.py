# JSON 저장/불러오기 모듈
# 단톡방 목록(이름+체크상태)과 메시지 1~12를 이름 지정 JSON 파일로 관리한다.
# 파일 손상, 권한 오류 등 모든 예외를 명시적으로 처리한다.

import json
import logging
import os
import shutil
import sys
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def default_room_list_filename(now: Optional[datetime] = None) -> str:
    """단톡방 목록 저장 대화상자의 기본 파일명을 만든다.

    예: 카카오톡_단톡방목록_20260721_153000.json
    now를 주입할 수 있게 해 시각에 의존하지 않고 단위 테스트할 수 있다.
    """
    now = now or datetime.now()
    return f"카카오톡_단톡방목록_{now.strftime('%Y%m%d_%H%M%S')}.json"


def get_storage_dir() -> str:
    """storage 디렉터리의 절대 경로를 반환한다 (EXE/개발 환경 모두 대응)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "storage")
    os.makedirs(path, exist_ok=True)
    return path


class DataManager:
    """단톡방 목록과 메시지를 JSON 파일로 저장/불러오기하는 클래스"""

    def __init__(self):
        self._storage_dir = get_storage_dir()

    # ===== 저장 =====

    def save(self, rooms: dict[str, bool], messages: dict[int, str], filepath: str) -> None:
        """단톡방 목록과 메시지를 지정된 파일에 저장한다.

        임시 파일에 먼저 쓴 후 교체하는 방식으로 저장 중 오류로 인한
        기존 파일 손상을 방지한다.

        Args:
            rooms:    {방이름: 체크상태}
            messages: {메시지번호(1~12): 텍스트}
            filepath: 저장할 파일의 절대 경로
        """
        data = {
            "rooms": [
                {"name": name, "checked": checked}
                for name, checked in rooms.items()
            ],
            "messages": {str(k): v for k, v in messages.items()},
        }
        self._atomic_write(data, filepath)

    def save_messages(self, messages: dict[int, str], filepath: str) -> None:
        """메시지만 지정된 파일에 저장한다 (단톡방 목록 저장과 별개).

        Args:
            messages: {메시지번호(1~12): 텍스트}
            filepath: 저장할 파일의 절대 경로
        """
        data = {"messages": {str(k): v for k, v in messages.items()}}
        self._atomic_write(data, filepath)

    def _atomic_write(self, data: dict, filepath: str) -> None:
        """임시 파일에 먼저 쓴 후 교체하는 방식으로 저장 중 오류로 인한
        기존 파일 손상을 방지한다."""
        tmp_path = filepath + ".tmp"
        try:
            # 임시 파일에 먼저 기록
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            # 기존 파일을 원자적으로 교체
            os.replace(tmp_path, filepath)
            logger.info(f"저장 완료: {filepath}")

        except PermissionError as e:
            logger.error(f"파일 쓰기 권한 오류: {filepath} — {e}")
            raise
        except Exception as e:
            logger.error(f"저장 오류: {filepath} — {e}")
            raise
        finally:
            # 임시 파일이 남아있으면 정리
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ===== 불러오기 =====

    def load(self, filepath: str) -> Optional[dict]:
        """지정된 파일에서 데이터를 불러온다.

        파일 손상(JSONDecodeError) 시 .corrupted 백업을 만들고 None을 반환한다.

        Returns:
            {"rooms": [...], "messages": {번호: 텍스트}} 또는 None
        """
        if not os.path.exists(filepath):
            logger.info(f"파일 없음: {filepath}")
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

        except json.JSONDecodeError as e:
            logger.error(f"JSON 파싱 오류 ({os.path.basename(filepath)}): {e}")
            # 손상된 파일을 .corrupted로 백업
            backup = filepath + ".corrupted"
            try:
                shutil.copy2(filepath, backup)
                logger.info(f"손상된 파일 백업: {backup}")
            except OSError:
                pass
            return None

        except PermissionError as e:
            logger.error(f"파일 읽기 권한 오류 ({os.path.basename(filepath)}): {e}")
            return None

        except Exception as e:
            logger.error(f"파일 불러오기 오류 ({os.path.basename(filepath)}): {e}")
            return None

        # messages 키를 str → int로 복원
        if "messages" in data:
            try:
                data["messages"] = {int(k): v for k, v in data["messages"].items()}
            except (ValueError, TypeError) as e:
                logger.warning(f"messages 키 변환 오류 — 기본값 사용: {e}")
                data["messages"] = {}

        logger.info(f"불러오기 완료: {filepath}")
        return data

    # ===== 파일 목록 조회 =====

    def list_saved_files(self) -> list[str]:
        """storage 디렉터리의 JSON 파일 경로 목록을 최신 순으로 반환한다."""
        try:
            files = [
                os.path.join(self._storage_dir, f)
                for f in os.listdir(self._storage_dir)
                if f.endswith(".json") and not f.endswith(".tmp")
            ]
            files.sort(key=os.path.getmtime, reverse=True)
            return files
        except OSError as e:
            logger.error(f"파일 목록 조회 오류: {e}")
            return []

    def get_storage_dir(self) -> str:
        return self._storage_dir

    def make_filepath(self, filename: str) -> str:
        """파일명으로 storage 디렉터리 내 절대 경로를 만든다."""
        # 파일명에 사용할 수 없는 문자 제거
        safe = "".join(c for c in filename if c not in r'\/:*?"<>|')
        if not safe:
            safe = "저장목록"
        if not safe.endswith(".json"):
            safe += ".json"
        return os.path.join(self._storage_dir, safe)
