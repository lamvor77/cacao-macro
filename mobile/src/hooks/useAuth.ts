// Supabase Auth(Google OAuth) — 기존 PC 프로그램과 동일한 app_users 승인 흐름을
// 그대로 재사용한다(새 사용자 테이블/가입 절차를 만들지 않는다, 요구사항 11절).

import { useCallback, useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "../supabaseClient";
import type { AppUserProfile, MobileRole } from "../types";
import { toMobileRole } from "../types";

interface AuthState {
  loading: boolean;
  session: Session | null;
  profile: AppUserProfile | null;
  role: MobileRole;
  error: string | null;
}

export function useAuth() {
  const [state, setState] = useState<AuthState>({
    loading: true,
    session: null,
    profile: null,
    role: "disabled",
    error: null,
  });

  const loadProfile = useCallback(async (session: Session | null) => {
    if (session === null) {
      setState({ loading: false, session: null, profile: null, role: "disabled", error: null });
      return;
    }
    const { data, error } = await supabase
      .from("app_users")
      .select("id,email,display_name,status,role")
      .eq("id", session.user.id)
      .limit(1)
      .maybeSingle();

    if (error) {
      setState({ loading: false, session, profile: null, role: "disabled", error: error.message });
      return;
    }
    const profile = (data as AppUserProfile) ?? null;
    setState({ loading: false, session, profile, role: toMobileRole(profile), error: null });
  }, []);

  useEffect(() => {
    let mounted = true;
    supabase.auth.getSession().then(({ data }) => {
      if (mounted) void loadProfile(data.session);
    });

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, session) => {
      if (mounted) void loadProfile(session);
    });

    return () => {
      mounted = false;
      subscription.subscription.unsubscribe();
    };
  }, [loadProfile]);

  const signInWithGoogle = useCallback(async () => {
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin },
    });
    if (error) {
      setState((s) => ({ ...s, error: error.message }));
    }
  }, []);

  const signOut = useCallback(async () => {
    await supabase.auth.signOut();
  }, []);

  const refreshProfile = useCallback(async () => {
    await loadProfile(state.session);
  }, [loadProfile, state.session]);

  return { ...state, signInWithGoogle, signOut, refreshProfile };
}
