// Supabase 클라이언트 — anon key만 사용한다(요구사항 11절). service_role key는
// 이 프로젝트 어디에도 존재하지 않는다(빌드 시 클라이언트 번들에 들어갈 값은
// VITE_ 접두사가 붙은 환경변수뿐이며, .env.example에도 anon key만 있다).

import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

export const isSupabaseConfigured = Boolean(url && anonKey);

if (!isSupabaseConfigured) {
  // 빌드 자체는 항상 성공해야 한다 — 실행 시점에만 화면에 설정 오류를 표시한다.
  // eslint-disable-next-line no-console
  console.warn("VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY가 설정되지 않았습니다.");
}

export const supabase = createClient(url ?? "", anonKey ?? "", {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
});
