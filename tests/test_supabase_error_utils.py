# services/supabase_error_utils.py 단위 테스트 — AdminService/SharedMessageService가
# 공유하는 APIError 필드 추출 및 user_id/project_ref 마스킹 유틸.
#
# 실행: python -m unittest tests.test_supabase_error_utils -v

import unittest

from postgrest.exceptions import APIError as PostgrestAPIError

from services.supabase_error_utils import (
    ApiErrorFields,
    extract_api_error_fields,
    project_ref_from_url,
    short_user_id,
)


class TestExtractApiErrorFields(unittest.TestCase):
    def test_all_fields_extracted(self):
        exc = PostgrestAPIError({
            "message": "PERMISSION_DENIED: 권한이 없습니다.",
            "code": "42501",
            "details": "일부 컬럼에 대한 권한이 없습니다.",
            "hint": "GRANT 문으로 권한을 부여하세요.",
        })
        fields = extract_api_error_fields(exc)
        self.assertIsInstance(fields, ApiErrorFields)
        self.assertEqual(fields.code, "42501")
        self.assertEqual(fields.message, "PERMISSION_DENIED: 권한이 없습니다.")
        self.assertEqual(fields.details, "일부 컬럼에 대한 권한이 없습니다.")
        self.assertEqual(fields.hint, "GRANT 문으로 권한을 부여하세요.")

    def test_missing_fields_default_to_none(self):
        exc = PostgrestAPIError({"message": "그냥 오류"})
        fields = extract_api_error_fields(exc)
        self.assertIsNone(fields.code)
        self.assertIsNone(fields.details)
        self.assertIsNone(fields.hint)
        self.assertEqual(fields.message, "그냥 오류")

    def test_non_apierror_exception_uses_str(self):
        fields = extract_api_error_fields(RuntimeError("일반 예외"))
        self.assertEqual(fields.message, "일반 예외")
        self.assertIsNone(fields.code)


class TestShortUserId(unittest.TestCase):
    def test_truncates_to_first_8_chars(self):
        self.assertEqual(short_user_id("11111111-2222-3333-4444-555555555555"), "11111111")

    def test_none_returns_unknown(self):
        self.assertEqual(short_user_id(None), "unknown")

    def test_empty_string_returns_unknown(self):
        self.assertEqual(short_user_id(""), "unknown")


class TestProjectRefFromUrl(unittest.TestCase):
    def test_extracts_ref_from_supabase_url(self):
        self.assertEqual(project_ref_from_url("https://kdyxxkltafeuucijiyzp.supabase.co"), "kdyxxkltafeuucijiyzp")

    def test_none_returns_unknown(self):
        self.assertEqual(project_ref_from_url(None), "unknown")

    def test_non_supabase_url_returns_unknown(self):
        self.assertEqual(project_ref_from_url("https://example.com"), "unknown")

    def test_url_never_leaks_query_string_or_key(self):
        # 잘못 조합된 URL(쿼리스트링에 키가 붙어 있는 경우 등)에서도 ref 외에는
        # 절대 반환하지 않는다 — 매치 실패 시 "unknown"으로 안전하게 처리된다.
        ref = project_ref_from_url("https://abcxyz.supabase.co/rest/v1/?apikey=SECRET_KEY_VALUE")
        self.assertEqual(ref, "abcxyz")
        self.assertNotIn("SECRET_KEY_VALUE", ref)


if __name__ == "__main__":
    unittest.main()
