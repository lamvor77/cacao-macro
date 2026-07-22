# 로컬 storage 자동/수동 백업 서비스 (Release Candidate Sprint 1)
#
# 책임: storage/ 아래 "사용자 데이터"만 ZIP으로 백업하고, 무결성(SHA-256)을
# 검증하고, 필요하면 안전하게 복구한다. UI(gui/panels/backup_panel.py)와
# 완전히 분리되어 있어 위젯 없이 테스트할 수 있다.
#
# 절대 백업하지 않는 것 (원칙 — 이 목록에 있는 파일은 백업 ZIP에 넣지 않는다):
#   - .env, license_build.json, OAuth/세션 토큰(storage/cloud_sync/session.dat)
#   - 로그, __pycache__, 빌드 산출물, 임시/손상 파일(.tmp/.corrupted)
#
# 복구(restore_backup)는 항상 다음을 보장한다:
#   - 복구를 시작하기 전에 "현재" storage를 먼저 백업한다(pre_restore).
#   - 복구 도중 어느 단계에서 실패하든 기존 데이터를 절대 잃지 않는다 —
#     실패 시 원래 storage로 되돌린다(무슨 일이 있어도 데이터 유실 없음이
#     이 클래스의 최우선 불변조건이다).
#   - 복구 후 Supabase에 아무 것도 쓰지 않는다 — 클라우드와의 차이는 다음
#     시작 시 기존 CloudSyncCoordinator의 LOCAL_PENDING/CONFLICT 판정에
#     맡긴다(이 서비스가 그 로직을 새로 만들지 않는다).

import hashlib
import json
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

BACKUP_FORMAT_VERSION = 1
MANIFEST_FILENAME = "backup_manifest.json"
VALID_BACKUP_TYPES = ("auto", "manual", "pre_restore")
MAX_AUTO_BACKUPS = 30

# storage/cloud_sync/ 아래에서 백업 대상으로 허용하는 파일만 화이트리스트로
# 관리한다 — session.dat(OAuth 토큰)은 여기 없으므로 항상 제외된다.
_ALLOWED_CLOUD_SYNC_FILES = {"messages.json", "local_sync_state.json", "message_cache.json"}


class BackupError(Exception):
    """백업/복구 처리 중 발생한 오류(예상된 실패 — 호출부가 사용자에게 보여줄 수 있음)."""


@dataclass
class BackupFileEntry:
    relative_path: str
    size: int
    sha256: str


@dataclass
class BackupMetadata:
    format_version: int
    app_version: str
    created_at: str
    backup_type: str
    files: list = field(default_factory=list)  # list[BackupFileEntry]

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "app_version": self.app_version,
            "created_at": self.created_at,
            "backup_type": self.backup_type,
            "files": [asdict(f) for f in self.files],
        }

    @staticmethod
    def from_dict(data: dict) -> "BackupMetadata":
        return BackupMetadata(
            format_version=data.get("format_version", 0),
            app_version=data.get("app_version", ""),
            created_at=data.get("created_at", ""),
            backup_type=data.get("backup_type", ""),
            files=[BackupFileEntry(**f) for f in data.get("files", [])],
        )


@dataclass
class BackupRecord:
    """list_backups()가 반환하는 요약 정보 — UI/진단정보 화면에서 사용."""

    filename: str
    path: str
    created_at: str
    backup_type: str
    app_version: str
    size_bytes: int
    file_count: int
    validated: bool
    validation_error: str = ""


@dataclass
class BackupValidationResult:
    valid: bool
    reason: str = ""
    manifest: Optional[BackupMetadata] = None


@dataclass
class RestoreResult:
    success: bool
    error: str = ""
    pre_restore_backup_path: str = ""


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class BackupService:
    def __init__(
        self,
        storage_dir_fn: Callable[[], str],
        app_version: str,
        backup_dir: Optional[str] = None,
    ):
        """
        Args:
            storage_dir_fn: 현재 storage 디렉터리 절대경로를 반환하는 콜백
                (storage.data_manager.DataManager.get_storage_dir을 그대로 넘긴다 —
                이 서비스가 storage 경로 계산 로직을 중복 구현하지 않는다).
            app_version: 백업 메타데이터에 기록할 현재 앱 버전(config.version.APP_VERSION).
            backup_dir: 백업 저장 위치. 생략하면 storage_dir_fn()의 부모 폴더 아래
                backup/를 사용한다(license_build.json 등과 동일한 exe-인접 관례).
        """
        self._get_storage_dir = storage_dir_fn
        self._app_version = app_version
        if backup_dir is not None:
            self._backup_dir = backup_dir
        else:
            storage_parent = os.path.dirname(os.path.abspath(storage_dir_fn()))
            self._backup_dir = os.path.join(storage_parent, "backup")
        os.makedirs(self._backup_dir, exist_ok=True)

    def get_backup_dir(self) -> str:
        return self._backup_dir

    # ===== 백업 대상 파일 목록 =====

    def _collect_source_files(self) -> list:
        """(절대경로, zip 내부 상대경로) 목록. storage 최상위 *.json +
        storage/cloud_sync/의 화이트리스트 파일만 — 그 외(로그, 토큰, 임시
        파일 등)는 절대 포함하지 않는다."""
        storage_dir = self._get_storage_dir()
        results = []
        if not os.path.isdir(storage_dir):
            return results

        for name in sorted(os.listdir(storage_dir)):
            full = os.path.join(storage_dir, name)
            if os.path.isfile(full) and name.endswith(".json") and not name.endswith(".tmp"):
                results.append((full, f"storage/{name}"))

        cloud_sync_dir = os.path.join(storage_dir, "cloud_sync")
        if os.path.isdir(cloud_sync_dir):
            for name in sorted(os.listdir(cloud_sync_dir)):
                if name not in _ALLOWED_CLOUD_SYNC_FILES:
                    continue
                full = os.path.join(cloud_sync_dir, name)
                if os.path.isfile(full):
                    results.append((full, f"storage/cloud_sync/{name}"))

        return results

    # ===== 생성 =====

    def create_backup(self, backup_type: str = "manual") -> BackupRecord:
        """백업 ZIP을 만들고 자체 검증까지 통과한 뒤에만 최종 파일명으로 확정한다.

        임시 파일(.zip.tmp)에 먼저 쓰고, 검증에 성공해야 os.replace()로 최종
        이름으로 바꾼다 — 검증 실패 시 최종 파일이 아예 생기지 않는다("백업이
        불완전하면 완료로 처리하지 않는다").
        """
        if backup_type not in VALID_BACKUP_TYPES:
            raise BackupError(f"알 수 없는 백업 유형: {backup_type}")

        sources = self._collect_source_files()
        created_at = datetime.now().isoformat(timespec="seconds")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"backup-{backup_type}-{timestamp}.zip"
        final_path = os.path.join(self._backup_dir, filename)
        tmp_path = final_path + ".tmp"

        entries = []
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for abs_path, arc_path in sources:
                    sha = _sha256_of_file(abs_path)
                    size = os.path.getsize(abs_path)
                    entries.append(BackupFileEntry(relative_path=arc_path, size=size, sha256=sha))
                    zf.write(abs_path, arcname=arc_path)

                manifest = BackupMetadata(
                    format_version=BACKUP_FORMAT_VERSION,
                    app_version=self._app_version,
                    created_at=created_at,
                    backup_type=backup_type,
                    files=entries,
                )
                zf.writestr(MANIFEST_FILENAME, json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
        except OSError as e:
            self._safe_remove(tmp_path)
            raise BackupError(f"백업 파일 작성 실패: {e}")

        validation = self.validate_backup(tmp_path)
        if not validation.valid:
            self._safe_remove(tmp_path)
            raise BackupError(f"백업 검증 실패 — 완료로 처리하지 않습니다: {validation.reason}")

        os.replace(tmp_path, final_path)
        logger.info(f"백업 생성 완료: {filename} ({len(entries)}개 파일)")

        return BackupRecord(
            filename=filename,
            path=final_path,
            created_at=created_at,
            backup_type=backup_type,
            app_version=self._app_version,
            size_bytes=os.path.getsize(final_path),
            file_count=len(entries),
            validated=True,
        )

    def should_create_auto_backup_today(self) -> bool:
        """오늘 날짜의 auto 백업이 이미 있으면 False(하루 최대 1개 정책)."""
        today = date.today().isoformat()
        for record in self.list_backups():
            if record.backup_type != "auto":
                continue
            if record.created_at[:10] == today:
                return False
        return True

    # ===== 조회 =====

    def list_backups(self) -> list:
        """최신순으로 정렬된 백업 목록. 손상된 zip도 목록에는 표시하되
        validated=False로 구분한다(파일이 조용히 사라진 것처럼 보이지 않게)."""
        records = []
        if not os.path.isdir(self._backup_dir):
            return records

        for name in os.listdir(self._backup_dir):
            if not (name.startswith("backup-") and name.endswith(".zip")):
                continue
            path = os.path.join(self._backup_dir, name)
            records.append(self._describe_backup_file(path, name))

        records.sort(key=lambda r: r.created_at or "", reverse=True)
        return records

    def _describe_backup_file(self, path: str, filename: str) -> BackupRecord:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        try:
            with zipfile.ZipFile(path, "r") as zf:
                raw = zf.read(MANIFEST_FILENAME)
                manifest = BackupMetadata.from_dict(json.loads(raw))
            return BackupRecord(
                filename=filename, path=path, created_at=manifest.created_at,
                backup_type=manifest.backup_type, app_version=manifest.app_version,
                size_bytes=size, file_count=len(manifest.files), validated=True,
            )
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as e:
            mtime = ""
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            except OSError:
                pass
            return BackupRecord(
                filename=filename, path=path, created_at=mtime, backup_type="unknown",
                app_version="", size_bytes=size, file_count=0, validated=False,
                validation_error=type(e).__name__,
            )

    def get_latest_backup_status(self) -> Optional[BackupRecord]:
        backups = self.list_backups()
        return backups[0] if backups else None

    # ===== 검증 =====

    def validate_backup(self, path: str) -> BackupValidationResult:
        """ZIP 무결성 + manifest 존재 + 파일 수/SHA-256 일치를 모두 확인한다."""
        if not os.path.exists(path):
            return BackupValidationResult(False, reason="백업 파일이 존재하지 않습니다.")

        try:
            with zipfile.ZipFile(path, "r") as zf:
                bad_file = zf.testzip()
                if bad_file is not None:
                    return BackupValidationResult(False, reason=f"손상된 항목: {bad_file}")

                try:
                    raw = zf.read(MANIFEST_FILENAME)
                except KeyError:
                    return BackupValidationResult(False, reason="manifest가 없습니다.")

                manifest = BackupMetadata.from_dict(json.loads(raw))
                names_in_zip = set(zf.namelist())

                for entry in manifest.files:
                    if entry.relative_path not in names_in_zip:
                        return BackupValidationResult(False, reason=f"파일 누락: {entry.relative_path}")
                    data = zf.read(entry.relative_path)
                    if len(data) != entry.size:
                        return BackupValidationResult(False, reason=f"크기 불일치: {entry.relative_path}")
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha != entry.sha256:
                        return BackupValidationResult(False, reason=f"SHA-256 불일치: {entry.relative_path}")

                expected_data_files = names_in_zip - {MANIFEST_FILENAME}
                manifest_paths = {e.relative_path for e in manifest.files}
                if expected_data_files != manifest_paths:
                    return BackupValidationResult(False, reason="manifest와 실제 파일 목록이 다릅니다.")

                return BackupValidationResult(True, manifest=manifest)
        except (zipfile.BadZipFile, json.JSONDecodeError, OSError) as e:
            return BackupValidationResult(False, reason=f"백업 파일을 읽을 수 없습니다: {type(e).__name__}")

    # ===== 복구 =====

    def restore_backup(self, path: str) -> RestoreResult:
        """선택한 백업으로 storage를 교체한다. 실패하면 항상 원래 상태로 되돌린다.

        절차(스펙 10절과 동일한 순서):
          1) 대상 백업 무결성 검증
          2) format_version 호환성 확인
          3) 복구 직전 현재 storage를 pre_restore 백업으로 저장(실패 시 복구 중단)
          4) 임시 폴더에 압축 해제
          5) 압축 해제된 파일의 SHA-256 재확인
          6~8) 현재 storage를 임시 이름으로 옮기고, 새 데이터를 그 자리에 배치
          9) 배치된 JSON 파일들이 실제로 파싱 가능한지 읽기 검증
          10~11) 성공 시 이전 storage 정리 / 실패 시 이전 storage로 되돌림
          12) 결과 반환(재시작 안내는 UI 책임)
        """
        validation = self.validate_backup(path)
        if not validation.valid:
            return RestoreResult(False, error=f"백업 검증 실패: {validation.reason}")

        manifest = validation.manifest
        if manifest.format_version != BACKUP_FORMAT_VERSION:
            return RestoreResult(
                False,
                error=f"백업 형식 버전이 호환되지 않습니다(백업: {manifest.format_version}, 현재: {BACKUP_FORMAT_VERSION}).",
            )
        if manifest.app_version and manifest.app_version != self._app_version:
            logger.warning(
                f"백업 시점 앱 버전({manifest.app_version})이 현재 버전({self._app_version})과 다릅니다 — 계속 진행합니다."
            )

        try:
            pre_restore = self.create_backup(backup_type="pre_restore")
        except BackupError as e:
            return RestoreResult(False, error=f"복구 전 안전 백업 생성 실패 — 복구를 중단합니다: {e}")

        storage_dir = self._get_storage_dir()
        extract_dir = tempfile.mkdtemp(prefix="cacao_restore_")
        moved_aside_dir = storage_dir + f".pre_restore_tmp_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        try:
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(extract_dir)

            extracted_storage_dir = os.path.join(extract_dir, "storage")
            for entry in manifest.files:
                extracted_path = os.path.join(extract_dir, entry.relative_path)
                if not os.path.exists(extracted_path):
                    raise BackupError(f"압축 해제 결과에 파일이 없습니다: {entry.relative_path}")
                if _sha256_of_file(extracted_path) != entry.sha256:
                    raise BackupError(f"압축 해제 후 SHA-256이 일치하지 않습니다: {entry.relative_path}")

            if os.path.isdir(storage_dir):
                os.rename(storage_dir, moved_aside_dir)

            shutil.move(extracted_storage_dir, storage_dir)

            self._verify_restored_json_readable(storage_dir)

        except Exception as e:
            logger.error(f"복구 실패 — 기존 데이터로 되돌립니다: {type(e).__name__}: {e}")
            self._rollback_restore(storage_dir, moved_aside_dir)
            shutil.rmtree(extract_dir, ignore_errors=True)
            return RestoreResult(False, error=f"복구 실패 — 기존 데이터를 보존했습니다: {e}")

        shutil.rmtree(extract_dir, ignore_errors=True)
        if os.path.isdir(moved_aside_dir):
            shutil.rmtree(moved_aside_dir, ignore_errors=True)

        logger.info(f"복구 완료: {os.path.basename(path)} (복구 전 안전 백업: {pre_restore.filename})")
        return RestoreResult(success=True, pre_restore_backup_path=pre_restore.path)

    def _verify_restored_json_readable(self, storage_dir: str) -> None:
        """복구된 최상위 *.json이 실제로 파싱 가능한지 확인한다 — 실패하면 예외를
        던져 restore_backup()이 롤백하도록 한다."""
        for name in os.listdir(storage_dir):
            full = os.path.join(storage_dir, name)
            if os.path.isfile(full) and name.endswith(".json"):
                with open(full, "r", encoding="utf-8") as f:
                    json.load(f)

    def _rollback_restore(self, storage_dir: str, moved_aside_dir: str) -> None:
        """복구 도중 실패 시 원래 storage를 되살린다. 이 함수 자체는 예외를
        삼키지 않는다 — 롤백조차 실패하면(디스크 오류 등) 반드시 알아야 한다."""
        if os.path.isdir(storage_dir) and os.path.isdir(moved_aside_dir):
            shutil.rmtree(storage_dir, ignore_errors=True)
        if os.path.isdir(moved_aside_dir):
            os.rename(moved_aside_dir, storage_dir)

    # ===== 삭제/정리 =====

    def delete_backup(self, path: str) -> bool:
        """백업 폴더 내부의 파일만 삭제를 허용한다(경로 조작으로 다른 파일이
        지워지는 것을 방지)."""
        abs_backup_dir = os.path.abspath(self._backup_dir)
        abs_path = os.path.abspath(path)
        if os.path.commonpath([abs_backup_dir, abs_path]) != abs_backup_dir:
            raise BackupError("백업 폴더 밖의 파일은 삭제할 수 없습니다.")
        if not os.path.exists(abs_path):
            return False
        os.remove(abs_path)
        logger.info(f"백업 삭제: {os.path.basename(abs_path)}")
        return True

    def cleanup_old_backups(self) -> int:
        """auto 백업만 최근 MAX_AUTO_BACKUPS개로 유지한다(수동/pre_restore 백업은
        사용자가 화면에서 직접 관리 — 정책이 다름을 문서에 명시함)."""
        auto_backups = [r for r in self.list_backups() if r.backup_type == "auto"]
        if len(auto_backups) <= MAX_AUTO_BACKUPS:
            return 0
        # list_backups()는 최신순 정렬이므로, 뒤쪽(오래된 것)부터 삭제 대상이다.
        to_delete = auto_backups[MAX_AUTO_BACKUPS:]
        deleted = 0
        for record in to_delete:
            try:
                if self.delete_backup(record.path):
                    deleted += 1
            except BackupError as e:
                logger.warning(f"오래된 백업 정리 실패(무시): {e}")
        return deleted

    @staticmethod
    def _safe_remove(path: str) -> None:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
