import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// 내부 직원용 PWA — 홈 화면 추가가 가능하도록 기본 manifest만 설정한다(요구사항 5절).
// 외부 배포/스토어 등록 대상이 아니므로 오프라인 전체 캐싱(서비스워커 프리캐시)은
// 최소화한다 — 메시지 데이터는 항상 Supabase가 기준(Single Source of Truth)이며,
// 오래된 정적 자산이 캐시에 남아 UI가 서버와 어긋나 보이는 위험을 피한다.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.svg"],
      manifest: {
        name: "카카오톡 메시지 관리",
        short_name: "메시지 관리",
        description: "1~12번 발송 메시지를 모바일에서 확인/수정합니다(내부 직원 전용).",
        theme_color: "#1a1a2e",
        background_color: "#1a1a2e",
        display: "standalone",
        start_url: "/",
        icons: [
          { src: "icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png" },
        ],
      },
    }),
  ],
  server: {
    port: 5173,
  },
});
